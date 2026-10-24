"""Atomic server-authoritative generation quote, reservation, and queue creation.

No provider call is made in the HTTP request. Generation row, credit reservation,
ledger entries, per-user idempotency record, and outbox event commit together.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any

import database
import generation_outbox
from paid_access import paid_available
from account_agreement_store import require_account_agreement
from pricing import quote as pricing_quote


class GenerationRequestConflict(ValueError):
    pass


class GenerationPriceChanged(ValueError):
    pass


class InsufficientCredits(ValueError):
    pass


def _uuid(value: str, label: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"Invalid {label}.") from exc


def _canonical_request(request: dict[str, Any]) -> tuple[str, str]:
    # Fingerprint only the normalized server-approved request; never client price.
    serialized = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(serialized.encode("utf-8")) > 64 * 1024:
        raise ValueError("Generation request is too large.")
    return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def create_reserved_generation(
    user_id: str,
    idempotency_key: str,
    *,
    prompt: str,
    provider: str,
    model: str,
    resolution: str,
    duration_seconds: int,
    generation_type: str = "text-to-video",
    project_id: str | None = None,
    aspect_ratio: str | None = None,
    image_url: str | None = None,
    source_image_id: str | None = None,
    source_audio_id: str | None = None,
    credit_limit: Decimal | None = None,
    generate_audio: bool = True,
    tts_voice: str | None = None,
) -> dict[str, Any]:
    """Create one billable generation in one PostgreSQL transaction.

    Client-supplied credit amounts/margins are not accepted. A retry with the
    same user/key and same canonical request returns the original generation.
    A key reused for different inputs raises GenerationRequestConflict.
    """
    if database._pool is None:
        raise RuntimeError("Durable generation storage is required.")
    if not isinstance(generate_audio, bool):
        raise ValueError("Sound selection must be a boolean.")
    if credit_limit is not None:
        credit_limit = Decimal(str(credit_limit))
        if not credit_limit.is_finite() or credit_limit <= 0 or credit_limit.quantize(Decimal("0.01")) != credit_limit:
            raise ValueError("Credit limit must be a positive amount with at most two decimals.")
    owner = _uuid(user_id, "user identifier")
    project = _uuid(project_id, "project identifier") if project_id else None
    key = (idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise ValueError("A valid Idempotency-Key header is required.")
    prompt = (prompt or "").strip()
    if not prompt or len(prompt) > 12000:
        raise ValueError("Prompt must contain between 1 and 12000 characters.")
    is_tts = generation_type == "text-to-speech"
    if is_tts:
        if not generate_audio or provider!="magic-hour" or model!="magic-hour-voice" or resolution!="audio" or duration_seconds!=1 or source_audio_id or source_image_id or image_url or aspect_ratio:
            raise ValueError("Invalid speech generation settings.")
        from speech_synthesis import quote as speech_quote
        quote=speech_quote(prompt,tts_voice)
        source_asset=None
        audio_asset=None
    else:
        if provider != "ltx":
            raise ValueError("The durable worker currently supports the LTX provider only. Other providers remain disabled in this flow.")
        if (generation_type != "audio-to-video" and duration_seconds not in {6, 8, 10}) or (generation_type == "audio-to-video" and not 2 <= duration_seconds <= 10):
            raise ValueError("Duration must be one of the supported billable durations: 6, 8, or 10 seconds.")
        if generation_type not in {"text-to-video", "image-to-video", "audio-to-video"}:
            raise ValueError("Unsupported generation type.")
        if generation_type == "image-to-video" and not (source_image_id or image_url):
            raise ValueError("Image-to-video requires a private source image asset.")
        audio_asset = _uuid(source_audio_id, "source audio identifier") if source_audio_id else None
        if generation_type == "audio-to-video":
            if not audio_asset or not source_image_id or image_url or not generate_audio:
                raise ValueError("Talking characters require private audio and portrait asset IDs with sound enabled.")
            if model != "ltx-2-5-fast" or resolution not in {"720p","1080p"} or aspect_ratio not in {None,"16:9","9:16","1:1"}:
                raise ValueError("Unsupported talking-character output settings.")
        elif audio_asset:
            raise ValueError("Uploaded speech requires audio-to-video mode.")
        source_asset = _uuid(source_image_id, "source image identifier") if source_image_id else None
        if aspect_ratio and aspect_ratio not in {"16:9", "9:16", "1:1", "21:9", "4:3", "3:4", "3:2", "2:3", "4:5"}:
            raise ValueError("Unsupported aspect ratio.")

    # These are the exact parameters to be used by the worker. Never quote one
    # resolution/duration and silently submit a different one.
    if not is_tts:
        quote = pricing_quote(provider, model, resolution, duration_seconds)
    normalized = {
        "type": generation_type,
        "prompt": prompt,
        "provider": provider,
        "model": quote["model"],
        "resolution": quote["resolution"],
        "duration": duration_seconds,
        "aspect_ratio": aspect_ratio,
        "source_image_id": source_asset,
    }
    if is_tts:
        normalized["tts_voice"] = tts_voice
    if audio_asset:
        normalized["source_audio_id"] = audio_asset
    if image_url and not source_asset:
        # Non-production compatibility only. The production HTTP endpoint rejects
        # public source URLs and requires source_image_id.
        normalized["image_url"] = image_url
    # Existing jobs omitted this field and the provider default was audio on.
    # Preserve their fingerprint for safe replay; silence is a distinct request.
    if not generate_audio:
        normalized["generate_audio"] = False
    serialized, fingerprint = _canonical_request(normalized)
    generation_id = str(uuid.uuid4())
    outbox_id = str(uuid.uuid4())
    operation_key = f"reserve:{generation_id}"
    amount = Decimal(quote["recommended_credits"]).quantize(Decimal("0.01"))

    async with database._pool.connection() as conn:
        async with conn.transaction():
            await require_account_agreement(conn,owner)
            # The idempotency key is scoped to the authenticated owner.
            inserted = await (await conn.execute(
                """INSERT INTO generation_requests
                   (user_id,idempotency_key,request_fingerprint,state)
                   VALUES (%s,%s,%s,'reserved')
                   ON CONFLICT (user_id,idempotency_key) DO NOTHING
                   RETURNING user_id::text""",
                (owner, key, fingerprint),
            )).fetchone()
            if not inserted:
                prior = await (await conn.execute(
                    """SELECT request_fingerprint,generation_id::text,state,response
                       FROM generation_requests
                       WHERE user_id=%s AND idempotency_key=%s FOR UPDATE""",
                    (owner, key),
                )).fetchone()
                if not prior or prior["request_fingerprint"] != fingerprint:
                    raise GenerationRequestConflict("Idempotency key was already used for a different request.")
                if prior["generation_id"]:
                    existing_generation = await (await conn.execute(
                        "SELECT status,credits_reserved FROM generations WHERE id=%s AND user_id=%s",
                        (prior["generation_id"], owner),
                    )).fetchone()
                    if not existing_generation:
                        raise GenerationRequestConflict("The idempotent generation record is missing and requires reconciliation.")
                    original = prior.get("response") or {}
                    return {
                        "ok": True,
                        "duplicate": True,
                        "generation_id": prior["generation_id"],
                        "status": existing_generation["status"],
                        "credits_reserved": str(existing_generation["credits_reserved"]),
                        "quote": original.get("quote", quote),
                    }
                # A correctly atomic creation never commits a key without its
                # generation. Treat legacy/interrupted records as a conflict.
                raise GenerationRequestConflict("An earlier request with this key needs reconciliation.")

            # A replay above returns the already accepted original job. A new
            # request may never reserve more than the reviewed spending limit.
            if credit_limit is not None and amount > credit_limit:
                raise GenerationPriceChanged("The price increased. Review the current price before generating; no credits were reserved.")

            if audio_asset:
                owned_audio = await (await conn.execute("""SELECT billable_seconds FROM speech_assets
                    WHERE id=%s AND user_id=%s FOR KEY SHARE""",(audio_asset,owner))).fetchone()
                if not owned_audio:
                    raise LookupError("Speech recording not found.")
                if owned_audio["billable_seconds"] != duration_seconds:
                    raise ValueError("Review the price for the full recording duration before generating.")
            if source_asset:
                owned_asset = await (await conn.execute(
                    """SELECT id FROM media_assets
                       WHERE id=%s AND user_id=%s AND asset_type='source_image'
                         AND deleted_at IS NULL
                       FOR KEY SHARE""",
                    (source_asset, owner),
                )).fetchone()
                if not owned_asset:
                    raise LookupError("Source image not found.")
            if project:
                owned_project = await (await conn.execute(
                    """SELECT id FROM projects
                       WHERE id=%s AND user_id=%s AND status='active'
                       FOR KEY SHARE""",
                    (project, owner),
                )).fetchone()
                if not owned_project:
                    raise LookupError("Project not found.")

            await conn.execute(
                """INSERT INTO generations
                   (id,user_id,project_id,provider,generation_type,status,request,
                    credits_reserved,credits_charged)
                   VALUES (%s,%s,%s,%s,%s,'queued',%s::jsonb,%s,0)""",
                (generation_id, owner, project, provider, generation_type, serialized, amount),
            )

            # Lock the wallet row and reserve credits in this same transaction.
            await conn.execute(
                "INSERT INTO wallets(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
                (owner,),
            )
            wallet = await (await conn.execute(
                "SELECT available_credits,reserved_credits,lifetime_consumed FROM wallets WHERE user_id=%s FOR UPDATE",
                (owner,),
            )).fetchone()
            available = Decimal(wallet["available_credits"])
            reserved = Decimal(wallet["reserved_credits"])
            if amount <= 0 or await paid_available(conn, owner, wallet) < amount:
                raise InsufficientCredits("Insufficient paid credits. Purchase credits before generating.")
            available -= amount
            reserved += amount
            await conn.execute(
                """UPDATE wallets SET available_credits=%s,reserved_credits=%s,updated_at=NOW()
                   WHERE user_id=%s""",
                (available, reserved, owner),
            )
            await conn.execute(
                """INSERT INTO wallet_reservations
                   (user_id,generation_id,reserved_remaining,total_reserved)
                   VALUES (%s,%s,%s,%s)""",
                (owner, generation_id, amount, amount),
            )
            result = {
                "available_credits": str(available),
                "reserved_credits": str(reserved),
                "operation_type": "reserve",
                "amount": str(amount),
                "generation_id": generation_id,
            }
            await conn.execute(
                """INSERT INTO wallet_operations
                   (id,user_id,operation_key,operation_type,amount,generation_id,result)
                   VALUES (%s,%s,%s,'reserve',%s,%s,%s::jsonb)""",
                (str(uuid.uuid4()), owner, operation_key, amount, generation_id, json.dumps(result)),
            )
            await conn.execute(
                """INSERT INTO credit_ledger
                   (id,user_id,generation_id,entry_type,amount,balance_after,metadata,operation_key)
                   VALUES (%s,%s,%s,'reserve',%s,%s,%s::jsonb,%s)""",
                (str(uuid.uuid4()), owner, generation_id, -amount, available,
                 json.dumps({"operation_key": operation_key, "operation_type": "reserve",
                             "amount": str(amount), "available_delta": str(-amount),
                             "reserved_delta": str(amount)}), operation_key),
            )
            await conn.execute(
                """UPDATE generation_requests
                   SET generation_id=%s,state='accepted',response=%s::jsonb,updated_at=NOW()
                   WHERE user_id=%s AND idempotency_key=%s""",
                (generation_id, json.dumps({"generation_id": generation_id, "status": "queued", "quote": quote}),
                 owner, key),
            )
            await conn.execute(
                """INSERT INTO generation_outbox
                   (id,generation_id,event_type,payload)
                   VALUES (%s,%s,'dispatch_generation','{}'::jsonb)
                   ON CONFLICT(generation_id,event_type) DO NOTHING""",
                (outbox_id, generation_id),
            )

    return {
        "ok": True,
        "duplicate": False,
        "generation_id": generation_id,
        "status": "queued",
        "credits_reserved": str(amount),
        "quote": quote,
    }

