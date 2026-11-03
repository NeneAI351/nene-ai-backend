"""Stop new real-money orders without preventing existing-order reconciliation."""
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
import payments


@pytest.mark.asyncio
async def test_live_checkout_rejects_before_order_or_provider_when_generation_closed(monkeypatch):
    monkeypatch.setenv('PAYSTACK_MODE', 'live')
    monkeypatch.setenv('PAYSTACK_SECRET_KEY', 'sk_live_fixture_only')
    monkeypatch.setenv('PAYSTACK_LIVE_PAYMENTS_APPROVED', 'true')
    monkeypatch.setenv('NENE_DURABLE_GENERATION_CREATE_ENABLED', 'false')
    monkeypatch.setattr(payments, '_require_database', AsyncMock())
    assert (await payments.list_credit_packages())['live_payments_enabled'] is False
    with pytest.raises(HTTPException) as error:
        await payments.create_checkout(payments.CheckoutRequest(package_id='starter',email='buyer@example.test',expected_mode='live'),'owner')
    assert error.value.status_code == 503
    assert 'No payment has been started' in error.value.detail


@pytest.mark.asyncio
async def test_generation_gate_does_not_block_verifying_an_existing_order(monkeypatch):
    monkeypatch.setenv('PAYSTACK_MODE', 'live')
    monkeypatch.setenv('PAYSTACK_SECRET_KEY', 'sk_live_fixture_only')
    monkeypatch.setenv('PAYSTACK_LIVE_PAYMENTS_APPROVED', 'true')
    monkeypatch.setenv('NENE_DURABLE_GENERATION_CREATE_ENABLED', 'false')
    reference = 'nene_' + 'a' * 32
    monkeypatch.setattr(payments, '_require_database', AsyncMock())
    monkeypatch.setattr(payments.database, 'fetch_one', AsyncMock(return_value={'reference':reference}))
    transaction = {'reference':reference,'status':'success'}
    monkeypatch.setattr(payments, '_paystack_get', AsyncMock(return_value=transaction))
    fulfilled = AsyncMock(return_value={'credited':True,'reference':reference})
    monkeypatch.setattr(payments, '_fulfill_verified_transaction', fulfilled)
    result = await payments.verify_checkout(payments.VerifyRequest(reference=reference),'owner')
    assert result['credited'] is True
    fulfilled.assert_awaited_once_with(transaction)


def test_test_mode_does_not_report_live_sales_even_when_flags_are_true(monkeypatch):
    monkeypatch.setenv('PAYSTACK_MODE','test')
    monkeypatch.setenv('PAYSTACK_LIVE_PAYMENTS_APPROVED','true')
    monkeypatch.setenv('NENE_DURABLE_GENERATION_CREATE_ENABLED','true')
    assert payments.live_checkout_enabled() is False
