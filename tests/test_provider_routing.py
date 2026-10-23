"""Tests for provider availability and safe automatic/manual selection."""
import pytest

import provider_routing


def test_status_does_not_expose_provider_secrets(monkeypatch):
    monkeypatch.setenv("LTX_API_KEY", "do-not-return-this")
    status = provider_routing.provider_status()
    assert all("do-not-return-this" not in repr(row) for row in status)
    ltx = next(row for row in status if row["id"] == "ltx")
    assert ltx["available"] is True
    assert "MAGIC_HOUR_API_KEY" not in repr(status)


def test_auto_selects_only_configured_verified_adapter(monkeypatch):
    monkeypatch.setenv("LTX_API_KEY", "test-key")
    selected = provider_routing.select_provider(mode="auto", generation_type="text-to-video")
    assert selected["provider"] == "ltx"
    assert selected["estimated_credits"] is None
    assert selected["requires_server_quote"] is True


def test_auto_fails_closed_when_no_adapter_configured(monkeypatch):
    monkeypatch.delenv("LTX_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="No configured, verified"):
        provider_routing.select_provider(mode="auto")


@pytest.mark.parametrize("name", ["magic-hour", "magic hour", "pixazo"])
def test_unverified_manual_provider_is_not_routed(name, monkeypatch):
    monkeypatch.setenv("LTX_API_KEY", "test-key")
    with pytest.raises(ValueError, match="not enabled"):
        provider_routing.select_provider(mode=name)


def test_unknown_provider_rejected():
    with pytest.raises(ValueError, match="Choose Auto"):
        provider_routing.select_provider(mode="unknown")


@pytest.mark.parametrize("duration", [0, 1, 6, 8, 10, 11, -1])
def test_duration_is_validated(duration, monkeypatch):
    monkeypatch.setenv("LTX_API_KEY", "test-key-not-used-for-network")
    if duration in {6, 8, 10}:
        provider_routing.select_provider(duration_seconds=duration)
    else:
        with pytest.raises(ValueError, match="Duration"):
            provider_routing.select_provider(duration_seconds=duration)


def test_generation_type_is_validated():
    with pytest.raises(ValueError, match="Unsupported generation type"):
        provider_routing.select_provider(generation_type="audio-to-video")
