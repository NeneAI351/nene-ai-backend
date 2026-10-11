"""PostgreSQL integration coverage for outbox deduplication and lease fencing."""
import os
import uuid

import pytest
import pytest_asyncio

import database
import generation_outbox


@pytest_asyncio.fixture(scope="session", autouse=True)
async def postgres_outbox():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for outbox integration tests.")

    if database._pool is None:
        await database.start_database()
    await database.initialize_schema()
    await generation_outbox.initialize_outbox_schema()
    yield


async def _generation():
    generation_id = str(uuid.uuid4())
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO generations
                   (id,user_id,provider,generation_type,status,request)
                   VALUES (%s,NULL,'test','text-to-video','queued','{}'::jsonb)""",
                (generation_id,),
            )
    return generation_id


async def test_enqueue_is_idempotent_and_preserves_original_event(postgres_outbox):
        generation_id = await _generation()
        event_id = await generation_outbox.enqueue_generation_event(
            generation_id, payload={"source": "first"}
        )
        replay_id = await generation_outbox.enqueue_generation_event(
            generation_id, payload={"source": "changed-on-replay"}
        )
        assert replay_id == event_id
        row = await database.fetch_one(
            "SELECT payload, state FROM generation_outbox WHERE id=%s", (event_id,)
        )
        assert row["payload"] == {"source": "first"}
        assert row["state"] == "pending"


async def test_claim_complete_and_stale_lease_is_fenced(postgres_outbox):
        generation_id = await _generation()
        event_id = await generation_outbox.enqueue_generation_event(generation_id)
        first = await generation_outbox.claim_events("worker-a", limit=10, lease_seconds=10)
        claimed = next(item for item in first if item["id"] == event_id)
        first_token = claimed["lease_token"]
        assert claimed["attempts"] == 1

        async with database._pool.connection() as conn:
            await conn.execute(
                "UPDATE generation_outbox SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=%s",
                (event_id,),
            )

        second = await generation_outbox.claim_events("worker-b", limit=10, lease_seconds=10)
        reclaimed = next(item for item in second if item["id"] == event_id)
        assert reclaimed["attempts"] == 2
        assert reclaimed["lease_token"] != first_token

        with pytest.raises(generation_outbox.OutboxConflict):
            await generation_outbox.complete_event(event_id, "worker-a", first_token)

        await generation_outbox.complete_event(event_id, "worker-b", reclaimed["lease_token"])
        row = await database.fetch_one(
            "SELECT state, lease_token FROM generation_outbox WHERE id=%s", (event_id,)
        )
        assert row["state"] == "completed"
        assert row["lease_token"] is None

        # A completed event cannot be claimed again.
        later = await generation_outbox.claim_events("worker-c", limit=100, lease_seconds=10)
        assert all(item["id"] != event_id for item in later)


async def test_retry_requires_current_lease_and_dead_letters_at_limit(postgres_outbox):
        generation_id = await _generation()
        event_id = await generation_outbox.enqueue_generation_event(generation_id)
        claimed = await generation_outbox.claim_events("retry-worker", limit=10, lease_seconds=10)
        event = next(item for item in claimed if item["id"] == event_id)

        with pytest.raises(generation_outbox.OutboxConflict):
            await generation_outbox.retry_event(
                event_id, "wrong-worker", "provider_timeout",
                delay_seconds=1, max_attempts=1, lease_token=event["lease_token"],
            )

        await generation_outbox.retry_event(
            event_id, "retry-worker", "provider_timeout",
            delay_seconds=1, max_attempts=1, lease_token=event["lease_token"],
        )
        row = await database.fetch_one(
            "SELECT state, lease_token, last_error_code FROM generation_outbox WHERE id=%s",
            (event_id,),
        )
        assert row["state"] == "dead"
        assert row["lease_token"] is None
        assert row["last_error_code"] == "provider_timeout"
