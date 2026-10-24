"""Persist scene sound preferences and invalidate stale scene jobs on edit."""
import os, uuid
import pytest
import pytest_asyncio
import database
import story_store

pytestmark=pytest.mark.asyncio

@pytest_asyncio.fixture(scope='session')
async def story_database():
    if not os.getenv('DATABASE_URL'):pytest.skip('DATABASE_URL required')
    await database.start_database()
    await database.initialize_schema()
    await story_store.initialize_story_schema()
    yield
    await database.close_database()

async def test_sound_round_trip_and_owner_isolation(story_database):
    owner,other=str(uuid.uuid4()),str(uuid.uuid4())
    async with database._pool.connection() as conn:
        await conn.execute('INSERT INTO users(id,email) VALUES (%s,%s)',(owner,f'story-{owner}@example.invalid'))
        await conn.commit()
    story=await story_store.create_story(owner,'River','Friends cross a river',None,{})
    scene={'scene_index':0,'prompt':'Opening','duration_seconds':6,'generate_audio':False,'sound_direction':'No sound'}
    saved=await story_store.save_scenes(owner,story['id'],[scene])
    assert saved['scenes'][0]['generate_audio'] is False
    assert saved['scenes'][0]['sound_direction']=='No sound'
    assert await story_store.get_story(other,story['id']) is None
    scene.update(generate_audio=True,sound_direction='River water')
    saved=await story_store.save_scenes(owner,story['id'],[scene])
    assert saved['scenes'][0]['generate_audio'] is True
    assert saved['scenes'][0]['sound_direction']=='River water'
    async with database._pool.connection() as conn:
        await conn.execute("UPDATE story_projects SET status='generating' WHERE id=%s",(story['id'],))
        await conn.commit()
    with pytest.raises(ValueError,match='cannot be edited'):
        await story_store.save_scenes(owner,story['id'],[scene])
