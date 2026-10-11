import readiness


def clear_readiness_env(monkeypatch):
    for key in (
        "NENE_ENV", "RENDER_SERVICE_ID", "K_SERVICE", "FLY_APP_NAME",
        "AUTH_JWKS_URL", "AUTH_ISSUER", "REDIS_URL", "RATE_LIMIT_HMAC_SECRET",
        "LTX_API_KEY", "ALLOWED_MEDIA_HOSTS",
        "NENE_MEDIA_BUCKET", "NENE_MEDIA_REGION", "NENE_MEDIA_ENDPOINT",
        "NENE_MEDIA_ACCESS_KEY_ID", "NENE_MEDIA_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def configure_media_storage(monkeypatch):
    monkeypatch.setenv("NENE_MEDIA_BUCKET", "nene-media-staging")
    monkeypatch.setenv("NENE_MEDIA_REGION", "eu-west-2")
    monkeypatch.setenv("NENE_MEDIA_ENDPOINT", "https://storage.example.test")
    monkeypatch.setenv("NENE_MEDIA_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("NENE_MEDIA_SECRET_ACCESS_KEY", "test-only-secret")


def configure_production_dependencies(monkeypatch):
    monkeypatch.setenv("AUTH_JWKS_URL", "https://example.supabase.co/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("AUTH_ISSUER", "https://example.supabase.co/auth/v1")
    monkeypatch.setenv("REDIS_URL", "rediss://redis.example.test:6379")
    monkeypatch.setenv("RATE_LIMIT_HMAC_SECRET", "x" * 32)
    monkeypatch.setenv("LTX_API_KEY", "test-provider-key")
    monkeypatch.setenv("ALLOWED_MEDIA_HOSTS", "media.example.test,cdn.example.test")
    configure_media_storage(monkeypatch)


def test_production_not_ready_when_critical_services_are_missing(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    snapshot = readiness.readiness_snapshot(production=None, database_ready=False)
    assert snapshot["ready"] is False
    assert snapshot["mode"] == "production"
    assert snapshot["checks"] == {
        "database_configured": False,
        "authentication_configured": False,
        "shared_rate_limits_configured": False,
        "ltx_provider_configured": False,
        "private_media_storage_configured": False,
    }


def test_production_ready_only_when_required_configuration_exists(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    configure_production_dependencies(monkeypatch)
    snapshot = readiness.readiness_snapshot(production=None, database_ready=True)
    assert snapshot["ready"] is True
    assert snapshot["mode"] == "production"
    assert all(snapshot["checks"].values())


def test_plain_redis_url_is_not_production_ready(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    configure_production_dependencies(monkeypatch)
    monkeypatch.setenv("REDIS_URL", "redis://redis.example.test:6379")
    snapshot = readiness.readiness_snapshot(production=None, database_ready=True)
    assert snapshot["ready"] is False
    assert snapshot["checks"]["shared_rate_limits_configured"] is False


def test_missing_private_media_storage_blocks_production_readiness(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    configure_production_dependencies(monkeypatch)
    monkeypatch.delenv("NENE_MEDIA_SECRET_ACCESS_KEY")
    snapshot = readiness.readiness_snapshot(production=None, database_ready=True)
    assert snapshot["ready"] is False
    assert snapshot["checks"]["private_media_storage_configured"] is False


def test_non_https_media_endpoint_blocks_production_readiness(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    configure_production_dependencies(monkeypatch)
    monkeypatch.setenv("NENE_MEDIA_ENDPOINT", "http://storage.example.test")
    snapshot = readiness.readiness_snapshot(production=None, database_ready=True)
    assert snapshot["ready"] is False
    assert snapshot["checks"]["private_media_storage_configured"] is False


def test_missing_ltx_provider_or_host_allowlist_blocks_production_readiness(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    configure_production_dependencies(monkeypatch)
    monkeypatch.delenv("LTX_API_KEY")
    snapshot = readiness.readiness_snapshot(production=None, database_ready=True)
    assert snapshot["ready"] is False
    assert snapshot["checks"]["ltx_provider_configured"] is False


def test_wildcard_or_url_media_allowlist_is_rejected(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    configure_production_dependencies(monkeypatch)
    for invalid in ("*", "*.example.test", "https://media.example.test", "media.example.test/path", "localhost"):
        monkeypatch.setenv("ALLOWED_MEDIA_HOSTS", invalid)
        snapshot = readiness.readiness_snapshot(production=None, database_ready=True)
        assert snapshot["ready"] is False, invalid
        assert snapshot["checks"]["ltx_provider_configured"] is False


def test_development_mode_remains_available_but_reports_missing_checks(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "development")
    snapshot = readiness.readiness_snapshot(production=None, database_ready=False)
    assert snapshot["ready"] is True
    assert snapshot["mode"] == "development"
    assert not any(snapshot["checks"].values())


def test_staging_mode_is_reported_distinctly(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "staging")
    snapshot = readiness.readiness_snapshot(production=None, database_ready=False)
    assert snapshot["ready"] is True
    assert snapshot["mode"] == "staging"
    assert not any(snapshot["checks"].values())


def test_readiness_does_not_expose_secret_values(monkeypatch):
    clear_readiness_env(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.setenv("RATE_LIMIT_HMAC_SECRET", "never-return-this-" + "x" * 32)
    snapshot = readiness.readiness_snapshot(production=None, database_ready=False)
    assert "never-return-this" not in repr(snapshot)
    assert "REDIS_URL" not in repr(snapshot)
    assert "test-only-secret" not in repr(snapshot)
    assert "test-provider-key" not in repr(snapshot)
