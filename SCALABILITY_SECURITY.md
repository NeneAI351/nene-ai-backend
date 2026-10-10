# NENE AI scale and security plan

## Current safety state

This branch is a foundation, not a claim of million-user production readiness.

- PostgreSQL wallet operations are transactional and tested.
- Authentication verifies signed JWTs through HTTPS JWKS, validates issuer/audience/expiry, and refuses browser-supplied user IDs.
- Expensive public endpoints have a shared Redis rate-limit implementation.
- Production rate limiting fails closed unless a TLS Redis URL and a strong HMAC secret are configured.
- Production CORS fails closed unless explicit web origins are configured.
- Production media downloads require an approved hostname allowlist, reject private/reserved IP resolutions, and do not follow redirects.
- Production video generation and job polling deliberately return 503 until server-side credit reservations, durable generation ownership, and recovery are connected. Do not remove these guards until those features are implemented and tested.
- No real Redis, database, identity-provider, CDN, WAF, or queue has been provisioned by this code change.

## Production environment configuration

Configure secrets only in the hosting provider's server-side environment settings, never in frontend HTML or Git.

Required for a production deployment of the protected APIs:

- `NENE_ENV=production`
- `AUTH_JWKS_URL=https://...` — the actual identity provider's JWKS endpoint.
- `AUTH_ISSUER=https://...` — exact issuer for the selected identity provider.
- `AUTH_AUDIENCE=authenticated` — only if this is the actual audience emitted by that provider.
- `DATABASE_URL=...` — private PostgreSQL connection string with TLS as supported by the chosen host.
- `REDIS_URL=rediss://...` — private/TLS Redis connection string.
- `RATE_LIMIT_HMAC_SECRET` — random secret of at least 32 bytes.
- `CORS_ALLOWED_ORIGINS=https://your-real-app-domain.example` — comma-separated exact origins, no trailing slash.
- `ALLOWED_MEDIA_HOSTS=...` — comma-separated exact provider/CDN hostnames verified from real generation results.

Do not copy the example domain names. Do not enable production mode until all required services and settings exist. This branch intentionally blocks production generation until wallet billing and ownership checks are complete.

## Architecture required for very high concurrency

Millions of registered accounts and millions of simultaneous active requests are different targets. Capacity must be measured and scaled in stages; no single app server or database can be assumed to handle either without load tests and capacity planning.

1. **Edge layer:** CDN for static frontend/media, WAF/bot protection, DDoS protection, TLS, request-size limits, and managed DNS.
2. **Stateless API tier:** multiple instances across failure domains, health/readiness checks, bounded concurrency, graceful shutdown, and autoscaling based on CPU, memory, latency, and queue depth. Do not store sessions, idempotency, or jobs only in process memory.
3. **Identity:** managed authentication with email verification, password recovery, MFA options, abuse/CAPTCHA controls, and configured auth rate limits. Keep account identity separate from roles/permissions.
4. **Shared abuse controls:** Redis-backed distributed rate limits for IP and authenticated user/account, per-plan quotas, provider spending caps, and circuit breakers. Rate limits must be tuned from load tests and abuse telemetry.
5. **Generation queue:** API validates request, reserves server-side credits, persists a durable job, then enqueues it. Separate workers process provider jobs. Use leases/visibility timeouts, retry only safe transient failures, dead-letter queues, and idempotent state transitions. Never repeat a paid provider submission merely because the client timed out.
6. **Database:** managed PostgreSQL with pooled connections, short transactions, indexed user/job queries, migrations, backups, point-in-time recovery, and restore drills. Use a pooler when horizontal app instances would otherwise exhaust database connections. Set each app instance's pool size from a total connection budget; do not give every autoscaled instance a large independent pool.
7. **Media:** object storage for uploads and generated outputs, signed expiring URLs, CDN delivery, lifecycle cleanup, malware/content checks where applicable, and no dependence on instance-local `/tmp` for user assets.
8. **Provider workers:** per-provider concurrency limits, API quotas, timeouts, bounded retries, cost telemetry, provider health, and no automatic cross-provider retry after a provider has accepted a paid job.
9. **Observability:** structured logs with request/job IDs but no tokens/secrets; metrics for p50/p95/p99 latency, 4xx/5xx, queue depth/age, DB pool saturation, Redis failures, provider failures/cost, and credit ledger mismatches. Alerts must page an owner.
10. **Recovery and security operations:** least-privilege service credentials, secret rotation, dependency updates, audit trail for admin actions, incident response plan, tested backup restoration, and regular independent penetration testing.

## Security controls that must be verified before launch

- Object-level authorization: every project, character, asset, generation, wallet and ledger lookup must include the authenticated owner in the query or prove ownership before returning/mutating data.
- Billing integrity: payment webhooks must verify provider signatures, persist event IDs, deduplicate deliveries, validate amount/currency/reference, and grant credits exactly once.
- Generation integrity: reserve credits before provider submission; store user, provider job ID, request fingerprint and status durably; capture/release once; reconcile stuck jobs and provider invoices.
- Input/egress safety: enforce request body and media size limits; allowlist media hosts; reject private/reserved IPs and redirects; guard against DNS rebinding and untrusted remote URLs.
- Browser safety: exact CORS origins, content-security policy on the frontend host, no secrets in JavaScript, and no privileged actions based on client-side state.
- API safety: per-IP and per-user rate limits, strict schema validation, safe error messages, timeouts, concurrency caps, and abuse monitoring.
- Account safety: email verification, safe recovery flows, optional MFA, bot protection, and no logging of access/refresh tokens.
- Operational safety: separate development/staging/production projects and keys; production changes reviewed and deployed gradually with rollback.

## Load-testing gates

Do not market a concurrency number until it has been measured. Run repeatable tests with realistic sign-in, job creation, polling, asset upload/download, and failure patterns. Increase load gradually and record p95/p99 latency, error rate, queue wait, database/Redis saturation, and provider cost. Test at least one instance failure, Redis/database disruption, provider timeout, and retry storm. Set a target for concurrent active users, request rate, queue backlog, and recovery time before choosing instance counts.

The first production target should be a measured, affordable cohort. Increase capacity in stages; video-generation providers' own quotas and cost limits can become bottlenecks even when NENE AI's API is healthy.
