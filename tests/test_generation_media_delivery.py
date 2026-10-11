from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app


@pytest.mark.asyncio
async def test_media_url_is_signed_only_after_owner_scoped_success_lookup(monkeypatch):
    generation_id = "00000000-0000-4000-8000-000000000001"
    owner_id = "00000000-0000-4000-8000-000000000002"
    key = f"generations/{generation_id}/video.mp4"
    monkeypatch.setattr(app, "enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(app, "get_authenticated_user_id", AsyncMock(return_value=owner_id))
    monkeypatch.setattr(app, "database_configured", lambda: True)
    monkeypatch.setattr(app.database, "fetch_one", AsyncMock(return_value={"result": {"object_key": key}}))
    monkeypatch.setattr("media_storage.create_signed_download_url", lambda object_key, ttl: "https://storage.example.test/signed")
    response = await app.get_generation_media_url(generation_id, SimpleNamespace())
    assert response == {
        "ok": True,
        "generation_id": generation_id,
        "url": "https://storage.example.test/signed",
        "expires_in": 300,
        "content_type": "video/mp4",
    }
    app.database.fetch_one.assert_awaited_once()
    sql = app.database.fetch_one.await_args.args[0]
    assert "user_id=%s" in sql
    assert "status='succeeded'" in sql


@pytest.mark.asyncio
async def test_media_url_does_not_disclose_missing_or_nonowned_generation(monkeypatch):
    generation_id = "00000000-0000-4000-8000-000000000001"
    owner_id = "00000000-0000-4000-8000-000000000002"
    monkeypatch.setattr(app, "enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(app, "get_authenticated_user_id", AsyncMock(return_value=owner_id))
    monkeypatch.setattr(app, "database_configured", lambda: True)
    monkeypatch.setattr(app.database, "fetch_one", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as error:
        await app.get_generation_media_url(generation_id, SimpleNamespace())
    assert error.value.status_code == 404
    assert error.value.detail == "Completed media not found."


@pytest.mark.asyncio
async def test_media_url_does_not_sign_rows_without_private_object_key(monkeypatch):
    generation_id = "00000000-0000-4000-8000-000000000001"
    owner_id = "00000000-0000-4000-8000-000000000002"
    monkeypatch.setattr(app, "enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(app, "get_authenticated_user_id", AsyncMock(return_value=owner_id))
    monkeypatch.setattr(app, "database_configured", lambda: True)
    monkeypatch.setattr(app.database, "fetch_one", AsyncMock(return_value={"result": {"provider_media_url": "https://provider.example/video.mp4"}}))
    with pytest.raises(HTTPException) as error:
        await app.get_generation_media_url(generation_id, SimpleNamespace())
    assert error.value.status_code == 404
