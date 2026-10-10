"""NENE AI server-side credit wallet primitives.

These operations require a trusted, authenticated user_id from the application layer.
Never accept user_id from an unauthenticated browser request.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from database import database_configured
from wallet_store import wallet_balance, wallet_ledger, wallet_reserve, wallet_release, wallet_capture

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


def authenticated_user_id(request: Request) -> str:
    """Fail closed until NENE's verified authentication middleware is installed.

    This deliberately does not trust user IDs supplied in headers, query strings,
    or request bodies. A future auth dependency must set request.state.user_id
    after validating a signed session/token.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(
            status_code=503,
            detail="Wallet endpoints are disabled until verified NENE AI authentication is configured.",
        )
    return str(user_id)


class ReserveRequest(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    generation_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=200)


class GrantRequest(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reason: str = Field(min_length=3, max_length=80)
    reference: str = Field(min_length=8, max_length=200)


class SettleRequest(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    generation_id: str
    idempotency_key: str = Field(min_length=8, max_length=200)


@router.get("")
async def get_wallet(user_id: str = Depends(authenticated_user_id)):
    if not database_configured():
        raise HTTPException(status_code=503, detail="NENE AI database is not configured.")
    return await wallet_balance(user_id)


@router.get("/ledger")
async def get_wallet_ledger(limit: int = 50, user_id: str = Depends(authenticated_user_id)):
    if not database_configured():
        raise HTTPException(status_code=503, detail="NENE AI database is not configured.")
    return {"items": await wallet_ledger(user_id, max(1, min(limit, 100)))}


@router.post("/grant")
async def grant_wallet_credits(req: GrantRequest, user_id: str = Depends(authenticated_user_id)):
    # This endpoint is intentionally unavailable to ordinary users. Only a future
    # explicit admin/payment-fulfillment service may call the internal grant helper.
    raise HTTPException(status_code=403, detail="Credits can only be granted by verified payment fulfillment or an authorized admin service.")


@router.post("/reserve")
async def reserve_wallet_credits(req: ReserveRequest, user_id: str = Depends(authenticated_user_id)):
    if not database_configured():
        raise HTTPException(status_code=503, detail="NENE AI database is not configured.")
    try:
        result = await wallet_reserve(user_id, req.amount, str(req.generation_id), req.idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=402, detail="Insufficient available credits.")
    return result


@router.post("/release")
async def release_wallet_credits(req: SettleRequest, user_id: str = Depends(authenticated_user_id)):
    if not database_configured():
        raise HTTPException(status_code=503, detail="NENE AI database is not configured.")
    try:
        return await wallet_release(user_id, req.amount, req.generation_id, req.idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/capture")
async def capture_wallet_credits(req: SettleRequest, user_id: str = Depends(authenticated_user_id)):
    if not database_configured():
        raise HTTPException(status_code=503, detail="NENE AI database is not configured.")
    try:
        return await wallet_capture(user_id, req.amount, req.generation_id, req.idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
