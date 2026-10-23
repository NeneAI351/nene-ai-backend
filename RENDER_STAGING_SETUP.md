# NENE AI — Safe Render Staging Setup

Status: **operator checklist; no separate Render staging service has been verified yet.** Do not treat this document as evidence that staging is deployed or that storage works.

This checklist is for a beginner setting up a staging service without changing the existing live `nene-ai` service.

## Safety rules

- The existing Render service shown in the dashboard is named `nene-ai`, deploys branch `main`, and is marked **Live**. Treat it as the existing live service. Do not add staging S3 credentials to it.
- Keep the existing service unchanged. Do not press Manual Deploy, Rollback, or change its branch while setting up staging.
- Use a separate Render web service for staging, with a clear name such as `nene-ai-staging`.
- Use only the `feature/wallet-v1-foundation` branch for the staging service until the draft pull request is reviewed. Do not merge the draft PR just to deploy staging.
- Staging must use staging-only database credentials and staging-only provider credentials. Never copy production secrets into staging. Do not enable paid generation until an explicit spend cap and provider test plan are in place.
- Supabase S3 keys can access all buckets in their Supabase project and bypass Storage RLS. Keep the key only in the staging service's server-side environment settings. Never put it in GitHub, frontend settings, chat, or screenshots.
- Do not set `NENE_MEDIA_SMOKE_TEST_CONFIRM` on a deployed service. The smoke-test script intentionally refuses detected hosted environments.

## 1. Create a separate staging service

1. In Render, leave the existing `nene-ai` service alone.
2. From the Render workspace, choose **New** and then the option to create a **Web Service**.
3. Connect/select the existing repository `NeneAI351/nene-ai-backend`.
4. Give the new service a clear name: `nene-ai-staging`.
5. Select branch `feature/wallet-v1-foundation`.
6. Use the same runtime, build command, and start command as the existing backend service. To avoid guessing, read those values from the existing service's **Settings** and copy only the commands/settings—not its secret environment variables.
7. Before creating/deploying, double-check the service name and branch. It must say `nene-ai-staging` and `feature/wallet-v1-foundation`, not `nene-ai` and `main`.

If Render offers to deploy before the environment is reviewed, do not add provider credentials or real user traffic. A staging service must be configured with separate staging database settings before it is considered ready.

## 2. Configure staging secrets only on the new service

In the new `nene-ai-staging` service, open its environment-variable settings and add values only after verifying that each resource is staging-only.

Storage variables for the private Supabase staging bucket:

- `NENE_ENV=staging`
- `NENE_MEDIA_BUCKET=nene-media-staging`
- `NENE_MEDIA_REGION=eu-west-2` (confirmed from the Supabase S3 dashboard screenshot; re-check the dashboard if it changes)
- `NENE_MEDIA_ENDPOINT=<exact HTTPS endpoint copied from Supabase Storage → S3>`
- `NENE_MEDIA_ACCESS_KEY_ID=<staging S3 access key ID>`
- `NENE_MEDIA_SECRET_ACCESS_KEY=<staging S3 secret key>`

Additional requirements before calling the service usable:

- Configure a **staging database**, not the live database.
- Confirm authentication, Redis/shared rate-limit settings, and other required variables are staging-specific and available.
- Do not enable production mode, live payments, or paid provider work.
- Do not set `NENE_MEDIA_SMOKE_TEST_CONFIRM` in Render. It is a local operator confirmation for the dedicated smoke-test run only.

Render may restart/redeploy the new service after saving environment settings. This is expected for the new staging service; never apply these changes to the existing live service.

## 3. Verify the staging service before connecting real traffic

1. Wait for the new service's deploy to finish.
2. Check the deploy logs for startup errors; do not share logs containing credentials.
3. Verify the staging health endpoint and confirm it is the **staging** URL.
4. Verify that the database points to staging and that no live provider credentials are configured.
5. Keep access restricted while storage and integration tests are pending.

A green Render deploy alone does not prove the S3 connection works.

## 4. Run the private-storage smoke test from a trusted operator environment

Use a trusted local operator environment with the staging-only S3 credentials, not a deployed Render Shell. The smoke-test script deliberately refuses to run when it detects Render or other hosted environments.

After verifying the credentials and dedicated private bucket, set the required environment variables in that local environment, including:

```text
NENE_MEDIA_SMOKE_TEST_CONFIRM=staging-only
```

Then, from the backend repository with requirements installed, run:

```bash
python scripts/media_storage_smoke_test.py
```

The test uses synthetic fixtures only; it does not call LTX or create video jobs. It checks upload, metadata, signed downloads, byte equality, and cleanup for both media prefixes. Do not paste secrets or signed URLs into chat or logs.

If there is no trusted local operator environment available, stop and arrange one rather than bypassing the script's hosted-environment guard.

## 5. Privacy verification and release gate

- Confirm `nene-media-staging` remains private.
- Verify an unauthenticated read is denied; do not use a signed URL for this check.
- Confirm only the staging backend has the staging S3 key.
- Confirm the smoke test passes and leaves no test objects behind.
- Record the commit SHA and environment with the test result.
- Keep the backend pull request in draft and production generation/payments disabled until all release gates have evidence.
