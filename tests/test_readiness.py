import readiness


def clear_readiness_env(monkeypatch):
    for key in (
        "NENE_ENV", "RENDER_SERVICE_ID", "K_SERVICE", "FLY_APP_NAME",
        "AUTH_JWKS_URL", "AUTH_ISSUER", "REDIS_URL", "RATE_LIMIT_HMAC_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)


def test_production_not_ready_when_critical_services_are_missing(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    snapshot = readiness.readiness_snapshot(production=None, database_ready=False)
    assert snapshot["ready"] is False
    assert snapshot["checks"] == {
        "database_configured": False,
        "authentication_configured": False,
        "shared_rate_limits_configured": False,
    }


def test_production_ready_only_when_required_configuration_exists(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.setenv("AUTH_JWKS_URL", "https://example.supabase.co/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("AUTH_ISSUER", "https://example.supabase.co/auth/v1")
    monkeypatch.setenv("REDIS_URL", "rediss://redis.example.test:6379")
    monkeypatch.setenv("RATE_LIMIT_HMAC_SECRET", "x" * 32)
    snapshot = readiness.readiness_snapshot(production=None, database_ready=True)
    assert snapshot["ready"] is True
    assert all(snapshot["checks"].values())


def test_plain_redis_url_is_not_production_ready(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.setenv("AUTH_JWKS_URL", "https://example.supabase.co/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("AUTH_ISSUER", "https://example.supabase.co/auth/v1")
    monkeypatch.setenv("REDIS_URL", "redis://redis.example.test:6379")
    monkeypatch.setenv("RATE_LIMIT_HMAC_SECRET", "x" * 32)
    snapshot = readiness.readiness_snapshot(production=None, database_ready=True)
    assert snapshot["ready"] is False
    assert snapshot["checks"]["shared_rate_limits_configured"] is False


def test_development_mode_remains_available_but_reports_missing_checks(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "development")
    snapshot = readiness.readiness_snapshot(production=None, database_ready=False)
    assert snapshot["ready"] is True
    assert snapshot["mode"] == "development"
    assert not any(snapshot["checks"].values())


def test_readiness_does_not_expose_secret_values(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.setenv("RATE_LIMIT_HMAC_SECRET", "never-return-this-" + "x" * 32)
    snapshot = readiness.readiness_snapshot(production=None, database_ready=False)
    assert "never-return-this" not in repr(snapshot)
    assert "REDIS_URL" not in repr(snapshot)
