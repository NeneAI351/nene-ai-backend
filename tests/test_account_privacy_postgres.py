"""Disposable PostgreSQL accounts only: erasure, recovery and billing boundaries."""
import asyncio,os,uuid,json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest,pytest_asyncio
from fastapi import HTTPException
from starlette.requests import Request
import database,account_privacy as privacy,auth,generation_outbox,payments,story_store,speech_assets,account_profile
from paid_fixtures import fund_paid

@pytest_asyncio.fixture(scope='session')
async def privacy_db():
    if not os.getenv('DATABASE_URL'):pytest.skip('DATABASE_URL required')
    if database._pool is None:await database.start_database()
    await database.initialize_schema();await payments.initialize_payment_schema();await generation_outbox.initialize_outbox_schema()
    await story_store.initialize_story_schema();await speech_assets.initialize_schema();await account_profile.initialize_profile_schema();await privacy.initialize_schema()
    # This is the isolated CI PostgreSQL service, never a real Supabase project.
    await database.execute('CREATE SCHEMA IF NOT EXISTS auth; CREATE TABLE IF NOT EXISTS auth.users(id UUID PRIMARY KEY,email TEXT)',())
    yield

async def make_account():
    user,gen,asset,story,assembly=[str(uuid.uuid4()) for _ in range(5)]
    await database.execute('INSERT INTO users(id,email) VALUES (%s,%s)',(user,f'{user}@erase.invalid'))
    await database.execute('INSERT INTO auth.users(id,email) VALUES (%s,%s)',(user,f'{user}@erase.invalid'))
    await fund_paid(user,'500')
    await database.execute("INSERT INTO account_profiles(user_id,data) VALUES (%s,%s::jsonb)",(user,json.dumps({'display_name':'PRIVATE_NAME','photo':'PRIVATE_PHOTO'})))
    await database.execute("INSERT INTO generations(id,user_id,provider,generation_type,status,request,result) VALUES (%s,%s,'ltx','text-to-video','succeeded',%s::jsonb,%s::jsonb)",(gen,user,json.dumps({'prompt':'PRIVATE_PROMPT'}),json.dumps({'object_key':f'generations/{gen}/video.mp4'})))
    await database.execute("INSERT INTO generation_idempotency(id,idempotency_key,request_fingerprint,response,generation_id) VALUES (%s,%s,'private-fingerprint',%s::jsonb,%s)",(str(uuid.uuid4()),'legacy:'+gen,json.dumps({'private_result':'PRIVATE_RETRY_RESULT'}),gen))
    await database.execute("INSERT INTO media_assets(id,user_id,asset_type,object_key,content_type,size_bytes) VALUES (%s,%s,'source_image',%s,'image/jpeg',10)",(asset,user,f'source-images/{user}/{asset}.jpg'))
    await database.execute('INSERT INTO speech_assets(id,user_id,object_key,duration_seconds,billable_seconds) VALUES (%s,%s,%s,3,3)',(gen,user,f'source-speech/{user}/{gen}.wav'))
    await database.execute("INSERT INTO story_projects(id,user_id,title,prompt) VALUES (%s,%s,'PRIVATE_TITLE','PRIVATE_STORY')",(story,user))
    await database.execute("INSERT INTO story_assembly_jobs(id,story_id,user_id,status,idempotency_key) VALUES (%s,%s,%s,'queued',%s)",(assembly,story,user,'assembly:'+assembly))
    return user,gen,asset,assembly

@pytest.fixture(autouse=True)
def simulate_same_supabase_project(monkeypatch):
    monkeypatch.setattr(privacy,'auth_project_matches',lambda:True)

async def claim(owner):
    receipt=str(uuid.uuid4());first=await privacy.request_deletion(owner,receipt)
    again=await privacy.request_deletion(owner,str(uuid.uuid4()));assert first['id']==again['id']
    token=str(uuid.uuid4())
    await database.execute("UPDATE privacy_deletions SET status='processing',lease_token=%s WHERE id=%s",(token,receipt))
    return await database.fetch_one('SELECT id::text,user_id::text,manifest,auth_deleted,lease_token::text FROM privacy_deletions WHERE id=%s',(receipt,))

@pytest.mark.asyncio
async def test_full_erasure_preserves_other_accounts_and_blocks_old_tokens(privacy_db,monkeypatch):
    owner,gen,asset,assembly=await make_account();other,other_gen,_,_=await make_account()
    job=await claim(owner);removed=[]
    monkeypatch.setattr(privacy,'purge_private_prefixes',lambda user,manifest:removed.extend(privacy.owned_prefixes(user,manifest)))
    await privacy.process_deletion(job)
    assert await database.fetch_one('SELECT id FROM users WHERE id=%s',(owner,)) is None
    assert await database.fetch_one('SELECT id FROM auth.users WHERE id=%s',(owner,)) is None
    assert await database.fetch_one('SELECT id FROM generations WHERE id=%s',(gen,)) is None
    assert await database.fetch_one('SELECT id FROM generations WHERE id=%s',(other_gen,))
    assert await database.fetch_one('SELECT id FROM generation_idempotency WHERE idempotency_key=%s',('legacy:'+gen,)) is None
    assert await database.fetch_one('SELECT id FROM generation_idempotency WHERE idempotency_key=%s',('legacy:'+other_gen,))
    assert f'generations/{gen}/' in removed and f'generations/{assembly}/' in removed
    assert f'generations/{other_gen}/' not in removed
    state=await database.fetch_one('SELECT user_id,status,manifest FROM privacy_deletions WHERE id=%s',(job['id'],))
    assert state['user_id'] is None and state['manifest']=={} and state['status']=='completed'
    archive=await database.fetch_all('SELECT * FROM privacy_payment_archive',())
    assert archive and 'PRIVATE_PROMPT' not in json.dumps(archive,default=str) and 'PRIVATE_NAME' not in json.dumps(archive,default=str)
    assert all('email' not in row and 'user_id' not in row for row in archive)
    monkeypatch.setenv('AUTH_JWKS_URL','https://identity.test/keys');monkeypatch.setenv('AUTH_ISSUER','https://identity.test');monkeypatch.setenv('AUTH_AUDIENCE','authenticated')
    monkeypatch.setattr(auth,'_jwks_client',lambda url:SimpleNamespace(get_signing_key_from_jwt=lambda token:SimpleNamespace(key='unit')))
    monkeypatch.setattr(auth.jwt,'decode',lambda *args,**kwargs:{'sub':owner,'exp':9999999999})
    monkeypatch.setattr(auth.database,'database_configured',lambda:True)
    request=Request({'type':'http','headers':[(b'authorization',b'Bearer unit')],'method':'GET','path':'/'})
    with pytest.raises(HTTPException) as exc:await auth.get_authenticated_user_id(request)
    assert exc.value.status_code==403
    assert await database.fetch_one('SELECT id FROM users WHERE id=%s',(owner,)) is None

@pytest.mark.asyncio
async def test_auth_cleanup_failure_keeps_manifest_and_restarts_safely(privacy_db,monkeypatch):
    owner,gen,_,_=await make_account();job=await claim(owner);original=privacy.delete_identity
    monkeypatch.setattr(privacy,'purge_private_prefixes',lambda *args:None)
    monkeypatch.setattr(privacy,'delete_identity',AsyncMock(side_effect=RuntimeError('temporary')))
    with pytest.raises(RuntimeError):await privacy.process_deletion(job)
    saved=await database.fetch_one('SELECT id::text,user_id::text,manifest,auth_deleted,lease_token::text,status FROM privacy_deletions WHERE id=%s',(job['id'],))
    assert saved['status']=='processing' and gen in saved['manifest']['generation_ids']
    assert await database.fetch_one('SELECT id FROM users WHERE id=%s',(owner,)) is None
    monkeypatch.setattr(privacy,'delete_identity',original)
    await privacy.process_deletion(saved)
    final=await database.fetch_one('SELECT status,user_id,manifest FROM privacy_deletions WHERE id=%s',(job['id'],))
    assert final['status']=='completed' and final['user_id'] is None and final['manifest']=={}

@pytest.mark.asyncio
async def test_late_verified_payment_never_recreates_or_credits_deleted_account(privacy_db,monkeypatch):
    owner,_,_,_=await make_account();job=await claim(owner)
    order=await database.fetch_one('SELECT reference,provider_transaction_id,amount_kobo,mode FROM payment_orders WHERE user_id=%s',(owner,))
    monkeypatch.setattr(privacy,'purge_private_prefixes',lambda *args:None)
    await privacy.process_deletion(job)
    result=await payments._fulfill_verified_transaction({'reference':order['reference'],'id':order['provider_transaction_id'],'amount':order['amount_kobo'],'currency':'NGN','status':'success','domain':order['mode']})
    assert result['credited'] is False and result['status']=='account_deleted'
    assert await database.fetch_one('SELECT user_id FROM wallets WHERE user_id=%s',(owner,)) is None

@pytest.mark.asyncio
async def test_delayed_signin_waits_for_erasure_and_cannot_recreate_user(privacy_db,monkeypatch):
    owner=str(uuid.uuid4())
    monkeypatch.setattr(auth,'get_verified_user_id',AsyncMock(return_value=owner))
    monkeypatch.setattr(auth.database,'database_configured',lambda:True)
    request=Request({'type':'http','headers':[],'method':'GET','path':'/'})
    task=None
    try:
        async with database._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(privacy.lock_key(owner),))
                task=asyncio.create_task(auth.get_authenticated_user_id(request))
                async def wait_until_fenced():
                    while not task.done():
                        waiting=await database.fetch_one("SELECT count(*) AS n FROM pg_locks WHERE locktype='advisory' AND NOT granted",())
                        if waiting['n']:return
                        await asyncio.sleep(.01)
                    pytest.fail('Sign-in bypassed the erasure lifecycle lock')
                await asyncio.wait_for(wait_until_fenced(),3)
                assert await database.fetch_one('SELECT id FROM users WHERE id=%s',(owner,)) is None
                await conn.execute('INSERT INTO privacy_deleted_subjects(subject_digest) VALUES (%s)',(privacy.digest(owner),))
        with pytest.raises(HTTPException) as exc:await asyncio.wait_for(task,3)
        assert exc.value.status_code==403
        assert await database.fetch_one('SELECT id FROM users WHERE id=%s',(owner,)) is None
    finally:
        if task and not task.done():
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
