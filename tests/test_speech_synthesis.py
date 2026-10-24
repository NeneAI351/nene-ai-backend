"""No paid calls: validate the script/audio contract and submission ambiguity."""
from unittest.mock import AsyncMock
import io,wave
import httpx,pytest
import speech_synthesis as speech
import generation_worker as worker
import speech_assets

OWNER='11111111-1111-4111-8111-111111111111'
JOB='22222222-2222-4222-8222-222222222222'

def generation(status='queued'):
    return {'id':JOB,'user_id':OWNER,'status':status,'provider':'magic-hour','generation_type':'text-to-speech','provider_job_id':'voice-job','request':{'prompt':'Welcome to NENE AI.','tts_voice':'Anne Hathaway'},'credits_reserved':'10','credits_charged':'0'}

def test_voice_quote_is_character_based_and_never_includes_a_video(monkeypatch):
    monkeypatch.setenv('NENE_MAGIC_HOUR_CREDIT_USD','0.0025')
    q=speech.quote('Welcome home.','Anne Hathaway')
    assert q['recommended_credits']>0 and q['provider_cost_usd']==.005
    assert q['video_included'] is False and q['estimated'] is True
    with pytest.raises(ValueError):speech.quote('Hello','unsupported')
    with pytest.raises(ValueError):speech.quote('x'*121,'Anne Hathaway')

@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['accepted','timeout','malformed','error500'])
async def test_tts_submits_exact_script_once_and_handles_uncertainty(monkeypatch,outcome):
    calls=[]
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,headers,json):
            calls.append((url,json))
            if outcome=='timeout':raise httpx.ReadTimeout('lost response')
            return httpx.Response(500 if outcome=='error500' else 200,json=[] if outcome=='malformed' else {'id':'voice-job'})
    monkeypatch.setenv('MAGIC_HOUR_API_KEY','unit-only')
    monkeypatch.setattr(speech.httpx,'AsyncClient',Client)
    begin,attach,unknown,enqueue=[AsyncMock() for _ in range(4)]
    monkeypatch.setattr(speech.generation_store,'begin_submission',begin)
    monkeypatch.setattr(speech.generation_store,'attach_provider_job',attach)
    monkeypatch.setattr(speech.generation_store,'mark_submission_unknown',unknown)
    monkeypatch.setattr(speech.generation_outbox,'enqueue_generation_event',enqueue)
    await worker._dispatch(generation())
    assert len(calls)==1
    assert calls[0][1]['style']=={'prompt':'Welcome to NENE AI.','voice_name':'Anne Hathaway'}
    assert attach.await_count==int(outcome=='accepted')
    assert unknown.await_count==int(outcome!='accepted')
    # Crash recovery or duplicate delivery cannot issue another paid POST.
    await worker._dispatch(generation('submitting'))
    await worker._dispatch(generation('submitted'))
    assert len(calls)==1

@pytest.mark.asyncio
async def test_existing_private_audio_completes_without_regenerating_or_redownloading(monkeypatch):
    monkeypatch.setattr(speech.database,'fetch_one',AsyncMock(return_value={'id':JOB}))
    download=AsyncMock();monkeypatch.setattr(speech,'download_audio',download)
    transition=AsyncMock();monkeypatch.setattr(speech.generation_store,'transition_generation',transition)
    monkeypatch.setattr(speech.provider_costs,'record_generation_cost_estimate',AsyncMock())
    monkeypatch.setattr(worker,'_load_generation',AsyncMock(return_value=None))
    assert await speech.reconcile(generation('submitted'))==0
    assert download.await_count==0
    assert transition.call_args.kwargs['result']['source_audio_id']==JOB
    assert 'object_key' not in transition.call_args.kwargs['result']

def test_long_synthesized_audio_is_not_silently_cut_to_ten_seconds():
    if not speech_assets.ffmpeg_executable():pytest.skip('FFmpeg required')
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(8000);audio.writeframes(b'\0\0'*8000*12)
    output,duration,billable=speech_assets.normalize(buffer.getvalue(),60,.1)
    assert duration==12 and billable==12
    with wave.open(io.BytesIO(output)) as audio:assert audio.getnframes()/audio.getframerate()==12

@pytest.mark.asyncio
async def test_voice_create_fails_closed_without_paid_launch(monkeypatch):
    monkeypatch.setattr(speech,'available',lambda:False)
    create=AsyncMock();monkeypatch.setattr(speech.generation_service,'create_reserved_generation',create)
    from fastapi import FastAPI
    app=FastAPI();app.include_router(speech.router);app.dependency_overrides[speech.get_authenticated_user_id]=lambda:OWNER
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/api/speech/generations',headers={'Idempotency-Key':'one'},json={'text':'Hello','voice':'Anne Hathaway','credit_limit':'5'})
    assert response.status_code==503 and create.await_count==0
