# Magic Hour video backup

LTX remains the primary video engine. Magic Hour uses the explicitly pinned
MiniMax H3 model for text and image video, 6/8/10 seconds at 720p or 1080p.
Image video matches the source image aspect ratio. Talking-character and Story
Studio remain on their existing LTX flow; they do not silently switch engines.

The backup is disabled by default. Code tests use mocked provider responses;
no real Magic Hour generation has been verified or purchased.

Before activation, configure BOTH the API and generation worker:

- MAGIC_HOUR_API_KEY: server secret; never put in frontend files.
- NENE_MAGIC_HOUR_CREDIT_USD: actual USD cost per provider credit for this account.
  Include the paid plan cost when budgeting; credit packs alone may not unlock
  the required resolutions. There is deliberately no assumed default cost.
- ALLOWED_MEDIA_HOSTS: preserve required LTX hosts and add videos.magichour.ai.
- NENE_MAGIC_HOUR_VIDEO_ENABLED=true only after account/model access, pricing,
  private input/output, successful/failed job settlement and recovery are tested.

The generation worker must be running and explicitly enabled. The existing
Render worker blueprint has not been applied and its paid compute is not
provisioned by this change. Add the Magic Hour variables to that worker before
activation. NENE_DURABLE_GENERATION_CREATE_ENABLED remains the overall API lock.
NENE_LTX_VIDEO_ENABLED=false can exclude LTX from NEW job availability; it does
not move accepted jobs to another provider.

Every selection requires a fresh server price review. Durable retries retain
provider, model and idempotency key. An unknown POST outcome is reconciled or
held for investigation; no second provider purchase is attempted. Outputs pass
the existing download validation and private storage flow before credit capture.

Provider contract references:
https://docs.magichour.ai/api-reference/models
https://docs.magichour.ai/api-reference/video-projects/text-to-video
https://docs.magichour.ai/api-reference/video-projects/image-to-video
https://docs.magichour.ai/integration/inputs-and-outputs

# Moving staging to production NENE AI

Promote the reviewed, tested release to the production service; do not replace
production with an unchecked branch or copy staging secrets wholesale. Inspect
current production data and configuration first, take a database backup, apply
compatible migrations, configure production auth/origins/private storage/live
billing and provider credentials, and verify a rollback path. Point the released
frontend at production only after smoke tests succeed.

Test orders and test credits must never become live spending credit. Confirm
whether environments share any database/auth/storage before planning account
migration; this change does not assume that they are isolated. Play Store billing
and release signing remain separate release requirements. No production switch,
live payment activation, paid worker or provider spending is performed here.
