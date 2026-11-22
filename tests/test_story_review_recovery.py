"""Story totals are server priced and retries share one atomic paid reservation."""
import asyncio
from decimal import Decimal
import uuid
import pytest
from fastapi import HTTPException
from stories import StoryQuote, quote_story
import database, generation_service, story_store
from test_generation_service_postgres import generation_service_db, _user_with_credits

@pytest.mark.asyncio
async def test_concurrent_retries_release_failed_reservation_once(generation_service_db, monkeypatch):
    monkeypatch.setenv('NENE_DURABLE_GENERATION_CREATE_ENABLED','true')
    monkeypatch.setenv('LTX_API_KEY','test-only')
    owner = await _user_with_credits('5000')
    await story_store.initialize_story_schema()
    story = await story_store.create_story(owner,'Test story','River crossing',None,{})
    await story_store.save_scenes(owner,story['id'],[{'scene_index':0,'prompt':'River crossing','duration_seconds':6}])
    original = await generation_service.create_reserved_generation(owner,str(uuid.uuid4()),prompt='River crossing',provider='ltx',model='ltx-2-5-fast',resolution='720p',duration_seconds=6)
    await story_store.link_scene_generation(owner,story['id'],0,original['generation_id'])
    # Failed but not yet settled: retry must release and reserve in one transaction.
    await database.execute("UPDATE generations SET status='failed' WHERE id=%s",(original['generation_id'],))
    amount=Decimal(original['credits_reserved'])
    first, second=await asyncio.gather(*[story_store.retry_scene(owner,story['id'],0,original['generation_id'],amount) for _ in range(2)])
    assert first['generation_id']==second['generation_id']
    wallet=await database.fetch_one('SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s',(owner,))
    assert Decimal(wallet['reserved_credits'])==amount
    assert Decimal(wallet['available_credits'])==Decimal('5000')-amount
    releases=await database.fetch_all("SELECT id FROM wallet_operations WHERE user_id=%s AND generation_id=%s AND operation_type='release'",(owner,original['generation_id']))
    assert len(releases)==1
    # Active or uncertain replacements must never be submitted again.
    for status in ['running','submission_unknown','succeeded']:
        await database.execute('UPDATE generations SET status=%s WHERE id=%s',(status,first['generation_id']))
        with pytest.raises(ValueError,match='confirmed failed'):
            await story_store.retry_scene(owner,story['id'],0,first['generation_id'],amount)
    other=await _user_with_credits('5000')
    assert await story_store.retry_scene(other,story['id'],0,first['generation_id'],amount) is None
