import os
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASE_ENABLED = bool(DATABASE_URL)

_pool: Optional[AsyncConnectionPool] = None


async def start_database() -> None:
    global _pool
    if not DATABASE_ENABLED or _pool is not None:
        return
    _pool = AsyncConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=int(os.getenv("DATABASE_POOL_MAX", "5")),
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await _pool.open()
    await _pool.wait()


async def close_database() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def database_configured() -> bool:
    return DATABASE_ENABLED


async def database_ping() -> bool:
    if _pool is None:
        return False
    async with _pool.connection() as conn:
        await conn.execute("SELECT 1")
    return True


async def execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    if _pool is None:
        raise RuntimeError("Database is not configured.")
    async with _pool.connection() as conn:
        await conn.execute(sql, params)
        await conn.commit()


async def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
    if _pool is None:
        raise RuntimeError("Database is not configured.")
    async with _pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()


async def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if _pool is None:
        raise RuntimeError("Database is not configured.")
    async with _pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()


async def initialize_schema() -> None:
    if _pool is None:
        return
    schema = """
    CREATE TABLE IF NOT EXISTS users (
        id UUID PRIMARY KEY,
        email TEXT UNIQUE,
        display_name TEXT,
        avatar_url TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS projects (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_projects_user_updated ON projects(user_id, updated_at DESC);

    CREATE TABLE IF NOT EXISTS characters (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        profile JSONB NOT NULL DEFAULT '{}'::jsonb,
        reference_asset_url TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_characters_user ON characters(user_id);

    CREATE TABLE IF NOT EXISTS generations (
        id UUID PRIMARY KEY,
        user_id UUID REFERENCES users(id) ON DELETE SET NULL,
        project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
        provider TEXT NOT NULL,
        provider_job_id TEXT,
        generation_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        request JSONB NOT NULL,
        result JSONB,
        error JSONB,
        credits_reserved NUMERIC(12,2) NOT NULL DEFAULT 0,
        credits_charged NUMERIC(12,2) NOT NULL DEFAULT 0,
        credits_refunded NUMERIC(12,2) NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ
    );
    CREATE INDEX IF NOT EXISTS idx_generations_user_updated ON generations(user_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_generations_provider_job ON generations(provider, provider_job_id);

    CREATE TABLE IF NOT EXISTS credit_ledger (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        generation_id UUID REFERENCES generations(id) ON DELETE SET NULL,
        entry_type TEXT NOT NULL,
        amount NUMERIC(12,2) NOT NULL,
        balance_after NUMERIC(12,2),
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_credit_ledger_user_created ON credit_ledger(user_id, created_at DESC);

    CREATE TABLE IF NOT EXISTS subscriptions (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        external_customer_id TEXT,
        external_subscription_id TEXT,
        plan_id TEXT NOT NULL,
        status TEXT NOT NULL,
        current_period_start TIMESTAMPTZ,
        current_period_end TIMESTAMPTZ,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(provider, external_subscription_id)
    );
    CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);

    CREATE TABLE IF NOT EXISTS provider_costs (
        id UUID PRIMARY KEY,
        generation_id UUID REFERENCES generations(id) ON DELETE SET NULL,
        provider TEXT NOT NULL,
        model TEXT,
        units NUMERIC(12,4) NOT NULL DEFAULT 0,
        unit_type TEXT,
        cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_provider_costs_generation ON provider_costs(generation_id);

    CREATE TABLE IF NOT EXISTS provider_rate_cards (
        id UUID PRIMARY KEY,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        resolution TEXT NOT NULL,
        usd_per_second NUMERIC(12,8) NOT NULL,
        cost_basis TEXT NOT NULL,
        estimated BOOLEAN NOT NULL DEFAULT FALSE,
        source_url TEXT,
        effective_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        active BOOLEAN NOT NULL DEFAULT TRUE,
        UNIQUE(provider, model, resolution)
    );
    CREATE INDEX IF NOT EXISTS idx_provider_rate_cards_lookup
        ON provider_rate_cards(provider, model, resolution, active);

    CREATE TABLE IF NOT EXISTS pricing_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        nene_credit_usd NUMERIC(12,6) NOT NULL DEFAULT 0.01,
        target_gross_margin NUMERIC(6,5) NOT NULL DEFAULT 0.70,
        provider_risk_buffer NUMERIC(6,5) NOT NULL DEFAULT 0.10,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS generation_idempotency (
        id UUID PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        request_fingerprint TEXT NOT NULL,
        response JSONB,
        generation_id UUID REFERENCES generations(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ
    );
    CREATE INDEX IF NOT EXISTS idx_generation_idempotency_created ON generation_idempotency(created_at);
    """
    async with _pool.connection() as conn:
        await conn.execute(schema)
        await conn.commit()


async def get_idempotency(key: str) -> Optional[dict[str, Any]]:
    return await fetch_one(
        """SELECT idempotency_key, request_fingerprint, response, generation_id
           FROM generation_idempotency
           WHERE idempotency_key = %s""",
        (key,),
    )


async def create_idempotency(key: str, fingerprint: str, record_id: str) -> bool:
    result = await fetch_one(
        """INSERT INTO generation_idempotency
           (id, idempotency_key, request_fingerprint)
           VALUES (%s, %s, %s)
           ON CONFLICT (idempotency_key) DO NOTHING
           RETURNING idempotency_key""",
        (record_id, key, fingerprint),
    )
    return result is not None


async def complete_idempotency(key: str, response: dict[str, Any], generation_id: Optional[str] = None) -> None:
    await execute(
        """UPDATE generation_idempotency
           SET response = %s::jsonb, generation_id = %s, completed_at = NOW()
           WHERE idempotency_key = %s""",
        (__import__("json").dumps(response), generation_id, key),
    )


async def delete_idempotency(key: str) -> None:
    await execute("DELETE FROM generation_idempotency WHERE idempotency_key = %s", (key,))
