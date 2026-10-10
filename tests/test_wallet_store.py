import asyncio
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

import database
from wallet_store import (
    initialize_wallet_schema,
    wallet_balance,
    wallet_reserve,
    wallet_release,
    wallet_capture,
)


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(scope="session", autouse=True)
async def postgres_database():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for wallet integration tests.")
    await database.start_database()
    await database.initialize_schema()
    await initialize_wallet_schema()
    yield
    await database.close_database()


async def create_test_user_and_generation():
    user_id = str(uuid.uuid4())
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO users(id,email) VALUES (%s,%s)",
                (user_id, f"wallet-test-{user_id}@example.invalid"),
            )
            await conn.execute(
                "INSERT INTO wallets(user_id,available_credits) VALUES (%s,100)",
                (user_id,),
            )
    return user_id, await create_test_generation(user_id)


async def create_test_generation(user_id: str | None = None):
    generation_id = str(uuid.uuid4())
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO generations
                   (id,user_id,provider,generation_type,status,request)
                   VALUES (%s,%s,'test','text-to-video','queued','{}'::jsonb)""",
                (generation_id, user_id),
            )
    return generation_id


async def test_reserve_capture_release_and_idempotency(postgres_database):
    user_id, generation_id = await create_test_user_and_generation()

    reserved = await wallet_reserve(user_id, Decimal("70"), generation_id, "reserve-test-0001")
    assert reserved["ok"] is True
    assert reserved["duplicate"] is False
    assert Decimal(reserved["available_credits"]) == Decimal("30")
    assert Decimal(reserved["reserved_credits"]) == Decimal("70")

    duplicate = await wallet_reserve(user_id, Decimal("70"), generation_id, "reserve-test-0001")
    assert duplicate["duplicate"] is True
    balance = (await wallet_balance(user_id))["wallet"]
    assert Decimal(balance["available_credits"]) == Decimal("30")
    assert Decimal(balance["reserved_credits"]) == Decimal("70")

    captured = await wallet_capture(user_id, Decimal("50"), generation_id, "capture-test-0001")
    assert Decimal(captured["reserved_credits"]) == Decimal("20")
    released = await wallet_release(user_id, Decimal("20"), generation_id, "release-test-0001")
    assert Decimal(released["available_credits"]) == Decimal("50")
    assert Decimal(released["reserved_credits"]) == Decimal("0")

    balance = (await wallet_balance(user_id))["wallet"]
    assert Decimal(balance["available_credits"]) == Decimal("50")
    assert Decimal(balance["reserved_credits"]) == Decimal("0")
    assert Decimal(balance["lifetime_consumed"]) == Decimal("50")


async def test_rejects_overspend_and_cross_generation_settlement(postgres_database):
    user_id, generation_a = await create_test_user_and_generation()
    generation_b = await create_test_generation(user_id)

    insufficient = await wallet_reserve(user_id, Decimal("101"), generation_a, "reserve-test-0002")
    assert insufficient is None

    await wallet_reserve(user_id, Decimal("20"), generation_a, "reserve-test-0003")
    with pytest.raises(ValueError, match="reserved for this generation"):
        await wallet_capture(user_id, Decimal("1"), generation_b, "capture-test-0002")
    with pytest.raises(ValueError, match="reserved for this generation"):
        await wallet_release(user_id, Decimal("21"), generation_a, "release-test-0002")


async def test_rejects_idempotency_key_reuse_with_different_request(postgres_database):
    user_id, generation_id = await create_test_user_and_generation()
    await wallet_reserve(user_id, Decimal("10"), generation_id, "reserve-key-mismatch-1")

    with pytest.raises(ValueError, match="idempotency key"):
        await wallet_reserve(user_id, Decimal("11"), generation_id, "reserve-key-mismatch-1")

    balance = (await wallet_balance(user_id))["wallet"]
    assert Decimal(balance["available_credits"]) == Decimal("90")
    assert Decimal(balance["reserved_credits"]) == Decimal("10")


async def test_rejects_generation_owned_by_another_user(postgres_database):
    user_a, generation_a = await create_test_user_and_generation()
    user_b, _ = await create_test_user_and_generation()

    with pytest.raises(ValueError, match="Generation not found for this user"):
        await wallet_reserve(user_b, Decimal("10"), generation_a, "reserve-cross-user-0001")

    balance = (await wallet_balance(user_b))["wallet"]
    assert Decimal(balance["available_credits"]) == Decimal("100")
    assert Decimal(balance["reserved_credits"]) == Decimal("0")


async def test_concurrent_reservations_cannot_overspend(postgres_database):
    user_id, generation_a = await create_test_user_and_generation()
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE wallets SET available_credits=100 WHERE user_id=%s", (user_id,)
            )
    generation_b = await create_test_generation(user_id)

    results = await asyncio.gather(
        wallet_reserve(user_id, Decimal("80"), generation_a, "reserve-concurrent-a"),
        wallet_reserve(user_id, Decimal("80"), generation_b, "reserve-concurrent-b"),
    )
    assert sum(result is not None for result in results) == 1
    balance = (await wallet_balance(user_id))["wallet"]
    assert Decimal(balance["available_credits"]) == Decimal("20")
    assert Decimal(balance["reserved_credits"]) == Decimal("80")
