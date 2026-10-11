"""Durable LTX generation worker.

Run as a separate process: python generation_worker.py. It uses the PostgreSQL
outbox and lease fencing; the API process does not run provider work inline.
Provider timeouts are ambiguous and never trigger a blind resubmission.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from decimal import Decimal
from typing import Any

import httpx

import database
import generation_outbox
import generation_store
from wallet_store import wallet_capture, wallet_release

logger = logging.getLogger("nene-ai.worker")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


def _owner_id(value: Any) -> str:
    if not value:
        raise ValueError("Generation has no active owner; manual reconciliation is required.")
    return str(uuid.UUID(str(value)))


def _provider_config() -> tuple[str, dict[str, str]]:
    key = os.getenv("LTX_API_KEY", "").strip()
    base = os.getenv("LTX_API_BASE_URL", "https://api.ltx.io").rstrip("/")
    if not key:
        raise RuntimeError("LTX_API_KEY is not configured.")
    return base, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def _load_generation(generation_id: str) -> dict[str, Any] | None:
    return await database.fetch_one(
        """SELECT id::text,user_id::text,provider,provider_job_id,generation_type,
                  status,request,credits_reserved,credits_charged,result
           FROM generations WHERE id=%s""",
        (generation_id,),
    )


async def _dispatch(generation: dict[str, Any]) -> None:
    generation_id = generation["id"]
    owner = _owner_id(generation.get("user_id"))
    status = generation["status"]
    if status != "queued":
        # A redelivered dispatch event must not submit a second external job.
        if status in {"submitted", "running", "succeeded", "failed", "submission_unknown"}:
            return
        raise RuntimeError("generation_state_not_dispatchable")
    if generation["provider"] != "ltx":
        raise RuntimeError("provider_adapter_not_enabled")
    base, headers = _provider_config()
    request = generation["request"]
    if isinstance(request, str):
        request = json.loads(request)
    kind = generation["generation_type"]
    if kind not in {"text-to-video", "image-to-video"}:
        raise RuntimeError("unsupported_generation_type")

    # Commit 'submitting' before the network call. After a crash/timeout we
    # never blindly resubmit: the provider may have accepted the original call.
    await generation_store.begin_submission(owner, generation_id)
    payload = {
        "prompt": str(request["prompt"]),
        "model": str(request["model"]),
        "resolution": str(request["resolution"]),
        "duration": int(request["duration"]),
    }
    if request.get("aspect_ratio"):
        payload["aspect_ratio"] = request["aspect_ratio"]
    if kind == "image-to-video":
        payload["image_uri"] = request.get("image_url")
        if not payload["image_uri"]:
            await generation_store.transition_generation(
                owner, generation_id, "failed", error={"code": "missing_source_image"}
            )
            await wallet_release(owner, Decimal(generation["credits_reserved"]), generation_id, "generation-failed")
            return

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
            response = await client.post(f"{base}/v2/{kind}", headers=headers, json=payload)
    except (httpx.TimeoutException, httpx.NetworkError):
        await generation_store.mark_submission_unknown(owner, generation_id)
        # Keep credits reserved. Reconciliation cannot safely infer provider
        # acceptance from a network error and must never submit a duplicate.
        await generation_outbox.enqueue_generation_event(
            generation_id, "reconcile_generation", {"reason": "submission_outcome_unknown"}
        )
        return
    except Exception:
        # Unexpected transport behavior is also ambiguous after the request may
        # have left this process. Fail closed rather than risk duplicate billing.
        await generation_store.mark_submission_unknown(owner, generation_id)
        await generation_outbox.enqueue_generation_event(
            generation_id, "reconcile_generation", {"reason": "submission_outcome_unknown"}
        )
        logger.exception("Unexpected LTX submission outcome for generation %s", generation_id)
        return

    if response.status_code >= 500:
        # A server error can occur after provider-side acceptance. Treat as unknown.
        await generation_store.mark_submission_unknown(owner, generation_id)
        await generation_outbox.enqueue_generation_event(
            generation_id, "reconcile_generation", {"reason": "provider_5xx_outcome_unknown"}
        )
        return
    if response.status_code >= 400:
        await generation_store.transition_generation(
            owner, generation_id, "failed", error={"code": "provider_rejected_request"}
        )
        await wallet_release(owner, Decimal(generation["credits_reserved"]), generation_id, "generation-failed")
        return

    try:
        data = response.json()
    except ValueError:
        await generation_store.mark_submission_unknown(owner, generation_id)
        await generation_outbox.enqueue_generation_event(
            generation_id, "reconcile_generation", {"reason": "invalid_provider_response"}
        )
        return
    provider_job_id = data.get("id") or data.get("job_id")
    if not isinstance(provider_job_id, str) or not provider_job_id.strip():
        await generation_store.mark_submission_unknown(owner, generation_id)
        await generation_outbox.enqueue_generation_event(
            generation_id, "reconcile_generation", {"reason": "missing_provider_job_id"}
        )
        return

    await generation_store.attach_provider_job(owner, generation_id, "ltx", provider_job_id)
    await generation_outbox.enqueue_generation_event(
        generation_id, "reconcile_generation", {"provider_job_id": provider_job_id}
    )


async def _reconcile(generation: dict[str, Any]) -> int:
    generation_id = generation["id"]
    owner = _owner_id(generation.get("user_id"))
    status = generation["status"]
    if status in {"succeeded", "failed"}:
        return 0
    provider_job_id = generation.get("provider_job_id")
    if not provider_job_id:
        # No provider ID means the external result cannot be proven. Keep the
        # reservation and report the ambiguity for operator reconciliation.
        if status != "submission_unknown":
            raise RuntimeError("missing_provider_job_requires_reconciliation")
        logger.error("Generation %s has ambiguous submission without provider job ID; not resubmitting.", generation_id)
        return 300
    if generation["provider"] != "ltx":
        raise RuntimeError("provider_adapter_not_enabled")

    base, headers = _provider_config()
    kind = generation["generation_type"]
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as client:
        response = await client.get(f"{base}/v2/{kind}/{provider_job_id}", headers=headers)
    if response.status_code >= 500 or response.status_code in {408, 429}:
        raise RuntimeError("provider_status_temporarily_unavailable")
    if response.status_code >= 400:
        # A status endpoint 404 is not sufficient evidence that generation failed.
        if response.status_code == 404:
            raise RuntimeError("provider_job_not_found_requires_reconciliation")
        raise RuntimeError("provider_status_rejected")

    data = response.json()
    raw_status = str(data.get("status") or "").strip().lower()
    if raw_status in {"queued", "pending", "submitted", "processing", "running", "in_progress"}:
        if status == "submitted":
            await generation_store.transition_generation(owner, generation_id, "running")
        return 30
    if raw_status in {"succeeded", "success", "completed", "complete"}:
        result = data.get("result") or {}
        video_url = result.get("video_url") if isinstance(result, dict) else None
        if not isinstance(video_url, str) or not video_url.startswith("https://"):
            raise RuntimeError("provider_success_missing_valid_result")
        # Persist the URL privately. Public status API must not expose this raw
        # provider URL; private object storage/signed delivery is a later gate.
        await generation_store.transition_generation(
            owner, generation_id, "succeeded",
            result={"provider_media_url": video_url, "provider": "ltx"},
        )
        reserved = Decimal(generation["credits_reserved"])
        charged = Decimal(generation["credits_charged"])
        remaining = reserved - charged
        if remaining > 0:
            await wallet_capture(owner, remaining, generation_id, "generation-succeeded")
        return 0
    if raw_status in {"failed", "failure", "error", "cancelled", "canceled"}:
        await generation_store.transition_generation(
            owner, generation_id, "failed", error={"code": "provider_generation_failed"}
        )
        reserved = Decimal(generation["credits_reserved"])
        charged = Decimal(generation["credits_charged"])
        remaining = reserved - charged
        if remaining > 0:
            await wallet_release(owner, remaining, generation_id, "generation-failed")
        return 0
    raise RuntimeError("provider_returned_unknown_status")


async def process_event(event: dict[str, Any], worker_id: str) -> None:
    event_id = event["id"]
    generation_id = event["generation_id"]
    token = event["lease_token"]
    delay = 30
    try:
        generation = await _load_generation(generation_id)
        if not generation:
            # The FK normally prevents this, but don't loop forever if the
            # generation was removed by administrative action.
            await generation_outbox.complete_event(event_id, worker_id, token)
            return
        if event["event_type"] == "dispatch_generation":
            await _dispatch(generation)
        elif event["event_type"] == "reconcile_generation":
            delay = await _reconcile(generation)
        else:
            raise RuntimeError("unsupported_outbox_event")
        if delay:
            await generation_outbox.retry_event(
                event_id, worker_id, "generation_pending",
                delay_seconds=delay, max_attempts=100, lease_token=token,
            )
        else:
            await generation_outbox.complete_event(event_id, worker_id, token)
    except Exception as exc:
        # Stable, non-sensitive codes only; never persist exception text or URLs.
        code = str(exc) if str(exc).replace("_", "").isalnum() else "worker_error"
        try:
            await generation_outbox.retry_event(
                event_id, worker_id, code[:100],
                delay_seconds=min(900, 2 ** min(int(event.get("attempts", 1)), 9)),
                max_attempts=12,
                lease_token=token,
            )
        except Exception:
            logger.exception("Could not schedule retry for outbox event %s", event_id)
        logger.exception("Generation worker event failed (event=%s)", event_id)


async def run_worker() -> None:
    if not database.database_configured():
        raise RuntimeError("DATABASE_URL must be configured for the generation worker.")
    await database.start_database()
    await database.initialize_schema()
    from wallet_store import initialize_wallet_schema
    await initialize_wallet_schema()
    await generation_outbox.initialize_outbox_schema()
    worker_id = f"{socket.gethostname()}-{uuid.uuid4()}"
    logger.info("Generation worker started with id %s", worker_id)
    try:
        while True:
            events = await generation_outbox.claim_events(worker_id, limit=10, lease_seconds=120)
            if not events:
                await asyncio.sleep(1.5)
                continue
            for event in events:
                await process_event(event, worker_id)
    finally:
        await database.close_database()


if __name__ == "__main__":
    asyncio.run(run_worker())
