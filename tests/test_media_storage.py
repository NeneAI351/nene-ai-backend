import socket
from pathlib import Path

import pytest

import media_storage


def fake_dns(address):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


def test_media_url_requires_https_and_rejects_credentials(monkeypatch):
    monkeypatch.setattr(media_storage.socket, "getaddrinfo", lambda *a, **k: fake_dns("8.8.8.8"))
    with pytest.raises(ValueError):
        media_storage.validate_provider_media_url("http://media.example.test/video.mp4")
    with pytest.raises(ValueError):
        media_storage.validate_provider_media_url("https://user:pass@media.example.test/video.mp4")


def test_media_url_rejects_non_public_dns(monkeypatch):
    monkeypatch.setattr(media_storage.socket, "getaddrinfo", lambda *a, **k: fake_dns("10.0.0.5"))
    with pytest.raises(ValueError, match="non-public"):
        media_storage.validate_provider_media_url("https://media.example.test/video.mp4")


def test_production_requires_host_allowlist(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.delenv("ALLOWED_MEDIA_HOSTS", raising=False)
    monkeypatch.setattr(media_storage.socket, "getaddrinfo", lambda *a, **k: fake_dns("8.8.8.8"))
    with pytest.raises(media_storage.MediaStorageNotConfigured):
        media_storage.validate_provider_media_url("https://media.example.test/video.mp4")


def test_media_url_enforces_host_allowlist(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.setenv("ALLOWED_MEDIA_HOSTS", "approved.example.test")
    monkeypatch.setattr(media_storage.socket, "getaddrinfo", lambda *a, **k: fake_dns("8.8.8.8"))
    with pytest.raises(ValueError, match="not allowlisted"):
        media_storage.validate_provider_media_url("https://attacker.example.test/video.mp4")


def test_media_url_accepts_public_allowlisted_host(monkeypatch):
    monkeypatch.setenv("NENE_ENV", "production")
    monkeypatch.setenv("ALLOWED_MEDIA_HOSTS", "example.test")
    monkeypatch.setattr(media_storage.socket, "getaddrinfo", lambda *a, **k: fake_dns("8.8.8.8"))
    assert media_storage.validate_provider_media_url("https://media.example.test/video.mp4")


def test_video_validator_requires_mp4_signature(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20)
    assert media_storage._check_video_file(str(path)) == path.stat().st_size
    path.write_bytes(b"<html>not a video</html>")
    with pytest.raises(ValueError, match="MP4"):
        media_storage._check_video_file(str(path))


def test_signed_url_rejects_arbitrary_object_key(monkeypatch):
    with pytest.raises(ValueError):
        media_storage.create_signed_download_url("private/secrets.txt")
    with pytest.raises(ValueError):
        media_storage.create_signed_download_url("generations/not-a-uuid/video.mp4")


def test_signed_url_ttl_is_bounded():
    with pytest.raises(ValueError):
        media_storage.create_signed_download_url("generations/00000000-0000-0000-0000-000000000000/video.mp4", 3600)
