import os
import base64
import uuid
import mimetypes
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nene-ai")
app = FastAPI(title="NENE AI Backend", version="0.7.0-multi-provider")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

LTX_API_KEY = os.getenv("LTX_API_KEY", "").strip()
PIXAZO_API_KEY = os.getenv("PIXAZO_API_KEY", "").strip()
MAGIC_HOUR_API_KEY = os.getenv("MAGIC_HOUR_API_KEY", "").strip()
MAGIC_HOUR_API_BASE_URL = os.getenv("MAGIC_HOUR_API_BASE_URL", "https://api.magichour.ai").rstrip("/")
LTX_API_BASE_URL = os.getenv("LTX_API_BASE_URL", "https://api.ltx.io").rstrip("/")
PIXAZO_API_BASE_URL = "https://gateway.pixazo.ai"
TEMP_DIR = Path("/tmp/nene_images")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

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
        "version": "0.7.0-multi-provider",
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
    model = (req.model or "").strip().lower()
    return model if model and model not in {"ltx", "ltx-2", "ltx-2.5"} else "ltx-2.5"


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

@app.post("/api/generate")
async def generate(req: GenerateRequest):
    kind = req.type.lower().strip()
    if kind not in {"text-to-video", "image-to-video"}:
        raise HTTPException(status_code=400, detail="Supported types: text-to-video, image-to-video")

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
        return await pixazo_generate(req, kind)
    if MAGIC_HOUR_API_KEY:
        return await magic_hour_generate(req, kind)
    if LTX_API_KEY:
        return await ltx_generate(req, kind)
    raise HTTPException(status_code=503, detail="No video provider is configured.")

@app.get("/api/jobs/{job_id}")
async def job(job_id: str, mode: str="text-to-video", provider: str=""):
    kind = mode if mode in {"text-to-video", "image-to-video"} else "text-to-video"
    active = (provider or ("pixazo" if PIXAZO_API_KEY else ("magic-hour" if MAGIC_HOUR_API_KEY else "ltx"))).lower().strip()

    if active in {"magic-hour", "magichour", "magic_hour"}:
        if not MAGIC_HOUR_API_KEY:
            raise HTTPException(status_code=503, detail="MAGIC_HOUR_API_KEY is not configured.")
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(
                f"{MAGIC_HOUR_API_BASE_URL}/v1/video-projects/{job_id}",
                headers=magic_hour_headers(),
            )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        data = response.json()
        status = str(data.get("status") or "").lower()
        downloads = data.get("downloads") or []
        data["_nene_status"] = status
        if status in {"complete", "completed"} and downloads:
            first = downloads[0]
            video_url = None
            if isinstance(first, dict):
                video_url = first.get("url")
            elif isinstance(first, str):
                video_url = first
            if video_url:
                data["_nene_video_url"] = video_url
                data["video_url"] = video_url
        return data

@app.get("/api/jobs/{job_id}")
async def job(job_id: str, mode: str="text-to-video", provider: str=""):
    kind=mode if mode in {"text-to-video","image-to-video"} else "text-to-video"
    active=(provider or ("pixazo" if PIXAZO_API_KEY else "ltx")).lower().strip()
    if active=="pixazo":
        if not PIXAZO_API_KEY: raise HTTPException(status_code=503, detail="PIXAZO_API_KEY is not configured.")
        endpoint=f"{PIXAZO_API_BASE_URL}/v2/requests/status/{job_id}"
        async with httpx.AsyncClient(timeout=60) as client:
            try: r=await client.get(endpoint, headers={"Ocp-Apim-Subscription-Key":PIXAZO_API_KEY})
            except httpx.HTTPError as exc: raise HTTPException(status_code=502, detail=f"Pixazo connection error: {exc}")
        if r.status_code>=400:
            logger.error("Pixazo STATUS HTTP %s job=%s: %s", r.status_code, job_id, r.text[:4000])
            raise HTTPException(status_code=r.status_code, detail=r.text)
        try:
            data = r.json()
        except Exception:
            logger.error("Pixazo STATUS non-JSON job=%s: %s", job_id, r.text[:4000])
            raise HTTPException(status_code=502, detail="Pixazo returned an invalid status response.")
        status = str(data.get("status") or "").upper()
        error = data.get("error")
        output = data.get("output") or {}
        media = output.get("media_url") if isinstance(output, dict) else None
        logger.info(
            "Pixazo STATUS job=%s status=%s error=%s media_url=%s",
            job_id, status, str(error)[:1000] if error else None,
            bool(media)
        )
        # Return the provider response plus small normalized fields for the frontend.
        data["_nene_status"] = status
        data["_nene_error"] = error
        if isinstance(media, list) and media:
            data["_nene_video_url"] = media[0]
        elif isinstance(media, str):
            data["_nene_video_url"] = media
        return data
    if not LTX_API_KEY: raise HTTPException(status_code=503, detail="LTX_API_KEY is not configured.")
    endpoint=f"{LTX_API_BASE_URL}/v2/{kind}/{job_id}"
    async with httpx.AsyncClient(timeout=60) as client:
        try: r=await client.get(endpoint, headers={"Authorization":f"Bearer {LTX_API_KEY}"})
        except httpx.HTTPError as exc: raise HTTPException(status_code=502, detail=f"LTX connection error: {exc}")
    if r.status_code>=400: raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()
