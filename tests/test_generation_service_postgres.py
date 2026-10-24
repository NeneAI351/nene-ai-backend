"""PostgreSQL integration tests for atomic generation pricing/reservation/queue creation."""
import os
import uuid

import pytest
import pytest_asyncio

import database
import generation_service
import generation_outbox
from paid_fixtures import fund_paid
from wallet_store import initialize_wallet_schema


@pytest_asyncio.fixture(scope="session", autouse=True)
async def generation_service_db():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for generation service integration tests.")
    if database._pool is None:
        await database.start_database()
    await database.initialize_schema()
    await initialize_wallet_schema()
    await generation_outbox.initialize_outbox_schema()
    import payments
    await payments.initialize_payment_schema()
    yield


async def _user_with_credits(credits: str) -> str:
    user_id = str(uuid.uuid4())
    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO users(id,email) VALUES (%s,%s)",
                (user_id, f"{user_id}@test.invalid"),
            )
            await conn.execute(
                "INSERT INTO wallets(user_id,available_credits) VALUES (%s,%s)",
                (user_id, "0"),
            )
    await fund_paid(user_id,credits)
    return user_id


@pytest.mark.asyncio
async def test_create_reserves_server_quote_and_replays_idempotently(generation_service_db):
    user_id = await _user_with_credits("500.00")
    kwargs = dict(
        prompt="A cinematic sunrise over the ocean",
        provider="ltx",
        model="ltx-2-5-fast",
        resolution="720p",
        duration_seconds=6,
    )
    first = await generation_service.create_reserved_generation(user_id, "request-1", **kwargs)
    replay = await generation_service.create_reserved_generation(user_id, "request-1", **kwargs)
    assert first["generation_id"] == replay["generation_id"]
    assert replay["duplicate"] is True
    assert Decimal(first["credits_reserved"]) == Decimal(first["quote"]["recommended_credits"])
    assert first["status"] == "queued"

    row = await database.fetch_one(
        """SELECT g.status,g.credits_reserved,w.available_credits,w.reserved_credits,
                  (SELECT COUNT(*) FROM generation_outbox o WHERE o.generation_id=g.id) AS outbox_count
           FROM generations g JOIN wallets w ON w.user_id=g.user_id WHERE g.id=%s""",
        (first["generation_id"],),
    )
    assert row["status"] == "queued"
    assert Decimal(row["credits_reserved"]) == Decimal(first["credits_reserved"])
    assert Decimal(row["reserved_credits"]) == Decimal(first["credits_reserved"])
    assert Decimal(row["available_credits"]) == Decimal("500.00") - Decimal(first["credits_reserved"])
    assert row["outbox_count"] == 1

    # Audio-on was the provider's historical default. Explicitly selecting it
    # must replay an older request without another reservation or submission.
    audio_replay=await generation_service.create_reserved_generation(user_id,'request-1',generate_audio=True,**kwargs)
    assert audio_replay['generation_id']==first['generation_id']
    with pytest.raises(generation_service.GenerationRequestConflict):
        await generation_service.create_reserved_generation(user_id,'request-1',generate_audio=False,**kwargs)

    silent=await generation_service.create_reserved_generation(user_id,'silent-request',generate_audio=False,**kwargs)
    silent_replay=await generation_service.create_reserved_generation(user_id,'silent-request',generate_audio=False,**kwargs)
    assert silent['generation_id']==silent_replay['generation_id']
    persisted=await database.fetch_one('SELECT request FROM generations WHERE id=%s',(silent['generation_id'],))
    assert persisted['request']['generate_audio'] is False

    with pytest.raises(generation_service.GenerationRequestConflict):
        await generation_service.create_reserved_generation(
            user_id, "request-1", **{**kwargs, "prompt": "different prompt"}
        )


@pytest.mark.asyncio
async def test_insufficient_credits_rolls_back_generation_and_idempotency(generation_service_db):
    user_id = await _user_with_credits("0.00")
    kwargs = dict(
        prompt="A cinematic sunrise over the ocean",
        provider="ltx",
        model="ltx-2-5-fast",
        resolution="720p",
        duration_seconds=6,
    )
    with pytest.raises(generation_service.InsufficientCredits):
        await generation_service.create_reserved_generation(user_id, "poor-request", **kwargs)

    wallet = await database.fetch_one(
        "SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s", (user_id,)
    )
    requests = await database.fetch_one(
        "SELECT COUNT(*) AS count FROM generation_requests WHERE user_id=%s", (user_id,)
    )
    generations = await database.fetch_one(
        "SELECT COUNT(*) AS count FROM generations WHERE user_id=%s", (user_id,)
    )
    assert Decimal(wallet["available_credits"]) == Decimal("0.00")
    assert Decimal(wallet["reserved_credits"]) == Decimal("0.00")
    assert requests["count"] == 0
    assert generations["count"] == 0


from decimal import Decimal



@pytest.mark.asyncio
async def test_image_generation_persists_private_asset_id_not_public_url(generation_service_db):
    user_id = await _user_with_credits("500.00")
    asset_id = str(uuid.uuid4())
    async with database._pool.connection() as conn:
        await conn.execute(
            """INSERT INTO media_assets
               (id,user_id,asset_type,object_key,content_type,size_bytes)
               VALUES (%s,%s,'source_image',%s,'image/jpeg',128)""",
            (asset_id, user_id, f"source-images/{user_id}/{asset_id}.jpg"),
        )
        await conn.commit()

    result = await generation_service.create_reserved_generation(
        user_id,
        "private-image-request",
        prompt="Animate the uploaded portrait with gentle camera movement",
        provider="ltx",
        model="ltx-2-5-fast",
        resolution="720p",
        duration_seconds=6,
        generation_type="image-to-video",
        source_image_id=asset_id,
    )
    row = await database.fetch_one(
        "SELECT request FROM generations WHERE id=%s AND user_id=%s",
        (result["generation_id"], user_id),
    )
    request = row["request"]
    if isinstance(request, str):
        import json
        request = json.loads(request)
    assert request["source_image_id"] == asset_id
    assert "image_url" not in request


@pytest.mark.asyncio
async def test_user_cannot_use_another_owners_private_source_asset(generation_service_db):
    owner_id = await _user_with_credits("500.00")
    other_user_id = await _user_with_credits("500.00")
    asset_id = str(uuid.uuid4())
    async with database._pool.connection() as conn:
        await conn.execute(
            """INSERT INTO media_assets
               (id,user_id,asset_type,object_key,content_type,size_bytes)
               VALUES (%s,%s,'source_image',%s,'image/jpeg',128)""",
            (asset_id, owner_id, f"source-images/{owner_id}/{asset_id}.jpg"),
        )
        await conn.commit()

    with pytest.raises(LookupError, match="Source image not found"):
        await generation_service.create_reserved_generation(
            other_user_id,
            "stolen-image-request",
            prompt="Animate a portrait",
            provider="ltx",
            model="ltx-2-5-fast",
            resolution="720p",
            duration_seconds=6,
            generation_type="image-to-video",
            source_image_id=asset_id,
        )


@pytest.mark.asyncio
async def test_reviewed_credit_limit_blocks_higher_price_without_side_effects(generation_service_db):
    user_id=await _user_with_credits("500.00")
    kwargs=dict(prompt="Advertisement for a neighbourhood bakery",provider="ltx",model="ltx-2-5-fast",resolution="720p",duration_seconds=6)
    price=Decimal(generation_service.pricing_quote('ltx','ltx-2-5-fast','720p',6)['recommended_credits'])
    with pytest.raises(generation_service.GenerationPriceChanged):
        await generation_service.create_reserved_generation(user_id,'ad-price-change',credit_limit=price-Decimal('0.01'),**kwargs)
    assert (await database.fetch_one('SELECT COUNT(*) AS n FROM generations WHERE user_id=%s',(user_id,)))['n']==0
    assert (await database.fetch_one('SELECT COUNT(*) AS n FROM generation_requests WHERE user_id=%s',(user_id,)))['n']==0
    wallet=await database.fetch_one('SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s',(user_id,))
    assert Decimal(wallet['available_credits'])==500 and Decimal(wallet['reserved_credits'])==0


@pytest.mark.asyncio
async def test_reviewed_price_replay_never_queues_or_charges_again(generation_service_db,monkeypatch):
    user_id=await _user_with_credits("500.00")
    kwargs=dict(prompt="Advertisement for a neighbourhood bakery",provider="ltx",model="ltx-2-5-fast",resolution="720p",duration_seconds=6)
    original=generation_service.pricing_quote('ltx','ltx-2-5-fast','720p',6)
    limit=Decimal(original['recommended_credits'])
    first=await generation_service.create_reserved_generation(user_id,'ad-safe-retry',credit_limit=limit,**kwargs)
    monkeypatch.setattr(generation_service,'pricing_quote',lambda *a,**kw:dict(original,recommended_credits=str(limit+100)))
    replay=await generation_service.create_reserved_generation(user_id,'ad-safe-retry',credit_limit=limit,**kwargs)
    assert replay['generation_id']==first['generation_id'] and replay['duplicate'] is True
    assert replay['credits_reserved']==first['credits_reserved']
    assert (await database.fetch_one('SELECT COUNT(*) AS n FROM generation_outbox WHERE generation_id=%s',(first['generation_id'],)))['n']==1

