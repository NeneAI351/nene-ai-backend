"""Private object storage for completed NENE AI videos.

Storage is S3-compatible (AWS S3, Cloudflare R2, or compatible providers).
Buckets must remain private; clients receive only short-lived presigned GET URLs.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from typing import BinaryIO

import boto3
from botocore.config import Config


MAX_VIDEO_BYTES = int(os.getenv("NENE_MAX_VIDEO_BYTES", str(500 * 1024 * 1024)))
SIGNED_URL_TTL_SECONDS = 300
ALLOWED_VIDEO_CONTENT_TYPES = {"video/mp4", "application/mp4", "application/octet-stream"}


class MediaStorageNotConfigured(RuntimeError):
    pass


def _storage_config() -> dict[str, str]:
    values = {
        "bucket": os.getenv("NENE_MEDIA_BUCKET", "").strip(),
        "region": os.getenv("NENE_MEDIA_REGION", "auto").strip(),
        "endpoint": os.getenv("NENE_MEDIA_ENDPOINT", "").strip(),
        "access_key": os.getenv("NENE_MEDIA_ACCESS_KEY_ID", "").strip(),
        "secret_key": os.getenv("NENE_MEDIA_SECRET_ACCESS_KEY", "").strip(),
    }
    if not values["bucket"] or not values["access_key"] or not values["secret_key"]:
        raise MediaStorageNotConfigured("Private media storage is not configured.")
    if values["endpoint"]:
        parsed = urlparse(values["endpoint"])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise MediaStorageNotConfigured("Media storage endpoint must be a valid HTTPS URL.")
    return values


def _client():
    cfg = _storage_config()
    kwargs = {
        "service_name": "s3",
        "region_name": cfg["region"],
        "aws_access_key_id": cfg["access_key"],
        "aws_secret_access_key": cfg["secret_key"],
        "config": Config(signature_version="s3v4", retries={"max_attempts": 4, "mode": "standard"}),
    }
    if cfg["endpoint"]:
        kwargs["endpoint_url"] = cfg["endpoint"]
    return boto3.client(**kwargs)


def validate_provider_media_url(url: str) -> str:
    """Validate a provider media URL before fetching; never follow redirects."""
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Provider media URL must be HTTPS.")
    if parsed.port not in (None, 443):
        raise ValueError("Provider media URL uses a disallowed port.")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Provider media host is not public.")
    allowed = [h.strip().lower().rstrip(".") for h in os.getenv("ALLOWED_MEDIA_HOSTS", "").split(",") if h.strip()]
    production = os.getenv("NENE_ENV", "").strip().lower() == "production"
    if production and not allowed:
        raise MediaStorageNotConfigured("ALLOWED_MEDIA_HOSTS must be configured in production.")
    if allowed and not any(host == item or host.endswith("." + item) for item in allowed):
        raise ValueError("Provider media host is not allowlisted.")
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Provider media host could not be resolved.") from exc
    if not answers:
        raise ValueError("Provider media host could not be resolved.")
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        if not address.is_global:
            raise ValueError("Provider media host resolved to a non-public address.")
    return url


def _check_video_file(path: str) -> int:
    file_path = Path(path)
    size = file_path.stat().st_size
    if size < 12 or size > MAX_VIDEO_BYTES:
        raise ValueError("Generated video size is outside the allowed range.")
    with file_path.open("rb") as handle:
        header = handle.read(64)
    # ISO BMFF/MP4 files have an ftyp box near the beginning. Reject HTML,
    # JSON error pages, and arbitrary files mislabeled as video.
    if len(header) < 12 or header[4:8] != b"ftyp":
        raise ValueError("Provider result is not a supported MP4 video.")
    return size


def upload_video_file(path: str, generation_id: str) -> dict[str, str | int]:
    """Upload a validated MP4 to a private, generation-scoped object key."""
    import uuid
    generation_uuid = str(uuid.UUID(generation_id))
    size = _check_video_file(path)
    key = f"generations/{generation_uuid}/video.mp4"
    client = _client()
    with open(path, "rb") as body:
        client.put_object(
            Bucket=_storage_config()["bucket"],
            Key=key,
            Body=body,
            ContentLength=size,
            ContentType="video/mp4",
            ContentDisposition="inline; filename=\"nene-video.mp4\"",
            CacheControl="private, no-store",
            Metadata={"generation-id": generation_uuid},
        )
    return {"object_key": key, "content_type": "video/mp4", "size_bytes": size}


def create_signed_download_url(object_key: str, ttl_seconds: int = SIGNED_URL_TTL_SECONDS) -> str:
    import uuid
    if not isinstance(object_key, str) or not object_key.startswith("generations/") or not object_key.endswith("/video.mp4"):
        raise ValueError("Invalid media object key.")
    pieces = object_key.split("/")
    if len(pieces) != 3 or str(uuid.UUID(pieces[1])) != pieces[1]:
        raise ValueError("Invalid media object key.")
    if not 1 <= int(ttl_seconds) <= SIGNED_URL_TTL_SECONDS:
        raise ValueError("Invalid signed URL expiry.")
    cfg = _storage_config()
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": cfg["bucket"], "Key": object_key, "ResponseContentType": "video/mp4"},
        ExpiresIn=int(ttl_seconds),
    )


ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_SOURCE_IMAGE_BYTES = 7 * 1024 * 1024


def validate_image_bytes(raw: bytes) -> tuple[str, str]:
    """Validate supported source-image signatures; never trust browser MIME labels."""
    if not raw or len(raw) > MAX_SOURCE_IMAGE_BYTES:
        raise ValueError("Source image size is outside the allowed range.")
    if raw.startswith(b"\\xff\\xd8\\xff"):
        return "image/jpeg", "jpg"
    if raw.startswith(b"\\x89PNG\\r\\n\\x1a\\n"):
        return "image/png", "png"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise ValueError("Only PNG, JPEG and WEBP images are supported.")


def upload_source_image(raw: bytes, owner_id: str, asset_id: str) -> dict[str, str | int]:
    """Store a validated source image privately under an owner-scoped object key."""
    import uuid
    owner = str(uuid.UUID(owner_id))
    asset = str(uuid.UUID(asset_id))
    content_type, extension = validate_image_bytes(raw)
    key = f"source-images/{owner}/{asset}.{extension}"
    cfg = _storage_config()
    _client().put_object(
        Bucket=cfg["bucket"], Key=key, Body=raw, ContentLength=len(raw),
        ContentType=content_type, ContentDisposition="inline",
        CacheControl="private, no-store",
        Metadata={"owner-id": owner, "asset-id": asset},
    )
    return {"object_key": key, "content_type": content_type, "size_bytes": len(raw)}


def create_signed_source_image_url(object_key: str, ttl_seconds: int = 300) -> str:
    """Create a short-lived provider-fetch URL only for a structurally valid source image key."""
    import uuid
    if not isinstance(object_key, str):
        raise ValueError("Invalid source image object key.")
    pieces = object_key.split("/")
    if len(pieces) != 3 or pieces[0] != "source-images":
        raise ValueError("Invalid source image object key.")
    owner = str(uuid.UUID(pieces[1]))
    filename = pieces[2]
    asset_part, dot, extension = filename.rpartition(".")
    asset = str(uuid.UUID(asset_part)) if dot else ""
    if not asset or extension not in {"jpg", "png", "webp"}:
        raise ValueError("Invalid source image object key.")
    if not 1 <= int(ttl_seconds) <= 600:
        raise ValueError("Invalid source image URL expiry.")
    cfg = _storage_config()
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": cfg["bucket"], "Key": object_key,
                "ResponseContentType": {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[extension]},
        ExpiresIn=int(ttl_seconds),
    )



def delete_source_image(object_key: str) -> None:
    """Best-effort cleanup for an uploaded source image whose DB row was not committed."""
    pieces = object_key.split("/") if isinstance(object_key, str) else []
    if len(pieces) != 3 or pieces[0] != "source-images":
        raise ValueError("Invalid source image object key.")
    import uuid
    str(uuid.UUID(pieces[1]))
    asset_part, dot, extension = pieces[2].rpartition(".")
    if not dot or extension not in {"jpg", "png", "webp"}:
        raise ValueError("Invalid source image object key.")
    str(uuid.UUID(asset_part))
    cfg = _storage_config()
    _client().delete_object(Bucket=cfg["bucket"], Key=object_key)
