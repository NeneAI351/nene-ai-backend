"""Talking-character contract tests; no paid provider calls."""
import io,wave,shutil
from unittest.mock import AsyncMock
import httpx,pytest
import speech_assets
import generation_worker as worker
import app

OWNER='11111111-1111-4111-8111-111111111111'
ASSET='22222222-2222-4222-8222-222222222222'

def wav(seconds):
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000);audio.writeframes(b'\0\0'*int(16000*seconds))
    return buffer.getvalue()

@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg required')
def test_real_audio_normalization_uses_server_duration_and_rejects_long_recordings():
    data,duration,billable=speech_assets.normalize(wav(2.2))
    assert duration==2.2 and billable==3
    with wave.open(io.BytesIO(data)) as audio:
        assert audio.getframerate()==48000 and audio.getnchannels()==1
    with pytest.raises(ValueError):speech_assets.normalize(wav(10.2))
    with pytest.raises(ValueError):speech_assets.normalize(wav(1))
    with pytest.raises(ValueError):speech_assets.normalize(b'#EXTM3U\nhttps://private.test')

@pytest.mark.parametrize('key',['https://attacker.test/file.wav','source-speech/../../file.wav',f'source-speech/{OWNER}/bad.wav'])
def test_signer_rejects_non_owned_key_structure(key):
    with pytest.raises(ValueError):speech_assets.signed_url(key)

@pytest.mark.asyncio
@pytest.mark.parametrize('timeout',[False,True])
async def test_audio_worker_signs_owned_assets_and_preserves_ambiguous_submission(monkeypatch,timeout):
    calls=[]
    class Client:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,headers,json):
            calls.append((url,json))
            if timeout:raise httpx.ReadTimeout('lost response')
            return httpx.Response(202,json={'id':'provider-job'})
    async def lookup(sql,params):
        assert params[1]==OWNER
        return {'object_key':f'source-images/{OWNER}/{ASSET}.jpg' if 'media_assets' in sql else f'source-speech/{OWNER}/{ASSET}.wav'}
    monkeypatch.setattr(worker.database,'fetch_one',lookup)
    monkeypatch.setattr(worker.httpx,'AsyncClient',Client)
    monkeypatch.setattr(worker,'_provider_config',lambda:('https://provider.test',{}))
    monkeypatch.setattr(worker.media_storage,'create_signed_source_image_url',lambda *args:'https://storage.test/image?short-lived')
    monkeypatch.setattr(worker.speech_assets,'signed_url',lambda *args:'https://storage.test/audio?short-lived')
    begin=AsyncMock();unknown=AsyncMock();attach=AsyncMock();enqueue=AsyncMock()
    monkeypatch.setattr(worker.generation_store,'begin_submission',begin)
    monkeypatch.setattr(worker.generation_store,'mark_submission_unknown',unknown)
    monkeypatch.setattr(worker.generation_store,'attach_provider_job',attach)
    monkeypatch.setattr(worker.generation_outbox,'enqueue_generation_event',enqueue)
    await worker._dispatch({'id':ASSET,'user_id':OWNER,'status':'queued','provider':'ltx','generation_type':'audio-to-video','request':{'prompt':'Speak naturally','model':'ltx-2-5-fast','resolution':'720p','duration':3,'source_audio_id':ASSET,'source_image_id':ASSET,'aspect_ratio':'9:16'}})
    assert len(calls)==1 and calls[0][0].endswith('/v2/audio-to-video')
    body=calls[0][1]
    assert body['audio_uri']=='https://storage.test/audio?short-lived'
    assert body['resolution']=='720x1280'
    assert 'duration' not in body and 'generate_audio' not in body and 'source_audio_id' not in body
    assert unknown.await_count==int(timeout)
    assert attach.await_count==int(not timeout)

@pytest.mark.asyncio
async def test_talking_audio_crosses_authenticated_generation_boundary(monkeypatch):
    monkeypatch.setenv('NENE_ENV','development');monkeypatch.setenv('LTX_API_KEY','test-only')
    monkeypatch.setattr(app,'enforce_rate_limit',AsyncMock())
    monkeypatch.setattr(app,'get_authenticated_user_id',AsyncMock(return_value=OWNER))
    monkeypatch.setattr(app,'database_configured',lambda:True)
    create=AsyncMock(return_value={'generation_id':ASSET})
    monkeypatch.setattr(app.generation_service,'create_reserved_generation',create)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),base_url='http://test') as client:
        response=await client.post('/api/generations',headers={'Idempotency-Key':'talking-test'},json={'type':'audio-to-video','provider':'ltx','prompt':'Speak','duration':3,'source_audio_id':ASSET,'source_image_id':ASSET,'credit_limit':'120'})
    assert response.status_code==202
    assert create.call_args.args[0]==OWNER
    assert create.call_args.kwargs['source_audio_id']==ASSET
