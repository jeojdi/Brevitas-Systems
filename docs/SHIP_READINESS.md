# Ship Readiness — 2026-07-30

**Branch:** `security/enterprise-audit-2026-07-30` · **Chain head on disk:** `202607280032`

---

## The answer

**Push and merge: yes, after two small fixes — one of which is that the security migration itself is not committed.**
**Start billing people ASAP: no.** Nothing bills today by design, and five things stand between here and a first invoice — four of them need you, not an agent.
**The revenue is real but small: ~$481/month from a 450-person, $9,500/month enterprise, and only after `202607280031` passes its review.**

---

## 1. Verdict — ship-with-fixes

Two blockers, both cheap, both requiring a human because agents may not commit:

**B1. `supabase/migrations/202607280032_browser_function_execute_contract.sql` is UNTRACKED.** `[verified]`
`git status` shows exactly one untracked file, and it is the security migration. Everything else from this run — the assertion suite, both manifests, the frozen checksum, `verify-migrations.mjs`, `run-migration-tests.sh`, `docs/PRODUCTION_DRIFT.md` — landed in merge commit `b321de6`. So the committed tree references a migration that does not exist in it. Pushing as-is gives a red CI on a fresh clone: `verifyManifest` requires `expectedFreshMigrationOrder` to equal `readdirSync('supabase/migrations').sort()` exactly. The local green everyone reported is green only because the file is present in the working tree.

**B2. The revoke loop aborts on any PROCEDURE in schema `public`.** `[verified]`
Lines 248 and 253 emit `revoke execute on function %s` / `grant execute on function %s` over a `pg_proc` sweep with **no `prokind` filter**. PostgreSQL rejects `ON FUNCTION` for `prokind='p'` (`ERROR: … is not a function`) and the whole transaction rolls back. Two consequences: the one production apply this migration exists for can abort on contact — wyfz was never measured for procedures in `public` — and the first future migration that adds a procedure to `public` turns the fresh-chain build permanently red. Fix is two words: `on function %s` → `on routine %s` in both places. `ON ROUTINE` accepts functions, procedures and aggregates alike.

Do B2 **before** B1, in one commit, and re-record the frozen checksum: the file is pinned at `52c1de6103ee3f67e502fed215e25925f5e39e799e1fec6377129e7dc0354ced` (`migration-frozen-checksums.txt:72`), which still matches the file on disk, so any edit reds `verifyFrozenChecksums` until the digest is updated.

Not blockers, but they ship wrong copy to customers if ignored — see §3.

Everything else is green as measured on this tree `[verified]`: `node scripts/ci/verify-migrations.mjs` exit 0; `npm test` 324/324; `pytest tests/` 1110 passed; `dashboard` checks 144/144; `scripts/ci/run-migration-tests.sh` exit 0 with the new suite reached and passed on **both** the fresh and the upgrade path. The mid-session merge conflicts are all resolved — zero conflict markers remain, zero unmerged paths.

---

## 2. Security — the hole is closed in the repo, still open in production

**The finding was real and worse than "callable".** `[verified on wyfz, SELECT-only]` 149 SECURITY DEFINER functions in `public`; **99 EXECUTE-able by `anon`**, the unauthenticated browser role; **zero of the 99 reference `auth.uid()`, `auth.jwt()` or `auth.role()`.** 14 are trigger-only and not RPC-reachable, so the real reachable surface is **85 callable, 61 of them writers**.

Do not let anyone summarize the compliance functions as "they check the actor." `compliance_actor_role()` is a **regex on a string**, not authentication — its whole body is `if p_actor_id !~ '^(system|brevitas_admin):[A-Za-z0-9._:-]{3,96}$' then raise`. The literal string `brevitas_admin:x` passes it. `[verified from the function body]` Exploitable today with no secret at all: the compliance erasure chain, `claim_billing_ledger_entries` + `complete_billing_ledger_entry` (mark fees "reported" that Stripe never received), `admin_usage_report(p_filters => '{}')` (every tenant's `owner_id`, spend and savings), `semantic_cache_store_bounded` (poison, and wipe the cache with `p_max_entries=1`), `approve_bvx_device`, and `append_company_audit` (forge audit rows attributed to any actor — treat wyfz audit logs as untrustworthy for any incident timeline).

**Root cause confirmed with the receipt** `[verified]`: `pg_default_acl` on wyfz carries `f:postgres={…anon=X/postgres,authenticated=X/postgres…}`. Supabase's project-level default privileges re-grant EXECUTE on every function `postgres` creates. Migrations `…0015 / 0024 / 0027` fought this for TABLES; the FUNCTION side was never closed.

**This is drift, not a repo bug** `[verified]`: a fresh local PG 17.10 chain (bootstrap + all manifest entries) yields **0** anon-executable and **0** authenticated-executable SECURITY DEFINER functions, despite local `pg_default_acl` carrying the same hazard — because every migration ends with its own per-function `REVOKE`. All 99 exposed prod functions have such a REVOKE in the repo. Production drifted from a guarantee the tests enforce.

**Why the tests missed it** `[verified]`: every existing privilege assertion is a hardcoded allowlist (`proname in (…)`, `proname like 'company_admin_%'`). No suite iterated all `prosecdef` functions and asserted the universal invariant. `scripts/ci/migration-browser-function-privilege-assertions.sql` is that suite now, and it is wired into `run_forward_assertions`.

**The blanket revoke is safe — verified from the built bundle, not memory.** `[verified]` `public/dashboard/assets/index-Cw4umoNL.js` (958,873 bytes): the single `.rpc(` and one of the 69 `.from(` are supabase-js **method definitions**; the other 68 are `Array.from` / `Buffer.from` / library internals. Zero `functions.invoke`. **Zero hits when all 99 exposed function names are grepped against the bundle** — an `.rpc()` call site cannot exist without its name as a string literal. The entire browser Supabase surface is `auth.*` (GoTrue): getSession, getUser, onAuthStateChange, signUp, signInWithPassword, signOut, resetPasswordForEmail, updateUser. All data flows over `fetch('/v1/…')` and `fetch('/api/admin/company/…')`. Server callers are `service_role` (`src/lib/billing/supabase.ts:130`, `api/store.py:3653`) and are untouched — service_role EXECUTE went 130 → 131 across the migration, the sole delta being the new guard function.

**Proven end to end against production-shaped drift** `[verified]`: injected the exact wyfz shape into a local DB → 153–154 SECURITY DEFINER functions anon-executable; applied `202607280032` → `NOTICE: browser EXECUTE removed from 153 SECURITY DEFINER and 6 SECURITY INVOKER routine(s)` → **0**. The stored `pg_default_acl` f-row went from naming anon/authenticated to `{service_role=X/…}`. Re-applying is a no-op (idempotent). Before the fix the new suite exits 3 and lists all 154 offenders by signature; after, exit 0.

**Does it prevent recurrence? Partly — and this is disclosed honestly rather than papered over.** `[verified, reproduced twice on PG 17.10]` `ALTER DEFAULT PRIVILEGES … REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` **cannot** remove PostgreSQL's built-in EXECUTE-to-PUBLIC default: a `pg_default_acl` row is a diff merged *additively* onto `acldefault()`, so it can only subtract entries the diff already contains. After the migration, a newly created SECURITY DEFINER function still comes back anon-executable via PUBLIC. So:

- The **Supabase-specific** vector (explicit `anon`/`authenticated` in `pg_default_acl`) is removed and cannot recur. ✅
- The **PostgreSQL** default means every new function is born PUBLIC-executable until its own migration revokes it — which is why all 82 migrations already do. The compensating control is the new standing assertion, which fails CI and names the offender. ✅ in CI, ❌ on wyfz, which has no migration ledger and never runs the harness.
- **Recommended follow-up, not in this run:** a `202607280033` that revokes `USAGE ON SCHEMA public` from anon and authenticated. Without schema USAGE no EXECUTE grant is reachable at all. Measured safe here (0 security_invoker views, 0 column defaults or check constraints referencing a public function, all 3 RLS policies use only `auth.uid()`, anon holds table grants on 4 RLS-gated tables). Give it its own review; do not fold it into 032.

**Scope caveat** `[unverified]`: the safety argument covers this repo as checked out. Any out-of-repo anon-key holder (third-party integration, external script, Supabase Edge Function) would not appear in these greps and would break on its next RPC. Nothing in-repo suggests one exists.

---

## 3. Onboarding — what a customer does now

Company Administration now returns a **ready-to-paste hosted config** the moment a service account is minted, in Python / Node / curl, each containing the key, `base_url = https://api.brevitassystems.com/v1`, and **both** required headers. One-time-reveal posture is preserved: the block renders only while the secret is in memory, is cleared on "Clear from view", on token change and on unmount, touches no storage API, and carries `ph-no-capture`. The customer-ID field is slugged to `[a-z0-9._-]` so nothing typed can reshape the snippet. `[verified — 144/144 dashboard tests, builders executed under `node:vm`, not grepped]`

End to end, from signup: **sign up → dashboard opens → Company Administration → create service account → copy the block → paste → first request bills.** Five steps.

**The finding that changed the work** `[verified from `api/server.py`]`: the gap is **two** headers, not one. `api/server.py:1731` reads the credential from `x-brevitas-key` only; `Authorization` is never consulted on the proxy path. The canonical snippet in `docs/ONBOARD_HOSTED_CUSTOMER.md` §1.1 and the one shipping in `dashboard/src/components/InstallCommand.jsx` set only the SDK `api_key` plus `X-Brevitas-Customer-ID` — both produce **`401 Missing X-Brevitas-Key`**, before the customer-id gate at :1757 is ever reached. `grep -c 'X-Brevitas-Key'` returns **0** on both files.

**Two non-blocking fixes someone must own before a design partner sees them:**
1. `InstallCommand.jsx` `HOSTED_SNIPPET` → add `"X-Brevitas-Key": os.environ["BREVITAS_API_KEY"]` to `default_headers`; same fix in `docs/ONBOARD_HOSTED_CUSTOMER.md` §1.1.
2. `InstallCommand.jsx` (~lines 97-101) claims `brevitas connect` "pins your own id to the key it mints so a missing header resolves rather than fails" and offers `--multi-tenant`. **That pin is not shipped** — `docs/ONBOARD_HOSTED_CUSTOMER.md` §1.2 says so in terms. The new block correctly says there is no fallback and no pinned default, so the product currently contradicts itself on two screens. Delete the claim.

Also: the snippets use `gpt-4o-mini` as a placeholder model and do not say so; a BYOK/Anthropic customer must swap it.

---

## 4. Before the first invoice — ordered, with owners

| # | Step | Owner | Why it blocks |
|---|---|---|---|
| 1 | Fix B2 (`on function` → `on routine`), commit `202607280032`, refreeze the checksum, push, merge | **human** | Otherwise CI is red and the security migration is not in the tree |
| 2 | Apply `scripts/db/wyfz_function_realignment_20260731.sql` (to be written) — repairs the two stale `claim_billing_ledger_*` bodies | **human** | See below. This is a live money defect and no forward migration can fix it |
| 3 | Apply `202607280032` to wyfz: one file via `supabase db query --linked -f`, read **both** NOTICEs for skipped granting roles (`supabase_admin` is the likely skip), then `select public.assert_browser_role_function_privileges();` | **human** | Production still has all 99 grants. CI green says nothing about wyfz |
| 4 | Apply `202607280030` (attestation) — committed, **never applied to wyfz** | **human** | It creates a `brevitas_attestor` LOGIN role needing an out-of-band password, and grants the three attestation functions to that role **only**, not `service_role`. No deployed service holds it, so the attestation surface exists but is unreachable until someone wires it up. **The settlement run halts at this gate — this is one of the two reasons the invoice is $0.** |
| 5 | `202607280031` (cache fee basis) — **DO NOT APPLY** until its own adversarial review passes | **human** | Its own header records defect 4 (dilution) as NOT CLOSED. It also changes the OUT lists of two functions, so it must drop/recreate inside its transaction, and it obsoletes `scripts/db/wyfz_function_realignment_20260730.sql` — re-running that file afterwards silently reverts the fee basis. **Until 031 ships, the fee is $0 by construction.** |
| 6 | Live Stripe webhook endpoint → `/api/billing/webhook`, 6 event types, one live test event answered 200 | **human, Stripe dashboard** | `docs/GO_LIVE_PLAN.md` 4.3 — still unconfigured |
| 7 | Live Customer Portal configuration saved | **human, Stripe dashboard** | `docs/GO_LIVE_PLAN.md` 4.4 — still unconfigured |
| 8 | Get real traffic through the hosted proxy | **human** | **No customer traffic has ever reached it.** Prod verified savings stopped 2026-07-17; every fee row since is `authoritative=false`. There is nothing to invoice |

**Step 2, expanded — the live money defect** `[verified]`: wyfz's `claim_billing_ledger_entries` / `claim_billing_ledger_entry` use `status in ('sending','reported','review')` where the repo uses `status in ('sending','reported') or (status='review' and outbound_started_at is not null)`. Production counts **never-sent** review rows toward the weekly cap and toward `expected_period_microusd`. Legitimate fees get stamped `capped` against money Stripe never received, and `billing_recovery` reconcile compares an expected total permanently larger than actual — it can never converge. The narrowing landed in `202607200006` at commit `fa9a2a0`; wyfz runs the pre-`fa9a2a0` body, and `202607200006` is checksum-frozen, so no forward migration will ever repair it. Full map in `docs/PRODUCTION_DRIFT.md`.

Drift context: 275 comparable signatures, **261 byte-identical**. Compliance (27/27), company-admin (23/23) and device/key (4/4) are clean. All drift is billing/settlement, semantic cache, and one CI-only helper, and decomposes into exactly three causes: 0031 not applied, 0030 not applied, and the two stale frozen bodies above. `[verified]`

---

## 5. The economics — say this out loud before you sell it

From `docs/ENTERPRISE_SIMULATION.md`, a real 326-request run against a throwaway project, not a spreadsheet:

- **32.21% of calls hit the cache** `[measured]` — and that is an **upper bound**. Everything ran at temperature 0, which is *required* for cacheability at all; the customer says several teams intentionally run temperature > 0 and all of that traffic is uncacheable.
- **The hit rate is a property of the traffic, not the cache.** The customer's own repeat-rate estimates round-tripped through a working cache almost exactly.
- **Never quote the 20.18% raw dollar figure.** Simulated prompts were a few hundred tokens with near-uniform cost, so the dollar percentage just tracks the call hit rate. Real workloads invert this: the expensive traffic (6-10k-token document extraction) is the least cacheable and the cheap traffic (CI fixtures) is the most. A call-level hit rate always overstates dollars.
- **Customer saves ~$1,622/month net (17.1%)** on $9,500/month spend `[extrapolated]` — clears their 12-15% bar.
- **Brevitas invoices ~$481/month** at 25%, and **$0 today** `[measured]`, halted independently by both the attestation gate and production's zero-spend-concentration rule.
- **53% of the commercial case is one cohort the customer has pre-announced they may delete.** CI eval fixtures hit at 81.6%. If they bypass CI evals — which they said they probably would — customer net falls to **$766/month (8.1%)**, below their floor and brushing their churn trigger, and our fee falls to **~$196/month**. A nightly CI cadence against a 24-hour hard TTL is additionally a coin-flip per fixture. `[extrapolated]`
- **The best-caching cohort arguably should not be cached at all.** Serving a CI eval suite from cache means the eval is not testing the model. That is the traffic carrying half the revenue.
- **Cache replays carry `actual_cost_usd = 0` by construction**, so the product's headline mechanism is precisely the traffic that is hardest to charge for. Structural, not incidental.
- **The build-vs-buy line is not refuted.** A Redis exact-match layer is two engineer-weeks with no third-party data exposure and pays back in about a quarter, with sub-millisecond hits instead of 903ms.

**Plainly: ~$481/month per 450-person enterprise, possibly $196.** The commercial problem is upstream of billing mechanics — 25% of exact-match savings, on enterprises whose expensive traffic is by nature unique, is a small number. The argument worth having next is about the 25%, or about whether exact-match caching alone is the product. Ship `202607280031` because the current state is unbillable, not because it raises revenue: it makes the fee smaller, not larger.
