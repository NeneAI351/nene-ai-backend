"""Prepare server-priced, owner-scoped photo edits without charging credits."""

import os
import uuid
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import BaseModel

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

import database
from auth import get_authenticated_user_id
from media_storage import MAX_SOURCE_IMAGE_BYTES, delete_source_image, upload_source_image, validate_image_bytes
from media_storage import create_signed_source_image_url, read_source_image
from paid_access import paid_available
from account_agreement_store import require_account_agreement
from photo_design_provider import PhotoEditError, edit_photo
from rate_limits import enforce_rate_limit

router = APIRouter(prefix="/api/photo-design", tags=["photo-design"])


async def initialize_schema():
    if database._pool is None:
        return
    async with database._pool.connection() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS photo_design_quotes (
                id UUID PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                source_key TEXT NOT NULL,
                prompt TEXT NOT NULL CHECK (length(prompt) BETWEEN 1 AND 2000),
                credit_cost NUMERIC(14,2) NOT NULL CHECK (credit_cost > 0),
                expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '15 minutes'
            );
            ALTER TABLE photo_design_quotes ADD COLUMN IF NOT EXISTS result_key TEXT;
            ALTER TABLE photo_design_quotes ADD COLUMN IF NOT EXISTS saved BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE photo_design_quotes ENABLE ROW LEVEL SECURITY;
            REVOKE ALL ON photo_design_quotes FROM PUBLIC;
        """)
        await conn.commit()


def staging_price():
    """Require an explicit staging price while settlement is still in development."""
    if database._pool is None or os.getenv("NENE_PHOTO_DESIGN_STAGING_QUOTES") != "true":
        return None
    try:
        price = Decimal(os.getenv("NENE_PHOTO_EDIT_CREDITS", "0"))
        if price.is_finite() and 0 < price <= 10000 and price == price.quantize(Decimal("0.01")):
            return price
    except InvalidOperation:
        pass
    return None


@router.get("/availability")
async def availability(_owner: str = Depends(get_authenticated_user_id)):
    return {"available": staging_price() is not None and bool(
        os.getenv("OPENAI_API_KEY") and os.getenv("NENE_PHOTO_EDIT_MODEL")
    )}


@router.post("/quote")
async def quote(
    request: Request,
    image: UploadFile = File(...),
    prompt: str = Form(...),
    owner: str = Depends(get_authenticated_user_id),
):
    price = staging_price()
    if price is None:
        raise HTTPException(503, "Photo Design pricing is unavailable.")
    await enforce_rate_limit(request, bucket="photo-design-quote", limit=5, window_seconds=60)
    prompt = prompt.strip()
    if not 1 <= len(prompt) <= 2000:
        raise HTTPException(422, "Describe your edit in 1 to 2,000 characters.")
    raw = await image.read(MAX_SOURCE_IMAGE_BYTES + 1)
    try:
        validate_image_bytes(raw)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None

    quote_id = str(uuid.uuid4())
    # Use the lifecycle lock shared with account deletion, including the upload.
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
                ("account-lifecycle:" + owner,),
            )
            account = await (await conn.execute("SELECT status FROM users WHERE id=%s", (owner,))).fetchone()
            if not account or account["status"] != "active":
                raise HTTPException(403, "This account is no longer active.")
            asset = await run_in_threadpool(upload_source_image, raw, owner, quote_id)
            try:
                await conn.execute(
                    """INSERT INTO photo_design_quotes (id,user_id,source_key,prompt,credit_cost)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (quote_id, owner, asset["object_key"], prompt, price),
                )
            except Exception:
                await run_in_threadpool(delete_source_image, asset["object_key"])
                raise
    return {"quote_id": quote_id, "credit_cost": str(price)}


async def charge_edit(conn, owner, quote_id, price, balance):
    operation = "photo-design:" + quote_id
    await conn.execute(
        """UPDATE wallets SET available_credits=%s, lifetime_consumed=lifetime_consumed+%s,
           updated_at=NOW() WHERE user_id=%s""",
        (balance - price, price, owner),
    )
    await conn.execute(
        """INSERT INTO credit_ledger (id,user_id,entry_type,amount,balance_after,metadata,operation_key)
           VALUES (%s,%s,'photo_design_charge',%s,%s,'{}'::jsonb,%s)""",
        (str(uuid.uuid4()), owner, -price, balance - price, operation),
    )


def public_edit(row):
    return {"id": str(row["id"]), "image_url": create_signed_source_image_url(row["result_key"])}


class EditRequest(BaseModel):
    quote_id: uuid.UUID


@router.post("/edits")
async def generate(body: EditRequest, request: Request, owner: str = Depends(get_authenticated_user_id)):
    if staging_price() is None:
        raise HTTPException(503, "Photo Design is unavailable.")
    await enforce_rate_limit(request, bucket="photo-design-edit", limit=5, window_seconds=60)
    quote_id = str(body.quote_id)
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
                ("account-lifecycle:" + owner,),
            )
            account = await (await conn.execute("SELECT status FROM users WHERE id=%s", (owner,))).fetchone()
            if not account or account["status"] != "active":
                raise HTTPException(403, "This account is no longer active.")
            row = await (await conn.execute(
                """SELECT *, expires_at > NOW() AS valid FROM photo_design_quotes
                   WHERE id=%s AND user_id=%s FOR UPDATE""", (quote_id, owner),
            )).fetchone()
            if not row:
                raise HTTPException(404, "Photo edit not found.")
            if row["result_key"]:
                return public_edit(row)
            if not row["valid"]:
                raise HTTPException(410, "The quote expired. Review the credit cost again.")
            price = row["credit_cost"]
            if price != staging_price():
                raise HTTPException(409, "The price changed. Review the credit cost again.")
            await require_account_agreement(conn, owner)
            wallet = await (await conn.execute(
                """SELECT available_credits,reserved_credits,lifetime_consumed
                   FROM wallets WHERE user_id=%s FOR UPDATE""", (owner,),
            )).fetchone()
            if not wallet or await paid_available(conn, owner, wallet) < price:
                raise HTTPException(402, "You need more paid credits for this edit.")
            try:
                source = await run_in_threadpool(read_source_image, row["source_key"])
                async with httpx.AsyncClient() as client:
                    result = await edit_photo(client, source, row["prompt"])
                asset = await run_in_threadpool(upload_source_image, result, owner, str(uuid.uuid4()))
                await charge_edit(conn, owner, quote_id, price, wallet["available_credits"])
                row["result_key"] = asset["object_key"]
                await conn.execute("UPDATE photo_design_quotes SET result_key=%s WHERE id=%s", (row["result_key"], quote_id))
                return public_edit(row)
            except PhotoEditError:
                raise HTTPException(502, "The photo edit failed. No credits were charged.") from None


@router.post("/edits/{edit_id}/save")
async def save(edit_id: uuid.UUID, owner: str = Depends(get_authenticated_user_id)):
    row = await database.fetch_one(
        """UPDATE photo_design_quotes SET saved=TRUE
           WHERE id=%s AND user_id=%s AND result_key IS NOT NULL RETURNING id""", (str(edit_id), owner),
    )
    if not row:
        raise HTTPException(404, "Photo edit not found.")
    return {"saved": True}


@router.get("/edits")
async def saved_edits(owner: str = Depends(get_authenticated_user_id)):
    rows = await database.fetch_all(
        """SELECT id,result_key FROM photo_design_quotes WHERE user_id=%s AND saved=TRUE
           ORDER BY expires_at DESC LIMIT 50""", (owner,),
    )
    return {"edits": [public_edit(row) for row in rows]}


async def cleanup_expired_quotes():
    """Remove abandoned uploads, skipping quotes that are being processed."""
    async with database._pool.connection() as conn:
        async with conn.transaction():
            rows = await (await conn.execute("""
                SELECT id, source_key FROM photo_design_quotes
                WHERE result_key IS NULL AND expires_at < NOW()
                ORDER BY expires_at LIMIT 25 FOR UPDATE SKIP LOCKED
            """)).fetchall()
            for row in rows:
                await run_in_threadpool(delete_source_image, row["source_key"])
                await conn.execute("DELETE FROM photo_design_quotes WHERE id=%s", (row["id"],))
