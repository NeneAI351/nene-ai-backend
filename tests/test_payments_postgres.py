"""PostgreSQL integration tests for Paystack payment fulfilment.

These run against the disposable PostgreSQL service configured in CI. They exercise
real transactions, row/advisory locks, unique constraints, wallet balance updates,
and the credit ledger; no real Paystack API or live money is used.
"""
import asyncio
import json
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException

import database
import payments
from wallet_store import initialize_wallet_schema


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def payment_db():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL payment integration tests")
    await database.start_database()
    await database.initialize_schema()
    await payments.initialize_payment_schema()
    await initialize_wallet_schema()
    user_id = str(uuid.uuid4())
    async with database._pool.connection() as conn:
        await conn.execute(
            "INSERT INTO users(id,email,display_name) VALUES (%s,%s,'payment integration test')",
            (user_id, f"{user_id}@example.test"),
        )
        await conn.commit()
    try:
        yield user_id
    finally:
        async with database._pool.connection() as conn:
            await conn.execute("DELETE FROM users WHERE id=%s", (user_id,))
            await conn.commit()
        await database.close_database()


async def create_order(user_id, *, reference=None, amount_kobo=500000, credits="400"):
    reference = reference or "nene_" + uuid.uuid4().hex
    async with database._pool.connection() as conn:
        await conn.execute(
            """INSERT INTO payment_orders
               (id,user_id,reference,package_id,amount_kobo,currency,credits,status)
               VALUES (%s,%s,%s,'starter',%s,'NGN',%s,'pending')""",
            (str(uuid.uuid4()), user_id, reference, amount_kobo, Decimal(credits)),
        )
        await conn.commit()
    return reference


def successful_transaction(reference, *, transaction_id=987654321, amount=500000, currency="NGN"):
    return {
        "reference": reference,
        "status": "success",
        "id": transaction_id,
        "amount": amount,
        "currency": currency,
    }


async def wallet_snapshot(user_id):
    row = await database.fetch_one(
        """SELECT available_credits,lifetime_purchased FROM wallets WHERE user_id=%s""",
        (user_id,),
    )
    ledger = await database.fetch_all(
        """SELECT entry_type,amount,operation_key,metadata FROM credit_ledger
           WHERE user_id=%s AND operation_key LIKE %s""",
        (user_id, "payment:%"),
    )
    return row, ledger


async def test_postgres_duplicate_success_notifications_credit_once(payment_db):
    user_id = payment_db
    reference = await create_order(user_id)
    transaction = successful_transaction(reference)

    first, second = await asyncio.gather(
        payments._fulfill_verified_transaction(transaction),
        payments._fulfill_verified_transaction(transaction),
    )
    wallet, ledger = await wallet_snapshot(user_id)

    assert sorted([first.get("duplicate", False), second.get("duplicate", False)]) == [False, True]
    assert Decimal(wallet["available_credits"]) == Decimal("400")
    assert Decimal(wallet["lifetime_purchased"]) == Decimal("400")
    assert len(ledger) == 1
    assert ledger[0]["operation_key"] == "payment:" + reference


async def test_postgres_transaction_id_cannot_be_reused_for_another_order(payment_db):
    user_id = payment_db
    first_reference = await create_order(user_id)
    second_reference = await create_order(user_id)
    tx_id = 123450001

    first = await payments._fulfill_verified_transaction(
        successful_transaction(first_reference, transaction_id=tx_id)
    )
    assert first["credited"] is True

    with pytest.raises(HTTPException) as exc:
        await payments._fulfill_verified_transaction(
            successful_transaction(second_reference, transaction_id=tx_id)
        )
    assert exc.value.status_code == 409

    wallet, ledger = await wallet_snapshot(user_id)
    assert Decimal(wallet["available_credits"]) == Decimal("400")
    assert len(ledger) == 1


async def test_postgres_amount_mismatch_does_not_create_or_credit_wallet(payment_db):
    user_id = payment_db
    reference = await create_order(user_id)

    with pytest.raises(HTTPException) as exc:
        await payments._fulfill_verified_transaction(
            successful_transaction(reference, amount=499999)
        )
    assert exc.value.status_code == 400
    assert "amount" in str(exc.value.detail).lower()

    wallet = await database.fetch_one("SELECT available_credits FROM wallets WHERE user_id=%s", (user_id,))
    ledger = await database.fetch_all("SELECT id FROM credit_ledger WHERE user_id=%s", (user_id,))
    order = await database.fetch_one("SELECT status,provider_transaction_id FROM payment_orders WHERE reference=%s", (reference,))
    assert wallet is None
    assert ledger == []
    assert order["status"] == "pending"
    assert order["provider_transaction_id"] is None


async def test_postgres_currency_mismatch_does_not_credit_wallet(payment_db):
    user_id = payment_db
    reference = await create_order(user_id)

    with pytest.raises(HTTPException) as exc:
        await payments._fulfill_verified_transaction(
            successful_transaction(reference, currency="USD")
        )
    assert exc.value.status_code == 400
    wallet, ledger = await wallet_snapshot(user_id)
    assert wallet is None
    assert ledger == []


async def test_postgres_repeated_notification_with_different_transaction_id_is_rejected(payment_db):
    user_id = payment_db
    reference = await create_order(user_id)
    await payments._fulfill_verified_transaction(
        successful_transaction(reference, transaction_id=123450002)
    )

    with pytest.raises(HTTPException) as exc:
        await payments._fulfill_verified_transaction(
            successful_transaction(reference, transaction_id=123450003)
        )
    assert exc.value.status_code == 409

    wallet, ledger = await wallet_snapshot(user_id)
    assert Decimal(wallet["available_credits"]) == Decimal("400")
    assert len(ledger) == 1
