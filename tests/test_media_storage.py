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



def test_source_image_signature_validation():
    assert media_storage.validate_image_bytes(b"\xff\xd8\xff" + b"jpeg")[0] == "image/jpeg"
    assert media_storage.validate_image_bytes(b"\x89PNG\r\n\x1a\n" + b"png")[1] == "png"
    assert media_storage.validate_image_bytes(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"webp")[0] == "image/webp"
    with pytest.raises(ValueError, match="Only PNG"):
        media_storage.validate_image_bytes(b"<html>not an image</html>")
    with pytest.raises(ValueError, match="size"):
        media_storage.validate_image_bytes(b"")


def test_private_source_image_upload_uses_owner_scoped_key(monkeypatch):
    calls = []

    class FakeS3:
        def put_object(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(media_storage, "_storage_config", lambda: {
        "bucket": "nene-private-test", "region": "auto", "endpoint": "",
        "access_key": "test-access", "secret_key": "test-secret",
    })
    monkeypatch.setattr(media_storage, "_client", lambda: FakeS3())
    owner = "00000000-0000-4000-8000-000000000001"
    asset = "00000000-0000-4000-8000-000000000002"
    result = media_storage.upload_source_image(b"\xff\xd8\xff" + b"jpeg", owner, asset)

    assert result["object_key"] == f"source-images/{owner}/{asset}.jpg"
    assert result["content_type"] == "image/jpeg"
    assert calls[0]["Bucket"] == "nene-private-test"
    assert calls[0]["Key"] == result["object_key"]
    assert calls[0]["CacheControl"] == "private, no-store"
    assert calls[0]["Metadata"] == {"owner-id": owner, "asset-id": asset}


def test_signed_source_image_url_rejects_video_and_arbitrary_keys():
    with pytest.raises(ValueError):
        media_storage.create_signed_source_image_url("generations/00000000-0000-0000-0000-000000000000/video.mp4")
    with pytest.raises(ValueError):
        media_storage.create_signed_source_image_url("source-images/not-a-user/not-an-asset.jpg")


def test_signed_source_image_url_rejects_invalid_expiry():
    owner = "00000000-0000-4000-8000-000000000001"
    asset = "00000000-0000-4000-8000-000000000002"
    key = f"source-images/{owner}/{asset}.png"
    for ttl in (0, 601, -1):
        with pytest.raises(ValueError, match="expiry"):
            media_storage.create_signed_source_image_url(key, ttl)


def test_delete_source_image_rejects_keys_outside_source_prefix():
    with pytest.raises(ValueError, match="Invalid source image object key"):
        media_storage.delete_source_image("generations/00000000-0000-0000-0000-000000000001/video.mp4")
    with pytest.raises(ValueError, match="Invalid source image object key"):
        media_storage.delete_source_image("source-images/not-a-uuid/00000000-0000-0000-0000-000000000002.png")


def test_delete_source_image_targets_only_validated_key(monkeypatch):
    calls = []

    class FakeS3:
        def delete_object(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(media_storage, "_storage_config", lambda: {
        "bucket": "nene-private-test", "region": "auto", "endpoint": "",
        "access_key": "test-access", "secret_key": "test-secret",
    })
    monkeypatch.setattr(media_storage, "_client", lambda: FakeS3())
    owner = "00000000-0000-4000-8000-000000000001"
    asset = "00000000-0000-4000-8000-000000000002"
    key = f"source-images/{owner}/{asset}.webp"
    media_storage.delete_source_image(key)
    assert calls == [{"Bucket": "nene-private-test", "Key": key}]
