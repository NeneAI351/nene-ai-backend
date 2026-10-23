"""NENE AI wallet read API and internal credit operations boundary.

The HTTP API exposes only authenticated wallet reads. Reserve/capture/release are
server-side primitives and must be invoked by the trusted generation orchestrator,
not directly by a browser. Credit grants remain unavailable until verified payment
fulfillment or an authorized admin service exists.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from database import database_configured
from auth import get_authenticated_user_id as authenticated_user_id
from wallet_store import wallet_balance, wallet_ledger

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


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


@router.get("/paid")
async def get_paid_wallet(user_id: str = Depends(authenticated_user_id)):
    if not database_configured():
        raise HTTPException(503, "NENE AI database is not configured.")
    import database
    from wallet_store import ensure_wallet
    from paid_access import paid_available
    await ensure_wallet(user_id)
    async with database._pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute("SELECT available_credits,reserved_credits,lifetime_consumed FROM wallets WHERE user_id=%s FOR UPDATE", (user_id,))).fetchone()
            spendable = await paid_available(conn,user_id,row)
    return {"ok":True,"paid_available_credits":str(spendable),"free_generation":False}
