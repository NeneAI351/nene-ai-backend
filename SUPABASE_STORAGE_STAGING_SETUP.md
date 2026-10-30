# NENE AI — Supabase Storage Staging Setup

Status: **instructions only; no storage bucket or credentials have been created or verified yet.**

This setup adapts the existing NENE AI S3-compatible `media_storage.py` implementation to Supabase Storage. Complete it only for a staging environment. Keep production generation disabled.

## Safety first

- Prefer a dedicated Supabase staging project. Supabase S3 access keys can access all buckets in their project and bypass Storage RLS; they are server-side credentials, not frontend credentials.
- Never put an S3 secret key, service-role key, database password, or signed URL in GitHub, this document, frontend code, screenshots, or chat.
- Do not use the Supabase publishable/anon key as the S3 secret key for this backend configuration.
- Confirm current project plan, quotas, and any possible usage charges in the Supabase dashboard before using storage. This guide does not promise unlimited free usage.
- Use synthetic test fixtures only. The smoke test does not call LTX or create video-generation jobs.

## 1. Create a private bucket

1. Open the Supabase dashboard and select the **staging** project.
2. Open **Storage** and create a bucket named `nene-media-staging`.
3. Ensure the bucket is **private** (not public). Do not enable public object access.
4. Do not add customer images or generated videos during the initial test.

## 2. Enable S3 protocol access

1. In the project dashboard, open **Storage → Configuration** and find the **S3** section.
2. Enable the S3 protocol connection if it is not already enabled.
3. Generate an S3 access key and secret key. Supabase shows the secret only once; store it in a trusted password manager or the backend hosting provider's secret manager.
4. Copy the S3 endpoint and region shown by the dashboard. Supabase's documented endpoint pattern is `https://<project-ref>.storage.supabase.co/storage/v1/s3`; use the exact endpoint and region displayed in the dashboard.
5. Treat these S3 keys as server-only credentials: they bypass Storage RLS and can operate across buckets in that project.

Official guides:
- https://supabase.com/docs/guides/storage/s3/authentication
- https://supabase.com/docs/guides/storage/s3/compatibility

## 3. Configure the backend staging environment

Set these environment variables in the backend staging service's secret/environment settings, not in frontend code and not in a committed file:

- `NENE_ENV=staging`
- `NENE_MEDIA_BUCKET=nene-media-staging`
- `NENE_MEDIA_REGION=<exact region shown in Supabase S3 settings>`
- `NENE_MEDIA_ENDPOINT=<exact HTTPS endpoint shown in Supabase S3 settings>`
- `NENE_MEDIA_ACCESS_KEY_ID=<staging S3 access key ID>`
- `NENE_MEDIA_SECRET_ACCESS_KEY=<staging S3 secret key>`
- `NENE_MEDIA_SMOKE_TEST_CONFIRM=staging-only`

Do not guess the region or endpoint. Use the values in the dashboard. Do not set `NENE_MEDIA_SMOKE_TEST_CONFIRM` on a deployed production service. The smoke script also refuses detected hosted/production environments.

## 4. Run the controlled storage smoke test from a trusted operator environment

From the backend repository, install the project's backend requirements and run:

```bash
python scripts/media_storage_smoke_test.py
```

The script uploads small synthetic fixtures under both `generations/` and `source-images/`, checks metadata, tests five-minute signed GET URLs, compares returned bytes, and deletes both test objects. It does not call LTX.

Expected result: all upload, metadata, signed-download, byte-comparison, cleanup, and deletion-confirmation checks pass.

If the script fails, do not repeatedly rerun it blindly. Read the sanitized error, check endpoint/region/credentials, and confirm whether any uniquely named smoke-test objects remain before retrying. Never paste secrets or signed URLs into issue comments or logs.

## 5. Independently verify privacy

The smoke test does not prove that the bucket is private or that access is least-privilege.

- Confirm the bucket remains private in the dashboard.
- While a synthetic object exists, test unauthenticated access without credentials or a signed query; it must be denied.
- Confirm only the intended backend staging service has the S3 keys.
- Record public-access denial and applicable encryption/retention settings as pass/fail/blocked.
- If the S3 access key is project-wide, do not treat it as prefix-scoped. Prefer a dedicated staging project and restrict who can access its credentials.

## 6. Release gate

Do not mark storage verification complete until the real staging round-trip passes and privacy/access checks have evidence. Keep the backend PR as draft and production generation disabled. The next independent gates still include controlled LTX submission/reconciliation, supervised worker deployment, monitoring, and security/privacy review.
