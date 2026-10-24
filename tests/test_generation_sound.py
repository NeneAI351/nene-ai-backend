"""Sound is a durable request setting, not an unbilled second provider call."""
from unittest.mock import AsyncMock
import httpx
import pytest
from pydantic import ValidationError
import app
import generation_worker as worker


@pytest.mark.parametrize('value',['false',0,1,None])
def test_audio_selection_is_a_strict_boolean(value):
    with pytest.raises(ValidationError):
        app.CreateGenerationRequest(provider='ltx',prompt='A river',generate_audio=value)


@pytest.mark.asyncio
@pytest.mark.parametrize('audio',[True,False])
async def test_audio_choice_crosses_authenticated_billing_boundary(monkeypatch,audio):
    monkeypatch.setenv('NENE_ENV','development');monkeypatch.setenv('LTX_API_KEY','unit-only')
    monkeypatch.setattr(app,'enforce_rate_limit',AsyncMock())
    monkeypatch.setattr(app,'get_authenticated_user_id',AsyncMock(return_value='owner'))
    monkeypatch.setattr(app,'database_configured',lambda:True)
    create=AsyncMock(return_value={'generation_id':'job','status':'queued'})
    monkeypatch.setattr(app.generation_service,'create_reserved_generation',create)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),base_url='http://test') as client:
        response=await client.post('/api/generations',headers={'Idempotency-Key':'sound-choice'},json={'provider':'ltx','prompt':'A river','credit_limit':'120','generate_audio':audio})
    assert response.status_code==202
    assert create.call_args.kwargs['generate_audio'] is audio
    assert str(create.call_args.kwargs['credit_limit'])=='120'


@pytest.mark.asyncio
@pytest.mark.parametrize('audio',[True,False,None])
async def test_worker_sends_audio_choice_to_the_single_video_request(monkeypatch,audio):
    sent=[]
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,headers,json):sent.append((url,json));return httpx.Response(200,json={'id':'provider-job'})
    monkeypatch.setattr(worker.httpx,'AsyncClient',Client)
    monkeypatch.setattr(worker,'_provider_config',lambda:('https://provider.test',{}))
    monkeypatch.setattr(worker.generation_store,'begin_submission',AsyncMock())
    monkeypatch.setattr(worker.generation_store,'attach_provider_job',AsyncMock())
    monkeypatch.setattr(worker.generation_outbox,'enqueue_generation_event',AsyncMock())
    request={'prompt':'A river','model':'ltx-2-5-fast','resolution':'720p','duration':6}
    if audio is not None:request['generate_audio']=audio
    await worker._dispatch({'id':'job','user_id':'11111111-1111-4111-8111-111111111111','provider':'ltx','status':'queued','generation_type':'text-to-video','request':request})
    assert len(sent)==1
    assert sent[0][1]['generate_audio'] is (True if audio is None else audio)
