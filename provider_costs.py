"""Idempotent per-generation provider cost records.

The first supported record is a clearly labelled estimate based on the server-side
rate card. It is not an invoice cost. A future trusted billing import can replace
an estimate with an actual cost; an estimate can never overwrite an actual record.
"""
from __future__ import annotations

import json
import math
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import database


def _finite_nonnegative(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite non-negative number.") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{label} must be a finite non-negative number.")
    return number


def estimate_generation_cost(
    *, generation_id: str, provider: str, model: str, duration_seconds: Any,
    resolution: str, rate_quote: dict[str, Any],
) -> dict[str, Any]:
    """Build a validated estimate from the authoritative server pricing quote."""
    try:
        generation_id = str(uuid.UUID(str(generation_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid generation identifier.") from exc
    provider = str(provider or "").strip().lower()
    model = str(model or "").strip()
    resolution = str(resolution or "").strip().lower()
    if not provider or not model or not resolution:
        raise ValueError("Provider, model, and resolution are required.")
    duration = _finite_nonnegative(duration_seconds, "duration_seconds")
    if duration <= 0:
        raise ValueError("duration_seconds must be greater than zero.")
    if not isinstance(rate_quote, dict):
        raise ValueError("A server pricing quote is required.")
    quote_provider = str(rate_quote.get("provider") or "").strip().lower()
    quote_model = str(rate_quote.get("model") or "").strip()
    quote_resolution = str(rate_quote.get("resolution") or "").strip().lower()
    quote_duration = _finite_nonnegative(rate_quote.get("duration_seconds"), "quoted duration")
    if (quote_provider, quote_model, quote_resolution) != (provider, model, resolution):
        raise ValueError("Pricing quote does not match the generation provider/model/resolution.")
    if quote_duration != duration:
        raise ValueError("Pricing quote duration does not match the generation.")
    cost_usd = _finite_nonnegative(rate_quote.get("provider_cost_usd"), "provider_cost_usd")
    buffered_usd = _finite_nonnegative(
        rate_quote.get("provider_cost_with_risk_buffer_usd", cost_usd),
        "provider_cost_with_risk_buffer_usd",
    )
    if buffered_usd < cost_usd:
        raise ValueError("Buffered estimate cannot be below the unbuffered provider cost.")
    return {
        "generation_id": generation_id,
        "provider": provider,
        "model": model,
        "units": duration,
        "unit_type": "seconds",
        "cost_usd": cost_usd,
        "metadata": {
            "record_type": "estimate",
            "cost_basis": str(rate_quote.get("cost_basis") or "server_rate_card"),
            "estimated": True,
            "resolution": resolution,
            "unbuffered_cost_usd": str(cost_usd),
            "buffered_cost_usd": str(buffered_usd),
            "risk_buffer_included_in_margin_quote": True,
            "source": "server_pricing_quote",
        },
    }


async def record_generation_cost_estimate(record: dict[str, Any]) -> dict[str, Any]:
    """Insert/update one estimate safely under retries; never downgrade actual cost.

    PostgreSQL transaction advisory locking serializes writers for the same
    generation/provider/model. Existing actual records are immutable here.
    """
    if database._pool is None:
        raise RuntimeError("Database is required to record provider costs.")
    generation_id = str(uuid.UUID(str(record["generation_id"])))
    provider = str(record["provider"])
    model = str(record["model"])
    cost = _finite_nonnegative(record["cost_usd"], "cost_usd")
    units = _finite_nonnegative(record["units"], "units")
    metadata = record.get("metadata") or {}
    if not isinstance(metadata, dict) or metadata.get("record_type") != "estimate":
        raise ValueError("Only explicit estimate records may use this function.")
    metadata_json = json.dumps(metadata, separators=(",", ":"), sort_keys=True)

    async with database._pool.connection() as conn:
        async with conn.transaction():
            # Advisory lock key is deterministic and scoped to this cost identity.
            lock_key = f"provider-cost:{generation_id}:{provider}:{model}"
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
            existing = await (await conn.execute(
                """SELECT id::text, metadata
                   FROM provider_costs
                   WHERE generation_id=%s AND provider=%s AND model=%s
                   ORDER BY created_at DESC LIMIT 1
                   FOR UPDATE""",
                (generation_id, provider, model),
            )).fetchone()
            if existing:
                old_metadata = existing["metadata"] if isinstance(existing, dict) else existing[1]
                if isinstance(old_metadata, str):
                    try:
                        old_metadata = json.loads(old_metadata)
                    except ValueError:
                        old_metadata = {}
                old_metadata = old_metadata or {}
                if old_metadata.get("record_type") == "actual" or old_metadata.get("estimated") is False:
                    return {"recorded": False, "reason": "actual_cost_already_recorded", "id": existing["id"] if isinstance(existing, dict) else existing[0]}
                cost_id = existing["id"] if isinstance(existing, dict) else existing[0]
                await conn.execute(
                    """UPDATE provider_costs
                       SET units=%s, unit_type=%s, cost_usd=%s, metadata=%s::jsonb
                       WHERE id=%s""",
                    (units, record.get("unit_type"), cost, metadata_json, cost_id),
                )
                return {"recorded": True, "action": "updated_estimate", "id": str(cost_id)}
            cost_id = str(uuid.uuid4())
            await conn.execute(
                """INSERT INTO provider_costs
                   (id, generation_id, provider, model, units, unit_type, cost_usd, metadata)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (cost_id, generation_id, provider, model, units, record.get("unit_type"), cost, metadata_json),
            )
            return {"recorded": True, "action": "inserted_estimate", "id": cost_id}
