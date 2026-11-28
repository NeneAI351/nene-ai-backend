"""One six-second provider check using a public-domain fixture, without wallet access."""
import asyncio
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

import httpx

import lip_sync
import magic_hour_video
import media_storage
import speech_assets
from rate_limits import _redis_client

LOG = logging.getLogger("nene-ai")
CHECK = "lip-sync-six-seconds-v1"
MAX_CREDITS = 144
FIXTURE = "https://upload.wikimedia.org/wikipedia/commons/6/6f/Anonymous_speaking_faka%27uvea.webm"


def split_fixture(data):
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "fixture.webm"
        video = Path(folder) / "video.mp4"
        audio = Path(folder) / "audio.wav"
        source.write_bytes(data)
        ffmpeg = speech_assets.ffmpeg_executable()
        if not ffmpeg:
            raise RuntimeError("ffmpeg_unavailable")
        for offset, options, output in [
            ("3", ["-an", "-vf", "fps=24", "-c:v", "libx264", "-pix_fmt", "yuv420p"], video),
            ("3.35", ["-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le"], audio),
        ]:
            subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-ss", offset, "-i", str(source),
                            "-t", "6", *options, str(output)], check=True, timeout=35, capture_output=True)
        return video.read_bytes(), audio.read_bytes()


async def run_once():
    if (os.getenv("NENE_LIP_SYNC_PROVIDER_CHECK") != CHECK
            or os.getenv("RENDER_SERVICE_ID") != "srv-daffnvdg1s2s73eclr9g"
            or os.getenv("NENE_LIP_SYNC_ENABLED") == "true"):
        return
    phase = "configuration"
    try:
        key = os.getenv("MAGIC_HOUR_API_KEY", "").strip()
        redis_url = os.getenv("REDIS_URL", "").strip()
        if not key or not redis_url.startswith("rediss://"):
            raise RuntimeError("provider_check_not_configured")
        headers = {"Authorization": "Bearer " + key}
        async with httpx.AsyncClient(timeout=35, follow_redirects=False, trust_env=False) as client:
            phase = "provider balance"
            response = await client.get(magic_hour_video.BASE + "/v1/account", headers=headers)
            response.raise_for_status()
            credits = response.json().get("credits")
            if not isinstance(credits, int) or credits < MAX_CREDITS:
                LOG.warning("Lip-sync check stopped before submission: API credits=%s, required=%s",
                            credits if isinstance(credits, int) else "unknown", MAX_CREDITS)
                return
            LOG.info("Lip-sync check API balance confirmed: credits=%s", credits)
            phase = "fixture"
            async with client.stream("GET", FIXTURE, headers={"User-Agent":"NeneAI-LipSyncVerification/1.0"}) as source:
                source.raise_for_status()
                buffer = bytearray()
                async for chunk in source.aiter_bytes():
                    if len(buffer) + len(chunk) > 12 * 1024 * 1024:
                        raise ValueError("fixture_size_exceeded")
                    buffer.extend(chunk)
            video, audio = await asyncio.to_thread(split_fixture, bytes(buffer))
            phase = "upload"
            video_path, audio_path = await lip_sync.upload_assets(client, headers, video, audio)
            phase = "paid submission"
            if not await _redis_client(redis_url).set("nene:verification:" + CHECK, "claimed", nx=True):
                LOG.info("Lip-sync check already submitted or claimed")
                return
            response = await client.post(magic_hour_video.BASE + "/v1/lip-sync", headers=headers,
                json={"name":"NENE AI six-second verification","start_seconds":0,"end_seconds":6,
                      "max_fps_limit":24,"style":{"generation_mode":"standard"},
                      "assets":{"video_source":"file","video_file_path":video_path,"audio_file_path":audio_path}})
            if not response.is_success:
                LOG.warning("Lip-sync check provider declined: HTTP %s", response.status_code)
                return
            result = response.json()
            job = result.get("id")
            if not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", job):
                raise ValueError("unknown_submission_outcome")
            charged = result.get("credits_charged")
            LOG.info("Lip-sync check submitted: quoted provider credits=%s", charged if isinstance(charged, int) else "unknown")
            phase = "provider processing"
            for _ in range(24):
                response = await client.get(magic_hour_video.BASE + "/v1/video-projects/" + job, headers=headers)
                response.raise_for_status()
                result = response.json()
                state = result.get("status")
                if state == "complete":
                    break
                if state in {"error", "canceled"}:
                    LOG.warning("Lip-sync check provider finished unsuccessfully: %s", state)
                    return
                await asyncio.sleep(5)
            else:
                raise TimeoutError("provider_generation_still_processing")
            phase = "media delivery"
            url = result["downloads"][0]["url"]
            media_storage.validate_provider_media_url(url)
            async with client.stream("GET", url) as output:
                output.raise_for_status()
                async for chunk in output.aiter_bytes():
                    if b"ftyp" not in chunk[:32]:
                        raise ValueError("provider_output_not_mp4")
                    break
                else:
                    raise ValueError("provider_output_empty")
            final = result.get("credits_charged")
            LOG.info("Lip-sync check succeeded: provider credits=%s, MP4 delivery confirmed",
                     final if isinstance(final, int) else "unknown")
    except httpx.HTTPStatusError as error:
        LOG.warning("Lip-sync check stopped at %s: HTTP %s", phase, error.response.status_code)
    except Exception as error:
        LOG.warning("Lip-sync check stopped at %s (%s)", phase, type(error).__name__)
