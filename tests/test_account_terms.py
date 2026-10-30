"""Account setup accepts once, without changing ordinary sign-in or purchase input."""
from unittest.mock import AsyncMock
import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
import account_terms
from account_agreement_store import TERMS_VERSION

@pytest.mark.parametrize('payload',[
    {'accepted':False,'terms_version':TERMS_VERSION},
    {'accepted':1,'terms_version':TERMS_VERSION},
    {'accepted':'true','terms_version':TERMS_VERSION},
    {'accepted':True,'terms_version':'old'},
    {'accepted':True,'terms_version':TERMS_VERSION,'user_id':'someone-else'},
    {'terms_version':TERMS_VERSION}])
def test_only_explicit_current_account_setup_assertion_is_valid(payload):
    with pytest.raises(ValidationError):account_terms.AccountAgreement(**payload)

@pytest.mark.asyncio
async def test_account_agreement_requires_authentication():
    api=FastAPI();api.include_router(account_terms.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as c:
        for method in ['GET','POST']:
            r=await c.request(method,'/api/account/agreement',**({'json':{'accepted':True,'terms_version':TERMS_VERSION}} if method=='POST' else {}))
            assert r.status_code==401

@pytest.mark.asyncio
async def test_acceptance_owner_comes_only_from_auth(monkeypatch):
    api=FastAPI();api.include_router(account_terms.router)
    api.dependency_overrides[account_terms.get_authenticated_user_id]=lambda:'authenticated-owner'
    monkeypatch.setattr(account_terms.database,'_pool',object())
    record=AsyncMock(return_value={'terms_version':TERMS_VERSION})
    monkeypatch.setattr(account_terms,'record_agreement',record)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as c:
        r=await c.post('/api/account/agreement',json={'accepted':True,'terms_version':TERMS_VERSION})
    assert r.status_code==200
    record.assert_awaited_once_with('authenticated-owner')

@pytest.mark.asyncio
async def test_published_document_contains_refund_policy_and_stable_digest():
    import hashlib,json
    document=await account_terms.read_terms()
    assert document['version']==TERMS_VERSION
    assert document['sha256']==hashlib.sha256(json.dumps(document['sections'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert any('change-of-mind cash refunds' in section['text'] for section in document['sections'])
