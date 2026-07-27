# Supabase Auth email templates

In **Supabase → Authentication → Email Templates**, use:

- Confirm signup subject: `Confirm your Brevitas account`
- Confirm signup body: `confirm-signup.html`
- Reset password subject: `Reset your Brevitas password`
- Reset password body: `reset-password.html`

## Which project

The dashboard reaches whatever `VITE_SUPABASE_URL` was set to **at build time** — Vite inlines it
into the bundle, so a mismatched value is invisible in the repo and survives until the next build.
Confirm the deployed bundle and the project you are configuring are the same before debugging auth:

```bash
ASSET=$(curl -sL https://brevitassystems.com/dashboard | grep -oE '/dashboard/assets/index-[A-Za-z0-9_-]+\.js' | head -1)
curl -sL "https://brevitassystems.com$ASSET" | grep -oE 'https://[a-z0-9]+\.supabase\.co' | sort -u
```

SMTP settings, redirect allowlists, and rate limits are all per-project. Applying them to the wrong
project leaves signup returning `500 unexpected_failure` with no trace of the change.

## Redirect URLs

`Auth.jsx` derives the confirmation target from the sign-in route, so the link carries an
`audience` query string (`confirmationPathForLoginAudience`) and `public/email-confirmed.html`
reads it back. Password reset uses a different target again — `resetPasswordForEmail` sends users
to `/dashboard`, where the `recovery` mode picks up the token. Allowlist every variant in
**Authentication → URL Configuration → Redirect URLs**, not just the bare path — an unlisted target
silently falls back to the Site URL, so the link still arrives and still lands in the wrong place:

- `https://brevitassystems.com/email-confirmed`
- `https://brevitassystems.com/email-confirmed?audience=personal`
- `https://brevitassystems.com/email-confirmed?audience=enterprise`
- `https://brevitassystems.com/invite`
- `https://brevitassystems.com/dashboard` — password reset; omitting it breaks recovery only
- the same five paths on `http://localhost:5174` for local development

Every entry is matched against `window.location.origin` plus the path. The apex is canonical
(`www` returns `308` to it), so allowlist the apex, not `www`.

## Email delivery

These templates are never sent until **Authentication → Emails → SMTP Settings** has a custom
provider with a verified sender domain. Without it, Supabase's built-in service refuses delivery
to any address outside the project team and caps at 2 messages/hour, so signup returns
`500 unexpected_failure` ("Error sending confirmation email"). Raise
**Authentication → Rate Limits** above the 30/hour default once SMTP is live.
