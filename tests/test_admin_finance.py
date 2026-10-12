import pytest
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
