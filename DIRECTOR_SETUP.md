# AI Director staging activation

Implemented: authenticated production briefs, structured scripts/scene prompts,
account-owned PostgreSQL plans, stable request keys, saved-plan retrieval, UI
error handling and explicit transfer of a scene prompt to Text → Video.
No video/audio/music/subtitle rendering or wallet debit occurs when planning.

## Deployment and activation

1. Deploy the Director backend branch only to staging. Startup creates
   `director_plans` with RLS enabled and no browser policies; all reads go through
   authenticated owner-scoped API routes. Keep production unchanged.
2. Deploy matching frontend source only after its checks pass. Its public
   `NENE_DIRECTOR_CONFIG.backendUrl` points to staging, matching staging auth.
3. Store a funded **OpenAI API** key in Render as `OPENAI_API_KEY`. Never put it
   in chat, GitHub, frontend code or logs. ChatGPT Plus is not an API balance.
4. Set `DIRECTOR_ALLOWED_USER_IDS` to explicitly approved staging test-user UUIDs.
   This is a closed beta, not an unrestricted public/free inference endpoint.
5. Keep `DIRECTOR_PLANNING_ENABLED=false` until a provider budget and one real
   planning smoke test are approved. Enable only for those test accounts.
6. Verify status, real plan generation, correct saved-plan ownership, reload,
   repeated submission/recovery and absence of any video/credit side effects.

Provider integration uses the fixed HTTPS OpenAI Responses endpoint, pinned
`gpt-5.4-mini-2026-03-17`, strict structured output, `store:false`, 4,000 output
tokens, 6,000 input characters, 2–6 scenes, no tools and no automatic retry.
Three attempts per approved account/day and twenty globally/day are enforced
under a PostgreSQL advisory transaction lock. Failed and uncertain attempts
count. Shared Redis additionally limits requests by client. A provider timeout
or backend restart never silently resubmits the same durable request key.
An incomplete/invalid/refused output is not saved as a successful plan.

Before broad launch: decide and implement planning pricing/entitlements, add a
reviewed content moderation/reporting workflow and update privacy disclosures
for the planning provider. Configure provider-side budget alerts/limits too.
The existing video-generation worker and its billing are separate from Director
planning: a text plan does not require a video worker.

Official API references:
https://developers.openai.com/api/docs/guides/structured-outputs
https://developers.openai.com/api/docs/models/gpt-5.4-mini
