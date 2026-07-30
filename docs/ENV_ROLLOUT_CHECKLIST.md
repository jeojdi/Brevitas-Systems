# Environment variable rollout checklist (owner-run)

Single, copy-pasteable runbook for staging **every environment-variable change the
production fixes imply**, per service. **No secret values appear here** — every secret is
either generated inline by a command you run, or pasted at an interactive prompt. Nothing in
this file is a secret.

Scope of the four surfaces and their platforms (see `DEPLOYMENT_GUIDE.md`,
`docs/RELEASE_PREFLIGHT.md`):

| Surface | Platform | Service / project name |
| --- | --- | --- |
| (A) App / website / dashboard / Stripe checkout+webhook | Vercel | project `brevitas-systems` (Production env) |
| (B) API / proxy | Railway | service `Brevitas-Systems`, domain `api.brevitassystems.com` |
| (C) Authoritative billing worker | Railway | service `worker-production` |
| (D) Compressor | Railway | service `compressor-production` (private network only) |

**Production Supabase project is `wyfzmfnswtzyhwbltbpy`.** The other project
`amjccgcgkcpbyevkjabw` is being decommissioned — never point anything new at it. The
currently-deployed dashboard bundle still inlines the wrong project (`amjccgcgkcpbyevkjabw`);
fixing that is part of (A) below and is why the ordering note at the end matters.

**Status legend** (as of the 2026-07-25/26 release handoff — always re-verify live with
`vercel env ls production` and `railway variables --service <svc>`):

- **SET** — believed present and correct; verify only.
- **VERIFY** — believed present but stale/unconfirmed value; confirm it is correct.
- **MISSING** — not set today; you must add it.
- **WRONG** — set today but pointing at the wrong thing; must be corrected.
- **BLOCKED** — cannot be set correctly until an upstream live resource exists.

**Source legend:**

- **generate** — you produce the value yourself with the inline command.
- **dashboard:X** — copy the value out of dashboard X.
- **known** — a fixed non-secret literal shown here.
- **platform-injected** — the platform sets it automatically; do **not** set it by hand.

> All `railway variables ... --skip-deploys` calls below intentionally defer redeploys so the
> full env set lands atomically. Redeploy only after a section is complete (see ordering note).

---

## (A) Vercel app — project `brevitas-systems`, Production env

`vercel env add <NAME> production` reads the value from stdin: for a secret, run it plain and
paste at the prompt; for a generated value, pipe the generator in as shown. Remove and re-add
to change an existing var (`vercel env rm <NAME> production --yes` first).

### A1. Backend origin and public URL

```bash
# BREVITAS_API_URL — known — the https Railway origin. Canonical backend var (next.config.ts,
# src/lib/admin/proxy.ts). Production build THROWS if unset or non-https. Status: SET (verify).
printf 'https://api.brevitassystems.com' | vercel env add BREVITAS_API_URL production

# BREVITAS_PUBLIC_URL — known — Stripe success/cancel + portal return base
# (src/lib/billing/config.ts -> src/lib/billing/config-predicate.mjs).
# billingIsConfigured() requires https whenever the surface is DEPLOYED
# (NODE_ENV=production or VERCEL_ENV set); plain-http loopback is accepted only in
# local development. Leaving it UNSET on Vercel is now a HARD FAIL — there is no
# longer a silent http://localhost:3000 default — so /api/billing/status reports
# configured:false, the dashboard button greys out, and checkout + webhook answer
# 503. Set it before flipping BREVITAS_BILLING_ENABLED or the flip does nothing.
# Code cannot catch a wrong-but-https value; only a missing or non-https one.
# Status: VERIFY (may be MISSING).
printf 'https://brevitassystems.com' | vercel env add BREVITAS_PUBLIC_URL production
```

> Legacy `API_URL` is dead (it pointed at a retired Railway host and caused onboarding 502s).
> If `vercel env ls production` shows `API_URL`, delete it: `vercel env rm API_URL production --yes`.

### A2. Stripe — full set (server-only; never `NEXT_PUBLIC_*`)

```bash
# STRIPE_SECRET_KEY — dashboard:Stripe (Developers > API keys, LIVE mode, sk_live_...).
#   Status: WRONG/ROLL — a live key was leaked in a prior session; roll it in Stripe and set the
#   NEW value here. Paste at the prompt (do not echo).
vercel env add STRIPE_SECRET_KEY production

# STRIPE_WEBHOOK_SECRET — dashboard:Stripe (Developers > Webhooks > the
#   https://brevitassystems.com/api/billing/webhook endpoint > Signing secret, whsec_...).
#   Status: SET (verify it matches the live endpoint you created).
vercel env add STRIPE_WEBHOOK_SECRET production

# STRIPE_PRICE_ID — dashboard:Stripe (created by `npm run billing:setup -- --live`; price_...).
#   Status: SET.
vercel env add STRIPE_PRICE_ID production

# STRIPE_METER_EVENT_NAME — known — must equal the meter's event_name. Status: SET.
printf 'brevitas_fee_microusd' | vercel env add STRIPE_METER_EVENT_NAME production

# STRIPE_AUTOMATIC_TAX — known — leave false unless Stripe Tax is configured. Status: SET.
printf 'false' | vercel env add STRIPE_AUTOMATIC_TAX production
```

### A3. Billing controls

```bash
# BILLING_RECOVERY_SECRET — generate — manual-recovery second factor; ASCII, 32-256 bytes,
#   no cron fallback (src/lib/billing/recovery-auth.mjs). Status: SET (verify strong).
openssl rand -base64 32 | vercel env add BILLING_RECOVERY_SECRET production

# BREVITAS_BILLING_WEEKLY_CAP_USD — generate/choose — you pick it; billingIsConfigured() requires
#   a finite number >0 and <=100000. Status: MISSING (you still owe this value). Example uses 100.
printf '100' | vercel env add BREVITAS_BILLING_WEEKLY_CAP_USD production

# BREVITAS_BILLING_ENABLED — known — KEEP false until the DB reconciliation (root blocker) is
#   green and the worker is ready. Status: SET=false. Flip to true only per (E) ordering.
printf 'false' | vercel env add BREVITAS_BILLING_ENABLED production
```

### A4. Supabase — server-side (secret) — project `wyfzmfnswtzyhwbltbpy`

```bash
# SUPABASE_URL — known — server rewrite / server calls. Status: VERIFY (must be the prod project).
printf 'https://wyfzmfnswtzyhwbltbpy.supabase.co' | vercel env add SUPABASE_URL production

# SUPABASE_SERVICE_ROLE_KEY — dashboard:Supabase (project wyfzmfnswtzyhwbltbpy >
#   Project Settings > API > service_role secret). Status: VERIFY (present but ~93d old; confirm it
#   is the wyfzmfnswtzyhwbltbpy key, not the decommissioned project's). Paste at the prompt.
vercel env add SUPABASE_SERVICE_ROLE_KEY production
```

### A5. Supabase — browser (public) — MUST be project `wyfzmfnswtzyhwbltbpy`

> **WRONG today.** The live bundle inlines `amjccgcgkcpbyevkjabw`. These `NEXT_PUBLIC_*` /
> `VITE_*` vars must name the **prod** project `wyfzmfnswtzyhwbltbpy`, and the dashboard bundle
> must be **rebuilt** so the corrected value is inlined (Vite freezes `VITE_*` at build time).

```bash
# NEXT_PUBLIC_SUPABASE_URL — known — prod project. Status: WRONG -> correct.
printf 'https://wyfzmfnswtzyhwbltbpy.supabase.co' | vercel env add NEXT_PUBLIC_SUPABASE_URL production

# NEXT_PUBLIC_SUPABASE_ANON_KEY — dashboard:Supabase (project wyfzmfnswtzyhwbltbpy > Settings >
#   API > anon/public key). Status: WRONG -> correct. Paste at the prompt.
vercel env add NEXT_PUBLIC_SUPABASE_ANON_KEY production

# VITE_SUPABASE_URL — known — same prod project (dashboard Vite build). Status: WRONG -> correct.
printf 'https://wyfzmfnswtzyhwbltbpy.supabase.co' | vercel env add VITE_SUPABASE_URL production

# VITE_SUPABASE_ANON_KEY — dashboard:Supabase (same anon key as above). Status: WRONG -> correct.
vercel env add VITE_SUPABASE_ANON_KEY production
```

After correcting A5, rebuild the tracked dashboard bundle so the right project is inlined
(see `DEPLOYMENT_GUIDE.md` §3): `npm run build:dashboard` from the repo root. Never
`cd dashboard && npm run build` — that path silently emits a bundle with no Supabase
configuration baked in. Do this only per the ordering
note in (E) — **after** the data migration.

- **platform-injected (do NOT set):** `VERCEL_GIT_COMMIT_SHA` (Vercel System Env Vars must be
  enabled so `/api/version` bakes it in; the production build fails if it is absent or conflicts
  with an explicit `BREVITAS_BUILD_SHA`).

---

## (B) Railway API — service `Brevitas-Systems`

```bash
# REDIS_URL — dashboard:Redis Cloud — MUST be rediss:// (TLS). Railway's own Redis plugin has no
#   TLS and the app fails closed, so use a paid multi-zone TLS-only Redis Cloud instance (AOF 1s).
#   Status: BLOCKED/MISSING (waiting on the rediss:// URL from your Redis Cloud account).
railway variables --service Brevitas-Systems \
  --set 'REDIS_URL=rediss://REPLACE_WITH_REDIS_CLOUD_TLS_URL' --skip-deploys
```

### B1. Company-admin secrets — two DIFFERENT values, shared across API + worker

Generate **once** into shell vars so the API and worker (same environment) get identical values;
they must differ from each other and be >=32 chars. Status: **MISSING** (generate fresh).

```bash
CURSOR=$(openssl rand -base64 48)   # COMPANY_ADMIN_CURSOR_SECRET
PEPPER=$(openssl rand -base64 48)   # COMPANY_ADMIN_INVITEE_PEPPER  (must be different)

railway variables --service Brevitas-Systems \
  --set "COMPANY_ADMIN_CURSOR_SECRET=$CURSOR" \
  --set "COMPANY_ADMIN_INVITEE_PEPPER=$PEPPER" --skip-deploys
# keep $CURSOR and $PEPPER in this shell — reused for the worker in (C1).
```

### B2. Managed KMS set (Google Cloud KMS) — config is `known`; the credential is `generate`

```bash
# Non-secret KMS config (known literals). Status: SET (verify) — KEY_ID was set by
# scripts/release/production-env-setup.sh; confirm it names keyRing brevitas-production.
railway variables --service Brevitas-Systems \
  --set 'BREVITAS_KMS_REQUIRED=true' \
  --set 'BREVITAS_KMS_PROVIDER=google-cloud-kms' \
  --set 'BREVITAS_KMS_KEY_ID=projects/divine-camera-465917-j7/locations/global/keyRings/brevitas-production/cryptoKeys/credential-envelope' \
  --set 'BREVITAS_KMS_KEY_VERSION=1' \
  --set 'BREVITAS_KMS_ALGORITHM=GOOGLE_SYMMETRIC_ENCRYPTION' \
  --set 'BREVITAS_KMS_ADAPTER_FACTORY=brevitas.security.google_cloud_kms:create_adapter' \
  --set 'BREVITAS_KMS_ADAPTER_TRUSTED_MODULES=brevitas.security.google_cloud_kms' \
  --set 'BREVITAS_GCP_KMS_TIMEOUT_SECONDS=0.75' \
  --set 'BREVITAS_KMS_READINESS_TIMEOUT_SECONDS=1' \
  --set 'BREVITAS_KMS_READINESS_MAX_AGE_SECONDS=30' --skip-deploys
```

```bash
# GCP_SA_KEY_JSON — generate (gcloud) — temporary key-based ADC exception (DEPLOYMENT_GUIDE §2);
#   the start-with-adc.sh shim materializes it to GOOGLE_APPLICATION_CREDENTIALS at boot. Scope the
#   SA to roles/cloudkms.cryptoKeyEncrypterDecrypter on the prod key only. Status: VERIFY/ROLL — a
#   prior key was leaked; roll and re-set. Prefer Workload Identity Federation over a key when
#   Railway can issue one. Read the JSON from a file so it never lands in shell history:
railway variables --service Brevitas-Systems \
  --set "GCP_SA_KEY_JSON=$(cat /path/to/brevitas-prod-kms-key.json)" --skip-deploys
```

### B3. Environment, origins, proxy trust

```bash
# BREVITAS_ENV — known — production fails closed if not production. Status: VERIFY.
# ALLOWED_ORIGINS — known — the Vercel prod origin for CORS. Status: VERIFY.
# FORWARDED_ALLOW_IPS — known — Railway terminates TLS at its edge and forwards; uvicorn must trust
#   the forwarded-for header or every client sees the proxy IP (api/server.py, Dockerfile CMD).
#   DO NOT use '*'. '*' trusts the entire client-supplied X-Forwarded-For chain and resolves the
#   peer to its left-most (client-controlled) entry, so any caller can spoof X-Forwarded-For per
#   request and mint a fresh rate-limit bucket — reopening the bypass _rate_key closed. Production
#   startup now fails closed on '*'.
#   RECOMMENDED VALUE (below): trust only private/internal ranges. The container is not publicly
#   reachable except via Railway's proxy, which connects from a private address, so uvicorn walks
#   X-Forwarded-For right-to-left and returns the first NON-private hop = the real client IP that
#   Railway's edge appended, ignoring any public address a caller spoofs into the header. This is
#   robust to Railway rotating its edge IPs (no brittle exact pin needed). uvicorn 0.51 supports
#   CIDR ranges (verified in middleware/proxy_headers.py::_TrustedHosts).
#   VERIFY after deploy: curl the API from a machine whose public IP you know; the access log /
#   429 buckets must key on THAT ip, not a single shared proxy address. If everything collapses to
#   one private IP, Railway is not appending the client to X-Forwarded-For — widen/adjust the list.
railway variables --service Brevitas-Systems \
  --set 'BREVITAS_ENV=production' \
  --set 'ALLOWED_ORIGINS=https://brevitassystems.com' \
  --set 'FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fd00::/8' --skip-deploys
```

- **platform-injected (do NOT set):** `RAILWAY_GIT_COMMIT_SHA` (native Railway builds supply it;
  startup fails closed without a full immutable SHA). Set `BREVITAS_BUILD_SHA` only for external/
  promoted images, and `BREVITAS_IMAGE_DIGEST` only after the registry returns the pushed digest.
- Also required on this service but owned by other rollout steps (out of scope here, verify
  present): `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY` (prod project
  `wyfzmfnswtzyhwbltbpy`), `BREVITAS_STORE=supabase`, `BREVITAS_CACHE_BACKEND=supabase`, and the
  compressor wiring in (D).

---

## (C) Railway billing worker — service `worker-production`

The worker is the **authoritative** Stripe meter-event writer. It needs everything the API needs
(B1 company-admin secrets, B2 KMS + GCP_SA_KEY_JSON, Supabase, Redis, `BREVITAS_ENV=production`)
**plus** the billing role and the Stripe set below.

### C1. Same shared secrets as the API (identical values)

```bash
# Reuse the SAME $CURSOR/$PEPPER generated in (B1) — they must match the API in this environment.
railway variables --service worker-production \
  --set "COMPANY_ADMIN_CURSOR_SECRET=$CURSOR" \
  --set "COMPANY_ADMIN_INVITEE_PEPPER=$PEPPER" --skip-deploys

# KMS config + credential (same as B2). Status: MISSING on worker per handoff — the worker service
# was created as an empty shell; set the full KMS block and GCP_SA_KEY_JSON here too.
railway variables --service worker-production \
  --set 'BREVITAS_KMS_REQUIRED=true' \
  --set 'BREVITAS_KMS_PROVIDER=google-cloud-kms' \
  --set 'BREVITAS_KMS_KEY_ID=projects/divine-camera-465917-j7/locations/global/keyRings/brevitas-production/cryptoKeys/credential-envelope' \
  --set 'BREVITAS_KMS_KEY_VERSION=1' \
  --set 'BREVITAS_KMS_ALGORITHM=GOOGLE_SYMMETRIC_ENCRYPTION' \
  --set 'BREVITAS_KMS_ADAPTER_FACTORY=brevitas.security.google_cloud_kms:create_adapter' \
  --set 'BREVITAS_KMS_ADAPTER_TRUSTED_MODULES=brevitas.security.google_cloud_kms' \
  --set 'BREVITAS_GCP_KMS_TIMEOUT_SECONDS=0.75' \
  --set 'BREVITAS_KMS_READINESS_TIMEOUT_SECONDS=1' \
  --set 'BREVITAS_KMS_READINESS_MAX_AGE_SECONDS=30' \
  --set 'BREVITAS_ENV=production' \
  --set 'REDIS_URL=rediss://REPLACE_WITH_REDIS_CLOUD_TLS_URL' --skip-deploys

railway variables --service worker-production \
  --set "GCP_SA_KEY_JSON=$(cat /path/to/brevitas-prod-kms-key.json)" --skip-deploys
```

### C2. Billing role + Stripe set (worker-only)

The worker's start command already injects `BREVITAS_WORKER_BILLING_ROLE=authoritative`
(`deploy/railway-worker.json`); set it as an env var too so it is explicit and audit-visible.
The worker takes the metering Stripe vars — **not** the webhook secret (webhooks land on Vercel).

```bash
# BREVITAS_WORKER_BILLING_ROLE — known — the tracked worker IS the authoritative billing writer.
# STRIPE_SECRET_KEY — dashboard:Stripe — the NEW rolled sk_live (paste separately, see below).
# STRIPE_PRICE_ID — dashboard:Stripe — price_... from billing:setup. Status: SET.
# STRIPE_METER_EVENT_NAME — known. Status: SET.
# BREVITAS_BILLING_WEEKLY_CAP_USD — choose — same policy value as Vercel A3.
# BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER — known — keep false until this worker is proven the sole writer.
railway variables --service worker-production \
  --set 'BREVITAS_WORKER_BILLING_ROLE=authoritative' \
  --set 'STRIPE_PRICE_ID=price_REPLACE_FROM_BILLING_SETUP' \
  --set 'STRIPE_METER_EVENT_NAME=brevitas_fee_microusd' \
  --set 'BREVITAS_BILLING_WEEKLY_CAP_USD=100' \
  --set 'BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER=false' \
  --set 'BREVITAS_BILLING_ENABLED=false' --skip-deploys

# STRIPE_SECRET_KEY — set separately so the sk_live never lands in a --set string in history.
# Status: MISSING on worker (the 3 Stripe secrets were not yet on Railway per handoff). Paste when prompted:
read -rs STRIPE_SK && railway variables --service worker-production \
  --set "STRIPE_SECRET_KEY=$STRIPE_SK" --skip-deploys && unset STRIPE_SK
```

> Keep `BREVITAS_BILLING_ENABLED=false` on the worker until the DB is reconciled. Any deliberately
> separate non-billing worker must instead carry `BREVITAS_WORKER_BILLING_ROLE=nonbilling`.

- **platform-injected (do NOT set):** `RAILWAY_GIT_COMMIT_SHA` (as with the API).

---

## (D) Compressor — service `compressor-production`

One shared bearer token protects the private compressor. Set the **same** high-entropy value on
the compressor AND on the API + worker (which call it). Generate once. Status per handoff: a token
was already generated and wired — **VERIFY** it matches on all three services; only regenerate if
you must rotate.

```bash
# BREVITAS_COMPRESS_TOKEN — generate — shared bearer (token_efficiency_model/lossless/remote_compress.py).
CTOKEN=$(openssl rand -base64 48)

railway variables --service compressor-production \
  --set "BREVITAS_COMPRESS_TOKEN=$CTOKEN" --skip-deploys

# Callers: same token + the PRIVATE internal URL (never a public domain). Production startup fails
# if the compress URL is non-private or the token is missing.
railway variables --service Brevitas-Systems \
  --set "BREVITAS_COMPRESS_TOKEN=$CTOKEN" \
  --set 'BREVITAS_COMPRESS_URL=http://compressor.railway.internal:8080' \
  --set 'BREVITAS_COMPRESS_REQUIRED=true' --skip-deploys

railway variables --service worker-production \
  --set "BREVITAS_COMPRESS_TOKEN=$CTOKEN" \
  --set 'BREVITAS_COMPRESS_URL=http://compressor.railway.internal:8080' \
  --set 'BREVITAS_COMPRESS_REQUIRED=true' --skip-deploys
unset CTOKEN
```

> Do NOT attach a public domain to the compressor. If you skip compression entirely, unset both
> `BREVITAS_COMPRESS_URL` and `BREVITAS_COMPRESS_TOKEN` on the callers and set
> `BREVITAS_COMPRESS_REQUIRED=false` (yields an alertable `degraded` readiness, not an outage).

---

## Missing-today summary (re-verify live before acting)

| Var | Service(s) | Source | Status |
| --- | --- | --- | --- |
| `BREVITAS_BILLING_WEEKLY_CAP_USD` | Vercel + worker | choose | **MISSING** (value not yet decided) |
| `BREVITAS_PUBLIC_URL` | Vercel | known | **VERIFY / likely MISSING** |
| `NEXT_PUBLIC_/VITE_ SUPABASE_URL`+`ANON_KEY` | Vercel | known + dashboard:Supabase | **WRONG** (points at `amjccgcgkcpbyevkjabw`) |
| `REDIS_URL` (rediss://) | API + worker | dashboard:Redis Cloud | **BLOCKED** (need TLS URL) |
| `COMPANY_ADMIN_CURSOR_SECRET` / `_INVITEE_PEPPER` | API + worker | generate | **MISSING** (generate fresh, two different) |
| KMS block + `GCP_SA_KEY_JSON` | worker | known + generate | **MISSING on worker** (empty shell) |
| Stripe set (`SECRET_KEY`,`PRICE_ID`,`METER_EVENT_NAME`,cap) | worker | dashboard:Stripe + known | **MISSING on worker** |
| `STRIPE_SECRET_KEY` | Vercel + worker | dashboard:Stripe | **ROLL** (leaked sk_live must be rotated) |
| `GCP_SA_KEY_JSON` | API | generate | **ROLL** (leaked key) |

---

## (E) Ordering — do these in order

1. **Set every env var first, with `--skip-deploys` / before any build.** A missing or wrong var
   bakes into the build (Next.js rewrite freezes `BREVITAS_API_URL`; Vite inlines `VITE_*`), so a
   redeploy against an incomplete env silently ships the wrong config.
2. **Reconcile the production Supabase schema BEFORE deploying the `wyfzmfnswtzyhwbltbpy` dashboard
   bundle.** Follow `docs/PROD_DB_RECONCILIATION.md` (backup → ordered `psql` apply, incl. the
   by-design `drop table user_keys` → verify `/v1/health/ready` = 200). Deploying the corrected
   dashboard (A5) at the prod project **before** the schema exists points real users at a
   schema-incomplete DB (signup/onboarding 500s). Migration first, then
   `npm run build:dashboard` from the repo root (never `cd dashboard && npm run build`,
   which drops the Supabase configuration from the bundle), then deploy.
3. **Redeploy each Railway service** (API, worker, compressor) only after its env section is
   complete. The API healthcheck (`/v1/health/ready`) will not pass until Supabase (step 2), KMS,
   and Redis are all set and reachable.
4. **Flip billing on last.** Keep `BREVITAS_BILLING_ENABLED=false` on Vercel and the worker until
   step 2 is green and the authoritative worker reports ready; only then set it to `true` on both.
5. **Then run the gates:** `npm run release:preflight -- production` and the tenant smoke
   (`docs/RELEASE_PREFLIGHT.md`, `docs/RELEASE_SECURITY.md`).

> **Rotation debt to clear as part of this rollout** (do not defer): the leaked `sk_live` Stripe
> key, the `brevitas-prod-kms@divine-camera-465917-j7` SA key, and the Redis Cloud default-user
> password. Re-set the rotated values everywhere they appear above.
