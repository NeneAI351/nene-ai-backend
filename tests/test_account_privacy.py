"""Erasure safety contracts without real user accounts or storage writes."""
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid
import pytest,httpx
from fastapi import FastAPI
from pydantic import ValidationError
import account_privacy as privacy
import generation_worker

OWNER='11111111-1111-4111-8111-111111111111'
OTHER='22222222-2222-4222-8222-222222222222'
GEN='33333333-3333-4333-8333-333333333333'

def test_delete_contract_cannot_select_another_owner_or_skip_confirmation():
    with pytest.raises(ValidationError):privacy.DeleteRequest(request_id=GEN,confirm_delete=True,user_id=OTHER)
    for value in [False,'true',1,None]:
        with pytest.raises(ValidationError):privacy.DeleteRequest(request_id=GEN,confirm_delete=value)
    assert privacy.digest(OWNER)!=OWNER and privacy.digest(OWNER)!=privacy.digest(OTHER)

def test_storage_cleanup_only_removes_structurally_owned_prefixes(monkeypatch):
    keys={f'source-images/{OWNER}/one.jpg',f'source-speech/{OWNER}/two.wav',f'generations/{GEN}/video.mp4',f'source-images/{OTHER}/keep.jpg'}
    class Storage:
        def list_objects_v2(self,Bucket,Prefix,MaxKeys):return {'Contents':[{'Key':key} for key in sorted(keys) if key.startswith(Prefix)][:1]}
        def delete_objects(self,Bucket,Delete):
            for obj in Delete['Objects']:keys.remove(obj['Key'])
            return {}
    monkeypatch.setattr(privacy.media_storage,'_client',lambda:Storage())
    monkeypatch.setattr(privacy.media_storage,'_storage_config',lambda:{'bucket':'disposable'})
    privacy.purge_private_prefixes(OWNER,{'generation_ids':[GEN]})
    assert keys=={f'source-images/{OTHER}/keep.jpg'}
    with pytest.raises(ValueError):privacy.owned_prefixes(OWNER,{'generation_ids':['../../other']})

def test_partial_storage_failure_never_reports_success(monkeypatch):
    class Storage:
        def list_objects_v2(self,**kwargs):return {'Contents':[{'Key':kwargs['Prefix']+'one'}]}
        def delete_objects(self,**kwargs):return {'Errors':[{'Code':'AccessDenied'}]}
    monkeypatch.setattr(privacy.media_storage,'_client',lambda:Storage())
    monkeypatch.setattr(privacy.media_storage,'_storage_config',lambda:{'bucket':'test'})
    with pytest.raises(RuntimeError,match='incomplete'):privacy.purge_private_prefixes(OWNER,{'generation_ids':[]})

@pytest.mark.asyncio
async def test_unconfigured_identity_cleanup_does_not_disable_account(monkeypatch):
    app=FastAPI();app.include_router(privacy.router);app.dependency_overrides[privacy.get_verified_user_id]=lambda:OWNER
    monkeypatch.setattr(privacy,'enforce_rate_limit',AsyncMock())
    monkeypatch.setattr(privacy.database,'_pool',object())
    monkeypatch.setattr(privacy.database,'fetch_one',AsyncMock(return_value=None))
    monkeypatch.setattr(privacy,'capabilities',AsyncMock(return_value={'account_deletion_available':False}))
    delete=AsyncMock();monkeypatch.setattr(privacy,'request_deletion',delete)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/api/privacy/account-deletion',json={'request_id':GEN,'confirm_delete':True})
    assert response.status_code==503 and delete.await_count==0

@pytest.mark.asyncio
async def test_worker_cannot_dispatch_or_store_after_deletion_starts(monkeypatch):
    @asynccontextmanager
    async def inactive(owner):yield False
    monkeypatch.setattr(privacy,'worker_guard',inactive)
    monkeypatch.setattr(generation_worker,'_load_generation',AsyncMock(return_value={'user_id':OWNER}))
    work=AsyncMock();monkeypatch.setattr(generation_worker,'_process_event_unlocked',work)
    await generation_worker.process_event({'generation_id':GEN},'test-worker')
    assert work.await_count==0

@pytest.mark.asyncio
async def test_public_receipt_contains_no_owner_email_or_manifest(monkeypatch):
    app=FastAPI();app.include_router(privacy.router)
    monkeypatch.setattr(privacy.database,'_pool',object())
    monkeypatch.setattr(privacy,'enforce_rate_limit',AsyncMock())
    monkeypatch.setattr(privacy.database,'fetch_one',AsyncMock(side_effect=[{'status':'completed','created_at':None,'completed_at':None},{'n':1}]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:response=await client.get('/api/privacy/deletions/'+GEN)
    data=response.json();assert data['provider_follow_up_pending'] is True
    assert 'user_id' not in data and 'manifest' not in data and 'email' not in data


def test_direct_identity_erasure_cannot_target_an_unrelated_auth_database(monkeypatch):
    monkeypatch.setenv('AUTH_ISSUER','https://project-a.supabase.co/auth/v1')
    monkeypatch.setattr(database_module := privacy.database,'DATABASE_URL','postgresql://postgres:fake@db.project-b.supabase.co/postgres')
    assert privacy.auth_project_matches() is False
    monkeypatch.setattr(database_module,'DATABASE_URL','postgresql://postgres:fake@db.project-a.supabase.co/postgres')
    assert privacy.auth_project_matches() is True
