# Brevitas cloud deployment

The production shape is one Railway FastAPI/proxy service, the existing Vercel site and
dashboard, and Supabase as the authoritative usage database. SQLite remains a local
development/test fallback only.

## 1. Supabase

Apply every file in `supabase/migrations/` in timestamp order. The final migration,
`20260710_cloud_usage.sql`, creates the canonical `usage_log`, API-key and provider-config
tables, idempotency constraint, indexes, and RLS boundaries.

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
```

The Next.js rewrite forwards `/v1/*` to Railway. Never put the Supabase service-role key or
`BREVITAS_SECRET_KEY` in a `NEXT_PUBLIC_*`/`VITE_*` variable.

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

The cloud receipt contains numeric token/cost fields and short labels only. Prompts,
responses, code, absolute paths, Git remotes, raw provider receipts, and provider keys are not
persisted to Supabase.
