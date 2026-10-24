"""Durable self-service erasure. No client can name another account to delete."""
from __future__ import annotations
import asyncio,hashlib,json,logging,os,re,uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse
import httpx
from datetime import datetime,timezone
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field,field_validator
import database,media_storage
from auth import get_verified_user_id,get_authenticated_user_id
from rate_limits import enforce_rate_limit

router=APIRouter(prefix='/api/privacy',tags=['privacy'])
log=logging.getLogger('nene-ai.privacy')

def digest(owner):return hashlib.sha256(str(uuid.UUID(str(owner))).encode()).hexdigest()
def lock_key(owner):return 'account-lifecycle:'+str(uuid.UUID(str(owner)))

async def initialize_schema():
    if database._pool is None:return
    await database.execute('''
      CREATE TABLE IF NOT EXISTS privacy_deleted_subjects (
        subject_digest TEXT PRIMARY KEY,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
      CREATE TABLE IF NOT EXISTS privacy_deletions (
        id UUID PRIMARY KEY,user_id UUID,subject_digest TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','processing','completed')),
        manifest JSONB NOT NULL DEFAULT '{}'::jsonb,auth_deleted BOOLEAN NOT NULL DEFAULT FALSE,
        not_before TIMESTAMPTZ NOT NULL DEFAULT NOW()+INTERVAL '2 minutes',
        lease_until TIMESTAMPTZ,lease_token UUID,attempts INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),completed_at TIMESTAMPTZ,last_error TEXT);
      CREATE TABLE IF NOT EXISTS privacy_provider_erasure (
        id UUID PRIMARY KEY,receipt_id UUID NOT NULL REFERENCES privacy_deletions(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,resource_id TEXT NOT NULL,kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','completed','manual_review')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(receipt_id,provider,resource_id,kind));
      CREATE TABLE IF NOT EXISTS privacy_payment_archive (
        reference TEXT PRIMARY KEY,provider TEXT NOT NULL,mode TEXT NOT NULL,
        amount_kobo BIGINT NOT NULL,currency TEXT NOT NULL,status TEXT NOT NULL,
        provider_transaction_id TEXT,created_at TIMESTAMPTZ NOT NULL,paid_at TIMESTAMPTZ,
        retain_until TIMESTAMPTZ NOT NULL DEFAULT NOW()+INTERVAL '7 years');
      CREATE TABLE IF NOT EXISTS privacy_enquiries (
        id UUID PRIMARY KEY,email TEXT NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL,fingerprint TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),status TEXT NOT NULL DEFAULT 'received');
      ALTER TABLE privacy_deleted_subjects ENABLE ROW LEVEL SECURITY;
      ALTER TABLE privacy_deletions ENABLE ROW LEVEL SECURITY;
      ALTER TABLE privacy_provider_erasure ENABLE ROW LEVEL SECURITY;
      ALTER TABLE privacy_payment_archive ENABLE ROW LEVEL SECURITY;
      ALTER TABLE privacy_enquiries ENABLE ROW LEVEL SECURITY;
      REVOKE ALL ON privacy_deleted_subjects,privacy_deletions,privacy_provider_erasure,privacy_payment_archive,privacy_enquiries FROM PUBLIC;
    ''')

def auth_project_matches():
    # Direct identity deletion is permitted only when Auth and the database are
    # demonstrably the same Supabase project. Never delete an unrelated auth DB.
    parsed=urlparse(os.getenv('AUTH_ISSUER','').rstrip('/'))
    if parsed.scheme!='https' or not parsed.hostname or not parsed.hostname.endswith('.supabase.co') or parsed.path!='/auth/v1':return False
    project=parsed.hostname.split('.')[0]
    try:
        from psycopg.conninfo import conninfo_to_dict
        connection=conninfo_to_dict(database.DATABASE_URL)
        return connection.get('host')==f'db.{project}.supabase.co' or connection.get('user','').endswith('.'+project)
    except Exception:return False

async def direct_auth_available():
    if not auth_project_matches():return False
    row=await database.fetch_one("""SELECT CASE WHEN to_regclass('auth.users') IS NOT NULL
        THEN has_table_privilege(current_user,'auth.users','DELETE') ELSE FALSE END AS allowed""",())
    return bool(row and row['allowed'])

def auth_admin_config():
    issuer=os.getenv('AUTH_ISSUER','').rstrip('/')
    parsed=urlparse(issuer)
    key=os.getenv('SUPABASE_SERVICE_ROLE_KEY','').strip()
    if not key or parsed.scheme!='https' or not parsed.hostname or not parsed.hostname.endswith('.supabase.co') or parsed.path!='/auth/v1' or parsed.port not in (None,443) or parsed.username or parsed.password:return None
    return issuer,key

async def capabilities():
    if database._pool is None:return {'account_deletion_available':False,'privacy_requests_available':False}
    try:auth_ready=bool(auth_admin_config()) or await direct_auth_available()
    except Exception:auth_ready=False
    try:media_storage._storage_config();storage_ready=True
    except Exception:storage_ready=False
    return {'account_deletion_available':auth_ready and storage_ready,'privacy_requests_available':True}

@router.get('/availability')
async def availability():return await capabilities()

# Fixed projections exclude passwords, session tokens, payment authorization
# URLs/codes, worker leases and provider credentials. Browser input never chooses
# a table, projection, owner or SQL fragment.
EXPORT_SECTIONS={
 'account':('users','id','id,email,display_name,avatar_url,status,created_at,updated_at'),
 'profile':('account_profiles','user_id','data,updated_at'),
 'projects':('projects','user_id','id,name,description,status,created_at,updated_at'),
 'characters':('characters','user_id','id,project_id,name,profile,created_at,updated_at'),
 'generations':('generations','user_id',"id,project_id,generation_type,status,request->>'prompt' AS prompt,credits_reserved,credits_charged,credits_refunded,created_at,updated_at,completed_at"),
 'plans_and_scripts':('director_plans','user_id','id,brief,status,plan,credit_cost,billing_state,created_at'),
 'stories':('story_projects','user_id','id,project_id,title,prompt,character_lock,status,created_at,updated_at'),
 'story_scenes':('story_scenes','user_id','id,story_id,scene_index,prompt,duration_seconds,generate_audio,sound_direction,status,created_at,updated_at'),
 'source_images':('media_assets','user_id','id,asset_type,content_type,size_bytes,created_at,deleted_at'),
 'source_recordings':('speech_assets','user_id','id,duration_seconds,billable_seconds,created_at'),
 'wallet':('wallets','user_id','available_credits,reserved_credits,lifetime_purchased,lifetime_consumed,created_at,updated_at'),
 'credit_history':('credit_ledger','user_id','id,generation_id,entry_type,amount,balance_after,created_at'),
 'purchases':('payment_orders','user_id','id,reference,provider,mode,package_id,amount_kobo,currency,credits,status,created_at,paid_at'),
 'agreements':('account_agreements','user_id','terms_version,refund_policy_version,terms_snapshot,accepted_at'),
 'support_reports':('support_reports','user_id','id,kind,message,rating,status,created_at'),
}
EXPORT_SECTION_LIMIT=500
EXPORT_MAX_BYTES=8*1024*1024

async def export_account_records(owner):
    sections={};unavailable=[]
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            await conn.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))',(lock_key(owner),))
            state=await (await conn.execute('SELECT status FROM users WHERE id=%s',(owner,))).fetchone()
            if not state or state['status']!='active':raise HTTPException(403,'This account is no longer active.')
            for name,(table,owner_column,columns) in EXPORT_SECTIONS.items():
                exists=await (await conn.execute('SELECT to_regclass(%s) AS name',('public.'+table,))).fetchone()
                if not exists['name']:unavailable.append(name);continue
                rows=await (await conn.execute(f'SELECT {columns} FROM {table} WHERE {owner_column}=%s LIMIT %s',(owner,EXPORT_SECTION_LIMIT+1))).fetchall()
                if len(rows)>EXPORT_SECTION_LIMIT:
                    raise HTTPException(413,'This account needs a larger export. Use Privacy help to request a complete copy; no partial file was downloaded.')
                sections[name]=rows
    data=jsonable_encoder({'format':'nene-ai-account-records-v1','exported_at':datetime.now(timezone.utc),'records':sections,'unavailable_sections':unavailable,
       'scope':'Server-held account records. Download video, audio and source files separately from their creation screens. Browser-only drafts, security logs and service-provider copies are not included. Use Privacy help for additional data requests.'})
    if len(json.dumps(data).encode())>EXPORT_MAX_BYTES:
        raise HTTPException(413,'This account needs a larger export. Use Privacy help to request a complete copy; no partial file was downloaded.')
    return data

@router.get('/account-export')
async def account_export(request:Request,owner:str=Depends(get_authenticated_user_id)):
    await enforce_rate_limit(request,bucket='privacy-export',limit=3,window_seconds=3600)
    if database._pool is None:raise HTTPException(503,'Account export is temporarily unavailable.')
    data=await export_account_records(owner)
    return JSONResponse(data,headers={'Cache-Control':'no-store, private','Pragma':'no-cache','Content-Disposition':'attachment; filename="nene-ai-account-records.json"','X-Content-Type-Options':'nosniff'})

class DeleteRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id:uuid.UUID
    confirm_delete:bool=Field(strict=True)
    @field_validator('confirm_delete')
    @classmethod
    def affirmative(cls,value):
        if value is not True:raise ValueError('Confirm permanent account deletion.')
        return value

class Enquiry(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    request_id:uuid.UUID
    email:str=Field(min_length=5,max_length=254)
    kind:str=Field(pattern='^(privacy|deletion_access_help)$')
    message:str=Field(min_length=10,max_length=3000)
    @field_validator('email')
    @classmethod
    def email_address(cls,value):
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value):raise ValueError('Enter a valid contact email.')
        return value.lower()

@router.post('/enquiries',status_code=201)
async def enquiry(body:Enquiry,request:Request):
    await enforce_rate_limit(request,bucket='privacy-contact',limit=3,window_seconds=3600)
    if database._pool is None:raise HTTPException(503,'Privacy requests are temporarily unavailable.')
    fingerprint=hashlib.sha256(json.dumps({'email':body.email,'kind':body.kind,'message':body.message},sort_keys=True).encode()).hexdigest()
    old=await database.fetch_one('SELECT fingerprint FROM privacy_enquiries WHERE id=%s',(str(body.request_id),))
    if old and old['fingerprint']!=fingerprint:raise HTTPException(409,'This receipt belongs to a different request. Submit a new request.')
    stored=await database.fetch_one('''INSERT INTO privacy_enquiries(id,email,kind,message,fingerprint) VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT(id) DO UPDATE SET id=EXCLUDED.id RETURNING fingerprint''',(str(body.request_id),body.email,body.kind,body.message,fingerprint))
    if stored['fingerprint']!=fingerprint:raise HTTPException(409,'This receipt belongs to a different request. Submit a new request.')
    return {'received':True,'receipt_id':str(body.request_id),'status':'received','account_deleted':False}

async def request_deletion(owner,request_id,email=None):
    owner=str(uuid.UUID(str(owner)));subject=digest(owner)
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('deletion-request:'+owner,))
            old=await (await conn.execute('SELECT id::text,status FROM privacy_deletions WHERE subject_digest=%s',(subject,))).fetchone()
            if old:return old
            await conn.execute("INSERT INTO users(id,status) VALUES (%s,'deleting') ON CONFLICT(id) DO UPDATE SET status='deleting',updated_at=NOW()",(owner,))
            await conn.execute('INSERT INTO privacy_deleted_subjects(subject_digest) VALUES (%s) ON CONFLICT DO NOTHING',(subject,))
            if email:
                await conn.execute('DELETE FROM privacy_enquiries WHERE lower(email)=%s',(email.lower(),))
            row=await (await conn.execute('''INSERT INTO privacy_deletions(id,user_id,subject_digest)
                VALUES (%s,%s,%s) RETURNING id::text,status''',(str(request_id),owner,subject))).fetchone()
            return row

@router.post('/account-deletion',status_code=202)
async def delete_account(body:DeleteRequest,request:Request,owner:str=Depends(get_verified_user_id)):
    await enforce_rate_limit(request,bucket='account-deletion',limit=6,window_seconds=3600)
    if database._pool is None:raise HTTPException(503,'Account deletion is temporarily unavailable.')
    prior=await database.fetch_one('SELECT id::text,status FROM privacy_deletions WHERE subject_digest=%s',(digest(owner),))
    if prior:return {'receipt_id':prior['id'],'status':prior['status']}
    if not (await capabilities())['account_deletion_available']:
        raise HTTPException(503,'Automatic account deletion is temporarily unavailable. Use Privacy help to submit an access or deletion request. Your account has not been changed.')
    try:row=await request_deletion(owner,body.request_id,getattr(request.state,'auth_claims',{}).get('email'))
    except Exception:
        log.exception('Deletion request could not be recorded')
        raise HTTPException(503,'The request could not be confirmed. Retry the same deletion request.')
    return {'receipt_id':row['id'],'status':row['status']}

@router.get('/deletions/{receipt_id}')
async def deletion_status(receipt_id:uuid.UUID,request:Request):
    # An unguessable receipt reveals state only, never identity or content.
    if database._pool is None:raise HTTPException(503,'Deletion status is temporarily unavailable.')
    await enforce_rate_limit(request,bucket='deletion-receipt',limit=30,window_seconds=60)
    row=await database.fetch_one('''SELECT status,created_at,completed_at FROM privacy_deletions WHERE id=%s''',(str(receipt_id),))
    if not row:raise HTTPException(404,'Deletion receipt not found.')
    pending=await database.fetch_one("SELECT count(*) AS n FROM privacy_provider_erasure WHERE receipt_id=%s AND status!='completed'",(str(receipt_id),))
    return {'receipt_id':str(receipt_id),**row,'provider_follow_up_pending':bool(pending and pending['n'])}

@asynccontextmanager
async def worker_guard(owner):
    if not owner:yield False;return
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))',(lock_key(owner),))
            row=await (await conn.execute('SELECT status FROM users WHERE id=%s',(owner,))).fetchone()
            yield bool(row and row['status']=='active')

async def ensure_active_upload(owner):
    row=await database.fetch_one('SELECT status FROM users WHERE id=%s',(owner,))
    if not row or row['status']!='active':raise HTTPException(403,'This account is being deleted. The upload was not saved.')

async def manifest_for(owner):
    jobs=await database.fetch_all('SELECT id::text,provider,provider_job_id,generation_type FROM generations WHERE user_id=%s',(owner,))
    assemblies=await database.fetch_all('SELECT id::text FROM story_assembly_jobs WHERE user_id=%s',(owner,))
    return {'generation_ids':[j['id'] for j in jobs]+[a['id'] for a in assemblies],
        'providers':[{'provider':j['provider'],'resource_id':j['provider_job_id'],'kind':j['generation_type']} for j in jobs if j.get('provider_job_id')]}

def owned_prefixes(owner,manifest):
    owner=str(uuid.UUID(owner))
    return [f'source-images/{owner}/',f'source-speech/{owner}/']+[f'generations/{uuid.UUID(i)}/' for i in manifest.get('generation_ids',[])]

def purge_private_prefixes(owner,manifest):
    client=media_storage._client();cfg=media_storage._storage_config()
    for prefix in owned_prefixes(owner,manifest):
        # Delete only structural prefixes derived from owner-scoped database IDs.
        while True:
            page=client.list_objects_v2(Bucket=cfg['bucket'],Prefix=prefix,MaxKeys=500)
            objects=page.get('Contents',[])
            if not objects:break
            keys=[{'Key':o['Key']} for o in objects]
            if any(not k['Key'].startswith(prefix) for k in keys):raise RuntimeError('storage_prefix_mismatch')
            result=client.delete_objects(Bucket=cfg['bucket'],Delete={'Objects':keys,'Quiet':True})
            if result.get('Errors'):raise RuntimeError('private_media_delete_incomplete')

async def delete_identity(owner):
    if await direct_auth_available():
        # Supabase documents direct auth.users deletion; never alter auth schema.
        await database.execute('DELETE FROM auth.users WHERE id=%s',(owner,));return
    config=auth_admin_config()
    if not config:raise RuntimeError('identity_deletion_not_configured')
    base,key=config
    async with httpx.AsyncClient(timeout=30,follow_redirects=False,trust_env=False) as client:
        response=await client.delete(base+'/admin/users/'+str(uuid.UUID(owner)),headers={'Authorization':'Bearer '+key,'apikey':key})
    if response.status_code not in {200,204,404}:raise RuntimeError('identity_deletion_unconfirmed')

async def erase_provider(task):
    provider,resource,kind=task['provider'],task['resource_id'],task['kind']
    if provider!='magic-hour':return 'manual_review'
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',resource):return 'manual_review'
    key=os.getenv('MAGIC_HOUR_API_KEY','').strip()
    if not key:return 'manual_review'
    collection='audio-projects' if kind=='text-to-speech' else 'video-projects'
    async with httpx.AsyncClient(timeout=20,follow_redirects=False,trust_env=False) as client:
        response=await client.delete('https://api.magichour.ai/v1/'+collection+'/'+resource,headers={'Authorization':'Bearer '+key})
    if response.status_code in {200,204,404}:return 'completed'
    return 'manual_review'

async def process_deletion(job):
    owner=job['user_id'];receipt=job['id'];token=job['lease_token']
    async with database._pool.connection() as guard:
        async with guard.transaction():
            locked=await (await guard.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked',(lock_key(owner),))).fetchone()
            if not locked['locked']:raise RuntimeError('account_work_finishing')
            manifest=job.get('manifest') or {}
            if not manifest:
                manifest=await manifest_for(owner)
                await database.execute('UPDATE privacy_deletions SET manifest=%s::jsonb WHERE id=%s AND lease_token=%s',(json.dumps(manifest),receipt,token))
            await asyncio.to_thread(purge_private_prefixes,owner,manifest)
            for task in manifest['providers']:
                await database.execute('''INSERT INTO privacy_provider_erasure(id,receipt_id,provider,resource_id,kind)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(str(uuid.uuid4()),receipt,task['provider'],task['resource_id'],task['kind']))
            # Retain only minimal transaction facts; no profile, email, prompts,
            # authorization URLs, card data, user ID or credit balance is archived.
            async with database._pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute('''INSERT INTO privacy_payment_archive(reference,provider,mode,amount_kobo,currency,status,provider_transaction_id,created_at,paid_at)
                        SELECT reference,provider,mode,amount_kobo,currency,status,provider_transaction_id,created_at,paid_at
                        FROM payment_orders WHERE user_id=%s ON CONFLICT(reference) DO NOTHING''',(owner,))
                    await conn.execute('DELETE FROM provider_costs WHERE generation_id IN (SELECT id FROM generations WHERE user_id=%s)',(owner,))
                    await conn.execute('DELETE FROM generation_idempotency WHERE generation_id IN (SELECT id FROM generations WHERE user_id=%s)',(owner,))
                    # generations.user_id is SET NULL, so explicitly erase it,
                    # including prompts/results, before deleting the user row.
                    await conn.execute('DELETE FROM generations WHERE user_id=%s',(owner,))
                    await conn.execute('DELETE FROM users WHERE id=%s',(owner,))
            if not job.get('auth_deleted'):
                await delete_identity(owner)
                await database.execute('UPDATE privacy_deletions SET auth_deleted=TRUE WHERE id=%s AND lease_token=%s',(receipt,token))
            await database.execute("UPDATE privacy_deletions SET status='completed',user_id=NULL,manifest='{}'::jsonb,lease_until=NULL,lease_token=NULL,last_error=NULL,completed_at=NOW() WHERE id=%s AND lease_token=%s",(receipt,token))

async def run_once():
    async with database._pool.connection() as conn:
        async with conn.transaction():
            row=await (await conn.execute("""SELECT id::text,user_id::text,manifest,auth_deleted FROM privacy_deletions
                WHERE not_before<=NOW() AND (status='queued' OR (status='processing' AND lease_until<NOW()))
                ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED""")).fetchone()
            if row:
                token=str(uuid.uuid4());row['lease_token']=token
                await conn.execute("UPDATE privacy_deletions SET status='processing',lease_until=NOW()+INTERVAL '10 minutes',lease_token=%s,attempts=attempts+1 WHERE id=%s",(token,row['id']))
    if row:
        try:await process_deletion(row)
        except Exception:
            # Stable codes only. Retry retained manifests after storage/auth failure.
            log.exception('Account deletion needs retry (receipt=%s)',row['id'])
            await database.execute("UPDATE privacy_deletions SET status='queued',lease_until=NULL,lease_token=NULL,last_error='cleanup_retry',not_before=NOW()+INTERVAL '1 minute' WHERE id=%s AND lease_token=%s",(row['id'],row['lease_token']))
    tasks=await database.fetch_all("SELECT id::text,provider,resource_id,kind FROM privacy_provider_erasure WHERE status='pending' ORDER BY created_at LIMIT 5",())
    for task in tasks:
        try:state=await erase_provider(task)
        except Exception:continue
        await database.execute('UPDATE privacy_provider_erasure SET status=%s WHERE id=%s',(state,task['id']))
    await database.execute("DELETE FROM privacy_deletions d WHERE status='completed' AND completed_at<NOW()-INTERVAL '1 year' AND NOT EXISTS (SELECT 1 FROM privacy_provider_erasure p WHERE p.receipt_id=d.id AND p.status!='completed')",())
    await database.execute("DELETE FROM privacy_provider_erasure WHERE status='completed' AND created_at<NOW()-INTERVAL '90 days'",())
    await database.execute('DELETE FROM privacy_payment_archive WHERE retain_until<NOW()',())
    await database.execute("DELETE FROM privacy_enquiries WHERE created_at<NOW()-INTERVAL '90 days' AND status='closed'",())

async def cleanup_loop():
    while True:
        try:await run_once()
        except asyncio.CancelledError:raise
        except Exception:log.exception('Privacy cleanup will retry')
        await asyncio.sleep(20)
