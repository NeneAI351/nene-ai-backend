"""Durable PostgreSQL outbox primitives for generation dispatch.

This module intentionally does not call video providers. A worker must claim a
row, verify generation state and wallet reservation, then dispatch using provider
idempotency where available. Expired leases may be reclaimed; ambiguous provider
submissions must be reconciled before another submission is attempted.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import database

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS generation_outbox (
    id UUID PRIMARY KEY,
    generation_id UUID NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ('dispatch_generation', 'reconcile_generation')),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'processing', 'completed', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(generation_id, event_type)
);
ALTER TABLE generation_outbox ADD COLUMN IF NOT EXISTS lease_token UUID;
CREATE INDEX IF NOT EXISTS idx_generation_outbox_claim
    ON generation_outbox(state, available_at, created_at);
CREATE INDEX IF NOT EXISTS idx_generation_outbox_lease
    ON generation_outbox(lease_expires_at) WHERE state='processing';
"""


class OutboxConflict(ValueError):
    """An outbox operation violates its state or ownership invariant."""


async def initialize_outbox_schema() -> None:
    if database._pool is None:
        return
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(OUTBOX_SCHEMA)


async def enqueue_generation_event(
    generation_id: str,
    event_type: str = "dispatch_generation",
    payload: dict[str, Any] | None = None,
) -> str:
    """Insert one durable event per generation/event type; safe to replay."""
    try:
        generation_uuid = str(uuid.UUID(generation_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid generation identifier.") from exc
    if event_type not in {"dispatch_generation", "reconcile_generation"}:
        raise ValueError("Unsupported generation outbox event.")
    body = json.dumps(payload or {}, separators=(",", ":"), ensure_ascii=False)
    if len(body.encode("utf-8")) > 16 * 1024:
        raise ValueError("Outbox payload is too large.")
    event_id = str(uuid.uuid4())
    pool = database._pool
    if pool is None:
        raise RuntimeError("Durable outbox storage is required.")
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute(
                """INSERT INTO generation_outbox
                   (id,generation_id,event_type,payload)
                   VALUES (%s,%s,%s,%s::jsonb)
                   ON CONFLICT(generation_id,event_type) DO UPDATE
                     SET generation_id=EXCLUDED.generation_id
                   RETURNING id::text""",
                (event_id, generation_uuid, event_type, body),
            )).fetchone()
            return row["id"]


async def claim_events(worker_id: str, limit: int = 10, lease_seconds: int = 60) -> list[dict[str, Any]]:
    """Claim available events atomically across workers using SKIP LOCKED."""
    worker = (worker_id or "").strip()
    if not worker or len(worker) > 200:
        raise ValueError("Invalid worker identifier.")
    if not 1 <= limit <= 100 or not 10 <= lease_seconds <= 900:
        raise ValueError("Invalid outbox claim limits.")
    pool = database._pool
    if pool is None:
        raise RuntimeError("Durable outbox storage is required.")
    async with pool.connection() as conn:
        async with conn.transaction():
            rows = await (await conn.execute(
                """WITH candidates AS (
                     SELECT id FROM generation_outbox
                     WHERE (state='pending' AND available_at <= NOW())
                        OR (state='processing' AND lease_expires_at < NOW())
                     ORDER BY available_at, created_at
                     FOR UPDATE SKIP LOCKED
                     LIMIT %s
                   )
                   UPDATE generation_outbox AS o
                   SET state='processing', lease_owner=%s, lease_token=gen_random_uuid(),
                       lease_expires_at=NOW() + (%s * INTERVAL '1 second'),
                       attempts=o.attempts+1, updated_at=NOW()
                   FROM candidates c
                   WHERE o.id=c.id
                   RETURNING o.id::text, o.generation_id::text, o.event_type,
                             o.payload, o.attempts, o.lease_token::text, o.lease_expires_at""",
                (limit, worker, lease_seconds),
            )).fetchall()
            return list(rows)


async def complete_event(event_id: str, worker_id: str, lease_token: str) -> None:
    """Only the current fenced lease may acknowledge a claimed event."""
    await _update_leased_event(event_id, worker_id, lease_token)


async def renew_event(event_id: str, worker_id: str, lease_token: str, lease_seconds: int = 120) -> None:
    """Extend a current, unexpired lease; never resurrect a lost claim."""
    worker = (worker_id or "").strip()
    if not worker or len(worker) > 200 or not 10 <= lease_seconds <= 900:
        raise ValueError("Invalid worker or lease duration.")
    try:
        event_uuid, token_uuid = str(uuid.UUID(event_id)), str(uuid.UUID(lease_token))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid event or lease token.") from exc
    if database._pool is None:
        raise RuntimeError("Durable outbox storage is required.")
    async with database._pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute(
                """UPDATE generation_outbox
                   SET lease_expires_at=NOW()+(%s * INTERVAL '1 second'),updated_at=NOW()
                   WHERE id=%s AND state='processing' AND lease_owner=%s AND lease_token=%s
                     AND lease_expires_at > NOW() RETURNING id""",
                (lease_seconds,event_uuid,worker,token_uuid),
            )).fetchone()
            if not row:
                raise OutboxConflict("Cannot renew an expired or replaced lease.")


async def retry_event(
    event_id: str,
    worker_id: str,
    error_code: str,
    delay_seconds: int = 30,
    max_attempts: int = 12,
    lease_token: str | None = None,
) -> None:
    """Schedule bounded retry; exhausted events become dead-lettered."""
    code = (error_code or "worker_error").strip()
    if not code or len(code) > 100 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in code):
        raise ValueError("Invalid stable error code.")
    if not 1 <= delay_seconds <= 86400 or not 1 <= max_attempts <= 100:
        raise ValueError("Invalid retry policy.")
    if not lease_token:
        raise ValueError("Lease token is required.")
    try:
        token_uuid = str(uuid.UUID(lease_token))
        event_uuid = str(uuid.UUID(event_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid event or lease token.") from exc
    pool = database._pool
    if pool is None:
        raise RuntimeError("Durable outbox storage is required.")
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute(
                """UPDATE generation_outbox
                   SET state=CASE WHEN attempts >= %s THEN 'dead' ELSE 'pending' END,
                       available_at=NOW() + (%s * INTERVAL '1 second'),
                       lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                       last_error_code=%s, updated_at=NOW()
                   WHERE id=%s AND state='processing' AND lease_owner=%s AND lease_token=%s
                   RETURNING state""",
                (max_attempts, delay_seconds, code, event_uuid, worker_id, token_uuid),
            )).fetchone()
            if not row:
                raise OutboxConflict("Event lease is missing, expired, or owned by another worker.")


async def _update_leased_event(event_id: str, worker_id: str, lease_token: str) -> None:
    try:
        event_uuid = str(uuid.UUID(event_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid outbox event identifier.") from exc
    worker = (worker_id or "").strip()
    if not worker:
        raise ValueError("Invalid worker identifier.")
    try:
        token_uuid = str(uuid.UUID(lease_token))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid lease token.") from exc
    pool = database._pool
    if pool is None:
        raise RuntimeError("Durable outbox storage is required.")
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute(
                """UPDATE generation_outbox SET state='completed', lease_owner=NULL,
                   lease_token=NULL, lease_expires_at=NULL, last_error_code=NULL, updated_at=NOW()
                   WHERE id=%s AND state='processing' AND lease_owner=%s AND lease_token=%s
                   RETURNING id""",
                (event_uuid, worker, token_uuid),
            )).fetchone()
            if not row:
                raise OutboxConflict("Event lease is missing, expired, or owned by another worker.")
