"""Authenticated generation status endpoint does not leak provider internals."""
import asyncio

import httpx

import app


async def _user(_request):
    return "00000000-0000-4000-8000-000000000001"


async def _no_rate_limit(*_args, **_kwargs):
    return None


async def _request(path):
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


def test_generation_status_requires_owner_scoped_lookup(monkeypatch):
    monkeypatch.setattr(app, "get_authenticated_user_id", _user)
    monkeypatch.setattr(app, "enforce_rate_limit", _no_rate_limit)
    monkeypatch.setattr(app, "database_configured", lambda: True)

    async def owned(_user_id, generation_id):
        assert generation_id == "00000000-0000-4000-8000-000000000002"
        return {
            "id": generation_id,
            "project_id": None,
            "generation_type": "text-to-video",
            "status": "running",
            "credits_reserved": "12.00",
            "credits_charged": "0.00",
            "error": None,
            "created_at": None,
            "updated_at": None,
            "completed_at": None,
            "provider": "ltx",
            "provider_job_id": "secret-provider-job-id",
            "result": {"video_url": "https://provider.invalid/private.mp4"},
        }

    monkeypatch.setattr(app.generation_store, "get_owned_generation", owned)
    response = asyncio.run(_request("/api/generations/00000000-0000-4000-8000-000000000002"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["generation"]["status"] == "running"
    assert payload["generation"]["credits_reserved"] == "12.00"
    assert "provider" not in payload["generation"]
    assert "provider_job_id" not in response.text
    assert "video_url" not in response.text


def test_generation_status_hides_unknown_and_foreign_ids(monkeypatch):
    monkeypatch.setattr(app, "get_authenticated_user_id", _user)
    monkeypatch.setattr(app, "enforce_rate_limit", _no_rate_limit)
    monkeypatch.setattr(app, "database_configured", lambda: True)

    async def not_owned(_user_id, _generation_id):
        return None

    monkeypatch.setattr(app.generation_store, "get_owned_generation", not_owned)
    response = asyncio.run(_request("/api/generations/00000000-0000-4000-8000-000000000099"))

    assert response.status_code == 404
    assert response.json()["detail"] == "Generation not found."


def test_generation_status_fails_closed_without_database(monkeypatch):
    monkeypatch.setattr(app, "get_authenticated_user_id", _user)
    monkeypatch.setattr(app, "enforce_rate_limit", _no_rate_limit)
    monkeypatch.setattr(app, "database_configured", lambda: False)

    response = asyncio.run(_request("/api/generations/00000000-0000-4000-8000-000000000002"))

    assert response.status_code == 503
