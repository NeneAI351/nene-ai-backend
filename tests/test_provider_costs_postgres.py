"""Real PostgreSQL integration tests for provider-cost persistence.

These tests run only against the disposable PostgreSQL service in GitHub Actions.
They intentionally create and remove only uniquely identified test rows.
"""
import json
import os
import uuid
from decimal import Decimal

import pytest

import database
import provider_costs


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def postgres_generation():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")
    await database.start_database()
    await database.initialize_schema()
    generation_id = str(uuid.uuid4())
    async with database._pool.connection() as conn:
        await conn.execute(
            """INSERT INTO generations
               (id, provider, generation_type, status, request)
               VALUES (%s, 'ltx', 'text_to_video', 'succeeded', %s::jsonb)""",
            (generation_id, json.dumps({"test": "provider-cost-integration"})),
        )
        await conn.commit()
    try:
        yield generation_id
    finally:
        async with database._pool.connection() as conn:
            await conn.execute("DELETE FROM provider_costs WHERE generation_id=%s", (generation_id,))
            await conn.execute("DELETE FROM generations WHERE id=%s", (generation_id,))
            await conn.commit()
        await database.close_database()


def make_record(generation_id, cost="0.78"):
    return {
        "generation_id": generation_id,
        "provider": "ltx",
        "model": "ltx-2-5-fast",
        "units": Decimal("6"),
        "unit_type": "seconds",
        "cost_usd": Decimal(cost),
        "metadata": {
            "record_type": "estimate",
            "estimated": True,
            "source": "postgres_integration_test",
        },
    }


async def fetch_cost_rows(generation_id):
    async with database._pool.connection() as conn:
        cursor = await conn.execute(
            """SELECT id::text AS id, cost_usd, metadata
               FROM provider_costs WHERE generation_id=%s
               ORDER BY created_at""",
            (generation_id,),
        )
        return await cursor.fetchall()


async def test_postgres_estimate_retries_update_single_row(postgres_generation):
    generation_id = postgres_generation
    first = await provider_costs.record_generation_cost_estimate(make_record(generation_id, "0.78"))
    second = await provider_costs.record_generation_cost_estimate(make_record(generation_id, "0.80"))

    rows = await fetch_cost_rows(generation_id)
    assert first["action"] == "inserted_estimate"
    assert second["action"] == "updated_estimate"
    assert len(rows) == 1
    assert Decimal(str(rows[0]["cost_usd"])) == Decimal("0.80")
    assert rows[0]["metadata"]["record_type"] == "estimate"


async def test_postgres_estimate_never_overwrites_actual_record(postgres_generation):
    generation_id = postgres_generation
    actual_id = str(uuid.uuid4())
    async with database._pool.connection() as conn:
        await conn.execute(
            """INSERT INTO provider_costs
               (id, generation_id, provider, model, units, unit_type, cost_usd, metadata)
               VALUES (%s, %s, 'ltx', 'ltx-2-5-fast', 6, 'seconds', 0.91,
                       '{"record_type":"actual","estimated":false}'::jsonb)""",
            (actual_id, generation_id),
        )
        await conn.commit()

    result = await provider_costs.record_generation_cost_estimate(make_record(generation_id, "0.78"))
    rows = await fetch_cost_rows(generation_id)
    assert result["recorded"] is False
    assert result["reason"] == "actual_cost_already_recorded"
    assert len(rows) == 1
    assert rows[0]["id"] == actual_id
    assert Decimal(str(rows[0]["cost_usd"])) == Decimal("0.91")
