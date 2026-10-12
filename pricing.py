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
TARGET_GROSS_MARGIN = float(os.getenv("NENE_TARGET_GROSS_MARGIN", "0.50"))
RISK_BUFFER = float(os.getenv("NENE_PROVIDER_RISK_BUFFER", "0.10"))

# Planning reference only. Refresh against the rate used to settle provider
# invoices before enabling live sales. Paystack's local fee schedule can change.
USD_TO_NGN = float(os.getenv("NENE_USD_TO_NGN", "1332.49"))
PAYSTACK_LOCAL_PERCENT = float(os.getenv("NENE_PAYSTACK_LOCAL_PERCENT", "0.015"))
PAYSTACK_LOCAL_FIXED_NGN = float(os.getenv("NENE_PAYSTACK_LOCAL_FIXED_NGN", "100"))
PAYSTACK_LOCAL_FIXED_FEE_WAIVER_BELOW_NGN = float(
    os.getenv("NENE_PAYSTACK_LOCAL_FIXED_FEE_WAIVER_BELOW_NGN", "2500")
)
PAYSTACK_LOCAL_FEE_CAP_NGN = float(os.getenv("NENE_PAYSTACK_LOCAL_FEE_CAP_NGN", "2000"))

# Server-owned package prices used for conservative credit economics. Keep these
# synchronized with payments.PACKAGES; the tests enforce the values.
CREDIT_PACKAGES = {
    "starter": {"price_ngn": 5000, "credits": 400},
    "creator": {"price_ngn": 15000, "credits": 1300},
    "studio": {"price_ngn": 50000, "credits": 4500},
}
# Worst-case package face value (Studio has the lowest price per credit).
CREDIT_VALUE_NGN = min(
    package["price_ngn"] / package["credits"]
    for package in CREDIT_PACKAGES.values()
)
# Conservative payment fee allocation: use Starter's fixed fee per credit plus
# the percentage fee. This is a guardrail; actual reporting uses actual order fee.
FIXED_FEE_PER_CREDIT_NGN = PAYSTACK_LOCAL_FIXED_NGN / CREDIT_PACKAGES["starter"]["credits"]

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
    if "720" in v: return "720p"
    if "480" in v: return "480p"
    raise ValueError("Unsupported resolution. Choose 480p, 720p, 1080p, 1440p, or 4k.")

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
    try:
        duration = float(duration)
        margin = TARGET_GROSS_MARGIN if margin is None else float(margin)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("duration and margin must be finite numbers") from exc
    if not math.isfinite(duration) or not 1 <= duration <= 20:
        raise ValueError("duration must be between 1 and 20 seconds")
    if not math.isfinite(margin) or not 0 <= margin < 0.95:
        raise ValueError("margin must be finite and between 0 and 0.95")
    if not math.isfinite(NENE_CREDIT_USD) or NENE_CREDIT_USD <= 0:
        raise ValueError("NENE_CREDIT_USD must be a positive finite number")
    if not math.isfinite(RISK_BUFFER) or not 0 <= RISK_BUFFER <= 2:
        raise ValueError("NENE_PROVIDER_RISK_BUFFER must be between 0 and 2")
    rate = get_rate(provider, model, resolution)
    if not rate:
        raise KeyError(f"No cost rate for {provider}/{model}/{resolution}")
    raw_cost = rate["usd_per_second"] * duration
    cost_with_buffer = raw_cost * (1 + RISK_BUFFER)
    buffered_cost_ngn = cost_with_buffer * USD_TO_NGN
    # Charge credits against the lowest effective package price, while reserving
    # room for payment processing and the requested contribution margin.
    if not math.isfinite(USD_TO_NGN) or USD_TO_NGN <= 0:
        raise ValueError("NENE_USD_TO_NGN must be a positive finite number")
    if not math.isfinite(PAYSTACK_LOCAL_PERCENT) or not 0 <= PAYSTACK_LOCAL_PERCENT < 1 - margin:
        raise ValueError("NENE_PAYSTACK_LOCAL_PERCENT must be finite and below 1 - margin")
    if not math.isfinite(PAYSTACK_LOCAL_FIXED_NGN) or PAYSTACK_LOCAL_FIXED_NGN < 0:
        raise ValueError("NENE_PAYSTACK_LOCAL_FIXED_NGN must be finite and non-negative")
    if not math.isfinite(PAYSTACK_LOCAL_FEE_CAP_NGN) or PAYSTACK_LOCAL_FEE_CAP_NGN < 0:
        raise ValueError("NENE_PAYSTACK_LOCAL_FEE_CAP_NGN must be finite and non-negative")
    if not math.isfinite(PAYSTACK_LOCAL_FIXED_FEE_WAIVER_BELOW_NGN) or PAYSTACK_LOCAL_FIXED_FEE_WAIVER_BELOW_NGN < 0:
        raise ValueError("NENE_PAYSTACK_LOCAL_FIXED_FEE_WAIVER_BELOW_NGN must be finite and non-negative")
    credit_revenue_ngn = CREDIT_VALUE_NGN
    allocated_payment_fee_ngn = (
        credit_revenue_ngn * PAYSTACK_LOCAL_PERCENT + FIXED_FEE_PER_CREDIT_NGN
    )
    # Target contribution is a percentage of customer revenue after fees:
    # revenue - allocated fee - provider cost >= margin * revenue.
    allowed_cost_per_credit_ngn = (
        credit_revenue_ngn * (1 - margin) - allocated_payment_fee_ngn
    )
    if allowed_cost_per_credit_ngn <= 0:
        raise ValueError("Credit package economics cannot support the requested margin")
    credits = math.ceil(buffered_cost_ngn / allowed_cost_per_credit_ngn)
    revenue = credits * NENE_CREDIT_USD
    profit = revenue - raw_cost
    realized_margin = (profit / revenue) if revenue else 0.0
    allocated_revenue_ngn = credits * CREDIT_VALUE_NGN
    estimated_fee_ngn = (
        allocated_revenue_ngn * PAYSTACK_LOCAL_PERCENT
        + credits * FIXED_FEE_PER_CREDIT_NGN
    )
    contribution_ngn = allocated_revenue_ngn - estimated_fee_ngn - buffered_cost_ngn
    contribution_margin = contribution_ngn / allocated_revenue_ngn if allocated_revenue_ngn else 0.0
    return {
        "provider": provider,
        "model": rate["model"],
        "resolution": rate["resolution"],
        "duration_seconds": duration,
        "provider_cost_usd": round(raw_cost, 6),
        "provider_cost_with_risk_buffer_usd": round(cost_with_buffer, 6),
        "target_gross_margin": margin,
        "nene_credit_usd": NENE_CREDIT_USD,
        "credit_face_value_ngn": round(CREDIT_VALUE_NGN, 4),
        "conservative_payment_fee_per_credit_ngn": round(allocated_payment_fee_ngn, 4),
        "provider_cost_with_risk_buffer_ngn": round(buffered_cost_ngn, 2),
        "recommended_credits": credits,
        "recommended_credit_value_ngn": round(credits * CREDIT_VALUE_NGN, 2),
        "recommended_customer_value_usd": round(revenue, 4),
        "expected_profit_usd": round(profit, 4),
        "realized_margin": round(realized_margin, 4),
        "fx_reference_ngn_per_usd": USD_TO_NGN,
        "estimated_package_revenue_ngn_allocated_to_generation": round(allocated_revenue_ngn, 2),
        "estimated_payment_fee_allocated_ngn": round(estimated_fee_ngn, 2),
        "estimated_contribution_after_provider_buffer_and_payment_fee_ngn": round(contribution_ngn, 2),
        "estimated_contribution_margin_after_payment_fee": round(contribution_margin, 4),
        "payment_fee_note": "Conservative planning allocation: percentage fee plus Starter fixed fee spread over Starter credits. actual package/order fees must be reconciled from provider records.",
        "cost_basis": rate["basis"],
        "estimated_provider_cost": rate["estimated"],
        "warning": "Estimated retail-equivalent provider cost; replace with actual usage/invoice cost when available." if rate["estimated"] else None,
    }

def catalog() -> dict[str, Any]:
    return {
        "nene_credit_usd": NENE_CREDIT_USD,
        "target_gross_margin": TARGET_GROSS_MARGIN,
        "risk_buffer": RISK_BUFFER,
        "fx_reference_ngn_per_usd": USD_TO_NGN,
        "credit_face_value_ngn": round(CREDIT_VALUE_NGN, 4),
        "credit_packages": CREDIT_PACKAGES,
        "rates": RATES,
    }


def package_economics() -> dict[str, Any]:
    """Show package credit economics and theoretical whole-video counts.

    These are maximum counts for a single selected model/resolution/duration;
    they do not promise a fixed number of videos because users can mix options.
    """
    packages: dict[str, Any] = {}
    for package_id, package in CREDIT_PACKAGES.items():
        price = float(package["price_ngn"])
        credits = int(package["credits"])
        payment_fee = min(
            price * PAYSTACK_LOCAL_PERCENT
            + (0 if price < PAYSTACK_LOCAL_FIXED_FEE_WAIVER_BELOW_NGN else PAYSTACK_LOCAL_FIXED_NGN),
            PAYSTACK_LOCAL_FEE_CAP_NGN,
        )
        packages[package_id] = {
            "price_ngn": price,
            "credits": credits,
            "ngn_per_credit": round(price / credits, 4),
            "estimated_local_payment_fee_ngn": round(payment_fee, 2),
            "revenue_after_payment_fee_ngn": round(price - payment_fee, 2),
            "max_direct_cost_for_target_contribution_ngn": round(max(0, price * (1 - TARGET_GROSS_MARGIN) - payment_fee), 2),
            "max_direct_cost_per_credit_for_target_margin_ngn": round(
                max(0, price * (1 - TARGET_GROSS_MARGIN) - payment_fee) / credits, 4
            ),
        }
    return {
        "target_contribution_margin": TARGET_GROSS_MARGIN,
        "fx_reference_ngn_per_usd": USD_TO_NGN,
        "provider_risk_buffer": RISK_BUFFER,
        "packages": packages,
        "note": "Estimates exclude taxes, hosting, storage, support, marketing, refunds not recovered from providers, and other overhead. Actual provider and payment records must replace planning rates.",
    }



def video_capacity() -> dict[str, Any]:
    """Calculate maximum whole videos per package for supported LTX choices.

    Capacity is theoretical and assumes all package credits are spent on one
    video type. It is not a promise of fixed video counts.
    """
    choices: list[dict[str, Any]] = []
    for model in ("ltx-2-5-fast", "ltx-2-5-pro"):
        for resolution in ("720p", "1080p", "1440p", "4k"):
            if not get_rate("ltx", model, resolution):
                continue
            for duration in (6, 8, 10):
                quote_data = quote("ltx", model, resolution, duration)
                credits = int(quote_data["recommended_credits"])
                choices.append({
                    "provider": "ltx",
                    "model": model,
                    "resolution": resolution,
                    "duration_seconds": duration,
                    "credits_per_video": credits,
                    "estimated_provider_cost_with_buffer_ngn": quote_data["provider_cost_with_risk_buffer_ngn"],
                    "estimated_revenue_allocated_per_video_ngn": quote_data["recommended_credit_value_ngn"],
                    "estimated_contribution_margin": quote_data["estimated_contribution_margin_after_payment_fee"],
                    "package_video_capacity": {
                        package_id: int(package["credits"]) // credits
                        for package_id, package in CREDIT_PACKAGES.items()
                    },
                    "cost_basis": quote_data["cost_basis"],
                    "estimated_provider_cost": quote_data["estimated_provider_cost"],
                })
    return {
        "target_contribution_margin": TARGET_GROSS_MARGIN,
        "fx_reference_ngn_per_usd": USD_TO_NGN,
        "provider_risk_buffer": RISK_BUFFER,
        "packages": CREDIT_PACKAGES,
        "choices": choices,
        "note": "Maximum whole-video count when spending all package credits on one video type. Actual provider invoice, FX, payment fee, and mixed usage can change results. Taxes and general overhead are excluded.",
    }
