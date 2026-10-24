"""Tests for validated provider-cost estimates and retry-safe persistence."""
from decimal import Decimal
from types import SimpleNamespace
import uuid

import pytest

import provider_costs


GENERATION_ID = "00000000-0000-4000-8000-000000000001"
QUOTE = {
    "provider": "ltx",
    "model": "ltx-2-5-fast",
    "resolution": "1080p",
    "duration_seconds": 6.0,
    "provider_cost_usd": 0.78,
    "provider_cost_with_risk_buffer_usd": 0.858,
    "cost_basis": "official_api",
    "estimated_provider_cost": False,
}


def build_record():
    return provider_costs.estimate_generation_cost(
        generation_id=GENERATION_ID,
        provider="ltx",
        model="ltx-2-5-fast",
        resolution="1080p",
        duration_seconds=6,
        rate_quote=QUOTE,
    )


def test_estimate_is_explicitly_labelled_and_keeps_buffer_separate():
    record = build_record()
    assert record["generation_id"] == GENERATION_ID
    assert record["cost_usd"] == Decimal("0.78")
    assert record["metadata"]["record_type"] == "estimate"
    assert record["metadata"]["estimated"] is True
    assert record["metadata"]["buffered_cost_usd"] == "0.858"
    assert record["metadata"]["unbuffered_cost_usd"] == "0.78"


@pytest.mark.parametrize("field,value", [
    ("provider_cost_usd", float("nan")),
    ("provider_cost_usd", -0.01),
    ("provider_cost_with_risk_buffer_usd", 0.1),
])
def test_invalid_cost_values_are_rejected(field, value):
    quote = dict(QUOTE)
    quote[field] = value
    with pytest.raises(ValueError):
        provider_costs.estimate_generation_cost(
            generation_id=GENERATION_ID,
            provider="ltx",
            model="ltx-2-5-fast",
            resolution="1080p",
            duration_seconds=6,
            rate_quote=quote,
        )


def test_estimate_rejects_mismatched_quote_identity_or_duration():
    for updates in ({"model": "ltx-2-5-pro"}, {"resolution": "720p"}, {"duration_seconds": 8}):
        quote = dict(QUOTE, **updates)
        with pytest.raises(ValueError):
            provider_costs.estimate_generation_cost(
                generation_id=GENERATION_ID,
                provider="ltx",
                model="ltx-2-5-fast",
                resolution="1080p",
                duration_seconds=6,
                rate_quote=quote,
            )


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    async def fetchone(self):
        return self.row


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeConnection:
    def __init__(self, existing=None):
        self.existing = existing
        self.calls = []

    def transaction(self):
        return FakeTransaction()

    async def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "SELECT id::text, metadata" in sql:
            return FakeResult(self.existing)
        return FakeResult()


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def connection(self):
        class Context:
            async def __aenter__(self_nonlocal):
                return self.conn
            async def __aexit__(self_nonlocal, *_args):
                return False
        return Context()


@pytest.mark.asyncio
async def test_persistence_inserts_once_under_transaction(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(provider_costs.database, "_pool", FakePool(conn))
    result = await provider_costs.record_generation_cost_estimate(build_record())
    assert result["recorded"] is True
    assert result["action"] == "inserted_estimate"
    assert any("pg_advisory_xact_lock" in sql for sql, _ in conn.calls)
    assert sum("INSERT INTO provider_costs" in sql for sql, _ in conn.calls) == 1


@pytest.mark.asyncio
async def test_existing_actual_cost_is_never_overwritten_by_estimate(monkeypatch):
    conn = FakeConnection(existing={
        "id": str(uuid.uuid4()),
        "metadata": {"record_type": "actual", "estimated": False},
    })
    monkeypatch.setattr(provider_costs.database, "_pool", FakePool(conn))
    result = await provider_costs.record_generation_cost_estimate(build_record())
    assert result["recorded"] is False
    assert result["reason"] == "actual_cost_already_recorded"
    assert not any("UPDATE provider_costs" in sql or "INSERT INTO provider_costs" in sql for sql, _ in conn.calls)


@pytest.mark.asyncio
async def test_existing_estimate_is_updated_not_duplicated(monkeypatch):
    cost_id = str(uuid.uuid4())
    conn = FakeConnection(existing={
        "id": cost_id,
        "metadata": {"record_type": "estimate", "estimated": True},
    })
    monkeypatch.setattr(provider_costs.database, "_pool", FakePool(conn))
    result = await provider_costs.record_generation_cost_estimate(build_record())
    assert result == {"recorded": True, "action": "updated_estimate", "id": cost_id}
    assert sum("UPDATE provider_costs" in sql for sql, _ in conn.calls) == 1
    assert not any("INSERT INTO provider_costs" in sql for sql, _ in conn.calls)
