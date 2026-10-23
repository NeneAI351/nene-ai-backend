import asyncio

import pytest
from fastapi import HTTPException

import wallet


def test_wallet_read_requires_database(monkeypatch):
    monkeypatch.setattr(wallet, "database_configured", lambda: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(wallet.get_wallet("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    assert exc.value.status_code == 503


def test_wallet_read_uses_authenticated_user_id(monkeypatch):
    user_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    expected = {"ok": True, "wallet": {"user_id": user_id, "available_credits": "12.50"}}
    monkeypatch.setattr(wallet, "database_configured", lambda: True)
    calls = []

    async def fake_balance(received_user_id):
        calls.append(received_user_id)
        return expected

    monkeypatch.setattr(wallet, "wallet_balance", fake_balance)
    result = asyncio.run(wallet.get_wallet(user_id))
    assert result == expected
    assert calls == [user_id]


@pytest.mark.parametrize(("requested", "expected"), [(0, 1), (-50, 1), (1, 1), (50, 50), (100, 100), (500, 100)])
def test_wallet_ledger_clamps_limit(monkeypatch, requested, expected):
    monkeypatch.setattr(wallet, "database_configured", lambda: True)
    calls = []

    async def fake_ledger(user_id, limit):
        calls.append((user_id, limit))
        return []

    monkeypatch.setattr(wallet, "wallet_ledger", fake_ledger)
    user_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    result = asyncio.run(wallet.get_wallet_ledger(requested, user_id))
    assert result == {"items": []}
    assert calls == [(user_id, expected)]


def test_wallet_ledger_requires_database(monkeypatch):
    monkeypatch.setattr(wallet, "database_configured", lambda: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(wallet.get_wallet_ledger(50, "cccccccc-cccc-4ccc-8ccc-cccccccccccc"))
    assert exc.value.status_code == 503
