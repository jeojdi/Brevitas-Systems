# Cross-Session State — Reconciled Record

**Reconciled 2026-07-31 02:20 UTC** (2026-07-30 19:20 PDT).
Repo: `Brevitas-Systems`, branch `security/enterprise-audit-2026-07-30`, HEAD `7c0b2b2`.
Production DB: Supabase project `wyfzmfnswtzyhwbltbpy` ("wyfz").

This document exists because two Claude sessions worked the same tree and the same production
database today and published contradictory summaries. It is built from three independent audits
(production / branch / mail), each of which measured systems directly rather than reading either
session's account. Every claim is tagged **[measured]** (someone ran the command and read the
output) or **[unverified]** (inference, or a fact that could not be read directly).

**A third session was still editing this tree while this was written.** The working tree changed
materially between 02:03 UTC and 02:20 UTC — see §1.2. Treat every repo fact below as true at
02:20 UTC and re-check before acting.

**Nothing in this audit wrote to production.** All DB access was `SELECT` via
`supabase db query --linked -f`. No migration applied, no `db push`, no git write. One exception,
disclosed: the mail audit issued a single `POST /auth/v1/recover` for the owner's own address,
which sends one email and mutates only GoTrue's `recovery_sent_at`.

---

## 1. WHAT IS ACTUALLY TRUE

### 1.1 Production schema state (wyfz)

wyfz has **no migration ledger** [measured, and consistent with the standing
`production-schema-drift` memory]. Applied-vs-absent below was therefore inferred by probing for
objects unique to each migration, and by comparing `md5(pg_get_functiondef())` against a reference
database (`brevitas_freshaudit`) built locally on postgresql@17 from
`scripts/ci/migration-bootstrap.sql` + all 77 entries of `scripts/ci/migration-fresh-manifest.txt`
(exit 0, BUILD_OK). The bootstrap replicates Supabase's default-privilege grants, so ACL
comparisons are apples-to-apples, not a platform artifact.

| Migration | State on wyfz | Evidence |
|---|---|---|
| 202607280005 – 202607280027 (24 files) | **APPLIED** | [measured] object probe, 26-row union query, all true |
| 202607280029 (period settlement claim path) | **APPLIED** | [measured] all 7 RPCs present, bodies md5-identical to reference, correct ACLs |
| 202607280030 (attestation writer) | **ABSENT** | [measured] 2 tables + 3 functions all missing |
| 202607280031 (observed price / cache fee basis) | **ABSENT** | [measured] its indexes, function and `usage_log` columns all missing |

**202607280029 is genuinely applied. The session that claimed it was right; the session that
flagged the claim as unverified was wrong.** [measured] Verified more strictly than a
postcondition: all seven RPCs (`claim_period_settlement_entries`,
`mark_period_settlement_outbound_started`, `renew_period_settlement_lease`,
`complete_period_settlement_entry`, `release_period_settlement_leases`,
`release_period_settlement_claim`, `billing_period_settlement_history`) exist, all seven bodies
hash identically to the repo-built reference, and all seven are SECURITY DEFINER with EXECUTE
denied to `anon` and `authenticated` and granted to `service_role`.

**202607280030 is committed but NOT live.** [measured] The HEAD commit message advertises an
"attestation surface"; `billing_arrangement_request`, `organization_billing_arrangement_log`,
`open_billing_arrangement_request`, `attest_billing_arrangement` and `revoke_billing_arrangement`
do not exist in production. Any code or doc on this branch that assumes attestation is reachable
in production is wrong today.

#### The important correction: "applied" ≠ "matches the repo"

Both sessions treated presence of a migration's objects as proof production matches the repo. It
is not. Five confirmed hand-drifts, none of which a presence probe would catch:

1. **99 of 149 SECURITY DEFINER functions in `public` are EXECUTE-able by `anon`.** [measured]
   The repo-built reference has 0 of 149. `anon` has USAGE on `public` and is **not** a member of
   `postgres` (`pg_has_role` = false), so these are direct grants, not inheritance. 28 of them are
   billing/usage functions, including `claim_billing_ledger_entries`, `claim_billing_ledger_entry`,
   `complete_billing_ledger_entry`, `mark_billing_outbound_started`,
   `manually_resolve_billing_ledger_entry`, `release_billing_ledger_leases`,
   `renew_billing_ledger_lease`, `save_billing_customer_identity`, `admin_usage_report`,
   `admin_usage_report_page`, `usage_page`. The repo explicitly revokes these
   (`202607170004:511`, `202607200006:435`, `202607280008:367`, `202607280012:159`,
   `202607280013:312`) and the revokes work when the chain is applied in order — so this is
   production drift, not an unclosed gap.
2. **`billing_period_settlement_evidence` was hand-recreated after 202607280013 ran.** [measured]
   Body md5 matches the repo; ACL is the Supabase default (`anon=X | authenticated=X`) and the
   COMMENT is missing both the 280012 and 280013 markers. In 280013 the CREATE is at line 236, the
   revoke at 312, grant at 314, comment at 318, `settle_billing_period` at 333 — and
   `settle_billing_period`'s prod body *does* match. A sequential apply cannot produce "CREATE ran,
   line 333 ran, lines 312–318 did not". A hand `DROP`+`CREATE` produces exactly that. [unverified:
   who did it, and when.]
3. **Production runs older bodies of two weekly-cap functions.** [measured] md5 mismatch on
   `claim_billing_ledger_entry(bigint,bigint)` and
   `claim_billing_ledger_entries(text,integer,integer,bigint)`. Prod counts the committed-fee cap
   as `status in ('sending','reported','review')`; the repo counts
   `status in ('sending','reported') or (status='review' and outbound_started_at is not null)`. The
   repo's own comment: counting a never-sent `review` row "poisons reconciliation: expected >
   actual forever."
4. **At least two applied migrations were edited in place afterward.** [measured] `202607170004`
   and `202607200006` edited 2026-07-27 (`e20d02a`, `fa9a2a0`); `202607280027` edited twice on
   2026-07-30 (`a2d0ea7`, `2897ea3`) after it was applied. All four files match their pins in
   `scripts/ci/migration-frozen-checksums.txt` **today**, so the checksum gate cannot see this.
5. **`public.user_keys` exists in production with 15 rows and a raw `api_key text` column.**
   [measured] `supabase/migrations/202607170001_enterprise_tenancy.sql:220` does
   `drop table if exists public.user_keys;` and the table is absent from the repo-built reference.
   RLS is enabled but **not forced**, one policy (`user_keys_owner`), and grants are
   `anon=arwd | authenticated=arwd` — browser roles hold SELECT/INSERT/UPDATE/DELETE.

Lower-severity prod-only objects [measured]: `rls_auto_enable()` (present but correctly locked
down — `anon` EXECUTE false, so 202607220002's hardening worked); `usage_log.cached_tokens` and
index `usage_log_key_hash_idx` (in no migration); `waitlist.source` / `waitlist.use_case` (only in
the loose non-migration `supabase/add_waitlist_fields.sql`). `pgcrypto`/`gen_random_uuid` live in
schema `extensions` on Supabase — expected platform difference, not drift.

#### Billing data state

| Metric | Value | |
|---|---|---|
| `usage_log` rows | 43,710 | [measured] |
| …`authoritative = true` | **0** | [measured] |
| …`authoritative = false` | 43,710 | [measured] |
| …`authoritative IS NULL` | 0 | [measured] |
| max `ts` overall | 2026-07-31 02:06:31 UTC | [measured] traffic flowing now |
| max `ts` where `verified_savings_usd > 0` | 2026-07-17 03:19:25 UTC | [measured] |
| rows with savings > 0 / fee > 0 | 1,897 / same 1,897 | [measured] |
| sum verified savings / fee | 163.3304243 USD / 40.8326060750 USD (exactly 25.0%) | [measured] |
| `billing_ledger` rows | **0** | [measured] |
| `period_settlement_ledger` rows | **0** | [measured] |
| `organization_billing_arrangement` rows | **0** | [measured] |
| `organizations` rows | 5 | [measured] |
| `billing_halting_conditions` | 1 row, fee share 0.25, zero-spend share 0.50, `updated_at` **2026-07-30 08:50:29 UTC** | [measured] |

Correction to the `no-billable-usage-since-jul-17` memory: it says "every row *since* is
authoritative=false." [measured] It is **every row, period** — including the 1,897 hand-repriced
ones. Zero authoritative rows exist.

**`BREVITAS_BILLING_ENABLED` cannot be read from the database** [measured] — it is a process env
var only (`tests/billing_launch_gate_parity.test.mjs:75-77`, `deploy/cloud-run-api-staging.yaml:76`,
`deploy/cloud-run-worker-staging.yaml:59`, default false in `.env.example:41`). What the DB does
prove: no money has moved and none could today — both ledgers empty, per-row fee trigger retired
(280006 applied), 0 attested arrangements, 0 authoritative rows.
`settle_billing_period`'s evidence CTE filters on `usage.authoritative and pricing_status='priced'`,
so every settlement computes an empty period. **Billing is inert by data, not only by flag.**

### 1.2 Branch state — and it changed while we audited

The branch audit snapshotted at **02:03:49 UTC**. I re-checked at **02:20:07 UTC**. The tree is
different. Both readings are correct for their instant; this is the clearest single illustration of
the coordination problem in §4.

**At 02:03 UTC** [measured]: 3 modified files (`api/store.py`, `tests/test_cloud_usage_api.py`,
`tests/test_proxy_cache.py`), 2 untracked (`202607280031_...sql`,
`scripts/ci/migration-anchored-savings-v2-assertions.sql`). 280031 was registered in **zero** of
the four registration surfaces. Local `verify-migrations.mjs` exited **1**; a fresh clone of the
same commit exited **0**. `npm test`: local 324/322 pass/**2 fail**; fresh clone 324/322/**0 fail**.
`pytest`: local 1110 passed; fresh clone 1104 passed; both exit 0.

**At 02:20 UTC** [measured] a concurrent session has registered 280031 in all four surfaces and
bumped the tripwire:

```
 M scripts/ci/migration-fresh-manifest.txt          (+1: 280031 at line 81)
 M scripts/ci/migration-upgrade-manifest.txt        (+1: 280031 at line 69)
 M scripts/ci/migration-frozen-checksums.txt        (+1: 2ce9da13… 280031 at line 71)
 M scripts/ci/verify-migrations.mjs                 (280031 in expectedFreshMigrationOrder:144;
                                                     REVERSE_POSTURE_CUTOFF 280031 -> 280032 :922)
 M scripts/ci/run-migration-tests.sh, 3 assertion .sql files
?? supabase/migrations/202607280031_observed_price_cache_fee_basis.sql   <-- STILL UNTRACKED
```

The pinned checksum matches the file on disk right now (`2ce9da13…`, 107,475 bytes, mtime
19:11:49 PDT) [measured]. The registration work is coherent. **The migration it registers is still
not in git.** This inverts the earlier finding: a fresh clone was green at 02:03 and would now go
**red** if the tracked changes are committed without `git add`-ing 280031 — the manifests would
reference a file that does not exist.

Settled facts about the 280028/280030 dispute [measured]:

- `202607280030_billing_attestation_writer.sql` **is committed**, in `supabase/migrations/`, and
  registered in all four surfaces (fresh-manifest:80, upgrade-manifest:68, frozen-checksums:70,
  verify-migrations.mjs:120). It was never at risk of being missed by a merge.
- `202607280028_anchored_zero_spend_fee_basis.sql` **is committed**, at
  `docs/quarantine/202607280028_anchored_zero_spend_fee_basis.sql`, added by the tip commit
  `7c0b2b2` itself and absent from `origin/main`. It is cleanly **deregistered** (commit `1224818`
  removed it from both manifests, frozen-checksums and `expectedFreshMigrationOrder`; the only
  residue is an explanatory comment at `run-migration-tests.sh:347`).
- **The session that said "280028 and 280030 are untracked" was wrong at HEAD — but it was probably
  right when it looked.** 280028 genuinely was untracked for most of 2026-07-30 and became tracked
  only at the tip commit. This is a timestamp problem, not a dishonest report. The description
  nonetheless describes a real file: **202607280031**.
- Stale text, harmless but note it: commit `1224818`'s own message says 280028 "stays untracked
  pending the owner's decision." Superseded by `7c0b2b2`.

Merge readiness [measured at 02:03 UTC]: 25 commits ahead / 3 behind `origin/main`; `git cherry`
shows 2 (`1e29946`, `65348d1`) already upstream by patch-id, so **23 genuinely new**. Merge base
`c9d8e35`. `git merge-tree` → exit 1, **20 conflicted files**, including **all four
migration-registration surfaces** plus `run-migration-tests.sh`, `api/store.py`, `api/server.py`,
`brevitas/proxy.py`, `src/app/api/billing/status/route.ts`. Conflict volume traces to `origin/main`
taking PR #39 (`a459289`, 210 files).

Registration integrity is otherwise sound [measured]: 77 tracked migrations, fresh-manifest 77,
upgrade-manifest 65, frozen-checksums 65. The 12-file gap is exactly the 12 legacy pre-serial
migrations (`20260611_create_user_keys.sql` … `20260716_stripe_billing_rate_25pct.sql`),
deliberately outside the frozen baseline and accepted by `verify-migrations`. A 159-path
dangling-reference scan against a fresh clone found **zero** genuine misses.

### 1.3 Mail state

**Neither session was wrong on mail.** The corrections needed here are to the repo's own docs and
to one causal inference in memory.

- **The Supabase → ionos path is alive and delivers end-to-end.** [measured] One
  `POST /auth/v1/recover` for the owner's address returned HTTP 200 in 1.01s at
  2026-07-31T02:04:33Z. Three independent corroborations: `auth.users.recovery_sent_at` reads
  `2026-07-31 02:04:33.87667+00` — a byte-level match, and GoTrue stamps it only after the mailer
  succeeds; the message physically arrived in the Gmail **inbox** at 02:04:34Z from
  `info@brevitassystems.com`, subject "Reset your password"; the user row is real and pre-existing
  (created 2026-06-25). The prior session's 00:49:06Z probe is visible in the same inbox — it was
  real and reproducible.
- **The Gmail "Send mail as" alias is genuinely dead.** [measured] Two DSNs from
  `mailer-daemon@googlemail.com` at 2026-07-30T17:20:56Z and 17:21:59Z for sends from
  `james@brevitassystems.com`: `Remote-MTA: dns; smtp.ionos.com (74.208.5.2)`,
  `Diagnostic-Code: smtp; 535 Authentication credentials invalid`, `Status: 5.7.8`, plus Google's
  "The settings for your Send mail as account are misconfigured or out of date." No successful send
  from `james@` in the last 24h.
- **These are two independent credentials, not one on borrowed time.** [measured] Supabase
  authenticates as `info@brevitassystems.com`; the Gmail alias sends as
  `james@brevitassystems.com`. Under ionos those are distinct mailboxes with distinct passwords,
  and the timeline proves independence: `james@` was already failing 535 at 17:20Z while `info@`
  delivered successfully ~9h later at 02:04Z. The `ionos-smtp-two-credentials` memory's *advice*
  (re-probe #1) is good; its stated *causal mechanism* (shared fate) is not established.
- **The dead alias is an inbox chore, not a product bug.** [measured] There is no mailer in this
  codebase at all — zero hits for smtplib/nodemailer/@sendgrid/mailgun/postmark/ses outside archive
  and unrelated text. All product mail leaves via Supabase Auth from exactly four call sites:
  `dashboard/src/components/Auth.jsx:108` (signUp), `:137` (resetPasswordForEmail),
  `dashboard/src/lib/supabase.js:249` (auth.resend),
  `dashboard/src/lib/email-verification.js:136` (signInWithOtp). All four use the `info@`
  credential. `james@` appears only as an inbound `mailto:` target in `public/terms.html` (8×),
  `public/privacy.html` (4×), `public/pricing.html:299`, `public/404.html:109`,
  `public/orchestration.html:492`, `public/components.jsx:954-955` — and **inbound works**
  (~201 threads delivered, most recent 2026-07-31T01:23Z), so the legal-contact obligations still
  function. Only replying by hand from that address is broken.
- **Confirm-email is ON.** [measured] Live `/auth/v1/settings`: `mailer_autoconfirm: false`,
  `disable_signup: false`, `external.email: true`. Mail is on the critical signup path; there is no
  degraded mode. `deferred-email-verification` remains dead in production.
- **The credential is in no repo file, by design.** [measured] `.env.local` (92 vars) and
  `.env.example` contain no SMTP/ionos/mailer variable; `scripts/` has zero hits.
  `supabase/config.toml:108-242` has SMTP blocks but they are **local-dev only and fully commented
  out** — do not mistake them for production config. Documented home: hosted Supabase dashboard →
  Authentication → Emails → SMTP Settings (`SUPABASE_SETUP.md:51`,
  `supabase/templates/README.md:45`, `docs/ONBOARDING.md:87`).
- **Four repo docs assert the opposite of measured reality and should not be trusted.** [measured]
  `SUPABASE_SETUP.md:48-51`, `docs/ONBOARDING.md:87`, `supabase/templates/README.md:45-49`, and
  most harmfully `docs/DATA_MIGRATION_amjcc_to_wyfz.md:117`, which calls the no-SMTP state a "known
  caveat" and prescribes recreating users with `email_confirm: true` to work around a problem that
  no longer exists.
- **DNS**: MX = `mx00.ionos.com` / `mx01.ionos.com` (10). SPF =
  `v=spf1 include:mailgun.org include:_spf-us.ionos.com ~all` [measured]. Nothing in the repo or in
  the live path uses Mailgun.

---

## 2. WHAT IS AT RISK — ranked by consequence

### R1 — CRITICAL: browser roles can execute the billing money path in production
[measured] 99/149 SECURITY DEFINER functions in `public` are EXECUTE-able by `anon` (reference: 0).
The `anon` JWT ships in the public dashboard bundle and PostgREST exposes public-schema functions
at `/rest/v1/rpc/<name>`. SECURITY DEFINER means they run as the owner and **bypass RLS**. Reachable
set includes the billing writers and the cross-tenant readers `admin_usage_report`,
`admin_usage_report_page`, `usage_page`. [unverified] HTTP reachability was deliberately not tested
— that would be an attack. Treat exploitability as unconfirmed and the grant itself as measured
fact. Currently the blast radius is limited because both ledgers are empty and no row is
authoritative — but that is luck, not a control.

### R2 — CRITICAL: `public.user_keys` holds 15 raw API keys, granted `arwd` to `anon`
[measured] RLS enabled but not forced, one policy. The repo dropped this table in 202607170001, so
no future migration, no privilege contract (202607280015/024/027 assert privileges only for tables
the repo knows about), and no compliance routine (`compliance_delete_tenant`,
`compliance_export_tenant`, `purge_warm_state`) will ever cover it. Credentials in a table the
codebase believes does not exist. [unverified] whether the 15 keys are live or legacy.

### R3 — HIGH: 280031 is registered-but-untracked *right now*
[measured at 02:20 UTC] All four registration surfaces are modified to include 280031; the file
itself is `??`. Two ways this breaks, and the tree is currently sitting between them:
- Commit the tracked changes without `git add supabase/migrations/202607280031_*.sql` → the
  manifests point at a nonexistent file, CI goes red in a fresh clone, and the money-path rewrite
  (settle_billing_period, billing_period_settlement_evidence, billing_observed_model_price,
  billing_period_settlement_summary, new `usage_log` columns) silently does not travel — together
  with `api/store.py`'s uncommitted `receipt_source != 'proxy'` anchor guard and its 292 lines of
  new tests, which depend on that schema.
- Commit 280031 without the registration edits → `verify-migrations` fails the same way 280028
  did in `1224818`.
Either half alone is broken. They must land in **one** commit. `scripts/ci/migration-anchored-savings-v2-assertions.sql`
is also untracked and referenced by nothing committed [measured: zero tracked hits for
"anchored-savings-v2"] — decide whether it is 280031's partner or an orphan before committing.

### R4 — HIGH: production runs older bodies of the weekly-cap functions than the repo
[measured] Prod counts every `review` row toward the cap; the repo counts only `review` rows with
`outbound_started_at` set. Per the repo's own comment this makes `expected_period_microusd` exceed
actual forever and lets `reconcile()` falsely ACCEPT an unsent entry. **Latent today** because
`billing_ledger` is empty — live the moment the per-row path is used again.

### R5 — HIGH: the merge itself
[measured] 20 conflicts, four of them in the files whose entire purpose is to be authoritative
about migrations. Conflict resolution there is not reviewable by eye. `verify-migrations.mjs` must
be re-run **on the merge result**, never on either parent. The branch is also 3 behind with 2 of
its own commits already upstream by patch-id, so a merge (vs. rebase) will replay duplicated
auth/analytics changes into the conflict set — likely the source of the `api/server.py` and
`brevitas/config.py` conflicts.

### R6 — HIGH: the forward-only checksum gate cannot detect prod/repo divergence
[measured] `202607170004`, `202607200006` and `202607280027` were edited after being applied to
production, and all match their frozen pins today. Because wyfz has no ledger, the only way to see
this is hashing `pg_get_functiondef()` against a repo-built database. **Any future claim of the
form "the chain is applied, therefore prod matches the repo" is unsound on this project.**

### R7 — HIGH: a total mail outage would present as user error
[measured] `dashboard/src/lib/supabase.js:41-63` collapses every GoTrue "Error sending * email" 500
into "We couldn't send to that address. Check that the email is spelled correctly and try again."
That is correct for the common typo case and its comment says so deliberately. Under a credential
rotation, every user on every correct address is told their own email is misspelled. Confirm-email
is ON, so signup, password reset, resend, and the verification magic link all die at once, silently.
Compounding: `release-handoff-2026-07-24` lists three pending secret rotations (Stripe `sk_live`,
GCP SA key, Redis Cloud password) — if ionos gets swept into that cleanup, this fires. Also
`dashboard/**` is eslint-ignored (`dashboard-code-is-unlinted`), so no static check will flag it,
and PostHog history is only ~3 days deep, so `signup_failed / emailDeliveryFailed` has no baseline
to alert against.

### R8 — MEDIUM: something wrote to billing config on production today
[measured] `billing_halting_conditions.updated_at = 2026-07-30 08:50:29 UTC`. Someone or something
wrote billing configuration to wyfz while two sessions held contradictory models of that database.
[unverified] who. Establish this before anything else touches wyfz.

### R9 — MEDIUM: 280030 is committed and registered but not applied
Prod and repo disagree about whether the attestation surface exists. **Applying it is not a safe
mechanical next step**: the file carries an apply-time `DO` block
(`run-migration-tests.sh:336` flags this specifically for hosted projects),
`organization_billing_arrangement` has 0 rows, and per `production-schema-drift` the chain must
never be replayed blind against wyfz.

### R10 — MEDIUM: compliance and doc misstatements
[measured] `docs/compliance/SUBPROCESSORS_DRAFT.md:21` lists Mailgun as "In production" for
transactional email with no supporting evidence anywhere (no code, config, or env var; the only
traces are the SPF include and a Mailgun marketing email). A subprocessor register is a legal
representation; overstating is as wrong as understating. Separately, the four stale SMTP docs in
§1.3 will walk the next responder straight back into the "no SMTP" diagnosis that was already
disproven on 2026-07-29.

### R11 — LOW/MEDIUM: SPF authorizes an unused sender and softfails
[measured] `include:mailgun.org` with nothing sending through it, terminating in `~all`. Widens the
spoofing surface for a domain that appears in legal notices.

### R12 — LOW/INFORMATIONAL: billing is inert by data
[measured] `usage_log` is accumulating with `authoritative=false` on every row, so the settlement
evidence query returns empty for every organization. Flipping `BREVITAS_BILLING_ENABLED` alone
would bill nobody. Conversely, **any change that starts marking rows authoritative immediately
makes R1 and R4 materially dangerous.** Sequence accordingly.

---

## 3. WHAT NEEDS A HUMAN

Ordered. Items 1–3 are the ones no agent should do unattended.

1. **Revoke `anon` EXECUTE on production's SECURITY DEFINER functions (R1).** This is a production
   write against a database with no migration ledger, so it needs a human decision on mechanism:
   a hand-authored, reviewed `REVOKE` script applied once via
   `supabase db query --linked -f`, *not* a replay of the migration chain. Verify afterward by
   re-running the same posture query (`anon` EXECUTE = false for all 149). **Do not** simply
   re-apply 202607280008/012/013 — presence probes already pass, and blind replay is what the
   `production-schema-drift` memory forbids.
2. **Decide what to do about `public.user_keys` (R2).** 15 rows, raw `api_key text`, browser roles
   hold `arwd`. Options: rotate and drop; or force RLS, revoke `anon`/`authenticated`, and register
   the table in the repo so contracts and erasure routines cover it. Someone has to know whether
   those 15 keys are live before anything is dropped. This is a credential decision, not a schema
   decision.
3. **Gmail / ionos password — yes, this is real, and only James can do it.** [measured] The
   `james@brevitassystems.com` "Send mail as" credential fails `535 Authentication credentials
   invalid` at `smtp.ionos.com`. Exact action:
   - First, in the **ionos control panel**, check whether `james@brevitassystems.com` is a real
     **mailbox** or an **alias/forwarder**. This matters and neither session checked it
     [unverified]. `james@` demonstrably *receives* (~201 threads), which an alias also does — but
     an alias can never authenticate, and no amount of password re-entry will fix it.
   - If it is a real mailbox: Gmail → Settings → Accounts and Import → "Send mail as" → edit
     `james@brevitassystems.com` → re-enter the ionos SMTP password (host `smtp.ionos.com`).
   - If it is an alias: do **not** retype a password. Configure the alias to authenticate as
     `info@brevitassystems.com` with the From header set to `james@`, or provision `james@` as a
     real mailbox.
   - Impact if left broken: replies from the legal/abuse/privacy contact address bounce. Inbound
     is unaffected, so terms/privacy obligations still function. This is **not** a product outage.
   - Do **not** rotate the `info@brevitassystems.com` password while doing this — it is the sole
     credential behind signup, reset, resend and verification.
4. **Land 280031 correctly (R3).** One commit containing the migration file **and** all four
   registration edits **and** the `REVERSE_POSTURE_CUTOFF` bump to `202607280032` (already staged in
   the working tree at `verify-migrations.mjs:922`), plus a decision on
   `scripts/ci/migration-anchored-savings-v2-assertions.sql`. Run
   `node scripts/ci/verify-migrations.mjs` and `npm test` **from a fresh clone of the resulting
   commit**, not from this working tree — this tree has never been what CI sees.
5. **Do the merge deliberately (R5).** Prefer rebase over merge to avoid replaying the 2
   already-upstream commits. Whichever is chosen: re-run `verify-migrations.mjs` on the merge
   result. Treat any hand-resolution inside the four registration files as requiring a second
   reviewer.
6. **Establish who wrote `billing_halting_conditions` at 2026-07-30 08:50:29 UTC (R8)** before any
   further wyfz writes.
7. **Decide the `mailer_autoconfirm` posture (R7).** Flipping "Confirm email" OFF in the Supabase
   dashboard converts a mail outage from a total signup outage into a cosmetic banner failure. It
   is the highest-leverage resilience change available and it is a dashboard toggle, not code.
   [unverified] whether that is acceptable for the compliance posture — owner's call.
8. **Fix the four stale SMTP docs and the Mailgun subprocessor line (R10).** Low effort, prevents
   a repeat of the 2026-07-29 misdiagnosis.

Cheapest ongoing mail health check, read-only and ~1s: `POST /auth/v1/recover` for an address the
owner controls, with the anon key. 200 = `info@` credential alive; 500 = dead. Strengthen to real
proof-of-send with `select recovery_sent_at from auth.users where email='…'` via
`supabase db query --linked -f` — if the timestamp advances to match the probe, the mailer actually
accepted the message. Note the tight rate limits (30 emails/hour, 60s minimum interval), so do not
loop it.

Triage order that still holds, from `signup-email-not-configured`: a 500 "Error sending confirmation
email" does **not** imply broken SMTP. Check Authentication → Users for the address and the signup
logs first, then `dig MX <recipient-domain>` + whois. Only after those are clean should a 500 be
read as a credential failure. The 2026-07-29 case was an NXDOMAIN recipient.

---

## 4. THE COORDINATION PROBLEM

Two — now at least three — sessions have been editing one working tree and one production database
with no locking and no shared clock. The concrete failure modes observed **today**, all measured:

1. **Reports went stale between being written and being read.** The "280028 and 280030 are
   untracked" claim was accurate for 280028 for most of 2026-07-30 and stopped being accurate at
   commit `7c0b2b2`. Two honest sessions checking hours apart reported opposite facts. Not a lying
   problem — a timestamp problem.
2. **A correct claim was flagged as unverified.** 202607280029 was applied and one session verified
   it by postcondition; another doubted it. The doubt cost real time and was wrong. The verifier
   was right, and this audit confirmed it independently and more strictly.
3. **The tree changed underneath a completed audit.** Between 02:03:49Z and 02:20:07Z, another
   session registered 280031 in four files and moved `REVERSE_POSTURE_CUTOFF`. The branch audit's
   headline conclusion — "local red, fresh clone green" — inverted inside 17 minutes.
4. **Every local test run measured a tree CI will never see.** Both prior sessions ran `npm test`
   and `pytest` against a working tree containing untracked files. Local was red for a reason that
   does not exist in CI; CI would have been green for a reason that does not exist locally. Both
   sessions' "tests pass" / "gate is red" claims are untrustworthy on this branch in **both**
   directions.
5. **Production drifted from the repo by hand, and no one recorded it.**
   `billing_period_settlement_evidence` was dropped and recreated by hand; 99 functions carry
   grants the repo revokes; `billing_halting_conditions` was written at 08:50 UTC by an
   unidentified actor. wyfz has no ledger, so none of this left a trace anywhere except in the
   catalog itself.
6. **Migrations were edited after being applied.** Three files, on two different days. The frozen
   checksums all match, so the gate that exists specifically to prevent this could not see it.

### Proposed working rule

**One writer, and it is a human-gated role.**

- **Exactly one session holds the PRODUCTION WRITE token at a time**, and it is granted explicitly
  by the owner per task, not assumed. Every other session is `SELECT`-only against wyfz. No session
  applies a migration, runs `db push`, or issues DDL/DML to wyfz without holding the token.
- **Exactly one session holds the WORKING-TREE token at a time.** The other sessions work in
  `git worktree` checkouts or read-only. If two agents must run concurrently, they must not share
  `/Users/jamesyang/Documents/GitHub/Brevitas-Systems`.
- **No untracked files in `supabase/migrations/`, ever.** A migration is either committed and
  registered in all four surfaces in the same commit, or it lives in `docs/quarantine/`. There is
  no valid third state. 280028's quarantine is the correct pattern; 280031's current state is the
  failure mode.
- **Every claim about the repo carries a HEAD + UTC timestamp, and every claim about production
  carries a UTC timestamp.** An untimestamped claim is not evidence and should be re-measured, not
  argued with.
- **CI claims must come from a fresh clone**, never from the working tree. Cheap to enforce:
  `git clone . /tmp/x && cd /tmp/x && npm test`.
- **Because wyfz has no ledger, "applied" is not a verification.** The only sound check is hashing
  `pg_get_functiondef()` and comparing ACLs against a database built from the repo's own fresh
  manifest. Presence probes would have reported all-green today, with 99 anon-executable SECURITY
  DEFINER functions and two stale money-path bodies sitting in production.
- **Applied migrations are immutable.** If an applied migration needs to change, write a new one.
  Three files violated this in the last four days and the checksum gate is structurally unable to
  catch it.
- **When two sessions disagree, do not adjudicate between the summaries — re-measure the system.**
  Every disagreement in this audit was settled in minutes by a direct `git`, filesystem, or catalog
  read. None required deciding who to believe.

---

## 5. OPEN QUESTIONS

Things the audits could not settle. All **[unverified]**.

1. **Who or what has been writing to production by hand?** `billing_period_settlement_evidence` was
   dropped and recreated; 99 functions carry grants the repo revokes; `billing_halting_conditions`
   was written at 2026-07-30 08:50:29 UTC. The catalog proves *that* it happened, not *who*.
   Supabase dashboard audit logs or the SQL editor history may answer this; neither was read.
2. **Is R1 actually exploitable over HTTP?** The grants are measured fact. Reachability via
   `/rest/v1/rpc/<name>` with the public anon key was deliberately not tested — that is an attack.
   Assume yes until proven otherwise.
3. **Are the 15 rows in `user_keys` live credentials?** Not determined. Row contents were not read
   beyond the column list.
4. **Is `james@brevitassystems.com` an ionos mailbox or an alias?** Determines whether re-entering
   the password can possibly fix the 535, or whether it never could. Only readable from the ionos
   control panel.
5. **What are the actual Supabase SMTP host/username fields?** The "ionos relay, `info@` sender"
   conclusion rests on the delivered message's From address, the domain's MX/SPF, and the DSN naming
   `smtp.ionos.com` — strong inference, not a direct field read. Dashboard-only.
6. **What is `BREVITAS_BILLING_ENABLED` set to in each deployed environment?** Not in the database.
   Requires reading the live Cloud Run / Vercel env, which was not done. The DB proves no money has
   moved regardless.
7. **Is `scripts/ci/migration-anchored-savings-v2-assertions.sql` 280031's partner or an orphan?**
   Untracked, referenced by nothing committed, wired into no CI script.
8. **Should 202607280030 be applied to wyfz at all, and 202607280031 after it?** Both are absent
   from production. Neither is a safe mechanical apply — 280030 carries an apply-time `DO` block and
   280031 rewrites the money path. Requires a plan, not a command.
9. **Was 202607280028's quarantine the final decision or a parking action?** `1224818` said "pending
   the owner's decision"; `7c0b2b2` committed it to quarantine without recording a decision.
10. **Why is production running pre-2026-07-27 bodies of `claim_billing_ledger_entry(_entries)`?**
    Either the 07-27 edits were never applied, or the functions were hand-recreated from an older
    source. The catalog cannot distinguish these.
