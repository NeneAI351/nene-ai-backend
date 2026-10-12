"""Owner-only financial overview for NENE AI.

This is a read-only reporting endpoint. It never initiates a payout and never
labels gross collections as withdrawable profit. Configure OWNER_ADMIN_USER_ID
to the UUID of the verified owner account before enabling access.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

import database
from auth import get_authenticated_user_id

router = APIRouter(prefix="/api/admin/finance", tags=["owner-finance"])


def _configured_owner_id() -> str:
    return os.getenv("OWNER_ADMIN_USER_ID", "").strip().lower()


def _money(value: Any) -> str:
    """Return exact decimal values as strings, avoiding JSON float rounding."""
    return str(Decimal(str(value or 0)).quantize(Decimal("0.01")))


async def _require_owner(user_id: str) -> None:
    owner_id = _configured_owner_id()
    if not owner_id:
        raise HTTPException(
            status_code=503,
            detail="Owner finance access is disabled until OWNER_ADMIN_USER_ID is configured.",
        )
    if str(user_id).strip().lower() != owner_id:
        raise HTTPException(status_code=403, detail="Owner access required.")


@router.get("/summary")
async def owner_finance_summary(user_id: str = Depends(get_authenticated_user_id)):
    """Return collections and recorded generation-cost estimates to the owner only."""
    await _require_owner(user_id)
    if not database.database_configured() or database._pool is None:
        raise HTTPException(status_code=503, detail="NENE AI financial database is not configured.")

    async with database._pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT
                    COALESCE(SUM(amount_kobo) FILTER (WHERE mode = 'live' AND status = 'success'), 0)::bigint AS collected_kobo,
                    COALESCE(SUM(amount_kobo) FILTER (WHERE mode = 'live' AND status IN ('initialized','pending')), 0)::bigint AS pending_kobo,
                    COUNT(*) FILTER (WHERE mode = 'live' AND status = 'success')::bigint AS successful_orders,
                    COUNT(*) FILTER (WHERE mode = 'live' AND status IN ('initialized','pending'))::bigint AS pending_orders,
                    COUNT(*) FILTER (WHERE mode = 'live' AND status = 'failed')::bigint AS failed_orders,
                    COALESCE(SUM(amount_kobo) FILTER (WHERE mode = 'test' AND status = 'success'), 0)::bigint AS test_collected_kobo,
                    COUNT(*) FILTER (WHERE mode = 'test' AND status = 'success')::bigint AS test_successful_orders,
                    COUNT(*) FILTER (WHERE mode = 'test' AND status IN ('initialized','pending'))::bigint AS test_pending_orders
                FROM payment_orders
            """)
            payments = await cur.fetchone()
            await cur.execute("""
                SELECT
                    COALESCE(SUM(cost_usd), 0)::numeric(18,6) AS estimated_cost_usd,
                    COUNT(*)::bigint AS cost_records,
                    COUNT(*) FILTER (WHERE COALESCE((metadata->>'estimated')::boolean, TRUE))::bigint AS estimated_records
                FROM provider_costs
            """)
            costs = await cur.fetchone()
            await cur.execute("""
                SELECT reference, package_id, amount_kobo, currency, mode, status, paid_at, created_at
                FROM payment_orders
                ORDER BY created_at DESC
                LIMIT 20
            """)
            recent = await cur.fetchall()

    collected_kobo = int(payments["collected_kobo"] or 0)
    pending_kobo = int(payments["pending_kobo"] or 0)
    return {
        "ok": True,
        "currency": "NGN",
        "collections": {
            "collected_ngn": _money(Decimal(collected_kobo) / Decimal(100)),
            "pending_ngn": _money(Decimal(pending_kobo) / Decimal(100)),
            "successful_orders": int(payments["successful_orders"] or 0),
            "pending_orders": int(payments["pending_orders"] or 0),
            "failed_orders": int(payments["failed_orders"] or 0),
            "basis": "live-mode orders only; excludes test transactions",
        },
        "test_mode": {
            "collected_ngn": _money(Decimal(int(payments["test_collected_kobo"] or 0)) / Decimal(100)),
            "successful_orders": int(payments["test_successful_orders"] or 0),
            "pending_orders": int(payments["test_pending_orders"] or 0),
            "is_real_revenue": False,
        },
        "generation_costs": {
            "estimated_usd": _money(costs["estimated_cost_usd"]),
            "records": int(costs["cost_records"] or 0),
            "estimated_records": int(costs["estimated_records"] or 0),
            "currency": "USD",
        },
        "withdrawable_balance_ngn": None,
        "contribution_profit_ngn": None,
        "payouts_enabled": False,
        "limitations": [
            "The live collections total excludes test-mode transactions; test totals are shown separately and are not real revenue.",
            "Gross collections are not the same as cleared bank funds.",
            "Provider costs are reported in USD and are not converted to NGN.",
            "Payment processing fees, refunds, taxes, chargebacks, and fixed operating costs must be reconciled before profit or withdrawable balance can be calculated.",
            "This endpoint is read-only and does not initiate bank transfers.",
        ],
        "recent_orders": [
            {
                "reference": row["reference"],
                "package_id": row["package_id"],
                "amount_ngn": _money(Decimal(int(row["amount_kobo"])) / Decimal(100)),
                "currency": row["currency"],
                "mode": row["mode"],
                "status": row["status"],
                "paid_at": row["paid_at"].isoformat() if row["paid_at"] else None,
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in recent
        ],
    }
