import json
import uuid
from unittest.mock import AsyncMock
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
import director

USER = str(uuid.uuid4())
BRIEF = dict(idea='A baker opens her first neighbourhood shop.',goal='Story film',scene_count=2,scene_seconds=6,aspect_ratio='16:9')
PLAN = dict(title='Opening Day',summary='A baker prepares and opens her shop.',continuity_notes='Keep her blue apron throughout.',scenes=[dict(title=t,visual_prompt='A baker in a blue apron works in her sunlit bakery.',narration='',on_screen_text='',sound_direction='Soft ambience') for t in ['Before opening','First customer']])

@pytest.fixture
def anyio_backend(): return 'asyncio'

@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv('DIRECTOR_PLANNING_ENABLED','true')
    monkeypatch.setenv('OPENAI_API_KEY','unit-test-secret')
    monkeypatch.setenv('DIRECTOR_ALLOWED_USER_IDS',USER)

@pytest.fixture
def api():
    app=FastAPI();app.include_router(director.router)
    return app

@pytest.mark.parametrize('change',[{'idea':' '},{'scene_count':7},{'scene_count':True},{'user_id':USER},{'scene_seconds':11}])
def test_invalid_brief_rejected(change):
    with pytest.raises(ValidationError):director.Brief(**(BRIEF|change))

def test_output_schema_preserves_title_and_forbids_extra_properties():
    schema=director.response_schema()
    assert 'title' in schema['properties']
    assert schema['additionalProperties'] is False
    assert set(schema['required'])==set(schema['properties'])

@pytest.mark.anyio
async def test_auth_required(api):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as c:
        response=await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':str(uuid.uuid4())})
    assert response.status_code==401

@pytest.mark.anyio
async def test_disabled_does_not_call_database_or_provider(api,monkeypatch):
    api.dependency_overrides[director.get_authenticated_user_id]=lambda:USER
    monkeypatch.delenv('DIRECTOR_PLANNING_ENABLED',raising=False)
    call=AsyncMock(side_effect=AssertionError('Must not call'))
    monkeypatch.setattr(director.database,'fetch_one',call)
    monkeypatch.setattr(director,'generate_plan',call)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as c:
        response=await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':str(uuid.uuid4())})
    assert response.status_code==503
    call.assert_not_awaited()

@pytest.mark.anyio
async def test_replay_returns_saved_plan_without_provider(api,monkeypatch,enabled):
    import hashlib
    api.dependency_overrides[director.get_authenticated_user_id]=lambda:USER
    row={'id':str(uuid.uuid4()),'status':'succeeded','brief':BRIEF,'plan':PLAN,'request_hash':hashlib.sha256(director.Brief(**BRIEF).model_dump_json().encode()).hexdigest()}
    monkeypatch.setattr(director.database,'fetch_one',AsyncMock(return_value=row))
    provider=AsyncMock(side_effect=AssertionError('No resubmission'))
    monkeypatch.setattr(director,'generate_plan',provider)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as c:
        response=await c.post('/api/director/plans',json=BRIEF,headers={'Idempotency-Key':str(uuid.uuid4())})
    assert response.status_code==201 and response.json()['plan']==PLAN
    provider.assert_not_awaited()

@pytest.mark.anyio
async def test_other_owners_plan_is_not_found(api,monkeypatch):
    api.dependency_overrides[director.get_authenticated_user_id]=lambda:USER
    lookup=AsyncMock(return_value=None);monkeypatch.setattr(director.database,'fetch_one',lookup)
    plan_id=str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as c:
        response=await c.get('/api/director/plans/'+plan_id)
    assert response.status_code==404
    assert lookup.call_args.args[1]==(plan_id,USER)

@pytest.mark.anyio
async def test_allowlist_is_required(api,monkeypatch,enabled):
    monkeypatch.setenv('DIRECTOR_ALLOWED_USER_IDS',str(uuid.uuid4()))
    api.dependency_overrides[director.get_authenticated_user_id]=lambda:USER
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as c:
        response=await c.get('/api/director/status')
    assert response.json()['available'] is False

@pytest.mark.anyio
@pytest.mark.parametrize('mode',['valid','wrong_count','incomplete','refusal','secret_error','timeout'])
async def test_provider_contract_and_sanitized_failures(monkeypatch,enabled,mode):
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,headers,json):
            assert url=='https://api.openai.com/v1/responses'
            assert json['model']=='gpt-5.4-mini-2026-03-17'
            assert json['store'] is False and json['max_output_tokens']==4000
            assert not json.get('tools')
            if mode=='timeout':raise httpx.ReadTimeout('unit-test-secret')
            if mode=='secret_error':return httpx.Response(401,json={'error':'unit-test-secret'})
            value=PLAN if mode!='wrong_count' else PLAN|{'scenes':PLAN['scenes'][:1]}
            content=[{'type':'output_text','text':__import__('json').dumps(value)}]
            if mode=='refusal':content=[{'type':'refusal','refusal':'unsafe'}]
            return httpx.Response(200,json={'status':'incomplete' if mode=='incomplete' else 'completed','output':[{'type':'message','content':content}]})
    monkeypatch.setattr(director.httpx,'AsyncClient',Client)
    if mode=='valid':
        plan=await director.generate_plan(director.Brief(**BRIEF));assert plan.title==PLAN['title']
    else:
        with pytest.raises(HTTPException) as exc:await director.generate_plan(director.Brief(**BRIEF))
        assert exc.value.status_code in {422,502,504}
        assert 'unit-test-secret' not in str(exc.value.detail)
