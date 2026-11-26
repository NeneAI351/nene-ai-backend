"""Verify photo edit settlement against PostgreSQL with a simulated provider."""
import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import database
import photo_design as photo
from paid_fixtures import fund_paid


@pytest.mark.asyncio
async def test_outfit_edit_failure_and_concurrent_replay(monkeypatch):
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL required")
    if database._pool is None:
        await database.start_database()
    await database.initialize_schema()
    await photo.initialize_schema()
    owner, quote = str(uuid.uuid4()), uuid.uuid4()
    await database.execute("INSERT INTO users(id) VALUES (%s)", (owner,))
    await fund_paid(owner, "100")
    prompt = "Change the shirt to a navy blazer. Preserve the face and pose."
    await database.execute(
        "INSERT INTO photo_design_quotes(id,user_id,source_key,prompt,credit_cost) VALUES (%s,%s,%s,%s,10)",
        (str(quote), owner, "source", prompt),
    )
    monkeypatch.setenv("NENE_PHOTO_DESIGN_STAGING_QUOTES", "true")
    monkeypatch.setenv("NENE_PHOTO_EDIT_CREDITS", "10")
    monkeypatch.setattr(photo, "enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(photo, "require_account_agreement", AsyncMock())
    monkeypatch.setattr(photo, "read_source_image", lambda key: b"original")
    monkeypatch.setattr(photo, "upload_source_image", lambda *args: {"object_key": "result"})
    monkeypatch.setattr(photo, "create_signed_source_image_url", lambda key: "https://example.com/" + key)
    provider = AsyncMock(side_effect=[photo.PhotoEditError("simulated failure"), b"edited"])
    monkeypatch.setattr(photo, "edit_photo", provider)
    body = photo.EditRequest(quote_id=quote)

    with pytest.raises(HTTPException) as error:
        await photo.generate(body, None, owner)
    assert error.value.status_code == 502
    wallet_sql = "SELECT available_credits,lifetime_consumed FROM wallets WHERE user_id=%s"
    assert (await database.fetch_one(wallet_sql, (owner,)))["available_credits"] == 100

    first, replay = await asyncio.gather(*(photo.generate(body, None, owner) for _ in range(2)))
    assert first == replay
    assert provider.await_count == 2  # One failure and one successful provider request.
    assert provider.await_args.args[2] == prompt
    wallet = await database.fetch_one(wallet_sql, (owner,))
    assert wallet["available_credits"] == 90 and wallet["lifetime_consumed"] == 10
    charges = await database.fetch_all(
        "SELECT amount FROM credit_ledger WHERE user_id=%s AND entry_type='photo_design_charge'", (owner,),
    )
    assert [row["amount"] for row in charges] == [-10]
    assert (await photo.save(quote, owner))["saved"]
    assert (await photo.saved_edits(owner))["edits"] == [first]
