import socket

import pytest
from fastapi import HTTPException

import app


def fake_dns(address):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


def test_rejects_private_ip_resolution(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "development")
    monkeypatch.setattr(app.socket, "getaddrinfo", lambda *args, **kwargs: fake_dns("127.0.0.1"))

    with pytest.raises(HTTPException) as error:
        app._validate_public_media_url("https://media.example.test/video.mp4")

    assert error.value.status_code == 400


def test_production_requires_media_host_allowlist(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.delenv("ALLOWED_MEDIA_HOSTS", raising=False)
    monkeypatch.setattr(app.socket, "getaddrinfo", lambda *args, **kwargs: fake_dns("8.8.8.8"))

    with pytest.raises(HTTPException) as error:
        app._validate_public_media_url("https://media.example.test/video.mp4")

    assert error.value.status_code == 503


def test_production_rejects_host_not_on_allowlist(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.setenv("ALLOWED_MEDIA_HOSTS", "approved.example.test")
    monkeypatch.setattr(app.socket, "getaddrinfo", lambda *args, **kwargs: fake_dns("8.8.8.8"))

    with pytest.raises(HTTPException) as error:
        app._validate_public_media_url("https://attacker.example.test/video.mp4")

    assert error.value.status_code == 400


def test_production_accepts_allowlisted_public_host(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.setenv("ALLOWED_MEDIA_HOSTS", "example.test")
    monkeypatch.setattr(app.socket, "getaddrinfo", lambda *args, **kwargs: fake_dns("8.8.8.8"))

    app._validate_public_media_url("https://media.example.test/video.mp4")
