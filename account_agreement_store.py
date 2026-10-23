"""Immutable, account-owned agreement acceptance recorded once at account setup."""
import hashlib
import json
from fastapi import HTTPException
import database
from billing_policy import POLICY_VERSION, REFUND_POLICY

TERMS_VERSION = "2026-10-23-account-v1"
TERMS = [
    {"title":"Your account", "text":"Create an account and verify your email before using protected features. Keep your sign-in details secure and provide accurate account information."},
    {"title":"Paid credits", "text":"Video generation and AI planning require purchased credits. There are no welcome or free generation credits. The applicable credit price must be shown before you request paid work. Credits are reserved before processing and charged when the purchased output is delivered. Credits are for NENE AI services and are not a cash account."},
    {"title":"Refund policy", "text":REFUND_POLICY},
    {"title":"Responsible use", "text":"Only upload content you own or have permission to use. Do not use NENE AI for unlawful, abusive or infringing content, or impersonation without required consent. Requests may be rejected by safety controls."},
    {"title":"AI outputs", "text":"AI outputs can contain mistakes and vary between requests. Review outputs before publishing or relying on them. You are responsible for ensuring your use respects applicable law and the rights of others. This does not limit mandatory consumer rights."},
    {"title":"Availability", "text":"Features may be unavailable during testing, maintenance or technical failures. NENE AI does not promise uninterrupted service or a particular creative result. Paid access remains closed until payment and generation verification are complete."}
]
TERMS_SHA256 = hashlib.sha256(json.dumps(TERMS,sort_keys=True,separators=(",", ":")).encode()).hexdigest()

async def initialize_account_agreement_schema():
    if database._pool is None:
        return
    await database.execute("""CREATE TABLE IF NOT EXISTS account_agreements (
        user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        terms_version TEXT NOT NULL,
        refund_policy_version TEXT NOT NULL,
        terms_sha256 TEXT NOT NULL,
        terms_snapshot JSONB NOT NULL,
        accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    ALTER TABLE account_agreements ENABLE ROW LEVEL SECURITY;
    REVOKE ALL ON account_agreements FROM PUBLIC;""")

async def record_agreement(user_id):
    # The authenticated user's explicit assertion, never a client-supplied owner
    # or timestamp. Conflict handling preserves the first acceptance forever.
    await database.execute("""INSERT INTO account_agreements
        (user_id,terms_version,refund_policy_version,terms_sha256,terms_snapshot)
        VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT(user_id) DO NOTHING""",
        (user_id,TERMS_VERSION,POLICY_VERSION,TERMS_SHA256,json.dumps(TERMS)))
    return await database.fetch_one("""SELECT terms_version,refund_policy_version,terms_sha256,accepted_at
        FROM account_agreements WHERE user_id=%s""",(user_id,))

async def require_account_agreement(conn, user_id):
    # Existing accepted versions remain valid: no repeat checkbox on login or
    # purchase, and the order records the version actually accepted at setup.
    row=await (await conn.execute("""SELECT terms_version,refund_policy_version,accepted_at
        FROM account_agreements WHERE user_id=%s""",(user_id,))).fetchone()
    if row is None:
        raise HTTPException(403,"Complete account setup and accept the Terms of Service and Refund Policy once before using paid features.")
    return row
