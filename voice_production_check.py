"""One explicitly configured provider call for production voice verification."""
import asyncio
import logging
import os
import re
import uuid

import httpx

import media_storage
import speech_assets
import speech_synthesis
from rate_limits import _redis_client

logger = logging.getLogger("nene-ai")
RUN_ID = "voice-production-check-v1"
SCRIPT = "Welcome to NENE AI. Your story starts here."


def verify_private_storage(audio):
    client = media_storage._client()
    bucket = media_storage._storage_config()["bucket"]
    key = "verification/voice-" + uuid.uuid4().hex + ".wav"
    client.put_object(Bucket=bucket, Key=key, Body=audio, ContentType="audio/wav",
                      CacheControl="private, no-store")
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        stream = response["Body"]
        try:
            if stream.read(len(audio) + 1) != audio:
                raise RuntimeError("voice_storage_round_trip_failed")
        finally:
            stream.close()
    finally:
        client.delete_object(Bucket=bucket, Key=key)


async def run_once():
    if (os.getenv("NENE_VOICE_PRODUCTION_CHECK") != RUN_ID
            or os.getenv("RENDER_SERVICE_ID") != "srv-daffnvdg1s2s73eclr9g"
            or os.getenv("NENE_ENV", "").strip().lower() == "staging"
            or os.getenv("NENE_TTS_CREATE_ENABLED", "").strip().lower() == "true"):
        return
    phase = "configuration"
    try:
        redis_url = os.getenv("REDIS_URL", "").strip()
        if not redis_url.startswith("rediss://") or not speech_assets.ffmpeg_executable():
            logger.warning("Voice verification skipped: Redis TLS or audio decoder unavailable")
            return
        headers = speech_synthesis.config()
        speech_synthesis.quote(SCRIPT, speech_synthesis.VOICES[0])
        phase = "claim"
        if not await _redis_client(redis_url).set("nene:verification:" + RUN_ID, "claimed", nx=True):
            return
        phase = "provider submission"
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = await client.post("https://api.magichour.ai/v1/ai-voice-generator", headers=headers,
                json={"name": "NENE AI production voice verification",
                      "style": {"prompt": SCRIPT, "voice_name": speech_synthesis.VOICES[0]}})
            if not response.is_success:
                logger.warning("Voice verification provider rejected submission: HTTP %s", response.status_code)
                return
            payload = response.json()
            identifier = payload.get("id")
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", identifier):
                raise ValueError("voice_project_id_invalid")
            provider_credits = payload.get("credits_charged")
            if not isinstance(provider_credits, int):
                provider_credits = None
            phase = "provider polling"
            for _ in range(20):
                response = await client.get("https://api.magichour.ai/v1/audio-projects/" + identifier, headers=headers)
                response.raise_for_status()
                result = response.json()
                state = result.get("status")
                if state == "complete":
                    break
                if state not in {"queued", "rendering"}:
                    raise RuntimeError("voice_project_unsuccessful")
                await asyncio.sleep(4)
            else:
                raise TimeoutError("voice_project_still_processing")
        phase = "download"
        raw = await speech_synthesis.download_audio(result["downloads"][0]["url"])
        phase = "decode"
        audio, duration, _ = await asyncio.to_thread(speech_assets.normalize, raw, 60, .1)
        phase = "private storage"
        await asyncio.to_thread(verify_private_storage, audio)
        logger.info("Voice verification succeeded: seconds=%.2f provider_credits=%s", duration, provider_credits)
    except Exception as error:
        logger.warning("Voice verification stopped during %s (%s)", phase, type(error).__name__)
