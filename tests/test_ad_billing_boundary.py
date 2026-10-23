from decimal import Decimal
from unittest.mock import AsyncMock
import httpx
import pytest
import app

@pytest.mark.asyncio
async def test_ad_credit_limit_is_forwarded_to_atomic_billing(monkeypatch):
    monkeypatch.setenv('NENE_ENV','development');monkeypatch.setenv('LTX_API_KEY','fake')
    monkeypatch.setattr(app,'enforce_rate_limit',AsyncMock())
    monkeypatch.setattr(app,'get_authenticated_user_id',AsyncMock(return_value='owner'))
    monkeypatch.setattr(app,'database_configured',lambda:True)
    create=AsyncMock(return_value={'generation_id':'job','status':'queued'})
    monkeypatch.setattr(app.generation_service,'create_reserved_generation',create)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),base_url='http://test') as c:
        r=await c.post('/api/generations',headers={'Idempotency-Key':'reviewed-ad'},json={'prompt':'A bakery advertisement','provider':'ltx','credit_limit':'123.45'})
    assert r.status_code==202
    assert create.call_args.kwargs['credit_limit']==Decimal('123.45')

@pytest.mark.asyncio
async def test_advertisement_availability_keeps_launch_closed(monkeypatch):
    monkeypatch.delenv('NENE_DURABLE_GENERATION_CREATE_ENABLED',raising=False)
    data=app.generation_availability()
    assert data['accepting_requests'] is False and data['paid_credits_required'] is True
    assert data['configuration_only'] is True
