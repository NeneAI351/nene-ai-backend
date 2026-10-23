"""Staging must enforce real security before provider credentials are added."""
import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app
import media_storage
import rate_limits


@pytest.mark.asyncio
async def test_staging_blocks_durable_creation_until_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "staging")
    monkeypatch.delenv("NENE_DURABLE_GENERATION_CREATE_ENABLED", raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        response = await client.post("/api/generations", json={
            "type": "text-to-video", "prompt": "Test", "provider": "ltx"})
    assert response.status_code == 503
    assert "not enabled" in response.json()["detail"]


@pytest.mark.asyncio
async def test_staging_legacy_generation_requires_sign_in_before_provider_work(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "staging")
    async def unexpected(*args, **kwargs):
        raise AssertionError("Unauthenticated request must never reach provider work")
    monkeypatch.setattr(app, "_generate_uncached", unexpected)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        response = await client.post("/api/generate", json={"type": "text-to-video", "prompt": "Test"})
    assert response.status_code in {401, 503}


@pytest.mark.asyncio
async def test_staging_requires_tls_shared_rate_limiter(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "staging")
    monkeypatch.delenv("REDIS_URL", raising=False)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                       "client": ("203.0.113.10", 1234)})
    with pytest.raises(HTTPException) as error:
        await rate_limits.enforce_rate_limit(request, bucket="test", limit=1, window_seconds=60)
    assert error.value.status_code == 503


def test_staging_requires_media_host_allowlist(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "staging")
    monkeypatch.delenv("ALLOWED_MEDIA_HOSTS", raising=False)
    with pytest.raises(media_storage.MediaStorageNotConfigured):
        media_storage.validate_provider_media_url("https://media.example.test/video.mp4")


@pytest.mark.asyncio
async def test_staging_health_keeps_correct_environment_label(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "staging")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.json()["environment"] == "staging"
