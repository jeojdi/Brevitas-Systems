# Brevitas cloud deployment

The production shape is Vercel for the website, dashboard, auth UI, Stripe checkout/webhooks,
and administrative routes; multiple stateless Railway FastAPI gateway replicas; continuously
running Railway job-worker replicas; and one private Railway compressor. Supabase/Postgres is
authoritative. Redis is coordination-only (admission leases and opaque job wake-ups). SQLite and
process-local state are development/test fallbacks only and must never be authoritative in a
hosted environment.

## 1. Supabase

Apply every file in `supabase/migrations/` in timestamp order, including the enterprise tenancy,
encrypted cache, and durable-job migrations dated `20260717`.
Do not also apply the duplicate base schema in `api/migrations/001_persistent_stores.sql`.

Service-owned tables have RLS enabled with no end-user policies. Only Railway's service-role
credential can access them. Raw API keys are returned once by the management API and are never
recoverable from Postgres.

To grant Brevitas operators the cross-customer view, set this on their Supabase Auth user:

```json
{ "brevitas_admin": true, "role": "brevitas_admin" }
```

under `app_metadata`. There is intentionally no static header-token bypass; every Admin API
request must carry a valid Supabase user session with this metadata.
Compliance administration requires the exact `role: "brevitas_admin"` value and derives its
tenant from that actor's current active finite-role database membership. Request bodies and
headers cannot select or override the compliance actor or organization.

## 2. Railway

Create three services from the same repository in one primary US region, colocated with Supabase
and Redis Cloud:

| Service | Railway config-as-code path | Network | Replicas | Readiness |
| --- | --- | --- | ---: | --- |
| `api` | `/railway.json` (or equivalent `/railway.toml`) | Public custom domain | 2 minimum | `/v1/health/ready` |
| `worker` | `/deploy/railway-worker.json` | No public domain | 2 minimum | `/ready` |
| `compressor` | `/deploy/railway.json` | Private network only | 1 initially | `/ready` |

Do not attach a volume to API or worker services. Railway load-balances API replicas without
sticky sessions, so sessions, leases, counters, jobs, and billing records must remain in shared
infrastructure. The API and worker use the same root image; the worker config overrides the start
command with `BREVITAS_WORKER_BILLING_ROLE=authoritative python -m api.worker`. The worker
continuously claims Postgres and billing leases and does not depend on Vercel cron for durable
work.

The JSON files are per-service templates, not a resource-provisioning manifest. In each Railway
service, set the corresponding config file path, select the same region, and verify the replica
count in the generated deployment plan. This repository does not provision or change live
resources.

Set these service variables:

```text
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...
SUPABASE_ANON_KEY=...
BREVITAS_STORE=supabase
BREVITAS_ENV=production
ALLOWED_ORIGINS=https://YOUR_VERCEL_DOMAIN
BREVITAS_PROXY_AUTH=true
REDIS_URL=rediss://...
BREVITAS_KMS_PROVIDER=YOUR_MANAGED_KMS_PROVIDER
BREVITAS_KMS_KEY_ID=YOUR_IMMUTABLE_MANAGED_KEY_ID
BREVITAS_KMS_KEY_VERSION=YOUR_IMMUTABLE_KEY_VERSION
BREVITAS_KMS_ADAPTER_FACTORY=your_runtime.kms:create_adapter
BREVITAS_KMS_ADAPTER_TRUSTED_MODULES=your_runtime.kms
COMPANY_ADMIN_CURSOR_SECRET=YOUR_RANDOM_32_PLUS_CHARACTER_CURSOR_SECRET
COMPANY_ADMIN_INVITEE_PEPPER=YOUR_RANDOM_32_PLUS_CHARACTER_INVITEE_PEPPER
BREVITAS_CACHE_ENABLED=false
BREVITAS_CACHE_BACKEND=supabase
BREVITAS_CACHE_ENCRYPTION_KEY=...
POSTHOG_PROJECT_ID=...
POSTHOG_PERSONAL_API_KEY=phx_...
POSTHOG_API_HOST=https://us.posthog.com
BREVITAS_COMPRESS_URL=http://compressor.railway.internal:8080
BREVITAS_COMPRESS_TOKEN=...
BREVITAS_COMPRESS_REQUIRED=false
BREVITAS_WORKER_BILLING_ROLE=authoritative
BREVITAS_BILLING_ENABLED=true
STRIPE_SECRET_KEY=...
STRIPE_PRICE_ID=...
STRIPE_METER_EVENT_NAME=...
BREVITAS_BILLING_WEEKLY_CAP_USD=...
```

The adapter factory is deployment-owned code that returns the repository's `ManagedKMS`
interface. Its module must be named exactly in `BREVITAS_KMS_ADAPTER_TRUSTED_MODULES`; arbitrary
dotted imports are rejected. Provider, device, and job ciphertext use versioned envelope
encryption, and legacy keys may only be supplied as explicit decrypt-only migration inputs.
Set the same environment-specific `COMPANY_ADMIN_CURSOR_SECRET` value on every API replica and
worker that constructs the Supabase store; it signs both company-administration and usage
pagination cursors. Set `COMPANY_ADMIN_INVITEE_PEPPER` independently to another environment-
specific secret. Both values must contain at least 32 characters, must match across replicas in
one environment, and must not be reused between staging and production.
Production fails closed if the managed adapter, immutable key identity, Postgres, Redis, or the
authoritative billing configuration is missing. Set `BREVITAS_WORKER_BILLING_ROLE=nonbilling`
only on a deliberately separate worker service; the tracked Railway worker template is the
authoritative billing worker. Use the Supabase Supavisor transaction pooler for application
database connections, with PITR enabled. Redis Cloud must be paid, multi-zone, TLS-only, and
configured for AOF every second; Postgres remains authoritative after every Redis loss.

The authoritative worker stays unready until its billing loop has validated both the Stripe
catalog and Supabase health and has completed a successful cycle. It becomes unready again after
`BREVITAS_BILLING_READINESS_STALE_SECONDS` (120 seconds by default) without success or after
`BREVITAS_BILLING_READINESS_ERROR_THRESHOLD` consecutive errors (3 by default). Treat either
condition as a paging signal; do not bypass the probe to restore traffic.

`BREVITAS_COMPRESS_URL` must use the Railway private hostname (the exact service name may differ),
not a generated or custom public domain. Set the same high-entropy `BREVITAS_COMPRESS_TOKEN` on
the API/worker and compressor services. Do not add a Railway public domain to the compressor.
Production startup fails if a compressor URL is configured with a non-private hostname or without
the internal token, even when compression is optional; this prevents any optimizer request from
reaching an unsafe endpoint. Omitting both settings is valid for an optional compressor and yields
an alertable degraded health status. See `docs/DEPLOY_COMPRESS.md`.

After deployment, confirm:

```bash
curl https://YOUR_RAILWAY_HOST/v1/health/live
curl https://YOUR_RAILWAY_HOST/v1/health/ready
```

Then attach the public domain (for example `api.brevitassystems.com`) and keep Railway HTTPS
enabled. Liveness proves that the process can answer; readiness requires authoritative Postgres
and coordination Redis. An optional compressor outage returns an alertable `degraded` readiness
payload without removing the otherwise healthy API from service. Set
`BREVITAS_COMPRESS_REQUIRED=true` only for a deployment whose contract requires lossy compression;
then compressor failure makes readiness unavailable. Health never returns credentials or endpoint
names.

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

The Next.js rewrite forwards `/v1/*` to Railway. Never put the Supabase service-role key, managed
KMS configuration, or server credentials in a `NEXT_PUBLIC_*`/`VITE_*` variable.

`vercel.json` intentionally has no billing cron. Keep the billing sync route as an authenticated
manual recovery control only; the continuously running Railway worker owns scheduled processing,
leases, recovery, and reconciliation. Stripe checkout and webhook routes remain on Vercel.

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

## 4. Company and end-customer identity

Company A creates one Brevitas organization service key per environment. Its end customers do
not receive Brevitas credentials and do not need Brevitas accounts. Company A's backend attaches
its own stable opaque customer identifier to every request:

```text
X-Brevitas-Key: bvt_company_environment_secret
X-Brevitas-Customer-ID: company_a_internal_customer_123
```

The service credential determines the organization. The exact customer ID is idempotently found
or created inside that organization. Never use names, email addresses, or semantic/fuzzy matching
for this identity boundary.

Existing customers can be bulk imported with `POST /v1/customers/import` using a signed-in admin
session, or simply lazy-created on their first attributed request. Import the same stable ID that
Company A will send at runtime. Historical traffic that lacks a stable ID remains deliberately
unattributed.

Provider credentials continue to use normal provider authentication and are never stored in
usage logs, Redis keys, jobs, or AgentMap inventory.

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
2. Send one provider call through Railway with `X-Brevitas-Customer-ID` and the normal optional
   project/environment/client labels.
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
10. Submit 1,000 unique idempotent jobs in staging, terminate a worker during processing, and
    verify exactly 1,000 tenant-correct terminal outcomes with no loss or duplicate completion.
11. Test two API replicas against the same Redis and verify organization, customer, key, token,
    and concurrency limits are shared. Repeat with Redis unavailable and confirm production
    admission fails closed.
12. Send SIGTERM to one API and one worker replica in staging. Confirm Railway stops routing new
    API requests and Uvicorn drains in-flight HTTP work before lifespan teardown. Do not treat the
    API lifespan readiness transition as a pre-stop load-balancer hook. Confirm the worker's
    readiness fails during its explicit drain, in-flight jobs either complete inside the drain
    window or become reclaimable after their Postgres lease expires, and other replicas continue.
13. Confirm the compressor has no public domain, rejects a missing/incorrect bearer token, and
    reports `/live`, `/startup`, and `/ready` without revealing its token or private hostname.

The cloud receipt contains numeric token/cost fields and short labels only. Prompts,
responses, code, absolute paths, Git remotes, raw provider receipts, and provider keys are not
persisted to Supabase.
