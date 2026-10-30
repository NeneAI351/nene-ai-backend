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
import signal
import socket
import tempfile
import uuid
from pathlib import Path
from decimal import Decimal
from typing import Any

import httpx

import database
import generation_outbox
import generation_store
import media_storage
import provider_costs
import speech_assets
from ltx_contract import output_resolution
from pricing import quote as pricing_quote
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


async def _download_and_store_video(video_url: str, generation_id: str) -> dict[str, Any]:
    """Fetch one allowlisted provider URL without redirects, then persist privately."""
    media_storage.validate_provider_media_url(video_url)
    max_bytes = media_storage.MAX_VIDEO_BYTES
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="nene-video-", suffix=".mp4", delete=False) as tmp:
            temp_path = tmp.name
            total = 0
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=10.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream("GET", video_url, headers={"Accept": "video/mp4"}) as response:
                    if response.status_code != 200:
                        raise RuntimeError("provider_media_download_failed")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type not in media_storage.ALLOWED_VIDEO_CONTENT_TYPES:
                        raise RuntimeError("provider_media_content_type_rejected")
                    declared = response.headers.get("content-length")
                    if declared:
                        try:
                            if int(declared) > max_bytes:
                                raise RuntimeError("provider_media_too_large")
                        except ValueError:
                            raise RuntimeError("provider_media_invalid_length")
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise RuntimeError("provider_media_too_large")
                        tmp.write(chunk)
            tmp.flush()
        if total <= 0:
            raise RuntimeError("provider_media_empty")
        # Validate MP4 signature and upload outside the event loop; boto3 is synchronous.
        stored = await asyncio.to_thread(media_storage.upload_video_file, temp_path, generation_id)
        return stored
    finally:
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary video file for generation %s", generation_id)


async def _dispatch(generation: dict[str, Any]) -> None:
    generation_id = generation["id"]
    owner = _owner_id(generation.get("user_id"))
    status = generation["status"]
    if status == "submitting":
        # A worker may have crashed after persisting the pre-submit state. The
        # network call may or may not have happened; never submit a second time.
        await generation_store.mark_submission_unknown(owner, generation_id)
        await generation_outbox.enqueue_generation_event(
            generation_id, "reconcile_generation", {"reason": "worker_restarted_during_submission"}
        )
        return
    if status != "queued":
        # A redelivered dispatch event must not submit a second external job.
        if status in {"succeeded", "failed"}:
            await _settle_terminal(generation)
            return
        if status in {"submitted", "running", "submission_unknown"}:
            return
        raise RuntimeError("generation_state_not_dispatchable")
    if generation["provider"] == "magic-hour" and generation["generation_type"] == "text-to-speech":
        import speech_synthesis
        await speech_synthesis.dispatch(generation)
        return
    if generation['provider'] == 'magic-hour':
        import magic_hour_video
        await magic_hour_video.dispatch(generation)
        return
    if generation["provider"] != "ltx":
        raise RuntimeError("provider_adapter_not_enabled")
    base, headers = _provider_config()
    request = generation["request"]
    if isinstance(request, str):
        request = json.loads(request)
    kind = generation["generation_type"]
    if kind not in {"text-to-video", "image-to-video", "audio-to-video"}:
        raise RuntimeError("unsupported_generation_type")

    payload = {
        "prompt": str(request["prompt"]),
        "model": str(request["model"]),
        "resolution": output_resolution(request["resolution"], request.get("aspect_ratio")),
        "duration": int(request["duration"]),
        "generate_audio": request.get("generate_audio", True),
    }
    if kind in {"image-to-video", "audio-to-video"}:
        source_image_id = request.get("source_image_id")
        if source_image_id:
            asset = await database.fetch_one(
                """SELECT object_key FROM media_assets
                   WHERE id=%s AND user_id=%s AND asset_type='source_image'
                     AND deleted_at IS NULL""",
                (source_image_id, owner),
            )
            if not asset:
                await generation_store.transition_generation(
                    owner, generation_id, "failed", error={"code": "source_image_not_found"}
                )
                terminal = await _load_generation(generation_id)
                if terminal:
                    await _settle_terminal(terminal)
                return
            # Mint a short-lived GET URL only immediately before provider submission;
            # never persist it in the generation request or expose it to the browser.
            payload["image_uri"] = await asyncio.to_thread(
                media_storage.create_signed_source_image_url, asset["object_key"], 300
            )
        else:
            # Legacy non-production generation requests only. Production API
            # rejects this public-URL path and requires an owner-scoped asset ID.
            payload["image_uri"] = request.get("image_url")
        if not payload["image_uri"]:
            await generation_store.transition_generation(
                owner, generation_id, "failed", error={"code": "missing_source_image"}
            )
            terminal = await _load_generation(generation_id)
            if terminal:
                await _settle_terminal(terminal)
            return

    if kind == "audio-to-video":
        audio = await database.fetch_one("SELECT object_key FROM speech_assets WHERE id=%s AND user_id=%s",(request.get("source_audio_id"),owner))
        if not audio:
            await generation_store.transition_generation(owner,generation_id,"failed",error={"code":"source_audio_not_found"})
            terminal=await _load_generation(generation_id)
            if terminal:await _settle_terminal(terminal)
            return
        payload["audio_uri"] = await asyncio.to_thread(speech_assets.signed_url,audio["object_key"])
        for field in ("duration","generate_audio","aspect_ratio"):payload.pop(field,None)

    # Validate local/private dependencies and mint any expiring source URL
    # before persisting 'submitting'. Only the provider network call itself is
    # inside the ambiguous-submission window.
    await generation_store.begin_submission(owner, generation_id)
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

    if response.status_code >= 500 or response.status_code in {408,409}:
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


async def _settle_terminal(generation: dict[str, Any]) -> None:
    """Idempotently finish credit settlement even after a crash between DB transactions."""
    owner = _owner_id(generation.get("user_id"))
    generation_id = generation["id"]
    reserved = Decimal(generation["credits_reserved"])
    charged = Decimal(generation["credits_charged"])
    remaining = reserved - charged
    if remaining <= 0:
        return
    if generation["status"] == "succeeded":
        await wallet_capture(owner, remaining, generation_id, "generation-succeeded")
    elif generation["status"] == "failed":
        await wallet_release(owner, remaining, generation_id, "generation-failed")


async def _reconcile(generation: dict[str, Any]) -> int:
    generation_id = generation["id"]
    owner = _owner_id(generation.get("user_id"))
    status = generation["status"]
    if status in {"succeeded", "failed"}:
        await _settle_terminal(generation)
        return 0
    provider_job_id = generation.get("provider_job_id")
    if not provider_job_id:
        # No provider ID means the external result cannot be proven. Keep the
        # reservation and report the ambiguity for operator reconciliation.
        if status != "submission_unknown":
            raise RuntimeError("missing_provider_job_requires_reconciliation")
        logger.error("Generation %s has ambiguous submission without provider job ID; not resubmitting.", generation_id)
        return 300
    if generation["provider"] == "magic-hour" and generation["generation_type"] == "text-to-speech":
        import speech_synthesis
        return await speech_synthesis.reconcile(generation)
    if generation['provider'] == 'magic-hour':
        import magic_hour_video
        return await magic_hour_video.reconcile(generation)
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
        # Never persist provider URLs as deliverable media. Download without
        # redirects, validate the MP4 signature/size, and store in a private bucket
        # before marking the generation successful or capturing credits.
        stored = await _download_and_store_video(video_url, generation_id)
        # Record a traceable estimate before marking the job successful. The
        # rate-card value is not an invoice amount; the metadata says so.
        request = generation.get("request") or {}
        if isinstance(request, str):
            request = json.loads(request)
        cost_quote = pricing_quote(
            provider="ltx",
            model=str(request.get("model") or ""),
            resolution=str(request.get("resolution") or ""),
            duration=float(request.get("duration") or 0),
        )
        cost_record = provider_costs.estimate_generation_cost(
            generation_id=generation_id,
            provider="ltx",
            model=str(request.get("model") or ""),
            resolution=str(request.get("resolution") or ""),
            duration_seconds=request.get("duration"),
            rate_quote=cost_quote,
        )
        await provider_costs.record_generation_cost_estimate(cost_record)
        await generation_store.transition_generation(
            owner, generation_id, "succeeded",
            result={
                "object_key": stored["object_key"],
                "content_type": stored["content_type"],
                "size_bytes": stored["size_bytes"],
                "provider": "ltx",
            },
        )
        # Re-read the terminal row so settlement recovery uses committed values.
        terminal = await _load_generation(generation_id)
        if terminal:
            await _settle_terminal(terminal)
        return 0
    if raw_status in {"failed", "failure", "error", "cancelled", "canceled"}:
        await generation_store.transition_generation(
            owner, generation_id, "failed", error={"code": "provider_generation_failed"}
        )
        terminal = await _load_generation(generation_id)
        if terminal:
            await _settle_terminal(terminal)
        return 0
    raise RuntimeError("provider_returned_unknown_status")


async def _process_event_unlocked(event: dict[str, Any], worker_id: str) -> None:
    event_id = event["id"]
    generation_id = event["generation_id"]
    token = event["lease_token"]
    delay = 0
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


async def process_event(event: dict[str, Any], worker_id: str) -> None:
    import account_privacy
    row=await _load_generation(event['generation_id'])
    if not row:return
    async with account_privacy.worker_guard(row.get('user_id')) as active:
        if active:await _process_event_unlocked(event,worker_id)


async def process_leased_event(event: dict[str, Any], worker_id: str, heartbeat_seconds: float = 30) -> None:
    """Keep a single active claim alive and stop work if lease renewal fails."""
    if not 0 < heartbeat_seconds < 120:
        raise ValueError("Heartbeat must run before the lease expires.")
    await generation_outbox.renew_event(event['id'],worker_id,event['lease_token'])
    async def heartbeat():
        while True:
            await asyncio.sleep(heartbeat_seconds)
            await generation_outbox.renew_event(event['id'],worker_id,event['lease_token'])
    work = asyncio.create_task(process_event(event,worker_id))
    pulse = asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait({work,pulse},return_when=asyncio.FIRST_COMPLETED)
        # Completion can clear the lease at the same instant as a heartbeat.
        if work in done:
            await work
        else:
            await pulse
    finally:
        for task in (work,pulse):
            if not task.done():task.cancel()
        await asyncio.gather(work,pulse,return_exceptions=True)


async def consume_events(worker_id: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        # Provisioning a worker must not automatically buy old queued work.
        if os.getenv('NENE_GENERATION_WORKER_ENABLED','').strip().lower() != 'true':
            try:await asyncio.wait_for(stop.wait(),timeout=1.5)
            except asyncio.TimeoutError:pass
            continue
        # Do not pre-claim jobs that will sit idle behind a slow provider call.
        events = await generation_outbox.claim_events(worker_id,limit=1,lease_seconds=120)
        if not events:
            try:await asyncio.wait_for(stop.wait(),timeout=1.5)
            except asyncio.TimeoutError:pass
            continue
        try:
            await process_leased_event(events[0],worker_id)
        except Exception:
            # The durable generation state handles ambiguous submissions on
            # reclaim. Do not resubmit or release reserved credits here.
            logger.error("Generation lease processing stopped (event=%s)",events[0]['id'])

async def run_worker() -> None:
    if not database.database_configured():
        raise RuntimeError("DATABASE_URL must be configured for the generation worker.")
    await database.start_database()
    await database.initialize_schema()
    from wallet_store import initialize_wallet_schema
    await initialize_wallet_schema()
    await generation_outbox.initialize_outbox_schema()
    await speech_assets.initialize_schema()
    import account_privacy
    await account_privacy.initialize_schema()
    worker_id = f"{socket.gethostname()}-{uuid.uuid4()}"
    logger.info("Generation worker started with id %s", worker_id)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered = []
    try:
        for sig in (signal.SIGTERM,signal.SIGINT):
            loop.add_signal_handler(sig,stop.set)
            registered.append(sig)
        await consume_events(worker_id,stop)
    finally:
        for sig in registered:loop.remove_signal_handler(sig)
        await database.close_database()


if __name__ == "__main__":
    asyncio.run(run_worker())

