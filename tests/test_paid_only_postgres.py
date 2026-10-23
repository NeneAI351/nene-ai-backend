"""Real wallet transactions with synthetic verified payments; no paid API calls."""
import asyncio
import os
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
import database
import director
import generation_service
import generation_outbox
import payments
import wallet_store
from paid_fixtures import fund_paid
from test_director import BRIEF, PLAN

KWARGS=dict(prompt='A sunrise over the city',provider='ltx',model='ltx-2-5-fast',resolution='720p',duration_seconds=6)

@pytest_asyncio.fixture
async def paid_db(monkeypatch):
    if not os.getenv('DATABASE_URL'):pytest.skip('Disposable PostgreSQL required')
    await database.start_database();await database.initialize_schema()
    await wallet_store.initialize_wallet_schema();await payments.initialize_payment_schema()
    await generation_outbox.initialize_outbox_schema();await director.initialize_director_schema()
    user=str(uuid.uuid4())
    await database.execute('INSERT INTO users(id) VALUES (%s)',(user,))
    monkeypatch.setenv('DIRECTOR_PLANNING_ENABLED','true');monkeypatch.setenv('OPENAI_API_KEY','fake')
    monkeypatch.setenv('DIRECTOR_ALLOWED_USER_IDS',user);monkeypatch.setenv('DIRECTOR_PLAN_CREDITS','10')
    monkeypatch.setattr(director,'enforce_rate_limit',AsyncMock())
    from account_agreement_store import initialize_account_agreement_schema,record_agreement
    await initialize_account_agreement_schema();await record_agreement(user)
    yield user
    await database.execute('DELETE FROM users WHERE id=%s',(user,))

@pytest.mark.asyncio
@pytest.mark.parametrize('funding',['empty','bonus','test','unverified'])
async def test_non_paid_balances_cannot_queue_video(paid_db,funding):
    user=paid_db
    await wallet_store.ensure_wallet(user)
    if funding=='test':await fund_paid(user,'500',mode='test')
    if funding in {'bonus','unverified'}:
        await database.execute('UPDATE wallets SET available_credits=500,lifetime_bonus=500 WHERE user_id=%s',(user,))
    with pytest.raises(generation_service.InsufficientCredits):
        await generation_service.create_reserved_generation(user,'no-free',**KWARGS)
    assert (await database.fetch_one('SELECT count(*) AS n FROM generations WHERE user_id=%s',(user,)))['n']==0
    assert (await database.fetch_one('SELECT count(*) AS n FROM generation_requests WHERE user_id=%s',(user,)))['n']==0

@pytest.mark.asyncio
async def test_live_paid_reservation_concurrent_spend_and_failure_restoration(paid_db):
    user=paid_db
    amount=str(generation_service.pricing_quote('ltx','ltx-2-5-fast','720p',6)['recommended_credits'])
    await fund_paid(user,amount)
    results=await asyncio.gather(*(generation_service.create_reserved_generation(user,'spend-'+str(i),**KWARGS) for i in range(2)),return_exceptions=True)
    accepted=[r for r in results if isinstance(r,dict)]
    assert len(accepted)==1
    assert sum(isinstance(r,generation_service.InsufficientCredits) for r in results)==1
    generation_id=accepted[0]['generation_id']
    await wallet_store.wallet_release(user,Decimal(amount),generation_id,'failed')
    await wallet_store.wallet_release(user,Decimal(amount),generation_id,'failed')
    snapshot=(await wallet_store.wallet_balance(user))['wallet']
    assert Decimal(snapshot['available_credits'])==Decimal(amount)
    assert Decimal(snapshot['reserved_credits'])==0
    # Releasing an undelivered job restores the paid entitlement as well.
    assert (await generation_service.create_reserved_generation(user,'after-failure',**KWARGS))['status']=='queued'

async def plan_request(user,provider,monkeypatch):
    monkeypatch.setattr(director,'generate_plan',provider)
    api=FastAPI();api.include_router(director.router)
    api.dependency_overrides[director.get_authenticated_user_id]=lambda:user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test')

@pytest.mark.asyncio
@pytest.mark.parametrize('funding',['empty','bonus','test'])
async def test_unpaid_director_never_calls_model(paid_db,monkeypatch,funding):
    user=paid_db
    await wallet_store.ensure_wallet(user)
    if funding=='test':await fund_paid(user,'100',mode='test')
    if funding=='bonus':await database.execute('UPDATE wallets SET available_credits=100,lifetime_bonus=100 WHERE user_id=%s',(user,))
    provider=AsyncMock(return_value=director.Plan(**PLAN))
    async with await plan_request(user,provider,monkeypatch) as c:
        response=await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':str(uuid.uuid4())})
    assert response.status_code==402
    provider.assert_not_awaited()

@pytest.mark.asyncio
async def test_paid_director_charges_once_and_failure_restores_once(paid_db,monkeypatch):
    user=paid_db;await fund_paid(user,'100')
    provider=AsyncMock(return_value=director.Plan(**PLAN))
    key=str(uuid.uuid4())
    async with await plan_request(user,provider,monkeypatch) as c:
        first=await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':key})
        replay=await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':key})
        assert first.status_code==replay.status_code==201
        assert first.json()==replay.json();assert provider.await_count==1
        await director.settle_plan(user,first.json()['id'],director.Plan(**PLAN))
        provider.side_effect=HTTPException(504,'Timed out')
        failed=await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':str(uuid.uuid4())})
        assert failed.status_code==504
    snapshot=(await wallet_store.wallet_balance(user))['wallet']
    assert Decimal(snapshot['available_credits'])==90
    assert Decimal(snapshot['reserved_credits'])==0
    assert Decimal(snapshot['lifetime_consumed'])==10
    states=await database.fetch_all('SELECT id::text,billing_state FROM director_plans WHERE user_id=%s',(user,))
    released=next(r['id'] for r in states if r['billing_state']=='released')
    await director.settle_plan(user,released,None)
    assert Decimal((await wallet_store.wallet_balance(user))['wallet']['available_credits'])==90

@pytest.mark.asyncio
async def test_test_transaction_cannot_fulfil_live_order(paid_db):
    user=paid_db;reference='nene_'+uuid.uuid4().hex
    await database.execute("""INSERT INTO payment_orders(id,user_id,mode,reference,package_id,amount_kobo,credits)
      VALUES (%s,%s,'live',%s,'fixture',100,100)""",(str(uuid.uuid4()),user,reference))
    with pytest.raises(HTTPException) as error:
        await payments._fulfill_verified_transaction({'reference':reference,'status':'success','domain':'test','id':123,'amount':100,'currency':'NGN'})
    assert error.value.status_code==400
    assert Decimal((await wallet_store.wallet_balance(user))['wallet']['available_credits'])==0
