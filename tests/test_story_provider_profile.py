import pytest
import provider_routing
import pricing
from stories import StoryQuote, quote_story


def test_story_selects_only_enabled_verified_adapter(monkeypatch):
    monkeypatch.setattr(provider_routing,'provider_status',lambda:[{'id':'ltx','available':False},{'id':'magic-hour','available':True}])
    profile=provider_routing.story_generation_profile()
    assert profile['provider']=='magic-hour' and profile['model']=='minimax-h3'
    assert profile['lip_sync_supported'] is False
    assert profile['reference_aspect_ratio'] is None
    monkeypatch.setattr(provider_routing,'provider_status',lambda:[])
    assert provider_routing.story_generation_profile() is None

@pytest.mark.asyncio
async def test_magic_hour_story_total_includes_every_scene_at_configured_cost(monkeypatch):
    monkeypatch.setenv('NENE_MAGIC_HOUR_CREDIT_USD','0.0025')
    result=await quote_story(StoryQuote(scenes=[{'provider':'magic-hour','model':'minimax-h3','duration':6}]*5),'owner')
    from decimal import Decimal
    assert Decimal(result['total_credits'])==5*Decimal(result['scenes'][0]['credits'])
    assert result['lip_sync_supported'] is False

@pytest.mark.parametrize('resolution,seconds',[(r,d) for r in ['480p','720p','1080p'] for d in [6,8,10]])
def test_conservative_cost_and_international_fee_allowance_keep_target_margin(monkeypatch,resolution,seconds):
    monkeypatch.setenv('NENE_MAGIC_HOUR_CREDIT_USD','0.0025')
    monkeypatch.setattr(pricing,'USD_TO_NGN',1600)
    monkeypatch.setattr(pricing,'RISK_BUFFER',.20)
    monkeypatch.setattr(pricing,'PAYMENT_FEE_ALLOWANCE',.039)
    q=pricing.quote('magic-hour','minimax-h3',resolution,seconds)
    assert q['estimated_contribution_margin_after_payment_fee']>=.50
    assert q['provider_cost_with_risk_buffer_usd']>=q['provider_cost_usd']*1.20-0.000001
    assert q['payment_fee_allowance_percent']==.039
    assert q['recommended_credit_value_ngn']>q['provider_cost_with_risk_buffer_ngn']

def test_unconfigured_provider_cost_never_creates_story_quote(monkeypatch):
    monkeypatch.delenv('NENE_MAGIC_HOUR_CREDIT_USD',raising=False)
    with pytest.raises(ValueError):pricing.quote('magic-hour','minimax-h3','720p',6)

@pytest.mark.asyncio
async def test_480p_story_quote_matches_dispatch_and_half_720p_provider_cost(monkeypatch):
    import magic_hour_video
    monkeypatch.setenv('NENE_MAGIC_HOUR_CREDIT_USD','0.0025')
    settings={'provider':'magic-hour','model':'minimax-h3','resolution':'480p','duration':6}
    magic_hour_video.validate(settings, 'text-to-video')
    result=await quote_story(StoryQuote(scenes=[settings]*5),'owner')
    from decimal import Decimal
    q=pricing.quote('magic-hour','minimax-h3','480p',6)
    assert Decimal(result['total_credits']) == 5*Decimal(str(q['recommended_credits']))
    assert q['provider_cost_usd'] == pricing.quote('magic-hour','minimax-h3','720p',6)['provider_cost_usd']/2
    with pytest.raises(Exception):
        await quote_story(StoryQuote(scenes=[{**settings,'provider':'ltx','model':'ltx-2-5-fast'}]),'owner')
