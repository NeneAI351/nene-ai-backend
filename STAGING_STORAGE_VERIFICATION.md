# NENE AI — Staging Private Media Verification

Status: **not yet verified against a live bucket**. This checklist is evidence collection for the backend draft PR, not production approval.

## Safety boundary

- Use a dedicated, empty-or-disposable **staging** bucket and staging-only credentials.
- Never use production credentials, production buckets, customer assets, or a production worker.
- Do not paste access keys, secret keys, bearer tokens, presigned URLs, or full environment dumps into GitHub, chat, CI logs, or screenshots.
- The script uploads two tiny synthetic fixtures and deletes them. It does not call LTX or create billable video jobs.
- Keep the backend generation-create feature flag disabled during this check. Do not merge or deploy this draft as part of the test.

## 1. Confirm the bucket and identity in the provider console

Before running the script, record these facts in the release evidence (values may be redacted):
- Environment/account/project is staging.
- Bucket name is the dedicated staging bucket.
- Public access is blocked at the account and bucket levels where supported.
- Anonymous requests cannot list or read either `generations/` or `source-images/`.
- Credentials are scoped to the required bucket and prefixes only; no account-wide administrator credential is used.
- Server-side encryption is enabled and the provider's TLS-only policy is active.
- Lifecycle/retention and deletion expectations are documented for both prefixes.
- The bucket's CORS policy is not being used as a substitute for private access control. Browser clients should receive only short-lived signed delivery URLs.

Do not record secret values. If any check cannot be confirmed, mark it **BLOCKED**, not passed.

## 2. Run the controlled round-trip test

Run from a trusted local/staging operator environment with the backend dependencies installed. Confirm no hosted/production environment markers are present.

Set the variables in your shell or secret manager (do not put actual values in this document):
- `NENE_ENV=staging`
- `NENE_MEDIA_BUCKET` to the dedicated staging bucket
- `NENE_MEDIA_REGION` to the provider-required region
- `NENE_MEDIA_ACCESS_KEY_ID` and `NENE_MEDIA_SECRET_ACCESS_KEY` to the staging-only identity
- `NENE_MEDIA_ENDPOINT` only when using an S3-compatible non-AWS endpoint; it must be HTTPS
- `NENE_MEDIA_SMOKE_TEST_CONFIRM=staging-only`

Then run:

```bash
python scripts/media_storage_smoke_test.py
```

Expected evidence:
1. Upload to `generations/{generation_uuid}/video.mp4` succeeds.
2. Upload to `source-images/{owner_uuid}/{asset_uuid}.png` succeeds.
3. Object metadata reports the expected content type and `private, no-store` cache policy.
4. Five-minute signed GET works for both objects, with redirects disabled by the test client.
5. Downloaded bytes exactly match the uploaded fixture.
6. Both temporary objects are deleted, and a follow-up HEAD confirms absence.
7. The script exits successfully. If cleanup fails, treat the run as failed and manually remove only the uniquely named smoke-test objects after verifying their keys.

The synthetic fixtures test storage transport, metadata, and signed access—not MP4 playback, image decoding, malware scanning, or transcoding.

## 3. Negative access checks

Using a clean unauthenticated client (no cloud credentials and no signed query string), try to read one test object while it exists, if the provider supports a controlled negative test. It must be denied. Do not copy a signed URL into logs or evidence.

If the provider's console offers a bucket-policy/access analyzer, use it to confirm anonymous access is blocked and the worker identity cannot access unrelated buckets or prefixes. The smoke script's success alone does **not** prove public access is blocked or IAM is least-privilege.

## 4. Record the result

Record only non-secret facts:
- Date/time and operator
- Staging provider and bucket alias (redact sensitive account identifiers)
- Script commit SHA and test result
- Signed GET for video: pass/fail
- Signed GET for source image: pass/fail
- Byte equality for both: pass/fail
- Cleanup and absence verification for both: pass/fail
- Public-access block, encryption, TLS, least privilege, and retention: pass/fail/blocked
- Sanitized error class/message if anything failed (never a signed URL or credential)

Do not mark the storage gate complete until all required console checks and the live round trip have evidence.

## 5. After storage verification

Storage success is only one release gate. Still required: controlled LTX submission/status/reconciliation tests; prove unknown submissions are not duplicated; supervised worker deployment in staging; authenticated ownership checks; stale outbox/lease and settlement recovery tests; media deletion/retention behavior; malware/transcoding review; load tests and alerts; backup/restore; security, privacy, terms, and applicable payment/refund review.

Production generation and real-money payments remain disabled until every required gate is independently reviewed and approved.
