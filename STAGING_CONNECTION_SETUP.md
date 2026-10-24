# Connect the existing NENE AI staging service

This updates the existing `nene-ai-staging` setup; it does not create another API service or change production `nene-ai`. No credential values belong in GitHub or chat.

## Current evidence (23 October 2026)

The staging health response reports database, authentication, shared rate limiter and provider credentials absent. Private storage configuration is present but still needs a connection and privacy check. The combined candidate is in backend PR 8, with PostgreSQL CI verified at code commit `1e88c3155894a4fa7fda2b60c6eed67773d39d15`. A later documentation commit records the evidence. The candidate has not been deployed.

## 1. Identify existing resources before editing

- In Render, open the existing `nene-ai-staging` service. Record its selected branch, start command, environment variable **names only**, worker services and recent deploy status.
- Identify which Supabase project is staging and verify it is not the production database. Auth issuer, signing keys, database and the intended test-user account must belong to the intended environment.
- Reuse existing staging Redis and worker services if present. Do not provision paid resources without a reviewed plan and cost approval.
- Keep production `nene-ai` unchanged; do not copy its provider/storage secrets into staging.

## 2. API environment values

| Variable | Set in Render staging only |
| --- | --- |
| `NENE_ENV` | `staging` |
| `DATABASE_URL` | Staging PostgreSQL connection string copied from the database provider; preserve SSL parameters and URL-encode any password special characters. Never use the Supabase REST URL here. |
| `AUTH_ISSUER` | Staging Supabase project URL followed by `/auth/v1` |
| `AUTH_JWKS_URL` | Same staging project URL followed by `/auth/v1/.well-known/jwks.json` |
| `AUTH_AUDIENCE` | `authenticated` for the configured Supabase setup |
| `REDIS_URL` | Redis-compatible **connection URL using `rediss://`**, not a REST endpoint or REST token |
| `RATE_LIMIT_HMAC_SECRET` | Random staging-only secret of at least 32 bytes, generated and stored through the hosting secret manager |
| `LTX_API_KEY` | Staging/test provider account key; no paid job is authorized by this document |
| `ALLOWED_MEDIA_HOSTS` | Exact provider-documented output CDN hostnames, comma-separated; no wildcard, full URL, or invented hostname |
| `CORS_ALLOWED_ORIGINS` | Actual test frontend origins, without URL paths. For GitHub Pages, the origin is `https://neneai351.github.io`. |
| `PAYSTACK_MODE` | `test` |
| `PAYSTACK_LIVE_PAYMENTS_APPROVED` | `false` |
| `NENE_DURABLE_GENERATION_CREATE_ENABLED` | Keep `false` until controlled end-to-end testing is authorized. Deploy the staging-security fix first: the old deployed code bypasses this gate in staging. |

The candidate now applies production-grade endpoint, source-image, rate-limit and media-host protections in staging too, while health still labels the environment `staging`. Deploy and verify these protections before adding provider credentials; keep the generation-create switch disabled.

Preserve the existing staging `NENE_MEDIA_BUCKET`, `NENE_MEDIA_REGION`, `NENE_MEDIA_ENDPOINT`, `NENE_MEDIA_ACCESS_KEY_ID` and `NENE_MEDIA_SECRET_ACCESS_KEY`, after confirming their destination. Never set `NENE_MEDIA_SMOKE_TEST_CONFIRM` on the deployed service.

The current source starts the API with `uvicorn app:app --host 0.0.0.0 --port $PORT`; inspect and preserve the existing service's runtime/build settings unless a verified change is necessary. Dependency install is `pip install -r requirements.txt`.

## 3. Worker setup (separate supervised processes)

- Generation worker command: `python generation_worker.py`.
- It needs the **same staging database** and staging provider/private-storage variables as the API; its database pool is separate.
- Story assembly command, only when Story tests are being enabled: `python story_assembly_worker.py`. It requires FFmpeg/FFprobe and initialized base/wallet/Story schemas; start the API/schema initialization before the assembly worker.
- Verify each worker appears as a running supervised service with restart-on-failure. A web service alone does not run these loops.
- Do not start a worker against a queue containing unreviewed billable jobs. Inspect staging queue state and agree a provider spend cap first.

## 4. Verify without video generation

After deploying the reviewed candidate **only to staging**:

1. GET `/api/health`: confirm staging environment and configuration booleans; no secrets should appear.
2. GET `/api/ready`: incomplete staging must return HTTP 503 in the corrected candidate; configuration alone is not connectivity.
3. In staging's shell, run `PYTHONPATH=. python scripts/staging_preflight.py`. It checks database/schema, public auth signing keys, Redis PING, and S3 HeadBucket. It performs no migrations, provider submissions, payments, uploads, or queue claims. Non-staging mode is refused. Exit 1 means missing/failed dependency.
4. A storage HeadBucket pass does not prove upload permission, bucket privacy, signed URL expiry or delivery. Use the dedicated storage smoke workflow under its existing staging-only guards for those checks.
5. A signing-key pass does not prove account login. Test sign-in and an authenticated wallet request with a real staging test account next.
6. Provider-account verification and a single capped generation, worker settlement, private upload and restart playback remain separate tests. Do not mark these passed from a configuration check.

## 5. Results needed before the real journey

Record code SHA, staging service/worker names, database/schema result, auth/JWKS result, Redis result and private storage evidence. Record only booleans/sanitized errors. Never put passwords, keys, connection strings, bearer tokens or signed media URLs in chat, logs or pull requests.

This procedure does not activate live payments, buy credits, promise public capacity, or make the app store-ready.
