"""One opt-in provider check using a synthetic photo, with no wallet access."""
import logging
import os
import struct
import zlib

import httpx

import database
from photo_design_provider import PhotoEditError, edit_photo

logger = logging.getLogger(__name__)
STAGING_ISSUER = "https://agchmegopyktwofcupny.supabase.co/auth/v1"
STAGING_DB_REF = "agchmegopyktwofcupny"
RUN_ID = "photo-design-outfit-smoke-v1"
PRODUCTION_RUN_ID = "photo-design-production-outfit-smoke-v1"
PRODUCTION_SERVICE = "srv-daffnvdg1s2s73eclr9g"


def shirt_fixture():
    """Create a small synthetic input so no customer photo leaves staging."""
    width, height = 512, 512
    background, shirt = bytes((240, 238, 234)), bytes((194, 42, 49))
    rows = []
    for y in range(height):
        if 125 <= y < 225:
            left, right = 90, 422
        elif 225 <= y < 400:
            left, right = 162, 350
        else:
            left = right = 0
        rows.append(b"\x00" + background * left + shirt * (right - left) + background * (width - right))

    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


async def run_once(*, production=False):
    if database._pool is None:
        return

    environment = os.getenv("NENE_ENV", "").strip().lower()
    if production:
        if (
            os.getenv("NENE_PHOTO_PRODUCTION_SMOKE") != PRODUCTION_RUN_ID
            or os.getenv("RENDER_SERVICE_ID") != PRODUCTION_SERVICE
            or environment == "staging"
            or STAGING_DB_REF in os.getenv("DATABASE_URL", "")
            or os.getenv("NENE_PHOTO_DESIGN_ENABLED") == "true"
        ):
            return
        run_id = PRODUCTION_RUN_ID
        label = "production"
    else:
        if (
            os.getenv("NENE_PHOTO_STAGING_SMOKE") != RUN_ID
            or environment != "staging"
            or os.getenv("AUTH_ISSUER", "").rstrip("/") != STAGING_ISSUER
            or STAGING_DB_REF not in os.getenv("DATABASE_URL", "")
        ):
            return
        run_id = RUN_ID
        label = "staging"

    async with database._pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (run_id,))
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS photo_design_staging_smoke (
                    run_id TEXT PRIMARY KEY,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                ALTER TABLE photo_design_staging_smoke ENABLE ROW LEVEL SECURITY;
                REVOKE ALL ON photo_design_staging_smoke FROM PUBLIC;
            """)
            claim = await (await conn.execute(
                "INSERT INTO photo_design_staging_smoke(run_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING run_id",
                (run_id,),
            )).fetchone()
    if not claim:
        return

    async def report_response(response):
        if not response.is_success:
            logger.warning("Photo Design %s smoke provider status=%s", label, response.status_code)
            return
        await response.aread()
        usage = response.json().get("usage") or {}
        logger.info("Photo Design %s smoke input_tokens=%s output_tokens=%s",
                    label, usage.get("input_tokens"), usage.get("output_tokens"))

    try:
        async with httpx.AsyncClient(event_hooks={"response": [report_response]}) as client:
            edited = await edit_photo(client, shirt_fixture(), "Change the red shirt into a navy blazer. Keep the plain background.")
        logger.info("Photo Design %s smoke succeeded: validated WebP, bytes=%s", label, len(edited))
    except PhotoEditError:
        logger.warning("Photo Design %s smoke failed; no app credits charged", label)
