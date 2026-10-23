import pytest
from scripts import staging_preflight as preflight


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
