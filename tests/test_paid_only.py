"""Paid-only HTTP boundaries; no provider traffic or real money."""
import uuid
from unittest.mock import AsyncMock
import httpx
import pytest
from fastapi import FastAPI
import app
import director
import payments
from billing_policy import POLICY_VERSION
from test_director import BRIEF, USER

@pytest.mark.asyncio
@pytest.mark.parametrize('environment',['development','staging','production'])
@pytest.mark.parametrize('path,payload',[
    ('/api/generate',{'prompt':'A sunrise','type':'text-to-video'}),
    ('/api/story/assemble',{'scene_urls':['https://example.com/scene.mp4'],'title':'Test'})])
async def test_retired_routes_never_call_provider_or_ffmpeg(monkeypatch,environment,path,payload):
    monkeypatch.setenv('NENE_ENV',environment)
    monkeypatch.setattr(app,'get_authenticated_user_id',AsyncMock(return_value=USER))
    provider=AsyncMock(side_effect=AssertionError('Unbilled provider call'))
    monkeypatch.setattr(app,'_generate_uncached',provider)
    monkeypatch.setattr(app,'_download_story_clip',provider)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),base_url='http://test') as c:
        response=await c.post(path,json=payload)
    assert response.status_code==503
    provider.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize('price',['','0','-1','NaN','Infinity','0.001','1000000'])
async def test_director_never_calls_provider_without_positive_price(monkeypatch,price):
    monkeypatch.setenv('DIRECTOR_PLANNING_ENABLED','true')
    monkeypatch.setenv('OPENAI_API_KEY','fake')
    monkeypatch.setenv('DIRECTOR_ALLOWED_USER_IDS',USER)
    monkeypatch.setenv('DIRECTOR_PLAN_CREDITS',price)
    api=FastAPI();api.include_router(director.router)
    api.dependency_overrides[director.get_authenticated_user_id]=lambda:USER
    provider=AsyncMock(side_effect=AssertionError('Free planning'))
    monkeypatch.setattr(director,'generate_plan',provider)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as c:
        response=await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':str(uuid.uuid4())})
    assert response.status_code==503
    provider.assert_not_awaited()

def test_checkout_requires_no_repeat_policy_acceptance():
    request=payments.CheckoutRequest(email="buyer@example.test",package_id="starter")
    assert request.model_dump()=={"email":"buyer@example.test","package_id":"starter"}
    assert 'accepted_policy_version' not in payments.CheckoutRequest.model_fields

@pytest.mark.asyncio
async def test_public_policy_preserves_required_refunds_and_credit_restoration():
    policy=await payments.billing_policy()
    assert policy['version']==POLICY_VERSION
    assert policy['free_generation'] is False
    assert policy['failed_generation_credit_restoration'] is True
    assert 'change-of-mind' in policy['refund_policy']
    assert 'app store/payment platform' in policy['refund_policy']
