"""Shared Redis-backed rate limiting for expensive/public API operations.

Production must use a shared Redis-compatible service. Per-process counters are
not safe when the API runs on multiple instances, so this module never silently
substitutes an in-memory production limiter.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from functools import lru_cache

from fastapi import HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger("nene-ai.security")

# One atomic fixed-window counter. Redis performs INCR and initial EXPIRE in
# one script so concurrent API instances cannot race the counter initialization.
_RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""


@lru_cache(maxsize=1)
def _redis_client(url: str) -> Redis:
    return Redis.from_url(
        url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
        max_connections=int(os.getenv("REDIS_MAX_CONNECTIONS", "100")),
    )


def _is_production() -> bool:
    configured = os.getenv("NENE_ENV", "").strip().lower()
    if configured:
        return configured == "production"
    return bool(os.getenv("RENDER_SERVICE_ID") or os.getenv("K_SERVICE") or os.getenv("FLY_APP_NAME"))


def _client_fingerprint(request: Request) -> str:
    # Do not trust X-Forwarded-For from arbitrary clients. Configure the reverse
    # proxy to provide a trusted client address before changing this behavior.
    host = request.client.host if request.client else "unknown"
    secret = os.getenv("RATE_LIMIT_HMAC_SECRET", "").strip()
    if not secret:
        if _is_production():
            raise HTTPException(
                status_code=503,
                detail="Rate-limit identity protection is not configured.",
            )
        secret = "development-only-rate-limit-key"
    if _is_production() and len(secret.encode()) < 32:
        raise HTTPException(
            status_code=503,
            detail="Rate-limit identity protection must use a secret of at least 32 bytes.",
        )
    return hmac.new(secret.encode(), host.encode(), hashlib.sha256).hexdigest()[:32]


async def enforce_rate_limit(
    request: Request,
    *,
    bucket: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Limit an endpoint across all API instances or fail closed in production."""
    if limit < 1 or window_seconds < 1:
        raise ValueError("Rate-limit limit and window must be positive.")

    redis_url = os.getenv("REDIS_URL", "").strip()
    if _is_production() and not redis_url.startswith("rediss://"):
        raise HTTPException(
            status_code=503,
            detail="Production shared rate limiting requires a TLS-protected Redis URL (rediss://).",
        )
    if not redis_url:
        if _is_production():
            raise HTTPException(
                status_code=503,
                detail="This operation is temporarily unavailable because shared abuse protection is not configured.",
            )
        return

    fingerprint = _client_fingerprint(request)
    key = f"nene:rl:v1:{bucket}:{fingerprint}"
    try:
        result = await _redis_client(redis_url).eval(
            _RATE_LIMIT_SCRIPT, 1, key, str(window_seconds)
        )
        count, ttl = int(result[0]), max(1, int(result[1]))
    except RedisError:
        logger.exception("Shared rate-limit service unavailable for bucket=%s", bucket)
        if _is_production():
            raise HTTPException(
                status_code=503,
                detail="This operation is temporarily unavailable because abuse protection is unavailable.",
            )
        return

    if count > limit:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait before trying again.",
            headers={"Retry-After": str(ttl)},
        )


async def close_rate_limit_client() -> None:
    """Close the shared Redis client during graceful application shutdown."""
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return
    client = _redis_client(redis_url)
    await client.aclose()
    _redis_client.cache_clear()
