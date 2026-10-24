import uuid
import pytest
import httpx
from pydantic import ValidationError
from fastapi import FastAPI
import account_profile as profile

@pytest.mark.parametrize('value',[{}, {'user_id':str(uuid.uuid4()),'display_name':'A'}, {'display_name':' '}, {'photo':'https://example.test/x.png'}, {'photo':'data:image/svg+xml;base64,abc'}, {'photo':'data:image/png;base64,YWJj'}, {'brand':{'name':'Brand','credits':10}}, {'display_name':None}])
def test_invalid_profile_update(value):
    with pytest.raises(ValidationError):profile.ProfileUpdate(**value)

@pytest.mark.parametrize('value',[{'kind':'rating','message':'Good'}, {'kind':'problem','message':' '}, {'kind':'abuse','message':'Issue','rating':4}, {'kind':'rating','message':'Good','rating':True}])
def test_invalid_report(value):
    with pytest.raises(ValidationError):profile.SupportReport(request_id=uuid.uuid4(),**value)

@pytest.mark.asyncio
async def test_endpoints_require_verified_authentication():
    app=FastAPI();app.include_router(profile.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        for method,path,body in [('GET','profile',None),('PATCH','profile',{'display_name':'A'}),('GET','reports',None),('POST','reports',{'request_id':str(uuid.uuid4()),'kind':'problem','message':'Broken'})]:
            result=await c.request(method,'/api/account/'+path,**({'json':body} if body else {}))
            assert result.status_code==401
