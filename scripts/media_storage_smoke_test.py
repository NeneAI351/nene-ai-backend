"""Run a controlled S3-compatible private-media round-trip smoke test.

Requires staging-only NENE_MEDIA_* credentials and a dedicated private test bucket.
The script verifies both generated-video and source-image object prefixes, signed
GET delivery, byte equality, and cleanup. It never calls LTX or incurs generation
charges. Fixtures validate storage transport/signatures only, not media decoding.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import httpx
from botocore.exceptions import ClientError

import media_storage


async def _signed_get_and_compare(http: httpx.AsyncClient, url: str, fixture: bytes, label: str) -> None:
    response = await http.get(url)
    if response.status_code != 200:
        raise RuntimeError(f"{label} signed download failed with HTTP {response.status_code}.")
    if response.content != fixture:
        raise RuntimeError(f"{label} downloaded bytes differ from uploaded fixture.")


async def main() -> None:
    if os.getenv("NENE_ENV", "").strip().lower() == "production" or any(
        os.getenv(name) for name in ("RENDER_SERVICE_ID", "K_SERVICE", "FLY_APP_NAME")
    ):
        raise SystemExit("Refusing to run storage smoke test in a detected hosted/production environment.")
    if os.getenv("NENE_MEDIA_SMOKE_TEST_CONFIRM") != "staging-only":
        raise SystemExit(
            "Set NENE_MEDIA_SMOKE_TEST_CONFIRM=staging-only after verifying these are staging credentials and a dedicated staging bucket."
        )

    cfg = media_storage._storage_config()
    client = media_storage._client()
    generation_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    video_key = f"generations/{generation_id}/video.mp4"
    video_fixture = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24
    # The production source-image validator checks file signatures. This synthetic
    # fixture intentionally tests storage transport, not full PNG decoding.
    image_fixture = b"\x89PNG\r\n\x1a\n" + b"NENE-STAGING-SMOKE-TEST"
    video_uploaded = False
    source_key: str | None = None
    try:
        client.put_object(
            Bucket=cfg["bucket"],
            Key=video_key,
            Body=video_fixture,
            ContentLength=len(video_fixture),
            ContentType="video/mp4",
            CacheControl="private, no-store",
            Metadata={"generation-id": generation_id, "smoke-test": "true"},
        )
        video_uploaded = True
        source = media_storage.upload_source_image(image_fixture, owner_id, asset_id)
        source_key = str(source["object_key"])
        expected_source_key = f"source-images/{owner_id}/{asset_id}.png"
        if source_key != expected_source_key:
            raise RuntimeError("Source-image object key is not scoped to the expected owner and asset.")
        if source["content_type"] != "image/png" or source["size_bytes"] != len(image_fixture):
            raise RuntimeError("Source-image metadata does not match the uploaded fixture.")

        video_metadata = client.head_object(Bucket=cfg["bucket"], Key=video_key)
        source_metadata = client.head_object(Bucket=cfg["bucket"], Key=source_key)
        for label, metadata, expected_type in (
            ("Generated-video", video_metadata, "video/mp4"),
            ("Source-image", source_metadata, "image/png"),
        ):
            if metadata.get("ContentType") != expected_type:
                raise RuntimeError(f"{label} object has an unexpected Content-Type.")
            if "no-store" not in metadata.get("CacheControl", ""):
                raise RuntimeError(f"{label} object is missing the private no-store cache policy.")

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        ) as http:
            await _signed_get_and_compare(
                http,
                media_storage.create_signed_download_url(video_key, 300),
                video_fixture,
                "Generated-video",
            )
            await _signed_get_and_compare(
                http,
                media_storage.create_signed_source_image_url(source_key, 300),
                image_fixture,
                "Source-image",
            )

            # The object is known to exist (verified above through authenticated
            # S3 HEAD). A raw anonymous request to Supabase's public-object route
            # must never return the fixture. This deliberately uses no API key,
            # JWT, cookies, or signed URL.
            public_object_url = (
                "https://agchmegopyktwofcupny.supabase.co/storage/v1/object/public/"
                f"{cfg['bucket']}/{video_key}"
            )
            anonymous_response = await http.get(public_object_url)
            if anonymous_response.status_code == 200 and anonymous_response.content == video_fixture:
                raise RuntimeError(
                    "SECURITY FAILURE: generated-video fixture was downloadable anonymously."
                )
            if anonymous_response.status_code == 200:
                raise RuntimeError(
                    "SECURITY FAILURE: anonymous public-object route returned HTTP 200 for a private test object."
                )
            print(
                "PASS: anonymous public-object request did not return the known-existing private video "
                f"(HTTP {anonymous_response.status_code})."
            )

        print("PASS: private generated-video upload and five-minute signed GET.")
        print("PASS: private source-image upload and five-minute signed GET.")
        print("PASS: both downloaded fixtures matched uploaded bytes.")
        print("NOTE: verify bucket public-access block, least-privilege policy, encryption, and retention in provider console.")
    finally:
        cleanup_errors: list[str] = []
        if source_key:
            try:
                media_storage.delete_source_image(source_key)
                try:
                    client.head_object(Bucket=cfg["bucket"], Key=source_key)
                except ClientError as check_error:
                    status = check_error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    code = check_error.response.get("Error", {}).get("Code")
                    if status != 404 and code not in {"404", "NoSuchKey", "NotFound"}:
                        raise
                else:
                    raise RuntimeError("Source-image object still exists after cleanup.")
                print("Temporary source-image object deleted and absence verified.")
            except Exception as exc:
                cleanup_errors.append(f"source-image cleanup failed ({type(exc).__name__})")
        if video_uploaded:
            try:
                client.delete_object(Bucket=cfg["bucket"], Key=video_key)
                try:
                    client.head_object(Bucket=cfg["bucket"], Key=video_key)
                except ClientError as check_error:
                    status = check_error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    code = check_error.response.get("Error", {}).get("Code")
                    if status != 404 and code not in {"404", "NoSuchKey", "NotFound"}:
                        raise
                else:
                    raise RuntimeError("Generated-video object still exists after cleanup.")
                print("Temporary generated-video object deleted and absence verified.")
            except Exception as exc:
                cleanup_errors.append(f"generated-video cleanup failed ({type(exc).__name__})")
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))


if __name__ == "__main__":
    asyncio.run(main())
