"""Paid, durable script -> voice. The resulting private audio is approved before animation."""
from __future__ import annotations
import asyncio,json,math,os,re,uuid
from decimal import Decimal,ROUND_CEILING
import httpx
from fastapi import APIRouter,Depends,Header,HTTPException,Request
from pydantic import BaseModel,ConfigDict,Field
import database,generation_service,generation_store,generation_outbox,media_storage,provider_costs,speech_assets,pricing
from auth import get_authenticated_user_id
from rate_limits import enforce_rate_limit

router=APIRouter(prefix='/api/speech',tags=['speech synthesis'])
VOICES=('Anne Hathaway','Morgan Freeman','David Attenborough')
MODEL='magic-hour-voice'

def rate():
    value=Decimal(os.getenv('NENE_MAGIC_HOUR_CREDIT_USD','0.0025'))
    if not value.is_finite() or not Decimal('0')<value<=1:raise ValueError('Speech pricing is unavailable.')
    return value

def available():
    return (os.getenv('NENE_DURABLE_GENERATION_CREATE_ENABLED','').lower()=='true'
        and os.getenv('NENE_TTS_CREATE_ENABLED','').lower()=='true'
        and bool(os.getenv('MAGIC_HOUR_API_KEY','').strip())
        and bool(os.getenv('NENE_MAGIC_HOUR_CREDIT_USD','').strip())
        and bool(speech_assets.ffmpeg_executable()))

def quote(text,voice):
    if voice not in VOICES:raise ValueError('Choose an available AI voice preset.')
    if not isinstance(text,str) or not text.strip() or len(text)>120:raise ValueError('Write a short script of 1–120 characters.')
    values=(pricing.CREDIT_VALUE_NGN,pricing.TARGET_GROSS_MARGIN,pricing.PAYSTACK_LOCAL_PERCENT,pricing.FIXED_FEE_PER_CREDIT_NGN,pricing.RISK_BUFFER,pricing.USD_TO_NGN)
    if not all(math.isfinite(v) for v in values) or pricing.CREDIT_VALUE_NGN<=0 or pricing.USD_TO_NGN<=0 or not 0<=pricing.TARGET_GROSS_MARGIN<.95 or not 0<=pricing.PAYSTACK_LOCAL_PERCENT<1-pricing.TARGET_GROSS_MARGIN or pricing.FIXED_FEE_PER_CREDIT_NGN<0 or not 0<=pricing.RISK_BUFFER<=2:
        raise ValueError('Speech pricing is unavailable.')
    characters=len(text.encode('utf-16-le'))//2
    cost=Decimal((characters+9)//10)*rate()
    denominator=Decimal(str(pricing.CREDIT_VALUE_NGN))*(1-Decimal(str(pricing.TARGET_GROSS_MARGIN))-Decimal(str(pricing.PAYSTACK_LOCAL_PERCENT)))-Decimal(str(pricing.FIXED_FEE_PER_CREDIT_NGN))
    if denominator<=0:raise ValueError('Speech pricing is unavailable.')
    buffered=cost*(1+Decimal(str(pricing.RISK_BUFFER)))
    credits=max(1,int((buffered*Decimal(str(pricing.USD_TO_NGN))/denominator).to_integral_value(rounding=ROUND_CEILING)))
    return {'provider':'magic-hour','model':MODEL,'resolution':'audio','duration_seconds':1,
        'characters':len(text),'voice':voice,'recommended_credits':credits,'provider_cost_usd':float(cost),
        'provider_cost_with_risk_buffer_usd':float(buffered),'rate_basis':'configured_credit_pack_estimate',
        'estimated':True,'video_included':False}

class Script(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text:str=Field(min_length=1,max_length=120)
    voice:str=Field(max_length=80)
class Submit(Script):
    credit_limit:Decimal=Field(gt=0,max_digits=14,decimal_places=2,allow_inf_nan=False)

@router.get('/availability')
def availability():
    return {'accepting_requests':available(),'voices':list(VOICES),'max_characters':120,'ai_generated':True,
      'message':'Create a voice track first. Listen and approve it, then review a separate video price.'}

@router.post('/quote')
async def preview(body:Script,owner:str=Depends(get_authenticated_user_id)):
    try:return quote(body.text.strip(),body.voice)
    except (ValueError,ArithmeticError):raise HTTPException(422,'A valid script, voice and speech price are required.')

@router.post('/generations',status_code=202)
async def create(body:Submit,request:Request,key:str=Header(alias='Idempotency-Key',min_length=1,max_length=200),owner:str=Depends(get_authenticated_user_id)):
    if not available():raise HTTPException(503,'Voice generation is not enabled yet. No credits were charged.')
    await enforce_rate_limit(request,bucket='speech-create',limit=6,window_seconds=60)
    try:
        return await generation_service.create_reserved_generation(owner,key,prompt=body.text,provider='magic-hour',model=MODEL,
            resolution='audio',duration_seconds=1,generation_type='text-to-speech',tts_voice=body.voice,credit_limit=body.credit_limit)
    except generation_service.InsufficientCredits as exc:raise HTTPException(402,str(exc))
    except (generation_service.GenerationRequestConflict,generation_service.GenerationPriceChanged) as exc:raise HTTPException(409,str(exc))
    except ValueError as exc:raise HTTPException(422,str(exc))
    except RuntimeError:raise HTTPException(503,'Voice generation is temporarily unavailable.')

@router.get('/generations/{job_id}')
async def status(job_id:uuid.UUID,request:Request,owner:str=Depends(get_authenticated_user_id)):
    await enforce_rate_limit(request,bucket='speech-poll',limit=60,window_seconds=60)
    row=await database.fetch_one("SELECT status,error FROM generations WHERE id=%s AND user_id=%s AND generation_type='text-to-speech'",(str(job_id),owner))
    if not row:raise HTTPException(404,'Voice track not found.')
    error=row.get('error');error={'code':error.get('code')} if isinstance(error,dict) and error.get('code') else None
    result={'generation_id':str(job_id),'status':row['status'],'error':error}
    if row['status']=='succeeded':
        asset=await database.fetch_one('SELECT id::text,object_key,duration_seconds,billable_seconds FROM speech_assets WHERE id=%s AND user_id=%s',(str(job_id),owner))
        if not asset:raise HTTPException(503,'The saved voice track is temporarily unavailable.')
        result['audio']={'source_audio_id':asset['id'],'duration_seconds':float(asset['duration_seconds']),
            'billable_seconds':asset['billable_seconds'],'private':True,'can_animate':2<=float(asset['duration_seconds'])<=10,
            'url':await asyncio.to_thread(speech_assets.signed_url,asset['object_key'])}
    return result

def config():
    key=os.getenv('MAGIC_HOUR_API_KEY','').strip()
    if not key:raise RuntimeError('speech_provider_not_configured')
    return {'Authorization':'Bearer '+key,'Content-Type':'application/json'}

async def unknown(owner,job,reason):
    await generation_store.mark_submission_unknown(owner,job)
    await generation_outbox.enqueue_generation_event(job,'reconcile_generation',{'reason':reason})

async def fail(generation,code):
    await generation_store.transition_generation(generation['user_id'],generation['id'],'failed',error={'code':code})
    from generation_worker import _load_generation,_settle_terminal
    row=await _load_generation(generation['id'])
    if row:await _settle_terminal(row)

async def dispatch(generation):
    request=generation['request'];request=json.loads(request) if isinstance(request,str) else request
    owner,job=generation['user_id'],generation['id']
    headers=config()
    await generation_store.begin_submission(owner,job)
    try:
        async with httpx.AsyncClient(timeout=45,follow_redirects=False,trust_env=False) as client:
            response=await client.post('https://api.magichour.ai/v1/ai-voice-generator',headers=headers,
                json={'name':'NENE AI voice '+job,'style':{'prompt':request['prompt'],'voice_name':request['tts_voice']}})
    except Exception:
        await unknown(owner,job,'speech_submission_outcome_unknown');return
    if response.status_code>=500 or response.status_code in {408,409}:
        await unknown(owner,job,'speech_submission_outcome_unknown');return
    if response.status_code>=400:
        await fail(generation,'speech_request_rejected');return
    try:data=response.json();provider_id=data.get('id') if isinstance(data,dict) else None
    except ValueError:provider_id=None
    if not isinstance(provider_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',provider_id):
        await unknown(owner,job,'speech_response_unconfirmed');return
    await generation_store.attach_provider_job(owner,job,'magic-hour',provider_id)
    await generation_outbox.enqueue_generation_event(job,'reconcile_generation',{})

async def download_audio(url):
    # Same SSRF/DNS/host allowlist controls as generated video delivery.
    media_storage.validate_provider_media_url(url)
    chunks=[];size=0
    async with httpx.AsyncClient(timeout=90,follow_redirects=False,trust_env=False) as client:
        async with client.stream('GET',url) as response:
            if response.status_code!=200:raise RuntimeError('speech_download_failed')
            async for chunk in response.aiter_bytes():
                size+=len(chunk)
                if size>speech_assets.MAX_BYTES:raise ValueError('speech_result_too_large')
                chunks.append(chunk)
    return b''.join(chunks)

async def save_audio(owner,job,raw):
    # Stable asset ID/key: retries after upload or DB failure reuse private bytes.
    data,duration,billable=await asyncio.to_thread(speech_assets.normalize,raw,60,.1)
    key=speech_assets.object_key(owner,job)
    await asyncio.to_thread(media_storage._client().put_object,Bucket=media_storage._storage_config()['bucket'],Key=key,
        Body=data,ContentType='audio/wav',ContentLength=len(data),CacheControl='private, no-store')
    await database.execute('''INSERT INTO speech_assets(id,user_id,object_key,duration_seconds,billable_seconds)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING''',(job,owner,key,duration,billable))
    return {'source_audio_id':job,'content_type':'audio/wav','duration_seconds':duration,'billable_seconds':billable}

async def reconcile(generation):
    owner,job,provider_job=generation['user_id'],generation['id'],generation['provider_job_id']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',provider_job):raise RuntimeError('invalid_speech_provider_id')
    asset=await database.fetch_one('SELECT id FROM speech_assets WHERE id=%s AND user_id=%s',(job,owner))
    if not asset:
        async with httpx.AsyncClient(timeout=30,follow_redirects=False,trust_env=False) as client:
            response=await client.get('https://api.magichour.ai/v1/audio-projects/'+provider_job,headers=config())
        if response.status_code!=200:raise RuntimeError('speech_status_unavailable')
        data=response.json();state=data.get('status')
        if state in {'queued','rendering'}:return 15
        if state in {'error','canceled'}:await fail(generation,'speech_generation_failed');return 0
        if state!='complete':raise RuntimeError('speech_status_unconfirmed')
        downloads=data.get('downloads') or []
        if not downloads or not isinstance(downloads[0].get('url'),str):raise RuntimeError('speech_output_missing')
        raw=await download_audio(downloads[0]['url'])
        await save_audio(owner,job,raw)
    request=generation['request'];request=json.loads(request) if isinstance(request,str) else request
    q=quote(request['prompt'],request['tts_voice'])
    record=provider_costs.estimate_generation_cost(generation_id=job,provider='magic-hour',model=MODEL,resolution='audio',duration_seconds=1,rate_quote=q)
    record['units']=len(request['prompt']);record['unit_type']='characters'
    await provider_costs.record_generation_cost_estimate(record)
    await generation_store.transition_generation(owner,job,'succeeded',result={'source_audio_id':job,'content_type':'audio/wav'})
    from generation_worker import _load_generation,_settle_terminal
    terminal=await _load_generation(job)
    if terminal:await _settle_terminal(terminal)
    return 0
