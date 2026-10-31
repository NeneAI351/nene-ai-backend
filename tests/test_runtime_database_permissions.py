from unittest.mock import AsyncMock
import pytest
import app
import database
import generation_worker

@pytest.mark.asyncio
async def test_api_runtime_connects_without_schema_ddl(monkeypatch):
    monkeypatch.setenv('NENE_DATABASE_MIGRATIONS_ENABLED','false')
    monkeypatch.setattr(app,'database_configured',lambda:True)
    start=AsyncMock()
    schema=AsyncMock(side_effect=AssertionError('Runtime must not migrate'))
    monkeypatch.setattr(app,'start_database',start)
    for name in ['initialize_schema','initialize_wallet_schema','initialize_payment_schema','initialize_account_agreement_schema','initialize_profile_schema','initialize_outbox_schema','initialize_director_schema']:
        monkeypatch.setattr(app,name,schema)
    monkeypatch.setattr(app.account_privacy,'cleanup_loop',AsyncMock())
    await app._startup_database()
    await app.app.state.privacy_cleanup_task
    start.assert_awaited_once()
    schema.assert_not_awaited()

@pytest.mark.asyncio
async def test_enabled_worker_runtime_connects_without_schema_ddl(monkeypatch):
    monkeypatch.setenv('NENE_DATABASE_MIGRATIONS_ENABLED','false')
    monkeypatch.setenv('NENE_GENERATION_WORKER_ENABLED','true')
    monkeypatch.setattr(database,'database_configured',lambda:True)
    start=AsyncMock()
    schema=AsyncMock(side_effect=AssertionError('Runtime must not migrate'))
    monkeypatch.setattr(database,'start_database',start)
    monkeypatch.setattr(database,'initialize_schema',schema)
    monkeypatch.setattr(database,'close_database',AsyncMock())
    consume=AsyncMock()
    monkeypatch.setattr(generation_worker,'consume_events',consume)
    await generation_worker.run_worker()
    start.assert_awaited_once()
    schema.assert_not_awaited()
    consume.assert_awaited_once()
