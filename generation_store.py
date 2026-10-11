"""Durable, owner-scoped generation lifecycle persistence.

This module intentionally does not submit provider work or settle credits by itself.
Callers must authenticate first, create a durable row before external submission, and
use wallet_store's atomic reservation/capture/release functions. Unknown provider
outcomes must remain recoverable and must never trigger blind duplicate submissions.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import database

# Explicit state machine. "submission_unknown" means a timeout may have occurred
# after the provider accepted the job; recovery must reconcile before retrying.
ALLOWED_TRANSITIONS = {
    "queued": {"submitting", "failed"},
    "submitting": {"submitted", "submission_unknown", "failed"},
    "submission_unknown": {"submitted", "running", "succeeded", "failed"},
    "submitted": {"running", "succeeded", "failed"},
    "running": {"succeeded", "failed"},
    "succeeded": set(),
    "failed": set(),
}


class GenerationConflict(ValueError):
    pass


async def create_generation(
    user_id: str,
    request: dict[str, Any],
    generation_type: str,
    provider: str,
    project_id: str | None = None,
    credits_reserved: str = "0.00",
) -> str:
    """Persist ownership and immutable request snapshot before external work."""
    if not database.database_configured():
        raise RuntimeError("Durable generation storage is required.")
    generation_id = str(uuid.uuid4())
    async with database._pool.connection() as conn:
        async with conn.transaction():
            if project_id:
                project = await (await conn.execute(
                    "SELECT id FROM projects WHERE id=%s AND user_id=%s AND status='active' FOR KEY SHARE",
                    (project_id, user_id),
                )).fetchone()
                if not project:
                    raise ValueError("Project not found for this user.")
            await conn.execute(
                """INSERT INTO generations
                   (id,user_id,project_id,provider,generation_type,status,request,credits_reserved)
                   VALUES (%s,%s,%s,%s,%s,'queued',%s::jsonb,%s)""",
                (generation_id, user_id, project_id, provider, generation_type,
                 json.dumps(request), credits_reserved),
            )
    return generation_id


async def get_owned_generation(user_id: str, generation_id: str) -> dict[str, Any] | None:
    """Return only safe fields for the owner; never expose provider credentials."""
    try:
        parsed = str(uuid.UUID(generation_id))
    except (ValueError, TypeError, AttributeError):
        return None
    return await database.fetch_one(
        """SELECT id::text, project_id::text, provider, provider_job_id,
                  generation_type, status, result, error, credits_reserved,
                  credits_charged, created_at, updated_at, completed_at
           FROM generations WHERE id=%s AND user_id=%s""",
        (parsed, user_id),
    )


async def attach_provider_job(
    user_id: str, generation_id: str, provider: str, provider_job_id: str
) -> None:
    """Attach provider identifiers only to the caller-owned row, atomically."""
    async with database._pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute(
                """SELECT status, provider FROM generations
                   WHERE id=%s AND user_id=%s FOR UPDATE""",
                (generation_id, user_id),
            )).fetchone()
            if not row:
                raise LookupError("Generation not found.")
            if row["provider"] != provider:
                raise GenerationConflict("Provider does not match the persisted generation.")
            if row["status"] not in {"submitting", "submission_unknown", "queued"}:
                raise GenerationConflict("Generation is not awaiting provider submission.")
            await conn.execute(
                """UPDATE generations SET provider_job_id=%s, status='submitted',
                   updated_at=NOW() WHERE id=%s AND user_id=%s""",
                (provider_job_id, generation_id, user_id),
            )


async def transition_generation(
    user_id: str,
    generation_id: str,
    new_status: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    """Enforce monotonic, owner-scoped lifecycle transitions with a row lock."""
    async with database._pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute(
                """SELECT status FROM generations
                   WHERE id=%s AND user_id=%s FOR UPDATE""",
                (generation_id, user_id),
            )).fetchone()
            if not row:
                raise LookupError("Generation not found.")
            old_status = row["status"]
            if new_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
                raise GenerationConflict(f"Invalid generation transition: {old_status} -> {new_status}.")
            terminal = new_status in {"succeeded", "failed"}
            await conn.execute(
                """UPDATE generations SET status=%s, result=COALESCE(%s::jsonb,result),
                   error=COALESCE(%s::jsonb,error), updated_at=NOW(),
                   completed_at=CASE WHEN %s THEN NOW() ELSE completed_at END
                   WHERE id=%s AND user_id=%s""",
                (new_status, json.dumps(result) if result is not None else None,
                 json.dumps(error) if error is not None else None,
                 terminal, generation_id, user_id),
            )


async def mark_submission_unknown(user_id: str, generation_id: str) -> None:
    """Mark ambiguous provider timeouts for reconciliation, never blind resubmission."""
    await transition_generation(user_id, generation_id, "submission_unknown",
                                error={"code": "provider_outcome_unknown",
                                       "message": "Provider outcome requires reconciliation."})
