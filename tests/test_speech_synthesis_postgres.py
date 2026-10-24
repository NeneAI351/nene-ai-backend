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
