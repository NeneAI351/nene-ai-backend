from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from jwt.exceptions import InvalidTokenError

import auth


def make_request(headers=None):
    encoded_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/wallet",
        "raw_path": b"/api/wallet",
        "query_string": b"",
        "headers": encoded_headers,
        "client": ("testclient", 123),
        "server": ("testserver", 443),
    })


@pytest.mark.asyncio
async def test_missing_bearer_token_is_rejected_even_if_request_state_is_spoofed():
    request = make_request()
    request.state.user_id = "00000000-0000-0000-0000-000000000001"

    with pytest.raises(HTTPException) as error:
        await auth.get_authenticated_user_id(request)

    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_authentication_fails_closed_when_provider_is_not_configured(monkeypatch):
    monkeypatch.delenv("AUTH_JWKS_URL", raising=False)
    monkeypatch.delenv("AUTH_ISSUER", raising=False)
    request = make_request({"Authorization": "Bearer placeholder-token"})

    with pytest.raises(HTTPException) as error:
        await auth.get_authenticated_user_id(request)

    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_invalid_signed_token_is_rejected(monkeypatch):
    monkeypatch.setenv("AUTH_JWKS_URL", "https://identity.example.invalid/jwks")
    monkeypatch.setenv("AUTH_ISSUER", "https://identity.example.invalid/")
    monkeypatch.setenv("AUTH_AUDIENCE", "authenticated")

    class FakeClient:
        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key="test-public-key")

    monkeypatch.setattr(auth, "_jwks_client", lambda url: FakeClient())

    def reject_token(*args, **kwargs):
        raise InvalidTokenError("invalid test token")

    monkeypatch.setattr(auth.jwt, "decode", reject_token)
    request = make_request({"Authorization": "Bearer invalid-token"})

    with pytest.raises(HTTPException) as error:
        await auth.get_authenticated_user_id(request)

    assert error.value.status_code == 401
