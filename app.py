import os
import asyncio
import json
import base64
import uuid
import mimetypes
import logging
import subprocess
import tempfile
import shutil
import socket
import ipaddress
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from database import (database_configured, start_database, close_database, initialize_schema, get_idempotency as db_get_idempotency, create_idempotency as db_create_idempotency, complete_idempotency as db_complete_idempotency, delete_idempotency as db_delete_idempotency)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nene-ai")
app = FastAPI(title="NENE AI Backend", version="0.9.0-postgres-foundation")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

LTX_API_KEY = os.getenv("LTX_API_KEY", "").strip()
PIXAZO_API_KEY = os.getenv("PIXAZO_API_KEY", "").strip()
MAGIC_HOUR_API_KEY = os.getenv("MAGIC_HOUR_API_KEY", "").strip()
MAGIC_HOUR_API_BASE_URL = os.getenv("MAGIC_HOUR_API_BASE_URL", "https://api.magichour.ai").rstrip("/")
LTX_API_BASE_URL = os.getenv("LTX_API_BASE_URL", "https://api.ltx.io").rstrip("/")
PIXAZO_API_BASE_URL = "https://gateway.pixazo.ai"
TEMP_DIR = Path("/tmp/nene_images")
TEMP_DIR.mkdir(parents=True, exist_ok=True)
STORY_VIDEO_DIR = Path("/tmp/nene_story_videos")
STORY_VIDEO_DIR.mkdir(parents=True, exist_ok=True)

# Prototype-safe idempotency registry. Production will move this to a shared
# database/Redis layer so it survives restarts and multiple backend instances.
GENERATION_IDEMPOTENCY = {}
GENERATION_IDEMPOTENCY_LOCK = asyncio.Lock()
GENERATION_IDEMPOTENCY_MAX = 1000

@app.on_event("startup")
async def _startup_database():
    if database_configured():
        await start_database()
        await initialize_schema()
        logger.info("NENE AI PostgreSQL database initialized.")

@app.on_event("shutdown")
async def _shutdown_database():
    await close_database()

def _trim_idempotency_registry():
    if len(GENERATION_IDEMPOTENCY) <= GENERATION_IDEMPOTENCY_MAX:
        return
    items = sorted(
        GENERATION_IDEMPOTENCY.items(),
        key=lambda item: item[1].get("created_at", 0),
        reverse=True,
    )[:GENERATION_IDEMPOTENCY_MAX]
    GENERATION_IDEMPOTENCY.clear()
    GENERATION_IDEMPOTENCY.update(items)

class GenerateRequest(BaseModel):
    type: str = Field(default="text-to-video")
    prompt: str
    model: str = "ltx"
    resolution: str = "720p"
    duration: Any = 6
    aspect_ratio: Optional[str] = None
    camera_motion: Optional[str] = None
    image_url: Optional[str] = None
    image_uri: Optional[str] = None
    audio_url: Optional[str] = None
    provider: str = "auto"

class ImageUploadRequest(BaseModel):
    data_uri: str


def pixazo_headers():
    return {"Content-Type": "application/json", "Ocp-Apim-Subscription-Key": PIXAZO_API_KEY}

def ltx_headers():
    return {"Authorization": f"Bearer {LTX_API_KEY}", "Content-Type": "application/json"}

def duration_value(value: Any) -> int:
    try: n = int(float(value))
    except Exception: n = 6
    return min([6, 8, 10], key=lambda x: abs(x-n))

def resolution_value(value: str) -> str:
    return "1080p" if "1080" in (value or "").lower() else "720p"

@app.get("/api/health")
def health():
    configured = []
    if PIXAZO_API_KEY:
        configured.append("pixazo")
    if MAGIC_HOUR_API_KEY:
        configured.append("magic-hour")
    if LTX_API_KEY:
        configured.append("ltx")
    return {
        "ok": True,
        "service": "nene-ai-backend",
        "version": "0.9.0-postgres-foundation",
        "database_configured": database_configured(),
        "provider": configured[0] if configured else "none",
        "providers": configured,
        "pixazo_configured": bool(PIXAZO_API_KEY),
        "magic_hour_configured": bool(MAGIC_HOUR_API_KEY),
        "ltx_configured": bool(LTX_API_KEY),
    }

@app.get("/api/provider-info")
def provider_info():
    return {
        "default_strategy": "auto",
        "providers": {
            "pixazo": {"configured": bool(PIXAZO_API_KEY), "base_url": PIXAZO_API_BASE_URL},
            "magic-hour": {"configured": bool(MAGIC_HOUR_API_KEY), "base_url": MAGIC_HOUR_API_BASE_URL},
            "ltx": {"configured": bool(LTX_API_KEY), "base_url": LTX_API_BASE_URL},
        },
        "selection_order": ["pixazo", "magic-hour", "ltx"],
        "note": "Auto routing does not retry an accepted generation on another provider.",
    }

@app.post("/api/image-upload")
async def image_upload(req: ImageUploadRequest):
    _cleanup_story_storage()
    """Accept supported browser image data URIs and normalize the file type safely.

    Some mobile browsers/file pickers can report an unusual MIME label even when the
    underlying bytes are a normal JPEG/PNG/WEBP. We therefore verify the actual file
    signature after decoding instead of trusting the MIME label alone.
    """
    if not req.data_uri.startswith("data:image/") or ";base64," not in req.data_uri:
        raise HTTPException(status_code=400, detail="Expected a base64 image data URI.")

    header, encoded = req.data_uri.split(",", 1)
    declared_mime = header.split(";", 1)[0][5:].lower().strip()
    logger.info("Image upload received: declared_mime=%s encoded_chars=%s", declared_mime, len(encoded))

    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image.")

    if len(raw) > 7 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image is too large.")
    if not raw:
        raise HTTPException(status_code=400, detail="Image data is empty.")

    # Detect the real file type from magic bytes. This handles mobile/browser MIME
    # mismatches such as image/jpg or image/pjpeg without weakening the file-type rule.
    if raw.startswith(b"\xff\xd8\xff"):
        detected_mime, ext = "image/jpeg", "jpg"
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_mime, ext = "image/png", "png"
    elif len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        detected_mime, ext = "image/webp", "webp"
    else:
        logger.warning("Image upload rejected: unsupported bytes declared_mime=%s", declared_mime)
        raise HTTPException(status_code=400, detail="Only PNG, JPEG and WEBP images are supported.")

    logger.info("Image upload accepted: declared_mime=%s detected_mime=%s bytes=%s", declared_mime, detected_mime, len(raw))
    name = f"{uuid.uuid4().hex}.{ext}"
    path = TEMP_DIR / name
    path.write_bytes(raw)
    # This public URL is fetched by Pixazo immediately after this request.
    return {
        "ok": True,
        "image_url": f"https://nene-ai.onrender.com/api/temp-images/{name}",
        "mime_type": detected_mime,
        "extension": ext,
    }

@app.get("/api/temp-images/{name}")
async def temp_image(name: str):
    safe = Path(name).name
    path = TEMP_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="Temporary image not found.")
    mime, _ = mimetypes.guess_type(str(path))
    return FileResponse(path, media_type=mime or "application/octet-stream", headers={"Cache-Control":"public, max-age=300"})

def magic_hour_headers():
    return {"Authorization": f"Bearer {MAGIC_HOUR_API_KEY}", "Content-Type": "application/json", "Accept": "application/json"}


def magic_hour_model(req: GenerateRequest) -> str:
    # Normalize NENE AI's internal model aliases to a model ID that Magic Hour
    # actually accepts. In particular, the frontend may send ltx-2-5-fast for
    # Auto/LTX, but Magic Hour expects the dotted ID ltx-2.5.
    model = (req.model or "").strip().lower()
    aliases = {
        "": "ltx-2.5",
        "ltx": "ltx-2.5",
        "ltx-2": "ltx-2.5",
        "ltx-2-5": "ltx-2.5",
        "ltx-2-5-fast": "ltx-2.5",
        "ltx-2.5-fast": "ltx-2.5",
        "ltx-2.5": "ltx-2.5",
    }
    return aliases.get(model, model)


def magic_hour_duration(value: Any) -> int:
    try:
        n = int(float(value))
    except Exception:
        n = 6
    return max(1, min(60, n))


def magic_hour_resolution(value: str) -> str:
    value = (value or "").lower()
    if "1080" in value:
        return "1080p"
    if "480" in value:
        return "480p"
    return "720p"


async def magic_hour_upload_image(image_url: str) -> str:
    if not image_url or not image_url.startswith("http"):
        raise HTTPException(status_code=400, detail="Magic Hour image-to-video requires an image URL.")

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        source = await client.get(image_url)
        if source.status_code >= 400:
            raise HTTPException(status_code=400, detail="Magic Hour could not download the source image.")
        raw = source.content
        content_type = (source.headers.get("content-type") or "").lower()
        extension = "jpg"
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = "png"
        elif len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            extension = "webp"

        upload_res = await client.post(
            f"{MAGIC_HOUR_API_BASE_URL}/v1/files/upload-urls",
            headers=magic_hour_headers(),
            json={"items": [{"type": "image", "extension": extension}]},
        )
        if upload_res.status_code >= 400:
            raise HTTPException(status_code=upload_res.status_code, detail=upload_res.text)
        data = upload_res.json()
        item = data.get("items", [])[0]
        upload_url = item["upload_url"]
        file_path = item["file_path"]
        put_res = await client.put(
            upload_url,
            content=raw,
            headers={"Content-Type": content_type or f"image/{extension}"},
        )
        if put_res.status_code >= 400:
            raise HTTPException(status_code=put_res.status_code, detail=put_res.text)
        return file_path


async def magic_hour_generate(req: GenerateRequest, kind: str):
    if not MAGIC_HOUR_API_KEY:
        raise HTTPException(status_code=503, detail="MAGIC_HOUR_API_KEY is not configured.")

    aspect = (req.aspect_ratio or "16:9").strip()
    if aspect not in {"16:9", "9:16", "1:1"}:
        aspect = "16:9"

    payload = {
        "end_seconds": magic_hour_duration(req.duration),
        "aspect_ratio": aspect,
        "resolution": magic_hour_resolution(req.resolution),
        "model": magic_hour_model(req),
        "audio": False,
        "style": {"prompt": req.prompt.strip()},
        "name": "NENE AI generation",
    }

    if kind == "image-to-video":
        image = req.image_url or req.image_uri
        file_path = await magic_hour_upload_image(image)
        payload["assets"] = {"image_file_path": file_path}
        endpoint = f"{MAGIC_HOUR_API_BASE_URL}/v1/image-to-video"
    else:
        endpoint = f"{MAGIC_HOUR_API_BASE_URL}/v1/text-to-video"

    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(endpoint, headers=magic_hour_headers(), json=payload)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    data = response.json()
    job_id = data.get("id")
    if not job_id:
        raise HTTPException(status_code=502, detail=f"Magic Hour returned no project id: {data}")
    return {"ok": True, "job_id": job_id, "provider": "magic-hour", "type": kind, "raw": data}


async def pixazo_generate(req: GenerateRequest, kind: str):
    if not PIXAZO_API_KEY:
        raise HTTPException(status_code=503, detail="PIXAZO_API_KEY is not configured.")
    if kind == "text-to-video":
        endpoint = f"{PIXAZO_API_BASE_URL}/ltx-video/v1/text-to-video"
        payload = {"prompt": req.prompt}
    else:
        endpoint = f"{PIXAZO_API_BASE_URL}/ltx-video/v1/image-to-video"
        image = req.image_url or req.image_uri
        if not image or not image.startswith("http"):
            raise HTTPException(status_code=400, detail="Image-to-video requires a public image URL.")
        # Keep the user's request, but add a short, deterministic identity guard.
        # This does not replace the user's motion instruction; it tells the model
        # what must remain unchanged while that motion is applied.
        identity_guard = (
            "Preserve the exact identity and appearance of the person in the source image. "
            "Keep the same face, facial features, hair, skin tone, clothing, body proportions, "
            "and overall appearance. Apply only the requested motion. "
            f"User motion instruction: {req.prompt.strip()}"
        )
        payload = {"prompt": identity_guard, "image_url": image}
    # The current Pixazo LTX 2.5 FREE endpoint does not use the Pro/Lite
    # duration/resolution fields. It exposes quality-relevant controls such as
    # strength, negative, aspect, frame_rate, steps and cfg instead.
    # Keep the existing frontend contract, but translate it safely here.
    if kind == "image-to-video":
        payload["strength"] = 1.0
        payload["negative"] = (
            "blurry, soft focus, low detail, low quality, distorted, worst quality, "
            "jpeg artifacts, compression artifacts, camera shake, shaky camera, jitter, "
            "judder, flicker, unstable motion, temporal flicker, motion smear, excessive motion blur, "
            "face distortion, identity drift, different person, changed facial features, "
            "warped face, deformed hands, warped body, duplicated features, unnatural movement"
        )
        # Fixed seed for this controlled quality experiment so we can compare
        # changes without introducing an additional random variable. This should
        # be removed/randomized for normal production generations later.
        payload["seed"] = 42

    # Preserve the user's requested shape while staying within the free API's
    # supported pixel budget. The existing 1080p UI maps to 1920x1080 and the
    # existing 720p option maps to 1280x704 (the documented free endpoint default).
    requested = (req.resolution or "").lower()
    if "3840" in requested or "2160" in requested:
        width, height = 1920, 1080
    elif "1280" in requested or "720" in requested:
        width, height = 1280, 704
    else:
        width, height = 1920, 1080

    aspect = (req.aspect_ratio or "").strip()
    if aspect in {"16:9", "9:16", "1:1", "21:9", "4:3", "3:4", "3:2", "2:3", "4:5"}:
        payload["aspect"] = aspect
    else:
        payload["width"] = width
        payload["height"] = height

    # Keep 24 fps so we do not confuse playback smoothness with generation
    # quality. Use a modest step increase for this controlled test; Pixazo
    # documents 8 as the tuned default and notes that higher values are slower
    # with diminishing returns, so we deliberately test only 12 here.
    payload["frame_rate"] = 24
    payload["steps"] = 12
    payload["cfg"] = 3.0
    async with httpx.AsyncClient(timeout=90) as client:
        try: r = await client.post(endpoint, headers=pixazo_headers(), json=payload)
        except httpx.HTTPError as exc: raise HTTPException(status_code=502, detail=f"Pixazo connection error: {exc}")
    if r.status_code >= 400:
        logger.error("Pixazo GENERATE HTTP %s: %s", r.status_code, r.text[:4000])
        raise HTTPException(status_code=r.status_code, detail=r.text)
    try:
        data = r.json()
    except Exception:
        logger.error("Pixazo GENERATE returned non-JSON: %s", r.text[:4000])
        raise HTTPException(status_code=502, detail="Pixazo returned an invalid JSON response.")
    job_id = data.get("request_id") or data.get("id") or data.get("job_id")
    logger.info("Pixazo GENERATE accepted: job_id=%s status=%s model=%s", job_id, data.get("status"), data.get("model_id"))
    if not job_id: raise HTTPException(status_code=502, detail=f"Pixazo returned no request id: {data}")
    return {"ok": True, "job_id": job_id, "provider": "pixazo", "type": kind, "raw": data}

async def ltx_generate(req: GenerateRequest, kind: str):
    if not LTX_API_KEY: raise HTTPException(status_code=503, detail="LTX_API_KEY is not configured.")
    endpoint = f"{LTX_API_BASE_URL}/v2/{kind}"
    payload = {"prompt": req.prompt, "model": req.model or "ltx-2-5-fast", "resolution": req.resolution or "1280x720", "duration": req.duration}
    if req.aspect_ratio: payload["aspect_ratio"] = req.aspect_ratio
    if req.camera_motion: payload["camera_motion"] = req.camera_motion
    if kind == "image-to-video":
        image = req.image_uri or req.image_url
        if not image: raise HTTPException(status_code=400, detail="Image-to-video requires an image.")
        payload["image_uri"] = image
    async with httpx.AsyncClient(timeout=90) as client:
        try: r = await client.post(endpoint, headers=ltx_headers(), json=payload)
        except httpx.HTTPError as exc: raise HTTPException(status_code=502, detail=f"LTX connection error: {exc}")
    if r.status_code >= 400: raise HTTPException(status_code=r.status_code, detail=r.text)
    data=r.json(); job_id=data.get("id") or data.get("job_id")
    if not job_id: raise HTTPException(status_code=502, detail=f"LTX returned no job id: {data}")
    return {"ok":True,"job_id":job_id,"provider":"ltx","type":kind,"raw":data}


class StoryAssembleRequest(BaseModel):
    scene_urls: list[str]
    title: str = "NENE AI Story"


def _safe_story_filename(name: str) -> str:
    base = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (name or "story").strip())
    return (base[:60] or "story")


def _cleanup_story_storage(max_age_seconds: int = 6 * 3600):
    """Best-effort cleanup for prototype files so /tmp cannot grow forever."""
    cutoff = __import__("time").time() - max_age_seconds
    for folder in (TEMP_DIR, STORY_VIDEO_DIR):
        try:
            for path in folder.iterdir():
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            continue


def _validate_public_media_url(url: str):
    """Reject local/private targets before the server downloads user-supplied media URLs."""
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Media URL must be a valid HTTP(S) URL.")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise HTTPException(status_code=400, detail="Private/local media URLs are not allowed.")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError:
        raise HTTPException(status_code=400, detail="Media host could not be resolved.")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise HTTPException(status_code=400, detail="Private/local media URLs are not allowed.")


async def _download_story_clip(client: httpx.AsyncClient, url: str, destination: Path, max_bytes: int = 80 * 1024 * 1024):
    if not url:
        raise HTTPException(status_code=400, detail="Story scene URLs must be public HTTP(S) URLs.")
    _validate_public_media_url(url)
    try:
        async with client.stream("GET", url, follow_redirects=True) as response:
            if response.status_code >= 400:
                raise HTTPException(status_code=400, detail=f"Could not download story scene: HTTP {response.status_code}.")
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise HTTPException(status_code=413, detail="A story scene is too large to assemble in the prototype.")
                except ValueError:
                    pass
            total = 0
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        handle.close()
                        destination.unlink(missing_ok=True)
                        raise HTTPException(status_code=413, detail="A story scene is too large to assemble in the prototype.")
                    handle.write(chunk)
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not download story scene: {exc}")


def _run_ffmpeg(args: list[str], timeout: int = 300):
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="FFmpeg is not available on the NENE AI video-processing runtime.")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Story assembly timed out. The scenes are still available individually.")
    if result.returncode != 0:
        logger.error("FFmpeg failed: %s", result.stderr[-4000:])
        raise HTTPException(status_code=502, detail="NENE AI could not assemble these scene videos. The individual scenes remain available.")
    return result


@app.post("/api/story/assemble")
async def assemble_story(req: StoryAssembleRequest):
    _cleanup_story_storage()
    urls = [str(url).strip() for url in (req.scene_urls or []) if str(url).strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="At least one completed story scene is required.")
    if len(urls) > 20:
        raise HTTPException(status_code=400, detail="A prototype story can contain at most 20 scenes.")
    if len(set(urls)) != len(urls):
        raise HTTPException(status_code=400, detail="Duplicate scene URLs were supplied. Refusing to assemble duplicate clips.")

    work_dir = Path(tempfile.mkdtemp(prefix="nene_story_"))
    normalized = []
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            for index, url in enumerate(urls, start=1):
                source = work_dir / f"source_{index:02d}.mp4"
                normalized_file = work_dir / f"scene_{index:02d}.mp4"
                await _download_story_clip(client, url, source)
                # Normalize every clip so different provider encoders/resolutions do
                # not make the concat step fail. Keep the user's scene order.
                # Story scenes currently come from providers with audio disabled.
                # Normalize them to a video-only stream so every clip has identical
                # stream layout and FFmpeg concat remains deterministic.
                _run_ffmpeg([
                    "-i", str(source),
                    "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                    "-r", "30",
                    "-an",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "22",
                    "-movflags", "+faststart",
                    str(normalized_file),
                ], timeout=180)
                normalized.append(normalized_file)

        concat_file = work_dir / "concat.txt"
        concat_file.write_text(
            "".join("file '" + str(path).replace("'", "'\\''") + "'\n" for path in normalized),
            encoding="utf-8",
        )
        output_name = f"{uuid.uuid4().hex}_{_safe_story_filename(req.title)}.mp4"
        output_path = STORY_VIDEO_DIR / output_name
        _run_ffmpeg([
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ], timeout=300)
        return {
            "ok": True,
            "video_url": f"https://nene-ai.onrender.com/api/story-videos/{output_name}",
            "scene_count": len(urls),
            "title": req.title,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.get("/api/story-videos/{name}")
async def story_video(name: str):
    _cleanup_story_storage()
    safe = Path(name).name
    path = STORY_VIDEO_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="Story video not found. Prototype storage is temporary.")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "public, max-age=3600"})


async def _generate_uncached(req: GenerateRequest, kind: str):
    requested = (req.provider or "auto").lower().strip()
    if requested in {"pixazo", "pixazo-ltx"}:
        return await pixazo_generate(req, kind)
    if requested in {"magic-hour", "magichour", "magic_hour"}:
        return await magic_hour_generate(req, kind)
    if requested in {"ltx", "ltx-direct"}:
        return await ltx_generate(req, kind)
    if requested not in {"auto", "best", ""}:
        raise HTTPException(status_code=400, detail="Unknown provider. Use auto, pixazo, magic-hour, or ltx.")

    if PIXAZO_API_KEY:
        try:
            return await pixazo_generate(req, kind)
        except HTTPException as exc:
            detail = str(exc.detail or "")
            if exc.status_code < 500 and "card_required" not in detail.lower():
                raise
            if "card_required" in detail.lower() and MAGIC_HOUR_API_KEY:
                logger.warning("Pixazo rejected Auto request because this account requires a card; falling back to Magic Hour.")
                fallback_req = GenerateRequest(
                    type=req.type, prompt=req.prompt, model=req.model,
                    resolution="480p", duration=3,
                    aspect_ratio=req.aspect_ratio, camera_motion=req.camera_motion,
                    image_url=req.image_url, image_uri=req.image_uri,
                    audio_url=req.audio_url, provider="magic-hour",
                )
                return await magic_hour_generate(fallback_req, kind)
            raise
    if MAGIC_HOUR_API_KEY:
        return await magic_hour_generate(req, kind)
    if LTX_API_KEY:
        return await ltx_generate(req, kind)
    raise HTTPException(status_code=503, detail="No video provider is configured.")


@app.post("/api/generate")
async def generate(req: GenerateRequest, idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key")):
    kind = req.type.lower().strip()
    if kind not in {"text-to-video", "image-to-video"}:
        raise HTTPException(status_code=400, detail="Supported types: text-to-video, image-to-video")

    key = (idempotency_key or "").strip()
    if not key:
        return await _generate_uncached(req, kind)
    if len(key) > 200:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long.")

    request_fingerprint = json.dumps(req.model_dump(), sort_keys=True, default=str)

    # Same-process joining is kept in memory. PostgreSQL makes completed
    # idempotency durable across Render restarts and backend instances.
    if database_configured():
        try:
            durable = await db_get_idempotency(key)
        except Exception as exc:
            logger.exception("Database idempotency lookup failed.")
            raise HTTPException(status_code=503, detail=f"NENE AI database is unavailable: {exc}")
        if durable:
            if durable.get("request_fingerprint") != request_fingerprint:
                raise HTTPException(status_code=409, detail="This Idempotency-Key was already used for a different generation request.")
            if durable.get("response") is not None:
                return durable["response"]
            raise HTTPException(status_code=409, detail="This generation is already in progress. Do not submit it again.")
        try:
            inserted = await db_create_idempotency(key, request_fingerprint, str(uuid.uuid4()))
        except Exception as exc:
            logger.exception("Database idempotency reservation failed.")
            raise HTTPException(status_code=503, detail=f"NENE AI database is unavailable: {exc}")
        if not inserted:
            durable = await db_get_idempotency(key)
            if durable and durable.get("request_fingerprint") != request_fingerprint:
                raise HTTPException(status_code=409, detail="This Idempotency-Key was already used for a different generation request.")
            if durable and durable.get("response") is not None:
                return durable["response"]
            raise HTTPException(status_code=409, detail="This generation is already in progress. Do not submit it again.")

    async with GENERATION_IDEMPOTENCY_LOCK:
        existing = GENERATION_IDEMPOTENCY.get(key)
        if existing:
            if existing.get("fingerprint") != request_fingerprint:
                raise HTTPException(status_code=409, detail="This Idempotency-Key was already used for a different generation request.")
            task = existing.get("task")
            if task is None:
                return existing["response"]
        else:
            task = asyncio.create_task(_generate_uncached(req, kind))
            GENERATION_IDEMPOTENCY[key] = {
                "fingerprint": request_fingerprint,
                "task": task,
                "created_at": asyncio.get_running_loop().time(),
            }
            _trim_idempotency_registry()

    try:
        response = await task
    except Exception:
        async with GENERATION_IDEMPOTENCY_LOCK:
            current = GENERATION_IDEMPOTENCY.get(key)
            if current and current.get("task") is task:
                GENERATION_IDEMPOTENCY.pop(key, None)
        if database_configured():
            try:
                await db_delete_idempotency(key)
            except Exception:
                logger.exception("Failed to clear durable idempotency reservation after generation failure.")
        raise

    async with GENERATION_IDEMPOTENCY_LOCK:
        current = GENERATION_IDEMPOTENCY.get(key)
        if current and current.get("task") is task:
            current["response"] = response
            current["task"] = None
            current["created_at"] = asyncio.get_running_loop().time()

    if database_configured():
        try:
            await db_complete_idempotency(key, response)
        except Exception:
            # Provider work is already accepted; never turn a successful
            # generation into an apparent failure that could cause a retry.
            logger.exception("Generation completed but durable idempotency could not be saved.")
    return response

@app.get("/api/jobs/{job_id}")
async def job(job_id: str, mode: str = "text-to-video", provider: str = ""):
    kind = mode if mode in {"text-to-video", "image-to-video"} else "text-to-video"
    active = (provider or "").lower().strip()

    # The frontend sends the provider returned by /api/generate. This is important
    # for Auto, because Auto may fall back from Pixazo to Magic Hour before a job
    # is accepted.
    if not active:
        if MAGIC_HOUR_API_KEY:
            active = "magic-hour"
        elif PIXAZO_API_KEY:
            active = "pixazo"
        elif LTX_API_KEY:
            active = "ltx"

    if active in {"magic-hour", "magichour", "magic_hour"}:
        if not MAGIC_HOUR_API_KEY:
            raise HTTPException(status_code=503, detail="MAGIC_HOUR_API_KEY is not configured.")
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.get(
                    f"{MAGIC_HOUR_API_BASE_URL}/v1/video-projects/{job_id}",
                    headers=magic_hour_headers(),
                )
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"Magic Hour connection error: {exc}")
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        data = response.json()
        status = str(data.get("status") or "").lower()
        downloads = data.get("downloads") or []
        data["_nene_status"] = status
        if status in {"complete", "completed"} and downloads:
            first = downloads[0]
            video_url = first.get("url") if isinstance(first, dict) else (first if isinstance(first, str) else None)
            if video_url:
                data["_nene_video_url"] = video_url
                data["video_url"] = video_url
        return data

    if active in {"pixazo", "pixazo-ltx"}:
        if not PIXAZO_API_KEY:
            raise HTTPException(status_code=503, detail="PIXAZO_API_KEY is not configured.")
        endpoint = f"{PIXAZO_API_BASE_URL}/v2/requests/status/{job_id}"
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                r = await client.get(endpoint, headers={"Ocp-Apim-Subscription-Key": PIXAZO_API_KEY})
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"Pixazo connection error: {exc}")
        if r.status_code >= 400:
            logger.error("Pixazo STATUS HTTP %s job=%s: %s", r.status_code, job_id, r.text[:4000])
            raise HTTPException(status_code=r.status_code, detail=r.text)
        try:
            data = r.json()
        except Exception:
            raise HTTPException(status_code=502, detail="Pixazo returned an invalid status response.")
        status = str(data.get("status") or "").upper()
        error = data.get("error")
        output = data.get("output") or {}
        media = output.get("media_url") if isinstance(output, dict) else None
        data["_nene_status"] = status
        data["_nene_error"] = error
        if isinstance(media, list) and media:
            data["_nene_video_url"] = media[0]
            data["video_url"] = media[0]
        elif isinstance(media, str):
            data["_nene_video_url"] = media
            data["video_url"] = media
        return data

    if active in {"ltx", "ltx-direct"}:
        if not LTX_API_KEY:
            raise HTTPException(status_code=503, detail="LTX_API_KEY is not configured.")
        endpoint = f"{LTX_API_BASE_URL}/v2/{kind}/{job_id}"
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                r = await client.get(endpoint, headers={"Authorization": f"Bearer {LTX_API_KEY}"})
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"LTX connection error: {exc}")
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        try:
            data = r.json()
        except Exception:
            raise HTTPException(status_code=502, detail="LTX returned an invalid status response.")
        status = str(data.get("status") or "").lower()
        data["_nene_status"] = status
        result = data.get("result") or {}
        if isinstance(result, dict) and result.get("video_url"):
            data["_nene_video_url"] = result.get("video_url")
            data["video_url"] = result.get("video_url")
        return data

    raise HTTPException(status_code=400, detail="Unknown provider. Use pixazo, magic-hour, or ltx.")

