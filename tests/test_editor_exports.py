import uuid
from decimal import Decimal
from unittest.mock import AsyncMock
import httpx
import pytest
from fastapi import FastAPI, HTTPException
import editor_exports as editor


@pytest.mark.asyncio
async def test_editor_routes_are_authenticated_and_reject_client_price_and_owner_fields(monkeypatch):
    app=FastAPI();app.include_router(editor.router)
    app.dependency_overrides[editor.get_authenticated_user_id]=lambda:'verified-owner'
    monkeypatch.setattr(editor,'enforce_rate_limit',AsyncMock())
    purchase=AsyncMock(return_value={'paid':True});monkeypatch.setattr(editor,'purchase_export',purchase)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://unit') as client:
        headers={'Idempotency-Key':str(uuid.uuid4())}
        path='/api/editor/exports/'+'a'*64+'/purchase'
        for payload in [{'credit_limit':10,'user_id':'someone-else'}, {'credit_limit':10,'price':0}, {'credit_limit':'NaN'}, {'credit_limit':-1}]:
            assert (await client.post(path,headers=headers,json=payload)).status_code==422
        assert (await client.post(path,headers=headers,json={'credit_limit':'10'})).status_code==200
        assert purchase.await_args.args[0]=='verified-owner'
        assert (await client.post('/api/editor/exports/bad/purchase',headers=headers,json={'credit_limit':10})).status_code==422
        assert (await client.post(path,json={'credit_limit':10})).status_code==422
    assert purchase.await_count==1


@pytest.mark.asyncio
async def test_unfunded_editor_is_local_and_new_export_can_be_disabled(monkeypatch):
    monkeypatch.setattr(editor.database,'_pool',object())
    monkeypatch.setenv('NENE_EDITOR_EXPORTS_ENABLED','false')
    response=await editor.availability()
    assert response['available'] is False and response['credit_cost']=='10'
    assert response['media_uploaded'] is False and response['editing_and_preview_free'] is True


@pytest.mark.asyncio
async def test_missing_database_cannot_charge():
    old=editor.database._pool;editor.database._pool=None
    try:
        with pytest.raises(HTTPException) as exc:
            await editor.purchase_export(str(uuid.uuid4()),'a'*64,uuid.uuid4(),Decimal('10'))
        assert exc.value.status_code==503
    finally:editor.database._pool=old
