# LTX staging setup

Generation remains locked. Readiness on 30 October 2026 reports the LTX key
missing, and Render has no generation worker. This document does not authorize
paid resources or provider spend.

## Configuration

Use the existing staging backend and combined release branch. Keep secrets out
of Git and chat. The generation worker needs its own server environment:

- `LTX_API_KEY`: from the official LTX Developer Console, supplied securely.
- Existing database and private media storage credentials from staging.
- `NENE_ENV=staging`.
- `ALLOWED_MEDIA_HOSTS`: explicit provider output hosts verified against LTX's
  actual delivery configuration. Do not guess a broad CDN allowlist or disable
  URL validation. Staging rejects provider media downloads without this value.
- The current worker entry point is `python generation_worker.py`.

The API process does not run jobs. Provision a supervised background worker only
after agreeing its recurring cost. Story assembly is a separate workload and
must also be supervised before claiming Story completion works.

## Worker lifecycle

The worker claims one event at a time, with a 120-second lease and 30-second
renewals. Renewal requires the current token, owner and an unexpired lease.
Lease renewal failure cancels active processing; durable submitting state keeps
uncertain provider acceptance from causing a blind resubmission on reclaim.

SIGTERM/SIGINT stop new claims and allow the current event to finish. Configure
Render's shutdown delay to cover expected request/download time (up to the
platform's supported maximum, currently 300 seconds). A forced kill can still
leave an uncertain submission requiring reconciliation. Test platform shutdown
and restart on the deployed worker before enabling purchases/generation.

## Paid test boundary

Paystack test payments verify payment delivery but cannot fund paid AI work.
`paid_access.py` deliberately counts only settled live-payment purchases.
Never change the entitlement query or fabricate a live payment to bypass this
boundary. Agree the owner-funded provider test budget and an appropriate billing
path before running an actual generation.

## Verification before unlock

1. Verify key authentication without creating a video where the provider offers
   a documented non-billable endpoint. Key presence is not proof of validity.
2. Verify database, private storage and the explicit output-host allowlist from
   the actual worker environment.
3. Confirm the supervised worker is live on the reviewed commit and can renew
   claims. An API deployment alone does not verify worker operation.
4. Use a bounded, approved paid test: LTX 2.5 Fast, 720p, six seconds. Check the
   current provider price before execution; do not enable automatic top-ups.
5. Confirm one provider job, private output delivery, playback/download, wallet
   settlement and recovery after restart. Repeat-request tests must not buy a
   second video. Keep the ten-second app limit.

Official references:
- https://docs.ltx.io/quickstart
- https://docs.ltx.io/authentication
- https://render.com/docs/deploys
