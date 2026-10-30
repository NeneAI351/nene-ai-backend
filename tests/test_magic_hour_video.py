"""Backup contracts and billing safety without external API calls."""
from unittest.mock import AsyncMock
import httpx
import pytest
import magic_hour_video as backup
import generation_worker as worker
import generation_service
import provider_routing
import pricing

OWNER='11111111-1111-4111-8111-111111111111'
JOB='22222222-2222-4222-8222-222222222222'
ASSET='33333333-3333-4333-8333-333333333333'

@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('MAGIC_HOUR_API_KEY','test-only-not-real')
    monkeypatch.setenv('NENE_MAGIC_HOUR_VIDEO_ENABLED','true')
    monkeypatch.setenv('NENE_MAGIC_HOUR_CREDIT_USD','0.0025')
    monkeypatch.setenv('ALLOWED_MEDIA_HOSTS','storage.googleapis.com,videos.magichour.ai')
    monkeypatch.setattr(backup.generation_store,'begin_submission',AsyncMock())
    monkeypatch.setattr(backup.generation_store,'attach_provider_job',AsyncMock())
    monkeypatch.setattr(backup.generation_store,'mark_submission_unknown',AsyncMock())
    monkeypatch.setattr(backup.generation_store,'transition_generation',AsyncMock())
    monkeypatch.setattr(backup.generation_outbox,'enqueue_generation_event',AsyncMock())
    monkeypatch.setattr(worker,'_load_generation',AsyncMock(return_value={'id':JOB,'user_id':OWNER,'status':'failed'}))
    monkeypatch.setattr(worker,'_settle_terminal',AsyncMock())
    monkeypatch.setattr(worker,'_download_and_store_video',AsyncMock(return_value={'object_key':'private/video.mp4','content_type':'video/mp4','size_bytes':100}))
    monkeypatch.setattr(backup.provider_costs,'record_generation_cost_estimate',AsyncMock())


def generation(kind='text-to-video',**settings):
    req={'prompt':'A river at sunrise','model':'minimax-h3','resolution':'720p','duration':6,'aspect_ratio':'16:9','generate_audio':True}
    if kind=='image-to-video':req.update(source_image_id=ASSET,aspect_ratio=None)
    req.update(settings)
    return {'id':JOB,'user_id':OWNER,'status':'queued','provider':'magic-hour','generation_type':kind,'provider_job_id':'provider-job','request':req}


def transport(monkeypatch,response=None,error=None):
    calls=[]
    class Client:
        def __init__(self,**kwargs):assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):
            calls.append(('POST',url,kwargs))
            if error:raise error
            return response
        async def get(self,url,**kwargs):
            calls.append(('GET',url,kwargs))
            if error:raise error
            return response
    monkeypatch.setattr(backup.httpx,'AsyncClient',Client)
    return calls

@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['text-to-video','image-to-video'])
async def test_backup_submission_pins_model_and_billable_settings(configured,monkeypatch,kind):
    calls=transport(monkeypatch,httpx.Response(200,json={'id':'provider-job','credits_charged':288}))
    monkeypatch.setattr(backup.database,'fetch_one',AsyncMock(return_value={'object_key':f'source-images/{OWNER}/{ASSET}.png'}))
    monkeypatch.setattr(backup.media_storage,'create_signed_source_image_url',lambda *args:'https://storage.test/source.png?signature=temporary')
    await worker._dispatch(generation(kind,generate_audio=False))
    assert len(calls)==1
    method,url,params=calls[0];body=params['json']
    assert url==backup.BASE+'/v1/'+kind
    assert body['model']=='minimax-h3' and body['end_seconds']==6 and body['audio'] is False
    assert body['resolution']=='720p'
    backup.generation_store.begin_submission.assert_awaited_once_with(OWNER,JOB)
    backup.generation_store.attach_provider_job.assert_awaited_once_with(OWNER,JOB,'magic-hour','provider-job')
    assert 'signature' not in repr(backup.generation_outbox.enqueue_generation_event.call_args)
    if kind=='image-to-video':assert body['assets']['image_file_path'].startswith('https://storage.test/source.png')

@pytest.mark.asyncio
@pytest.mark.parametrize('code',[408,409,500,503,302])
async def test_uncertain_response_never_switches_or_releases_credits(configured,monkeypatch,code):
    calls=transport(monkeypatch,httpx.Response(code,json={}))
    await worker._dispatch(generation())
    assert len(calls)==1
    backup.generation_store.mark_submission_unknown.assert_awaited_once_with(OWNER,JOB)
    worker._settle_terminal.assert_not_awaited()
    backup.generation_store.attach_provider_job.assert_not_awaited()

@pytest.mark.asyncio
async def test_network_timeout_is_not_retried_on_another_provider(configured,monkeypatch):
    calls=transport(monkeypatch,error=httpx.ReadTimeout('test timeout'))
    await worker._dispatch(generation())
    assert len(calls)==1
    backup.generation_store.mark_submission_unknown.assert_awaited_once()
    worker._settle_terminal.assert_not_awaited()

@pytest.mark.asyncio
async def test_restart_in_submitting_does_not_submit_backup_again(configured,monkeypatch):
    calls=transport(monkeypatch,httpx.Response(200,json={'id':'another-job'}))
    g=generation();g['status']='submitting'
    await worker._dispatch(g)
    assert calls==[]
    backup.generation_store.mark_submission_unknown.assert_awaited_once()

@pytest.mark.asyncio
async def test_explicit_provider_rejection_releases_only_through_terminal_settlement(configured,monkeypatch):
    transport(monkeypatch,httpx.Response(402,json={'code':'insufficient_credits'}))
    await worker._dispatch(generation())
    assert backup.generation_store.transition_generation.call_args.args[2]=='failed'
    worker._settle_terminal.assert_awaited_once()
    backup.generation_store.mark_submission_unknown.assert_not_awaited()

@pytest.mark.asyncio
async def test_backup_success_stores_privately_before_success_and_capture(configured,monkeypatch):
    url='https://videos.magichour.ai/provider-job/output.mp4?temporary=secret'
    transport(monkeypatch,httpx.Response(200,json={'id':'provider-job','status':'complete','downloads':[{'url':url}],'credits_charged':288}))
    g=generation();g['status']='submitted'
    await worker._reconcile(g)
    worker._download_and_store_video.assert_awaited_once_with(url,JOB)
    args=backup.generation_store.transition_generation.call_args
    assert args.args[2]=='succeeded'
    assert args.kwargs['result']['object_key']=='private/video.mp4'
    assert url not in repr(args)
    assert backup.provider_costs.record_generation_cost_estimate.call_args.args[0]['metadata']['provider_credits_charged']==288
    worker._settle_terminal.assert_awaited_once()

@pytest.mark.asyncio
async def test_download_failure_does_not_capture_or_mark_success(configured,monkeypatch):
    transport(monkeypatch,httpx.Response(200,json={'id':'provider-job','status':'complete','downloads':[{'url':'https://unapproved.test/out.mp4'}]}))
    worker._download_and_store_video.side_effect=ValueError('host rejected')
    with pytest.raises(ValueError):await worker._reconcile(generation())
    backup.generation_store.transition_generation.assert_not_awaited()
    worker._settle_terminal.assert_not_awaited()

@pytest.mark.asyncio
async def test_reconciliation_continues_when_new_backup_jobs_are_locked(configured,monkeypatch):
    monkeypatch.setenv('NENE_MAGIC_HOUR_VIDEO_ENABLED','false')
    calls=transport(monkeypatch,httpx.Response(200,json={'id':'provider-job','status':'rendering'}))
    g=generation();g['status']='submitted'
    assert await worker._reconcile(g)==30
    assert calls[0][0]=='GET'

@pytest.mark.parametrize('settings',[{'model':'default'},{'duration':11},{'resolution':'4k'},{'aspect_ratio':'4:3'}])
def test_unsupported_backup_settings_are_rejected(settings):
    with pytest.raises(ValueError):backup.validate(generation(**settings)['request'],'text-to-video')

@pytest.mark.asyncio
async def test_foreign_private_image_never_reaches_provider(configured,monkeypatch):
    calls=transport(monkeypatch,httpx.Response(200,json={'id':'provider-job'}))
    monkeypatch.setattr(backup.database,'fetch_one',AsyncMock(return_value=None))
    await worker._dispatch(generation('image-to-video'))
    assert calls==[]
    backup.generation_store.begin_submission.assert_not_awaited()

@pytest.mark.parametrize('unit',['','nan','inf','0','-1','bad'])
def test_backup_pricing_fails_closed_without_valid_account_credit_cost(monkeypatch,unit):
    monkeypatch.setenv('NENE_MAGIC_HOUR_CREDIT_USD',unit)
    with pytest.raises(ValueError):pricing.quote('magic-hour','minimax-h3','720p',6)


def test_backup_price_preserves_margin_and_model(configured):
    q=pricing.quote('magic-hour','minimax-h3','720p',6)
    assert q['model']=='minimax-h3' and q['provider_cost_usd']==pytest.approx(48*6*.0025)
    assert q['estimated_contribution_margin_after_payment_fee']>=.5
    assert q['estimated_provider_cost'] is True


def test_pre_submission_routing_uses_backup_only_if_primary_is_unavailable(configured,monkeypatch):
    monkeypatch.setenv('LTX_API_KEY','fixture')
    assert provider_routing.select_provider()['provider']=='ltx'
    monkeypatch.setenv('NENE_LTX_VIDEO_ENABLED','false')
    result=provider_routing.select_provider()
    assert result['provider']=='magic-hour' and result['model']=='minimax-h3'
    with pytest.raises(RuntimeError):provider_routing.select_provider(generation_type='audio-to-video',duration_seconds=3)
