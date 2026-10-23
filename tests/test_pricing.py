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
