# Brevitas cloud deployment

The production shape is one Railway FastAPI/proxy service, the existing Vercel site and
dashboard, and Supabase as the authoritative usage database. SQLite remains a local
development/test fallback only.

## 1. Supabase

Apply every file in `supabase/migrations/` in timestamp order through
`20260716_posthog_warehouse_view.sql`. `20260710_cloud_usage.sql` creates the canonical usage and
API-key tables; the later migrations add device authorization and versioned legal/privacy
acceptance records plus a narrow PostHog warehouse view.
Do not also apply the duplicate base schema in `api/migrations/001_persistent_stores.sql`.

Service-owned tables have RLS enabled with no end-user policies. Only Railway's service-role
credential can access them. `user_keys` has an owner-only policy so an authenticated dashboard
user can recover their own Brevitas key.

To grant Brevitas operators the cross-customer view, set this on their Supabase Auth user:

```json
{ "brevitas_admin": true }
```

under `app_metadata`. A server-only `BREVITAS_ADMIN_TOKEN` is also supported for internal
automation.

## 2. Railway

Create one service from the repository root. Railway uses the root `Dockerfile`; `railway.toml`
sets `/v1/health` as the health check. Do not attach a volume.

Set these service variables:

```text
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...
SUPABASE_ANON_KEY=...
BREVITAS_SECRET_KEY=...
BREVITAS_STORE=supabase
ALLOWED_ORIGINS=https://YOUR_VERCEL_DOMAIN
BREVITAS_PROXY_AUTH=true
BREVITAS_PROXY_RPM=300
BREVITAS_PROXY_CONCURRENCY=20
POSTHOG_PROJECT_ID=...
POSTHOG_PERSONAL_API_KEY=phx_...
POSTHOG_API_HOST=https://us.posthog.com
```

`BREVITAS_SECRET_KEY` must be a stable Fernet key; changing it makes previously encrypted
Playground provider credentials unreadable. Add `BREVITAS_COMPRESS_URL` and
`BREVITAS_COMPRESS_TOKEN` only if the optional lossy compressor is deployed.

After deployment, confirm:

```bash
curl https://YOUR_RAILWAY_HOST/v1/health
```

Then attach the public domain (for example `api.brevitassystems.com`) and keep Railway HTTPS
enabled.

## 3. Vercel

Keep the repository root as the Next.js project and set:

```text
API_URL=https://YOUR_RAILWAY_HOST
NEXT_PUBLIC_SUPABASE_URL=https://YOUR_PROJECT.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=...
NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN=phc_...
NEXT_PUBLIC_POSTHOG_UI_HOST=https://us.posthog.com
POSTHOG_HOST=https://us.i.posthog.com
POSTHOG_ASSETS_HOST=https://us-assets.i.posthog.com
```

The Next.js rewrite forwards `/v1/*` to Railway. Never put the Supabase service-role key or
`BREVITAS_SECRET_KEY` in a `NEXT_PUBLIC_*`/`VITE_*` variable.

Create a US Cloud PostHog project and use the same project token for the public site and
dashboard. Create a project-scoped personal API key with only the read/query access needed by
the admin traffic summary; it belongs on Railway only. The public project token is expected to
be visible in the browser and is served by `/api/analytics-config`. PostHog SDK and capture
requests are proxied through `/ingest/*` on the Brevitas domain.

### Optional Supabase warehouse source

The warehouse source must use the dedicated `posthog_reader` role and the `analytics` schema;
never use Supabase's `postgres` user, service-role key, or the `public`/`auth` schemas. After the
latest migration is applied, set a long random password once in the Supabase SQL editor:

```sql
alter role posthog_reader with login password 'GENERATE_A_LONG_RANDOM_PASSWORD';
```

In PostHog choose Supabase standard sync, the Supabase Session Pooler host, port `5432`, database
`postgres`, user `posthog_reader.PROJECT_REF`, the generated password, and schema `analytics`.
Keep SSH and CDC disabled, use table prefix `supabase`, and sync only `posthog_usage`. Full refresh
or incremental sync on `ts` is sufficient. This view excludes API-key hashes/raw keys, provider
configuration, prompts/responses, session/run/request identifiers, and auth/legal tables.

The tracked Vite dashboard bundle lives in `public/dashboard`. Rebuild it after dashboard
source changes with:

```bash
cd dashboard
npm run build
```

## 4. Customer/provider keys

Brevitas does not need a shared OpenAI, Anthropic, DeepSeek, or other model-provider key.
Each customer keeps their own provider key in their application or coding client. The key is
forwarded to that provider and is never written to `usage_log`. Railway needs only the
Supabase and Brevitas secrets listed above.

Calls to the hosted model proxy use `X-Brevitas-Key` for Brevitas authentication. Provider
authentication remains in `Authorization` (OpenAI-compatible APIs) or `X-Api-Key`
(Anthropic). This separation prevents the two credentials from colliding.

## 5. Historical SQLite import

Run the import once from an environment configured for the target Supabase project:

```bash
BREVITAS_STORE=supabase python -m api.import_usage /path/to/brevitas.db
```

The command is idempotent. Running it again reports duplicates and does not change totals.
Rows without project/source metadata appear as `Unattributed`.

## 6. Production checks

1. Sign in to `/dashboard` and create a Brevitas API key.
2. Send one provider call through Railway with `X-Brevitas-Project`,
   `X-Brevitas-Environment`, and `X-Brevitas-Client` headers.
3. Confirm the response streams normally even if telemetry is unavailable.
4. Confirm the event appears in the customer Projects tab under the expected
   project/client/provider/model row.
5. Confirm an operator with `brevitas_admin=true` can see the same numeric row in Admin, while
   a normal user receives `403` from `/v1/admin/*`.
6. Repeat the same `X-Brevitas-Request-Id` and confirm totals do not increase.
7. Confirm pageviews arrive in PostHog, the Privacy choices control stops future events, and
   GPC/DNT browsers start opted out.
8. Inspect a marketing and authenticated replay. Inputs, account email/UUID, API/provider keys,
   Playground content, financial details, URL query strings, and network bodies must be masked
   or absent.
9. Confirm the Admin traffic section loads through `/v1/admin/analytics`; remove the PostHog
   personal key temporarily and confirm financial operations still work while traffic returns
   a contained unavailable state.

The cloud receipt contains numeric token/cost fields and short labels only. Prompts,
responses, code, absolute paths, Git remotes, raw provider receipts, and provider keys are not
persisted to Supabase.
