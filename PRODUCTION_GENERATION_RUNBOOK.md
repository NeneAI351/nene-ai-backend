# NENE AI Production Generation and Monetization Gate

This document is an engineering release gate, not a claim that production generation is ready.

## Current release posture

- Keep production generation disabled until the end-to-end flow below is implemented and verified.
- Keep the wallet ledger as the source of truth for credits. Never trust a price, user ID, provider, plan, or credit amount supplied by the client.
- Keep the backend security PR in draft until review and staging verification are complete.
- A passing CI suite validates only the cases it runs; it does not establish million-user capacity.

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
- [ ] Output media is durably stored and safely delivered.
- [ ] PostgreSQL integration tests cover duplicate requests, concurrent reservations, ownership, transitions, crash points, and settlement.
- [ ] Provider contract tests and staging smoke tests pass with controlled spend.
- [ ] Load tests, alerts, rollback plan, backup restore test, and cost/margin review are complete.
- [ ] Frontend uses NENE generation IDs and displays progress, cost, and actionable failure states.
- [ ] Human review approves the security, privacy, terms, and market-specific payment/refund obligations.

Until all gates are checked with evidence, production generation stays fail-closed.
