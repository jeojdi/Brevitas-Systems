# WYFZ Apply Plan — 202607280004 → tip

Bring the production Supabase project (**wyfz**) from `202607280004_onboarding_local_proxy_evidence.sql`
to the tip of `supabase/migrations/`, one file at a time, by hand.

Audience: a human with production access. Every command is copy-pasteable and assumes nothing.
Nothing in this document runs itself.

- Written: 2026-07-30
- Repo state assumed: branch `chore/retire-per-row-fee-trigger`, working tree at the commit whose
  file checksums are listed in [§3](#3-verify-you-are-applying-the-reviewed-bytes).
- Files to apply: **20** — `202607280005` … `202607280024`.

---

## 0. Laws

These are not preferences. Violating any one of them can destroy financial evidence or double-bill
a customer.

| # | Law | Why |
|---|-----|-----|
| L1 | **Never `supabase db push` against wyfz.** | wyfz has **no migration ledger**. `db push` derives order from the filesystem and would replay the entire chain from scratch. `20260720_split_savings_metrics.sql` sorts last lexically but is an *early* numeric version, so filesystem order is provably wrong for this repo (see the header of `scripts/db/apply-migrations.sh`). |
| L2 | **Never replay `202607170012_receipt_accounting_alignment.sql` against wyfz.** | It guards on `queue_brevitas_fee_after_usage` being *present* on `public.usage_log` and raises otherwise. That trigger is already absent on wyfz. It is also already applied. A replay aborts, and a "fix" that satisfies its guard means re-attaching the per-row fee trigger — which is exactly what `202607280006` exists to prevent. |
| L3 | **Never replay the whole chain blind.** Only the 20 files named in [§6](#6-apply-order) may be applied. Everything at or below `202607280004` is already on wyfz and must not be re-run unless a specific step below says so. | Most pre-`280004` files are `create table if not exists` + `create or replace`, but several are not idempotent (`insert` seeds, `alter … add constraint` without `if not exists`, one-time backfills), and several are checksum-frozen precisely because they have already run against a ledger-less database. |
| L4 | **One file per invocation.** `supabase db query --linked -f <one file>`. Never concatenate. Never pipe two files. | Each file is exactly one transaction (verified: [§4](#4-prove-the-applier-honours-transactions)). Concatenating them merges those transactions and destroys the per-file blast-radius guarantee this whole runbook rests on. |
| L5 | **Do not create `public.brevitas_schema_migrations` during this pass.** | `scripts/db/apply-migrations.sh` records applied files there. Creating it now would leave it containing only these 20 rows, so a later `--status` would report the 60+ earlier migrations as *unapplied* and invite exactly the blind replay L1/L3 forbid. Keep the hand-written apply log from [§5](#5-session-setup-and-evidence-capture) instead. |
| L6 | **Stop on the first non-zero exit.** Do not proceed to the next file, do not retry with edits. Go to [§10](#10-abort-and-rollback). | Preconditions in these files are chained: `280013` refuses to install if `280010` has not, `280009` refuses if the per-row trigger is back, etc. A skipped failure produces a half-built settlement path, which is the one state nobody has tested. |

---

## 1. Ground truth (read-only probe of wyfz, 2026-07-29)

Everything below was observed, not assumed. Re-confirm it with [§7](#7-global-preflight) before you
apply anything — if any line disagrees, **stop and re-plan**.

- Schema stops at `202607280004`. All four of `period_settlement_ledger`,
  `billing_halting_conditions`, `organization_billing_arrangement`,
  `release_billing_ledger_unsent` are **absent**.
- `public.usage_log` has **zero** non-internal triggers. The per-row fee trigger
  `queue_brevitas_fee_after_usage` is **not attached**.
- `public.billing_ledger` has **zero** rows.
- Usage since 07-15 (per day totals): 07-15 `2715`, 07-16 `3662`, 07-17 `551`,
  **07-18 … 07-26 no rows at all**, 07-27 `5583`, 07-28 `11406`, 07-29 `12418`, 07-30 `1491`.
  And on **every** day: `authoritative = 0`, `authoritative-with-savings = 0`, `priced = 0`.

Three consequences that shape this plan:

1. **The `280005`–`280013` apply cannot double-write anything.** There is no per-row writer, and
   the ledger it would have written into is empty. Every `freeze_still_held` precondition in
   `280008`/`280009`/`280012`/`280013` will pass on the first try.
2. **`202607280006` is a no-op on wyfz.** `drop trigger if exists` finds nothing. It is still
   applied, because its `do $$ … $$` assertion is the thing that makes "no per-row writer" a
   *checked* property rather than a coincidence, and because `280008`/`280009`/`280012`/`280013`
   are written to refuse installation next to that trigger.
3. **Nothing that lands in this pass can bill anyone.** Traffic is 100% non-authoritative and 100%
   unpriced, so `billing_period_settlement_evidence` computes zero eligible rows for every period,
   and `settle_billing_period` writes `draft` rows only. Promotion to `pending` —
   `promote_billing_period_settlement` — is granted to **nobody** and is reachable only from a
   direct SQL session.

### 1.1 One open contradiction — resolve before §6 Window B

Session memory records *"the 1,897 fee rows were hand-repriced to 25% on 07-29"*, but today's probe
found `public.billing_ledger` **empty** on wyfz. Those two statements cannot both describe wyfz's
`billing_ledger`. The most likely reconciliation is that the repriced rows are in
`public.billing_events` (which `202607280014` drops a view over) or on a different project.

This matters for exactly two steps:

- `202607280007` asserts `not exists (select 1 from public.billing_ledger where id >= <seq start>)`
  — the settlement id space must not collide with the per-row ledger id space in Stripe
  identifiers. Empty ledger ⇒ passes trivially. A non-empty ledger with ids ≥ 1,000,000,000 ⇒
  **aborts**, correctly.
- `202607280014` drops `public.billing_monthly`, which aggregates `public.billing_events`.

The preflight in [§7](#7-global-preflight) counts both tables. **Do not proceed past Window B until
the number you see there is understood.** If `billing_events` holds 1,897 rows, that is consistent
and fine; if `billing_ledger` is non-empty, stop.

---

## 2. What is in scope, and the 280014–280024 decision

The lane brief asked for `280005`–`280013` and a justified decision on the concurrent session's
`280014`–`280023`. Note first: **there is a `202607280024`**, and it is not optional if `280015`
lands — `280024`'s precondition requires `public.assert_browser_role_table_privileges()`, which
only `280015` creates. `280015` without `280024` leaves `audit_events`,
`organization_invitations`, `legal_acceptances` and the `audit_evidence_archive` schema holding
Supabase's project-default `GRANT ALL` for `anon`/`authenticated` — including `TRUNCATE`, which is
not subject to RLS at all.

**Verdict: apply all 20 in the same pass, in three windows, with `202607280017` promoted to the
front.**

The justification rests on four properties I verified across all twenty files:

1. **Every file is exactly one transaction.** `grep -c '^begin;'` = 1 and `grep -c '^commit;'` = 1
   for all 20, and `grep -ci concurrently` = 0 for all 20. There is no `CREATE INDEX CONCURRENTLY`,
   no `ALTER TYPE … ADD VALUE`, no `VACUUM` — nothing that cannot run inside a transaction. So a
   mid-file failure rolls the whole file back, and partial DDL is impossible (subject to
   [§4](#4-prove-the-applier-honours-transactions)).
2. **No file requires app code that is not yet deployed.** The dependency runs the other way:
   the currently-deployed code calls RPCs whose *signatures* none of these files change, and three
   files fix bugs in code that is live right now.
3. **Every fixture written by an embedded self-test unwinds itself.** `280009`, `280010`, `280012`
   and `280013` insert probe rows into `auth.users`, `public.organizations`,
   `public.billing_accounts`, `public.usage_log`, `public.warm_budget_ledger` and
   `public.period_settlement_ledger`. Each does so inside an inner `begin … exception` block that
   is deliberately unwound by raising a private SQLSTATE (`ZZ010`, `ZZ013`, …) — a plpgsql
   exception block is an implicit savepoint, so the fixture is rolled back and the probe then
   *asserts* nothing was left behind. `280009` and `280012` additionally `delete` their probe rows
   explicitly. Belt and braces: the enclosing file transaction would roll them back anyway.
4. **Every precondition is satisfiable from wyfz's current state.** All resolved and listed
   per-file below; the aggregate check is [§7](#7-global-preflight).

### 2.1 Why `202607280017` goes first, out of manifest order

`202607280017_warm_evidence_retention_floor.sql` fixes a **live, ongoing destruction of billing
evidence**.

The deployed worker (`api/worker.py` at `HEAD`, line 904) calls:

```
_warm_bound("BREVITAS_WARM_RETENTION_DAYS", 7, 1, 365)
```

— i.e. it invokes `public.purge_warm_state(7)` on a **300-second loop**, and
`purge_warm_state` deletes `warm_budget_ledger` rows older than that horizon. But
`warm_budget_ledger` is the only record of what a warming ping cost, and
`billing_period_settlement_evidence` (`280008`, and again in `280012`/`280013`) *recomputes* the
warm deduction from it specifically "so that no settlement writer can supply it". A Stripe period is
7 days and settlement is manual, so a period is always written **after** its earliest days have
already been purged — the deduction shrinks toward zero and the fee ceiling rises to the
*undeducted* savings. That is an overcharge on savings manufactured with the customer's own
provider budget.

`280017` floors the ledger horizon at 365 days **inside** the function, so no caller can shorten it
even by passing 7. It has exactly one precondition — `public.purge_warm_state(integer)` must exist,
from `202607280001`, already on wyfz — and it touches no object that any other file in this pass
touches (`purge_warm_state` appears in only `202607280001` and `202607280017` across the whole
migrations directory). Applying it before `280005` is therefore safe, and the from-scratch replay
end state is unchanged because the manifest order is what CI replays.

**Faster mitigation, do this first — it needs no DDL at all.** The deployed worker reads the
horizon from the environment, and `_warm_bound` clamps to `[1, 365]`:

> Set `BREVITAS_WARM_RETENTION_DAYS=365` in the API/worker service environment and restart the
> worker. This stops the bleed within one deploy cycle, before you touch the database.

Then still apply `280017`, because the env var is a configuration promise and `280017` is a
structural one.

### 2.2 Risk classification

Flagged per the brief: risky on a live DB, or coupled to app code.

| File | Risk on a live DB | App-code coupling |
|---|---|---|
| `280005` | Cursor-driven **backfill** inserts real `devices` + `installations` rows. Bounded by the number of unrevoked device keys with an activation event and no installation row — count it first. | Unblocks the **already-shipped** BVX 0.1.27. Signature `(text,text,text)` unchanged from `202607170010`. Safe with deployed code. |
| `280006` | None. No-op on wyfz. | None. |
| `280007` | Structure only. New table, 7 indexes, 2 triggers on a table that starts empty. | None — nothing reads or writes the table yet. |
| `280008` | Seeds **one** singleton row. | None. |
| `280009` | Probe writes to `auth.users`/`organizations`, self-unwound. **Revokes** `EXECUTE` on `assert_billing_period_halting_conditions` from `service_role`. | None deployed calls either. |
| `280010` | Probe writes to `period_settlement_ledger`, self-unwound. Advances that table's identity sequence — harmless, nothing reads its position. | None. |
| `280011` | None. One new function. | **New** `api/billing_recovery.py` calls `release_billing_ledger_unsent`. **DB must go first.** |
| `280012` | None. `create or replace`, diagnostics-only fix. | None. |
| `280013` | Largest file (1,598 lines). Probe writes to `auth.users`/`organizations`/`billing_accounts`/`usage_log`/`warm_budget_ledger`/`period_settlement_ledger`, self-unwound. `drop function` + recreate widens the evidence OUT list. | **New** `src/app/api/billing/status/route.ts` calls `billing_period_settlement_summary`. **DB must go first** or that route 500s. Deployed route does not call it, so DB-first is safe. |
| `280014` | `drop view public.billing_monthly`. **PITR-only reverse.** | Repo-wide grep finds no caller outside the two migrations that create it. |
| `280015` | ⚠️ **`ACCESS EXCLUSIVE`** on 14 tables including `public.usage_log`, which is taking ~12k inserts/day. Drops the `"Users can update own profile"` policy. **PITR-only reverse.** | Verified safe: the dashboard bundle's *entire* Supabase surface is `supabase.auth.*` — zero `.from()` and zero `.rpc()` calls. `supabase.auth.updateUser` writes `auth.users`, not `public.profiles`. `service_role` is untouched. |
| `280016` | Widens tenant **erasure** to delete `warm_credentials`/`warm_prefixes`/`warm_budget_ledger`. Nothing is deleted at apply time — only when `scripts/dr/tenant-data.sh` is run. **PITR-only reverse.** | Signatures preserved; `create or replace` keeps `202607200011`'s grants. |
| `280017` | None. `create or replace`. **Apply first — see §2.1.** | Fixes a bug in **deployed** code. The `api/store.py:3326` SQLite mirror and the `api/worker.py` default should follow, but neither blocks. |
| `280018` | None. `create or replace` of two warming RPCs, signatures unchanged. | Fixes a bug in **deployed** `warm_prefix_observe` behaviour. Safe. |
| `280019` | ⚠️ Has a **refuse-if-exists** precondition: aborts if `public.company_admin_revoke_key(uuid,uuid,uuid,text)` exists. `202607170009:174` drops it, so wyfz should be clear — **verify anyway**. | Adds a *new* function `company_admin_revoke_tenant_key`; leaves `company_admin_revoke_dashboard_session_key` in place. Deployed `api/store.py` keeps working unchanged. New code must deploy to expose customer device-key revocation. |
| `280020` | Widens the `claim_ai_job` expiry sweep to lease-expired abandoned rows. Rows a live worker still owns are never touched. First call after apply may terminalize a backlog of abandoned jobs. | Signature `(text,integer)` unchanged; deployed `api/jobs.py` unaffected. |
| `280021` | ⚠️⚠️ **Replaces an `AFTER INSERT` trigger on `auth.users`.** It fires on the **next real signup**, the moment it lands. A defect here breaks account creation for everyone. | Verified low-risk: `on conflict (user_id)` is valid (`legal_acceptances.user_id` is the PK, `20260714:2`), and the pinned versions `2026-07-14` / `2026-07-15` are **byte-identical** to what the deployed bundle sends (`HEAD:dashboard/src/components/Auth.jsx:111-112`). So for a real signup the recorded values do not change; only the *authority* for them does. Requires PG14+ for `create or replace trigger` (Supabase is ≥15). |
| `280022` | ⚠️ Non-concurrent **`CREATE UNIQUE INDEX`** on `public.audit_events` (append-only, 400-day retention, potentially the largest table here) — takes `SHARE`, which **blocks audit appends** for the duration, and every company-admin RPC appends. Plus a **one-time `UPDATE public.api_keys`** backfilling `created_by` for device keys from `device_key.activated` audit events. **PITR-only reverse.** | Signatures unchanged; deployed `api/company_admin.py` keeps working. Behaviour change: `'%.read'` rows are excluded from the returned page, so paging cannot feed on itself. |
| `280023` | ⚠️ **Arms deletion.** Adds `delete from public.waitlist` (24-month cutoff) and in-place minimization of `usage_log` (13-month cutoff) to `compliance_run_retention`. **Nothing runs at apply time** — verified: `compliance_run_retention` is invoked only by `scripts/dr/retention.sh` (a manual `psql` script), and `compliance_retention_worker_cycle` has **no caller in `api/`**. `alter table … add column not null default 0` uses PG11+ fast defaults; the four `CHECK`s validate under `ACCESS EXCLUSIVE` on a small evidence table. **PITR-only reverse.** | None. **Do not run `scripts/dr/retention.sh` in the same window** — do a `--dry-run` first and read the new counters. |
| `280024` | ⚠️ **`ACCESS EXCLUSIVE`** on `audit_events`, `organization_invitations`, `legal_acceptances`, and every relation in `audit_evidence_archive`. **PITR-only reverse.** **Hard dependency on `280015`.** | Same verification as `280015`: the dashboard never touches these with the anon key. `legal_acceptances` keeps `SELECT` because its "view own" RLS policy is what narrows it. |

---

## 3. Verify you are applying the reviewed bytes

All twenty files are already pinned in `scripts/ci/migration-frozen-checksums.txt`, and the working
tree matches. Confirm both before you connect to production.

```bash
cd /path/to/Brevitas-Systems

# 1. Repo-level check: file bodies match the frozen manifest.
shasum -a 256 -c <(grep -E '2026072800(0[5-9]|1[0-9]|2[0-4])_' scripts/ci/migration-frozen-checksums.txt)
```

Expect twenty `: OK` lines and nothing else.

```bash
# 2. Independent confirmation, against the values recorded in this runbook.
shasum -a 256 supabase/migrations/2026072800{05,06,07,08,09,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24}_*.sql
```

```
94626dccbcad9563ef0dab30f4012930814a2961e89cc7e0bf5b8d3fa2a3ac6b  202607280005_installation_on_device_activation.sql
db5d1b59e07977341aba308c9cdd66e387ac92a43f37e1749d6b61689ec0a72b  202607280006_retire_per_row_fee_trigger.sql
8d2186990b7b3de7ad4afb3a26603f46e162e9c37092d6fb8a12dca95503de0b  202607280007_period_settlement_ledger.sql
9e1e80c0bcb8e1c7c3f6e451c1c6e41466873bc1886d5210cb39cb350bd17f0f  202607280008_billing_halting_conditions.sql
103b333c5c0ce8acabc2d41664dbe7d67683e2b6afaefc4228dcbe2cfebf499b  202607280009_billing_arrangement_attestation.sql
b56e217ae9fd6b2fd27cd630a32921c11391b5e0cf3c6cd12fa59cc1f848f315  202607280010_period_settlement_send_latches.sql
c8143eaf0b04d3624c9d3bc73da21bc5b68d8752f3ce08f3991f62d4a87ad074  202607280011_billing_ledger_unsent_release.sql
8eecf62096fbdf936524bdcd054e166e0130eb42e342d65a0abc50528b2069d9  202607280012_settlement_evidence_warm_days.sql
40549af4f8bba148333492122de6ccddca6e6c5582e065a491cdfd9c6aed85e3  202607280013_period_settlement_writer.sql
eb5a95e7d22e453f09537f209a0cdf627548d1e9e666f61cfa5b59f4e4893eca  202607280014_drop_billing_monthly_view.sql
41680e6d217e28fbb779cf41a558a79acfa15a5a9fb65beef5ee4bd431e80725  202607280015_browser_role_privilege_contract.sql
4a9e874f6f2ef4c4faf301d3616167eb75e44351bdb0e312efeca704bb128bdd  202607280016_compliance_warm_state_erasure.sql
3352beade3cb00a36af57266cc2c52c1eea47404258b107b21ae312bcb06a2c3  202607280017_warm_evidence_retention_floor.sql
6c62bfcb2c9dc388de9096da26cfeaee9aa389444fade8dda3350d89aaa52c11  202607280018_warm_claim_lease_fence.sql
59a3736d7c18532c836aca41bfe8668e15c6227517ae6a7e838493eac3c7728b  202607280019_tenant_device_key_revocation.sql
166e847d3392b80ae7f25978fcc020448f89aece33a998b6e3f63b0cf6c6fa39  202607280020_expired_job_reclaim_fence.sql
86971af68b9f9f24105fe45f9f133d7b90468e5ef7d6ceea43c74a20193566b1  202607280021_server_authoritative_legal_acceptance.sql
2cc4b725c216864cd3065774b231cbd9ad6156650d0fffdd7749e1e7ca2ea10c  202607280022_audit_read_and_transition_evidence.sql
17fa82997d889e502139ab585c860a69a7ce1b6190a2e0a2bddbc85174828f8a  202607280023_retention_minimization_and_waitlist.sql
bf7e2358b134b2f42c3aaf5631cdfe52fb14ac208c6afae97c028ca839237826  202607280024_browser_role_privilege_completion.sql
```

If any line differs from **both** lists: someone edited a frozen migration. **Stop.** Do not apply.

---

## 4. Prove the applier honours transactions

Every blast-radius claim in this runbook — "a mid-file failure rolls back, partial DDL is
impossible" — depends on the applier actually honouring the `begin;` / `commit;` inside each file.
If `supabase db query` splits statements and autocommits each one, that guarantee is gone and this
plan is invalid.

Prove it once, cheaply, before applying anything.

```bash
# Confirm the subcommand exists at all. If this fails, STOP -- do NOT substitute `db push` (L1).
supabase db query --help
```

```bash
mkdir -p /tmp/wyfz-apply
cat > /tmp/wyfz-apply/atomicity-probe.sql <<'SQL'
begin;
create table public.wyfz_atomicity_probe_20260730 (x integer);
do $probe$
begin
    raise exception using errcode = '55000',
        message = 'deliberate abort: proving the applier honours the file transaction';
end;
$probe$;
commit;
SQL

supabase db query --linked -f /tmp/wyfz-apply/atomicity-probe.sql
```

This **must** fail with `deliberate abort: …`. Then verify nothing survived:

```bash
cat > /tmp/wyfz-apply/atomicity-check.sql <<'SQL'
select to_regclass('public.wyfz_atomicity_probe_20260730') is null as transaction_honoured;
SQL

supabase db query --linked -f /tmp/wyfz-apply/atomicity-check.sql
```

- `transaction_honoured = t` → the applier honours file transactions. Proceed.
- `transaction_honoured = f` → **STOP.** The applier autocommits statement-by-statement. Every
  blast-radius assessment below is void. Clean up with
  `drop table public.wyfz_atomicity_probe_20260730;` and find a transactional path before
  continuing.

---

## 5. Session setup and evidence capture

wyfz has no migration ledger and you are not creating one (L5), so **your log is the only record of
what happened.** Keep it.

```bash
cd /path/to/Brevitas-Systems
git rev-parse HEAD | tee /tmp/wyfz-apply/APPLIED_FROM_COMMIT.txt

mkdir -p /tmp/wyfz-apply/evidence
export WYFZ_LOG=/tmp/wyfz-apply/evidence/apply-log-$(date -u +%Y%m%dT%H%M%SZ).txt
: > "$WYFZ_LOG"
```

Wrapper used by every step below. It timestamps, tees, and refuses to hide a failure:

```bash
wyfz_run() {  # wyfz_run <sql-file>
  local f="$1"
  printf '\n===== %s  START %s\n' "$(date -u +%FT%TZ)" "$f" | tee -a "$WYFZ_LOG"
  if supabase db query --linked -f "$f" 2>&1 | tee -a "$WYFZ_LOG"; then
    printf '===== %s  OK    %s\n' "$(date -u +%FT%TZ)" "$f" | tee -a "$WYFZ_LOG"
  else
    printf '===== %s  FAIL  %s  <-- STOP, see docs/WYFZ_APPLY_PLAN.md §10\n' \
      "$(date -u +%FT%TZ)" "$f" | tee -a "$WYFZ_LOG"
    return 1
  fi
}
```

> Note on `pipefail`: `wyfz_run` reads the pipeline's exit status, which under the default shell is
> `tee`'s. Run `set -o pipefail` in the shell first, or re-check by eye — the migrations raise loud
> `ERROR:` lines, so a `FAIL` is never silent in the log.

**Before Window A, take a fresh PITR reference point.** Note the current UTC time and confirm in the
Supabase dashboard that PITR is enabled and its window covers it. Nine of these twenty files are
marked `REVERSE: PITR-ONLY` in their own headers. PITR is the *only* rollback for those.

```bash
date -u +%FT%TZ | tee /tmp/wyfz-apply/PITR_REFERENCE_UTC.txt
```

---

## 6. Apply order

Three windows. Do not interleave them. Stop at the end of each window, run its verification, and
only then continue.

| Window | Files | When | Gate to leave the window |
|---|---|---|---|
| **A — stop the bleeding** | `280017`, `280018`, `280020` | Immediately. No traffic constraint. | §9 Window A checks green. |
| **B — billing chain** | `280005`, `280006`, `280007`, `280008`, `280009`, `280010`, `280011`, `280012`, `280013`, `280014` | After §1.1 is resolved. No traffic constraint. | §9 Window B checks green; `billing_periods_awaiting_settlement` returns sanely. |
| **C — privilege / compliance / auth** | `280015`, `280016`, `280019`, `280021`, `280022`, `280023`, `280024` | **Low-traffic window only** — takes `ACCESS EXCLUSIVE` on `usage_log` and `audit_events`, and replaces the signup trigger. | §9 Window C checks green; one real test signup succeeds. |

Within a window, the order given is mandatory.

### 6.1 Lock hazard, and the honest limitation

`280015`, `280022`, `280023` and `280024` take `ACCESS EXCLUSIVE` (or `SHARE`, for
`280022`'s index build) on tables that are being written continuously — `usage_log` is absorbing
~12k inserts/day and `audit_events` is appended by every company-admin RPC. An `ACCESS EXCLUSIVE`
request queues behind any in-flight transaction on the table **and blocks every request that
arrives behind it**, so a single long-running reader can turn a millisecond `REVOKE` into a
multi-minute outage.

**I cannot inject `lock_timeout` through `supabase db query --linked -f`** — a `set lock_timeout`
in a separate invocation is a separate session and does not carry over, and the migration files are
checksum-frozen so a `set local` cannot be added to them. The mitigations available to you are:

1. Run Window C in the quietest window you have.
2. Keep this open in a second terminal and watch it while each Window C file runs:

```bash
cat > /tmp/wyfz-apply/locks.sql <<'SQL'
select blocked.pid            as blocked_pid,
       blocked.wait_event_type,
       left(blocked.query, 90) as blocked_query,
       blocking.pid           as blocking_pid,
       left(blocking.query, 90) as blocking_query,
       now() - blocking.query_start as blocking_age
  from pg_stat_activity blocked
  join pg_stat_activity blocking
    on blocking.pid = any(pg_blocking_pids(blocked.pid))
 where blocked.datname = current_database()
 order by blocking_age desc;
SQL

supabase db query --linked -f /tmp/wyfz-apply/locks.sql
```

3. If a Window C file hangs for more than ~30 seconds, **interrupt the client** (Ctrl-C). The
   server-side transaction aborts and rolls back — that is safe, by [§4](#4-prove-the-applier-honours-transactions).
   Confirm with `select pg_cancel_backend(<pid>)` from the query above if the client detaches
   without killing the backend. Then retry when the blocker is gone.

---

## 7. Global preflight

Run this **once**, before Window A. It is entirely read-only. Every assertion is worded so that
`true` means "safe to proceed".

```bash
cat > /tmp/wyfz-apply/preflight.sql <<'SQL'
\echo == 1. server version (280021 needs >= 14 for CREATE OR REPLACE TRIGGER) ==
select current_setting('server_version') as server_version,
       current_setting('server_version_num')::int >= 140000 as ok_pg14_plus;

\echo == 2. schema tip: these FOUR must all be absent ==
select to_regclass('public.period_settlement_ledger')          is null as psl_absent,
       to_regclass('public.billing_halting_conditions')        is null as bhc_absent,
       to_regclass('public.organization_billing_arrangement')  is null as oba_absent,
       to_regprocedure('public.release_billing_ledger_unsent(bigint,text)')
                                                               is null as rblu_absent;

\echo == 3. the per-row fee trigger must be ABSENT (L2, and every freeze_still_held guard) ==
select count(*) as non_internal_usage_log_triggers,
       count(*) filter (where tgname = 'queue_brevitas_fee_after_usage') = 0
         as fee_trigger_absent
  from pg_trigger
 where tgrelid = 'public.usage_log'::regclass
   and not tgisinternal;

\echo == 4. SEE SECTION 1.1 -- understand these numbers before Window B ==
select (select count(*) from public.billing_ledger) as billing_ledger_rows,
       (select count(*) from public.billing_events) as billing_events_rows,
       (select coalesce(max(id), 0) from public.billing_ledger) as billing_ledger_max_id;

\echo == 5. 280005/280006 prerequisites (all must be present) ==
select to_regclass('public.bvx_device_consumption_receipts') is not null as receipts,
       to_regclass('public.installations')                   is not null as installations,
       to_regclass('public.devices')                          is not null as devices,
       to_regclass('public.api_keys')                         is not null as api_keys,
       to_regprocedure('public.consume_bvx_device_idempotent(text,text,text)')
                                                              is not null as consume_rpc,
       to_regprocedure('public.queue_brevitas_fee()')          is not null as fee_fn_retained;

\echo == 6. 280007/280008/280012/280013 prerequisites ==
select to_regclass('public.warm_budget_ledger') is not null as warm_ledger,
       to_regprocedure('public.billing_period_for_occurrence(timestamptz,timestamptz,timestamptz)')
         is not null as period_fn,
       to_regprocedure('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)')
         is null as evidence_absent_as_expected;

\echo == 7. 280016 prerequisites: the company-scoped compliance wrappers ==
select to_regprocedure('public.compliance_delete_tenant_pre_company_identity(uuid,uuid,text)')
         is not null as delete_wrapper,
       to_regprocedure('public.compliance_export_tenant_pre_company_identity(uuid,uuid,text)')
         is not null as export_wrapper,
       to_regprocedure('public.compliance_export_subject_pre_company_identity(uuid,uuid,text)')
         is not null as subject_wrapper,
       to_regclass('public.warm_credentials') is not null as warm_credentials,
       to_regclass('public.warm_prefixes')    is not null as warm_prefixes;

\echo == 8. 280019: BLOCKING -- the retired generic RPC must NOT exist ==
select to_regprocedure('public.company_admin_revoke_dashboard_session_key(uuid,uuid,uuid,text)')
         is not null as session_rpc_present,
       to_regprocedure('public.company_admin_revoke_key(uuid,uuid,uuid,text)')
         is null      as generic_rpc_absent_REQUIRED,
       to_regprocedure('public.lock_company_admin_namespace(uuid)') is not null as lock_ns,
       to_regprocedure('public.lock_company_actor_role(uuid,uuid)') is not null as lock_role;

\echo == 9. 280020/280022/280023/280024 prerequisites ==
select to_regprocedure('public.claim_ai_job(text,integer)') is not null as claim_ai_job,
       to_regprocedure('public.mark_ai_job_provider_outbound_started(uuid,text)')
         is not null as outbound_fence,
       to_regprocedure('public.company_admin_audit_page(uuid,uuid,timestamptz,bigint,integer,text)')
         is not null as audit_page,
       to_regprocedure('public.compliance_run_retention(uuid,text,integer,boolean)')
         is not null as retention_fn,
       to_regprocedure('public.compliance_retention_worker_cycle(uuid,uuid,uuid,uuid,text,text,integer)')
         is not null as retention_worker,
       to_regclass('public.waitlist')                 is not null as waitlist,
       to_regclass('public.organization_invitations') is not null as invitations,
       to_regclass('public.legal_acceptances')        is not null as legal_acceptances,
       exists (select 1 from pg_namespace where nspname = 'audit_evidence_archive')
         as archive_schema;

\echo == 10. 280017 blast-radius baseline: how much warm evidence is ALREADY gone? ==
select count(*)      as warm_ledger_rows,
       min(day)      as oldest_day_retained,
       max(day)      as newest_day,
       (now() at time zone 'utc')::date - min(day) as days_of_history
  from public.warm_budget_ledger;

\echo == 11. 280005 backfill size: how many installations rows will be created? ==
select count(*) as installations_to_backfill
  from public.api_keys credential
 where credential.key_type = 'device'
   and credential.revoked_at is null
   and (credential.expires_at is null or credential.expires_at > now())
   and exists (
       select 1 from public.bvx_device_consumption_receipts receipt
        where receipt.key_hash = credential.key_hash
          and receipt.organization_id = credential.organization_id
          and receipt.quarantined_at is null)
   and exists (
       select 1 from public.audit_events activation
        where activation.organization_id = credential.organization_id
          and activation.action = 'device_key.activated'
          and activation.target_type = 'api_key'
          and activation.target_id = credential.id::text
          and activation.outcome = 'committed')
   and not exists (
       select 1 from public.installations existing
        where existing.organization_id = credential.organization_id
          and existing.registration_key_id = credential.id
          and existing.revoked_at is null);

\echo == 12. 280022 lock + backfill size ==
select (select count(*) from public.audit_events) as audit_events_rows,
       pg_size_pretty(pg_total_relation_size('public.audit_events')) as audit_events_size,
       (select count(*) from public.audit_events where action like '%.read')
         as existing_read_rows_must_be_0,
       (select count(*) from public.api_keys
         where key_type = 'device' and created_by is null) as api_keys_to_backfill;

\echo == 13. 280023 deletion exposure: what WILL retention remove once armed? ==
select (select count(*) from public.waitlist
         where created_at < clock_timestamp() - interval '24 months')
         as waitlist_rows_past_cutoff,
       (select count(*) from public.waitlist) as waitlist_rows_total;
SQL

supabase db query --linked -f /tmp/wyfz-apply/preflight.sql 2>&1 | tee -a "$WYFZ_LOG"
```

**Gate.** Do not apply anything unless:

- (1) `ok_pg14_plus = t`
- (2) all four `*_absent = t` — this is the "schema stops at `202607280004`" claim
- (3) `fee_trigger_absent = t` **and** `non_internal_usage_log_triggers = 0`
- (4) understood per [§1.1](#11-one-open-contradiction--resolve-before-6-window-b); `billing_ledger_max_id` **< 1000000000**
- (5)–(9) every column `t`, especially **(8) `generic_rpc_absent_REQUIRED = t`**
- (12) `existing_read_rows_must_be_0 = 0` — otherwise `280022`'s unique index will conflict with
  retained append-only audit evidence, which cannot be deleted to make room

Record (10), (11), (12) and (13) — they are the *before* side of postcondition comparisons later.

---

## 8. Per-migration steps

Format for each: **(a)** what it does · **(b)** precondition query · **(c)** apply command ·
**(d)** postcondition query · **(e)** blast radius if it fails mid-file · **(f)** safe alongside the
currently-deployed (pre-everything) app code?

A shared note on **(e)**, true for all twenty: each file is a single `begin;`…`commit;` with no
`CONCURRENTLY`, so — given [§4](#4-prove-the-applier-honours-transactions) passed — a failure
anywhere in the file leaves the database **byte-identical to before the file ran**. Partial DDL is
not possible. Where a file has additional blast radius beyond that, it is spelled out.

---

### WINDOW A — stop the bleeding

#### A1 · `202607280017_warm_evidence_retention_floor.sql`

**(a)** Replaces `public.purge_warm_state(integer)` so the `warm_budget_ledger` delete horizon is
floored at **365 days** regardless of the caller's argument, and adds a 30-day absolute cap on
`warm_prefixes.payload_ciphertext` rows. Signature, privileges and (except one added key) result
shape preserved.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280017.sql <<'SQL'
select to_regprocedure('public.purge_warm_state(integer)') is not null as required_fn,
       (select prosrc like '%365%' from pg_proc
         where oid = to_regprocedure('public.purge_warm_state(integer)'))
         as already_floored_expect_f,
       (select count(*) from public.warm_budget_ledger) as ledger_rows_before,
       (select min(day) from public.warm_budget_ledger) as oldest_day_before;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280017.sql
```
Require `required_fn = t`. `already_floored_expect_f = f` means it has not been applied yet.

**(c)**
```bash
wyfz_run supabase/migrations/202607280017_warm_evidence_retention_floor.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280017.sql <<'SQL'
select prosrc like '%greatest(coalesce(p_retention_days, 0), 365)%' as ledger_floor_installed,
       prosrc like '%v_prefix_absolute_days integer := 30%'          as prefix_cap_installed,
       prosecdef                                                     as still_definer,
       has_function_privilege('service_role','public.purge_warm_state(integer)','EXECUTE')
         as worker_can_call,
       not has_function_privilege('anon','public.purge_warm_state(integer)','EXECUTE')
         as anon_cannot_call
  from pg_proc where oid = to_regprocedure('public.purge_warm_state(integer)');
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280017.sql
```
All five must be `t`.

**(e)** Single transaction. Beyond that: none — this file writes no rows. Marked
`REVERSE: PITR-ONLY` **by policy**: restoring the old body is a trivial `CREATE OR REPLACE`, but
lowering the retention floor again would re-permit deletion of warm-spend billing evidence, so it is
forbidden rather than impossible.

**(f)** **Yes, and it is the single most urgent file in the pass.** It *fixes* the deployed worker
rather than depending on new code. The deployed `api/worker.py` keeps calling
`purge_warm_state(7)`; after this lands, that call still cleans up prefixes on a 7-day TTL but can
no longer touch the last 365 days of `warm_budget_ledger`. The `api/store.py` SQLite mirror and the
`BREVITAS_WARM_RETENTION_DAYS` default should follow in the code deploy; neither blocks.

---

#### A2 · `202607280018_warm_claim_lease_fence.sql`

**(a)** Fences `warm_prefix_observe`'s `next_due_at` reassignment on the warming `claim_token`, so a
live arrival inside the lease window no longer erases the lease; and clears `claim_token` when a
claim settles as `release`. Signatures unchanged.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280018.sql <<'SQL'
select to_regprocedure('public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric)')
         is not null as observe_rpc,
       to_regprocedure('public.warm_ping_settle(uuid,uuid,text,text,date,numeric,numeric,text,integer,integer,uuid)')
         is not null as settle_rpc;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280018.sql
```
Both must be `t`.

**(c)**
```bash
wyfz_run supabase/migrations/202607280018_warm_claim_lease_fence.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280018.sql <<'SQL'
select (select prosrc like '%claim_token%' from pg_proc
         where oid = to_regprocedure('public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric)'))
         as observe_reads_claim_token,
       has_function_privilege('service_role',
         'public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric)','EXECUTE')
         as service_role_can_call,
       not has_function_privilege('authenticated',
         'public.warm_ping_settle(uuid,uuid,text,text,date,numeric,numeric,text,integer,integer,uuid)','EXECUTE')
         as browser_cannot_call;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280018.sql
```

**(e)** Single transaction; no data writes. Marked `REVERSE: PITR-ONLY` by policy — removing the
lease fence reintroduces the double-claim race, and there is no evidence-preserving inverse.

**(f)** Yes. Fixes deployed behaviour; the deployed worker and `api/server.py` call both RPCs with
unchanged signatures. Worst case if it is wrong: warming efficiency changes. No money moves.

---

#### A3 · `202607280020_expired_job_reclaim_fence.sql`

**(a)** Widens `claim_ai_job`'s expiry sweep to cover lease-expired abandoned rows, so a job leased
before its `expires_at` can no longer be reclaimed and re-executed arbitrarily far past its
retention window. Otherwise transcribed verbatim from `202607200015`; signature, privileges and the
provider-outbound-ambiguity fence unchanged.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280020.sql <<'SQL'
select to_regprocedure('public.claim_ai_job(text,integer)') is not null as claim_fn,
       to_regprocedure('public.mark_ai_job_provider_outbound_started(uuid,text)')
         is not null as outbound_fence,
       count(*) filter (where status in ('leased','running')
                          and lease_expires_at <= now()
                          and expires_at <= now())
         as rows_this_will_terminalize,
       count(*) as ai_jobs_total
  from public.ai_jobs;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280020.sql
```
Note `rows_this_will_terminalize` — those are the abandoned, past-retention jobs the next
`claim_ai_job` call will mark expired. Confirm the number is plausible before proceeding.

**(c)**
```bash
wyfz_run supabase/migrations/202607280020_expired_job_reclaim_fence.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280020.sql <<'SQL'
select prosrc like '%provider_outcome_ambiguous%' as ambiguity_fence_intact,
       prosecdef as definer,
       has_function_privilege('service_role','public.claim_ai_job(text,integer)','EXECUTE')
         as worker_can_call,
       not has_function_privilege('anon','public.claim_ai_job(text,integer)','EXECUTE')
         as anon_cannot_call
  from pg_proc where oid = to_regprocedure('public.claim_ai_job(text,integer)');
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280020.sql
```

**(e)** Single transaction; no data writes at apply time. The *first call after apply* terminalizes
the backlog counted in (b) — that write is the intended behaviour, is bounded by `p_limit`, and is
not part of the migration transaction. Marked `REVERSE: PITR-ONLY`, which refers to that effect:
restoring `202607200015`'s `claim_ai_job` body is mechanically clean, but jobs already terminalized
by the widened sweep stay terminalized.

**(f)** Yes. Signature `(text,integer)` is unchanged, so deployed `api/jobs.py` and
`api/server.py` are unaffected. The SQLite/in-memory mirrors (`api/jobs.py:496`, `:259`) still have
the old asymmetry; that is a divergence between backends, not a break.

---

### WINDOW B — billing chain

> Gate: [§1.1](#11-one-open-contradiction--resolve-before-6-window-b) resolved, preflight item (4)
> understood, `billing_ledger_max_id < 1000000000`.

#### B1 · `202607280005_installation_on_device_activation.sql`

**(a)** Makes device-key activation register the server-side `public.installations` row inside the
same atomic consume transaction (so the `cli_connected` onboarding gate from `202607280004` can ever
be satisfied by the shipped BVX 0.1.27), **and backfills** the missing `devices` + `installations`
rows for device keys already activated.

**(b)** Preflight item (11) is the precondition — re-run it immediately before applying, and record
the number:
```bash
supabase db query --linked -f /tmp/wyfz-apply/preflight.sql 2>&1 | sed -n '/installations_to_backfill/,+3p'
```
Also confirm the shape the backfill depends on:
```bash
cat > /tmp/wyfz-apply/pre-280005.sql <<'SQL'
select to_regprocedure('public.consume_bvx_device_idempotent(text,text,text)') is not null as consume_rpc,
       to_regclass('public.installations') is not null as installations,
       to_regclass('public.devices')       is not null as devices,
       to_regclass('public.bvx_device_consumption_receipts') is not null as receipts,
       (select count(*) from public.installations) as installations_before,
       (select count(*) from public.devices)       as devices_before;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280005.sql
```

**(c)**
```bash
wyfz_run supabase/migrations/202607280005_installation_on_device_activation.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280005.sql <<'SQL'
select (select count(*) from public.installations) as installations_after,
       (select count(*) from public.devices)       as devices_after,
       (select count(*) from public.installations
         where client_name = 'bvx' and bvx_version = 'device-auth')
         as backfilled_rows,
       (select prosrc like '%insert into public.installations%' from pg_proc
         where oid = to_regprocedure('public.consume_bvx_device_idempotent(text,text,text)'))
         as consume_registers_installation,
       has_function_privilege('service_role',
         'public.consume_bvx_device_idempotent(text,text,text)','EXECUTE') as callable;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280005.sql
```
`installations_after - installations_before` must equal the `installations_to_backfill` you
recorded. `backfilled_rows` is the same number (the sentinel `bvx_version='device-auth'` marks
backfilled rows).

Then confirm the gate actually opened. Do **not** call
`public.organization_onboarding_status(p_actor_user_id, p_organization_id)` for this — it is
role-gated on the actor and would return a denial for a synthetic caller. Check the exact predicate
its `cli_connected` branch evaluates (`202607280004:56-80`) instead:
```bash
cat > /tmp/wyfz-apply/post-280005-gate.sql <<'SQL'
select count(distinct installation.organization_id) as orgs_now_cli_connected
  from public.installations installation
  join public.api_keys credential
    on credential.id = installation.registration_key_id
   and credential.key_hash = installation.registration_key_hash
   and credential.organization_id = installation.organization_id
   and credential.key_type = 'device'
   and credential.revoked_at is null
   and (credential.expires_at is null or credential.expires_at > now())
  join public.audit_events activation
    on activation.organization_id = installation.organization_id
   and activation.action = 'device_key.activated'
   and activation.target_type = 'api_key'
   and activation.target_id = credential.id::text
   and activation.outcome = 'committed'
 where installation.revoked_at is null
   and installation.device_auth_receipt_id is not null
   and lower(installation.client_name) = 'bvx'
   and installation.bvx_version <> ''
   and installation.device_id is not null;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280005-gate.sql
```
Expect `orgs_now_cli_connected > 0` if `installations_to_backfill` was > 0. (The live gate adds
`installation.installed_at >= v_started_at` per onboarding run; the backfill sets `installed_at` to
the real consume time precisely so prior activations still count, but an org whose onboarding run
started *after* its activation will still show closed — that is correct behaviour, not a backfill
failure.)

**(e)** Single transaction, so a mid-file failure undoes the backfill too. **Additional radius:** on
success it permanently creates real `devices` and `installations` rows. Reversing that means
deleting rows the onboarding gate now reads — recoverable in principle (the sentinel
`bvx_version='device-auth'` identifies exactly the rows created here), but you would be deleting
evidence, so prefer PITR.

**(f)** Yes, and it is the only file here that fixes a *user-visible* production outage: every new
workspace currently stalls forever at "connect the CLI". The RPC signature is byte-identical to
`202607170010`'s `(text,text,text)`, so nothing calling it changes.

---

#### B2 · `202607280006_retire_per_row_fee_trigger.sql`

**(a)** Drops `queue_brevitas_fee_after_usage` from `public.usage_log`, re-comments the retained
`queue_brevitas_fee()` function as RETIRED, and asserts the trigger is gone.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280006.sql <<'SQL'
select count(*) filter (where tgname = 'queue_brevitas_fee_after_usage')
         as fee_trigger_count_expect_0,
       count(*) as non_internal_triggers_expect_0
  from pg_trigger where tgrelid = 'public.usage_log'::regclass and not tgisinternal;
select to_regprocedure('public.queue_brevitas_fee()') is not null as fn_retained_expect_t,
       (select count(*) from public.billing_ledger) as ledger_rows_expect_0;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280006.sql
```

**(c)**
```bash
wyfz_run supabase/migrations/202607280006_retire_per_row_fee_trigger.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280006.sql <<'SQL'
select not exists (
         select 1 from pg_trigger
          where tgrelid = 'public.usage_log'::regclass
            and tgname = 'queue_brevitas_fee_after_usage'
            and not tgisinternal) as trigger_absent,
       to_regprocedure('public.queue_brevitas_fee()') is not null as fn_still_retained,
       obj_description(to_regprocedure('public.queue_brevitas_fee()'), 'pg_proc')
         like 'RETIRED 202607280006%' as comment_updated;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280006.sql
```
All three `t`.

**(e)** Single transaction. On wyfz the `drop trigger if exists` is a **no-op** (§1 consequence 2) —
the only durable effect is the function comment. Effectively zero blast radius.

**(f)** Yes. Nothing changes behaviourally, because the trigger was already absent. **This is the
file that makes L2 permanent:** after it, `202607170012` will raise if replayed. That is the desired
end state, not a problem.

---

#### B3 · `202607280007_period_settlement_ledger.sql`

**(a)** Creates `public.period_settlement_ledger` (immutable, append-a-revision corrections), 7
indexes, the fee helper `period_settlement_fee_microusd(numeric,numeric)`, and two guard triggers
(`prevent_period_settlement_delete`, `prevent_period_settlement_identity_change`). **Structure
only** — no writer, no trigger on any other table, no row written.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280007.sql <<'SQL'
select to_regclass('public.period_settlement_ledger') is null as table_absent_expect_t,
       to_regclass('public.organizations') is not null        as organizations,
       to_regclass('public.billing_ledger') is not null       as billing_ledger,
       (select count(*) from public.billing_ledger)           as ledger_rows,
       (select coalesce(max(id),0) from public.billing_ledger) as ledger_max_id_must_be_lt_1e9,
       not exists (select 1 from pg_trigger
                    where tgrelid='public.usage_log'::regclass
                      and tgname='queue_brevitas_fee_after_usage' and not tgisinternal)
         as freeze_held;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280007.sql
```
`ledger_max_id_must_be_lt_1e9` must be `< 1000000000`, or the file's own assertion aborts it (see
§1.1).

**(c)**
```bash
wyfz_run supabase/migrations/202607280007_period_settlement_ledger.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280007.sql <<'SQL'
select to_regclass('public.period_settlement_ledger') is not null as table_created,
       (select relrowsecurity from pg_class
         where oid='public.period_settlement_ledger'::regclass) as rls_enabled,
       (select count(*) from public.period_settlement_ledger)    as rows_must_be_0,
       (select count(*) from pg_trigger
         where tgrelid='public.period_settlement_ledger'::regclass
           and not tgisinternal)                                 as triggers_must_be_2,
       (select count(*) from pg_index
         where indrelid='public.period_settlement_ledger'::regclass) as index_count,
       to_regprocedure('public.period_settlement_fee_microusd(numeric,numeric)')
         is not null as fee_helper,
       public.period_settlement_fee_microusd(100,100) = 25000000 as fee_25pct_of_net,
       public.period_settlement_fee_microusd(100,40)  = 10000000 as fee_capped_at_verified,
       public.period_settlement_fee_microusd(-5,-5)   = 0        as fee_never_negative;
\echo -- no browser role may reach the new table --
select has_table_privilege('anon','public.period_settlement_ledger','SELECT') as anon_select_expect_f,
       has_table_privilege('authenticated','public.period_settlement_ledger','SELECT') as auth_select_expect_f,
       has_table_privilege('service_role','public.period_settlement_ledger','INSERT') as sr_insert_expect_f;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280007.sql
```
`rows_must_be_0 = 0`, `triggers_must_be_2 = 2`, all three privilege columns `f`.

**(e)** Single transaction. A new empty table + indexes + triggers; nothing else in the schema is
touched, so a failure is invisible. **Note the one non-transactional residue** across the whole
pass: the table's identity sequence is advanced by the `280010` and `280013` probes even though
their rows roll back. That is documented in `280010` as deliberate — `280007` asserts the sequence's
declared `START`, not its current position, and nothing reads the position.

**(f)** Yes, trivially — no deployed code knows this table exists.

---

#### B4 · `202607280008_billing_halting_conditions.sql`

**(a)** Creates `public.billing_halting_conditions` (one singleton row: `max_fee_share_of_verified_savings`,
`max_zero_spend_savings_share`), `billing_period_settlement_evidence(uuid,timestamptz,timestamptz)`,
and `assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)` — three circuit
breakers that must pass before any fee can be committed.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280008.sql <<'SQL'
select to_regclass('public.period_settlement_ledger') is not null as needs_280007,
       to_regclass('public.warm_budget_ledger')       is not null as needs_280001,
       to_regclass('public.billing_halting_conditions') is null   as not_yet_applied,
       not exists (select 1 from pg_trigger
                    where tgrelid='public.usage_log'::regclass
                      and tgname='queue_brevitas_fee_after_usage' and not tgisinternal)
         as freeze_held_REQUIRED;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280008.sql
```
All four `t`. If `needs_280001 = f` the file aborts loudly on purpose: without
`warm_budget_ledger` the warm deduction would silently be zero, which **overcharges**.

**(c)**
```bash
wyfz_run supabase/migrations/202607280008_billing_halting_conditions.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280008.sql <<'SQL'
select count(*) as singleton_rows_must_be_1,
       max(max_fee_share_of_verified_savings) as fee_share,
       max(max_zero_spend_savings_share)      as zero_spend_share
  from public.billing_halting_conditions;
select to_regprocedure('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)')
         is not null as evidence_fn,
       to_regprocedure('public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)')
         is not null as guard_fn,
       has_table_privilege('service_role','public.billing_halting_conditions','SELECT')
         as sr_reads_thresholds,
       not has_table_privilege('service_role','public.billing_halting_conditions','UPDATE')
         as sr_cannot_relax_thresholds,
       not has_table_privilege('anon','public.billing_halting_conditions','SELECT')
         as anon_blind;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280008.sql
```
`singleton_rows_must_be_1 = 1`; all boolean columns `t`.

**(e)** Single transaction. Writes exactly one row (the singleton). The file's own contract block
deliberately attempts to violate the thresholds and the singleton constraint and requires those
attempts to raise — all inside the transaction, all rolled back.

**(f)** Yes. Nothing deployed calls these. They are inert until `280013`'s writer exists.

---

#### B5 · `202607280009_billing_arrangement_attestation.sql`

**(a)** Creates `public.organization_billing_arrangement` (per-org attestation: `unknown` /
`marginal_per_call` / `enterprise_handshake`), `organization_billing_arrangement_state(uuid)`, and
`assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)` — then **revokes
`EXECUTE` on the inner `assert_billing_period_halting_conditions` from `service_role`**, so the
wrapper becomes the only settlement door and the attestation cannot be skipped.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280009.sql <<'SQL'
select to_regclass('public.billing_halting_conditions') is not null as needs_280008,
       to_regprocedure('public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)')
         is not null as needs_inner_guard,
       to_regclass('public.organization_billing_arrangement') is null as not_yet_applied,
       not exists (select 1 from pg_trigger
                    where tgrelid='public.usage_log'::regclass
                      and tgname='queue_brevitas_fee_after_usage' and not tgisinternal)
         as freeze_held_REQUIRED;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280009.sql
```

**(c)**
```bash
wyfz_run supabase/migrations/202607280009_billing_arrangement_attestation.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280009.sql <<'SQL'
select to_regclass('public.organization_billing_arrangement') is not null as table_created,
       (select count(*) from public.organization_billing_arrangement)
         as rows_MUST_be_0_absence_is_unbillable,
       to_regprocedure('public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)')
         is not null as wrapper_fn,
       has_function_privilege('service_role',
         'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)','EXECUTE')
         as wrapper_callable,
       not has_function_privilege('service_role',
         'public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)','EXECUTE')
         as inner_guard_SEALED_REQUIRED,
       not has_table_privilege('anon','public.organization_billing_arrangement','SELECT') as anon_blind;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280009.sql
```
`rows_MUST_be_0…` must be **0** — absence of a row *is* the unbillable state; do not backfill this
table for anyone. `inner_guard_SEALED_REQUIRED` must be `t`.

**(e)** Single transaction. The contract block creates a probe `auth.users` + `organizations` +
arrangement row, exercises all three arrangement values, then `delete`s them explicitly; the
enclosing transaction is the backstop.

**(f)** Yes. The `service_role` revoke removes `EXECUTE` on a function no deployed code calls
(verified: `assert_billing_period_halting_conditions` appears only in `scripts/ci/**` and never in
`api/`, `src/`, `dashboard/` at `HEAD`).

---

#### B6 · `202607280010_period_settlement_send_latches.sql`

**(a)** Replaces `prevent_period_settlement_identity_change()` so `outbound_started_at`,
`reported_at` and `settled_at` become **latches** — once set they can never be cleared, so a
reported period cannot be voided and re-billed at the full rate.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280010.sql <<'SQL'
select to_regclass('public.period_settlement_ledger') is not null as needs_280007,
       to_regprocedure('public.prevent_period_settlement_identity_change()') is not null as guard_fn,
       exists (select 1 from pg_trigger
                where tgrelid='public.period_settlement_ledger'::regclass
                  and tgname='prevent_period_settlement_identity_change'
                  and not tgisinternal) as guard_ATTACHED_REQUIRED,
       (select count(*) from pg_trigger
         where tgrelid='public.period_settlement_ledger'::regclass and not tgisinternal)
         as trigger_count_expect_2,
       (select count(*) from public.period_settlement_ledger) as settlements_expect_0;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280010.sql
```

**(c)**
```bash
wyfz_run supabase/migrations/202607280010_period_settlement_send_latches.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280010.sql <<'SQL'
select prosrc like '%old.outbound_started_at is not null%' as latch_outbound,
       prosrc like '%old.reported_at is not null%'          as latch_reported,
       prosrc like '%old.settled_at is not null%'           as latch_settled,
       prosrc like '%new.outbound_started_at is null%'      as rejects_clearing
  from pg_proc where oid = to_regprocedure('public.prevent_period_settlement_identity_change()');
select (select count(*) from pg_trigger
         where tgrelid='public.period_settlement_ledger'::regclass and not tgisinternal)
         as trigger_count_still_2,
       (select count(*) from public.period_settlement_ledger)
         as settlements_still_0_probe_unwound;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280010.sql
```
All four latch columns `t`; `trigger_count_still_2 = 2`; `settlements_still_0_probe_unwound = 0`.

**(e)** Single transaction. The probe writes three `period_settlement_ledger` rows and an
`auth.users` + `organizations` fixture, then unwinds them by raising `ZZ010` — it cannot `DELETE`
them, because the table carries an unconditional `BEFORE DELETE` guard. It then asserts none
survived. The one residue is the advanced identity sequence (documented, harmless).

**(f)** Yes. `280013` refuses to install unless these latches exist, which is the whole reason this
file precedes it.

---

#### B7 · `202607280011_billing_ledger_unsent_release.sql`

**(a)** Adds `public.release_billing_ledger_unsent(bigint,text)` — returns one attempt when Stripe
rate-limits a send it provably did not ingest, fenced on `id`, `lease_owner` and `status='sending'`.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280011.sql <<'SQL'
select to_regclass('public.billing_ledger') is not null as billing_ledger,
       to_regprocedure('public.release_billing_ledger_unsent(bigint,text)') is null
         as not_yet_applied,
       not has_table_privilege('service_role','public.billing_ledger','UPDATE')
         as sr_has_no_direct_write_REQUIRED,
       (select count(*) from public.billing_ledger where status = 'sending')
         as rows_currently_sending;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280011.sql
```

**(c)**
```bash
wyfz_run supabase/migrations/202607280011_billing_ledger_unsent_release.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280011.sql <<'SQL'
select prosecdef as definer_owns_the_write,
       prosrc like '%and lease_owner = p_owner%' as lease_fenced,
       prosrc like '%and status = ''sending''%'  as status_fenced,
       prosrc like '%outbound_started_at = null%' as clears_marker,
       prosrc like '%greatest(0, attempts - 1)%'  as returns_one_attempt
  from pg_proc where oid = to_regprocedure('public.release_billing_ledger_unsent(bigint,text)');
select has_function_privilege('service_role','public.release_billing_ledger_unsent(bigint,text)','EXECUTE')
         as worker_can_call,
       not has_function_privilege('anon','public.release_billing_ledger_unsent(bigint,text)','EXECUTE')
         as anon_cannot,
       not has_function_privilege('authenticated','public.release_billing_ledger_unsent(bigint,text)','EXECUTE')
         as authenticated_cannot;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280011.sql
```

**(e)** Single transaction; no data writes. The contract block only reads `pg_proc` and privileges.

**(f)** Yes, and it **must** land before the new `api/billing_recovery.py` deploys — that revision
calls this RPC on the `StripeUnavailable` branch and would fail with `42883` otherwise. The deployed
revision does not call it (verified: `release_billing_ledger_unsent` appears nowhere in
`HEAD:api/`), so DB-first is strictly safe. With `billing_ledger` empty there is nothing for it to
act on either way.

---

#### B8 · `202607280012_settlement_evidence_warm_days.sql`

**(a)** Fixes `warm_spend_days` in `billing_period_settlement_evidence` to
`count(distinct warm.day)` instead of `count(*)` — `warm_budget_ledger`'s PK is
`(organization_id, provider, day)`, so two providers on one day previously reported 2 days. The
money terms (`warm_spend_usd`, `net_verified_savings_usd`) are deliberately left byte-identical.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280012.sql <<'SQL'
select to_regclass('public.warm_budget_ledger') is not null as needs_280001,
       to_regprocedure('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)')
         is not null as needs_280008,
       not exists (select 1 from pg_trigger
                    where tgrelid='public.usage_log'::regclass
                      and tgname='queue_brevitas_fee_after_usage' and not tgisinternal)
         as freeze_held_REQUIRED;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280012.sql
```

**(c)**
```bash
wyfz_run supabase/migrations/202607280012_settlement_evidence_warm_days.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280012.sql <<'SQL'
select prosrc like '%count(distinct%' as counts_distinct_days,
       has_function_privilege('service_role',
         'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)','EXECUTE')
         as callable,
       not has_function_privilege('anon',
         'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)','EXECUTE')
         as anon_cannot
  from pg_proc where oid = to_regprocedure('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)');
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280012.sql
```

**(e)** Single transaction. The contract block inserts and then explicitly deletes probe
`warm_budget_ledger` + `organizations` rows.

**(f)** Yes. Diagnostics-only change to a function no deployed code calls.

---

#### B9 · `202607280013_period_settlement_writer.sql`

**(a)** The settlement writer. Widens `billing_period_settlement_evidence` by one OUT column
(`usage_log_watermark_id`) and adds four functions:
`settle_billing_period(uuid,timestamptz,text,boolean)` (writes **`draft` only**, granted to
`service_role`), `promote_billing_period_settlement(bigint,text,text)` (`draft → pending`, the only
money-moving door, granted to **nobody**), `billing_period_settlement_summary(uuid,timestamptz)`,
and `billing_periods_awaiting_settlement(integer)`. No trigger, no table privilege.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280013.sql <<'SQL'
select to_regclass('public.period_settlement_ledger')           is not null as needs_280007,
       to_regclass('public.billing_halting_conditions')         is not null as needs_280008,
       to_regprocedure('public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)')
         is not null as needs_280009,
       to_regprocedure('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)')
         is not null as needs_evidence_fn,
       to_regprocedure('public.period_settlement_fee_microusd(numeric,numeric)')
         is not null as needs_fee_helper,
       to_regprocedure('public.billing_period_for_occurrence(timestamptz,timestamptz,timestamptz)')
         is not null as needs_170004_period_fn,
       (select prosrc like '%old.settled_at is not null%' from pg_proc
         where oid = to_regprocedure('public.prevent_period_settlement_identity_change()'))
         as needs_280010_latches,
       not exists (select 1 from pg_trigger
                    where tgrelid='public.usage_log'::regclass
                      and tgname='queue_brevitas_fee_after_usage' and not tgisinternal)
         as freeze_held_REQUIRED;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280013.sql
```
All eight `t`.

**(c)**
```bash
wyfz_run supabase/migrations/202607280013_period_settlement_writer.sql
```

**(d)** The privilege posture is the whole safety argument — check it explicitly:
```bash
cat > /tmp/wyfz-apply/post-280013.sql <<'SQL'
\echo == the four functions exist ==
select to_regprocedure('public.settle_billing_period(uuid,timestamptz,text,boolean)') is not null as writer,
       to_regprocedure('public.promote_billing_period_settlement(bigint,text,text)')  is not null as promoter,
       to_regprocedure('public.billing_period_settlement_summary(uuid,timestamptz)')  is not null as summary,
       to_regprocedure('public.billing_periods_awaiting_settlement(integer)')         is not null as enumerator;

\echo == RUNTIME MAY COMPUTE, ONLY A HUMAN MAY BILL ==
select has_function_privilege('service_role',
         'public.settle_billing_period(uuid,timestamptz,text,boolean)','EXECUTE')
         as sr_may_draft_expect_t,
       not has_function_privilege('service_role',
         'public.promote_billing_period_settlement(bigint,text,text)','EXECUTE')
         as sr_may_NOT_promote_REQUIRED,
       not has_function_privilege('anon',
         'public.promote_billing_period_settlement(bigint,text,text)','EXECUTE')
         as anon_may_NOT_promote,
       not has_function_privilege('authenticated',
         'public.promote_billing_period_settlement(bigint,text,text)','EXECUTE')
         as auth_may_NOT_promote,
       not has_function_privilege('public',
         'public.promote_billing_period_settlement(bigint,text,text)','EXECUTE')
         as public_may_NOT_promote;

\echo == evidence function widened, probe unwound, ledger still empty ==
select (select 'usage_log_watermark_id' = any(proargnames) from pg_proc
         where oid = to_regprocedure('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)'))
         as watermark_is_an_OUT_column,
       (select prosrc like '%usage_log_watermark_id%' from pg_proc
         where oid = to_regprocedure('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)'))
         as watermark_added,
       (select count(*) from public.period_settlement_ledger) as settlements_must_be_0,
       (select count(*) from public.organizations
         where name like '202607280013%') as probe_orgs_must_be_0,
       (select count(*) from auth.users
         where email like '202607280013%') as probe_users_must_be_0;

\echo == nothing is billable: no arrangement attested, no draft, no pending ==
select (select count(*) from public.organization_billing_arrangement) as arrangements_expect_0,
       (select count(*) from public.period_settlement_ledger where status='draft')   as drafts_expect_0,
       (select count(*) from public.period_settlement_ledger where status='pending') as pending_expect_0;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280013.sql
```
`sr_may_NOT_promote_REQUIRED` and all three other `*may_NOT_promote` columns **must** be `t`. All
the `*_expect_0` / `*_must_be_0` columns must be `0`.

**(e)** Single transaction. **Additional radius, and the sharpest in the pass:** the file begins with
`drop function if exists public.billing_period_settlement_evidence(…)` before recreating it wider.
If the transaction aborts *after* that drop, the rollback restores the previous definition — so the
window is invisible to anything outside the transaction. But if [§4](#4-prove-the-applier-honours-transactions)
did **not** pass, this is the one file that can leave the schema with a *missing* evidence function
rather than merely an unchanged one. Do not run it if §4 is unresolved. The probe fixture is the
largest in the pass (`auth.users`, `organizations`, `billing_accounts`, `usage_log`,
`warm_budget_ledger`, `period_settlement_ledger`) and unwinds via `ZZ013`, asserting afterwards that
no settlement row and no organization survived.

**(f)** Yes, and DB **must** go first. The new `src/app/api/billing/status/route.ts` calls
`billing_period_settlement_summary`; the deployed route does not (verified against `HEAD`), so
landing this before the code deploy is safe in both directions. Nothing here can bill: no
organization has an attested arrangement (postcondition `arrangements_expect_0 = 0`), traffic is
100% non-authoritative and unpriced, and promotion is granted to nobody.

---

#### B10 · `202607280014_drop_billing_monthly_view.sql`

**(a)** Drops `public.billing_monthly`, a definer view over RLS-protected `public.billing_events`
that lacks `security_invoker` and was never revoked from `anon`/`authenticated` — i.e. per-tenant
call volume, savings and Brevitas revenue readable with the public anon key baked into the
dashboard bundle.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280014.sql <<'SQL'
select to_regclass('public.billing_monthly') is not null as view_present,
       (select count(*) from pg_depend d
         join pg_rewrite r on r.oid = d.objid
          and r.ev_class <> 'public.billing_monthly'::regclass
        where d.refobjid = 'public.billing_monthly'::regclass
          and d.refclassid = 'pg_class'::regclass
          and d.deptype = 'n') as OTHER_objects_depending_on_it,
       has_table_privilege('anon','public.billing_monthly','SELECT') as anon_can_read_today,
       (select count(*) from public.billing_events) as billing_events_rows;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280014.sql
```
If `view_present = f`, skip — the file is idempotent (`drop view if exists`) and would be a no-op,
but there is nothing to verify. `anon_can_read_today = t` confirms the exposure this closes is real
on wyfz. `OTHER_objects_depending_on_it` should be 0; if not, something else depends on the view and the
`drop` (without `cascade`) will fail — investigate before applying.

**(c)**
```bash
wyfz_run supabase/migrations/202607280014_drop_billing_monthly_view.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280014.sql <<'SQL'
select to_regclass('public.billing_monthly') is null as view_dropped,
       (select count(*) from public.billing_events) as billing_events_rows_UNCHANGED;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280014.sql
```
`view_dropped = t`, and `billing_events_rows_UNCHANGED` must equal the number from (b) — dropping a
view never touches base-table rows, and this confirms it.

**(e)** Single transaction. The view holds no data. **But this is `REVERSE: PITR-ONLY` by policy,
not by mechanism:** recreating it is a two-line `CREATE VIEW`, and the file forbids that because
recreating the definer view reopens the cross-tenant anon billing leak.

**(f)** Yes. Repo-wide grep finds no caller outside the two migrations that create it: the
compliance exporters read `public.billing_events` directly (`202607170007:1521,1885`), and
`scripts/db/migrate-project-data.sh` copies tables only.

---

### WINDOW C — privilege / compliance / auth

> **Low-traffic window only.** Keep the [§6.1](#61-lock-hazard-and-the-honest-limitation) lock query
> running in a second terminal for every file in this window.

#### C1 · `202607280015_browser_role_privilege_contract.sql`

**(a)** Revokes Supabase's project-default `GRANT ALL` (TRUNCATE included) from `anon` and
`authenticated` on the fifteen tables named by the `202607220001` `service_role` contract, drops the
unnarrowed `"Users can update own profile"` policy (which let any user rewrite `profiles.email` —
the only account name the operator console has), revokes `public.usage_log_id_seq`, and installs
`public.assert_browser_role_table_privileges()` as a re-runnable executable contract.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280015.sql <<'SQL'
\echo == all fifteen tables must exist ==
select t as table_name, to_regclass('public.'||t) is not null as present
  from unnest(array['organizations','organization_members','customers','service_accounts',
                    'bvx_device_auth','api_keys','installations','devices','key_repositories',
                    'profiles','usage_log','provider_config','ai_jobs','billing_accounts',
                    'billing_ledger']) t;
\echo == the exposure being closed, before ==
select has_table_privilege('anon','public.usage_log','TRUNCATE')      as anon_truncate_usage_log,
       has_table_privilege('authenticated','public.api_keys','SELECT') as auth_select_api_keys,
       exists (select 1 from pg_policy
                where polrelid='public.profiles'::regclass and polcmd in ('w','*'))
         as profiles_update_policy_present;
\echo == service_role contract must be untouched by this file -- record it ==
select has_table_privilege('service_role','public.usage_log','INSERT') as sr_usage_insert,
       has_table_privilege('service_role','public.usage_log','SELECT') as sr_usage_select,
       has_table_privilege('service_role','public.profiles','SELECT')  as sr_profiles_select;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280015.sql
```

**(c)**
```bash
wyfz_run supabase/migrations/202607280015_browser_role_privilege_contract.sql
```

**(d)** The file ends with `select public.assert_browser_role_table_privileges();` inside its own
transaction, so a successful apply already proves the contract. Confirm independently, and confirm
`service_role` was **not** collateral damage:
```bash
cat > /tmp/wyfz-apply/post-280015.sql <<'SQL'
\echo == the contract itself: this RAISES on violation, and returns an empty result otherwise ==
select public.assert_browser_role_table_privileges();
\echo == and the individual revokes ==
select not has_table_privilege('anon','public.usage_log','TRUNCATE')       as anon_truncate_gone,
       not has_table_privilege('authenticated','public.api_keys','SELECT')  as auth_api_keys_gone,
       has_table_privilege('authenticated','public.profiles','SELECT')      as profiles_select_KEPT,
       not has_table_privilege('authenticated','public.profiles','UPDATE')  as profiles_update_gone,
       not exists (select 1 from pg_policy
                    where polrelid='public.profiles'::regclass and polcmd in ('w','*'))
         as profiles_update_policy_gone;
\echo == service_role must be IDENTICAL to the pre-check ==
select has_table_privilege('service_role','public.usage_log','INSERT') as sr_usage_insert,
       has_table_privilege('service_role','public.usage_log','SELECT') as sr_usage_select,
       has_table_privilege('service_role','public.profiles','SELECT')  as sr_profiles_select;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280015.sql
```
Then, from a real client, confirm the API still ingests usage (see
[§9.3](#93-window-c-gate)).

**(e)** Single transaction, so a failure leaves privileges exactly as they were. **Additional
radius:** `ACCESS EXCLUSIVE` on fourteen tables, one of which (`usage_log`) is under continuous
insert load — see [§6.1](#61-lock-hazard-and-the-honest-limitation). The privilege change itself is
`REVERSE: PITR-ONLY` by policy: re-granting the browser-role defaults reopens the exposure.

**(f)** Yes — verified, not assumed. The dashboard bundle's entire Supabase surface is
`supabase.auth.getSession / onAuthStateChange / signOut / signUp / signInWithPassword /
resetPasswordForEmail / updateUser`. There are **zero** `.from()` and **zero** `.rpc()` calls
anywhere in `dashboard/src/`, so no deployed browser code reads any of these fifteen tables with the
anon key. `supabase.auth.updateUser` writes `auth.users`, not `public.profiles`. `service_role` is
not named anywhere in this file.

---

#### C2 · `202607280016_compliance_warm_state_erasure.sql`

**(a)** Redefines the `202607200011` outer wrappers `compliance_delete_tenant`,
`compliance_export_tenant` and `compliance_export_subject` (transcribed verbatim, plus warming
statements) so tenant erasure also deletes `warm_credentials` / `warm_prefixes` /
`warm_budget_ledger`, and portability exports all three. Closes the gap where a terminated
customer's KMS-encrypted provider key ciphertext, consenting-user UUID and consent timestamp
survived a deletion reported as `completed`.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280016.sql <<'SQL'
select to_regprocedure('public.compliance_delete_tenant_pre_company_identity(uuid,uuid,text)')
         is not null as inner_delete,
       to_regprocedure('public.compliance_export_tenant_pre_company_identity(uuid,uuid,text)')
         is not null as inner_export_tenant,
       to_regprocedure('public.compliance_export_subject_pre_company_identity(uuid,uuid,text)')
         is not null as inner_export_subject,
       to_regclass('public.warm_credentials')  is not null as warm_credentials,
       to_regclass('public.warm_prefixes')     is not null as warm_prefixes,
       to_regclass('public.warm_budget_ledger') is not null as warm_budget_ledger,
       (select prosrc like '%warm_credentials%' from pg_proc
         where oid = to_regprocedure('public.compliance_delete_tenant(uuid,uuid,text)'))
         as already_applied_expect_f;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280016.sql
```

**(c)**
```bash
wyfz_run supabase/migrations/202607280016_compliance_warm_state_erasure.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280016.sql <<'SQL'
select (select prosrc like '%delete from public.warm_credentials%' from pg_proc
         where oid = to_regprocedure('public.compliance_delete_tenant(uuid,uuid,text)'))
         as erasure_covers_warm_credentials,
       (select prosrc like '%warm_prefixes%' from pg_proc
         where oid = to_regprocedure('public.compliance_delete_tenant(uuid,uuid,text)'))
         as erasure_covers_warm_prefixes,
       (select prosrc like '%warm_budget_ledger%' from pg_proc
         where oid = to_regprocedure('public.compliance_export_tenant(uuid,uuid,text)'))
         as export_covers_warm_ledger,
       has_function_privilege('service_role','public.compliance_delete_tenant(uuid,uuid,text)','EXECUTE')
         as callable,
       not has_function_privilege('anon','public.compliance_delete_tenant(uuid,uuid,text)','EXECUTE')
         as anon_cannot;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280016.sql
```
All five `t`.

**(e)** Single transaction; no data written or deleted **at apply time**. The widened deletion only
runs when a human invokes `scripts/dr/tenant-data.sh`. `create or replace` preserves the argument
lists and `202607200011`'s grants, so the company-identity isolation those wrappers provide is
unchanged. `REVERSE: PITR-ONLY` — once a wider erasure has run, restoring the narrow wrappers
cannot bring the deleted warm-state evidence back.

**(f)** Yes. Signatures unchanged; nothing deployed changes behaviour until an erasure or export is
actually requested.

---

#### C3 · `202607280019_tenant_device_key_revocation.sql`

**(a)** Adds ONE security-definer dispatcher `public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)`
that locks the target row and branches on `key_type`: `dashboard_session` keeps today's semantics
exactly (and its pinned `dashboard_session.*` audit actions), `device` requires
`company_owner`/`company_admin` and emits distinct actions. Fixes the case where a lost laptop's
device credential could not be revoked by the customer at all.

**(b)** ⚠️ This file **refuses to run** if the retired generic RPC exists.
```bash
cat > /tmp/wyfz-apply/pre-280019.sql <<'SQL'
select to_regprocedure('public.company_admin_revoke_dashboard_session_key(uuid,uuid,uuid,text)')
         is not null as session_rpc_present_REQUIRED,
       to_regprocedure('public.company_admin_revoke_key(uuid,uuid,uuid,text)')
         is null      as generic_rpc_ABSENT_REQUIRED,
       to_regprocedure('public.lock_company_admin_namespace(uuid)') is not null as lock_ns,
       to_regprocedure('public.lock_company_actor_role(uuid,uuid)') is not null as lock_role,
       to_regprocedure('public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)')
         is null      as not_yet_applied;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280019.sql
```
All five `t`. `202607170009:174` drops the generic RPC, so wyfz should be clear; if
`generic_rpc_ABSENT_REQUIRED = f`, **stop** — the two revocation paths must not coexist, and
resolving that is a design decision, not a runbook step.

**(c)**
```bash
wyfz_run supabase/migrations/202607280019_tenant_device_key_revocation.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280019.sql <<'SQL'
select to_regprocedure('public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)')
         is not null as dispatcher_created,
       to_regprocedure('public.company_admin_revoke_dashboard_session_key(uuid,uuid,uuid,text)')
         is not null as legacy_session_rpc_RETAINED,
       (select prosecdef from pg_proc
         where oid = to_regprocedure('public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)'))
         as definer,
       has_function_privilege('service_role',
         'public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)','EXECUTE') as api_can_call,
       not has_function_privilege('anon',
         'public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)','EXECUTE') as anon_cannot,
       not has_function_privilege('authenticated',
         'public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)','EXECUTE') as auth_cannot;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280019.sql
```

**(e)** Single transaction; purely additive (one new function). No existing RPC is dropped or
replaced, so the deployed revocation path is bit-for-bit unchanged. `REVERSE: PITR-ONLY` applies to
*keys already revoked through the new path*, not to the function — dropping the function is a clean
`DROP FUNCTION`.

**(f)** Yes. `company_admin_revoke_dashboard_session_key` is retained, so deployed
`api/store.py` / `api/company_admin.py` keep working unchanged. The customer-facing device
revocation only becomes reachable once new API code routes `DELETE /v1/keys/{key_id}` to the
dispatcher — until then this is dormant capability.

---

#### C4 · `202607280021_server_authoritative_legal_acceptance.sql`

⚠️ **This replaces the `AFTER INSERT` trigger on `auth.users`. It fires on the next real signup, the
moment it commits.**

**(a)** Adds `legal_acceptances.accepted boolean not null default true`; pins the published document
versions server-side in `public.current_legal_versions()`; rewrites
`public.record_legal_acceptance()` to write those server versions and **always** insert a row, so a
signup that asserted nothing is recorded as presented-and-not-accepted instead of vanishing; and
re-creates the trigger `on_auth_user_legal_acceptance`.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280021.sql <<'SQL'
\echo == PG14+ required for CREATE OR REPLACE TRIGGER ==
select current_setting('server_version_num')::int >= 140000 as ok_pg14_plus;
\echo == objects the file requires ==
select to_regclass('public.legal_acceptances') is not null as table_present,
       to_regprocedure('public.record_legal_acceptance()') is not null as trigger_fn_present,
       exists (select 1 from pg_trigger
                where tgrelid='auth.users'::regclass
                  and tgname='on_auth_user_legal_acceptance' and not tgisinternal)
         as trigger_attached;
\echo == ON CONFLICT (user_id) needs a unique/PK on user_id -- MUST be t ==
select exists (
    select 1 from pg_constraint
     where conrelid = 'public.legal_acceptances'::regclass
       and contype in ('p','u')
       and conkey = array[(select attnum from pg_attribute
                            where attrelid='public.legal_acceptances'::regclass
                              and attname='user_id')]
  ) as on_conflict_target_exists_REQUIRED;
\echo == every other AFTER INSERT trigger on auth.users -- know what else fires ==
select tgname, pg_get_triggerdef(oid) as definition
  from pg_trigger where tgrelid='auth.users'::regclass and not tgisinternal order by tgname;
\echo == baseline ==
select count(*) as legal_acceptances_before,
       count(distinct terms_version) as distinct_terms_versions,
       min(terms_version) as min_terms_version,
       max(terms_version) as max_terms_version
  from public.legal_acceptances;
select (select count(*) from auth.users) as users_total,
       (select count(*) from auth.users u
         where not exists (select 1 from public.legal_acceptances la where la.user_id = u.id))
         as users_with_NO_acceptance_row_today;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280021.sql
```
`ok_pg14_plus` and `on_conflict_target_exists_REQUIRED` must both be `t`. (Verified in-repo:
`20260714_legal_acceptances.sql:2` declares `user_id uuid primary key`, so this should pass.)
`users_with_NO_acceptance_row_today` quantifies the "no row is indistinguishable from never asked"
defect on wyfz.

**(c)**
```bash
wyfz_run supabase/migrations/202607280021_server_authoritative_legal_acceptance.sql
```

**(d)** Structural check, then a **live signup test** — this is the one file where structure alone is
not enough:
```bash
cat > /tmp/wyfz-apply/post-280021.sql <<'SQL'
select to_regprocedure('public.current_legal_versions()') is not null as versions_fn,
       public.current_legal_versions()->>'terms_version'   as pinned_terms_version,
       public.current_legal_versions()->>'privacy_version' as pinned_privacy_version;
select exists (select 1 from information_schema.columns
                where table_schema='public' and table_name='legal_acceptances'
                  and column_name='accepted' and is_nullable='NO') as accepted_column_added,
       (select count(*) from public.legal_acceptances where accepted is null) as nulls_must_be_0,
       (select count(*) from public.legal_acceptances) as rows_unchanged_from_pre_check;
select (select prosrc like '%public.current_legal_versions()%' from pg_proc
         where oid = to_regprocedure('public.record_legal_acceptance()'))
         as reads_server_versions,
       (select prosrc not like '%raw_user_meta_data->>''terms_version''%' from pg_proc
         where oid = to_regprocedure('public.record_legal_acceptance()'))
         as no_longer_trusts_client_version,
       (select count(*) from pg_trigger
         where tgrelid='auth.users'::regclass
           and tgname='on_auth_user_legal_acceptance' and not tgisinternal)
         as trigger_count_must_be_1;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280021.sql
```
`pinned_terms_version` must be `2026-07-14` and `pinned_privacy_version` `2026-07-15` — the
**same literals the deployed bundle sends** (`HEAD:dashboard/src/components/Auth.jsx:111-112`), so no
real signup changes what gets recorded.

Then, immediately: **sign up a throwaway account through the live dashboard.** Confirm the signup
succeeds, then:

```bash
cat > /tmp/wyfz-apply/post-280021-live.sql <<'SQL'
select la.user_id, la.terms_version, la.privacy_version, la.accepted, la.accepted_at
  from public.legal_acceptances la
  join auth.users u on u.id = la.user_id
 order by u.created_at desc limit 3;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280021-live.sql
```

Expect one fresh row with `accepted = t` and the pinned versions. **If signup fails, treat it as a
Sev-1 and go straight to [§10.2](#102-the-one-fast-rollback-worth-having-280021).**

**(e)** Single transaction. `add column … not null default true` uses PG11+ fast defaults, so no
table rewrite; `legal_acceptances` is one row per user and small. **Additional radius, and the
worst timing risk in the pass:** after commit, every `auth.users` insert runs the new trigger body.
A trigger exception aborts the GoTrue insert, which means **signup returns 500 for everyone**. This
is why the live signup test is mandatory and why `280021` has the only fast rollback in this
runbook. `REVERSE: PITR-ONLY` for the *rows* — reverting to client-authored versions would corrupt
acceptance evidence already written server-side.

**(f)** Yes, with the verification above. Two independent reasons it is low-risk despite the
timing: the pinned versions are byte-identical to what the deployed bundle sends, and the deliberate
omissions in the file's header (no `BEFORE UPDATE OR DELETE` rejection trigger, no PK change) exist
precisely so GoTrue admin user-deletes keep working — `legal_acceptances.user_id` is
`on delete cascade`, and a rejection trigger would have broken every such delete.

---

#### C5 · `202607280022_audit_read_and_transition_evidence.sql`

⚠️ Non-concurrent unique index on `public.audit_events`, plus a one-time `UPDATE public.api_keys`.

**(a)** Adds a partial unique index `audit_events_read_request_idx on public.audit_events
(organization_id, request_id, action) where action like '%.read'`; adds
`append_company_audit_read(...)`; replaces `company_admin_audit_page` to append at most one read
event per request and to exclude `'%.read'` rows from the page it returns (so paging cannot feed on
itself); replaces `company_admin_set_member` to emit `member.role_changed.*` /
`member.status_changed.*` transition events; replaces `company_admin_create_service_account`; and
**backfills `api_keys.created_by`** for device keys from the earliest `device_key.activated` audit
event.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280022.sql <<'SQL'
\echo == required RPC boundary ==
select to_regprocedure('public.company_admin_audit_page(uuid,uuid,timestamptz,bigint,integer,text)')
         is not null as audit_page,
       to_regprocedure('public.company_admin_set_member(uuid,uuid,uuid,text,text,text)')
         is not null as set_member,
       to_regprocedure('public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text)')
         is not null as create_sa,
       to_regprocedure('public.append_company_audit(uuid,text,text,text,text,text,text,text)')
         is not null as append_audit;
\echo == THE INDEX MUST NOT CONFLICT: audit_events is append-only, rows cannot be deleted ==
select count(*) as existing_read_rows_MUST_BE_0
  from public.audit_events where action like '%.read';
select organization_id, request_id, action, count(*)
  from public.audit_events where action like '%.read'
 group by 1,2,3 having count(*) > 1;   -- MUST return zero rows
\echo == index build cost: SHARE lock blocks audit appends for this long ==
select count(*) as audit_events_rows,
       pg_size_pretty(pg_total_relation_size('public.audit_events')) as total_size;
\echo == backfill scope ==
select count(*) filter (where created_by is null) as device_keys_null_created_by,
       count(*) as device_keys_total
  from public.api_keys where key_type = 'device';
select count(*) as activation_events_available
  from public.audit_events
 where action = 'device_key.activated' and actor_user_id is not null;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280022.sql
```
`existing_read_rows_MUST_BE_0` must be `0` and the duplicate query must return **zero rows** — the
index is `UNIQUE` and `audit_events` mutation is rejected by trigger for every role including
`service_role`, so a conflict here has no in-place remedy. Record
`device_keys_null_created_by` — the backfill will change at most that many rows.

**(c)**
```bash
wyfz_run supabase/migrations/202607280022_audit_read_and_transition_evidence.sql
```
Watch [§6.1](#61-lock-hazard-and-the-honest-limitation)'s lock query while this runs.

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280022.sql <<'SQL'
select exists (select 1 from pg_class where relname='audit_events_read_request_idx') as index_created,
       (select indisunique from pg_index i join pg_class c on c.oid=i.indexrelid
         where c.relname='audit_events_read_request_idx') as index_unique,
       (select pg_get_indexdef(c.oid) like '%WHERE%.read%'
          from pg_class c where c.relname='audit_events_read_request_idx') as index_is_partial,
       (select pg_get_indexdef(c.oid)
          from pg_class c where c.relname='audit_events_read_request_idx') as index_definition,
       to_regprocedure('public.append_company_audit_read(uuid,text,text,text,text,text,text)')
         is not null as read_appender;
select (select prosrc like '%append_company_audit_read%' from pg_proc
         where oid = to_regprocedure('public.company_admin_audit_page(uuid,uuid,timestamptz,bigint,integer,text)'))
         as page_records_reads,
       (select prosrc like '%.read%' from pg_proc
         where oid = to_regprocedure('public.company_admin_audit_page(uuid,uuid,timestamptz,bigint,integer,text)'))
         as page_excludes_read_rows,
       (select prosrc like '%role_changed%' from pg_proc
         where oid = to_regprocedure('public.company_admin_set_member(uuid,uuid,uuid,text,text,text)'))
         as transition_events_added;
\echo == backfill result: device keys with NULL created_by should have dropped ==
select count(*) filter (where created_by is null) as device_keys_null_created_by_AFTER,
       count(*) as device_keys_total_UNCHANGED
  from public.api_keys where key_type = 'device';
\echo == the backfill must not have invented an owner outside the org ==
select count(*) as mismatched_backfills_MUST_BE_0
  from public.api_keys k
 where k.key_type='device' and k.created_by is not null
   and not exists (select 1 from public.organization_members m
                    where m.organization_id = k.organization_id
                      and m.user_id = k.created_by);
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280022.sql
```
`device_keys_total_UNCHANGED` must equal the pre-check total. `mismatched_backfills_MUST_BE_0`
should be `0` — if not, an activating user has since left the organization; that is a data
observation, not necessarily a fault, but investigate before relying on `created_by`-scoped
revocation.

**(e)** Single transaction. **Additional radius:** (i) `CREATE UNIQUE INDEX` without
`CONCURRENTLY` takes `SHARE` on `audit_events`, which **blocks every audit append** — and every
company-admin RPC appends — for the build duration; size it from the pre-check. (ii) The backfill
takes row locks on device `api_keys` rows and permanently sets `created_by`. That column was `NULL`
before, so the change is additive and the source (`device_key.activated`, earliest event wins) is
append-only evidence — but `REVERSE: PITR-ONLY`, because you cannot distinguish backfilled
`created_by` values from genuinely-recorded ones afterwards.

**(f)** Yes. All three replaced RPCs keep their exact signatures, so deployed
`api/company_admin.py` continues to work. The one behavioural change a caller could notice is that
`'%.read'` rows no longer appear in returned audit pages — deliberate, and no deployed code depends
on their presence (there are none today).

---

#### C6 · `202607280023_retention_minimization_and_waitlist.sql`

⚠️ **Arms two new deletion/minimization classes.** Nothing runs at apply time.

**(a)** Adds four counters to `public.compliance_retention_runs` (with batch-bounded CHECKs);
replaces `compliance_run_retention` to (i) minimize in place — past a 13-month cutoff — the
ledger-referenced `usage_log` rows it can never delete, and (ii) `delete from public.waitlist` past
a 24-month cutoff; replaces `compliance_retention_worker_cycle`; and adds
`public.erase_waitlist_signup(text,text,text)`. `key_hash` and `request_id` are deliberately **not**
cleared (the partial unique index on those columns — `(key_hash, request_id)` before
`202607280026`, `(key_hash, request_id, authoritative)` after it — would collide and abort
the whole run either way).

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280023.sql <<'SQL'
select to_regprocedure('public.compliance_run_retention(uuid,text,integer,boolean)')
         is not null as retention_fn,
       to_regprocedure('public.compliance_retention_worker_cycle(uuid,uuid,uuid,uuid,text,text,integer)')
         is not null as worker_cycle_fn,
       to_regclass('public.waitlist') is not null as waitlist_table,
       to_regclass('public.compliance_retention_runs') is not null as runs_table;
\echo == what this will make deletable, and how much ==
select (select count(*) from public.waitlist) as waitlist_total,
       (select count(*) from public.waitlist
         where created_at < clock_timestamp() - interval '24 months') as waitlist_past_cutoff,
       (select min(created_at) from public.waitlist) as waitlist_oldest;
select count(*) as usage_rows_past_13_months
  from public.usage_log where ts < clock_timestamp() - interval '13 months';
\echo == the ALTER: table size determines CHECK validation cost ==
select count(*) as retention_runs_rows,
       pg_size_pretty(pg_total_relation_size('public.compliance_retention_runs')) as size;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280023.sql
```
`usage_rows_past_13_months` should be **0** given the schema is ~6 weeks old — confirm, because it
bounds the first minimization run.

**(c)**
```bash
wyfz_run supabase/migrations/202607280023_retention_minimization_and_waitlist.sql
```

**(d)**
```bash
cat > /tmp/wyfz-apply/post-280023.sql <<'SQL'
select count(*) as new_counter_columns_MUST_BE_4
  from information_schema.columns
 where table_schema='public' and table_name='compliance_retention_runs'
   and column_name in ('usage_minimize_candidates','waitlist_candidates',
                       'usage_minimized','waitlist_deleted');
select count(*) as new_check_constraints_MUST_BE_4
  from pg_constraint
 where conrelid='public.compliance_retention_runs'::regclass
   and conname in ('compliance_retention_runs_usage_minimize_candidates_check',
                   'compliance_retention_runs_waitlist_candidates_check',
                   'compliance_retention_runs_usage_minimized_check',
                   'compliance_retention_runs_waitlist_deleted_check');
select (select prosrc like '%update public.usage_log%' from pg_proc
         where oid = to_regprocedure('public.compliance_run_retention(uuid,text,integer,boolean)'))
         as minimizes_usage_log,
       (select prosrc like '%delete from public.waitlist%' from pg_proc
         where oid = to_regprocedure('public.compliance_run_retention(uuid,text,integer,boolean)'))
         as deletes_stale_waitlist,
       (select prosrc not like '%usage.key_hash =%' from pg_proc
         where oid = to_regprocedure('public.compliance_run_retention(uuid,text,integer,boolean)'))
         as key_hash_deliberately_untouched,
       to_regprocedure('public.erase_waitlist_signup(text,text,text)') is not null as erase_fn,
       not has_function_privilege('anon','public.erase_waitlist_signup(text,text,text)','EXECUTE')
         as anon_cannot_erase;
\echo == NOTHING was deleted by applying this ==
select (select count(*) from public.waitlist) as waitlist_total_UNCHANGED,
       (select count(*) from public.compliance_retention_runs) as runs_UNCHANGED;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280023.sql
```
Both `MUST_BE_4` counts must be `4`; `waitlist_total_UNCHANGED` and `runs_UNCHANGED` must equal the
pre-check values.

> **Then, before you ever run retention again:** do a dry run and read the new counters. Do **not**
> run `scripts/dr/retention.sh` in apply mode in the same window.
> ```bash
> scripts/dr/retention.sh --dry-run   # confirm the flag name against the script's own --help
> ```
> `compliance_run_retention`'s fourth argument is the apply/dry-run boolean; the script threads it
> through as `:'apply_value'::boolean`. Confirm `waitlist_candidates` and
> `usage_minimize_candidates` match the numbers from (b) before applying for real.

**(e)** Single transaction; **no rows deleted or modified at apply time**. Verified: the only caller
of `compliance_run_retention` is `scripts/dr/retention.sh` (manual `psql`), and
`compliance_retention_worker_cycle` has **no caller anywhere in `api/`** — so nothing on a schedule
picks this up. `add column … not null default 0` uses fast defaults; the four `CHECK`s validate
existing rows under `ACCESS EXCLUSIVE` on a small evidence table. `REVERSE: PITR-ONLY` once a
retention run has minimized or deleted anything.

**(f)** Yes. No deployed code path reaches the new behaviour without a human running the DR script.

---

#### C7 · `202607280024_browser_role_privilege_completion.sql`

**(a)** Completes the browser-role contract for the four surfaces `280015` did not cover:
`public.audit_events` (RLS-enabled with **zero** policies — so reads are blocked but `TRUNCATE` is
not, and destroying append-only audit history is unrecoverable evidence loss), its identity
sequence, `public.organization_invitations` (holds token digests), `public.legal_acceptances`
(SELECT stays, every write/TRUNCATE goes — its "view own" RLS policy is what narrows it), and every
relation inside the `audit_evidence_archive` schema. Extends
`assert_browser_role_table_privileges()` to cover all of it.

**(b)**
```bash
cat > /tmp/wyfz-apply/pre-280024.sql <<'SQL'
select to_regclass('public.audit_events')              is not null as audit_events,
       to_regclass('public.organization_invitations')   is not null as invitations,
       to_regclass('public.legal_acceptances')          is not null as legal_acceptances,
       to_regclass('public.audit_events_id_seq')        is not null as audit_seq,
       to_regprocedure('public.assert_browser_role_table_privileges()')
         is not null as needs_280015_REQUIRED,
       exists (select 1 from pg_namespace where nspname='audit_evidence_archive')
         as archive_schema;
\echo == the exposure being closed, before ==
select has_table_privilege('anon','public.audit_events','TRUNCATE') as anon_can_truncate_audit,
       has_table_privilege('authenticated','public.organization_invitations','SELECT')
         as auth_can_read_invitations,
       has_table_privilege('authenticated','public.legal_acceptances','UPDATE')
         as auth_can_rewrite_acceptances;
\echo == relations inside the archive schema (may legitimately be zero) ==
select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
 where n.nspname='audit_evidence_archive' and c.relkind='r';
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280024.sql
```
`needs_280015_REQUIRED` must be `t` — this file's own precondition raises otherwise.

**(c)**
```bash
wyfz_run supabase/migrations/202607280024_browser_role_privilege_completion.sql
```

**(d)** The file ends with `select public.assert_browser_role_table_privileges();`, so a successful
apply already proves the extended contract. Confirm independently:
```bash
cat > /tmp/wyfz-apply/post-280024.sql <<'SQL'
\echo == the extended contract: RAISES on violation, empty result otherwise ==
select public.assert_browser_role_table_privileges();
\echo == and the individual revokes ==
select not has_table_privilege('anon','public.audit_events','TRUNCATE')            as audit_truncate_gone,
       not has_table_privilege('authenticated','public.audit_events','SELECT')      as audit_select_gone,
       not has_table_privilege('authenticated','public.organization_invitations','SELECT')
         as invitations_gone,
       has_table_privilege('authenticated','public.legal_acceptances','SELECT')      as acceptances_SELECT_KEPT,
       not has_table_privilege('authenticated','public.legal_acceptances','UPDATE')  as acceptances_UPDATE_gone,
       not has_table_privilege('authenticated','public.legal_acceptances','TRUNCATE') as acceptances_TRUNCATE_gone,
       not has_schema_privilege('anon','audit_evidence_archive','USAGE')             as archive_schema_sealed;
\echo == service_role must be untouched ==
select has_table_privilege('service_role','public.audit_events','INSERT') as sr_audit_insert,
       has_table_privilege('service_role','public.audit_events','SELECT') as sr_audit_select;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280024.sql
```

**(e)** Single transaction. **Additional radius:** `ACCESS EXCLUSIVE` on `audit_events` (hot,
append-only) and three other relations — see [§6.1](#61-lock-hazard-and-the-honest-limitation).
`REVERSE: PITR-ONLY` by policy.

**(f)** Yes — same verification as `280015`: the dashboard never touches these tables with the anon
key. `legal_acceptances` retains `SELECT` for browser roles so the "view own" RLS policy keeps
working. `service_role` is not named in the file.

---

## 9. Verification checklist

Run the window gate after each window; run the whole-chain gate once, at the end.

All verification queries are written out first, at column 0, so they can be copy-pasted without
re-indenting. Create them once, then the checklists below just tell you when to run each and what
the answer must be.

### 9.0 Create the verification queries

```bash
cat > /tmp/wyfz-apply/verify-warm-floor.sql <<'SQL'
select count(*) as rows, min(day) as oldest_day, max(day) as newest_day,
       now() at time zone 'utc' as checked_at_utc
  from public.warm_budget_ledger;
SQL
```

```bash
cat > /tmp/wyfz-apply/verify-no-probes.sql <<'SQL'
select (select count(*) from public.organizations where name like '2026072800%') as probe_orgs,
       (select count(*) from auth.users where email like '2026072800%')          as probe_users,
       (select count(*) from auth.users where email like '%@example.invalid')    as invalid_users,
       (select count(*) from public.period_settlement_ledger)                    as settlements,
       (select count(*) from public.billing_accounts
         where stripe_customer_id like '%probe%')                                as probe_customers;
SQL
```

```bash
cat > /tmp/wyfz-apply/verify-settlement-dryrun.sql <<'SQL'
select * from public.billing_periods_awaiting_settlement(10);
SQL
```

```bash
cat > /tmp/wyfz-apply/verify-billing-frozen.sql <<'SQL'
select (select count(*) from public.period_settlement_ledger)                        as settlements_expect_0,
       (select count(*) from public.organization_billing_arrangement)                as arrangements_expect_0,
       (select count(*) from public.billing_halting_conditions)                      as halting_rows_expect_1,
       (select count(*) from public.billing_ledger)                                  as billing_ledger_expect_0,
       (select count(*) from pg_trigger
         where tgrelid = 'public.usage_log'::regclass and not tgisinternal)          as usage_triggers_expect_0,
       to_regclass('public.billing_monthly') is null                                 as monthly_view_dropped,
       (select count(*) from public.billing_events)                                  as billing_events_compare_to_preflight;
SQL
```

```bash
cat > /tmp/wyfz-apply/verify-ingest.sql <<'SQL'
select count(*) as rows_last_10_min, max(ts) as newest
  from public.usage_log where ts > now() - interval '10 minutes';
SQL
```

```bash
cat > /tmp/wyfz-apply/verify-audit-read.sql <<'SQL'
select action, count(*) from public.audit_events
 where action like '%.read' group by 1 order by 2 desc;
SQL
```

```bash
cat > /tmp/wyfz-apply/verify-tip.sql <<'SQL'
select to_regclass('public.period_settlement_ledger')          is not null as t_psl,
       to_regclass('public.billing_halting_conditions')        is not null as t_bhc,
       to_regclass('public.organization_billing_arrangement')  is not null as t_oba,
       to_regprocedure('public.release_billing_ledger_unsent(bigint,text)')                  is not null as f_release,
       to_regprocedure('public.settle_billing_period(uuid,timestamptz,text,boolean)')        is not null as f_settle,
       to_regprocedure('public.promote_billing_period_settlement(bigint,text,text)')         is not null as f_promote,
       to_regprocedure('public.billing_period_settlement_summary(uuid,timestamptz)')         is not null as f_summary,
       to_regprocedure('public.billing_periods_awaiting_settlement(integer)')                is not null as f_awaiting,
       to_regprocedure('public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)')        is not null as f_revoke,
       to_regprocedure('public.current_legal_versions()')                                    is not null as f_legal,
       to_regprocedure('public.erase_waitlist_signup(text,text,text)')                       is not null as f_erase,
       to_regprocedure('public.append_company_audit_read(uuid,text,text,text,text,text,text)') is not null as f_auditread,
       to_regprocedure('public.assert_browser_role_table_privileges()')                      is not null as f_contract,
       to_regclass('public.billing_monthly')                   is null     as v_monthly_dropped;
SQL
```

### 9.1 Window A gate

- [ ] `280017`, `280018`, `280020` each exited 0 and carry an `OK` line in `$WYFZ_LOG`.
- [ ] `post-280017.sql`: all five columns `t` — the 365-day ledger floor and 30-day prefix cap are in
      the function body, it is still `SECURITY DEFINER`, `service_role` can call it, `anon` cannot.
- [ ] `post-280018.sql` and `post-280020.sql` green.
- [ ] **Warm evidence has stopped shrinking.** Run `verify-warm-floor.sql`, note `oldest_day`, wait
      at least 10 minutes (two worker cycles at 300 s), run it again, and confirm `oldest_day` has
      **not** advanced. This is the proof that the money bug is actually closed:
      `supabase db query --linked -f /tmp/wyfz-apply/verify-warm-floor.sql`
- [ ] The worker is healthy — no `warm_state_purge_failed` in its logs.

### 9.2 Window B gate

- [ ] All ten files exited 0.
- [ ] `verify-billing-frozen.sql`: `settlements_expect_0 = 0`, `arrangements_expect_0 = 0`
      (absence *is* the unbillable state — never backfill it), `halting_rows_expect_1 = 1`,
      `billing_ledger_expect_0 = 0`, `usage_triggers_expect_0 = 0`, `monthly_view_dropped = t`, and
      `billing_events_compare_to_preflight` equal to the preflight number.
- [ ] `verify-no-probes.sql`: **every column 0.** No probe organization, user, settlement or
      billing account survived any of the four self-testing migrations.
- [ ] Nobody can bill: `post-280013.sql`'s `sr_may_NOT_promote_REQUIRED`, `anon_may_NOT_promote`,
      `auth_may_NOT_promote` and `public_may_NOT_promote` are all `t`.
- [ ] The inner guard is sealed: `service_role` has **no** `EXECUTE` on
      `assert_billing_period_halting_conditions` (from `post-280009.sql`).
- [ ] `verify-settlement-dryrun.sql` returns without error. Read the rows; **promote nothing.**
      With traffic 100% non-authoritative and unpriced, every period should show no eligible rows.
- [ ] Onboarding actually unblocked: `post-280005-gate.sql` shows `orgs_now_cli_connected > 0`
      (assuming `installations_to_backfill` was > 0).

### 9.3 Window C gate

- [ ] All seven files exited 0.
- [ ] `select public.assert_browser_role_table_privileges();` returns without raising — the combined
      `280015` + `280024` contract.
- [ ] `service_role` privileges on `usage_log`, `profiles` and `audit_events` are **identical** to
      the pre-`280015` / pre-`280024` readings you recorded.
- [ ] **Usage ingestion still works** — the check that `280015`'s revokes did not hit the API's role.
      Send one real request through the proxy, then run
      `supabase db query --linked -f /tmp/wyfz-apply/verify-ingest.sql`. `rows_last_10_min` must be
      > 0 and `newest` must be recent.
- [ ] **Signup still works** — the throwaway signup from `280021`(d) succeeded and
      `post-280021-live.sql` shows a fresh `legal_acceptances` row with `accepted = t` and versions
      `2026-07-14` / `2026-07-15`.
- [ ] **Login still works** for an existing account. `280015` dropped a `profiles` policy; the
      dashboard reads `profiles` only through the API's `service_role`, so this should be unaffected
      — confirm rather than assume.
- [ ] Company-admin audit paging still returns a page, and read events now appear:
      `supabase db query --linked -f /tmp/wyfz-apply/verify-audit-read.sql`
- [ ] `select count(*) from public.waitlist` unchanged, `compliance_retention_runs` row count
      unchanged, and **`scripts/dr/retention.sh` has not been run in apply mode.**
- [ ] Device-key backfill sane: `post-280022.sql`'s `mismatched_backfills_MUST_BE_0` = 0 and
      `device_keys_total_UNCHANGED` matches its pre-check.

### 9.4 Whole-chain gate

- [ ] Each of the twenty files appears exactly once in `$WYFZ_LOG` with an `OK` line.
- [ ] `202607170012` appears **nowhere** in `$WYFZ_LOG`.
- [ ] `supabase db push` appears **nowhere** in this session's shell history.
- [ ] `supabase db query --linked -f /tmp/wyfz-apply/verify-tip.sql` — **all fourteen columns `t`.**
- [ ] `public.brevitas_schema_migrations` still does **not** exist (L5).
- [ ] `$WYFZ_LOG` and `/tmp/wyfz-apply/evidence/` archived somewhere durable, together with
      `APPLIED_FROM_COMMIT.txt` and `PITR_REFERENCE_UTC.txt`. **This is the only ledger wyfz has.**
- [ ] Clean up `/tmp/wyfz-apply` only *after* the evidence is archived.

---

## 10. Abort and rollback

### 10.1 If a file fails

1. **Do nothing else to the database.** Do not retry. Do not edit the migration. Do not skip ahead.
2. Capture the error verbatim from `$WYFZ_LOG`.
3. Confirm the file left nothing behind — a single-transaction file that aborts is a no-op. Run this
   (block is at column 0 so it pastes cleanly):

```bash
cat > /tmp/wyfz-apply/abort-check.sql <<'SQL'
select to_regclass('public.period_settlement_ledger')         is not null as psl,
       to_regclass('public.billing_halting_conditions')       is not null as bhc,
       to_regclass('public.organization_billing_arrangement') is not null as oba,
       (select count(*) from public.organizations where name like '2026072800%') as probe_orgs,
       (select count(*) from auth.users where email like '%@example.invalid')    as probe_users,
       (select count(*) from pg_stat_activity
         where datname = current_database() and state = 'idle in transaction')   as stuck_txns;
SQL
supabase db query --linked -f /tmp/wyfz-apply/abort-check.sql
```

   `probe_orgs` and `probe_users` must be `0`. If they are not, the applier did **not** honour the
   transaction — treat as a Sev-1 and go to [§10.4](#104-escalation).
4. Most failures are a precondition raising on purpose. Read the `HINT:` — these files carry
   actionable hints (`'Apply 202607280007 first'`, `'Reapply 202607280006 first'`,
   `'refuses to run beside the retired generic key RPC'`). Fix the *precondition*, never the file:
   the file bodies are checksum-frozen and editing one silently forks production from CI.
5. If the failure is a lock timeout or a cancelled statement, nothing was applied. Wait for the
   blocker, then re-run the same file.

### 10.2 The one fast rollback worth having (`280021`)

`280021` is the only file whose failure mode is an **immediate customer-facing outage** (signup
returns 500 for everyone). Prepare this before applying it, and keep it in a terminal.

```bash
cat > /tmp/wyfz-apply/EMERGENCY-detach-legal-trigger.sql <<'SQL'
-- EMERGENCY ONLY. Unblocks signup by detaching the legal-acceptance trigger.
-- Cost: new accounts get NO legal_acceptances row until this is resolved.
-- That is a compliance gap you must close the same day, and it is strictly
-- better than a total signup outage.
begin;
drop trigger if exists on_auth_user_legal_acceptance on auth.users;
commit;
SQL
```

Use it only if a live signup fails after `280021`. Then, deliberately, either re-create the trigger
from `202607280021` once the defect is understood, or restore the prior body from
`20260715_analytics_privacy.sql` — noting that the prior body writes **client-authored** versions,
which is the defect `280021` exists to fix. Prefer forward-fixing.

Files with published in-file rollback procedures — read them in the file itself, they are precise:

| File | Where |
|---|---|
| `280007` | tail comment: verify `count(*) = 0`, drop 2 triggers, drop table, drop 3 functions |
| `280010` | tail comment: re-run `280007`'s `prevent_period_settlement_identity_change()` body |
| `280011` | tail comment: `DROP FUNCTION … release_billing_ledger_unsent`; pause workers first |
| `280013` | tail comment: drop 4 functions in order, then restore `280012`'s narrower evidence function verbatim |

`280010` and `280013`'s procedures **reopen the defects they closed** (`PSL-LATCH`; re-billing a
reported period at full rate). Use them only to unblock an incident, and re-apply immediately.

### 10.3 What CANNOT be rolled back — read this before Window C

Be honest with yourself about this list. There is no `down` migration for any of it.

**PITR is the only rollback.** **Twelve** of the twenty files declare `REVERSE: PITR-ONLY` in their
own headers — `280013`, `280014`, `280015`, `280016`, `280017`, `280018`, `280019`, `280020`,
`280021`, `280022`, `280023`, `280024`, i.e. **everything in Windows B-tail and C** plus two of
Window A. Confirm the list yourself before you start:

```bash
grep -l "REVERSE: PITR-ONLY" supabase/migrations/2026072800{05,06,07,08,09,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24}_*.sql
```

For `280019` and `280020` the marker refers to *effects*, not to the functions: dropping the added
function or restoring the prior `claim_ai_job` body is mechanically clean — what cannot be undone is
a device key already revoked through the new path, or a job already terminalized by the widened
sweep.

Your PITR reference point is in `/tmp/wyfz-apply/PITR_REFERENCE_UTC.txt`. **And PITR restores the
whole database** — including every row of real customer traffic written since that timestamp
(~12k `usage_log` rows/day, plus signups, audit events and billing events). A PITR restore to undo a
privilege revoke would silently discard hours of production data. It is a last resort, not a
rollback plan.

Specifically irreversible, even with PITR:

| What | Why it cannot be undone |
|---|---|
| **`202607170012` becomes unreplayable** | Once `280006` commits, `202607170012` raises if replayed, because it guards on the per-row fee trigger being present. Satisfying it again means re-attaching that trigger — which `280008`/`280009`/`280012`/`280013` all refuse to coexist with. This is intended and permanent. |
| **The `280005` installations backfill** | Real `devices` and `installations` rows now exist and the onboarding gate reads them. The sentinel `bvx_version = 'device-auth'` identifies them, so deletion is *possible* — but you would be deleting evidence that a device activated, and any org that has since progressed past the gate would regress. |
| **The `280022` `api_keys.created_by` backfill** | Backfilled values are indistinguishable from natively-recorded ones after the fact. There is no "was backfilled" marker. |
| **The `audit_events` unique index, if it ever conflicts** | `audit_events` is append-only for every role including `service_role` (`reject_audit_event_mutation`) with a 400-day retention obligation. You cannot delete a conflicting row to make the index buildable. This is why the pre-check demands `existing_read_rows_MUST_BE_0 = 0`. |
| **Any `period_settlement_ledger` row, once written** | The table has an unconditional `BEFORE DELETE` guard and immutable identity columns. Corrections *append a revision*; they never mutate. A row written here is a seven-year financial record. `280007`'s own rollback procedure requires `count(*) = 0` for exactly this reason. |
| **Any settlement promoted to `pending`** | `280010`'s latches make `outbound_started_at` / `reported_at` / `settled_at` one-way. Once the outbound marker is set, the row can never be voided-and-re-sent — by design, so Stripe cannot be double-charged. There is no un-promote. |
| **Warm-spend evidence already purged before `280017`** | The deployed worker has been deleting `warm_budget_ledger` rows older than 7 days on a 300-second loop. Whatever is already gone is gone; `280017` only stops future loss. Preflight item (10) tells you how much history survives. Any settlement over a period whose warm days were already purged will **overstate** net savings and must not be promoted. |
| **Anything `scripts/dr/retention.sh` deletes after `280023`** | `usage_log` minimization overwrites columns in place; the waitlist `delete` is a delete. `compliance_retention_runs` is immutable evidence *of* the run, not of the data. This is why `280023` must be followed by a `--dry-run`, never a blind apply run. |
| **Anything a tenant erasure deletes after `280016`** | Widened erasure now removes `warm_credentials` (KMS-encrypted provider key ciphertext), `warm_prefixes` and `warm_budget_ledger`. Restoring the narrow wrappers afterwards brings none of it back. |
| **`public.billing_monthly`** | Mechanically trivial to recreate (two lines). Forbidden, because recreating the definer view reopens cross-tenant anon-readable billing. Treat as permanent. |
| **The browser-role privilege posture (`280015`, `280024`)** | Mechanically reversible with `GRANT`. Forbidden: re-granting the Supabase project defaults hands `anon`/`authenticated` `TRUNCATE` on `audit_events` — unrecoverable evidence loss — and `SELECT` on `api_keys` and per-tenant billing. If something breaks, fix the caller, not the grant. |

### 10.4 Escalation

If the applier turns out not to honour file transactions ([§4](#4-prove-the-applier-honours-transactions)),
or if `probe_orgs`/`probe_users` are non-zero after a failure, or if a `period_settlement_ledger`
row exists that you did not deliberately create: **stop the pass entirely.** Do not attempt repair
with ad-hoc SQL against a ledger-less production database. Capture `$WYFZ_LOG`, the output of
`abort-check.sql`, and `verify-no-probes.sql`, and escalate.

---

## 11. Sequencing relative to the app-code deploy

The headline: **DB-first for every one of the twenty files.** Not one of them requires code that is
not yet deployed, and three of them fix bugs in code that is running right now.

| Order | Action | Why this order |
|---|---|---|
| **1** | **Env only:** set `BREVITAS_WARM_RETENTION_DAYS=365`, restart the worker. | Stops the live deletion of warm-spend billing evidence in one deploy cycle, with no DDL. See [§2.1](#21-why-202607280017-goes-first-out-of-manifest-order). |
| **2** | **DB: Window A** (`280017`, `280018`, `280020`). | Structural version of step 1, plus two fences on deployed behaviour. All three *fix* deployed code; none needs new code. Code-first would be strictly worse — every hour of delay on `280017` destroys another slice of evidence. |
| **3** | **DB: Window B** (`280005` … `280014`). | `280005` fixes a live onboarding outage for the already-shipped BVX 0.1.27 and needs no new code at all. `280011` and `280013` **must** precede the code deploy: the new `api/billing_recovery.py` calls `release_billing_ledger_unsent` and the new `src/app/api/billing/status/route.ts` calls `billing_period_settlement_summary`; both would fail with `42883` against today's schema. The deployed revisions call neither, so DB-first is safe in both directions. |
| **4** | **DB: Window C** (`280015`, `280016`, `280019`, `280021`, `280022`, `280023`, `280024`). | All safe with deployed code (verified per-file in §8(f)). Do it before the code deploy so that if signup or ingestion breaks, you are debugging *one* change and not two. Low-traffic window. |
| **5** | **Verify** — all of [§9](#9-verification-checklist), including live signup and live usage ingestion. | The schema must be proven good while the *old* code is still the only thing running. That isolation is the entire reason for DB-first. |
| **6** | **Deploy the app code** (API + worker + dashboard + Next.js routes). | Now every RPC the new code calls exists. Nothing in the deploy can 42883. |
| **7** | **Verify again** after the deploy: billing status route returns without a 500, worker's `StripeUnavailable` branch is reachable, device-key revocation appears in the dashboard, `bvx login` completes onboarding end to end. | These are the code-side halves of DB changes made in steps 3–4. |
| **8** | **Separately, deliberately, later:** the billing flip. | Reviewed as its own change, gated on: a reconciled provider invoice; at least one org with an attested `organization_billing_arrangement`; `usage_log` producing rows that are actually `authoritative` and `priced` (today: **zero**, every day since 07-15); and enough post-`280017` warm history that the deduction is real. `promote_billing_period_settlement` is granted to nobody precisely so this cannot happen by deploy. |

### The one thing that is code-first

Nothing in the database. But two code-side follow-ups are owed and are **not** blockers:

- `api/store.py:3326` is the declared SQLite mirror of `purge_warm_state`; after `280017` it
  diverges from Postgres until the code deploy.
- `api/jobs.py:496` / `:259` reproduce the pre-`280020` expiry asymmetry in the SQLite and in-memory
  job stores.

Both are backend-parity gaps in non-production stores. Ship them with step 6.

### If you must deploy code before finishing the DB

Then the minimum viable DB set before **any** code deploys is: **Window A + `280011` + `280013`**
(and `280013` requires `280007` → `280008` → `280009` → `280010`, and `280008`/`280009`/`280013`
require `280006`'s freeze). In other words: Window A, then `280006` through `280013`. `280005`,
`280014` and all of Window C can trail the code deploy safely — but `280005` is fixing a live
onboarding outage, so trailing it costs you customers.

---

## WINDOW B3 ADDENDUM (2026-07-30) — `202607280029_period_settlement_claim_path.sql`

Appended 2026-07-30. Everything above described the `280005`–`280024` pass, which is **done**:
wyfz now carries the full `280005`–`280024` chain, applied through Windows A–C, **plus** the
2026-07-30 function realignment (`scripts/db/wyfz_function_realignment_20260730.sql`), which
reconciled the two functions that diverged because the chain reached wyfz out of manifest order
(`280021`'s live `auth.users` trigger broke `280010`/`280012`'s embedded probes; see that file's
header). wyfz does **not** carry `202607280025`–`202607280028`, and `202607280028` is
**quarantined** (its review found a blocker + 3 highs) — it must not be applied in this window or
any other until its owner decision lands.

This addendum covers one future file: **`202607280029_period_settlement_claim_path.sql`**, the
settlement claim/send path (eight `security definer` functions: `claim_period_settlement_entries`,
`mark_period_settlement_outbound_started`, `renew_period_settlement_lease`,
`complete_period_settlement_entry`, `release_period_settlement_leases`,
`release_period_settlement_claim`, `period_settlement_recovery_health`,
`billing_period_settlement_history`). Function-only; it must write no rows and grant no table
privilege on `period_settlement_ledger`.

All Laws (L1–L6) apply unchanged: one file, `supabase db query --linked -f`, never `db push`,
stop on first non-zero exit, no `brevitas_schema_migrations`, keep the hand-written log.

### B3.0 Blocking gate — the file DOES NOT EXIST yet. Do not open this window.

As of 2026-07-30 the migration has **not been authored**: `supabase/migrations/` ends at
`202607280028`, and the Phase-4 build's adversarial review verdict is **do-not-ship** (0 of the 8
functions defined anywhere; see `docs/STRIPE_BUILD_REPORT.md`, "SETTLEMENT SENDER (Phase 4)").
This addendum is written against the reviewed *design contract*, so its queries pin what the file
must produce — but every one of the following must be true before you run anything here:

- [ ] `supabase/migrations/202607280029_period_settlement_claim_path.sql` exists, is registered
      in `expectedFreshMigrationOrder`, **both** manifests, and
      `scripts/ci/migration-frozen-checksums.txt` (same commit — the shared-registrar rule), and
      has an apply line in `scripts/ci/run-migration-tests.sh`.
- [ ] It carries a `-- REVERSE:` posture header (`verifyReversePosture` governs everything from
      `202607280013` on; the `REVERSE_POSTURE_CUTOFF` constant does **not** exempt it).
- [ ] `node scripts/ci/verify-migrations.mjs` exit 0, `npm test` fully green (including
      `tests/billing_route_dependency_degradation.test.mjs`, which is red today precisely because
      this file is missing), and the migration harness exit 0 on **both** paths.
- [ ] The Phase-4 review's do-not-ship has been re-ruled after the SQL landed, and the caestus
      acceptance run (claim → begin_send → real TEST-mode meter event → reconcile → `reported`,
      settlement `1000000005`) has been executed and matches its expected numbers.
- [ ] `shasum -a 256` of the file matches the frozen checksum line (§3 idiom).
- [ ] **Dependency check against the quarantine:** confirm the authored file has no precondition
      on `202607280025`–`202607280028` (wyfz has none of them). In particular it must not assert
      `202607280028`'s anchored shape of `billing_period_settlement_evidence` — the precondition
      query below pins wyfz's realigned (pre-0028) md5. If the file requires any of 0025–0028,
      **stop and re-plan this window**; do not apply 0028 to satisfy it.
- [ ] **The `280021` probe trap:** if the file carries embedded self-tests that insert
      `auth.users` probe rows, they MUST set `created_at` (the live legal-acceptance trigger
      copies it into a NOT NULL column; bare probe inserts are exactly what broke `280010`/
      `280012` on wyfz). Read the file's probe blocks and verify before applying.

### B3.1 Ordering note — deliberate divergence, recorded

Applying `280029` while skipping `280025`–`280027` diverges wyfz further from manifest order.
That is acceptable and deliberate: none of the three is a settlement-sender prerequisite
(waitlist budget, usage-log dedupe, browser TRUNCATE contract), `280028` is quarantined, and wyfz
already diverges by construction (the realignment file is the precedent and the record). Record
in `$WYFZ_LOG` that 0025–0028 were intentionally not applied, so the next operator does not
"helpfully" backfill them — 0028 especially.

### B3.2 · `202607280029_period_settlement_claim_path.sql`

**(a)** Adds the claim/lease/send/complete/release path for `period_settlement_ledger` plus the
recovery-health and settlement-history reads. All eight functions `security definer`, EXECUTE
revoked from `public`/`anon`/`authenticated`, granted to `service_role` only. The claim leases
rows that **stay `status='pending'`** (only `begin_send` enters `'sending'`, stamping
`outbound_started_at` in the same UPDATE — the `202607280010` latches make any other shape
permanently stuck). No table privileges change. No rows written.

**(b)** Precondition:
```bash
cat > /tmp/wyfz-apply/pre-280029.sql <<'SQL'
\echo == prerequisites: the 280007-280013 settlement stack must be present ==
select to_regclass('public.period_settlement_ledger') is not null as psl_table,
       to_regclass('public.billing_halting_conditions') is not null as bhc_table,
       to_regprocedure('public.settle_billing_period(uuid,timestamptz,text,boolean)')
         is not null as f_settle,
       to_regprocedure('public.promote_billing_period_settlement(bigint,text,text)')
         is not null as f_promote,
       to_regprocedure('public.billing_period_settlement_summary(uuid,timestamptz)')
         is not null as f_summary;
\echo == wyfz realignment end-state: both md5s must match the canonical (pre-0028) chain ==
select md5(pg_get_functiondef(to_regprocedure(
         'public.prevent_period_settlement_identity_change()')))
         = 'fc6a55f1f773925355d3274f55f6a7c0' as identity_guard_canonical,
       md5(pg_get_functiondef(to_regprocedure(
         'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)')))
         = 'ddcb0f6d601e7a29370da6920e63e24e' as evidence_fn_canonical_pre_0028;
\echo == none of the new functions may exist yet ==
select to_regprocedure('public.claim_period_settlement_entries(text,integer,integer,bigint)') is null as claim_absent,
       to_regprocedure('public.mark_period_settlement_outbound_started(bigint,text)')  is null as begin_send_absent,
       to_regprocedure('public.renew_period_settlement_lease(bigint,text,integer)')    is null as renew_absent,
       to_regprocedure('public.complete_period_settlement_entry(bigint,text,text,text)') is null as complete_absent,
       to_regprocedure('public.release_period_settlement_leases(text)')                is null as release_owner_absent,
       to_regprocedure('public.release_period_settlement_claim(bigint,text)')          is null as release_claim_absent,
       to_regprocedure('public.period_settlement_recovery_health()')                   is null as health_absent,
       to_regprocedure('public.billing_period_settlement_history(uuid,integer)')       is null as history_absent,
       to_regprocedure('public.release_period_settlement_unsent(bigint,text)')         is null as banned_name_absent_REQUIRED;
\echo == money state: wyfz must still be frozen ==
select (select count(*) from public.period_settlement_ledger) as settlements_expect_0,
       (select count(*) from public.billing_ledger)           as billing_ledger_expect_0,
       (select coalesce(max(id),0) from public.billing_ledger) as ledger_max_id_must_be_lt_1e9,
       (select seqstart from pg_sequence
         where seqrelid = pg_get_serial_sequence(
                 'public.period_settlement_ledger','id')::regclass)
         as psl_seq_start_expect_1e9,
       (select count(*) from pg_trigger
         where tgrelid='public.usage_log'::regclass and not tgisinternal)
         as usage_triggers_expect_0;
SQL
supabase db query --linked -f /tmp/wyfz-apply/pre-280029.sql 2>&1 | tee -a "$WYFZ_LOG"
```
Every boolean `t`, `settlements_expect_0 = 0` (**if not 0, STOP** — a settlement row on wyfz that
this runbook did not create is the §10.4 escalation case), `billing_ledger_expect_0 = 0`,
`psl_seq_start_expect_1e9 = 1000000000` (the Stripe identifier id-space disjointness that
`202607280007:478-486` asserts at apply time and the new file's own self-check should re-assert).

**(c)**
```bash
wyfz_run supabase/migrations/202607280029_period_settlement_claim_path.sql
```

**(d)** Postcondition:
```bash
cat > /tmp/wyfz-apply/post-280029.sql <<'SQL'
\echo == all eight functions exist ==
select to_regprocedure('public.claim_period_settlement_entries(text,integer,integer,bigint)') is not null as f_claim,
       to_regprocedure('public.mark_period_settlement_outbound_started(bigint,text)')  is not null as f_begin_send,
       to_regprocedure('public.renew_period_settlement_lease(bigint,text,integer)')    is not null as f_renew,
       to_regprocedure('public.complete_period_settlement_entry(bigint,text,text,text)') is not null as f_complete,
       to_regprocedure('public.release_period_settlement_leases(text)')                is not null as f_release_owner,
       to_regprocedure('public.release_period_settlement_claim(bigint,text)')          is not null as f_release_claim,
       to_regprocedure('public.period_settlement_recovery_health()')                   is not null as f_health,
       to_regprocedure('public.billing_period_settlement_history(uuid,integer)')       is not null as f_history;
\echo == privilege posture: service_role only, for every one of the eight ==
select p.oid::regprocedure as fn,
       has_function_privilege('service_role', p.oid, 'EXECUTE')      as sr_expect_t,
       not has_function_privilege('anon', p.oid, 'EXECUTE')          as anon_blocked_expect_t,
       not has_function_privilege('authenticated', p.oid, 'EXECUTE') as auth_blocked_expect_t
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public'
   and p.proname in ('claim_period_settlement_entries','mark_period_settlement_outbound_started',
                     'renew_period_settlement_lease','complete_period_settlement_entry',
                     'release_period_settlement_leases','release_period_settlement_claim',
                     'period_settlement_recovery_health','billing_period_settlement_history')
 order by 1;
\echo == the table stays sealed: zero PostgREST table privileges (21 checks, all false) ==
select count(*) as postgrest_table_privs_MUST_BE_0
  from (values ('anon'),('authenticated'),('service_role')) roles(r)
 cross join (values ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),
                    ('TRUNCATE'),('REFERENCES'),('TRIGGER')) privs(p)
 where has_table_privilege(roles.r, 'public.period_settlement_ledger', privs.p);
\echo == the banned 429-path name must still not exist, and the replacement must not touch the marker ==
select to_regprocedure('public.release_period_settlement_unsent(bigint,text)') is null
         as banned_name_still_absent_REQUIRED,
       (select prosrc not like '%outbound_started_at%' from pg_proc
         where oid = to_regprocedure('public.release_period_settlement_claim(bigint,text)'))
         as release_claim_never_clears_marker_REQUIRED;
\echo == the migration wrote no rows ==
select (select count(*) from public.period_settlement_ledger) as settlements_STILL_0,
       (select count(*) from public.billing_ledger)           as billing_ledger_STILL_0;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280029.sql 2>&1 | tee -a "$WYFZ_LOG"
```
Then two behavioral reads — safe on an empty ledger, and the only exercise wyfz can give this
path today:
```bash
cat > /tmp/wyfz-apply/post-280029-behave.sql <<'SQL'
\echo == health over an empty ledger: one row, zeros/nulls ==
select * from public.period_settlement_recovery_health();
\echo == claim on an empty ledger: zero rows, nothing swept, nothing written ==
select * from public.claim_period_settlement_entries('wyfz-b3-probe', 60, 1, 1000000);
select count(*) as settlements_STILL_0_after_claim from public.period_settlement_ledger;
\echo == history refusal vocabulary for an org with no billing account ==
select public.billing_period_settlement_history(
         '00000000-0000-0000-0000-000000000000'::uuid, 1) as expect_ok_false_no_billing_account;
SQL
supabase db query --linked -f /tmp/wyfz-apply/post-280029-behave.sql 2>&1 | tee -a "$WYFZ_LOG"
```
The claim call is a real invocation, not read-only — on an empty ledger its sweeps match zero
rows and it returns zero rows, so its only observable effect is nothing; the follow-up count
pins that. Expect the history call to return `{"ok": false, "code": "no_billing_account"}`.

**(e)** Single transaction (verify the file: `grep -c '^begin;'` = 1, `grep -c '^commit;'` = 1,
no `CONCURRENTLY`), functions only, no rows, no locks beyond catalog. A mid-file failure is a
no-op. If the authored file carries self-test fixtures, they must self-unwind AND respect the
B3.0 `created_at` trap; re-run `verify-no-probes.sql` after applying regardless. Reverse posture:
whatever its `-- REVERSE:` header says — expect `DDL:` drop statements for the eight functions,
since it writes no data.

**(f)** **DB-first, and the code that uses it stays dark.** The deployed worker calls none of
these RPCs unless `BREVITAS_BILLING_SETTLEMENT_ENABLED` is exactly `"true"` — leave it unset on
Railway; the review requires the observability fixes (per-row gauge clobber, dead settlement
alert names) before that flag ever flips. The status route's history read degrades to
`settlement_history: null` + HTTP 200 when the RPC is absent, so either deploy order is safe;
DB-first simply makes the settled-weeks UI truthful on the first post-deploy request. **Nothing
this file installs can move money on wyfz**: zero settlements exist, `billing_ledger` is empty,
`promote_billing_period_settlement` is granted to nobody, and production traffic is still 100%
unbillable (the drought) — the apply is inert plumbing, which is exactly why it is safe to land
DB-first and prove in place.

### B3.3 Window gate

- [ ] B3.0 fully satisfied **before** connecting to wyfz (file exists, registered, frozen,
      reviewed, caestus acceptance run green, no 0025–0028 dependency, probe trap checked).
- [ ] `pre-280029.sql` all green, `settlements_expect_0 = 0`.
- [ ] `280029` exited 0 with an `OK` line in `$WYFZ_LOG`.
- [ ] `post-280029.sql`: eight functions present; every row of the privilege table
      `t`/`t`/`t`; `postgrest_table_privs_MUST_BE_0 = 0`; both `REQUIRED` booleans `t`;
      both row counts still 0.
- [ ] `post-280029-behave.sql`: health returns, claim returns zero rows and writes nothing,
      history refuses with `no_billing_account`.
- [ ] `verify-no-probes.sql` (§9.0): every column still 0.
- [ ] `BREVITAS_BILLING_SETTLEMENT_ENABLED` confirmed **unset** in the Railway service env.
- [ ] `$WYFZ_LOG` records that `202607280025`–`202607280028` were deliberately not applied.
