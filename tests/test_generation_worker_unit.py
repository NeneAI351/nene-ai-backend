"""Unit tests for generation worker safety invariants."""
import asyncio
from decimal import Decimal

import pytest

import generation_worker
import account_privacy
from contextlib import asynccontextmanager


@pytest.fixture(autouse=True)
def isolate_account_guard(monkeypatch):
    @asynccontextmanager
    async def active(owner):yield True
    monkeypatch.setattr(account_privacy,'worker_guard',active)

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


@pytest.mark.asyncio
async def test_active_event_renews_lease_until_processing_finishes(monkeypatch):
    event={'id':'event','generation_id':'generation','lease_token':'token'}
    renewed=asyncio.Event();calls=[]
    async def renew(*args):
        calls.append(args)
        if len(calls)>=3:renewed.set()
    async def process(*args):await asyncio.wait_for(renewed.wait(),timeout=1)
    monkeypatch.setattr(generation_worker.generation_outbox,'renew_event',renew)
    monkeypatch.setattr(generation_worker,'process_event',process)
    await generation_worker.process_leased_event(event,'worker',heartbeat_seconds=.001)
    assert len(calls)>=3
    assert all(call==('event','worker','token') for call in calls)


@pytest.mark.asyncio
async def test_lost_lease_cancels_active_processing_without_resubmission(monkeypatch):
    event={'id':'event','generation_id':'generation','lease_token':'token'}
    started=asyncio.Event();cancelled=asyncio.Event();calls=[]
    async def renew(*args):
        calls.append(args)
        if len(calls)>1:
            await started.wait()
            raise generation_worker.generation_outbox.OutboxConflict('replaced')
    async def process(*args):
        started.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    monkeypatch.setattr(generation_worker.generation_outbox,'renew_event',renew)
    monkeypatch.setattr(generation_worker,'process_event',process)
    with pytest.raises(generation_worker.generation_outbox.OutboxConflict):
        await generation_worker.process_leased_event(event,'worker',heartbeat_seconds=.001)
    assert cancelled.is_set()
    assert len(calls)==2


@pytest.mark.asyncio
async def test_invalid_initial_lease_never_starts_provider_processing(monkeypatch):
    from unittest.mock import AsyncMock
    process=AsyncMock()
    monkeypatch.setattr(generation_worker,'process_event',process)
    monkeypatch.setattr(generation_worker.generation_outbox,'renew_event',AsyncMock(side_effect=generation_worker.generation_outbox.OutboxConflict('expired')))
    with pytest.raises(generation_worker.generation_outbox.OutboxConflict):
        await generation_worker.process_leased_event({'id':'event','lease_token':'token'},'worker')
    process.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_finishes_claimed_event_without_claiming_more_jobs(monkeypatch):
    monkeypatch.setenv('NENE_GENERATION_WORKER_ENABLED','true')
    stop=asyncio.Event();claims=[];processed=[]
    async def claim(worker,**kwargs):
        claims.append(kwargs);return [{'id':'current'}]
    async def process(event,worker):
        stop.set();processed.append(event['id'])
    monkeypatch.setattr(generation_worker.generation_outbox,'claim_events',claim)
    monkeypatch.setattr(generation_worker,'process_leased_event',process)
    await generation_worker.consume_events('worker',stop)
    assert claims==[{'limit':1,'lease_seconds':120}]
    assert processed==['current']


@pytest.mark.asyncio
async def test_worker_does_not_claim_jobs_until_explicitly_enabled(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.delenv('NENE_GENERATION_WORKER_ENABLED',raising=False)
    claim=AsyncMock();monkeypatch.setattr(generation_worker.generation_outbox,'claim_events',claim)
    stop=asyncio.Event()
    asyncio.get_running_loop().call_soon(stop.set)
    await generation_worker.consume_events('worker',stop)
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_worker_starts_without_database_and_exits_on_sigterm():
    import os
    import sys
    import signal
    environment = dict(os.environ, NENE_GENERATION_WORKER_ENABLED='false', DATABASE_URL='')
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-u', 'generation_worker.py', env=environment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=15)
        assert b'Generation worker paused' in line
        assert process.returncode is None
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), timeout=5) == 0
        assert b'Traceback' not in await process.stdout.read()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_enabled_worker_requires_database_before_any_job_claim(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setenv('NENE_GENERATION_WORKER_ENABLED', 'true')
    monkeypatch.setattr(generation_worker.database, 'database_configured', lambda: False)
    start = AsyncMock()
    claim = AsyncMock()
    monkeypatch.setattr(generation_worker.database, 'start_database', start)
    monkeypatch.setattr(generation_worker.database, 'close_database', AsyncMock())
    monkeypatch.setattr(generation_worker.generation_outbox, 'claim_events', claim)
    with pytest.raises(RuntimeError, match='DATABASE_URL must be configured'):
        await generation_worker.run_worker()
    start.assert_not_awaited()
    claim.assert_not_awaited()
