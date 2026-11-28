"""Owner-scoped lip sync for saved NENE AI video and voice assets."""
import asyncio
import os
import re
import uuid
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

import database
import generation_outbox
import generation_service
import generation_store
import magic_hour_video
import media_storage
import pricing
from auth import get_authenticated_user_id
from rate_limits import enforce_rate_limit

router = APIRouter(prefix="/api/lip-sync", tags=["lip-sync"])
MODEL = "lip-sync-standard"
FPS = 24
CREDITS_PER_SECOND = 14


def quote(duration):
    if duration not in {6, 8, 10}:
        raise ValueError("Choose 6, 8, or 10 seconds.")
    rate = Decimal(os.getenv("NENE_MAGIC_HOUR_CREDIT_USD", "0.0025"))
    if not rate.is_finite() or not 0 < rate <= 1:
        raise ValueError("Lip-sync pricing is unavailable.")
    cost = rate * duration * FPS
    return {"provider": "magic-hour", "model": MODEL, "resolution": "source",
            "duration_seconds": duration, "recommended_credits": duration * CREDITS_PER_SECOND,
            "provider_cost_usd": float(cost), "provider_cost_with_risk_buffer_usd": float(cost) * (1 + pricing.RISK_BUFFER),
            "rate_basis": "standard_mode_capped_frames"}


def enabled():
    if (not database._pool or os.getenv("NENE_LIP_SYNC_ENABLED") != "true"
            or os.getenv("NENE_DURABLE_GENERATION_CREATE_ENABLED") != "true"
            or not os.getenv("MAGIC_HOUR_API_KEY", "").strip()
            or not os.getenv("NENE_MAGIC_HOUR_CREDIT_USD", "").strip()
            or "videos.magichour.ai" not in {host.strip().lower() for host in os.getenv("ALLOWED_MEDIA_HOSTS", "").split(",")}):
        return False
    try:
        cost = quote(10)["provider_cost_with_risk_buffer_usd"] * pricing.USD_TO_NGN
        revenue = 10 * CREDITS_PER_SECOND * (
            pricing.CREDIT_VALUE_NGN * (1 - pricing.PAYMENT_FEE_ALLOWANCE) - pricing.FIXED_FEE_PER_CREDIT_NGN)
        return 0 < cost <= revenue * .8
    except (ArithmeticError, ValueError):
        return False


class Submit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video_id: uuid.UUID
    audio_id: uuid.UUID
    duration: Literal[6, 8, 10]
    credit_limit: int = Field(ge=1, strict=True)


@router.get("/options")
async def options(request: Request, owner: str = Depends(get_authenticated_user_id)):
    await enforce_rate_limit(request, bucket="lip-sync-options", limit=20, window_seconds=60)
    if not enabled():
        return {"available": False}
    videos = await database.fetch_all(
        """SELECT id::text, left(request->>'prompt',100) AS title, request->'duration' AS duration
           FROM generations WHERE user_id=%s AND status='succeeded' AND result->>'object_key' IS NOT NULL
             AND generation_type IN ('text-to-video','image-to-video','audio-to-video','lip-sync')
           ORDER BY updated_at DESC, id DESC LIMIT 50""", (owner,))
    voices = await database.fetch_all(
        """SELECT a.id::text, left(g.request->>'prompt',100) AS title,
                  a.duration_seconds::float8 AS duration
           FROM speech_assets a LEFT JOIN generations g ON g.id=a.id AND g.user_id=a.user_id
           WHERE a.user_id=%s ORDER BY a.created_at DESC, a.id DESC LIMIT 50""", (owner,))
    return {"available": True, "videos": videos, "voices": voices,
            "credits_per_second": CREDITS_PER_SECOND}


@router.post("/generations", status_code=202)
async def submit(body: Submit, request: Request, key: str = Header(alias="Idempotency-Key", max_length=200),
                 owner: str = Depends(get_authenticated_user_id)):
    if not enabled():
        raise HTTPException(503, "Lip-sync is unavailable.")
    await enforce_rate_limit(request, bucket="lip-sync-create", limit=5, window_seconds=60)
    try:
        return await generation_service.create_reserved_generation(
            owner, key, prompt="Lip-sync video", provider="magic-hour", model=MODEL,
            resolution="source", duration_seconds=body.duration, generation_type="lip-sync",
            source_video_id=str(body.video_id), source_audio_id=str(body.audio_id), credit_limit=Decimal(body.credit_limit))
    except generation_service.InsufficientCredits as error:
        raise HTTPException(402, str(error)) from None
    except (generation_service.GenerationRequestConflict, generation_service.GenerationPriceChanged) as error:
        raise HTTPException(409, str(error)) from None
    except LookupError:
        raise HTTPException(404, "The saved video or voice track was not found.") from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def read_private(key, limit):
    client = media_storage._client()
    item = client.get_object(Bucket=media_storage._storage_config()["bucket"], Key=key)
    stream = item["Body"]
    try:
        if not 0 < item["ContentLength"] <= limit:
            raise ValueError("Source file exceeds the upload limit.")
        data = stream.read(limit + 1)
        if not 0 < len(data) <= limit:
            raise ValueError("Source file exceeds the upload limit.")
        return data
    finally:
        stream.close()


async def upload_assets(client, headers, video, audio):
    # The upload endpoint returns one scoped PUT URL per requested file.
    response = await client.post(magic_hour_video.BASE + "/v1/files/upload-urls", headers=headers,
        json={"items": [{"type": "video", "extension": "mp4"}, {"type": "audio", "extension": "wav"}]})
    response.raise_for_status()
    items = response.json()["items"]
    if not isinstance(items, list) or len(items) != 2:
        raise ValueError("Provider upload response invalid.")
    paths = []
    for item, data, kind in zip(items, (video, audio), ("video/mp4", "audio/wav"), strict=True):
        url = urlsplit(item["upload_url"])
        path = item["file_path"]
        if (url.scheme != "https" or url.netloc != "videos.magichour.ai"
                or not isinstance(path, str) or not re.fullmatch(r"api-assets/[A-Za-z0-9_./-]{1,240}", path)
                or ".." in path or url.path != "/" + path):
            raise ValueError("Provider upload destination invalid.")
        uploaded = await client.put(url.geturl(), content=data, headers={"Content-Type": kind})
        uploaded.raise_for_status()
        paths.append(path)
    return paths


async def dispatch(generation):
    owner, job = generation["user_id"], generation["id"]
    req = magic_hour_video.request_data(generation)
    video = await database.fetch_one(
        """SELECT result->>'object_key' AS object_key FROM generations
           WHERE id=%s AND user_id=%s AND status='succeeded'""", (req["source_video_id"], owner))
    audio = await database.fetch_one(
        "SELECT object_key FROM speech_assets WHERE id=%s AND user_id=%s", (req["source_audio_id"], owner))
    if not video or not video["object_key"] or not audio:
        return await magic_hour_video.fail(generation, "lip_sync_source_missing")
    key = os.getenv("MAGIC_HOUR_API_KEY", "").strip()
    if not key:
        return await magic_hour_video.fail(generation, "lip_sync_provider_unavailable")
    headers = {"Authorization": "Bearer " + key}
    async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
        try:
            source_video = await asyncio.to_thread(read_private, video["object_key"], 32 * 1024 * 1024)
            source_audio = await asyncio.to_thread(read_private, audio["object_key"], 8 * 1024 * 1024)
            video_path, audio_path = await upload_assets(client, headers, source_video, source_audio)
        except Exception:
            return await magic_hour_video.fail(generation, "lip_sync_upload_failed")
        await generation_store.begin_submission(owner, job)
        try:
            response = await client.post(magic_hour_video.BASE + "/v1/lip-sync", headers=headers,
                json={"name":"NENE AI lip-sync " + job, "start_seconds":0, "end_seconds":req["duration"],
                      "max_fps_limit":FPS, "style":{"generation_mode":"standard"},
                      "assets":{"video_source":"file","video_file_path":video_path,"audio_file_path":audio_path}})
        except httpx.HTTPError:
            return await magic_hour_video.unknown(owner, job)
    if response.status_code >= 500 or response.status_code in {408, 409} or response.is_redirect:
        return await magic_hour_video.unknown(owner, job)
    if not response.is_success:
        return await magic_hour_video.fail(generation, "lip_sync_provider_rejected")
    try:
        identifier = response.json()["id"]
    except (ValueError, TypeError, KeyError):
        return await magic_hour_video.unknown(owner, job)
    if not isinstance(identifier, str) or not magic_hour_video.JOB_ID.fullmatch(identifier):
        return await magic_hour_video.unknown(owner, job)
    await generation_store.attach_provider_job(owner, job, "magic-hour", identifier)
    await generation_outbox.enqueue_generation_event(job, "reconcile_generation", {})
