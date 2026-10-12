import pytest
from decimal import Decimal
from fastapi import HTTPException

import admin_finance


@pytest.mark.asyncio
async def test_owner_gate_fails_closed_when_owner_not_configured(monkeypatch):
    monkeypatch.delenv("OWNER_ADMIN_USER_ID", raising=False)
    with pytest.raises(HTTPException) as exc:
        await admin_finance._require_owner("owner-uuid")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_owner_gate_rejects_non_owner(monkeypatch):
    monkeypatch.setenv("OWNER_ADMIN_USER_ID", "owner-uuid")
    with pytest.raises(HTTPException) as exc:
        await admin_finance._require_owner("someone-else")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_owner_gate_accepts_configured_owner_case_insensitively(monkeypatch):
    monkeypatch.setenv("OWNER_ADMIN_USER_ID", "ABC-123")
    await admin_finance._require_owner("abc-123")


def test_money_keeps_decimal_precision():
    assert admin_finance._money("12.345") == "12.34"
    assert admin_finance._money(None) == "0.00"



@pytest.mark.asyncio
async def test_finance_summary_separates_test_transactions_from_live_collections(monkeypatch):
    import admin_finance
    import database

    class FakeCursor:
        def __init__(self):
            self.query = ""
            self.fetchone_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, query):
            self.query = query

        async def fetchone(self):
            self.fetchone_calls += 1
            if self.fetchone_calls == 1:
                assert "mode = 'live' AND status = 'success'" in self.query
                assert "mode = 'test' AND status = 'success'" in self.query
                return {
                    "collected_kobo": 250000,
                    "pending_kobo": 50000,
                    "successful_orders": 2,
                    "pending_orders": 1,
                    "failed_orders": 0,
                    "test_collected_kobo": 1000000,
                    "test_successful_orders": 3,
                    "test_pending_orders": 1,
                }
            return {"estimated_cost_usd": Decimal("4.25"), "cost_records": 2, "estimated_records": 2}

        async def fetchall(self):
            assert "mode" in self.query
            return [{
                "reference": "nene_example",
                "package_id": "starter",
                "amount_kobo": 500000,
                "currency": "NGN",
                "mode": "test",
                "status": "success",
                "paid_at": None,
                "created_at": None,
            }]

    class FakeConnection:
        def __init__(self):
            self.fake_cursor = FakeCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def cursor(self):
            return self.fake_cursor

    class FakePool:
        def connection(self):
            return FakeConnection()

    monkeypatch.setenv("OWNER_ADMIN_USER_ID", "owner-uuid")
    monkeypatch.setattr(database, "database_configured", lambda: True)
    monkeypatch.setattr(database, "_pool", FakePool())

    summary = await admin_finance.owner_finance_summary(user_id="owner-uuid")

    assert summary["collections"]["collected_ngn"] == "2500.00"
    assert summary["collections"]["pending_ngn"] == "500.00"
    assert summary["test_mode"]["collected_ngn"] == "10000.00"
    assert summary["test_mode"]["is_real_revenue"] is False
    assert summary["recent_orders"][0]["mode"] == "test"
    assert summary["withdrawable_balance_ngn"] is None
    assert summary["payouts_enabled"] is False
