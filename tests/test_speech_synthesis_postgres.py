"""Atomic paid voice creation, approval asset recovery, and wallet settlement."""
import os,uuid
from decimal import Decimal
import pytest,pytest_asyncio
import database,generation_service,generation_store,generation_outbox,speech_assets
from generation_worker import _settle_terminal
from paid_fixtures import fund_paid

@pytest_asyncio.fixture(scope='session')
async def voice_db():
    if not os.getenv('DATABASE_URL'):pytest.skip('DATABASE_URL required')
    if database._pool is None:await database.start_database()
    await database.initialize_schema();await generation_outbox.initialize_outbox_schema();await speech_assets.initialize_schema()
    yield

@pytest.mark.asyncio
async def test_voice_reservation_replay_and_settlement_are_exactly_once(voice_db,monkeypatch):
    monkeypatch.setenv('NENE_MAGIC_HOUR_CREDIT_USD','0.0025')
    owner=str(uuid.uuid4())
    await database.execute('INSERT INTO users(id,email) VALUES (%s,%s)',(owner,f'{owner}@voice.invalid'))
    await fund_paid(owner,'500')
    args=dict(prompt='Welcome to NENE AI.',provider='magic-hour',model='magic-hour-voice',resolution='audio',duration_seconds=1,generation_type='text-to-speech',tts_voice='Anne Hathaway',credit_limit='500')
    with pytest.raises(generation_service.GenerationPriceChanged):await generation_service.create_reserved_generation(owner,'low-limit',**{**args,'credit_limit':'.01'})
    first=await generation_service.create_reserved_generation(owner,'voice-one',**args)
    replay=await generation_service.create_reserved_generation(owner,'voice-one',**args)
    assert first['generation_id']==replay['generation_id'] and replay['duplicate'] is True
    with pytest.raises(generation_service.GenerationRequestConflict):await generation_service.create_reserved_generation(owner,'voice-one',**{**args,'tts_voice':'Morgan Freeman'})
    job=first['generation_id']
    await generation_store.begin_submission(owner,job)
    await generation_store.attach_provider_job(owner,job,'magic-hour','voice-job')
    await generation_store.transition_generation(owner,job,'succeeded',result={'source_audio_id':job})
    row=await database.fetch_one('SELECT id::text,user_id::text,status,credits_reserved,credits_charged FROM generations WHERE id=%s',(job,))
    await _settle_terminal(row);await _settle_terminal(row)
    wallet=await database.fetch_one('SELECT available_credits,reserved_credits,lifetime_consumed FROM wallets WHERE user_id=%s',(owner,))
    amount=Decimal(first['credits_reserved'])
    assert Decimal(wallet['available_credits'])==500-amount
    assert Decimal(wallet['reserved_credits'])==0 and Decimal(wallet['lifetime_consumed'])==amount

@pytest.mark.asyncio
async def test_complete_voice_is_stored_privately_previewed_and_charged_once(voice_db,monkeypatch):
    import io,wave,httpx
    from unittest.mock import AsyncMock
    import speech_synthesis as speech,media_storage,provider_costs
    from generation_worker import _load_generation
    if not speech_assets.ffmpeg_executable():pytest.skip('Audio decoder required')
    monkeypatch.setenv('NENE_MAGIC_HOUR_CREDIT_USD','0.0025')
    monkeypatch.setenv('MAGIC_HOUR_API_KEY','disposable-test-key')
    owner,other=str(uuid.uuid4()),str(uuid.uuid4())
    await database.execute('INSERT INTO users(id) VALUES (%s)',(owner,));await fund_paid(owner,'500')
    created=await generation_service.create_reserved_generation(owner,'voice-preview-test',prompt='Welcome, NENE AI!',provider='magic-hour',model='magic-hour-voice',resolution='audio',duration_seconds=1,generation_type='text-to-speech',tts_voice='Anne Hathaway',credit_limit='500')
    job=created['generation_id'];await generation_store.begin_submission(owner,job);await generation_store.attach_provider_job(owner,job,'magic-hour','test-voice-project')
    raw=io.BytesIO()
    with wave.open(raw,'wb') as output:
        output.setnchannels(1);output.setsampwidth(2);output.setframerate(8000);output.writeframes(b'\0\0'*8000*3)
    fetched=[];stored=[]
    class Provider:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,headers):
            fetched.append(url)
            return httpx.Response(200,json={'status':'complete','downloads':[{'url':'https://audio.unit.test/voice.wav'}]})
    class PrivateStorage:
        def put_object(self,**kwargs):stored.append(kwargs)
    original_client=httpx.AsyncClient
    monkeypatch.setattr(speech.httpx,'AsyncClient',Provider)
    download=AsyncMock(return_value=raw.getvalue());monkeypatch.setattr(speech,'download_audio',download)
    monkeypatch.setattr(media_storage,'_client',lambda:PrivateStorage());monkeypatch.setattr(media_storage,'_storage_config',lambda:{'bucket':'disposable-private-voice'})
    row=await _load_generation(job)
    await speech.reconcile(row);await speech.reconcile(row)
    assert len(fetched)==1 and len(stored)==1 and download.await_count==1
    assert stored[0]['Key']==f'source-speech/{owner}/{job}.wav' and stored[0]['CacheControl']=='private, no-store'
    asset=await database.fetch_one('SELECT duration_seconds,billable_seconds FROM speech_assets WHERE id=%s AND user_id=%s',(job,owner))
    assert float(asset['duration_seconds'])==3 and asset['billable_seconds']==3
    wallet=await database.fetch_one('SELECT available_credits,reserved_credits,lifetime_consumed FROM wallets WHERE user_id=%s',(owner,))
    cost=Decimal(created['credits_reserved']);assert wallet['available_credits']==500-cost and wallet['reserved_credits']==0 and wallet['lifetime_consumed']==cost
    # The real owner-checked status route issues a fresh private preview. Another
    # account gets 404; no provider call or extra paid generation is involved.
    monkeypatch.setattr(speech.httpx,'AsyncClient',original_client)
    monkeypatch.setattr(speech,'enforce_rate_limit',AsyncMock())
    signed=[]
    def sign(key):signed.append(key);return 'https://private.unit.test/audio.wav?short-lived=unit'
    monkeypatch.setattr(speech_assets,'signed_url',sign)
    from fastapi import FastAPI
    app=FastAPI();app.include_router(speech.router);app.dependency_overrides[speech.get_authenticated_user_id]=lambda:owner
    async with original_client(transport=httpx.ASGITransport(app=app),base_url='http://unit') as client:
        response=await client.get('/api/speech/generations/'+job)
        assert response.status_code==200 and response.json()['audio']['private'] is True and response.json()['audio']['can_animate'] is True
        app.dependency_overrides[speech.get_authenticated_user_id]=lambda:other
        forbidden=await client.get('/api/speech/generations/'+job)
        assert forbidden.status_code==404
    assert len(signed)==1 and len(stored)==1
