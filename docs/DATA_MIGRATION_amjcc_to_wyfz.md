# Data migration runbook: `amjccgcgkcpbyevkjabw` → `wyfzmfnswtzyhwbltbpy`

> ## ⚠️ SEQUENCING HAZARD — READ FIRST
>
> The **live site currently reads Supabase project `amjccgcgkcpbyevkjabw`** (the
> deployed dashboard bundle and the Railway API resolve their Supabase URL / keys
> to it). The **declared production project is `wyfzmfnswtzyhwbltbpy`**, which holds
> the reconciled *schema* but **little or no user data**.
>
> **DO NOT repoint the deployed bundle, the API env, or any Supabase URL/anon/
> service-role secret to `wyfzmfnswtzyhwbltbpy` until this migration has completed
> and reconciled clean.** If you cut the bundle over first, every live user
> authenticates against an empty project and loses their account, API keys, usage
> and billing history. The only safe order is:
>
> 1. Run this migration (copy data amjcc → wyfz).
> 2. Reconcile row counts per table.
> 3. **Only then** repoint the bundle / API env (Step 7).
>
> `amjccgcgkcpbyevkjabw` is the project being decommissioned — but it is the
> **source of truth today**, so it is decommissioned *last*, after cutover is
> verified.

> **No automated agent has run this.** This file and
> `scripts/db/migrate-project-data.sh` were authored by an assistant with **no
> database credentials**. Every step that touches a live secret or mutates either
> project must be run by **you (James)** in your own terminal. The script defaults
> to `--dry-run` and refuses to write without `--execute` plus a typed confirmation.

---

## 0. Relationship to the schema reconciliation runbook

`docs/PROD_DB_RECONCILIATION.md` gets the **schema** of `wyfzmfnswtzyhwbltbpy`
into shape (apply the 46-migration chain; it also *drops* `user_keys` by design).
**That must be done first** — this runbook assumes the destination already has all
tables. This runbook is the **complementary data step**: it moves the *rows* that
live in `amjccgcgkcpbyevkjabw` into that freshly-migrated destination.

Two projects, two distinct jobs:

| Concern | Project | Runbook |
| --- | --- | --- |
| Schema present + correct | `wyfzmfnswtzyhwbltbpy` | `PROD_DB_RECONCILIATION.md` |
| **Rows carried over** | `amjcc…` → `wyfz…` | **this file** |

---

## 1. Prerequisites (you)

- **Both projects' DB connection strings**, from Supabase dashboard → *Project
  Settings → Database → Connection string*. Use the **session-mode pooler
  (port 5432)** or the **direct connection**, **not** the transaction pooler
  (6543): `pg_dump`, `setval()` and `--disable-triggers` all need a real session
  and the `postgres` role.
  - `SRC_DATABASE_URL` → `amjccgcgkcpbyevkjabw` (source / live).
  - `DST_DATABASE_URL` → `wyfzmfnswtzyhwbltbpy` (destination / declared prod).
  - **These are secrets. Keep them in your shell only; never paste into chat, and
    never commit them.**
- **Postgres 16/17 client** (`pg_dump`, `pg_restore`, `psql`) locally. The client
  major version must be ≥ the server's.
- **A maintenance window.** During the copy the source must be effectively
  read-only, or rows created mid-flight will be missed. Options, best first:
  1. Put the app in maintenance mode (stop writes) for the window, **or**
  2. Pause new signups/usage and accept that anything written after the dump
     snapshot needs a delta pass.
  The window is short (this dataset is small) but the cutover in Step 7 should
  happen inside the same window so no writes land on the abandoned source.
- The **schema reconciliation of the destination is already done** (see §0).
- A **full backup of the destination** taken *before* you truncate/load, in case
  a `--truncate-dst` run needs undoing:
  ```bash
  pg_dump "$DST_DATABASE_URL" --format=custom --no-owner --no-privileges \
    -f scratchpad/wyfz_pre_dataload_$(date +%Y%m%d_%H%M).dump
  ```

---

## 2. What is copied, and in what order

### 2a. `auth.users` FIRST — and why it is special

Almost every business table foreign-keys to `auth.users(id)`: `profiles`,
`billing_accounts`, `billing_events`, `billing_ledger`, `legal_acceptances`,
`organizations`, `organization_members`, `organization_invitations`,
`active_company_selections`, `bvx_device_consumption_receipts`, and the
compliance tables. **No public row can point at a user that does not exist in the
destination's `auth.users`.** So auth users must be present in the destination
*before* the public-table load.

**`auth.users` cannot be trivially `pg_dump`ed and re-inserted** the way public
tables can:

- It is a **GoTrue-managed** schema. Rows carry bcrypt password hashes,
  confirmation/recovery token state, and related rows in `auth.identities`,
  `auth.sessions`, `auth.refresh_tokens`, `auth.mfa_factors`, etc. Sessions and
  refresh tokens are **short-lived and should not be copied** (users simply
  re-log-in); users + identities are what matter.
- Supabase's own guidance for cross-project user moves is the **Auth admin API**
  (`GET /auth/v1/admin/users` on the source, then
  `POST /auth/v1/admin/users` on the destination with
  `email_confirm: true`) — this recreates users with their UUIDs preserved. Doing
  it via the admin API keeps GoTrue's invariants intact. **Preserving each user's
  UUID is mandatory**, because it is the FK target every public row references.
- The lower-level alternative is a **`pg_dump` of the `auth` schema** (users +
  identities only) loaded with `--disable-triggers`. The script exposes this
  behind `--include-auth`, but treat it as best-effort: encrypted columns and
  GoTrue-owned sequences can drift, and passwords may need a reset flow. **Prefer
  the Auth admin API** unless you have confirmed the pg_dump path against staging.

Whichever path you use, verify before loading public data:
```bash
psql "$SRC_DATABASE_URL" -qtAX -c "select count(*) from auth.users;"
psql "$DST_DATABASE_URL" -qtAX -c "select count(*) from auth.users;"   # must match
```

> **Signup email caveat (known):** the hosted destination has **no custom SMTP**,
> so GoTrue confirmation mail never reaches new external addresses. Recreate users
> with `email_confirm: true` (admin API) so migrated users are pre-confirmed and
> do not depend on a confirmation email that will never arrive.

### 2b. Public tables, in FK (load) order

The script loads with `--disable-triggers` (FK checks deferred during load), but
the order below is still the correct dependency order and is what reconciliation
walks. `public.user_keys` is **intentionally not copied** — the destination drops
it by design (KMS `key_repositories` supersedes it); its rows survive only in the
source backup.

**Tier 1 — depend only on `auth.users` (or standalone):**
`profiles`, `api_keys`, `provider_config`, `usage_log`, `organizations`,
`billing_events`, `legal_acceptances`, `waitlist`, `bvx_device_auth`,
`billing_accounts`

**Tier 2 — depend on Tier 1:**
`organization_members`, `customers`, `service_accounts`, `key_repositories`
(FK `api_keys.key_hash`), `organization_invitations`

**Tier 3 — depend on Tier 1/2:**
`billing_ledger` (FK `usage_log.id` **and** `auth.users`), `devices`,
`installations`, `active_company_selections`, `bvx_device_consumption_receipts`,
`data_subject_requests`, `billing_recovery_audit`,
`billing_checkout_reservations` (FK `billing_accounts`), `stripe_webhook_events`,
`audit_events`

**Tier 4 — depend on Tier 3:**
`legal_holds`, `legal_hold_actions` (FK `data_subject_requests`)

**Ephemeral / operational — NOT copied by default** (regenerate; enable only with
a reason via `--include-ephemeral`): `semantic_cache`, `ai_jobs`,
`shared_endpoint_rate_limits`, `compliance_retention_runs`,
`compliance_retention_worker_state`, `backup_deletion_tombstones`.

### 2c. Identity / sequence preservation (critical)

`usage_log.id` is `bigint generated always as identity` and `waitlist.id` is
`bigserial`. `billing_ledger.usage_log_id` is a **unique FK onto
`usage_log.id`**, so the identity values **must be preserved** across the copy or
billing rows will point at the wrong usage rows. `pg_dump --data-only` handles
this — it emits `OVERRIDING SYSTEM VALUE` inserts and a trailing
`setval(...)` so the destination sequence resumes above the copied ids. After the
load, spot-check:
```bash
psql "$DST_DATABASE_URL" -qtAX -c \
  "select max(id) from public.usage_log;"
psql "$DST_DATABASE_URL" -qtAX -c \
  "select last_value from pg_get_serial_sequence('public.usage_log','id')::regclass;"
# last_value must be >= max(id)
```
If you ran `--truncate-dst`, `RESTART IDENTITY` already reset these; the copied
`setval` then advances them correctly.

---

## 3. Conflict / idempotency handling

`pg_dump --data-only` produces plain `INSERT`s with no `ON CONFLICT` clause, so a
second load into a **non-empty** table collides on primary keys. The script's
model is therefore **"load into empty destination tables"**:

- **Preflight** counts every destination table. If any target is non-empty and you
  did **not** pass `--truncate-dst` or `--allow-nonempty`, it **aborts** and lists
  them — no half-load.
- **Re-runs:** pass `--truncate-dst`. It runs a single
  `TRUNCATE <all selected tables> RESTART IDENTITY;` (one statement so mutually
  dependent tables truncate together and sequences reset), then reloads. This is
  the idempotent path — you can run it repeatedly and converge on the same state.
- `--allow-nonempty` is an escape hatch for when the destination legitimately
  holds a **disjoint** keyspace you want to keep; expect PK collisions otherwise.

There is **no partial-row merge**. If the destination already accumulated real
production writes (it should not have — see the sequencing hazard), stop and
reconcile manually; do not blindly truncate.

---

## 4. Dry run first (always)

The script defaults to `--dry-run`: it connects **read-only**, verifies the
destination schema, prints the per-table source counts it *would* copy, and does
**not** write. Run it and read the plan:

```bash
export SRC_DATABASE_URL='...amjcc session-pooler DSN...'
export DST_DATABASE_URL='...wyfz session-pooler DSN...'

scripts/db/migrate-project-data.sh --dry-run
```

Confirm the table list, the source counts look sane, and preflight reports the
destination schema is complete.

---

## 5. Rehearse against a STAGING copy of the destination (strongly recommended)

Before touching real prod, prove the load end-to-end against a throwaway/staging
Postgres shaped like the destination (apply the migration chain to it first, per
§0), then point `DST_DATABASE_URL` at it and run a full `--execute` with
`--include-auth` if you plan to use the pg_dump auth path. Verify:

- reconciliation comes back all-`OK`,
- `usage_log` sequence ≥ `max(id)` (§2c),
- a sample `billing_ledger` row still joins to the right `usage_log` row,
- RLS: a normal (non-service) role can read its own `profiles`/`usage_log` and not
  others'.

Only after a clean staging rehearsal do you run against `wyfzmfnswtzyhwbltbpy`.

---

## 6. Execute against production destination (you)

Inside the maintenance window, with the source read-only:

```bash
# 1) auth users first (preferred: Supabase Auth admin API — see §2a),
#    OR let the script attempt the pg_dump path with --include-auth.

# 2) full data load + reconciliation. First run into a fresh destination:
scripts/db/migrate-project-data.sh --execute
#    (script prompts:  type exactly  MIGRATE amjcc TO wyfz )

# re-run / recovery variant (empties targets first, fully idempotent):
scripts/db/migrate-project-data.sh --execute --truncate-dst

# if you also drove auth via the script rather than the admin API:
scripts/db/migrate-project-data.sh --execute --truncate-dst --include-auth
```

The run finishes with a **reconciliation table** (source count vs destination
count per table). It exits non-zero if any table mismatches.

---

## 7. Reconcile, then — and only then — cut over

```bash
# standalone reconciliation any time (read-only):
scripts/db/migrate-project-data.sh --reconcile-only
```

**Gate:** every table must read `OK`. Spot-check beyond counts:

- `select count(*) from auth.users;` matches source.
- `select count(*) from public.profiles;` == `auth.users` (if 1:1 in your data).
- a known user's `api_keys`, `usage_log`, `billing_ledger` all present.

**Cutover (the hazard boundary):** only after reconciliation is green, repoint the
consumers to `wyfzmfnswtzyhwbltbpy`:

- the deployed dashboard bundle's Supabase URL + anon key
  (rebuild/redeploy — see the release + Supabase-split memory notes; read the ref
  from the live bundle to confirm it *was* pointing at `amjcc…`),
- the Railway API `SUPABASE_URL` / service-role key,
- any other env referencing the old project ref.

Keep `amjccgcgkcpbyevkjabw` **running and untouched** until the cutover is verified
in production (users can log in, keys resolve, usage/billing render). It is the
rollback target.

---

## 8. Rollback / abort path

- **During the load (before cutover):** the copy only wrote to the *destination*,
  and the *source is unchanged*. To abort, restore the destination from the Step-1
  pre-load backup (or just re-run with `--truncate-dst` once the issue is fixed):
  ```bash
  pg_restore --clean --if-exists --no-owner --no-privileges \
    -d "$DST_DATABASE_URL" scratchpad/wyfz_pre_dataload_<stamp>.dump
  ```
  Nothing about the live site changed, because the bundle still points at
  `amjcc…`. This is the whole point of migrating **before** cutover.

- **After cutover, if prod misbehaves:** revert the bundle/API env back to
  `amjccgcgkcpbyevkjabw` (redeploy the previous bundle / restore the old env
  values). Because the source was left running and read-only during the window,
  it is still authoritative and users are made whole immediately. Then diagnose
  the destination offline and retry the migration.

- **Do not** delete or decommission `amjccgcgkcpbyevkjabw` until the destination
  has served production cleanly for an agreed soak period.

---

## 9. Post-migration checklist

- [ ] `auth.users` counts match (source vs destination).
- [ ] `scripts/db/migrate-project-data.sh --reconcile-only` → all `OK`.
- [ ] `usage_log` / `waitlist` sequences ≥ `max(id)` (§2c).
- [ ] Sample business joins intact (`billing_ledger` → `usage_log`,
      `organization_members` → `organizations`).
- [ ] RLS read/write behaves for a normal user role.
- [ ] Bundle + API env repointed to `wyfzmfnswtzyhwbltbpy` (Step 7) — **not before**.
- [ ] Live login / key resolution / usage render verified in production.
- [ ] `amjccgcgkcpbyevkjabw` retained as rollback target for the soak period.
