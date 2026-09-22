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
app = FastAPI(title="NENE AI Backend", version="0.6.0-quality-test")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

LTX_API_KEY = os.getenv("LTX_API_KEY", "").strip()
PIXAZO_API_KEY = os.getenv("PIXAZO_API_KEY", "").strip()
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
    return {"ok": True, "service": "nene-ai-backend", "provider": "pixazo" if PIXAZO_API_KEY else "ltx", "pixazo_configured": bool(PIXAZO_API_KEY), "ltx_configured": bool(LTX_API_KEY), "api_base": PIXAZO_API_BASE_URL if PIXAZO_API_KEY else LTX_API_BASE_URL}

@app.get("/api/provider-info")
def provider_info():
    return {
        "provider": "pixazo" if PIXAZO_API_KEY else "ltx",
        "pixazo_configured": bool(PIXAZO_API_KEY),
        "pixazo_base": PIXAZO_API_BASE_URL,
        "status_endpoint": f"{PIXAZO_API_BASE_URL}/v2/requests/status/{{request_id}}",
        "polling": "5-10 seconds",
        "terminal_statuses": ["COMPLETED", "FAILED", "ERROR"],
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
    kind=req.type.lower().strip()
    if kind not in {"text-to-video","image-to-video"}: raise HTTPException(status_code=400, detail="Supported types: text-to-video, image-to-video")
    if PIXAZO_API_KEY: return await pixazo_generate(req, kind)
    return await ltx_generate(req, kind)

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
