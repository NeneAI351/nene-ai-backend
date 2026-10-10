"""Verified OIDC/JWKS bearer-token authentication for protected NENE AI routes.

Configure AUTH_JWKS_URL and AUTH_ISSUER for the identity provider. The subject
must be a UUID because NENE's internal users table uses UUID primary keys.
Tokens are never accepted from query parameters or request bodies.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from functools import lru_cache
from urllib.parse import urlparse

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

import database


@lru_cache(maxsize=8)
def _jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_keys=True, lifespan=300)


async def get_authenticated_user_id(request: Request) -> str:
    """Validate a signed bearer token, then ensure its user row exists.

    The user ID comes only from a cryptographically verified JWT subject. Any
    user_id supplied by a browser, header, query parameter, or body is ignored.
    """
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="A valid NENE AI sign-in token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jwks_url = os.getenv("AUTH_JWKS_URL", "").strip()
    issuer = os.getenv("AUTH_ISSUER", "").strip()
    audience = os.getenv("AUTH_AUDIENCE", "authenticated").strip()
    if not jwks_url or not issuer or not audience:
        raise HTTPException(
            status_code=503,
            detail="Verified authentication is not configured on NENE AI yet.",
        )
    jwks_parsed = urlparse(jwks_url)
    issuer_parsed = urlparse(issuer)
    if (
        jwks_parsed.scheme != "https"
        or not jwks_parsed.hostname
        or issuer_parsed.scheme != "https"
        or not issuer_parsed.hostname
    ):
        raise HTTPException(
            status_code=503,
            detail="Authentication JWKS URL and issuer must use HTTPS.",
        )

    try:
        signing_key = await asyncio.to_thread(
            _jwks_client(jwks_url).get_signing_key_from_jwt, token.strip()
        )
    except PyJWKClientConnectionError:
        raise HTTPException(
            status_code=503,
            detail="NENE AI could not reach the identity provider's signing-key service.",
        )
    except PyJWKClientError:
        raise HTTPException(status_code=401, detail="The sign-in token is invalid or uses an unknown signing key.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="The sign-in token is invalid.")

    try:
        claims = await asyncio.to_thread(
            jwt.decode,
            token.strip(),
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "sub"]},
        )
        user_id = str(uuid.UUID(str(claims.get("sub", ""))))
    except (jwt.PyJWTError, ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=401, detail="The sign-in token is invalid or expired.")

    if not database.database_configured() or database._pool is None:
        raise HTTPException(status_code=503, detail="NENE AI account storage is not configured.")

    try:
        async with database._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO users(id) VALUES (%s) ON CONFLICT(id) DO NOTHING",
                    (user_id,),
                )
                row = await (await conn.execute(
                    "SELECT status FROM users WHERE id=%s",
                    (user_id,),
                )).fetchone()
    except Exception:
        # Do not leak SQL, connection details, tokens, or provider claims.
        raise HTTPException(status_code=503, detail="NENE AI could not verify the account status.")

    if not row or row["status"] != "active":
        raise HTTPException(status_code=403, detail="This NENE AI account is not active.")

    request.state.user_id = user_id
    return user_id
