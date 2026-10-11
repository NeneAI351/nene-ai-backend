# NENE AI Production Generation and Monetization Gate

This document is an engineering release gate, not a claim that production generation is ready.

## Current release posture

- Keep production generation disabled until the end-to-end flow below is implemented and verified.
- Keep the wallet ledger as the source of truth for credits. Never trust a price, user ID, provider, plan, or credit amount supplied by the client.
- Keep the backend security PR in draft until review and staging verification are complete.
- A passing CI suite validates only the cases it runs; it does not establish million-user capacity.

## Implementation status on the feature branch

Implemented but not yet production-enabled:
- `POST /api/generations` authenticates the owner, applies shared rate limiting, recalculates the quote server-side, and creates the generation, reservation, ledger rows, idempotency record, and dispatch outbox row in one PostgreSQL transaction.
- The durable generation flow currently accepts only the LTX provider adapter. Other providers are rejected rather than silently routed to a provider with different cost semantics.
- `generation_worker.py` is a separate-process worker for LTX dispatch and status reconciliation. Run it as a distinct service/process with the same database configuration and the LTX API key.
- A worker restart during an in-flight submission transitions the job to `submission_unknown`; the worker does not blindly submit again.
- Terminal credit settlement is retried through the outbox if a worker crashes between the generation status update and the wallet transaction. Wallet operation keys make capture/release retries idempotent.
- Unit tests cover worker restart ambiguity, terminal settlement amounts, and successful dispatch-event acknowledgement. PostgreSQL integration tests cover atomic generation reservation and idempotency.

Known limitations that still block production:
- A submission timeout without a returned provider job ID cannot be automatically resolved unless LTX offers a documented lookup/idempotency mechanism for that request. The job retains its reservation and requires reconciliation; it is never blindly resubmitted.
- Completed LTX output is downloaded without following redirects, constrained by a configured host allowlist in production, checked for public DNS resolution, bounded to a configurable maximum size, and checked for an MP4 signature before upload to private S3-compatible object storage. The database stores the object key and media metadata, not the provider URL. Authenticated `GET /api/generations/{generation_id}/media-url` returns a five-minute signed URL only for the owner of a succeeded generation. This is baseline validation, not malware scanning or full media transcoding/inspection.
- The worker has not been deployed as a supervised service, and provider contract tests against a controlled staging account have not yet been run.
- The frontend durable Text → Video and Image → Video paths now use NENE generation IDs and submit private `source_image_id` values; the legacy Story scene flow remains on the old provider-job contract.
- Payment checkout, verified payment webhooks, subscriptions, and real-money credit fulfillment remain disabled/not implemented.

## Private media storage configuration

Configure these only in the worker/API secret manager; never commit credentials:
- `NENE_MEDIA_BUCKET`: private bucket name.
- `NENE_MEDIA_REGION`: storage region (use the provider's required region value).
- `NENE_MEDIA_ENDPOINT`: optional HTTPS S3-compatible endpoint (for example, an S3-compatible provider endpoint); omit for AWS S3.
- `NENE_MEDIA_ACCESS_KEY_ID` and `NENE_MEDIA_SECRET_ACCESS_KEY`: least-privilege credentials scoped to the `generations/` and `source-images/` prefixes only; source image objects are stored under `source-images/{user_id}/{asset_id}.{ext}`.
- `ALLOWED_MEDIA_HOSTS`: comma-separated exact provider media hostnames or provider-owned parent domains. Required in production; keep the list narrow and provider-documented.
- `NENE_MAX_VIDEO_BYTES`: optional max downloaded output size; default 500 MiB.

Keep the bucket private, block public access, enforce TLS, use server-side encryption, and configure lifecycle/retention/deletion rules. Signed download URLs expire after five minutes. The API endpoint checks authenticated ownership and successful completion before signing. Do not put signed URLs in logs, analytics, database records, or long-lived frontend state. Provider redirects are intentionally rejected. Production `/api/image-upload` stores validated JPEG/PNG/WEBP files privately and returns an owner-scoped `source_image_id`, not a public URL. Durable generation requests validate that asset belongs to the authenticated owner; the worker mints a short-lived provider-fetch URL only immediately before dispatch. Public `/api/temp-images/{name}` is disabled in production. The database stores object keys and metadata, never the signed source URL. Configure retention and deletion for source images as well as completed videos.

### Staging storage smoke test

Run only against a dedicated staging bucket with staging credentials. Do not set `NENE_ENV=production`:
```bash
python scripts/media_storage_smoke_test.py
```
This test uploads a tiny synthetic MP4-signature fixture, obtains a five-minute signed GET URL, verifies downloaded bytes, and deletes the temporary object. It does not call the video provider or incur generation charges. A passing round trip verifies connectivity and signed delivery only; an operator must separately verify that public bucket access is blocked, the bucket policy is least-privilege, encryption is enabled, and lifecycle/retention is configured. Do not paste credentials or signed URLs into issues or logs.

## Required request lifecycle

1. Authenticate the caller and derive the user ID from the verified token.
2. Validate and normalize the request. Resolve the provider/model server-side; do not use automatic provider fallback after quoting/reserving unless the fallback is explicitly re-quoted and safely re-reserved.
3. Calculate a versioned server-side quote from a provider cost/rate card. Do not charge from estimated pricing until the estimate is reviewed and a margin guard is applied.
4. Atomically claim an idempotency key scoped to the authenticated user and bind it to a canonical request fingerprint. A key replay with different parameters is a conflict. A pending replay returns the existing generation; it never starts another provider submission.
5. Persist the generation and durable dispatch/outbox intent before any provider call. A worker claims dispatch work with a lease so multiple instances cannot submit the same item concurrently.
6. Reserve the quoted credits using a stable wallet operation key. If the reservation fails, mark the generation/request failed without contacting a provider.
7. Mark the submission attempt before calling the provider. If the call times out or its outcome is ambiguous, mark `submission_unknown`, retain the reservation, and reconcile with the provider before any retry. Never blindly repeat a non-idempotent provider call.
8. Persist the provider job ID and transition to submitted only after confirmed acceptance. Poll asynchronously with bounded concurrency, exponential backoff, jitter, and provider-specific rate limits.
9. On confirmed success, capture the final billable amount and release any unused reservation exactly once. On confirmed definitive failure, release the reservation exactly once. Do not treat an unknown outcome as a failure.
10. Store completed media in durable object storage, scan/validate it, and deliver through short-lived signed URLs or an authenticated media endpoint. Never expose provider credentials, raw provider payloads, or permanent private provider URLs.
11. Return only the NENE generation ID and safe status fields to clients. Poll `GET /api/generations/{generation_id}`; do not require clients to know provider job IDs.
12. Emit structured audit events for quote, reservation, provider submission, reconciliation, capture/release, and delivery. Never log secrets, authorization headers, signed URLs, or full sensitive prompts by default.

## Reliability and scale requirements

- PostgreSQL: managed HA, backups/PITR, migration discipline, bounded connection pools, indexes verified with query plans, and load-tested pool limits.
- Queue: durable queue/outbox with at-least-once delivery; idempotent consumers; leases/visibility timeout; dead-letter handling; retry budgets; reconciliation for stale jobs.
- API: stateless horizontally scaled instances, shared rate limiting, request-size/time limits, per-user and per-IP quotas, and provider concurrency budgets.
- Workers: independent autoscaling by queue depth and provider limits; do not scale only on CPU.
- Storage/CDN: object storage for output, lifecycle/retention policy, signed delivery, egress controls, and user ownership checks.
- Operations: metrics for queue age, time-to-first-progress, completion/failure rate, provider spend, gross margin, refunds/corrections, idempotency conflicts, and stuck/unknown submissions; actionable alerts and runbooks.
- Security: ownership checks on every resource access, secret manager for provider credentials, least-privilege database roles, abuse controls, audit retention, and incident response.
- Capacity: run staged load tests for expected peak concurrency, long-running generation behavior, database saturation, provider quotas, queue backpressure, and recovery after worker/API restarts. Do not advertise million-user capacity until measured results support it.

## Monetization controls

- Keep a free tier with explicit daily/monthly quotas and low-cost models; protect it against account farming and automated abuse.
- Offer paid credit packs and/or subscriptions only after payment webhooks are signature-verified, replay-safe, and reconciled against provider transaction IDs.
- Credit grants must be ledger entries in an atomic, idempotent transaction. No client-side credit grants.
- Enforce margin floors by model, duration, resolution, and provider. Disable a model automatically if its verified cost exceeds its configured ceiling or the rate card is stale.
- Show the user the expected credit cost before generation and the final charged amount after completion. Explain when an estimate can change.
- Keep a clear policy for failed generations, duplicate/unauthorized charges, billing errors, and legal consumer rights. Never promise a blanket no-refund policy that conflicts with applicable law.
- Track gross margin per generation, paid conversion, repeat usage, failed-job cost, support/refund rate, and free-to-paid conversion. Growth is not healthy if provider spend grows faster than collected revenue.

## Release gates (all required)

- [ ] Provider selection and pricing are deterministic and server-side.
- [ ] User-scoped durable idempotency is integrated into the production endpoint.
- [ ] Generation creation, wallet reservation, and dispatch intent have crash-recovery behavior.
- [ ] Provider ambiguity is reconciled without duplicate jobs or duplicate charges.
- [ ] Success/failure settlement and unused-credit release are idempotent under concurrency.
- [ ] Status polling is owner-scoped and never leaks provider internals.
- [ ] Output media is durably stored and safely delivered. The private-storage implementation is present on the feature branch; bucket configuration, staging verification, retention/scanning, and delivery integration remain release gates.
- [ ] PostgreSQL integration tests cover duplicate requests, concurrent reservations, ownership, transitions, crash points, and settlement.
- [ ] Provider contract tests and staging smoke tests pass with controlled spend.
- [ ] Load tests, alerts, rollback plan, backup restore test, and cost/margin review are complete.
- [ ] Frontend uses NENE generation IDs and displays progress, cost, and actionable failure states.
- [ ] Human review approves the security, privacy, terms, and market-specific payment/refund obligations.

Until all gates are checked with evidence, production generation stays fail-closed.
