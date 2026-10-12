"""Unit tests for generation worker safety invariants."""
import asyncio
from decimal import Decimal

import pytest

import generation_worker


@pytest.mark.asyncio
async def test_restart_during_submission_is_marked_unknown_without_second_provider_call(monkeypatch):
    calls = []
    marked = []
    queued = []

    async def mark_unknown(owner, generation_id):
        marked.append((owner, generation_id))

    async def enqueue(generation_id, event_type, payload):
        queued.append((generation_id, event_type, payload))
        return "event"

    monkeypatch.setattr(generation_worker.generation_store, "mark_submission_unknown", mark_unknown)
    monkeypatch.setattr(generation_worker.generation_outbox, "enqueue_generation_event", enqueue)
    monkeypatch.setattr(generation_worker, "_provider_config", lambda: calls.append("provider-config"))
    generation = {
        "id": "00000000-0000-4000-8000-000000000001",
        "user_id": "00000000-0000-4000-8000-000000000002",
        "status": "submitting",
        "provider": "ltx",
    }

    await generation_worker._dispatch(generation)

    assert marked == [(generation["user_id"], generation["id"])]
    assert queued and queued[0][1] == "reconcile_generation"
    assert calls == []


@pytest.mark.asyncio
async def test_terminal_settlement_releases_or_captures_only_remaining_reservation(monkeypatch):
    calls = []

    async def capture(owner, amount, generation_id, key):
        calls.append(("capture", owner, amount, generation_id, key))

    async def release(owner, amount, generation_id, key):
        calls.append(("release", owner, amount, generation_id, key))

    monkeypatch.setattr(generation_worker, "wallet_capture", capture)
    monkeypatch.setattr(generation_worker, "wallet_release", release)
    base = {
        "id": "00000000-0000-4000-8000-000000000001",
        "user_id": "00000000-0000-4000-8000-000000000002",
        "credits_reserved": Decimal("100.00"),
        "credits_charged": Decimal("25.00"),
    }

    await generation_worker._settle_terminal({**base, "status": "succeeded"})
    await generation_worker._settle_terminal({**base, "status": "failed"})
    await generation_worker._settle_terminal({**base, "status": "failed", "credits_charged": Decimal("100.00")})

    assert calls == [
        ("capture", base["user_id"], Decimal("75.00"), base["id"], "generation-succeeded"),
        ("release", base["user_id"], Decimal("75.00"), base["id"], "generation-failed"),
    ]


@pytest.mark.asyncio
async def test_dispatch_event_completes_after_successful_dispatch_not_retried(monkeypatch):
    calls = []

    async def load(_generation_id):
        return {
            "id": "00000000-0000-4000-8000-000000000001",
            "status": "submitted",
            "provider": "ltx",
            "user_id": "00000000-0000-4000-8000-000000000002",
        }

    async def dispatch(_generation):
        calls.append("dispatch")

    async def complete(event_id, worker_id, token):
        calls.append(("complete", event_id, worker_id, token))

    async def retry(*_args, **_kwargs):
        calls.append("retry")

    monkeypatch.setattr(generation_worker, "_load_generation", load)
    monkeypatch.setattr(generation_worker, "_dispatch", dispatch)
    monkeypatch.setattr(generation_worker.generation_outbox, "complete_event", complete)
    monkeypatch.setattr(generation_worker.generation_outbox, "retry_event", retry)

    await generation_worker.process_event(
        {
            "id": "00000000-0000-4000-8000-000000000003",
            "generation_id": "00000000-0000-4000-8000-000000000001",
            "event_type": "dispatch_generation",
            "lease_token": "00000000-0000-4000-8000-000000000004",
            "attempts": 1,
        },
        "test-worker",
    )

    assert calls[0] == "dispatch"
    assert calls[1][0] == "complete"
    assert "retry" not in calls


@pytest.mark.asyncio
async def test_cost_persistence_failure_never_marks_generation_succeeded(monkeypatch):
    generation_id = "00000000-0000-4000-8000-000000000001"
    owner_id = "00000000-0000-4000-8000-000000000002"
    calls = []

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"status": "succeeded", "result": {"video_url": "https://media.example.test/video.mp4"}}

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(generation_worker, "_provider_config", lambda: ("https://api.ltx.io", {}))
    monkeypatch.setattr(generation_worker.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(
        generation_worker, "_download_and_store_video",
        lambda *_args: _async_return({"object_key": f"generations/{generation_id}/video.mp4",
                                     "content_type": "video/mp4", "size_bytes": 128}),
    )

    def quote(**_kwargs):
        return {
            "provider": "ltx", "model": "ltx-2-5-fast", "resolution": "1080p",
            "duration_seconds": 6.0, "provider_cost_usd": 0.78,
            "provider_cost_with_risk_buffer_usd": 0.858, "cost_basis": "test",
        }

    async def fail_cost(_record):
        calls.append("cost_persistence")
        raise RuntimeError("database_temporarily_unavailable")

    async def transition(*_args, **_kwargs):
        calls.append("transition_succeeded")

    monkeypatch.setattr(generation_worker, "pricing_quote", quote)
    monkeypatch.setattr(generation_worker.provider_costs, "record_generation_cost_estimate", fail_cost)
    monkeypatch.setattr(generation_worker.generation_store, "transition_generation", transition)

    generation = {
        "id": generation_id, "user_id": owner_id, "provider": "ltx",
        "provider_job_id": "job-test", "generation_type": "text-to-video",
        "status": "running",
        "request": {"model": "ltx-2-5-fast", "resolution": "1080p", "duration": 6},
    }
    with pytest.raises(RuntimeError, match="database_temporarily_unavailable"):
        await generation_worker._reconcile(generation)

    assert calls == ["cost_persistence"]


async def _async_return(value):
    return value


@pytest.mark.asyncio
async def test_cost_persistence_failure_schedules_outbox_retry(monkeypatch):
    calls = []
    generation_id = "00000000-0000-4000-8000-000000000001"

    async def load(_generation_id):
        return {
            "id": generation_id,
            "user_id": "00000000-0000-4000-8000-000000000002",
            "provider": "ltx",
            "status": "running",
            "provider_job_id": "job-test",
        }

    async def reconcile(_generation):
        raise RuntimeError("database_temporarily_unavailable")

    async def retry(event_id, worker_id, error_code, **kwargs):
        calls.append(("retry", event_id, worker_id, error_code, kwargs))

    async def complete(*args):
        calls.append(("complete", *args))

    monkeypatch.setattr(generation_worker, "_load_generation", load)
    monkeypatch.setattr(generation_worker, "_reconcile", reconcile)
    monkeypatch.setattr(generation_worker.generation_outbox, "retry_event", retry)
    monkeypatch.setattr(generation_worker.generation_outbox, "complete_event", complete)

    await generation_worker.process_event(
        {
            "id": "00000000-0000-4000-8000-000000000003",
            "generation_id": generation_id,
            "event_type": "reconcile_generation",
            "lease_token": "00000000-0000-4000-8000-000000000004",
            "attempts": 1,
        },
        "test-worker",
    )

    assert len(calls) == 1
    action, event_id, worker_id, error_code, kwargs = calls[0]
    assert action == "retry"
    assert event_id == "00000000-0000-4000-8000-000000000003"
    assert worker_id == "test-worker"
    assert error_code == "database_temporarily_unavailable"
    assert kwargs["delay_seconds"] == 2
    assert kwargs["max_attempts"] == 12
    assert kwargs["lease_token"] == "00000000-0000-4000-8000-000000000004"
