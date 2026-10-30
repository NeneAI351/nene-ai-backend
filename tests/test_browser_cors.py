"""Exercise real browser preflights, which direct API calls do not cover."""
import importlib

import httpx
import pytest
import app


@pytest.mark.asyncio
async def test_story_scene_put_allowed_only_from_configured_frontend(monkeypatch):
    monkeypatch.setenv('NENE_ENV', 'staging')
    monkeypatch.setenv('CORS_ALLOWED_ORIGINS', 'https://neneai351.github.io')
    # CORS configuration is captured when the application is created.
    importlib.reload(app)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='https://api.test') as client:
            for origin, expected in [('https://neneai351.github.io', 200), ('https://untrusted.test', 400)]:
                response = await client.options('/api/stories/test/scenes', headers={
                    'Origin': origin,
                    'Access-Control-Request-Method': 'PUT',
                    'Access-Control-Request-Headers': 'authorization,content-type,idempotency-key',
                })
                assert response.status_code == expected
                if expected == 200:
                    assert response.headers['access-control-allow-origin'] == origin
                    assert 'PUT' in response.headers['access-control-allow-methods']
                else:
                    assert 'access-control-allow-origin' not in response.headers
    finally:
        # Avoid changing the module-level app for subsequent tests.
        monkeypatch.undo()
        importlib.reload(app)
