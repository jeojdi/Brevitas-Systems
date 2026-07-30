# Go-live plan: from here to a customer paying

Written 2026-07-30. This is the ordered plan, who does each step, and how each one is
checked. It supersedes nothing — it sequences the existing documents:
`docs/WYFZ_APPLY_PLAN.md` (production DDL), `docs/PRODUCTION_ENABLE_SAVINGS.md`
(config), `docs/STRIPE_TEST_PLAN.md` §5 (go/no-go), `docs/STRIPE_BUILD_REPORT.md`
(what is proven).

**Where we already are** `[verified 2026-07-30]`:

- Production `wyfz` carries the full `202607280005`–`202607280024` chain plus the
  function realignment; the billing path is byte-identical to canonical (md5-verified).
- The money loop is proven end to end in Stripe test mode against a throwaway Supabase
  project: checkout → webhook → settle → promote → worker → Stripe meter, exact to the
  micro-dollar.
- No customer has ever been billed. Production has produced **zero** billable usage
  since 2026-07-15.

---

## Stage 1 — Ship the code (in progress)

| # | Step | Owner | Check |
|---|------|-------|-------|
| 1.1 | Merge `origin/main` into the branch, resolve conflicts | done | 132/132 dashboard checks |
| 1.2 | PR #39 CI green | CI | all required checks pass |
| 1.3 | Merge PR #39 → auto-deploys Vercel + Railway | you (or me on your word) | deploy succeeds |
| 1.4 | Confirm the worker did not crash-loop | me | Railway `/ready`, billing block honest |

**Why this order is safe:** the database is deliberately *ahead* of the code. Every
migration on `wyfz` was checked for compatibility with the currently-deployed (older)
app code. Deploying code that is behind the schema is the safe direction; the reverse
is not.

---

## Stage 2 — Settlement sender (the last missing code)

A settlement promoted to `pending` currently has **no path to Stripe** — the claim RPC
reads only `billing_ledger`. Until this lands, a settled week cannot be charged.

| # | Step | Owner | Check |
|---|------|-------|-------|
| 2.1 | Build migration `202607280029` + claim/send/sweep RPCs | me | assertion suite on real PG |
| 2.2 | Worker integration | me (done) | pytest, offline fakes |
| 2.3 | Status route shows settled prior periods | me (done) | route tests |
| 2.4 | Adversarial money review | me | verdict must be ship / ship-with-fixes |
| 2.5 | Prove on the throwaway: settlement → Stripe meter | me | meter aggregate == fee exactly |
| 2.6 | Apply `280029` to `wyfz` (Window B3) | me, on your go | postcondition queries in the runbook |

**Why 2.5 stays on the throwaway:** proving the loop requires a settlement to charge.
`wyfz` has none, so a proof there would mean fabricating usage and a fee in a
**seven-year immutable ledger** that blocks deletion by design — unrecoverable. The code
cannot tell the two databases apart, so a green proof on the throwaway is a green proof
of the code. The first *real* settlement on `wyfz` comes from a real customer's real
savings, in Stage 5.

---

## Stage 3 — Turn savings back on

Nothing is billable today because the savings path is switched off in two places, both
defaulting off, both failing silently (200 responses, zero savings, no error).
Full detail: `docs/PRODUCTION_ENABLE_SAVINGS.md`.

| # | Step | Owner | Check |
|---|------|-------|-------|
| 3.1 | Railway worker: `BREVITAS_WARM_RETENTION_DAYS=365` | **you** | stops the 5-minute evidence purge |
| 3.2 | Railway API: `BREVITAS_CACHE_ENABLED=true` | **you** | I cannot reach Railway |
| 3.3 | Per-tenant `organizations.cache_enabled = true` | me, on your list of orgs | SQL, sanctioned path |
| 3.4 | `BREVITAS_BILLING_WEEKLY_CAP_USD` on Vercel + Railway | **you** | currently missing in both |
| 3.5 | Watch usage for savings appearing | me | query in §3 of the savings doc |

**3.5 is the real gate.** If `authoritative` stays 0, traffic is not going through the
hosted proxy — local `bvx` proxies are non-billable by design (anti-forgery). That is an
integration problem, not a billing one, and no amount of billing work fixes it.

---

## Stage 4 — Live Stripe

| # | Step | Owner | Check |
|---|------|-------|-------|
| 4.1 | **Rotate the leaked `sk_live` key in Stripe** | **you** | `GO_LIVE_RUNBOOK.md:30-32` — removing it from Vercel is not enough |
| 4.2 | `npm run billing:setup` in live mode | me, with your live key in env | both validators accept the catalog |
| 4.3 | Live webhook endpoint → `/api/billing/webhook`, 6 event types | **you** (dashboard) | one live test event answered 200 |
| 4.4 | Live Customer Portal configuration saved | **you** (dashboard) | portal session opens |
| 4.5 | Pin the same Stripe API version live | me | already pinned in code |

**Do not hand me the live key in chat.** Put it in Railway/Vercel yourself; I only need
to know it is set.

---

## Stage 5 — First real customer

| # | Step | Owner | Check |
|---|------|-------|-------|
| 5.1 | Attest the org (`organization_billing_arrangement`) | me, per org | see the note below |
| 5.2 | Customer signs up, routes traffic through the **hosted** proxy | customer | savings rows appear |
| 5.3 | Customer subscribes via Checkout | customer | `subscription_status = active`, 7-day period |
| 5.4 | Flip `BREVITAS_BILLING_ENABLED=true` | **you**, after go/no-go | `STRIPE_TEST_PLAN.md` §5 |
| 5.5 | Settle + promote the first closed week | me, with you watching | fee = 25% of verified savings |
| 5.6 | Worker sends; Stripe invoices | automatic | meter aggregate == fee |

### Attestation, and making it automatic

`202607280009` deliberately makes `organization_billing_arrangement` unwritable even by
`service_role`: an org cannot be billed until a human attests the arrangement out of
band. That is a safety property, not an oversight — it is the last thing standing
between a bug and an unauthorized charge.

You asked to make it automatic on signup. The right way is **not** to remove the gate,
but to anchor it to a consent event that already exists: `202607280021` records terms
acceptance **server-authoritatively** in `public.legal_acceptances` (the browser cannot
forge it). So attestation can be derived — "this org accepted terms version X at time T,
and terms version X contains the 25%-of-verified-savings arrangement" — rather than
asserted by a human typing SQL.

That is a real design change with a legal dimension (your terms must actually state the
billing arrangement for the derivation to be honest). Tracked as **Stage 6**, deliberately
not bundled into go-live.

---

## Stage 6 — Deferred, each needing a decision

| Item | Why it is deferred |
|------|--------------------|
| **Anchored cache savings** (`202607280028`, quarantined) | Its review found a blocker + 3 highs, incl. one sub-cent payment unlocking $125. Needs the **price-source** redesign: bill 25% of `tokens_saved × the customer's observed price`, not a mere existence gate. Until then cache-only savings bill **$0**. |
| **Auto-attestation from legal acceptance** | Needs your terms to state the arrangement; see above. |
| **Automatic weekly settlement** | Settlement is operator-gated by design. Fine for the first customers, painful at scale. |
| **Provider-invoice reconciliation** (`G13`) | A real provider invoice must be reconciled against `sum(actual_cost_usd)` once, before any period leaves draft. Calendar-time gate; no code discharges it. |

---

## The honest summary

After Stages 1–4, you can onboard a company and they *can* pay you. Whether they pay
anything depends on Stage 3 producing real savings and on the Stage 6 cache-savings
decision — because if a customer's savings come only from cache replays, today's rules
bill them **$0**.
