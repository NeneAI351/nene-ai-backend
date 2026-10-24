# NENE AI privacy and erasure operations

Public URLs: `/privacy.html`, `/delete-account.html`, `/privacy-help.html` on the frontend origin. In-app Profile -> Privacy & Safety has deletion and privacy-help links. The same deletion resource works without installing the app. Populate the Google Play privacy-policy and account-deletion URL fields with these exact deployed URLs; this code cannot submit the Play Console Data Safety form.

## Erasure capability and deployment

`GET /api/privacy/availability` is a safe readiness check. Automatic erasure requires private media storage and either (a) DELETE privilege on the same trusted database's Supabase `auth.users` table, or (b) a server-only `SUPABASE_SERVICE_ROLE_KEY` with `AUTH_ISSUER` pointing to the same Supabase project. Never put that key in frontend configuration, messages or logs. No new database privilege is granted by this feature. If capability is absent, the automatic request returns 503 before disabling an account, and the public privacy-help form still records a request for verified operator handling.

The API starts a durable cleanup consumer, so no extra paid worker is needed. PostgreSQL receipts, fenced claims, a two-minute quiet period and per-account worker locks allow restart/retry. Generation and Story workers must be updated together: they stop work for deleting accounts and hold shared lifecycle locks during provider/storage operations. Erasure holds the exclusive lock. Test storage failure, identity deletion failure and process restart before enabling a public launch. Monitor queued receipts and `last_error`; do not assume a queued request is fulfilled.

The sign-in token is verified cryptographically for self-service deletion, but account-setup terms are not required. Old tokens are blocked by an irreversible account-ID digest before any local user row can be recreated. The receipt endpoint reveals status and dates only, never an email, owner UUID or creative data.

## What is erased and retained

`GET /api/privacy/account-export` lets an authenticated active account download curated server-held account records without accepting new service terms. The response is a private, non-cacheable JSON attachment; ownership is resolved from the verified token, never a query parameter. Export projections exclude checkout authorization URLs/codes, passwords, tokens, worker internals and media access links. Video/audio/source binaries, browser-only drafts, security logs and provider-held copies are outside this download and remain available through their respective screens or a verified Privacy help request. Accounts with more than 500 records in any section or an export above 8 MiB are referred to Privacy help; no partial download is presented as complete. Optional uninstalled feature tables are listed as unavailable sections. Treat exports as personal files and keep them private.

Private source prefixes plus all owned generation/assembly prefixes are deleted before app rows are removed. Manifest IDs are obtained from owner-scoped queries, saved before destruction, and retained on retry. Never use a browser-supplied object key. `generations.user_id` uses SET NULL, so generation records are explicitly deleted before deleting the user row. Other owned rows use CASCADE. Supabase Auth identity deletion must succeed before the receipt is marked completed. Completion clears the raw user UUID and storage manifest.

An accounting-only archive keeps random payment references, processor, mode, amount/currency, transaction ID, status and dates for up to seven years. It excludes email/profile/prompt/content/session/checkout URL fields. Expired archives are purged. A late verified payment notification for an archived order must not recreate an account or grant credits. Handle any resulting refund/dispute according to mandatory payment-platform and consumer rules; account deletion does not waive those rights.

App deletion clears current-owner browser keys and legacy unscoped NENE workspace caches on the requesting browser, without deleting another account's scoped records. Other devices, files already downloaded or customer-published copies are outside server erasure.

## Service-provider follow-up and enquiries

Magic Hour project deletion is attempted with the server API key; unknown/failed deletion is retained as a manual task. LTX and other provider jobs without an implemented documented erasure API go to `privacy_provider_erasure.status='manual_review'`. Staff must submit the erasure request through the provider's official privacy/support channel, record the result, and mark the task completed only after confirmation. Never mark these tasks complete merely because local deletion finished. Signed local media links do not prove remote erasure.

Regularly review `privacy_enquiries` for privacy questions and deletion requests from people who cannot sign in. A form receipt is not identity verification and does not authorize deleting an account by email alone. Verify ownership through the existing identity provider/account-recovery process; never ask for passwords, OTP codes or payment-card details in a support message. Resolve enquiries using the supplied contact address through the operator's approved support channel, then set status `closed`; closed enquiries are purged after 90 days. This repository does not configure an operator mailbox or promise automatic email replies.

Review `privacy_deletions`, `privacy_provider_erasure` and `privacy_enquiries` in the owner's authenticated database administration console. Do not expose these tables through public PostgREST roles: RLS is enabled and PUBLIC privileges revoked. Deletion receipts and security markers are minimal operational/security records, not an active account.

For backup restoration, restore/reapply deletion security markers and rerun pending erasure manifests before allowing logins. Backups and providers can have different retention; do not claim every backup/provider copy was erased solely from a local completion receipt.

## Play Data Safety preparation

Known categories to verify against the final Android binary: account/email identifiers, optional profile info/photos, creative text/images/audio/video, purchases and support messages; security/request metadata from hosting services. Account/content is used for app functionality, account management, payment handling, security and requested AI processing. Selected providers receive the inputs needed for the user-requested task. Determine service-provider exemptions from Google's *sharing* definition using actual contracts; do not automatically tick “no sharing.” Verify transport security across deployed database/storage/provider connections and any Android SDKs before completing the form. The web interface has no advertising SDK; a future Android wrapper must be checked separately.

Privacy policy names the app and actual processors, describes retention, gives a public contact mechanism and links to self-service deletion. Check any formal business identity, age/target-audience declarations, provider contract retention and support staffing before certifying Play compliance.
