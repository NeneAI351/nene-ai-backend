"""Real PostgreSQL checks for Director idempotency, owner isolation and limits."""
import asyncio
import os
import uuid
from unittest.mock import AsyncMock
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
import database
import director
from test_director import BRIEF, PLAN

@pytest_asyncio.fixture
async def director_db():
    if not os.getenv('DATABASE_URL'):pytest.skip('DATABASE_URL required')
    await database.start_database();await database.initialize_schema();await director.initialize_director_schema()
    user=str(uuid.uuid4());other=str(uuid.uuid4())
    await database.execute('INSERT INTO users(id) VALUES (%s),(%s)',(user,other))
    yield user,other
    await database.execute('DELETE FROM users WHERE id IN (%s,%s)',(user,other))

@pytest.mark.asyncio
async def test_durable_replay_ownership_and_changed_brief(director_db,monkeypatch):
    user,other=director_db
    monkeypatch.setenv('DIRECTOR_PLANNING_ENABLED','true');monkeypatch.setenv('OPENAI_API_KEY','fake')
    monkeypatch.setenv('DIRECTOR_ALLOWED_USER_IDS',user)
    provider=AsyncMock(return_value=director.Plan(**PLAN));monkeypatch.setattr(director,'generate_plan',provider)
    monkeypatch.setattr(director,'enforce_rate_limit',AsyncMock())
    app=FastAPI();app.include_router(director.router)
    app.dependency_overrides[director.get_authenticated_user_id]=lambda:user
    key=str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        first=await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':key})
        replay=await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':key})
        assert first.status_code==replay.status_code==201
        assert first.json()==replay.json()
        changed=await c.post('/api/director/plans',json=BRIEF|{'idea':'A new entirely different story.'},headers={'Idempotency-Key':key})
        assert changed.status_code==409
        app.dependency_overrides[director.get_authenticated_user_id]=lambda:other
        assert (await c.get('/api/director/plans/'+first.json()['id'])).status_code==404
        assert (await c.get('/api/director/plans')).json()['items']==[]
    assert provider.await_count==1

@pytest.mark.asyncio
async def test_concurrent_duplicate_calls_model_once(director_db,monkeypatch):
    user,_=director_db
    monkeypatch.setenv('DIRECTOR_PLANNING_ENABLED','true');monkeypatch.setenv('OPENAI_API_KEY','fake')
    monkeypatch.setenv('DIRECTOR_ALLOWED_USER_IDS',user)
    entered=asyncio.Event();release=asyncio.Event()
    async def model(_):
        entered.set();await release.wait();return director.Plan(**PLAN)
    provider=AsyncMock(side_effect=model);monkeypatch.setattr(director,'generate_plan',provider)
    monkeypatch.setattr(director,'enforce_rate_limit',AsyncMock())
    app=FastAPI();app.include_router(director.router);app.dependency_overrides[director.get_authenticated_user_id]=lambda:user
    key=str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        first=asyncio.create_task(c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':key}))
        await asyncio.wait_for(entered.wait(),5)
        try:
            second=await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':key})
            assert second.status_code==409
        finally:release.set()
        assert (await first).status_code==201
    assert provider.await_count==1

@pytest.mark.asyncio
async def test_failures_count_toward_daily_account_cap(director_db,monkeypatch):
    user,_=director_db
    monkeypatch.setenv('DIRECTOR_PLANNING_ENABLED','true');monkeypatch.setenv('OPENAI_API_KEY','fake')
    monkeypatch.setenv('DIRECTOR_ALLOWED_USER_IDS',user)
    from fastapi import HTTPException
    provider=AsyncMock(side_effect=HTTPException(504,'Timed out'));monkeypatch.setattr(director,'generate_plan',provider)
    monkeypatch.setattr(director,'enforce_rate_limit',AsyncMock())
    app=FastAPI();app.include_router(director.router);app.dependency_overrides[director.get_authenticated_user_id]=lambda:user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        key=str(uuid.uuid4())
        assert (await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':key})).status_code==504
        assert (await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':key})).status_code==409
        for _ in range(2):
            assert (await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':str(uuid.uuid4())})).status_code==504
        assert (await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':str(uuid.uuid4())})).status_code==429
    assert provider.await_count==3
