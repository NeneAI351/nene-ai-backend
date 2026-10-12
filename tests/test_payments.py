from fastapi import HTTPException
import pytest

import payments


def test_packages_are_fixed_server_side_values():
    assert payments.PACKAGES["starter"]["amount_kobo"] == 500000
    assert str(payments.PACKAGES["starter"]["credits"]) == "400"
    assert payments.PACKAGES["creator"]["amount_kobo"] == 1500000
    assert str(payments.PACKAGES["creator"]["credits"]) == "1300"
    assert payments.PACKAGES["studio"]["amount_kobo"] == 5000000
    assert str(payments.PACKAGES["studio"]["credits"]) == "4500"


def test_payment_reference_format_is_unambiguous():
    assert payments.REFERENCE_RE.fullmatch("nene_" + "a" * 32)
    assert not payments.REFERENCE_RE.fullmatch("nene_" + "A" * 32)
    assert not payments.REFERENCE_RE.fullmatch("other_" + "a" * 32)
    assert not payments.REFERENCE_RE.fullmatch("nene_" + "a" * 31)


def test_test_mode_rejects_missing_or_live_secret(monkeypatch):
    monkeypatch.setenv("PAYSTACK_MODE", "test")
    monkeypatch.delenv("PAYSTACK_SECRET_KEY", raising=False)
    with pytest.raises(HTTPException) as exc:
        payments._test_mode_only()
    assert exc.value.status_code == 503

    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_live_not-for-test")
    with pytest.raises(HTTPException) as exc:
        payments._test_mode_only()
    assert exc.value.status_code == 503


def test_test_mode_accepts_only_test_secret_shape(monkeypatch):
    monkeypatch.setenv("PAYSTACK_MODE", "test")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_example_only")
    payments._test_mode_only()


def test_live_mode_has_an_explicit_launch_lock(monkeypatch):
    monkeypatch.setenv("PAYSTACK_MODE", "live")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_live_example_only")
    monkeypatch.delenv("PAYSTACK_LIVE_PAYMENTS_APPROVED", raising=False)
    with pytest.raises(HTTPException) as exc:
        payments._test_mode_only()
    assert exc.value.status_code == 503

    monkeypatch.setenv("PAYSTACK_LIVE_PAYMENTS_APPROVED", "true")
    payments._test_mode_only()


def test_package_values_match_pricing_economics():
    import pricing
    for package_id, package in payments.PACKAGES.items():
        economics = pricing.CREDIT_PACKAGES[package_id]
        assert package["amount_kobo"] == economics["price_ngn"] * 100
        assert str(package["credits"]) == str(economics["credits"])


def test_checkout_email_validation_accepts_normal_addresses_and_trims_spaces():
    assert payments.is_valid_checkout_email("customer@example.com")
    assert payments.is_valid_checkout_email("  customer.name+video@example.co.ng  ")


def test_checkout_email_validation_rejects_whitespace_and_malformed_addresses():
    for email in ("", "not-an-email", "customer @example.com", "customer@example", "@example.com", "customer@@example.com"):
        assert not payments.is_valid_checkout_email(email), email


def test_webhook_signature_accepts_exact_raw_body_signature():
    import hashlib
    import hmac

    secret = "sk_test_unit_test_secret"
    raw_body = b'{"event":"charge.success","data":{"reference":"nene_abc"}}'
    signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    assert payments.is_valid_webhook_signature(secret, raw_body, signature)


def test_webhook_signature_rejects_wrong_secret_body_or_missing_signature():
    import hashlib
    import hmac

    secret = "sk_test_unit_test_secret"
    raw_body = b'{"event":"charge.success","data":{"reference":"nene_abc"}}'
    signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    assert not payments.is_valid_webhook_signature("different-secret", raw_body, signature)
    assert not payments.is_valid_webhook_signature(secret, raw_body + b" ", signature)
    assert not payments.is_valid_webhook_signature(secret, raw_body, "")
    assert not payments.is_valid_webhook_signature("", raw_body, signature)
