"""NENE AI provider cost and margin calculator.

Public rates are refreshed manually from provider documentation. Each rate is
tagged with its source basis so estimated retail-equivalent rates are never
mistaken for an invoice cost.
"""

import math
import os
from typing import Any

# NENE's internal accounting unit. Default: 1 NENE credit has a face value of
# $0.01 before plan discounts. This is a pricing/accounting convention, not a
# provider cost.
NENE_CREDIT_USD = float(os.getenv("NENE_CREDIT_USD", "0.01"))
TARGET_GROSS_MARGIN = float(os.getenv("NENE_TARGET_GROSS_MARGIN", "0.70"))
RISK_BUFFER = float(os.getenv("NENE_PROVIDER_RISK_BUFFER", "0.10"))

# Exact public API rates where the provider publishes them directly.
RATES = [
    # LTX direct API — exact published rates.
    {"provider":"ltx","model":"ltx-2-5-fast","resolution":"720p","usd_per_second":0.09,"basis":"official_api","estimated":False},
    {"provider":"ltx","model":"ltx-2-5-fast","resolution":"1080p","usd_per_second":0.13,"basis":"official_api","estimated":False},
    {"provider":"ltx","model":"ltx-2-5-fast","resolution":"1440p","usd_per_second":0.19,"basis":"official_api","estimated":False},
    {"provider":"ltx","model":"ltx-2-5-fast","resolution":"4k","usd_per_second":0.30,"basis":"official_api","estimated":False},
    {"provider":"ltx","model":"ltx-2-5-pro","resolution":"720p","usd_per_second":0.12,"basis":"official_api","estimated":False},
    {"provider":"ltx","model":"ltx-2-5-pro","resolution":"1080p","usd_per_second":0.17,"basis":"official_api","estimated":False},
    {"provider":"ltx","model":"ltx-2-5-pro","resolution":"1440p","usd_per_second":0.25,"basis":"official_api","estimated":False},
    {"provider":"ltx","model":"ltx-2-5-pro","resolution":"4k","usd_per_second":0.39,"basis":"official_api","estimated":False},

    # Pixazo LTX 2.5 API — exact published rates.
    {"provider":"pixazo","model":"ltx-2-5-lite","resolution":"720p","usd_per_second":0.09,"basis":"official_api","estimated":False},
    {"provider":"pixazo","model":"ltx-2-5-lite","resolution":"1080p","usd_per_second":0.13,"basis":"official_api","estimated":False},
    {"provider":"pixazo","model":"ltx-2-5-lite","resolution":"1440p","usd_per_second":0.19,"basis":"official_api","estimated":False},
    {"provider":"pixazo","model":"ltx-2-5-lite","resolution":"4k","usd_per_second":0.30,"basis":"official_api","estimated":False},
    {"provider":"pixazo","model":"ltx-2-5-pro","resolution":"720p","usd_per_second":0.12,"basis":"official_api","estimated":False},
    {"provider":"pixazo","model":"ltx-2-5-pro","resolution":"1080p","usd_per_second":0.17,"basis":"official_api","estimated":False},

    # Magic Hour LTX-2.5: 24/48/72 credits/sec. We convert credits using the
    # currently displayed $10 / 4,000 credit pack (= $0.0025/credit). This is
    # deliberately marked estimated: API usage-based pricing may differ.
    {"provider":"magic-hour","model":"ltx-2.5","resolution":"480p","usd_per_second":0.06,"basis":"credit_pack_equivalent","estimated":True},
    {"provider":"magic-hour","model":"ltx-2.5","resolution":"720p","usd_per_second":0.12,"basis":"credit_pack_equivalent","estimated":True},
    {"provider":"magic-hour","model":"ltx-2.5","resolution":"1080p","usd_per_second":0.18,"basis":"credit_pack_equivalent","estimated":True},
]

def _norm_resolution(value: str) -> str:
    v = (value or "").lower().replace(" ", "")
    if "2160" in v or "4k" in v: return "4k"
    if "1440" in v: return "1440p"
    if "1080" in v: return "1080p"
    return "720p"

def _norm_model(provider: str, model: str) -> str:
    m = (model or "").lower().strip()
    if provider == "magic-hour":
        return "ltx-2.5"
    if provider == "pixazo" and m in {"ltx","ltx-2.5","ltx-2-5","ltx-2-5-fast"}:
        return "ltx-2-5-lite"
    if provider == "ltx" and m in {"ltx","ltx-2.5","ltx-2-5"}:
        return "ltx-2-5-fast"
    return m

def get_rate(provider: str, model: str, resolution: str) -> dict[str, Any] | None:
    p = (provider or "").lower().strip()
    m = _norm_model(p, model)
    r = _norm_resolution(resolution)
    for row in RATES:
        if row["provider"] == p and row["model"] == m and row["resolution"] == r:
            return dict(row)
    return None

def quote(provider: str, model: str, resolution: str, duration: float, margin: float | None = None) -> dict[str, Any]:
    duration = max(0.0, float(duration or 0))
    margin = TARGET_GROSS_MARGIN if margin is None else float(margin)
    if not 0 <= margin < 1:
        raise ValueError("margin must be between 0 and 1")
    rate = get_rate(provider, model, resolution)
    if not rate:
        raise KeyError(f"No cost rate for {provider}/{model}/{resolution}")
    raw_cost = rate["usd_per_second"] * duration
    cost_with_buffer = raw_cost * (1 + RISK_BUFFER)
    revenue_needed = cost_with_buffer / (1 - margin) if duration else 0.0
    credits = math.ceil(revenue_needed / NENE_CREDIT_USD) if revenue_needed else 0
    revenue = credits * NENE_CREDIT_USD
    profit = revenue - raw_cost
    realized_margin = (profit / revenue) if revenue else 0.0
    return {
        "provider": provider,
        "model": rate["model"],
        "resolution": rate["resolution"],
        "duration_seconds": duration,
        "provider_cost_usd": round(raw_cost, 6),
        "provider_cost_with_risk_buffer_usd": round(cost_with_buffer, 6),
        "target_gross_margin": margin,
        "nene_credit_usd": NENE_CREDIT_USD,
        "recommended_credits": credits,
        "recommended_customer_value_usd": round(revenue, 4),
        "expected_profit_usd": round(profit, 4),
        "realized_margin": round(realized_margin, 4),
        "cost_basis": rate["basis"],
        "estimated_provider_cost": rate["estimated"],
        "warning": "Estimated retail-equivalent provider cost; replace with actual usage/invoice cost when available." if rate["estimated"] else None,
    }

def catalog() -> dict[str, Any]:
    return {
        "nene_credit_usd": NENE_CREDIT_USD,
        "target_gross_margin": TARGET_GROSS_MARGIN,
        "risk_buffer": RISK_BUFFER,
        "rates": RATES,
    }
