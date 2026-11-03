"""Auth-admin transport accepts current server keys without exposing them."""
from unittest.mock import AsyncMock
import httpx
import pytest
import account_privacy as privacy

@pytest.mark.asyncio
@pytest.mark.parametrize('current',[True,False])
async def test_identity_erasure_sends_current_or_legacy_server_key_correctly(monkeypatch,current):
    monkeypatch.setenv('AUTH_ISSUER','https://example.supabase.co/auth/v1')
    monkeypatch.setenv('SUPABASE_SECRET_KEY','sb_secret_fixture_only' if current else '')
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY','legacy.fixture.jwt')
    monkeypatch.setattr(privacy,'direct_auth_available',AsyncMock(return_value=False))
    key='sb_secret_fixture_only' if current else 'legacy.fixture.jwt'
    assert privacy.auth_admin_config()==('https://example.supabase.co/auth/v1',key)
    requests=[]
    class Client:
        def __init__(self,**options):
            assert options['follow_redirects'] is False
            assert options['trust_env'] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def delete(self,url,headers):
            requests.append((url,headers))
            return httpx.Response(204)
    monkeypatch.setattr(privacy.httpx,'AsyncClient',Client)
    owner='11111111-1111-4111-8111-111111111111'
    await privacy.delete_identity(owner)
    assert requests[0][0]=='https://example.supabase.co/auth/v1/admin/users/'+owner
    assert requests[0][1]['apikey']==key
    if current:assert 'Authorization' not in requests[0][1]
    else:assert requests[0][1]['Authorization']=='Bearer '+key
