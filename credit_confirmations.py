"""Queue branded confirmations alongside verified live credit purchases."""
import asyncio
import logging
import os
import re

import httpx

import database

log = logging.getLogger("nene-ai.credit-confirmation")
SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_confirmations (
    reference TEXT PRIMARY KEY REFERENCES payment_orders(reference) ON DELETE CASCADE,
    recipient TEXT NOT NULL,
    credits NUMERIC(14,2) NOT NULL,
    next_attempt TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE credit_confirmations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON credit_confirmations FROM PUBLIC;
"""


def configuration():
    key = os.getenv("NENE_RECEIPT_RESEND_API_KEY", "").strip()
    sender = os.getenv("NENE_RECEIPT_FROM_EMAIL", "").strip()
    if key and valid_email(sender):
        return key, sender
    return None


def valid_email(value):
    return isinstance(value, str) and len(value) <= 254 and bool(
        re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value)
    )


async def enqueue(conn, order, transaction, reference):
    if order["mode"] != "live" or not configuration():
        return
    customer = transaction.get("customer")
    recipient = customer.get("email") if isinstance(customer, dict) else None
    if not valid_email(recipient):
        return
    await conn.execute(
        """INSERT INTO credit_confirmations(reference, recipient, credits)
           VALUES (%s, %s, %s) ON CONFLICT(reference) DO NOTHING""",
        (reference, recipient, order["credits"]),
    )


async def deliver_one(config, client):
    key, sender = config
    async with database._pool.connection() as conn:
        async with conn.transaction():
            # Bound retries within the email provider's idempotency window.
            await conn.execute(
                "DELETE FROM credit_confirmations WHERE created_at < NOW() - INTERVAL '23 hours'"
            )
            row = await (await conn.execute(
                """SELECT reference, recipient, credits FROM credit_confirmations
                   WHERE next_attempt <= NOW() ORDER BY next_attempt
                   LIMIT 1 FOR UPDATE SKIP LOCKED"""
            )).fetchone()
            if not row:
                return
            reference = row["reference"]
            credits = format(row["credits"].normalize(), "f")
            try:
                response = await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {key}",
                             "Idempotency-Key": f"nene-credits/{reference}"},
                    json={"from": f"NENE AI <{sender}>", "to": [row["recipient"]],
                          "subject": "Your NENE AI credits have been added",
                          "text": f"{credits} credits are now in your NENE AI wallet.\n\n"
                                  f"Payment reference: {reference}\n"
                                  "View your wallet at https://neneai.app/"},
                )
                sent = response.is_success
            except httpx.RequestError:
                sent = False
            if sent:
                await conn.execute("DELETE FROM credit_confirmations WHERE reference=%s", (reference,))
            else:
                await conn.execute(
                    """UPDATE credit_confirmations SET next_attempt=NOW()+INTERVAL '5 minutes'
                       WHERE reference=%s""", (reference,)
                )


async def run():
    config = configuration()
    if not config:
        return
    async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
        while True:
            try:
                await asyncio.wait_for(deliver_one(config, client), timeout=15)
            except Exception:
                log.warning("Credit confirmation delivery will retry.")
            await asyncio.sleep(5)
