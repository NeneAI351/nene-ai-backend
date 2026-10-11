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
from pricing import quote as pricing_quote


class GenerationRequestConflict(ValueError):
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
) -> dict[str, Any]:
    """Create one billable generation in one PostgreSQL transaction.

    Client-supplied credit amounts/margins are not accepted. A retry with the
    same user/key and same canonical request returns the original generation.
    A key reused for different inputs raises GenerationRequestConflict.
    """
    if database._pool is None:
        raise RuntimeError("Durable generation storage is required.")
    owner = _uuid(user_id, "user identifier")
    project = _uuid(project_id, "project identifier") if project_id else None
    key = (idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise ValueError("A valid Idempotency-Key header is required.")
    prompt = (prompt or "").strip()
    if not prompt or len(prompt) > 12000:
        raise ValueError("Prompt must contain between 1 and 12000 characters.")
    if provider not in {"ltx", "magic-hour"}:
        raise ValueError("This generation flow currently supports explicitly priced LTX or Magic Hour routes only.")
    if duration_seconds not in {6, 8, 10}:
        raise ValueError("Duration must be one of the supported billable durations: 6, 8, or 10 seconds.")
    if generation_type not in {"text-to-video", "image-to-video"}:
        raise ValueError("Unsupported generation type.")
    if generation_type == "image-to-video" and not image_url:
        raise ValueError("Image-to-video requires a source image URL.")
    if aspect_ratio and aspect_ratio not in {"16:9", "9:16", "1:1", "21:9", "4:3", "3:4", "3:2", "2:3", "4:5"}:
        raise ValueError("Unsupported aspect ratio.")

    # These are the exact parameters to be used by the worker. Never quote one
    # resolution/duration and silently submit a different one.
    quote = pricing_quote(provider, model, resolution, duration_seconds)
    normalized = {
        "type": generation_type,
        "prompt": prompt,
        "provider": provider,
        "model": quote["model"],
        "resolution": quote["resolution"],
        "duration": duration_seconds,
        "aspect_ratio": aspect_ratio,
        "image_url": image_url,
    }
    serialized, fingerprint = _canonical_request(normalized)
    generation_id = str(uuid.uuid4())
    outbox_id = str(uuid.uuid4())
    operation_key = f"reserve:{generation_id}"
    amount = Decimal(quote["recommended_credits"]).quantize(Decimal("0.01"))

    async with database._pool.connection() as conn:
        async with conn.transaction():
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
                "SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s FOR UPDATE",
                (owner,),
            )).fetchone()
            available = Decimal(wallet["available_credits"])
            reserved = Decimal(wallet["reserved_credits"])
            if available < amount:
                raise InsufficientCredits("Insufficient available credits for this generation.")
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
