"""Explicit MiniMax H3 backup. No cross-provider retries or implicit model choice."""
import asyncio
import json
import os
import re

import httpx

import database
import generation_outbox
import generation_store
import media_storage
import provider_costs
from pricing import quote

MODEL = 'minimax-h3'
BASE = 'https://api.magichour.ai'
JOB_ID = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')


def enabled():
    return (os.getenv('NENE_MAGIC_HOUR_VIDEO_ENABLED','').strip().lower() == 'true'
            and bool(os.getenv('MAGIC_HOUR_API_KEY','').strip())
            and 'videos.magichour.ai' in {host.strip().lower().rstrip('.') for host in os.getenv('ALLOWED_MEDIA_HOSTS','').split(',')})


def validate(request, kind):
    if kind not in {'text-to-video','image-to-video'} or request.get('model') != MODEL:
        raise ValueError('Unsupported Magic Hour backup mode or model.')
    if request.get('resolution') not in {'720p','1080p'} or request.get('duration') not in {6,8,10}:
        raise ValueError('Unsupported Magic Hour backup quality or duration.')
    if request.get('aspect_ratio') not in {None,'16:9','9:16','1:1'}:
        raise ValueError('Unsupported Magic Hour backup aspect ratio.')
    if not isinstance(request.get('generate_audio',True),bool):
        raise ValueError('Sound selection must be a boolean.')
    if kind == 'image-to-video' and (not request.get('source_image_id') or request.get('image_url')):
        raise ValueError('Magic Hour backup requires an owned private source image.')
    if kind == 'image-to-video' and request.get('aspect_ratio'):
        raise ValueError('Backup image format must match the source image.')


def headers():
    key=os.getenv('MAGIC_HOUR_API_KEY','').strip()
    if not enabled() or not key:raise RuntimeError('magic_hour_video_locked')
    return {'Authorization':'Bearer '+key,'Content-Type':'application/json'}


def request_data(generation):
    data=generation['request']
    return json.loads(data) if isinstance(data,str) else data


async def unknown(owner,job):
    await generation_store.mark_submission_unknown(owner,job)
    await generation_outbox.enqueue_generation_event(job,'reconcile_generation',{'reason':'backup_submission_outcome_unknown'})


async def fail(generation,code):
    await generation_store.transition_generation(generation['user_id'],generation['id'],'failed',error={'code':code})
    from generation_worker import _load_generation,_settle_terminal
    row=await _load_generation(generation['id'])
    if row:await _settle_terminal(row)


async def dispatch(generation):
    owner,job,kind=generation['user_id'],generation['id'],generation['generation_type']
    req=request_data(generation);validate(req,kind)
    auth=headers()
    payload={'name':'NENE AI '+job,'model':MODEL,'end_seconds':req['duration'],
             'resolution':req['resolution'],'audio':req.get('generate_audio',True),
             'style':{'prompt':req['prompt']}}
    if kind == 'text-to-video':payload['aspect_ratio']=req.get('aspect_ratio') or '16:9'
    else:
        asset=await database.fetch_one("SELECT object_key FROM media_assets WHERE id=%s AND user_id=%s AND asset_type='source_image' AND deleted_at IS NULL",(req['source_image_id'],owner))
        if not asset:await fail(generation,'backup_source_image_missing');return
        # Official API accepts pre-signed file URLs. Never save or log the URL.
        source=await asyncio.to_thread(media_storage.create_signed_source_image_url,asset['object_key'],600)
        payload['assets']={'image_file_path':source}
        # Image-to-video derives its aspect ratio from the input; reject rather
        # than silently promise a different requested shape until crop support.
    await generation_store.begin_submission(owner,job)
    try:
        async with httpx.AsyncClient(timeout=90,follow_redirects=False,trust_env=False) as client:
            response=await client.post(BASE+'/v1/'+kind,headers=auth,json=payload)
    except Exception:
        await unknown(owner,job);return
    if response.status_code >= 500 or response.status_code in {408,409} or 300 <= response.status_code < 400:
        await unknown(owner,job);return
    if response.status_code >= 400:
        await fail(generation,'backup_request_rejected');return
    try:data=response.json();provider_id=data.get('id') if isinstance(data,dict) else None
    except ValueError:provider_id=None
    if not isinstance(provider_id,str) or not JOB_ID.fullmatch(provider_id):
        await unknown(owner,job);return
    await generation_store.attach_provider_job(owner,job,'magic-hour',provider_id)
    await generation_outbox.enqueue_generation_event(job,'reconcile_generation',{})


async def reconcile(generation):
    owner,job,provider_job=generation['user_id'],generation['id'],generation['provider_job_id']
    if not isinstance(provider_job,str) or not JOB_ID.fullmatch(provider_job):raise RuntimeError('invalid_backup_provider_id')
    # An accepted job must remain recoverable even if NEW backup jobs are locked.
    key=os.getenv('MAGIC_HOUR_API_KEY','').strip()
    if not key:raise RuntimeError('magic_hour_credentials_unavailable')
    async with httpx.AsyncClient(timeout=45,follow_redirects=False,trust_env=False) as client:
        response=await client.get(BASE+'/v1/video-projects/'+provider_job,headers={'Authorization':'Bearer '+key})
    if response.status_code != 200:raise RuntimeError('backup_status_unavailable')
    data=response.json()
    if not isinstance(data,dict) or data.get('id') != provider_job:raise RuntimeError('backup_status_unconfirmed')
    state=data.get('status')
    if state in {'queued','rendering'}:
        if generation['status']=='submitted':await generation_store.transition_generation(owner,job,'running')
        return 30
    if state in {'error','canceled'}:await fail(generation,'backup_generation_failed');return 0
    if state != 'complete':raise RuntimeError('backup_status_unconfirmed')
    downloads=data.get('downloads')
    if not isinstance(downloads,list) or not downloads or not isinstance(downloads[0],dict) or not isinstance(downloads[0].get('url'),str):
        raise RuntimeError('backup_output_missing')
    from generation_worker import _download_and_store_video,_load_generation,_settle_terminal
    stored=await _download_and_store_video(downloads[0]['url'],job)
    req=request_data(generation)
    record=provider_costs.estimate_generation_cost(generation_id=job,provider='magic-hour',model=MODEL,
        resolution=req['resolution'],duration_seconds=req['duration'],rate_quote=quote('magic-hour',MODEL,req['resolution'],req['duration']))
    charged=data.get('credits_charged')
    if isinstance(charged,int) and not isinstance(charged,bool) and charged>=0:record['metadata']['provider_credits_charged']=charged
    await provider_costs.record_generation_cost_estimate(record)
    await generation_store.transition_generation(owner,job,'succeeded',result={**stored,'provider':'magic-hour'})
    row=await _load_generation(job)
    if row:await _settle_terminal(row)
    return 0
