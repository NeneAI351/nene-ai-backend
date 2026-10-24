# Script-to-voice launch preparation

The same supervised `generation_worker.py` handles voice and video jobs. Voice creation uses Magic Hour's documented `POST /v1/ai-voice-generator`; animation uses LTX V2 audio-to-video. No voice cloning is implemented. The UI identifies the selectable presets as AI voices.

Before enabling paid voice creation:

- Fund and configure Magic Hour (`MAGIC_HOUR_API_KEY`) as well as LTX (`LTX_API_KEY`).
- Confirm the actual Magic Hour USD cost per provider credit and set `NENE_MAGIC_HOUR_CREDIT_USD`. Quotes otherwise use the existing $0.0025 planning reference, and creation stays disabled without explicit configuration.
- Include the actual returned Magic Hour audio CDN in `ALLOWED_MEDIA_HOSTS` alongside the existing LTX result hosts. Downloads validate DNS/public addresses, reject redirects and cap bytes. Do not disable these checks.
- Verify private S3 storage, the database, paid wallet and the supervised worker. API and worker initialize speech asset schema. Audio normalization uses the bundled decoder if system FFmpeg is absent.
- `NENE_DURABLE_GENERATION_CREATE_ENABLED` and the additional `NENE_TTS_CREATE_ENABLED` must both be true. Keep them false until supervised funded testing.

User flow: review voice credits -> generate voice -> listen -> approve voice -> review video credits -> animate. Neither approval nor status polling submits another paid voice. Both paid submissions retain their own owner-scoped request key and exact input. An uncertain response or worker crash never triggers an automatic replacement submission. Missing provider IDs require operator reconciliation.

The synthesized file is stored privately without silently cutting the script. Audio from 0.1–60 seconds can be previewed; only 2–10 seconds can currently animate a character. Longer or shorter speech must be revised explicitly. A completed voice remains separately paid even if the user does not proceed to video. Failed video jobs use the existing video credit release rules; they do not regenerate or charge for the approved voice again.

Funded acceptance test: a short script containing a name and punctuation; listen for wording/pronunciation, approve, animate, inspect audible speech and mouth alignment, retry each submission with the same key, restart the worker/API and refresh both private outputs. Check voice and video ledger entries separately. Mocked provider tests cannot establish real speech or lip-sync quality.
