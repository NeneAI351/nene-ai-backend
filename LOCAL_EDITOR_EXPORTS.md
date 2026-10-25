# Local editor and paid export

The editor processes imported files entirely on the device. It does not call a
video, speech, music or transcription provider. Existing API/database/wallet
operations still have ordinary hosting costs. No zero-operating-cost guarantee
is made.

## Editing

- Up to six clips, 80 MB per video, 160 MB of total source files, and a
  60-second finished timeline. Inputs can be up to three minutes before trimming.
- Reorder, remove, and trim each clip. Each clip has an original-audio volume.
- Portrait 9:16, landscape 16:9, square 1:1, and feed portrait 4:5. Choose
  proportional fit or center crop. Export is up to 720p.
- A title, manual timed captions, SRT import, burned captions, and SRT download.
  These are not automatic speech recognition or conversational AI editing.
- One imported music track and one imported voiceover, each with volume and
  timeline start. Music can loop. No bundled music licences are implied.
- A shared canvas/Web Audio engine composes the preview and real-time export.
  Recording is paused between clip loads. H.264/AAC MP4 is offered only when
  MediaRecorder advertises that exact format; otherwise WebM is clearly labelled.
  Browser capability detection is not proof that a particular phone will encode
  successfully. Playback stalls, hidden pages and cancellation stop the export.

## Ten-credit purchase

The browser renders, checks playback metadata/first-frame decoding, then commits
the Blob, canonical edit fingerprint and request key to IndexedDB. No purchase
is sent when rendering or durable storage fails. The user then reviews the
server price and explicitly confirms the fixed **10 paid credits**.

`POST /api/editor/exports/{sha256}/purchase` accepts a UUID Idempotency-Key and
credit_limit, never an owner, amount to deduct, media, caption or provider URL.
Verified authentication chooses the owner. A shared account-lifecycle lock fences
account deletion. The wallet row serializes all charges. A confirmed live-payment
balance is required; test payments and bonus credits do not qualify.

Receipt, wallet decrement, lifetime consumption, operation and ledger commit in
one transaction. Both owner/request-key and owner/edit-fingerprint are unique.
Changing a key cannot charge the same edit twice. A key reused for another edit
conflicts. An uncertain response is resolved by checking the receipt, or retrying
the same saved purchase. Receipt reads and repeat downloads never charge.

The fingerprint uses file-byte SHA256 values, ordered trims, formats, title,
captions and audio settings. Rebuilding the same edit can reuse its receipt,
including after losing the original Blob. A genuinely changed edit has a new
fingerprint and requires another explicit export purchase. Simply downloading a
video already bought through generation has no additional fee.

This is a feature entitlement, **not DRM**. A server cannot prove that a local
Blob exists, inspect local pixels, prevent screen recording, or prevent a person
from modifying their own browser code without moving rendering/delivery to a
trusted paid server. Do not describe local rendering as tamper-proof paid media.

New purchases default on when the database is ready. Set
`NENE_EDITOR_EXPORTS_ENABLED=false` to pause new purchases; existing receipts
remain retrievable. This does not enable provider generation or live payments.

## Persistence and deletion

Draft sources and three finished exports are owner-scoped in IndexedDB, not
cloud-synced. Quota failure is reported before purchase. Account changes abort
work and clear visible sources/download URLs. The editor can remove local files.
Self-service account deletion attempts to remove the owner's local IndexedDB
items on that device, and database receipts cascade with the account. Other
devices and downloaded copies require separate local removal. Account JSON
exports include receipt metadata, not local media.

## Verification

CI runs model, UI, fake IndexedDB and simulated-media tests. Real PostgreSQL tests
cover concurrent duplicate requests, request-key conflicts, test/insufficient
credits, ownership, paused purchases, rollback and account deletion. The media
tests exercise composition and state cleanup using browser-API doubles; they do
not prove phone codec quality or audio sync.

Before inviting paying users, smoke-test a real Android Chrome and iPhone:
join two contrasting clips, burn a caption, mix voice and music, crop vertical,
export, replay the entire file and post a private test. Verify duration, sound
sync, save/reopen, cancellation, quota error and repeat download. Test the fixed
charge only with owner-authorized live purchased credits; do not issue free
production credits to make a test pass. No provider jobs are needed for editor
tests.
