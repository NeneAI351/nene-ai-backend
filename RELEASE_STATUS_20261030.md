# NENE AI release work — 30 October 2026

Status: code fixes prepared locally; no public release, production deployment,
payment, account deletion or provider generation performed.

## Candidate

Backend is based on `d30e2157dc0f690807e72927b67b905692a5cf9c`
(`verify/android-readiness-20261023`), the combined candidate with paid access,
wallet/payment foundations, private media, Story, voice, account privacy and
editor export billing. These capabilities are not on backend main. Preserve
that integration when reviewing; do not deploy the small main branch as if it
contained these features.

Frontend is based on `50a47ebba644744943d4520c9716d55f9dd72120` (main).
Its newer video and Story modules already use the durable endpoints. The old
durable-frontend and Story draft branches predate these modules and conflict
with current index.html; they must not be merged blindly over the current UI.

## Fixes

- Allow authenticated browser PUT preflights from configured frontend origins.
  Story Studio saves scenes with PUT; previously CORS rejected those requests
  before the API handler could receive them. Unlisted origins remain rejected.
- Staging preflight now checks the release's wallet, payment, agreement, profile,
  reports, voice, privacy and editor receipt tables, in addition to generation
  and Story tables. Partial schema installation cannot pass the old short list.
- Frontend adds explicit in-app reports on generated videos, Story films and
  voice outputs. Reports contain an output type/ID and user description, never
  a signed media URL. They use authenticated existing abuse-report intake.
  Request IDs survive lost responses and reopening; account changes close the
  form. Receipt text distinguishes recorded intake from moderator review.
- Frontend cache version advances to 48 and includes the reporting module.

## Validation

- Backend: 293 passed, 67 skipped without a disposable PostgreSQL instance.
  The skipped database tests are unverified by this local run.
- New CORS test exercises browser OPTIONS behavior for both the configured
  origin and an untrusted origin. Preflight regression tests check that missing
  payment/privacy/editor/reporting tables fail and database connections close.
- Frontend reporting tests cover explicit submission, content-safe rendering,
  lost-response retries, duplicate taps, account switching and failed receipts.
- Live staging `/api/ready` returned `ready: false`, with database, authentication,
  shared rate limits and private storage configuration true, but LTX provider
  configuration false. These booleans do not prove dependency connectivity.
  This supersedes the older report that the four other settings were absent.

## Remaining external and release gates

1. Review/push both candidates and run PostgreSQL CI on their exact commits.
2. Connect the existing Render staging database, verified Supabase auth,
   TLS Redis, private storage and provider settings using
   STAGING_CONNECTION_SETUP.md. Keep paid generation disabled until the controlled
   test and spend limit are agreed. Verify supervised generation/assembly workers.
3. Test a real account, payment test checkout/webhook, wallet fulfilment,
   generation settlement, private playback/download and restart recovery.
4. Verify real account erasure (identity, database and storage), privacy enquiry
   handling and provider erasure follow-up. Existing deletion code is not live
   deletion evidence.
5. Monitor `support_reports` kind `abuse` in the owner's trusted database console.
   Review `received` reports, investigate the referenced owned output, apply
   needed provider/account/content action and record `reviewing`/`closed` status.
   Do not describe report intake as automated moderation or staff notification:
   the endpoint reports staff_notification_enabled=false. Reports and their
   handling must be verified on staging before declaring this release gate met.
6. Prepare and sign an Android AAB. No Android project/signing configuration or
   binary was found in the inspected repositories. Test media codecs, preview,
   mixed audio, export, installation and downloads on a real Android phone.
7. Implement and verify Google Play Billing before opening Android credit sales;
   Paystack web checkout is not evidence of Play purchase support.
8. Complete Play Console verification, final-binary privacy/Data Safety, AI
   reporting, content rating, store assets and applicable testing requirements.

The app remains unsuitable for public paid release until these gates pass.
