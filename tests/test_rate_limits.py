import pytest
from fastapi import HTTPException
from starlette.requests import Request

import rate_limits


def make_request(host="203.0.113.10"):
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/generate",
        "raw_path": b"/api/generate",
        "query_string": b"",
        "headers": [],
        "client": (host, 12345),
        "server": ("testserver", 443),
    })


@pytest.mark.asyncio
async def test_production_fails_closed_without_shared_rate_limit_service(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("RATE_LIMIT_HMAC_SECRET", "test-only-secret-that-is-at-least-32-bytes-long")

    with pytest.raises(HTTPException) as error:
        await rate_limits.enforce_rate_limit(
            make_request(), bucket="generate", limit=5, window_seconds=60
        )

    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_production_requires_hmac_secret(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.setenv("REDIS_URL", "rediss://localhost:6379/0")
    monkeypatch.delenv("RATE_LIMIT_HMAC_SECRET", raising=False)

    with pytest.raises(HTTPException) as error:
        await rate_limits.enforce_rate_limit(
            make_request(), bucket="generate", limit=5, window_seconds=60
        )

    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_distributed_limit_returns_429_after_threshold(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.setenv("REDIS_URL", "rediss://localhost:6379/0")
    monkeypatch.setenv("RATE_LIMIT_HMAC_SECRET", "test-only-secret-that-is-at-least-32-bytes-long")

    class FakeRedis:
        def __init__(self):
            self.count = 0

        async def eval(self, script, numkeys, key, window):
            self.count += 1
            return [self.count, 42]

    fake = FakeRedis()
    monkeypatch.setattr(rate_limits, "_redis_client", lambda url: fake)

    request = make_request()
    for _ in range(5):
        await rate_limits.enforce_rate_limit(
            request, bucket="generate", limit=5, window_seconds=60
        )

    with pytest.raises(HTTPException) as error:
        await rate_limits.enforce_rate_limit(
            request, bucket="generate", limit=5, window_seconds=60
        )

    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "42"


@pytest.mark.asyncio
async def test_client_ip_is_hmac_fingerprinted(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.setenv("RATE_LIMIT_HMAC_SECRET", "test-only-secret-that-is-at-least-32-bytes-long")
    monkeypatch.setenv("REDIS_URL", "rediss://localhost:6379/0")

    class FakeRedis:
        async def eval(self, script, numkeys, key, window):
            assert "203.0.113.10" not in key
            assert key.startswith("nene:rl:v1:generate:")
            return [1, 60]

    monkeypatch.setattr(rate_limits, "_redis_client", lambda url: FakeRedis())
    await rate_limits.enforce_rate_limit(
        make_request(), bucket="generate", limit=5, window_seconds=60
    )
