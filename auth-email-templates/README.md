# NENE AI authentication email branding

Hosted Supabase project: `agchmegopyktwofcupny`. These files are reviewed source templates; committing them does not change hosted Supabase settings.

In Authentication → Emails → Templates → Reset password:

- Subject: **Reset your NENE AI password**
- Body: paste `recovery.html` without substituting `{{ .ConfirmationURL }}`. Supabase generates and verifies this per-request link. Never use a static reset link, user metadata URL, or copy a real recovery token into source control.

In Authentication → Emails → SMTP Settings, use the sender display name **NENE AI**. Keep the existing SMTP host, credentials and verified From address if already configured. A branded sender address requires a mail service and a verified sending domain; do not invent an address or enable a partially configured SMTP server. Supabase's default mail service is for testing and restricts delivery to project-team addresses; custom SMTP is required for a public launch.

Check the existing URL Configuration allow list includes the deployed app root and any dedicated recovery/deletion page that requests a reset. Do not widen the allow list to arbitrary URLs or disable email verification to fix branding. Changing the email body does not rename the sender address or the Supabase verification-link domain. A branded link hostname requires separate custom-domain configuration.

After saving, request one password reset from a designated test account with the owner's approval. Verify actual inbox sender name/address, subject, styling, reset destination, one-time use and expiry; do not mark live delivery verified from a template preview alone. No test email is sent by this repository.

Official references:
- https://supabase.com/docs/guides/auth/auth-email-templates
- https://supabase.com/docs/guides/auth/auth-smtp
