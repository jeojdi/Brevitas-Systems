# Production function drift: wyfz vs. the repo migration chain

**Measured 2026-07-30. Read-only. Nothing was applied to wyfz.**

Method: `md5(pg_get_functiondef(oid))` for every `prokind='f'` function in schema `public`,
taken from wyfz via `supabase db query --linked -f` (SELECT only, no function was ever
executed), compared against a fresh local PostgreSQL 17.10 database built from
`scripts/ci/migration-bootstrap.sql` plus all 78 entries of
`scripts/ci/migration-fresh-manifest.txt` in manifest order. Both sides applied clean.

- wyfz reports PostgreSQL **17.6**; `public` holds **269** functions.
- Fresh local holds **309** functions, of which **36 signatures (22 distinct names) are
  pgcrypto** (`digest`, `hmac`, `pgp_*`, `crypt`, `gen_salt`, `armor`, …). On wyfz pgcrypto
  is installed into the `extensions` schema, not `public`. **This is a Supabase
  schema-placement convention, not drift**, and those 36 are excluded from every count
  below, leaving 273 local signatures.

Comparable universe: **275 signatures** (273 local + 2 that exist only on prod).
Prod reconciles as 261 + 6 + 2 = 269; local as 261 + 6 + 6 = 273.

| Verdict | Count |
|---|---|
| matches (byte-identical `pg_get_functiondef`) | **261** |
| differs | **6** |
| missing on prod | **6** |
| extra on prod | **2** (1 benign, see §5) |

Family breakdown of the paths this audit cares about:

| Family | match | differs | missing | extra |
|---|---|---|---|---|
| compliance (`compliance_*`) | **27** | 0 | 0 | 0 |
| company-admin (`company_admin_*`, `company_role_*`, `lock_company_actor_role`, `append_company_audit`) | **23** | 0 | 0 | 0 |
| device / key (`approve_bvx_device`, `get_bvx_device_exchange`, …) | **4** | 0 | 0 | 0 |
| billing / settlement | 21 | 4 | 5 | 1 |
| semantic cache | 3 | 1 | 1 | 0 |
| everything else | 183 | 1 | 0 | 1 |

**The compliance and company-administration surfaces on wyfz are byte-identical to the repo.
All drift is confined to billing/settlement, the semantic cache, and one CI-only assertion
helper.**

---

## 1. The headline: prod is the repo chain stopped at 202607280030, minus two stale bodies

A second local database was built from the same manifest **with `202607280031` removed**.
Against that database wyfz shows:

```
MATCH 265   DIFFER 3   MISSING_ON_PROD 3   EXTRA_ON_PROD 1
DIFFER : assert_browser_role_truncate_contract()
         claim_billing_ledger_entries(text, integer, integer, bigint)
         claim_billing_ledger_entry(bigint, bigint)
MISSING: attest_billing_arrangement(...)          <- 202607280030
         open_billing_arrangement_request(...)    <- 202607280030
         revoke_billing_arrangement(...)          <- 202607280030
EXTRA  : rls_auto_enable()                        <- Supabase-managed, benign
```

So the drift decomposes cleanly into exactly three causes:

1. **`202607280031` was never applied** → 6 of the 12 findings (§2.1–§2.3, §3.1–§3.3).
2. **`202607280030` was never applied** → 3 missing functions (§3.4).
3. **Two functions carry pre-freeze revisions of migrations that were later corrected in
   git** → `claim_billing_ledger_entr*` and `assert_browser_role_truncate_contract`
   (§2.4–§2.6). These are the genuinely dangerous ones, because no future migration in the
   chain will ever repair them: the files that define them are checksum-frozen and will not
   be re-run.

### The prior finding about 202607280008 / 012 / 013 is now CLOSED

The earlier audit reported that `202607280008`, `202607280012` and `202607280013` passed a
presence probe while wyfz carried older definitions, and that
`billing_period_settlement_evidence` looked hand-recreated. That was true then and is **no
longer true for the bodies**:

- `prevent_period_settlement_identity_change()` on wyfz = `fc6a55f1f773925355d3274f55f6a7c0`
- `billing_period_settlement_evidence(uuid, timestamptz, timestamptz)` on wyfz =
  `ddcb0f6d601e7a29370da6920e63e24e`

Those are precisely the two md5s that `scripts/db/wyfz_function_realignment_20260730.sql`
declares as its targets in its header. **That realignment file was applied and it worked.**
`settle_billing_period`, `billing_period_settlement_summary`,
`assert_billing_period_settlement_allowed` and `promote_billing_period_settlement` on wyfz
are now byte-identical to the repo's `202607280013`/`202607280008` end-state.

Two consequences that matter:

- The realignment file is now **stale**: `202607280031` supersedes both bodies it pins, and
  its "canonical md5" comments will be wrong the moment 280031 ships. It must not be
  re-applied after 280031 without being rewritten.
- The **ACL half of that hand-recreation was never repaired.**
  `billing_period_settlement_evidence` on wyfz carries
  `=X/postgres,postgres=X/postgres,anon=X/postgres,authenticated=X/postgres,service_role=X/postgres`,
  i.e. the Supabase default-privileges ACL, while the same function locally carries only
  `owner + service_role`. Same for `claim_billing_ledger_entries`,
  `claim_billing_ledger_entry`, `semantic_cache_lookup` and `semantic_cache_store_bounded`.
  Every function on wyfz that is byte-identical to the repo kept its `REVOKE`; every
  drifted or hand-recreated one lost it. That is the same hole
  `supabase/migrations/202607280032_browser_function_execute_contract.sql` (authored by the
  parallel remediation lane) exists to close, and it is closed there, not here.

---

## 2. The six functions whose bodies DIFFER

### 2.1 `settle_billing_period(uuid, timestamptz, text, boolean)` — the money writer
`prod 51a4216ad07678df2dc7beacb02813c3` vs `repo 37d889dfaac37fa4d64db3033594209a`

wyfz derives the fee from `v_evidence.net_verified_savings_usd` (gross verified savings);
the repo derives it from `v_evidence.billable_savings_basis_usd` and passes the *gross* fee
to the halting gate while writing the *basis* fee to the ledger.

**Dangerous — but in the over-billing direction it is currently masked.** On wyfz the basis
narrowing does not exist at all, so the function bills on gross savings. In practice the
`zero_spend_concentration` halting condition (202607280008) halts exactly the
cache-replay-only shape this affects, which is why 280031's header records that the
product's headline mechanism is "structurally unbillable" today. Net effect on wyfz: it
does not over-bill, it refuses to bill. This matches the standing observation that no
authoritative billable usage has been produced since 2026-07-17.

### 2.2 `billing_period_settlement_summary(uuid, timestamptz)` — the customer-facing number
`prod 2e5bdde39bb5a0fb78f7455fe9c0c0b6` vs `repo 21a2db4974c58d95cb0ef7a3b573fd83`

wyfz computes the displayed ceiling as `v_gate->>'fee_ceiling_microusd'` only; the repo
computes `least(gross_ceiling, basis_ceiling)` and additionally returns
`billable_savings_basis_usd`, `anchored_zero_spend_savings_usd`,
`unanchored_zero_spend_savings_usd`, `cache_savings_spend_ratio`.

**Not dangerous, but it is a consistency bug the moment 280031 ships.** Today wyfz's summary
agrees with wyfz's writer (both gross). If 280031 is applied to the writer without the
summary, the dashboard would quote a ceiling the ledger's own CHECK constraint would reject.
They must move together — they are in the same file, so applying 280031 whole is sufficient.

### 2.3 `semantic_cache_lookup(vector, text, float8, text, text)` — not SECURITY DEFINER
`prod f462234e04152373664e6fcc8f2446fb` vs `repo cb2e9b34e34fe86e8476a619ec75d0b2`

wyfz's `RETURNS TABLE` omits the trailing `origin_request_id text` column the repo adds.

**Not a security difference; it is a hard compatibility fence.** Any proxy build that
selects `origin_request_id` from this function will fail on wyfz today, and conversely
applying 280031 changes the OUT list, so this function cannot be `CREATE OR REPLACE`d —
it must be dropped and recreated in the same transaction (280031 already does this; the
same 42P13 problem the 20260730 realignment file documents for the evidence function).

### 2.4 `claim_billing_ledger_entries(text, integer, integer, bigint)` — **STALE BODY, real defect**
`prod e8890fa74159d5695cd30d4e2a9fb574` vs `repo 9d56e908f57965382b463197db93ee6d`

The only difference is the `committed` predicate:

```sql
-- wyfz (stale)
and ledger.status in ('sending','reported','review')
-- repo (canonical)
and (ledger.status in ('sending','reported')
     or (ledger.status='review' and ledger.outbound_started_at is not null))
```

**Dangerous.** A `review` row with `outbound_started_at IS NULL` was provably never sent to
Stripe. wyfz counts it toward both the weekly cap and `expected_period_microusd`. Two
consequences, both live: legitimate fees get stamped `status='capped'` against money Stripe
never received (revenue lost, silently), and `billing_recovery`'s reconcile path compares an
expected total that is permanently larger than the actual Stripe total, so reconciliation
can never converge for any organisation that has ever produced an unsent `review` row.

The canonical narrowing landed in `202607200006_company_billing_authorization.sql` in commit
`fa9a2a0` ("audit fixes 2026-07-27"). wyfz is running the pre-`fa9a2a0` revision of that
file. **The file is checksum-frozen** (`scripts/ci/migration-frozen-checksums.txt:24`), so
nothing in the forward chain will ever re-run it and repair this.

### 2.5 `claim_billing_ledger_entry(bigint, bigint)` — **STALE BODY, same defect**
`prod 47a421a543bf614371d63970900369ef` vs `repo fb15630b22a7619c92743fe500e0f2ff`

Identical predicate difference, identical cause (`fa9a2a0`), identical danger, on the
single-entry claim path. Fix the two together or the cap disagrees with itself.

### 2.6 `assert_browser_role_truncate_contract()` — CI-only helper, benign on wyfz
`prod 4c3c25d5319e87d9b89f4fada3229319` vs `repo 6efa6acf97825c3a79b1493c0b8f7394`

wyfz hard-codes `array['TRUNCATE','TRIGGER','REFERENCES','MAINTAIN']`; the repo builds that
list from `current_setting('server_version_num')` so the assertion also runs on PG16, where
`has_table_privilege(..., 'MAINTAIN')` raises rather than returning false. Landed in commit
`2897ea3`; wyfz has the pre-`2897ea3` revision of `202607280027`, which is likewise
checksum-frozen (`migration-frozen-checksums.txt:68`).

**Not dangerous on wyfz.** wyfz is PostgreSQL 17.6, so `MAINTAIN` exists and both bodies
evaluate identically. It is a portability fix only. Its ACL on wyfz is already correct
(`postgres=X/postgres,service_role=X/postgres`). Realign for hygiene, not urgency.

---

## 3. Missing on prod (6)

| Function | Defined by | Why it is absent | Danger |
|---|---|---|---|
| `billing_period_settlement_evidence(uuid, timestamptz, timestamptz, bigint)` | 202607280031 | 280031 not applied | The 4-arg form adds `billable_savings_basis_usd`, `anchored_zero_spend_*`, `cache_savings_spend_ratio` and the watermark **input** that makes a settled fee reproducible. Its absence is what forces §2.1. |
| `billing_observed_model_price(uuid, timestamptz, timestamptz, integer, bigint)` | 202607280031 | 280031 not applied | Supporting function for the above. Absent alone it is inert. |
| `semantic_cache_store_bounded(..., p_origin_request_id text)` (11-arg) | 202607280031 | 280031 not applied | wyfz has only the 10-arg overload. Any writer passing 11 args gets `42883`. The local chain carries **both** overloads (280031 adds, does not drop) — see §6. |
| `attest_billing_arrangement(uuid, text, text, text, uuid)` | 202607280030 | 280030 not applied | The enterprise-arrangement attestation writer. Absent, no arrangement can be attested at all. |
| `open_billing_arrangement_request(uuid, uuid, text, text, text, text, text)` | 202607280030 | 280030 not applied | Same surface. |
| `revoke_billing_arrangement(uuid, text)` | 202607280030 | 280030 not applied | Same surface. |

`202607280030` also creates the `brevitas_attestor` role, the
`organization_billing_arrangement`, `organization_billing_arrangement_log` and
`billing_arrangement_request` tables, and grants EXECUTE on the three functions to
`brevitas_attestor` **only** — explicitly not to `service_role`. Applying it is therefore
not a pure function change; it introduces a login role that needs a password managed out of
band. Treat it as a deployment, not a realignment.

---

## 4. Extra on prod (2)

| Function | Verdict |
|---|---|
| `billing_period_settlement_evidence(uuid, timestamptz, timestamptz)` (3-arg) | **Expected.** This is the canonical pre-280031 form, restored by `scripts/db/wyfz_function_realignment_20260730.sql`. It stops being "extra" the moment 280031 replaces it. Its ACL is wrong (§1). |
| `rls_auto_enable()` | **Benign, do not remove.** A Supabase-managed event-trigger function that plain-PostgreSQL fixtures never install, which is exactly why `202607220002_supabase_advisor_hardening.sql:18` guards it behind `to_regprocedure(...) is not null`. Its wyfz ACL is `postgres=X/postgres` — already locked down by that migration. Its absence locally is expected, not a gap. |

---

## 5. Remediation

Three independent workstreams. They are ordered by risk, not by migration number.

### Step 1 — the stale bodies (do this first; nothing else repairs them)

`claim_billing_ledger_entries`, `claim_billing_ledger_entry`, and
`assert_browser_role_truncate_contract` come from **checksum-frozen** files
(`202607200006`, `202607280027`). Re-running those files wholesale against wyfz is not an
option: `202607200006` is a 400+ line migration that rebuilds tables and grants, and
`202607280027` runs a privilege sweep. Neither is idempotent against a live database in the
way a body replacement is.

**Vehicle: a new realignment script, `scripts/db/wyfz_function_realignment_20260731.sql`,
following the precedent of `wyfz_function_realignment_20260730.sql`.** It should contain,
in one transaction:

1. `CREATE OR REPLACE FUNCTION` for the three bodies, taken verbatim from
   `pg_get_functiondef()` on a local database that applied the full canonical manifest —
   not retyped from the migration source, so the resulting md5 is provably the canonical one.
   All three keep their existing argument lists, so `CREATE OR REPLACE` is legal; no
   drop/recreate is needed.
2. The matching `revoke all on function ... from public, anon, authenticated;` plus
   `grant execute ... to service_role;` for the two claim functions, whose wyfz ACLs
   currently carry the Supabase default `anon=X`.
3. A trailing verification `SELECT` asserting the three md5s, exactly as the 20260730 file
   does. Target values, measured today:
   - `claim_billing_ledger_entries` → `9d56e908f57965382b463197db93ee6d`
   - `claim_billing_ledger_entry` → `fb15630b22a7619c92743fe500e0f2ff`
   - `assert_browser_role_truncate_contract` → `6efa6acf97825c3a79b1493c0b8f7394`

This step is body-only and ACL-only. It changes no table, no constraint, no trigger.

### Step 2 — the browser-role EXECUTE contract

`supabase/migrations/202607280032_browser_function_execute_contract.sql` (authored by the
parallel remediation lane on this branch) is the correct and only vehicle for the
`anon`/`authenticated` EXECUTE grants described in §1. Apply it **after** Step 1, so that
the two claim functions are realigned before their privileges are frozen, and so that the
blanket revoke also covers the functions Step 1 replaces. Do not fold ACL work for the 99
exposed functions into the realignment script — that migration must live in the chain so a
fresh database gets it too.

### Step 3 — the unapplied migrations, in chain order

3a. **`202607280030_billing_attestation_writer.sql`** — apply as a normal migration. It is
    additive (new tables, new role, three new functions) and creates nothing that collides
    with what is on wyfz. Requires provisioning the `brevitas_attestor` role password out of
    band before or during application.

3b. **`202607280031_observed_price_cache_fee_basis.sql` — DO NOT APPLY YET.** It is the
    current on-disk tip and is awaiting its own adversarial review; its header records that
    defect 4 (dilution) is explicitly **NOT CLOSED**. Applying it would change the fee basis
    for every settlement, drop and recreate `billing_period_settlement_evidence` and
    `semantic_cache_lookup` (OUT-list changes, so `CREATE OR REPLACE` would raise 42P13),
    and add a second `semantic_cache_store_bounded` overload. Nothing in §2.1–§2.3 is
    urgent — wyfz's current gross-basis behaviour under-bills rather than over-bills — so
    this can and should wait for the review verdict.

    When it does ship, `scripts/db/wyfz_function_realignment_20260730.sql` becomes obsolete
    and must not be re-run: its pinned md5s (`fc6a55f1…`, `ddcb0f6d…`) describe the state
    280031 replaces.

### What NOT to do

- **Never replay the migration chain against wyfz.** wyfz has no migration ledger; the
  chain has already been hand-applied out of order once (that is why the 20260730
  realignment file exists), and `202607280010`/`280012`'s embedded self-test probes insert a
  bare `auth.users` row that the live `202607280021` legal-acceptance trigger rejects.
- **Never edit `202607200006` or `202607280027`** to "fix" the stale bodies. They are
  checksum-frozen, their current content is correct, and a fresh database already produces
  the right result from them. The defect is in wyfz, not in the files.
- **Do not remove `rls_auto_enable()`.**

---

## 6. Loose end worth flagging

The canonical local chain ends with **two** live overloads of
`semantic_cache_store_bounded` — the 10-arg (`39cffad3dfecf002ea1710ad5c7e63bf`, from
`202607170002`) and the 11-arg (`2fe228565cb0d3c5bfb4ad89c71c8e62`, from `202607280031`).
`202607280031` adds the new signature without dropping the old one. wyfz has only the
10-arg form, so applying 280031 will leave production in the same two-overload state.
A caller passing 10 positional arguments will continue to resolve to the old, non-anchoring
writer. This is a repo property, not drift, but it is the kind of ambiguity that produces
the next drift report and should be resolved deliberately in 280031's review.
