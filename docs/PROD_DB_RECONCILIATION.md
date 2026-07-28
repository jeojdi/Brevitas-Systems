# Production Supabase schema reconciliation runbook

**Status:** this is the ROOT BLOCKER for the enterprise release. Until it is done, the
API on Railway (`api.brevitassystems.com`) keeps failing its healthcheck and billing
cannot be turned on.

**Owner actions vs. Claude actions.** Every step that touches a live secret (the prod
DB connection string / password) or mutates production **must be run by you (James) in
your own terminal**. Claude never handles the prod DB password. Claude *can* rehearse
the migration chain against a **schema-only** copy of prod (no secrets, no row data),
which is what de-risks the real run.

---

## Why a plain `supabase db push` is unsafe here

Audited state of prod project `wyfzmfnswtzyhwbltbpy` (2026-07-24):

- **7 tables present**, hand-created out of order via the SQL editor:
  `user_keys, profiles, api_keys, provider_config, usage_log, bvx_device_auth, waitlist`
- **24 tables missing**, including the ones that gate the release:
  `organizations, organization_members, billing_accounts, billing_ledger,
  semantic_cache, billing_events, legal_acceptances, …`
- **No migration ledger.** `supabase_migrations.schema_migrations` does not reflect
  reality, so `supabase db push` has no trustworthy idea of what is/isn't applied.
- The present tables are **not a clean prefix** of the migration order (`waitlist` from
  a LATE migration exists while EARLY `billing_events`/`legal_acceptances` do not), so a
  "tail apply" would skip the missing early objects. The columns on the hand-made tables
  may also differ from what later `ALTER`s expect.

Conclusion: apply the **full 46-migration chain**, which is guarded (`create ... if not
exists`, idempotent alters — proven by the double-apply rehearsal), **but only after
rehearsing it against a clone shaped like prod's actual schema.**

## Rehearsal result against prod's actual schema (2026-07-26) — now 46/46 clean

A schema-only snapshot of prod (`scratchpad/prod_schema_snapshot.sql`, 7 tables) was
loaded into a throwaway loopback Postgres 17 and the full chain applied in filename
order. The first pass hit **two genuine collisions** caused by prod's divergence; both
are now fixed in the migrations themselves, and a re-run applies **46/46 clean** (0
collided), with all 8 gate tables created, `user_keys` dropped as designed, and
`usage_log` gaining `organization_id`/`customer_id`. The clean-DB harness
(`scripts/ci/run-migration-tests.sh`) still passes (fresh + upgrade + double-apply).

**Re-rehearsed 2026-07-27 after the cache-warming additions: 51/51 clean.** The three
new migrations (`202607280001_cache_warming`, `202607280002_usage_stats_cache_metrics`,
`202607280003_multi_provider_warming`) apply on top of the same prod snapshot
(bootstrap stubs from `scripts/ci/migration-bootstrap.sql`, then snapshot, then the
full fresh-manifest chain) with zero failures, and the clean-DB harness passes with
the cache-warming behavioral assertions included.

The two divergences found and fixed:

1. **`profiles` policies already exist.** Prod hand-created `profiles` plus its two RLS
   policies. `20260624_create_profiles.sql` issued *unguarded* `create policy` (Postgres
   has no `create policy if not exists`), so it aborted. **Fix:** added
   `drop policy if exists` before each `create policy` (the convention 3 other
   migrations already use). No-op on a fresh DB.

2. **`round(double precision, integer)` does not exist** (float schema drift). Prod's
   `usage_log.cost_saved_usd` and `.brevitas_fee_usd` are `double precision`, not the
   intended `numeric(18,10)`. `20260710_cloud_usage.sql` only did
   `add column if not exists`, which **cannot change the type of a column that already
   exists**, so the drift survived; then `202607170006_database_scaling.sql` calls
   `round(sum(brevitas_fee_usd), 8)` — `round(x, n)` has only a `numeric` overload — and
   failed. **Fix:** added explicit `alter column … type numeric(18,10) using …::numeric`
   for both columns in `20260710_cloud_usage.sql`. No-op where already numeric; on prod
   it converts the float columns in place. This is the concrete instance of the known
   "prod holds billing amounts in float; migrations can't self-repair it" drift — the
   `if not exists` guard that makes the chain idempotent is exactly what let the drift
   persist, so it needed an explicit coercion.

(A third first-pass failure in `20260714_legal_acceptances.sql`, `column
"raw_user_meta_data" does not exist`, was a **rehearsal artifact** — the throwaway
`auth.users` stub lacked that column, which real Supabase always has. With a
Supabase-shaped stub it applies clean; nothing to fix.)

## The one destructive step — decide before you run

`supabase/migrations/202607170001_enterprise_tenancy.sql` (line ~220) contains:

```sql
drop table if exists public.user_keys;
```

This is **by design** (comment at lines 218–219: raw creds are no longer stored
server-side; replaced by KMS `key_repositories`). But prod's `user_keys` currently
**exists and holds live rows** — applying the chain **will drop that table and its data.**

**Do not proceed to Step 4 until you have explicitly decided** that dropping prod
`user_keys` is acceptable (it should be, since the KMS path supersedes it — but confirm
no live code still reads it, and the pg_dump backup in Step 3 captures the rows either
way).

---

## Step 0 — Prereqs (you)

- Supabase CLI logged in (`supabase login`).
- The prod DB connection string / password (from Supabase dashboard → Project Settings →
  Database). **This is a secret — keep it in your shell only; never paste it into chat.**
- `pg_dump`/`psql` (Postgres 16 client) available locally.

## Step 1 — Schema-only dump of prod (you → hand the file to Claude)

This produces a **secret-free, data-free** structural snapshot. Safe to share.

```bash
# uses your secret DSN from the env, writes structure only (no rows, no roles/grants)
pg_dump "$PROD_DB_URL" \
  --schema-only --no-owner --no-privileges --schema=public \
  -f scratchpad/prod_schema_snapshot.sql
```

Then tell Claude the file is written. Claude will load it into a throwaway loopback
Postgres and run the **full migration chain on top of it** — this validates the
migrations against prod's *actual* divergence (not just a clean DB, which was already
rehearsed green). Claude reports any `ALTER`/FK failure with the exact migration + line.

**DONE (2026-07-26):** this rehearsal has been run against `prod_schema_snapshot.sql`.
It surfaced two real collisions, both now fixed in the migrations (see "Rehearsal
result" above); a re-run is **46/46 clean**. So Step 4 below now applies without aborting
— but still take the Step 2 backup and, if possible, the Step 3 staging pass first.

## Step 2 — Full backup of prod (you) — rollback insurance

```bash
pg_dump "$PROD_DB_URL" --format=custom --no-owner --no-privileges \
  -f scratchpad/prod_full_backup_$(date +%Y%m%d_%H%M).dump
```

Keep this off the repo. It is your restore point if Step 4 goes wrong.

## Step 3 — Rehearse on STAGING Supabase (you) — real PostgREST/RLS

The loopback rehearsal (Step 1) validates SQL but not PostgREST/RLS behavior. If the
staging Supabase project is available, apply the chain there first:

```bash
supabase link --project-ref <STAGING_REF>     # prompts for staging DB password (secret)
supabase db push                               # or apply the chain with psql, ordered
```

Confirm `organizations`, `billing_accounts`, `semantic_cache` become queryable and that
`usage_log` accepts an insert with `organization_id`/`customer_id`.

## Step 4 — Apply to PRODUCTION (you) — only after Steps 1–3 are green + go-ahead

Because there is no ledger, apply the ordered chain directly with `psql` (idempotent
guards make this safe), then seed the ledger so future `db push` is honest:

```bash
# apply every migration in filename order (guards skip what already exists;
# 202607170001 will DROP user_keys — Step 3 decision must be settled)
for f in $(ls supabase/migrations/*.sql | sort); do
  echo ">>> $f"
  psql "$PROD_DB_URL" --set ON_ERROR_STOP=1 -f "$f" || { echo "FAILED at $f"; break; }
done
```

(If you prefer `supabase db push`, first `supabase migration repair` to seed the ledger
to a known baseline — but the direct ordered psql apply above is the most predictable
given the divergence.)

## Step 5 — Verify (you or Claude, read-only)

- `curl -s https://api.brevitassystems.com/v1/health/ready` → expect `200` (was 404/503).
- Trigger a Railway redeploy of `Brevitas-Systems`; the healthcheck should pass now that
  `organizations` exists (`SupabaseStore.healthy()` GET organizations → 200).
- Confirm `semantic_cache` present and API cache env set to `backend=supabase`.

## Step 6 — Only then: billing enablement

Separate from the DB. After DB green and secrets set on Railway (see handoff memory),
flip `BREVITAS_BILLING_ENABLED=true`. Leave it `false` until Step 5 passes.

---

## Rollback

If Step 4 fails midway: stop, restore from the Step 2 custom-format dump:

```bash
pg_restore --clean --if-exists --no-owner --no-privileges -d "$PROD_DB_URL" \
  scratchpad/prod_full_backup_<stamp>.dump
```

then re-open this runbook and fix the failing migration against the Step 1 clone before
retrying.
