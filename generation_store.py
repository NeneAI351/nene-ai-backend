"""Durable, owner-scoped generation lifecycle persistence.

This module deliberately does not call providers or settle credits. The API layer
must authenticate the user, persist a generation before provider work, reserve
credits through wallet_store, and reconcile ambiguous provider outcomes instead
of blindly submitting a duplicate job.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import database

# Terminal states are immutable. submission_unknown must be reconciled before any
# retry because a provider may have accepted a request before a network timeout.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"submitting", "failed"},
    "submitting": {"submitted", "submission_unknown", "failed"},
    "submission_unknown": {"submitted", "running", "succeeded", "failed"},
    "submitted": {"running", "succeeded", "failed"},
    "running": {"succeeded", "failed"},
    "succeeded": set(),
    "failed": set(),
}
SAFE_STATUSES = frozenset(ALLOWED_TRANSITIONS)
MAX_REQUEST_BYTES = 64 * 1024
_SENSITIVE_KEYS = frozenset({
    "authorization", "api_key", "apikey", "token", "secret",
    "password", "service_role_key", "access_token", "refresh_token",
})


class GenerationConflict(ValueError):
    """The requested lifecycle operation conflicts with the persisted state."""


def _require_pool():
    pool = database._pool
    if pool is None:
        raise RuntimeError("Durable generation storage is required.")
    return pool


def _safe_snapshot(request: dict[str, Any]) -> str:
    """Bound the persisted request and strip credential-like fields recursively."""
    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): clean(item)
                for key, item in value.items()
                if str(key).lower() not in _SENSITIVE_KEYS
            }
        if isinstance(value, list):
            return [clean(item) for item in value[:100]]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        raise ValueError("Generation request contains an unsupported value.")

    snapshot = json.dumps(clean(request), separators=(",", ":"), ensure_ascii=False)
    if len(snapshot.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("Generation request is too large.")
    return snapshot


async def claim_generation_idempotency(
    user_id: str, idempotency_key: str, request_fingerprint: str
) -> dict[str, Any]:
    """Atomically claim a key within one user namespace.

    A reused key with a different fingerprint is a conflict. Existing pending
    records are returned as pending; callers must not resubmit provider work
    merely because the first HTTP request timed out.
    """
    pool = _require_pool()
    try:
        owner_uuid = str(uuid.UUID(user_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid user identifier.") from exc
    key = (idempotency_key or "").strip()
    fingerprint = (request_fingerprint or "").strip()
    if not key or len(key) > 200 or not fingerprint or len(fingerprint) > 128:
        raise ValueError("Invalid idempotency key or request fingerprint.")

    async with pool.connection() as conn:
        async with conn.transaction():
            inserted = await (await conn.execute(
                """INSERT INTO generation_requests
                   (user_id,idempotency_key,request_fingerprint,state)
                   VALUES (%s,%s,%s,'reserved')
                   ON CONFLICT (user_id,idempotency_key) DO NOTHING
                   RETURNING user_id::text""",
                (owner_uuid, key, fingerprint),
            )).fetchone()
            if inserted:
                return {"claimed": True, "state": "reserved", "generation_id": None}
            existing = await (await conn.execute(
                """SELECT request_fingerprint,state,generation_id::text,response
                   FROM generation_requests
                   WHERE user_id=%s AND idempotency_key=%s FOR UPDATE""",
                (owner_uuid, key),
            )).fetchone()
            if not existing:
                raise RuntimeError("Idempotency record disappeared during lookup.")
            if existing["request_fingerprint"] != fingerprint:
                raise GenerationConflict("Idempotency key reused for a different request.")
            return {
                "claimed": False,
                "state": existing["state"],
                "generation_id": existing["generation_id"],
                "response": existing["response"],
            }


async def bind_generation_idempotency(
    user_id: str, idempotency_key: str, request_fingerprint: str, generation_id: str
) -> None:
    """Bind the claimed key to the durable generation without changing its owner."""
    pool = _require_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute(
                """SELECT request_fingerprint,generation_id,state
                   FROM generation_requests
                   WHERE user_id=%s AND idempotency_key=%s FOR UPDATE""",
                (user_id, idempotency_key),
            )).fetchone()
            if not row or row["request_fingerprint"] != request_fingerprint:
                raise GenerationConflict("Generation idempotency claim does not match.")
            if row["generation_id"] and row["generation_id"] != generation_id:
                raise GenerationConflict("Idempotency key is already bound to another generation.")
            await conn.execute(
                """UPDATE generation_requests
                   SET generation_id=%s, updated_at=NOW()
                   WHERE user_id=%s AND idempotency_key=%s""",
                (generation_id, user_id, idempotency_key),
            )


async def finish_generation_idempotency(
    user_id: str, idempotency_key: str, generation_id: str,
    state: str, response: dict[str, Any] | None = None,
) -> None:
    """Mark a request accepted/failed; never overwrite a different generation."""
    if state not in {"accepted", "failed"}:
        raise ValueError("Invalid terminal idempotency state.")
    pool = _require_pool()
    response_json = json.dumps(response, separators=(",", ":"), ensure_ascii=False) if response is not None else None
    if response_json and len(response_json.encode("utf-8")) > 16 * 1024:
        raise ValueError("Idempotency response is too large.")
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute(
                """SELECT generation_id::text,state FROM generation_requests
                   WHERE user_id=%s AND idempotency_key=%s FOR UPDATE""",
                (user_id, idempotency_key),
            )).fetchone()
            if not row or row["generation_id"] != generation_id:
                raise GenerationConflict("Idempotency key is not bound to this generation.")
            if row["state"] in {"accepted", "failed"} and row["state"] != state:
                raise GenerationConflict("Terminal idempotency state cannot be reversed.")
            await conn.execute(
                """UPDATE generation_requests SET state=%s,
                   response=COALESCE(%s::jsonb,response), updated_at=NOW()
                   WHERE user_id=%s AND idempotency_key=%s""",
                (state, response_json, user_id, idempotency_key),
            )


async def create_generation(
    user_id: str,
    request: dict[str, Any],
    generation_type: str,
    provider: str,
    project_id: str | None = None,
) -> str:
    """Persist ownership and a bounded request snapshot before external work.

    Credits are intentionally initialized to zero here. A wallet reservation is
    a separate atomic operation and its successful result must be recorded by the
    orchestration layer. This avoids claiming a reservation before it exists.
    """
    pool = _require_pool()
    try:
        user_uuid = str(uuid.UUID(user_id))
        project_uuid = str(uuid.UUID(project_id)) if project_id else None
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid user or project identifier.") from exc
    if not generation_type or len(generation_type) > 80:
        raise ValueError("Invalid generation type.")
    if not provider or len(provider) > 80:
        raise ValueError("Invalid provider.")
    snapshot = _safe_snapshot(request)
    generation_id = str(uuid.uuid4())

    async with pool.connection() as conn:
        async with conn.transaction():
            if project_uuid:
                project = await (await conn.execute(
                    """SELECT id FROM projects
                       WHERE id=%s AND user_id=%s AND status='active'
                       FOR KEY SHARE""",
                    (project_uuid, user_uuid),
                )).fetchone()
                if not project:
                    # Do not reveal whether another user's project exists.
                    raise LookupError("Project not found.")
            await conn.execute(
                """INSERT INTO generations
                   (id,user_id,project_id,provider,generation_type,status,request,credits_reserved)
                   VALUES (%s,%s,%s,%s,%s,'queued',%s::jsonb,0)""",
                (generation_id, user_uuid, project_uuid, provider, generation_type, snapshot),
            )
    return generation_id


async def get_owned_generation(user_id: str, generation_id: str) -> dict[str, Any] | None:
    """Owner-scoped read. Provider payloads and raw provider URLs are not exposed."""
    try:
        owner_uuid = str(uuid.UUID(user_id))
        parsed_id = str(uuid.UUID(generation_id))
    except (ValueError, TypeError, AttributeError):
        return None
    row = await database.fetch_one(
        """SELECT id::text, project_id::text, provider, provider_job_id,
                  generation_type, status, error, credits_reserved, credits_charged,
                  created_at, updated_at, completed_at
           FROM generations WHERE id=%s AND user_id=%s""",
        (parsed_id, owner_uuid),
    )
    if row:
        # Error details are internal diagnostic data; expose only a stable code.
        error = row.get("error")
        row["error"] = {"code": error.get("code")} if isinstance(error, dict) and error.get("code") else None
    return row


async def begin_submission(user_id: str, generation_id: str) -> None:
    """Claim a queued generation before the one allowed provider submission."""
    await transition_generation(user_id, generation_id, "submitting")


async def attach_provider_job(
    user_id: str, generation_id: str, provider: str, provider_job_id: str
) -> None:
    """Attach a provider ID to the owner row without permitting reassignment."""
    pool = _require_pool()
    try:
        owner_uuid = str(uuid.UUID(user_id))
        generation_uuid = str(uuid.UUID(generation_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LookupError("Generation not found.") from exc
    if not provider_job_id or len(provider_job_id) > 512:
        raise ValueError("Invalid provider job identifier.")

    async with pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute(
                """SELECT status, provider, provider_job_id FROM generations
                   WHERE id=%s AND user_id=%s FOR UPDATE""",
                (generation_uuid, owner_uuid),
            )).fetchone()
            if not row:
                raise LookupError("Generation not found.")
            if row["provider"] != provider:
                raise GenerationConflict("Provider does not match the persisted generation.")
            if row["status"] not in {"submitting", "submission_unknown"}:
                raise GenerationConflict("Generation is not awaiting provider reconciliation.")
            if row["provider_job_id"] and row["provider_job_id"] != provider_job_id:
                raise GenerationConflict("A different provider job is already attached.")
            await conn.execute(
                """UPDATE generations SET provider_job_id=%s, status='submitted',
                   error=NULL, updated_at=NOW()
                   WHERE id=%s AND user_id=%s""",
                (provider_job_id, generation_uuid, owner_uuid),
            )


async def transition_generation(
    user_id: str,
    generation_id: str,
    new_status: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    """Enforce valid owner-scoped lifecycle transitions under a row lock."""
    if new_status not in SAFE_STATUSES:
        raise GenerationConflict("Unknown generation status.")
    pool = _require_pool()
    try:
        owner_uuid = str(uuid.UUID(user_id))
        generation_uuid = str(uuid.UUID(generation_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LookupError("Generation not found.") from exc

    # Persist only bounded, JSON-serializable result/error structures. Public
    # polling must still use an explicit response allowlist and not return result.
    result_json = json.dumps(result, separators=(",", ":"), ensure_ascii=False) if result is not None else None
    error_json = json.dumps(error, separators=(",", ":"), ensure_ascii=False) if error is not None else None
    if result_json and len(result_json.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("Generation result is too large.")
    if error_json and len(error_json.encode("utf-8")) > 4096:
        error_json = json.dumps({"code": "provider_error", "message": "Provider operation failed."})

    async with pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute(
                """SELECT status FROM generations
                   WHERE id=%s AND user_id=%s FOR UPDATE""",
                (generation_uuid, owner_uuid),
            )).fetchone()
            if not row:
                raise LookupError("Generation not found.")
            old_status = row["status"]
            if new_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
                raise GenerationConflict(
                    f"Invalid generation transition: {old_status} -> {new_status}."
                )
            terminal = new_status in {"succeeded", "failed"}
            await conn.execute(
                """UPDATE generations SET status=%s,
                   result=COALESCE(%s::jsonb,result),
                   error=CASE WHEN %s::jsonb IS NULL THEN error ELSE %s::jsonb END,
                   updated_at=NOW(),
                   completed_at=CASE WHEN %s THEN NOW() ELSE completed_at END
                   WHERE id=%s AND user_id=%s""",
                (new_status, result_json, error_json, error_json, terminal,
                 generation_uuid, owner_uuid),
            )


async def mark_submission_unknown(user_id: str, generation_id: str) -> None:
    """Keep the reservation intact until provider acceptance is reconciled."""
    await transition_generation(
        user_id,
        generation_id,
        "submission_unknown",
        error={
            "code": "provider_outcome_unknown",
            "message": "Provider outcome requires reconciliation.",
        },
    )
