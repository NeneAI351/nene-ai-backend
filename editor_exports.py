"""Fixed-price authorization for locally rendered edits; media never enters API.

The phone completes and durably saves its export before requesting purchase.
This endpoint is a paid feature entitlement, not DRM or a claim to verify pixels
on an untrusted device. Ownership, pricing and exactly-once wallet settlement
are enforced here. Rebuilding the identical edit can reuse its entitlement.
"""
from __future__ import annotations
import json
import os
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import database
from auth import get_authenticated_user_id
from account_agreement_store import require_account_agreement
from paid_access import paid_available
from rate_limits import enforce_rate_limit

router = APIRouter(prefix='/api/editor', tags=['local-editor'])
PRICE = Decimal('10')
Digest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]


def enabled():
    return database._pool is not None and os.getenv('NENE_EDITOR_EXPORTS_ENABLED', 'true').lower() == 'true'


async def initialize_schema():
    if database._pool is None:
        return
    async with database._pool.connection() as conn:
      await conn.execute('''CREATE TABLE IF NOT EXISTS editor_export_receipts (
      id UUID PRIMARY KEY, user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      project_digest TEXT NOT NULL CHECK(project_digest ~ '^[a-f0-9]{64}$'),
      request_key UUID NOT NULL, credit_cost NUMERIC(14,2) NOT NULL CHECK(credit_cost>0),
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      UNIQUE(user_id,project_digest), UNIQUE(user_id,request_key));
      ALTER TABLE editor_export_receipts ENABLE ROW LEVEL SECURITY;
      REVOKE ALL ON editor_export_receipts FROM PUBLIC;''')
      await conn.commit()


def public_receipt(row, duplicate=True):
    return {'paid': True, 'receipt_id': str(row['id']), 'project_digest': row['project_digest'],
            'credit_cost': str(row['credit_cost']), 'duplicate': duplicate,
            'message': 'This exact edit is paid. Download or rebuild it without another export fee.'}


@router.get('/availability')
async def availability():
    return {'available': enabled(), 'credit_cost': str(PRICE), 'paid_credits_required': True,
            'editing_and_preview_free': True, 'media_uploaded': False,
            'message': 'Editing and preview are free. Confirm 10 paid credits only after your finished file is ready.'}


@router.get('/exports/{project_digest}')
async def receipt(project_digest: Digest, user_id: str = Depends(get_authenticated_user_id)):
    if database._pool is None:
        raise HTTPException(503, 'Export receipts are temporarily unavailable.')
    row = await database.fetch_one('SELECT id,project_digest,credit_cost FROM editor_export_receipts WHERE user_id=%s AND project_digest=%s', (user_id, project_digest))
    if not row:
        return {'paid': False, 'credit_cost': str(PRICE)}
    return public_receipt(row)


class Purchase(BaseModel):
    model_config = ConfigDict(extra='forbid')
    credit_limit: Decimal = Field(ge=0, le=100)


async def purchase_export(user_id, project_digest, request_key, credit_limit):
    """Replay, charge, ledger and receipt all commit together under wallet lock."""
    if database._pool is None:
        raise HTTPException(503, 'Export billing is temporarily unavailable. Your local file is kept.')
    async with database._pool.connection() as conn:
        async with conn.transaction():
            # Same shared lifecycle lock used by generation and account deletion.
            await conn.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))', ('account-lifecycle:' + user_id,))
            account = await (await conn.execute('SELECT status FROM users WHERE id=%s', (user_id,))).fetchone()
            if not account or account['status'] != 'active':
                raise HTTPException(403, 'This account is no longer active.')
            await conn.execute('INSERT INTO wallets(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING', (user_id,))
            wallet = await (await conn.execute('SELECT available_credits,reserved_credits,lifetime_consumed FROM wallets WHERE user_id=%s FOR UPDATE', (user_id,))).fetchone()
            prior_key = await (await conn.execute('SELECT project_digest FROM editor_export_receipts WHERE user_id=%s AND request_key=%s', (user_id, str(request_key)))).fetchone()
            if prior_key and prior_key['project_digest'] != project_digest:
                raise HTTPException(409, 'This request key belongs to a different edit.')
            row = await (await conn.execute('SELECT id,project_digest,credit_cost FROM editor_export_receipts WHERE user_id=%s AND project_digest=%s', (user_id, project_digest))).fetchone()
            if row:
                return public_receipt(row)
            if not enabled():
                raise HTTPException(503, 'New export purchases are temporarily unavailable. Your local file is kept.')
            if not credit_limit.is_finite() or credit_limit < PRICE:
                raise HTTPException(409, 'Review the current 10-credit export price before confirming.')
            await require_account_agreement(conn, user_id)
            if await paid_available(conn, user_id, wallet) < PRICE:
                raise HTTPException(402, 'You need 10 paid credits for this export. Your finished local file is kept.')
            receipt_id = str(uuid.uuid4())
            balance = Decimal(wallet['available_credits']) - PRICE
            await conn.execute('UPDATE wallets SET available_credits=%s,lifetime_consumed=lifetime_consumed+%s,updated_at=NOW() WHERE user_id=%s', (balance, PRICE, user_id))
            await conn.execute('''INSERT INTO editor_export_receipts(id,user_id,project_digest,request_key,credit_cost)
                VALUES (%s,%s,%s,%s,%s)''', (receipt_id, user_id, project_digest, str(request_key), PRICE))
            operation = 'editor-export:' + receipt_id
            result = {'receipt_id': receipt_id, 'project_digest': project_digest, 'credit_cost': str(PRICE)}
            await conn.execute('''INSERT INTO credit_ledger(id,user_id,entry_type,amount,balance_after,metadata,operation_key)
                VALUES (%s,%s,'editor_export_charge',%s,%s,%s::jsonb,%s)''',
                (str(uuid.uuid4()), user_id, -PRICE, balance, json.dumps(result), operation))
            await conn.execute('''INSERT INTO wallet_operations(id,user_id,operation_key,operation_type,amount,result)
                VALUES (%s,%s,%s,'editor_export',%s,%s::jsonb)''',
                (str(uuid.uuid4()), user_id, operation, PRICE, json.dumps(result)))
            return public_receipt({'id': receipt_id, 'project_digest': project_digest, 'credit_cost': PRICE}, False)


@router.post('/exports/{project_digest}/purchase')
async def purchase(project_digest: Digest, body: Purchase, request: Request,
                   idempotency_key: uuid.UUID = Header(alias='Idempotency-Key'),
                   user_id: str = Depends(get_authenticated_user_id)):
    await enforce_rate_limit(request, bucket='editor-export', limit=30, window_seconds=60)
    return await purchase_export(user_id, project_digest, idempotency_key, body.credit_limit)
