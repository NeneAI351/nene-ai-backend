"""Real database coverage for audio ownership, price duration and replay."""
import os,uuid
import pytest,pytest_asyncio
import database,generation_service,generation_outbox,speech_assets
from paid_fixtures import fund_paid

@pytest_asyncio.fixture(scope='session')
async def speech_db():
    if not os.getenv('DATABASE_URL'):pytest.skip('DATABASE_URL required')
    if database._pool is None:await database.start_database()
    await database.initialize_schema()
    await generation_outbox.initialize_outbox_schema()
    await speech_assets.initialize_schema()
    yield

@pytest.mark.asyncio
async def test_private_audio_billing_and_idempotency(speech_db):
    owner,other,image,audio=[str(uuid.uuid4()) for _ in range(4)]
    for user in [owner,other]:
        await database.execute('INSERT INTO users(id,email) VALUES (%s,%s)',(user,f'{user}@speech.invalid'))
        await fund_paid(user,'5000')
    await database.execute("INSERT INTO media_assets(id,user_id,asset_type,object_key,content_type,size_bytes) VALUES (%s,%s,'source_image',%s,'image/jpeg',100)",(image,owner,f'source-images/{owner}/{image}.jpg'))
    await database.execute('INSERT INTO speech_assets(id,user_id,object_key,duration_seconds,billable_seconds) VALUES (%s,%s,%s,6.4,7)',(audio,owner,speech_assets.object_key(owner,audio)))
    args=dict(prompt='Speak naturally',provider='ltx',model='ltx-2-5-fast',resolution='720p',duration_seconds=7,generation_type='audio-to-video',source_image_id=image,source_audio_id=audio,credit_limit='5000')
    with pytest.raises(LookupError):await generation_service.create_reserved_generation(other,'stolen-recording',**args)
    with pytest.raises(ValueError,match='full recording'):await generation_service.create_reserved_generation(owner,'short-price',**{**args,'duration_seconds':6})
    before=await database.fetch_one('SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s',(owner,))
    assert float(before['available_credits'])==5000 and float(before['reserved_credits'])==0
    first=await generation_service.create_reserved_generation(owner,'talking-one',**args)
    replay=await generation_service.create_reserved_generation(owner,'talking-one',**args)
    assert first['generation_id']==replay['generation_id'] and replay['duplicate'] is True
    row=await database.fetch_one('SELECT request FROM generations WHERE id=%s',(first['generation_id'],))
    assert row['request']['source_audio_id']==audio and row['request']['duration']==7
    with pytest.raises(generation_service.GenerationRequestConflict):await generation_service.create_reserved_generation(owner,'talking-one',**{**args,'prompt':'Changed words'})
