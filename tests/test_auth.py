from types import SimpleNamespace
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

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

@pytest.mark.asyncio
async def test_valid_signed_token_resolves_internal_user(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    user_id = str(uuid.uuid4())
    issuer = "https://identity.example.invalid/"
    monkeypatch.setenv("AUTH_JWKS_URL", "https://identity.example.invalid/jwks")
    monkeypatch.setenv("AUTH_ISSUER", issuer)
    monkeypatch.setenv("AUTH_AUDIENCE", "authenticated")

    token = jwt.encode(
        {
            "sub": user_id,
            "iss": issuer,
            "aud": "authenticated",
            "exp": int(time.time()) + 300,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    class FakeClient:
        def get_signing_key_from_jwt(self, supplied_token):
            assert supplied_token == token
            return SimpleNamespace(key=public_key)

    class FakeResult:
        async def fetchone(self):
            return {"status": "active"}

    class FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def transaction(self):
            return FakeTransaction()

        async def execute(self, query, params=()):
            return FakeResult()

    class FakePool:
        def connection(self):
            return FakeConnection()

    monkeypatch.setattr(auth, "_jwks_client", lambda url: FakeClient())
    monkeypatch.setattr(auth.database, "database_configured", lambda: True)
    monkeypatch.setattr(auth.database, "_pool", FakePool())

    request = make_request({"Authorization": f"Bearer {token}"})
    resolved_user_id = await auth.get_authenticated_user_id(request)

    assert resolved_user_id == user_id
    assert request.state.user_id == user_id
