import pytest
from scripts import staging_preflight as preflight


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', [None, 'payment_orders', 'privacy_deletions', 'editor_export_receipts', 'support_reports'])
async def test_schema_probe_requires_current_release_tables_and_always_closes(monkeypatch, missing):
    queried = []
    closed = []
    async def start(): pass
    async def ping(): return True
    async def close(): closed.append(True)
    async def fetch(query, params):
        queried.append(params[0])
        return {'name': None if params[0] == missing else params[0]}
    monkeypatch.setattr(preflight.database, 'start_database', start)
    monkeypatch.setattr(preflight.database, 'database_ping', ping)
    monkeypatch.setattr(preflight.database, 'close_database', close)
    monkeypatch.setattr(preflight.database, 'fetch_one', fetch)
    assert await preflight.check_database() is (missing is None)
    assert closed == [True]
    if missing is None:
        assert {'payment_orders', 'account_agreements', 'privacy_deletions',
                'editor_export_receipts', 'support_reports', 'speech_assets'} <= set(queried)


@pytest.mark.asyncio
async def test_refuses_non_staging_without_connecting(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "production")
    result = await preflight.run_checks()
    assert result["mode"] == "refused"
    assert result["dependencies_ready"] is False


@pytest.mark.asyncio
async def test_missing_configuration_skips_connections(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "staging")
    monkeypatch.setattr(preflight.readiness, "readiness_snapshot", lambda: {
        "ready": False, "checks": {key: False for key in (
            "database_configured", "authentication_configured",
            "shared_rate_limits_configured", "private_media_storage_configured")}})
    async def unexpected():
        raise AssertionError("No probe should run without its configuration")
    for name in ("check_database", "check_authentication", "check_rate_limits", "check_storage"):
        monkeypatch.setattr(preflight, name, unexpected)
    result = await preflight.run_checks()
    assert set(result["connections"].values()) == {"missing_configuration"}
    assert result["dependencies_ready"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_reports_connection_failures_without_secret_details(monkeypatch, fail):
    monkeypatch.setenv("NENE_ENV", "staging")
    monkeypatch.setattr(preflight.readiness, "readiness_snapshot", lambda: {
        "ready": True, "checks": {key: True for key in (
            "database_configured", "authentication_configured", "shared_rate_limits_configured",
            "private_media_storage_configured", "ltx_provider_configured")}})
    async def passed(): return True
    async def storage():
        if fail: raise RuntimeError("secret-password-and-signed-url")
        return True
    for name in ("check_database", "check_authentication", "check_rate_limits"):
        monkeypatch.setattr(preflight, name, passed)
    monkeypatch.setattr(preflight, "check_storage", storage)
    result = await preflight.run_checks()
    assert result["dependencies_ready"] is (not fail)
    assert "secret-password" not in str(result)
    assert result["worker_supervision"] == "not_tested"
    assert result["provider_account_and_generation"] == "not_tested_no_provider_request"
