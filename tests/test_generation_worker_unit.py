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
