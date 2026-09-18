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
app = FastAPI(title="NENE AI Backend", version="0.5.0")
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
    if not req.data_uri.startswith("data:image/") or ";base64," not in req.data_uri:
        raise HTTPException(status_code=400, detail="Expected a base64 image data URI.")
    header, encoded = req.data_uri.split(",", 1)
    mime = header.split(";", 1)[0][5:].lower()
    ext = {"jpeg":"jpg", "jpg":"jpg", "png":"png", "webp":"webp"}.get(mime)
    if not ext:
        raise HTTPException(status_code=400, detail="Only PNG, JPEG and WEBP images are supported.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image.")
    if len(raw) > 7 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image is too large.")
    name = f"{uuid.uuid4().hex}.{ext}"
    path = TEMP_DIR / name
    path.write_bytes(raw)
    # This public URL is fetched by Pixazo immediately after this request.
    return {"ok": True, "image_url": f"https://nene-ai.onrender.com/api/temp-images/{name}"}

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
        payload = {"prompt": req.prompt, "image_url": image}
    payload["duration"] = duration_value(req.duration)
    payload["resolution"] = resolution_value(req.resolution)
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
