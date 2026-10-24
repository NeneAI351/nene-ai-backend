"""Pricing invariants: untrusted client values must never produce free/invalid quotes."""
import math
import pytest

import pricing


@pytest.mark.parametrize("duration", [0, -1, -100, float("nan"), float("inf"), float("-inf"), "not-a-number", None])
def test_quote_rejects_invalid_duration(duration):
    with pytest.raises(ValueError, match="duration"):
        pricing.quote("ltx", "ltx-2-5-fast", "720p", duration)


@pytest.mark.parametrize("margin", [-0.1, 1, 0.99, float("nan"), float("inf")])
def test_quote_rejects_invalid_margin(margin):
    with pytest.raises(ValueError, match="margin"):
        pricing.quote("ltx", "ltx-2-5-fast", "720p", 6, margin)


@pytest.mark.parametrize("resolution", ["", "nonsense", "8k", "0p"])
def test_quote_rejects_unknown_resolution(resolution):
    with pytest.raises(ValueError, match="resolution"):
        pricing.quote("ltx", "ltx-2-5-fast", resolution, 6)


def test_quote_has_positive_credit_reserve_and_expected_margin():
    quote = pricing.quote("ltx", "ltx-2-5-fast", "720p", 6)
    assert quote["recommended_credits"] > 0
    assert quote["provider_cost_usd"] > 0
    assert quote["recommended_customer_value_usd"] >= quote["provider_cost_with_risk_buffer_usd"] / (1 - quote["target_gross_margin"])
    assert math.isfinite(quote["realized_margin"])


def test_quote_includes_fee_aware_credit_cost_and_margin():
    quote = pricing.quote("ltx", "ltx-2-5-fast", "1080p", 6)
    assert quote["recommended_credits"] > 0
    assert quote["provider_cost_with_risk_buffer_ngn"] > 0
    assert quote["estimated_payment_fee_allocated_ngn"] > 0
    assert quote["estimated_contribution_after_provider_buffer_and_payment_fee_ngn"] >= (
        quote["estimated_package_revenue_ngn_allocated_to_generation"] * quote["target_gross_margin"] - 2
    )
    assert quote["fx_reference_ngn_per_usd"] > 0
    assert "actual package/order fees" in quote["payment_fee_note"]


def test_quote_fee_aware_price_accounts_for_provider_risk_buffer():
    quote = pricing.quote("ltx", "ltx-2-5-fast", "1080p", 6)
    assert quote["recommended_credit_value_ngn"] > quote["provider_cost_with_risk_buffer_ngn"]



def test_package_economics_matches_payment_package_values():
    report = pricing.package_economics()
    assert report["target_contribution_margin"] == pytest.approx(0.50)
    assert report["packages"]["starter"]["price_ngn"] == 5000
    assert report["packages"]["starter"]["credits"] == 400
    assert report["packages"]["creator"]["price_ngn"] == 15000
    assert report["packages"]["creator"]["credits"] == 1300
    assert report["packages"]["studio"]["price_ngn"] == 50000
    assert report["packages"]["studio"]["credits"] == 4500
    for package in report["packages"].values():
        assert package["max_direct_cost_for_target_contribution_ngn"] > 0


def test_credit_charge_protects_margin_using_lowest_package_credit_value():
    quote = pricing.quote("ltx", "ltx-2-5-fast", "1080p", 6)
    assert quote["credit_face_value_ngn"] == pytest.approx(50000 / 4500)
    assert quote["estimated_contribution_margin_after_payment_fee"] >= 0.50


def test_video_capacity_covers_supported_ltx_choices_and_package_limits():
    report = pricing.video_capacity()
    assert report["target_contribution_margin"] == pytest.approx(0.50)
    assert report["choices"]
    assert len(report["choices"]) == 24
    for choice in report["choices"]:
        assert choice["credits_per_video"] > 0
        assert choice["estimated_provider_cost_with_buffer_ngn"] > 0
        assert choice["estimated_contribution_margin"] >= 0.50
        for package_id, package in pricing.CREDIT_PACKAGES.items():
            assert choice["package_video_capacity"][package_id] == (
                package["credits"] // choice["credits_per_video"]
            )


def test_video_capacity_does_not_promise_a_video_when_credits_are_insufficient():
    report = pricing.video_capacity()
    expensive = next(
        choice for choice in report["choices"]
        if choice["model"] == "ltx-2-5-pro"
        and choice["resolution"] == "4k"
        and choice["duration_seconds"] == 10
    )
    assert expensive["package_video_capacity"]["starter"] == 0


def test_usd_revenue_and_profit_use_same_conservative_ngn_package_economics():
    quote = pricing.quote("ltx", "ltx-2-5-fast", "1080p", 6)
    expected_revenue_usd = (
        quote["estimated_package_revenue_ngn_allocated_to_generation"]
        / quote["fx_reference_ngn_per_usd"]
    )
    expected_profit_usd = (
        quote["estimated_contribution_after_provider_buffer_and_payment_fee_ngn"]
        / quote["fx_reference_ngn_per_usd"]
    )
    assert quote["recommended_customer_value_usd"] == pytest.approx(expected_revenue_usd, abs=0.0001)
    assert quote["expected_profit_usd"] == pytest.approx(expected_profit_usd, abs=0.0001)
    assert quote["realized_margin"] == pytest.approx(
        quote["estimated_contribution_margin_after_payment_fee"], abs=0.0001
    )
