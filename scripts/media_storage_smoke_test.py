"""Run a controlled S3-compatible media storage round-trip smoke test.

Requires staging-only NENE_MEDIA_* credentials and a dedicated private test bucket.
The script uploads a tiny synthetic MP4-signature fixture, downloads it using a
five-minute signed URL, compares bytes, and deletes the object even on failure.
It does not call LTX or incur video-generation charges.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import httpx

import media_storage


async def main() -> None:
    if os.getenv("NENE_ENV", "").strip().lower() == "production":
        raise SystemExit("Refusing to run storage smoke test with NENE_ENV=production.")
    cfg = media_storage._storage_config()
    client = media_storage._client()
    generation_id = str(uuid.uuid4())
    key = f"generations/{generation_id}/video.mp4"
    # Minimal ISO BMFF signature fixture: tests storage transport, not playback.
    fixture = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24
    uploaded = False
    try:
        client.put_object(
            Bucket=cfg["bucket"],
            Key=key,
            Body=fixture,
            ContentLength=len(fixture),
            ContentType="video/mp4",
            CacheControl="private, no-store",
            Metadata={"generation-id": generation_id, "smoke-test": "true"},
        )
        uploaded = True
        url = media_storage.create_signed_download_url(key, 300)
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0), follow_redirects=False, trust_env=False) as http:
            response = await http.get(url)
        if response.status_code != 200:
            raise RuntimeError(f"Signed download failed with HTTP {response.status_code}.")
        if response.content != fixture:
            raise RuntimeError("Downloaded bytes differ from uploaded fixture.")
        print("PASS: staging object upload, five-minute signed GET, and byte-for-byte verification.")
        print("NOTE: confirm bucket public-access block, policy, encryption, and retention in provider console.")
    finally:
        if uploaded:
            client.delete_object(Bucket=cfg["bucket"], Key=key)
            print("Temporary smoke-test object deleted.")


if __name__ == "__main__":
    asyncio.run(main())
