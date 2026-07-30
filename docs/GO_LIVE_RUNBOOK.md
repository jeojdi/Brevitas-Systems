# Brevitas — Enterprise Go-Live Runbook

**Owner-run.** Generated 2026-07-27 from the full backend audit + live production diagnosis.
Goal: take the system from "usage tracking just recovered" to **deployed, data-consolidated,
secrets-rotated, and operationally proven** — i.e. actually able to support enterprise usage.

Legend:  ✅ done & verified   🟡 staged on branch `audit-fixes-2026-07-27` (not deployed)   🔴 owner action required

---

## ADDENDUM 2026-07-30 — apply-before-deploy precondition for the current tree

The working tree adds migrations `202607280006`–`202607280024`. Production (`wyfz`) has the
chain applied **through `202607280005` only** and has **no migration ledger**, so:

- 🔴 **Before (or immediately after) the next Railway `api/` push and Vercel deploy**, hand-apply
  `202607280006`–`202607280024` to `wyfz`, one file at a time, in lexical order, via
  `supabase db query --linked -f supabase/migrations/<file>.sql` — never `db push`, never a blind
  chain replay (see memory: *Supabase prod migration apply* / *Production schema drift*).
- The two code paths that depend on the new RPCs both **degrade gracefully** on a code-first deploy,
  so ordering is a short-window UX cost, not an outage:
  - `DELETE /v1/keys/{id}` falls back to the pre-`202607280019` dashboard-session RPC when
    `company_admin_revoke_tenant_key` is missing (PGRST202). Device-key revocation (the new
    kill switch) only works once `202607280019` is applied. Remove
    `SupabaseUsageStore._revoke_key_via_predispatcher_rpc` in `api/store.py` once 0019 is
    confirmed on wyfz.
  - `/api/billing/status` renders every money field as `null` behind `settlement_pending: true`
    ("Unavailable" in the dashboard) until `202607280013` is applied.
- `202607280022` also closes a security gap on apply: removing/disabling a member now revokes
  their device + session credentials, and it backfills `api_keys.created_by` for device keys from
  the `device_key.activated` audit trail. Until it is applied, a departed engineer's `bvx login`
  key keeps working — apply it promptly.
- Digests for `202607280013`–`202607280023` were refrozen during review on the premise that those
  files have reached **no database beyond ephemeral CI**. Confirm none of them was ever
  hand-applied to wyfz before replaying (wyfz has no ledger to detect drift).

---

## Current state (2026-07-27)

| Area | State |
|---|---|
| API usage tracking (`/v1/usage`) | ✅ **fixed live** — wyfz `usage_log` reconciled, returning `200 OK` |
| All audit code fixes | 🟡 green on branch `audit-fixes-2026-07-27`, **not deployed** |
| Worker (`worker-production`) | 🔴 crashlooping — `authoritative` role but no `STRIPE_SECRET_KEY` |
| Compressor (`compressor-production`) | 🔴 serving an old build in `development` mode, body-drop bug still live |
| Data | 🔴 split across two Supabase projects (`wyfz` = declared prod, `amjcc` = what the dashboard serves) |
| Secrets | 🔴 leaked `sk_live` Stripe key must be **rotated** (deleting the Vercel var was not enough) |

**Golden rule for the whole runbook:** do NOT deploy the rebuilt `wyfz` dashboard bundle to
production until the `amjcc → wyfz` data migration has completed. Otherwise live users are pointed
at an empty database and lose their sessions/accounts.

---

## Phase 0 — Pre-flight (do first, in order)

0.1 🔴 **Rotate the leaked `sk_live` Stripe key.** Stripe Dashboard → Developers → API keys → roll the
   `sk_live_51T1Yq8…` key. Then update `STRIPE_SECRET_KEY` (the correctly-named var) on Vercel with the
   new value. Confirm the old key is revoked, not just removed from Vercel.

0.2 🔴 **Rotate the GCP KMS service-account key** if `GCP_SA_KEY_JSON` was ever exposed; prefer moving to
   Workload Identity Federation (tracked separately — not a go-live blocker if the current key is clean).

0.3 🔴 **Merge the fix branch.** Push + open the PR, review, merge to `main`:
   ```
   git push -u origin audit-fixes-2026-07-27
   gh pr create --title "Enterprise audit fixes (2026-07-27)" --base main \
     --body "Audit sweep: compress body-drop, rate-limit bypass, billing 429/readiness, postcss CVE, migration hardening, docs + runbooks. Green: 159 node + 57 dashboard + 936 pytest, npm audit 0 high."
   ```
   Do NOT auto-deploy on merge yet — deploy is sequenced in Phase 3/4.

0.4 ✅ Branch is green (159 node, 57 dashboard, 936 pytest, 0 high npm-audit). Re-run CI on the PR to confirm.

---

## Phase 1 — Consolidate data (`amjcc → wyfz`)

The single biggest architectural gap. Follow `docs/DATA_MIGRATION_amjcc_to_wyfz.md` + `scripts/db/migrate-project-data.sh`.

1.1 🔴 Obtain **session-pooler connection strings** for BOTH projects (Supabase → each project → Connect
    button → Session pooler). Source = `amjcc`, destination = `wyfz`.

1.2 🔴 **Reconcile the destination schema first.** `usage_log` is already fixed (Phase-0-adjacent);
    apply the rest of the chain so every table matches: run `scripts/db/reconcile-production-prelude.sql`
    then `scripts/db/apply-migrations.sh --db-url "$WYFZ_DSN"` (dry-run/status first). Confirm the
    `brevitas_schema_migrations` ledger before applying edited dated migrations, or the checksum guard fires.
    The applier keeps the DSN out of `psql`'s argv by routing it through `scripts/dr/libpq-exec.py`,
    so `python3` must be on PATH.

1.3 🔴 **Rehearse on a staging copy** of `wyfz` before touching prod. Take a PITR checkpoint / backup.

1.4 🔴 **Migrate.** `scripts/db/migrate-project-data.sh` (defaults to `--dry-run`; add `--execute` only
    after a clean dry run). `auth.users` must go via the Supabase Auth admin API to preserve UUIDs
    (pg_dump of `auth` won't carry identities cleanly). Schedule a **source read-only window** during cutover.

1.5 🔴 **Reconcile row counts** per table (source vs destination) before declaring success. Abort path is
    documented in the script header.

**Verify:** `select count(*)` parity on `profiles`, `api_keys`, `usage_log`, `billing_ledger`,
`billing_accounts`, `organizations`, `waitlist`.

---

## Phase 2 — Environment completeness (env BEFORE redeploy)

Use `docs/ENV_ROLLOUT_CHECKLIST.md`. Set everything, then deploy — never the reverse (the new code
hard-fails fast on missing config by design).

2.1 🔴 **Vercel (app):** most already set. Confirm `BREVITAS_API_URL` is `https://api.brevitassystems.com`,
    `NEXT_PUBLIC_/VITE_SUPABASE_*` point at **wyfz**, `STRIPE_SECRET_KEY` = the rotated key, and add
    `BREVITAS_BILLING_WEEKLY_CAP_USD` **only when you intend to enable billing** (Phase 5).

2.2 🔴 **Railway API (`Brevitas-Systems`):** confirm `REDIS_URL` is a real `rediss://` TLS URL (Railway's
    built-in Redis has no TLS — app fails closed), set `FORWARDED_ALLOW_IPS` to the Railway edge CIDR
    (new hard requirement), `ALLOWED_ORIGINS=https://brevitassystems.com`, KMS block + `GCP_SA_KEY_JSON`
    present, `BREVITAS_ENV=production`.

2.3 🔴 **Railway worker (`worker-production`):** it crashes because the start command forces
    `BREVITAS_WORKER_BILLING_ROLE=authoritative` but `STRIPE_SECRET_KEY` is absent. Pick ONE:
    - **(a) Enable billing:** add the rotated `STRIPE_SECRET_KEY` + `BREVITAS_BILLING_WEEKLY_CAP_USD`
      to the worker service → it boots authoritative. (Repo/topology tests expect authoritative.)
    - **(b) Defer billing:** in the Railway dashboard, edit the worker **start command** to
      `…env BREVITAS_WORKER_BILLING_ROLE=nonbilling…` → it boots and processes jobs, billing recovery off.
      Flip back to authoritative later. (Do this in the dashboard, not the repo — `test_production_topology`
      asserts the repo default stays authoritative.)

2.4 🔴 **Compressor:** set `BREVITAS_ENV=production` (it's currently running in `development`), confirm the
    shared `BREVITAS_COMPRESS_TOKEN` matches API + worker.

---

## Phase 3 — Deploy (order matters)

Deploy the merged `main` to each service **in this order**, waiting for `/ready` (or `/v1/health/ready`) green before the next:

3.1 🔴 **Compressor** first (API depends on it). Ships the body-drop fix → `/v1/optimize` works, savings > 0.
3.2 🔴 **API** second. Ships rate-limit fix, billing safety, the `next`/security fixes.
3.3 🔴 **Worker** last (per its Phase-2.3 decision).

**Verify each:** `railway logs -d -s <svc> --lines 50` shows a clean startup and healthcheck pass; no
`RuntimeError`, no `400`/`500` on core routes.

---

## Phase 4 — Dashboard repoint + rebuild

4.1 🔴 Confirm Vercel `NEXT_PUBLIC_/VITE_SUPABASE_*` = **wyfz** (Phase 2.1). The bundle bakes these at build time.
4.2 🔴 Redeploy Vercel so `npm run build:dashboard` regenerates the (now-untracked) bundle against wyfz.
4.3 🔴 **Only now** is it safe — Phase 1 has populated wyfz, so users won't hit an empty DB.

**Verify:** load the dashboard, sign in, confirm data appears; `curl https://brevitassystems.com/api/version`
matches the deployed SHA.

---

## Phase 5 — Enable billing (business decision)

5.1 🔴 Decide `BREVITAS_BILLING_WEEKLY_CAP_USD` (fail-closed operator ceiling). Setting it + `BREVITAS_BILLING_ENABLED=true`
    turns on **real charging** on the live Stripe key — do this deliberately.
5.2 🔴 Ensure exactly ONE worker is `authoritative` (Phase 2.3a) before enabling; verify
    `BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER` only after proving no other meter-event writer exists.
5.3 🔴 Run `npm run billing:setup` against the live key if `STRIPE_PRICE_ID`/meter aren't already provisioned.

---

## Phase 6 — Release gates & operational readiness

6.1 🔴 Add a `DATABASE_URL` secret to the GitHub **staging** and **production** Environments. This secret is
    now **REQUIRED for any release run**: `scripts/ci/check-schema-drift.mjs` fails closed and exits 1
    without it, which blocks the rest of the release chain — it does **not** no-op. `--allow-missing-credentials`
    is a local-only escape and is deliberately absent from the `release.yml` invocation (pinned by
    `tests/release_security.test.mjs`), so do not add it to CI to work around a missing secret.
6.2 🔴 Populate `OPERATIONAL_READINESS_EVIDENCE_JSON` (backups/PITR/restore/telemetry/alerts/on-call/rollback)
    and run the new `release.yml` orchestrator: migrations check → schema-drift → operational-readiness →
    preflight → staging smoke → canary.
6.3 🔴 Prove **backup + restore** at least once end-to-end (Supabase PITR restore into a scratch project).

---

## Phase 7 — Post-deploy verification + rollback

**Smoke (prod):** one real request through `/v1/messages` with a live key → 200; `/v1/usage` → 200 and a row
lands in wyfz; dashboard shows it; compressor returns >0% savings; worker `/ready` green.

**Rollback:** each Railway service → `railway redeploy` to the previous deployment; Vercel → promote the prior
production deployment; DB → PITR to the pre-cutover checkpoint. Keep the `amjcc` project **read-only but intact**
until wyfz has run clean for a full billing week.

---

## Enterprise-readiness checklist (the actual bar)

- [ ] Fix branch merged & deployed to all three services (Phase 0/3)
- [ ] Data consolidated onto wyfz; amjcc retired read-only (Phase 1)
- [ ] Leaked Stripe key rotated; secrets clean (Phase 0)
- [ ] Redis TLS, KMS, `FORWARDED_ALLOW_IPS`, tenancy secrets all set (Phase 2)
- [ ] Dashboard serves wyfz, post-migration (Phase 4)
- [ ] Billing decision made & exclusive-writer proven (Phase 5)
- [ ] Release gates green + backup/restore proven + on-call/rollback documented (Phase 6)
- [ ] Prod smoke passes end-to-end (Phase 7)

Until every box is checked, the honest status is **not enterprise-ready** — but the code is done and the
path above is entirely owner-gated execution, not further engineering.
