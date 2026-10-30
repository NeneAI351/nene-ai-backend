import asyncio
import os
import uuid
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
import database
import payments
import director
import generation_service
from account_agreement_store import initialize_account_agreement_schema,record_agreement,TERMS_VERSION,TERMS_SHA256
from test_director import BRIEF

@pytest_asyncio.fixture
async def account_db():
    if not os.getenv('DATABASE_URL'):pytest.skip('Disposable PostgreSQL required')
    await database.start_database();await database.initialize_schema()
    await payments.initialize_payment_schema();await initialize_account_agreement_schema()
    from wallet_store import initialize_wallet_schema
    await initialize_wallet_schema()
    user=str(uuid.uuid4());other=str(uuid.uuid4())
    await database.execute('INSERT INTO users(id) VALUES (%s),(%s)',(user,other))
    yield user,other
    await database.execute('DELETE FROM users WHERE id IN (%s,%s)',(user,other))

@pytest.mark.asyncio
async def test_account_acceptance_is_once_immutable_and_owner_scoped(account_db):
    user,other=account_db
    first=await record_agreement(user)
    repeated=await asyncio.gather(*(record_agreement(user) for _ in range(3)))
    assert all(row==first for row in repeated)
    assert first['terms_version']==TERMS_VERSION
    assert first['terms_sha256']==TERMS_SHA256
    assert (await database.fetch_one('SELECT count(*) AS n FROM account_agreements WHERE user_id=%s',(user,)))['n']==1
    assert await database.fetch_one('SELECT user_id FROM account_agreements WHERE user_id=%s',(other,)) is None
    snapshot=await database.fetch_one('SELECT terms_snapshot FROM account_agreements WHERE user_id=%s',(user,))
    assert snapshot['terms_snapshot']

@pytest.mark.asyncio
async def test_checkout_without_repeat_checkbox_uses_original_account_agreement(account_db,monkeypatch):
    user,_=account_db
    agreement=await record_agreement(user)
    monkeypatch.setenv('PAYSTACK_MODE','test');monkeypatch.setenv('PAYSTACK_SECRET_KEY','sk_test_fixture')
    # Synthetic provider client: never a real payment or external request.
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'status':True,'data':{'authorization_url':'https://checkout.paystack.com/fixture','access_code':'fixture'}}
    class Provider:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):return Response()
    monkeypatch.setattr(payments.httpx,'AsyncClient',Provider)
    for _ in range(2):
        result=await payments.create_checkout(payments.CheckoutRequest(package_id='starter',email='buyer@example.test'),user)
        assert result['ok'] is True
    rows=await database.fetch_all('SELECT policy_version,policy_accepted_at FROM payment_orders WHERE user_id=%s',(user,))
    assert len(rows)==2
    assert all(row['policy_accepted_at']==agreement['accepted_at'] and row['policy_version']==agreement['refund_policy_version'] for row in rows)
    assert await record_agreement(user)==agreement

@pytest.mark.asyncio
async def test_paid_features_block_incomplete_account_setup(account_db,monkeypatch):
    from fastapi import HTTPException
    user,_=account_db
    monkeypatch.setenv('PAYSTACK_MODE','test');monkeypatch.setenv('PAYSTACK_SECRET_KEY','sk_test_fixture')
    with pytest.raises(HTTPException) as error:
        await payments.create_checkout(payments.CheckoutRequest(package_id='starter',email='buyer@example.test'),user)
    assert error.value.status_code==403
    assert (await database.fetch_one('SELECT count(*) AS n FROM payment_orders WHERE user_id=%s',(user,)))['n']==0
    with pytest.raises(HTTPException) as error:
        await generation_service.create_reserved_generation(user,'setup-required',prompt='A city at sunrise',provider='ltx',model='ltx-2-5-fast',resolution='720p',duration_seconds=6)
    assert error.value.status_code==403


@pytest.mark.asyncio
async def test_checkout_response_cannot_overwrite_early_success_webhook(account_db, monkeypatch):
    user, _ = account_db
    await record_agreement(user)
    monkeypatch.setenv('PAYSTACK_MODE', 'test')
    monkeypatch.setenv('PAYSTACK_SECRET_KEY', 'sk_test_fixture')
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'status': True, 'data': {'authorization_url': 'https://checkout.paystack.com/fixture'}}
    class Provider:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            reference = kwargs['json']['reference']
            # Simulate a success webhook committed while initialize is in flight.
            await payments._fulfill_verified_transaction({
                'reference': reference, 'domain': 'test', 'status': 'success',
                'id': 876543210, 'amount': 500000, 'currency': 'NGN',
            })
            return Response()
    monkeypatch.setattr(payments.httpx, 'AsyncClient', Provider)
    result = await payments.create_checkout(payments.CheckoutRequest(package_id='starter', email='buyer@example.test'), user)
    order = await database.fetch_one('SELECT status FROM payment_orders WHERE reference=%s', (result['reference'],))
    assert order['status'] == 'success'
    wallet = await database.fetch_one('SELECT available_credits FROM wallets WHERE user_id=%s', (user,))
    assert wallet['available_credits'] == 400
    ledger = await database.fetch_one("SELECT count(*) AS n FROM credit_ledger WHERE user_id=%s AND entry_type='purchase'", (user,))
    assert ledger['n'] == 1
