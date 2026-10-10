"""Atomic PostgreSQL wallet operations for NENE AI.

Every mutation locks the wallet row and updates wallet, generation reservation,
operation idempotency record and credit ledger inside one transaction.
Caller must provide a user ID established by verified authentication.
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
CREATE TABLE IF NOT EXISTS wallet_reservations (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    generation_id UUID NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
    reserved_remaining NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (reserved_remaining >= 0),
    total_reserved NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (total_reserved >= 0),
    total_captured NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (total_captured >= 0),
    total_released NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (total_released >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(user_id, generation_id)
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


async def _require_pool() -> None:
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
    return await database.fetch_all(
        """SELECT id::text, generation_id::text, entry_type, amount,
                  balance_after, metadata, created_at
           FROM credit_ledger WHERE user_id = %s
           ORDER BY created_at DESC LIMIT %s""", (user_id, limit)
    )


async def _apply(user_id: str, amount: Decimal, operation_type: str,
                 generation_id: str, operation_key: str):
    await _require_pool()
    amount = Decimal(amount)
    if not amount.is_finite() or amount <= 0 or amount.quantize(Decimal("0.01")) != amount:
        raise ValueError("Amount must be a positive credit amount with at most two decimal places.")

    async with database._pool.connection() as conn:
        async with conn.transaction():
            # Create and lock first. This serializes all wallet operations for a
            # user, including concurrent requests with the same idempotency key.
            await conn.execute(
                "INSERT INTO wallets(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            row = await (await conn.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s FOR UPDATE",
                (user_id,),
            )).fetchone()

            prior = await (await conn.execute(
                """SELECT operation_type, amount, generation_id::text, result
                   FROM wallet_operations WHERE user_id=%s AND operation_key=%s""",
                (user_id, operation_key),
            )).fetchone()
            if prior:
                if (prior["operation_type"] != operation_type
                    or Decimal(prior["amount"]) != amount
                    or prior["generation_id"] != generation_id):
                    raise ValueError("This idempotency key was already used for a different wallet operation.")
                return {"ok": True, "duplicate": True, **(prior["result"] or {})}

            available = Decimal(row["available_credits"])
            reserved = Decimal(row["reserved_credits"])
            reservation = await (await conn.execute(
                """SELECT reserved_remaining FROM wallet_reservations
                   WHERE user_id=%s AND generation_id=%s FOR UPDATE""",
                (user_id, generation_id),
            )).fetchone()

            if operation_type == "reserve":
                if available < amount:
                    return None
                available -= amount
                reserved += amount
                remaining = (Decimal(reservation["reserved_remaining"]) if reservation else Decimal("0")) + amount
                if reservation:
                    await conn.execute(
                        """UPDATE wallet_reservations SET reserved_remaining=%s,
                           total_reserved=total_reserved+%s, updated_at=NOW()
                           WHERE user_id=%s AND generation_id=%s""",
                        (remaining, amount, user_id, generation_id),
                    )
                else:
                    await conn.execute(
                        """INSERT INTO wallet_reservations
                           (user_id,generation_id,reserved_remaining,total_reserved)
                           VALUES (%s,%s,%s,%s)""",
                        (user_id, generation_id, amount, amount),
                    )
                ledger_type, ledger_amount = "reserve", -amount
                lifetime_delta = Decimal("0")
            elif operation_type in {"release", "capture"}:
                if not reservation or Decimal(reservation["reserved_remaining"]) < amount:
                    raise ValueError(f"Cannot {operation_type} more credits than are reserved for this generation.")
                remaining = Decimal(reservation["reserved_remaining"]) - amount
                await conn.execute(
                    f"""UPDATE wallet_reservations SET reserved_remaining=%s,
                       total_{'released' if operation_type == 'release' else 'captured'}=
                         total_{'released' if operation_type == 'release' else 'captured'}+%s,
                       updated_at=NOW() WHERE user_id=%s AND generation_id=%s""",
                    (remaining, amount, user_id, generation_id),
                )
                reserved -= amount
                if operation_type == "release":
                    available += amount
                    ledger_type, ledger_amount = "release", amount
                    lifetime_delta = Decimal("0")
                else:
                    ledger_type, ledger_amount = "generation_charge", Decimal("0")
                    lifetime_delta = amount
            else:
                raise ValueError("Unsupported wallet operation.")

            await conn.execute(
                """UPDATE wallets SET available_credits=%s, reserved_credits=%s,
                   lifetime_consumed=lifetime_consumed+%s, updated_at=NOW()
                   WHERE user_id=%s""",
                (available, reserved, lifetime_delta, user_id),
            )
            result = {
                "available_credits": str(available),
                "reserved_credits": str(reserved),
                "operation_type": operation_type,
                "amount": str(amount),
                "generation_id": generation_id,
            }
            await conn.execute(
                """INSERT INTO wallet_operations
                   (id,user_id,operation_key,operation_type,amount,generation_id,result)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (str(uuid.uuid4()), user_id, operation_key, operation_type,
                 amount, generation_id, json.dumps(result)),
            )
            await conn.execute(
                """INSERT INTO credit_ledger
                   (id,user_id,generation_id,entry_type,amount,balance_after,metadata,operation_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                (str(uuid.uuid4()), user_id, generation_id, ledger_type, ledger_amount,
                 available, json.dumps({
                     "operation_key": operation_key,
                     "operation_type": operation_type,
                     "amount": str(amount),
                     "available_delta": str(ledger_amount),
                     "reserved_delta": str(amount if operation_type == "reserve" else -amount),
                 }), operation_key),
            )
            return {"ok": True, "duplicate": False, **result}


async def wallet_reserve(user_id: str, amount: Decimal, generation_id: str, idempotency_key: str):
    return await _apply(user_id, amount, "reserve", generation_id, f"reserve:{idempotency_key}")


async def wallet_release(user_id: str, amount: Decimal, generation_id: str, idempotency_key: str):
    return await _apply(user_id, amount, "release", generation_id, f"release:{idempotency_key}")


async def wallet_capture(user_id: str, amount: Decimal, generation_id: str, idempotency_key: str):
    return await _apply(user_id, amount, "capture", generation_id, f"capture:{idempotency_key}")


async def wallet_grant(user_id: str, amount: Decimal, reason: str, reference: str):
    raise NotImplementedError("Credit grants remain disabled until verified payment fulfillment or an authorized admin service is implemented.")
