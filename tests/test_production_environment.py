import app


def clear_hosted_markers(monkeypatch):
    for key in ("RENDER_SERVICE_ID", "K_SERVICE", "FLY_APP_NAME", "NENE_ENV"):
        monkeypatch.delenv(key, raising=False)


def test_render_host_is_production_when_environment_is_unset(monkeypatch):
    clear_hosted_markers(monkeypatch)
    monkeypatch.setenv("RENDER_SERVICE_ID", "nene-ai-staging-service")
    assert app._is_production_environment() is True


def test_cloud_run_host_is_production_when_environment_is_unset(monkeypatch):
    clear_hosted_markers(monkeypatch)
    monkeypatch.setenv("K_SERVICE", "nene-ai")
    assert app._is_production_environment() is True


def test_fly_host_is_production_when_environment_is_unset(monkeypatch):
    clear_hosted_markers(monkeypatch)
    monkeypatch.setenv("FLY_APP_NAME", "nene-ai")
    assert app._is_production_environment() is True


def test_explicit_development_mode_is_not_production(monkeypatch):
    clear_hosted_markers(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "development")
    monkeypatch.setenv("RENDER_SERVICE_ID", "local-test-service")
    assert app._is_production_environment() is False


def test_explicit_production_mode_is_production(monkeypatch):
    clear_hosted_markers(monkeypatch)
    monkeypatch.setenv("NENE_ENV", "production")
    assert app._is_production_environment() is True
