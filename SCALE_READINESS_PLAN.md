# NENE AI — Scale and Reliability Readiness Plan

Status: planning and release-gate document. This is not evidence that production capacity has been load-tested. Keep production generation and real payments disabled until the applicable gates pass.

## Objective

Build toward millions of users by proving capacity in measurable stages. Do not infer capacity from unit tests, a successful prototype, or a single successful generation. The number of registered users is not the same as concurrent video-generation jobs; capacity planning must model active users, job arrival rate, average provider latency, output size, retry rate, and storage/bandwidth consumption.

## Non-negotiable architecture rules

1. **API requests stay short.** Validate and persist a generation request, reserve credits atomically, and enqueue durable work. Do not hold an HTTP request open while a video model renders.
2. **The database is authoritative.** PostgreSQL owns generation state, idempotency, wallet ledger, source-asset ownership, and dispatch/outbox state. A queue message is a delivery mechanism, not the source of truth.
3. **At-least-once delivery must be safe.** Workers can receive a message more than once. State transitions, provider submission, credit settlement, and output attachment must be idempotent. Use fencing/leases so an expired worker cannot overwrite a newer worker's result.
4. **Never blindly retry an ambiguous provider submission.** If a timeout occurs after submission may have succeeded but before a provider job ID is recorded, reconcile with the provider using a stable request key or put the job into a visible reconciliation state. Do not submit again until duplicate risk is resolved.
5. **Bound every resource.** Apply limits to request size, image size, video size, provider timeout, job attempts, queue age, signed URL lifetime, per-user concurrency, and total concurrent provider jobs.
6. **Private media only.** Source images and generated outputs stay in private storage. Issue short-lived signed URLs only to the authenticated owner or immediately before trusted provider dispatch. Never store signed URLs as durable asset identifiers.
7. **Credits must balance.** Reserve credits before work; capture only once for a successful eligible job; release reservations for definitive failures according to policy. A transient timeout is not automatically proof of failure. Never accept payment events without verified provider signatures and idempotent fulfillment.
8. **Fail closed on unsafe production configuration.** Missing storage, provider allowlist, authentication, rate-limit backend, or required database configuration must prevent production generation rather than silently falling back to unsafe defaults.

## Capacity model to measure

Record and report, by generation mode and model/provider:

- Accepted jobs per minute and peak arrival rate.
- Queue depth and oldest queued-job age.
- Provider submission latency, render latency, polling/reconciliation latency, and end-to-end completion time (p50/p95/p99).
- Success, user-cancelled, validation-rejected, definitive-failure, timeout, and ambiguous-submission rates.
- Worker active count, restart count, lease expiry count, retry count, and dead-letter/reconciliation backlog.
- Database connection pool utilization, query latency, lock wait time, transaction failures, and connection errors.
- Storage upload/download latency, bytes stored, bytes egressed, failed transfers, and orphan-object cleanup backlog.
- Cost per successful generation and gross margin by mode/quality tier, including provider fees, retries, storage, bandwidth, and payment fees.

Do not publish a user-facing wait-time estimate until it is grounded in observed production-like measurements and a clear queue policy.

## Staged verification gates

### Gate A — deterministic correctness
- Unit and PostgreSQL integration tests pass on the exact PR head.
- Test duplicate create requests, duplicate queue deliveries, stale worker leases, worker restart, repeated settlement, and transaction rollback.
- Verify ownership checks for every generation, project, character, source image, and media-download route.
- Verify rate limiting is shared across backend instances, not only process-local.
- Verify provider callbacks/polling and state transitions cannot move terminal jobs backward.

### Gate B — staging integration
- Run the private-storage smoke test against a dedicated staging bucket using synthetic fixtures only.
- Verify anonymous/public reads are denied; signed downloads expire; uploads and deletes behave as expected.
- Run a controlled provider test with an explicit spend cap and one synthetic prompt/image. Verify the complete lifecycle from API acceptance through provider submission, output validation, private upload, database completion, and owner-only signed delivery.
- Exercise provider timeout both before and after submission may have occurred. Record the reconciliation result; do not hide ambiguous states.
- Confirm staging credentials are not present in frontend bundles, repository files, CI logs, or screenshots.

### Gate C — operational safety
- Run the worker as a supervised service with graceful shutdown, bounded concurrency, readiness/liveness checks, and recovery after restart.
- Add dashboards and alerts for API error rate, queue age, provider failures, ambiguous submissions, worker crashes, database saturation, storage failures, and spend anomalies.
- Establish on-call/incident procedures, backup and restore checks, secret rotation, retention/deletion rules, and an incident communication path.
- Verify graceful degradation: stop accepting new paid work if the provider is unhealthy or budget/queue thresholds are breached, while preserving already accepted jobs and accurate credit accounting.

### Gate D — load and resilience
Use synthetic accounts, fixtures, and staging resources. Set an explicit cost cap before every run. Increase load gradually; stop automatically on error-rate, queue-age, latency, database, or cost thresholds.

For each run record:
- exact commit SHA and environment;
- arrival rate, concurrency, duration, and payload sizes;
- provider/model and configured limits;
- pass/fail thresholds and observed p50/p95/p99;
- peak queue depth, database utilization, worker count, bytes and cost;
- errors, duplicate prevention, and recovery results.

Start with a small baseline, then test 2x and 5x observed baseline traffic, and continue only when the system remains within predeclared thresholds. This is a test sequence, not a claim that the current architecture already supports those levels. Video-model capacity and provider quotas may be the limiting factor even if the API can scale.

### Gate E — staged release
- Private internal test, then small invite-only cohort, then controlled expansion with rollback capability.
- Increase concurrency and user limits only when metrics, provider capacity, storage, and unit economics support it.
- Define SLOs from measured behavior before broad launch. Track availability, successful completion, queue wait, and data durability separately.
- Do not claim support for millions of concurrent users until realistic load and provider-capacity tests demonstrate it.

## Initial operational guardrails

Values below must be configured from observed provider limits and tested in staging; they are not assumed production defaults.

- Per-account active-job cap and creation rate.
- Global provider concurrency cap and spend ceiling.
- Maximum queue age before the product returns a clear delayed/unavailable status.
- Bounded retry count with exponential backoff and jitter for retryable operations only.
- Circuit breaker for provider or storage failures.
- Idempotency key retention longer than the maximum client retry window.
- Periodic reconciliation for stale pending jobs, expired leases, unresolved provider submissions, and unbalanced reservations.
- Orphan media cleanup that checks database references before deleting any object.
- Data retention and user-requested deletion behavior for source images and generated media.

## Current known blockers

- The real Supabase/S3-compatible private bucket is not yet configured or smoke-tested by an operator.
- Controlled live LTX staging submission and ambiguous-submission reconciliation need evidence.
- The worker is not yet verified as a supervised deployed service.
- Load tests, monitoring/alerts, media lifecycle, security/privacy/legal review, and production operational procedures remain open.
- Payment processing and real-money credit fulfillment are not implemented. Do not accept real payments.

## Completion rule

A checklist item is complete only when there is reproducible evidence tied to a commit and environment. Documentation, unit tests, and green CI are necessary but do not substitute for live staging, load, privacy, and cost evidence.
