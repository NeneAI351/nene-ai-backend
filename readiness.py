"""Production readiness checks that expose configuration state, never secrets."""
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from database import database_configured

router = APIRouter()


def readiness_snapshot(*, production=None, database_ready=None):
    """Report whether critical production configuration is present.

    This is a configuration gate, not a live dependency/latency probe. The
    response deliberately reports booleans only and never returns secret values.
    """
    if production is None:
        configured_env = os.getenv("NENE_ENV", "").strip().lower()
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

    checks = {
        "database_configured": bool(database_ready),
        "authentication_configured": auth_ready,
        "shared_rate_limits_configured": rate_limits_ready,
    }
    ready = all(checks.values()) if production else True
    return {
        "ready": ready,
        "mode": "production" if production else "development",
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
