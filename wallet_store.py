"""Atomic PostgreSQL wallet operations for NENE AI.

The caller must supply a verified user ID from the authentication layer.
All balance-changing operations lock the wallet row and write the ledger in the
same transaction. Idempotency keys are unique per user and operation.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import database


WALLET_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    available_credits NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (available_credits >= 0),
    reserved_credits NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (reserved_credits >= 0),
    lifetime_purchased NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (lifetime_purchased >= 0),
    lifetime_bonus NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (lifetime_bonus >= 0),
    lifetime_consumed NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (lifetime_consumed >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS wallet_operations (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    operation_key TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    amount NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    generation_id UUID REFERENCES generations(id) ON DELETE SET NULL,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, operation_key)
);
CREATE INDEX IF NOT EXISTS idx_wallet_operations_user_created
    ON wallet_operations(user_id, created_at DESC);
ALTER TABLE credit_ledger ADD COLUMN IF NOT EXISTS operation_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_ledger_user_operation
    ON credit_ledger(user_id, operation_key) WHERE operation_key IS NOT NULL;
"""


async def initialize_wallet_schema() -> None:
    if database._pool is None:
        return
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(WALLET_SCHEMA)


async def _require_pool():
    if database._pool is None:
        raise RuntimeError("NENE AI database is not configured.")


async def ensure_wallet(user_id: str) -> None:
    await _require_pool()
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO wallets(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )


async def wallet_balance(user_id: str) -> dict[str, Any]:
    await ensure_wallet(user_id)
    row = await database.fetch_one(
        """SELECT user_id::text, available_credits, reserved_credits,
                  lifetime_purchased, lifetime_bonus, lifetime_consumed,
                  created_at, updated_at
           FROM wallets WHERE user_id = %s""", (user_id,)
    )
    return {"ok": True, "wallet": row}


async def wallet_ledger(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = await database.fetch_all(
        """SELECT id::text, generation_id::text, entry_type, amount,
                  balance_after, metadata, created_at
           FROM credit_ledger WHERE user_id = %s
           ORDER BY created_at DESC LIMIT %s""", (user_id, limit)
    )
    return rows


async def _apply(user_id: str, amount: Decimal, operation_type: str,
                 generation_id: str | None, operation_key: str) -> dict[str, Any]:
    await _require_pool()
    if amount <= 0:
        raise ValueError("Amount must be positive.")
    async with database._pool.connection() as conn:
        async with conn.transaction():
            prior = await (await conn.execute(
                """SELECT result FROM wallet_operations
                   WHERE user_id=%s AND operation_key=%s""",
                (user_id, operation_key),
            )).fetchone()
            if prior:
                return {"ok": True, "duplicate": True, **(prior["result"] or {})}

            await conn.execute(
                "INSERT INTO wallets(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            row = await (await conn.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s FOR UPDATE",
                (user_id,),
            )).fetchone()
            available = Decimal(row["available_credits"])
            reserved = Decimal(row["reserved_credits"])
            if operation_type == "reserve":
                if available < amount:
                    return None
                available -= amount
                reserved += amount
                ledger_type = "reserve"
            elif operation_type == "release":
                if reserved < amount:
                    raise ValueError("Cannot release more credits than are reserved.")
                reserved -= amount
                available += amount
                ledger_type = "release"
            elif operation_type == "capture":
                if reserved < amount:
                    raise ValueError("Cannot capture more credits than are reserved.")
                reserved -= amount
                ledger_type = "generation_charge"
            else:
                raise ValueError("Unsupported wallet operation.")

            await conn.execute(
                """UPDATE wallets SET available_credits=%s, reserved_credits=%s,
                   lifetime_consumed=lifetime_consumed + %s, updated_at=NOW()
                   WHERE user_id=%s""",
                (available, reserved, amount if operation_type == "capture" else Decimal("0"), user_id),
            )
            result = {
                "available_credits": str(available),
                "reserved_credits": str(reserved),
                "operation_type": operation_type,
            }
            op_id = str(uuid.uuid4())
            await conn.execute(
                """INSERT INTO wallet_operations
                   (id,user_id,operation_key,operation_type,amount,generation_id,result)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (op_id, user_id, operation_key, operation_type, amount, generation_id,
                 json.dumps(result)),
            )
            await conn.execute(
                """INSERT INTO credit_ledger
                   (id,user_id,generation_id,entry_type,amount,balance_after,metadata,operation_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                (str(uuid.uuid4()), user_id, generation_id, ledger_type,
                 -amount if operation_type == "capture" else Decimal("0"),
                 available, json.dumps({"operation_key": operation_key, "reserved_delta": str(
                     amount if operation_type == "reserve" else -amount)}), operation_key),
            )
            return {"ok": True, "duplicate": False, **result}


async def wallet_reserve(user_id: str, amount: Decimal, generation_id: str, idempotency_key: str):
    return await _apply(user_id, amount, "reserve", generation_id, f"reserve:{idempotency_key}")


async def wallet_release(user_id: str, amount: Decimal, generation_id: str, idempotency_key: str):
    return await _apply(user_id, amount, "release", generation_id, f"release:{idempotency_key}")


async def wallet_capture(user_id: str, amount: Decimal, generation_id: str, idempotency_key: str):
    return await _apply(user_id, amount, "capture", generation_id, f"capture:{idempotency_key}")


async def wallet_grant(user_id: str, amount: Decimal, reason: str, reference: str):
    raise NotImplementedError("Grant fulfillment is intentionally withheld until authenticated payment/admin services are implemented.")
