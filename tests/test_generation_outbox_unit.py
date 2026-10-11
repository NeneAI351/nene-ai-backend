"""Input and fail-closed tests for durable generation outbox primitives."""
import asyncio
import uuid

import pytest

import database
import generation_outbox


def test_enqueue_rejects_invalid_generation_id():
    with pytest.raises(ValueError, match="Invalid generation identifier"):
        asyncio.run(generation_outbox.enqueue_generation_event("not-a-uuid"))


def test_enqueue_rejects_unknown_event_type():
    with pytest.raises(ValueError, match="Unsupported generation outbox event"):
        asyncio.run(generation_outbox.enqueue_generation_event(
            str(uuid.uuid4()), event_type="run-arbitrary-code"
        ))


def test_outbox_requires_durable_database(monkeypatch):
    monkeypatch.setattr(database, "_pool", None)
    with pytest.raises(RuntimeError, match="Durable outbox storage is required"):
        asyncio.run(generation_outbox.claim_events("worker-a"))


@pytest.mark.parametrize("worker,limit,lease", [
    ("", 1, 60),
    ("worker-a", 0, 60),
    ("worker-a", 101, 60),
    ("worker-a", 1, 9),
    ("worker-a", 1, 901),
])
def test_claim_rejects_invalid_limits(worker, limit, lease):
    with pytest.raises(ValueError, match="Invalid"):
        asyncio.run(generation_outbox.claim_events(worker, limit, lease))


def test_retry_rejects_untrusted_error_code():
    with pytest.raises(ValueError, match="Invalid stable error code"):
        asyncio.run(generation_outbox.retry_event(
            str(uuid.uuid4()), "worker-a", "bad error\nwith log injection"
        ))


def test_retry_rejects_unbounded_policy():
    with pytest.raises(ValueError, match="Invalid retry policy"):
        asyncio.run(generation_outbox.retry_event(
            str(uuid.uuid4()), "worker-a", "provider_timeout", delay_seconds=0
        ))
