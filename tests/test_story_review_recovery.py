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

@pytest.mark.asyncio
async def test_selected_batches_are_atomic_owned_and_replay_safe(generation_service_db,monkeypatch):
    monkeypatch.setenv('NENE_DURABLE_GENERATION_CREATE_ENABLED','true')
    monkeypatch.setenv('LTX_API_KEY','test-only')
    monkeypatch.setenv('NENE_LTX_VIDEO_ENABLED','true')
    owner=await _user_with_credits('5000')
    await story_store.initialize_story_schema()
    settings={str(i):{'provider':'ltx','model':'ltx-2-5-fast','resolution':'720p','type':'text-to-video','aspect_ratio':'16:9'} for i in range(2)}
    story=await story_store.create_story(owner,'Selected scenes','River story',None,{'scene_settings':settings})
    await story_store.save_scenes(owner,story['id'],[{'scene_index':i,'prompt':f'River {i}','duration_seconds':6} for i in range(2)])
    price=Decimal(str(generation_service.pricing_quote('ltx','ltx-2-5-fast','720p',6)['recommended_credits']))
    # Leave enough for one scene, not the two-scene batch.
    await database.execute('UPDATE wallets SET available_credits=%s WHERE user_id=%s',(price,owner))
    selection=[{'scene_index':i,'credit_limit':price} for i in range(2)]
    with pytest.raises(generation_service.InsufficientCredits,match='more paid credits'):
        await story_store.generate_selected_scenes(owner,story['id'],selection)
    assert not await database.fetch_all('SELECT id FROM generations WHERE user_id=%s',(owner,))
    # Even an error AFTER the first reservation rolls the entire batch back.
    await database.execute('UPDATE wallets SET available_credits=5000 WHERE user_id=%s',(owner,))
    original_create=generation_service.create_reserved_generation
    calls=0
    async def fail_second(*args,**kwargs):
        nonlocal calls
        calls+=1
        if calls==2:raise ValueError('simulated second-scene rejection')
        return await original_create(*args,**kwargs)
    monkeypatch.setattr(generation_service,'create_reserved_generation',fail_second)
    with pytest.raises(ValueError,match='second-scene rejection'):
        await story_store.generate_selected_scenes(owner,story['id'],selection)
    assert not await database.fetch_all('SELECT id FROM generations WHERE user_id=%s',(owner,))
    wallet=await database.fetch_one('SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s',(owner,))
    assert Decimal(wallet['available_credits'])==5000 and Decimal(wallet['reserved_credits'])==0
    monkeypatch.setattr(generation_service,'create_reserved_generation',original_create)
    await database.execute('UPDATE wallets SET available_credits=%s WHERE user_id=%s',(price,owner))
    first,second=await asyncio.gather(*[story_store.generate_selected_scenes(owner,story['id'],selection[:1]) for _ in range(2)])
    assert first['scenes'][0]['generation_id']==second['scenes'][0]['generation_id']
    saved=await story_store.get_story(owner,story['id'])
    assert saved['scenes'][1]['generation_id'] is None
    assert saved['character_lock']['scene_settings']==settings
    other=await _user_with_credits('5000')
    assert await story_store.generate_selected_scenes(other,story['id'],selection) is None
    # Top up, then pay only for the remaining scene; completed work stays linked.
    await database.execute('UPDATE wallets SET available_credits=%s WHERE user_id=%s',(price,owner))
    last=await story_store.generate_selected_scenes(owner,story['id'],selection[1:])
    assert last['scenes'][0]['scene_index']==1
    assert len(await database.fetch_all('SELECT id FROM generations WHERE user_id=%s',(owner,)))==2
