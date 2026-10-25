"""Real PostgreSQL paid entitlement, concurrency, rollback and privacy tests."""
import asyncio
import os
import uuid
from decimal import Decimal
import pytest
import pytest_asyncio
from fastapi import HTTPException
import database
import editor_exports as editor
from paid_fixtures import fund_paid

@pytest_asyncio.fixture(scope='session')
async def editor_db():
    if not os.getenv('DATABASE_URL'):pytest.skip('DATABASE_URL required')
    if database._pool is None:await database.start_database()
    await database.initialize_schema();await editor.initialize_schema()
    yield

async def user(amount='100',mode='live'):
    owner=str(uuid.uuid4());await database.execute('INSERT INTO users(id) VALUES (%s)',(owner,))
    await fund_paid(owner,amount,mode=mode);return owner

@pytest.mark.asyncio
async def test_replays_and_concurrent_keys_charge_identical_edit_exactly_once(editor_db):
    owner=await user();digest='a'*64;key=uuid.uuid4()
    receipts=await asyncio.gather(*[editor.purchase_export(owner,digest,key if i%2 else uuid.uuid4(),Decimal('10')) for i in range(8)])
    assert len({r['receipt_id'] for r in receipts})==1
    assert sum(not r['duplicate'] for r in receipts)==1
    wallet=await database.fetch_one('SELECT available_credits,lifetime_consumed FROM wallets WHERE user_id=%s',(owner,))
    assert wallet['available_credits']==90 and wallet['lifetime_consumed']==10
    charges=await database.fetch_all("SELECT amount FROM credit_ledger WHERE user_id=%s AND entry_type='editor_export_charge'",(owner,))
    assert len(charges)==1 and charges[0]['amount']==-10

@pytest.mark.asyncio
async def test_different_edit_is_new_fee_and_same_request_key_conflicts(editor_db):
    owner=await user();key=uuid.uuid4()
    await editor.purchase_export(owner,'a'*64,key,Decimal('10'))
    with pytest.raises(HTTPException) as exc:await editor.purchase_export(owner,'b'*64,key,Decimal('10'))
    assert exc.value.status_code==409
    await editor.purchase_export(owner,'b'*64,uuid.uuid4(),Decimal('10'))
    assert (await database.fetch_one('SELECT available_credits FROM wallets WHERE user_id=%s',(owner,)))['available_credits']==80

@pytest.mark.asyncio
async def test_bonus_test_and_insufficient_credits_cannot_fund_export(editor_db):
    for amount,mode in [('100','test'),('5','live')]:
        owner=await user(amount,mode)
        with pytest.raises(HTTPException) as exc:await editor.purchase_export(owner,'c'*64,uuid.uuid4(),Decimal('10'))
        assert exc.value.status_code==402
        assert not await database.fetch_one('SELECT id FROM editor_export_receipts WHERE user_id=%s',(owner,))
        assert (await database.fetch_one('SELECT available_credits FROM wallets WHERE user_id=%s',(owner,)))['available_credits']==Decimal(amount)

@pytest.mark.asyncio
async def test_owner_isolation_price_check_and_pause_still_allows_prior_receipt(editor_db,monkeypatch):
    first,other=await user(),await user()
    with pytest.raises(HTTPException) as exc:await editor.purchase_export(first,'d'*64,uuid.uuid4(),Decimal('9'))
    assert exc.value.status_code==409
    paid=await editor.purchase_export(first,'d'*64,uuid.uuid4(),Decimal('10'))
    assert (await editor.receipt('d'*64,user_id=other))['paid'] is False
    monkeypatch.setenv('NENE_EDITOR_EXPORTS_ENABLED','false')
    old=await editor.purchase_export(first,'d'*64,uuid.uuid4(),Decimal('10'));assert old['receipt_id']==paid['receipt_id']
    with pytest.raises(HTTPException) as exc:await editor.purchase_export(other,'d'*64,uuid.uuid4(),Decimal('10'))
    assert exc.value.status_code==503

@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_wallet_and_ledger(editor_db):
    owner=await user();other=await user();receipt=str(uuid.uuid4())
    # Force a constraint failure at INSERT after the wallet UPDATE. The whole
    # transaction must roll back instead of leaving an unreceipted charge.
    await database.execute('''INSERT INTO editor_export_receipts(id,user_id,project_digest,request_key,credit_cost)
        VALUES (%s,%s,%s,%s,10)''',(receipt,other,'e'*64,str(uuid.uuid4())))
    from unittest.mock import patch
    with patch.object(editor.uuid,'uuid4',return_value=uuid.UUID(receipt)):
        with pytest.raises(Exception):await editor.purchase_export(owner,'f'*64,uuid.uuid4(),Decimal('10'))
    wallet=await database.fetch_one('SELECT available_credits,lifetime_consumed FROM wallets WHERE user_id=%s',(owner,))
    assert wallet['available_credits']==100 and wallet['lifetime_consumed']==0
    assert not await database.fetch_one("SELECT id FROM credit_ledger WHERE user_id=%s AND entry_type='editor_export_charge'",(owner,))

@pytest.mark.asyncio
async def test_account_deletion_cascades_receipts_and_deleting_owner_cannot_purchase(editor_db):
    owner=await user();await editor.purchase_export(owner,'b'*64,uuid.uuid4(),Decimal('10'))
    await database.execute("UPDATE users SET status='deleting' WHERE id=%s",(owner,))
    with pytest.raises(HTTPException) as exc:await editor.purchase_export(owner,'c'*64,uuid.uuid4(),Decimal('10'))
    assert exc.value.status_code==403
    await database.execute('DELETE FROM users WHERE id=%s',(owner,))
    assert not await database.fetch_one('SELECT id FROM editor_export_receipts WHERE user_id=%s',(owner,))
