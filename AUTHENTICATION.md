# NENE AI authentication foundation

The wallet API accepts **only signed bearer access tokens**. It never trusts a user ID
sent by a browser in a body, query string, or custom header.

## Required Render environment variables

Configure these only after creating and testing an identity-provider project:

- `AUTH_JWKS_URL`: the provider's HTTPS JSON Web Key Set (JWKS) endpoint.
- `AUTH_ISSUER`: the exact issuer value present in access-token `iss`.
- `AUTH_AUDIENCE`: expected access-token audience; defaults to `authenticated`.

The verifier currently allows RS256 and ES256 signatures, validates signature, issuer,
audience, and expiration, and requires the token subject (`sub`) to be a UUID. The
subject UUID becomes NENE AI's internal `users.id`. The backend creates that internal
user row only after token verification and denies accounts whose status is not
`active`.

For a Supabase Auth project using asymmetric signing keys, the JWKS endpoint commonly
follows this shape:

`https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json`

Use the exact issuer shown by that project's Auth configuration; do not guess the
project URL or copy an issuer from another project. Older projects configured with
legacy shared-secret JWT signing may not expose a compatible JWKS endpoint; this
implementation intentionally does not accept unsigned tokens or a browser-provided
user ID as a workaround.

## Current safety boundaries

- Wallet routes return 401 without a bearer token.
- Wallet routes return 503 until JWKS URL, issuer, and database storage are configured.
- Wallet credit grants remain disabled.
- Payment webhooks, customer billing, account recovery, and subscription fulfillment
  are not implemented by this foundation.
- The frontend's existing localStorage credits are still prototype-only and are not
  authoritative. Do not accept real payments or advertise server-backed credits yet.
- Set `DATABASE_URL` only after choosing and securing the production PostgreSQL
  project. Use a private server-side connection string; never put it in the frontend.
