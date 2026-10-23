"""Paystack test-mode credit checkout and idempotent fulfilment.

Live payments are disabled unless PAYSTACK_MODE=live is explicitly configured.
Never grant credits from a browser redirect: verify with Paystack or validate its
signed webhook, then match reference, user-owned order, amount and currency.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from decimal import Decimal
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

import database
from billing_policy import POLICY_VERSION, REFUND_POLICY
from auth import get_authenticated_user_id

router = APIRouter(prefix="/api/payments", tags=["payments"])

# Fixed server-owned packages; clients cannot choose an amount or credit count.
PACKAGES: dict[str, dict[str, Any]] = {
    "starter": {"amount_kobo": 500000, "credits": Decimal("400"), "label": "Starter"},
    "creator": {"amount_kobo": 1500000, "credits": Decimal("1300"), "label": "Creator"},
    "studio": {"amount_kobo": 5000000, "credits": Decimal("4500"), "label": "Studio"},
}
REFERENCE_RE = re.compile(r"^nene_[0-9a-f]{32}$")


def is_valid_webhook_signature(secret: str, raw_body: bytes, signature: str) -> bool:
    """Constant-time validation of Paystack's SHA-512 raw-body signature."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)


def is_valid_checkout_email(email: str) -> bool:
    """Reject whitespace and malformed address separators before checkout."""
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", (email or "").strip()))


def payment_mode() -> str:
    return os.getenv("PAYSTACK_MODE", "test").strip().lower()


def paystack_secret() -> str:
    return os.getenv("PAYSTACK_SECRET_KEY", "").strip()


def _test_mode_only() -> None:
    mode = payment_mode()
    secret = paystack_secret()
    if mode not in {"test", "live"}:
        raise HTTPException(status_code=503, detail="Payment mode is invalid.")
    if not secret:
        raise HTTPException(status_code=503, detail="Payment checkout is not configured.")
    if mode == "test" and not secret.startswith("sk_test_"):
        raise HTTPException(status_code=503, detail="Test mode requires a Paystack test secret key.")
    if mode == "live" and not secret.startswith("sk_live_"):
        raise HTTPException(status_code=503, detail="Live mode requires a Paystack live secret key.")
    # Additional deliberate live launch gate: no accidental switch by setting a key alone.
    if mode == "live" and os.getenv("PAYSTACK_LIVE_PAYMENTS_APPROVED", "").lower() != "true":
        raise HTTPException(status_code=503, detail="Live payments are locked until business approval and launch checks are complete.")


async def initialize_payment_schema() -> None:
    if database._pool is None:
        return
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS payment_orders (
                    id UUID PRIMARY KEY,
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL DEFAULT 'paystack',
                    mode TEXT NOT NULL DEFAULT 'test' CHECK (mode IN ('test','live')),
                    reference TEXT NOT NULL UNIQUE,
                    package_id TEXT NOT NULL,
                    amount_kobo BIGINT NOT NULL CHECK (amount_kobo > 0),
                    currency TEXT NOT NULL DEFAULT 'NGN' CHECK (currency = 'NGN'),
                    credits NUMERIC(14,2) NOT NULL CHECK (credits > 0),
                    status TEXT NOT NULL DEFAULT 'initialized'
                        CHECK (status IN ('initialized','pending','success','failed','abandoned')),
                    authorization_url TEXT,
                    access_code TEXT,
                    provider_transaction_id TEXT,
                    paid_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'test' CHECK (mode IN ('test','live'));
                ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS policy_version TEXT;
                ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS policy_accepted_at TIMESTAMPTZ;
                CREATE INDEX IF NOT EXISTS idx_payment_orders_user_created
                    ON payment_orders(user_id, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_orders_provider_tx
                    ON payment_orders(provider, provider_transaction_id)
                    WHERE provider_transaction_id IS NOT NULL;
            """)


class CheckoutRequest(BaseModel):
    package_id: str = Field(min_length=1, max_length=40)
    email: str = Field(min_length=5, max_length=254)
    accepted_policy_version: Literal["2026-10-23-paid-only-v1"]


class VerifyRequest(BaseModel):
    reference: str = Field(min_length=1, max_length=80)


async def _require_database() -> None:
    if not database.database_configured() or database._pool is None:
        raise HTTPException(status_code=503, detail="NENE AI payment storage is not configured.")


async def _paystack_get(reference: str) -> dict[str, Any]:
    _test_mode_only()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                "https://api.paystack.co/transaction/verify/" + reference,
                headers={"Authorization": f"Bearer {paystack_secret()}"},
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Payment provider verification is temporarily unavailable.")
    if not isinstance(payload, dict) or payload.get("status") is not True or not isinstance(payload.get("data"), dict):
        raise HTTPException(status_code=502, detail="Payment provider did not return a valid verification result.")
    return payload["data"]


def validate_verified_transaction(transaction: dict[str, Any]) -> tuple[str, str, int] | None:
    """Validate provider transaction fields before touching wallet state.

    Returns None for a non-success event; returns (reference, transaction_id,
    amount_kobo) for a structurally valid successful NGN transaction.
    """
    reference = str(transaction.get("reference", ""))
    if not REFERENCE_RE.fullmatch(reference):
        raise HTTPException(status_code=400, detail="Invalid payment reference.")
    if transaction.get("status") != "success":
        return None
    if str(transaction.get("currency", "")).upper() != "NGN":
        raise HTTPException(status_code=400, detail="Payment currency does not match the order.")
    tx_id = transaction.get("id")
    if tx_id is None or not str(tx_id).isdigit():
        raise HTTPException(status_code=400, detail="Payment provider transaction ID is invalid.")
    amount = transaction.get("amount")
    if isinstance(amount, bool) or not str(amount).isdigit():
        raise HTTPException(status_code=400, detail="Payment amount is invalid.")
    return reference, str(tx_id), int(amount)


async def _fulfill_verified_transaction(transaction: dict[str, Any]) -> dict[str, Any]:
    validated = validate_verified_transaction(transaction)
    if validated is None:
        return {"ok": True, "credited": False, "status": str(transaction.get("status", "unknown"))}
    reference, tx_id, amount_kobo = validated

    await _require_database()
    async with database._pool.connection() as conn:
        async with conn.transaction():
            order = await (await conn.execute(
                """SELECT id::text,user_id::text,package_id,amount_kobo,currency,credits,status,
                          provider_transaction_id,mode
                   FROM payment_orders WHERE reference=%s FOR UPDATE""",
                (reference,),
            )).fetchone()
            if not order:
                raise HTTPException(status_code=404, detail="Payment order was not found.")
            if transaction.get("domain") != order["mode"]:
                raise HTTPException(status_code=400, detail="Payment environment does not match the order.")
            if amount_kobo != int(order["amount_kobo"]):
                raise HTTPException(status_code=400, detail="Paid amount does not match the order.")
            if "NGN" != order["currency"]:
                raise HTTPException(status_code=400, detail="Paid currency does not match the order.")
            if order["provider_transaction_id"] is not None and tx_id != str(order["provider_transaction_id"]):
                raise HTTPException(status_code=409, detail="This order is already linked to a different transaction ID.")

            # Serialize claims on the provider transaction ID across different orders.
            # The unique index remains the final database-level invariant.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("paystack-transaction:" + tx_id,),
            )
            claimed = await (await conn.execute(
                """SELECT reference FROM payment_orders
                   WHERE provider='paystack' AND provider_transaction_id=%s
                     AND reference<>%s LIMIT 1""",
                (tx_id, reference),
            )).fetchone()
            if claimed:
                raise HTTPException(status_code=409, detail="This transaction ID is already linked to another payment.")
            if order["status"] == "success":
                return {"ok": True, "credited": True, "duplicate": True, "reference": reference}

            # The order row lock plus the unique provider transaction ID and
            # wallet ledger operation key prevent duplicate credit delivery.
            user_id = order["user_id"]
            credits = Decimal(order["credits"])
            await conn.execute(
                "INSERT INTO wallets(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            wallet = await (await conn.execute(
                "SELECT available_credits FROM wallets WHERE user_id=%s FOR UPDATE",
                (user_id,),
            )).fetchone()
            existing = await (await conn.execute(
                "SELECT id FROM credit_ledger WHERE user_id=%s AND operation_key=%s",
                (user_id, "payment:" + reference),
            )).fetchone()
            if existing:
                # Repair an interrupted/legacy order state without granting twice.
                await conn.execute(
                    """UPDATE payment_orders SET status='success',provider_transaction_id=%s,
                       paid_at=COALESCE(paid_at,NOW()),updated_at=NOW() WHERE id=%s""",
                    (str(tx_id), order["id"]),
                )
                return {"ok": True, "credited": True, "duplicate": True, "reference": reference}
            new_balance = Decimal(wallet["available_credits"]) + credits
            await conn.execute(
                """UPDATE wallets SET available_credits=%s,
                   lifetime_purchased=lifetime_purchased+%s,updated_at=NOW() WHERE user_id=%s""",
                (new_balance, credits, user_id),
            )
            await conn.execute(
                """INSERT INTO credit_ledger
                   (id,user_id,generation_id,entry_type,amount,balance_after,metadata,operation_key)
                   VALUES (%s,%s,NULL,'purchase',%s,%s,%s::jsonb,%s)""",
                (str(uuid.uuid4()), user_id, credits, new_balance,
                 json.dumps({"provider": "paystack", "reference": reference,
                             "transaction_id": str(tx_id), "package_id": order["package_id"]}),
                 "payment:" + reference),
            )
            await conn.execute(
                """UPDATE payment_orders SET status='success',provider_transaction_id=%s,
                   paid_at=NOW(),updated_at=NOW() WHERE id=%s""",
                (str(tx_id), order["id"]),
            )
            return {"ok": True, "credited": True, "duplicate": False,
                    "reference": reference, "credits_added": str(credits),
                    "available_credits": str(new_balance)}


@router.get("/policy")
async def billing_policy():
    return {"version": POLICY_VERSION, "refund_policy": REFUND_POLICY,
            "free_generation": False, "failed_generation_credit_restoration": True}


@router.get("/packages")
async def list_credit_packages():
    return {"ok": True, "currency": "NGN", "mode": payment_mode(),
            "policy_version": POLICY_VERSION, "refund_policy": REFUND_POLICY,
            "packages": [{"id": key, "name": value["label"],
                          "amount_kobo": value["amount_kobo"],
                          "amount_ngn": value["amount_kobo"] // 100,
                          "credits": str(value["credits"])}
                         for key, value in PACKAGES.items()],
            "live_payments_enabled": payment_mode() == "live"
                and os.getenv("PAYSTACK_LIVE_PAYMENTS_APPROVED", "").lower() == "true"}


@router.post("/checkout")
async def create_checkout(req: CheckoutRequest, user_id: str = Depends(get_authenticated_user_id)):
    await _require_database()
    _test_mode_only()
    if not is_valid_checkout_email(req.email):
        raise HTTPException(status_code=400, detail="Enter a valid email address for the payment receipt.")
    package = PACKAGES.get(req.package_id)
    if not package:
        raise HTTPException(status_code=400, detail="Choose a valid NENE AI credit package.")
    reference = "nene_" + uuid.uuid4().hex
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO payment_orders
                   (id,user_id,provider,mode,reference,package_id,amount_kobo,currency,credits,status,policy_version,policy_accepted_at)
                   VALUES (%s,%s,'paystack',%s,%s,%s,%s,'NGN',%s,'initialized',%s,NOW())""",
                (str(uuid.uuid4()), user_id, payment_mode(), reference, req.package_id,
                 package["amount_kobo"], package["credits"],req.accepted_policy_version),
            )
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.paystack.co/transaction/initialize",
                headers={"Authorization": f"Bearer {paystack_secret()}",
                         "Content-Type": "application/json"},
                json={"email": req.email.strip(), "amount": package["amount_kobo"],
                      "currency": "NGN", "reference": reference,
                      "metadata": {"nene_user_id": user_id, "package_id": req.package_id}},
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        async with database._pool.connection() as conn:
            await conn.execute(
                "UPDATE payment_orders SET status='failed',updated_at=NOW() WHERE reference=%s AND status='initialized'",
                (reference,),
            )
        raise HTTPException(status_code=502, detail="Could not initialize checkout; no credits were added.")
    data = payload.get("data") if isinstance(payload, dict) and payload.get("status") is True else None
    if not isinstance(data, dict) or not data.get("authorization_url"):
        raise HTTPException(status_code=502, detail="Payment provider returned an invalid checkout response.")
    async with database._pool.connection() as conn:
        await conn.execute(
            """UPDATE payment_orders SET status='pending',authorization_url=%s,access_code=%s,updated_at=NOW()
               WHERE reference=%s""",
            (data["authorization_url"], data.get("access_code"), reference),
        )
    return {"ok": True, "reference": reference, "authorization_url": data["authorization_url"],
            "mode": payment_mode(), "message": "Credits are added only after verified payment success."}


@router.post("/verify")
async def verify_checkout(req: VerifyRequest, user_id: str = Depends(get_authenticated_user_id)):
    if not REFERENCE_RE.fullmatch(req.reference):
        raise HTTPException(status_code=400, detail="Invalid payment reference.")
    await _require_database()
    row = await database.fetch_one(
        "SELECT reference FROM payment_orders WHERE reference=%s AND user_id=%s",
        (req.reference, user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Payment order was not found for this account.")
    transaction = await _paystack_get(req.reference)
    # Enforce order ownership through the authenticated lookup above; fulfilment
    # still independently checks the exact reference, amount, currency and state.
    return await _fulfill_verified_transaction(transaction)


@router.post("/webhook")
async def paystack_webhook(request: Request, x_paystack_signature: str = Header(default="")):
    # Apply the same mode/key/launch gate to inbound payment events as checkout
    # and verification. A webhook must never bypass the live-payment release lock.
    _test_mode_only()
    secret = paystack_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="Payment webhook is not configured.")
    raw = await request.body()
    if not is_valid_webhook_signature(secret, raw, x_paystack_signature):
        raise HTTPException(status_code=401, detail="Invalid payment webhook signature.")
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid payment webhook payload.")
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Invalid payment webhook payload.")
    if event.get("event") != "charge.success":
        return {"ok": True, "ignored": True}
    data = event.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid payment event.")
    return await _fulfill_verified_transaction(data)
