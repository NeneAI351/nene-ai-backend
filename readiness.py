"""Production readiness checks that expose configuration state, never secrets."""
import os
from urllib.parse import urlparse

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from database import database_configured

router = APIRouter()


def readiness_snapshot(*, production=None, database_ready=None):
    """Report whether critical production configuration is present.

    This is a configuration gate, not a live dependency/latency probe. The
    response deliberately reports booleans only and never returns secret values.
    """
    configured_env = os.getenv("NENE_ENV", "").strip().lower()
    if production is None:
        if configured_env:
            production = configured_env == "production"
        else:
            production = bool(
                os.getenv("RENDER_SERVICE_ID")
                or os.getenv("K_SERVICE")
                or os.getenv("FLY_APP_NAME")
            )

    if database_ready is None:
        database_ready = database_configured()

    auth_ready = bool(
        os.getenv("AUTH_JWKS_URL", "").strip()
        and os.getenv("AUTH_ISSUER", "").strip()
    )
    redis_url = os.getenv("REDIS_URL", "").strip()
    rate_limit_secret = os.getenv("RATE_LIMIT_HMAC_SECRET", "")
    rate_limits_ready = bool(
        redis_url.startswith("rediss://")
        and len(rate_limit_secret.encode("utf-8")) >= 32
    )

    # The current NENE staging design uses an explicit S3-compatible endpoint
    # (Supabase Storage). Do not report production readiness when any required
    # storage setting is absent or the endpoint is not HTTPS.
    media_endpoint = os.getenv("NENE_MEDIA_ENDPOINT", "").strip()
    parsed_endpoint = urlparse(media_endpoint)
    media_storage_ready = bool(
        os.getenv("NENE_MEDIA_BUCKET", "").strip()
        and os.getenv("NENE_MEDIA_REGION", "").strip()
        and os.getenv("NENE_MEDIA_ACCESS_KEY_ID", "").strip()
        and os.getenv("NENE_MEDIA_SECRET_ACCESS_KEY", "").strip()
        and parsed_endpoint.scheme == "https"
        and parsed_endpoint.hostname
        and not parsed_endpoint.username
        and not parsed_endpoint.password
    )

    checks = {
        "database_configured": bool(database_ready),
        "authentication_configured": auth_ready,
        "shared_rate_limits_configured": rate_limits_ready,
        "private_media_storage_configured": media_storage_ready,
    }
    ready = all(checks.values()) if production else True

    # Preserve staging as its own reported mode.
    if production:
        mode = "production"
    elif configured_env == "staging":
        mode = "staging"
    else:
        mode = "development"

    return {
        "ready": ready,
        "mode": mode,
        "checks": checks,
    }


@router.get("/api/ready")
def readiness():
    snapshot = readiness_snapshot()
    return JSONResponse(
        status_code=200 if snapshot["ready"] else 503,
        content=snapshot,
        headers={"Cache-Control": "no-store"},
    )
