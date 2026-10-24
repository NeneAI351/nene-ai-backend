"""Character Studio image bounds, verified API access and retry safety."""
import base64
import uuid

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import characters
import character_store
from auth import get_authenticated_user_id


def test_private_reference_round_trip_and_description_limit():
    image = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * 30000).decode()
    profile = {'appearance': 'Gold coat', 'referenceImage': image}
    assert characters._validate_profile(profile) == profile
    with pytest.raises(HTTPException) as error:
        characters._validate_profile({'appearance': 'x' * 17000, 'referenceImage': image})
    assert error.value.status_code == 422


@pytest.mark.parametrize('reference', [True, {}, 'https://example.test/public.png',
    'data:image/svg+xml;base64,PHN2Zy8+', 'data:image/png;base64,YWJj',
    'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * (400 * 1024)).decode()])
def test_bad_or_oversized_reference_is_rejected(reference):
    with pytest.raises(HTTPException) as error:
        characters._validate_profile({'referenceImage': reference})
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_api_requires_auth_for_all_character_operations():
    app = FastAPI(); app.include_router(characters.router)
    cid = str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for method, path in [('GET', ''), ('POST', ''), ('GET', '/'+cid), ('PATCH', '/'+cid), ('DELETE', '/'+cid)]:
            response = await client.request(method, '/api/characters'+path, json={'name':'Hero'})
            assert response.status_code == 401


@pytest.mark.asyncio
async def test_api_uses_verified_owner_and_client_retry_id(monkeypatch):
    app = FastAPI(); app.include_router(characters.router)
    owner, cid = str(uuid.uuid4()), str(uuid.uuid4())
    async def auth(): return owner
    app.dependency_overrides[get_authenticated_user_id] = auth
    monkeypatch.setattr(characters.database, 'database_configured', lambda: True)
    calls = []
    async def create(user, character, project, name, profile):
        calls.append((user, character, project, name, profile))
        return {'id':character,'user_id':user,'name':name,'profile':profile}
    monkeypatch.setattr(character_store, 'create_character', create)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        body = {'id':cid,'name':' Hero ','profile':{'appearance':'Blue coat'}}
        for _ in range(2):
            response = await client.post('/api/characters', json=body)
            assert response.status_code == 201
            assert response.json()['user_id'] == owner
        assert calls[0] == calls[1]
        assert calls[0][1] == cid
        assert (await client.post('/api/characters',json={**body,'user_id':str(uuid.uuid4())})).status_code == 422
        async def conflict(*args): raise ValueError('conflict')
        monkeypatch.setattr(character_store, 'create_character', conflict)
        assert (await client.post('/api/characters',json=body)).status_code == 409
