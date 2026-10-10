"""Regression tests for production generation safety gates.

These tests intentionally keep paid generation and provider-job polling disabled
until generation ownership, server-side billing, and durable recovery are wired
into the production path.
"""
import asyncio

from fastapi.testclient import TestClient

import app


async def _allow_test_user(_request):
    return "00000000-0000-4000-8000-000000000001"


async def _skip_rate_limit(*_args, **_kwargs):
    return None


def _force_production(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.setattr(app, "get_authenticated_user_id", _allow_test_user)
    monkeypatch.setattr(app, "enforce_rate_limit", _skip_rate_limit)


def test_production_generation_is_authenticated_then_fail_closed(monkeypatch):
    _force_production(monkeypatch)
    with TestClient(app.app) as client:
        response = client.post(
            "/api/generate",
            json={"type": "text-to-video", "prompt": "A safe test prompt"},
        )

    assert response.status_code == 503
    assert "server-side credit reservations" in response.json()["detail"]
    assert "generation ownership" in response.json()["detail"]
    assert "durable job recovery" in response.json()["detail"]


def test_production_job_polling_is_fail_closed(monkeypatch):
    _force_production(monkeypatch)
    with TestClient(app.app) as client:
        response = client.get("/api/jobs/provider-job-id")

    assert response.status_code == 503
    assert "authenticated generation ownership" in response.json()["detail"]


def test_generation_guard_precedes_provider_submission(monkeypatch):
    _force_production(monkeypatch)
    submitted = []

    async def unexpected_provider_call(*_args, **_kwargs):
        submitted.append(True)
        raise AssertionError("Production guard must block provider submission.")

    monkeypatch.setattr(app, "_generate_uncached", unexpected_provider_call)
    with TestClient(app.app) as client:
        response = client.post(
            "/api/generate",
            json={"type": "text-to-video", "prompt": "A safe test prompt"},
        )

    assert response.status_code == 503
    assert submitted == []
