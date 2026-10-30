"""Non-generating staging dependency checks. Never prints credentials or URLs.

Run with PYTHONPATH=. python scripts/staging_preflight.py in staging's environment.
No migrations, uploads, payments, generation submissions, or queue consumption.
"""
import asyncio
import json
import logging
import os

import boto3
from botocore.config import Config
import httpx
from redis.asyncio import Redis

import database
import media_storage
import readiness


async def check_database():
    try:
        await database.start_database()
        if not await database.database_ping():
            return False
        # Verify the API has initialized the schemas without creating them here.
        names = ["users", "wallets", "wallet_reservations", "wallet_operations",
                 "payment_orders", "account_agreements", "account_profiles",
                 "support_reports", "generations", "generation_outbox",
                 "media_assets", "speech_assets", "director_plans",
                 "story_projects", "story_scenes", "story_assembly_jobs",
                 "privacy_deleted_subjects", "privacy_deletions",
                 "privacy_provider_erasure", "privacy_payment_archive",
                 "privacy_enquiries", "editor_export_receipts"]
        for name in names:
            row = await database.fetch_one("SELECT to_regclass(%s)::text AS name", (name,))
            if not row or not row["name"]:
                return False
        return True
    finally:
        await database.close_database()


async def check_authentication():
    async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
        response = await client.get(os.environ["AUTH_JWKS_URL"])
        response.raise_for_status()
        keys = response.json().get("keys", [])
        return any(key.get("kid") and key.get("kty") in {"RSA", "EC"} for key in keys)


async def check_rate_limits():
    client = Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=5, socket_timeout=5)
    try:
        return bool(await client.ping())
    finally:
        await client.aclose()


async def check_storage():
    cfg = media_storage._storage_config()
    def head():
        client = boto3.client("s3", endpoint_url=cfg["endpoint"], region_name=cfg["region"],
            aws_access_key_id=cfg["access_key"], aws_secret_access_key=cfg["secret_key"],
            config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=5,
                          retries={"max_attempts": 0}))
        try:
            client.head_bucket(Bucket=cfg["bucket"])
            return True
        finally:
            client.close()
    return await asyncio.to_thread(head)


async def run_checks():
    if os.getenv("NENE_ENV", "").strip().lower() != "staging":
        return {"mode": "refused", "dependencies_ready": False,
                "reason": "Run only with NENE_ENV=staging."}
    configuration = readiness.readiness_snapshot()
    output = {"mode": "staging", "configuration": configuration["checks"], "connections": {}}
    probes = [("database_configured", "database_and_schema", check_database),
              ("authentication_configured", "authentication_signing_keys", check_authentication),
              ("shared_rate_limits_configured", "redis_ping", check_rate_limits),
              ("private_media_storage_configured", "storage_bucket_access", check_storage)]
    for config_key, name, probe in probes:
        if not configuration["checks"][config_key]:
            output["connections"][name] = "missing_configuration"
            continue
        try:
            ok = await asyncio.wait_for(probe(), timeout=15)
            output["connections"][name] = "passed" if ok else "failed"
        except Exception:
            # Exception strings can contain DSNs, credentials, response bodies or
            # signed URLs. Give only a fixed diagnostic and the failed probe name.
            output["connections"][name] = "failed"
    output["dependencies_ready"] = configuration["ready"] and all(
        value == "passed" for value in output["connections"].values())
    output["provider_account_and_generation"] = "not_tested_no_provider_request"
    output["worker_supervision"] = "not_tested"
    output["storage_privacy_upload_and_delivery"] = "not_tested_head_bucket_only"
    output["real_sign_in_and_token_validation"] = "not_tested"
    return output


if __name__ == "__main__":
    # Dependency logs may include connection details. The CLI emits only its
    # fixed JSON results; inspect provider dashboards separately for diagnostics.
    logging.disable(logging.CRITICAL)
    result = asyncio.run(run_checks())
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["dependencies_ready"] else 1)
