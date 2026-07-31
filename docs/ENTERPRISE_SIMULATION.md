# Enterprise Simulation: Copperline Underwriting Group

**Date:** 2026-07-29 (one-day simulation)
**Code under test:** `/tmp/brevitas-sim` worktree pinned to commit `7c0b2b2`
**Database:** caestus-labs throwaway Supabase project only (org `b9d8bb73-f02f-435e-9d54-a71c0a9e98b1`). Production wyfz, Railway, and Vercel were never touched. Stripe stayed in test mode.
**LLM provider:** DeepSeek (real API calls, 242 completions, ~$0.031 total spend, well inside the ~600-completion ceiling).

The owner's question: *"Pretend you're an enterprise with 100 users — would it work, would we make money, would they save money, would everything work out?"*

Short answer: **the product works and the customer saves real money; Brevitas invoices $0 under today's shipped rules, and roughly $481/month per customer of this size after the billing redesign ships — about $196/month if the customer does the one thing they have already said they will do.** Details, with every number tagged `[measured]` or `[extrapolated]`, below.

---

## 1. The company simulated

Copperline Underwriting Group: a ~450-person commercial-insurance MGA writing small-fleet trucking policies. 100 seats/service-owners touch their LLM gateway daily. Spend is ~$9,500/month across OpenAI + Anthropic APIs (Copilot/Cursor seat licenses are out of scope — they never transit the gateway). The CFO flagged 8%/month invoice growth and someone promised "caching savings with no code changes."

Their traffic, by cohort (their own honest estimates of verbatim repeat rates):

| Cohort | Users | Calls/user/day | Claimed verbatim repeat | Spend line |
|---|---|---|---|---|
| Document intake pipeline (service accounts) | 5 | 400 | ~12% (retries, duplicate emails, batch reprocessing) | ~$5,800/mo |
| Claims & support agents (drafts + FAQ chips) | 24 | 30 | ~30% blended (chips repeat, interpolated drafts never) | ~$900/mo |
| Software engineers (direct API only) | 38 | 25 | ~2% | (in ~$700/mo eng+ops) |
| QA / eval engineers (CI fixture suites) | 8 | 300 | ~85% (frozen fixtures, temperature 0) | ~$1,400/mo |
| Underwriters (doc-Q&A assistant) | 20 | 15 | ~6% (only the suggested-question buttons repeat) | ~$700/mo |
| Ops / product managers (ad hoc) | 5 | 8 | ~3% | (in eng+ops) |

The structural problem they walked in with, in their own words: the spend distribution is inverted against exact-match caching. ~$5,800/month is document extraction where every request body is unique; the two cohorts with real byte-identical repetition (CI evals, FAQ chips) represent roughly $1,700/month. Their success bar: at least 12-15% net reduction (~$1,000+/month) with p95 added latency under 100ms on misses. Their stated build-vs-buy alternative: an in-house Redis exact-match layer, two engineer-weeks.

## 2. What was actually run

A real end-to-end run, not a spreadsheet:

- Fresh org onboarded on the caestus throwaway with `organizations.cache_enabled=true`, a real Stripe **test** customer with an attached card and a weekly metered subscription, and `billing_accounts` seeded from that subscription's own 604,800-second period.
- The hosted API from the pinned worktree booted on `:8000` with both halves of the savings switch on (`BREVITAS_CACHE_ENABLED=true` + tenant flag), **exact-match cache only** (`BREVITAS_SEMANTIC_CACHE=false` — no fuzzy reuse), sqlite cache backend.
- An `organization_service` key with the correct five scopes. Honesty note: the key row was **inserted directly into the database**, not minted through the company-admin dashboard route (which needs a session cookie). Authorization was then verified through the same `service_key_authorization` RPC the API uses, so the auth path under test is genuine — but the issuance path was not exercised.
- 326 requests transcribed from the cohort briefs (repeated prompts bound to constants so repeats are byte-identical by construction), 220 unique bodies, deterministically shuffled so users interleave, 4 workers, all with `X-Brevitas-Key` + `X-Brevitas-Customer-ID`, DeepSeek BYOK, temperature 0, max_tokens 800. 326/326 returned HTTP 200 in 393 seconds. `[measured]`
- Settlement was then exercised for real: the live window correctly refused (`period_not_closed`), after which the Stripe-anchored billing window was shifted back one full period (on the throwaway only) and `settle_billing_period` run against the closed period.
- Server killed at the end; port confirmed free.

**Scale honesty:** 326 requests is 5.1% of one Copperline day (6,410 calls). One day, limited volume, one provider/model. Everything scaled beyond that is `[extrapolated]`.

## 3. The measured cache hit rate, and why it came out that way

**32.21% of calls hit the cache (105 of 326).** `[measured]`

Per cohort `[measured]`:

| Cohort | Requests | Hits | Hit rate | Customer's own claim |
|---|---|---|---|---|
| QA / CI evals | 87 | 71 | 81.6% | ~85% |
| Support | 52 | 16 | 30.8% | ~30% |
| Claims | 38 | 8 | 21.1% | (part of support's 30% blend) |
| Intake | 20 | 4 | 20.0% | ~12% (n=20 — too small to trust; use 12%) |
| Underwriters | 24 | 2 | 8.3% | ~6% |
| Engineers | 103 | 4 | 3.9% | ~2% |
| Ops | 2 | 0 | 0.0% | ~3% |

Why it came out this way:

- **The cache itself was near-perfect.** Of 106 theoretically available hits (326 requests, 220 unique bodies), it captured 105 — 99.06%. `[measured]` The single loss was an in-flight duplicate under 4-way concurrency where both copies missed — an error in the safe direction (understates savings, never invents them).
- **The hit rate is therefore a property of the traffic, not the cache.** The customer's repeat-rate estimates round-tripped through a working cache almost exactly. Their honest doubts were honest.
- First occurrences miss and save nothing — correct behavior, not a fault. Truncated responses (27 of 326 hit max_tokens) were correctly refused storage and never replay. `[measured]`
- **Flattering condition to keep in view:** every request ran at temperature 0, which is *required* for cacheability at all. Copperline says several teams intentionally run temperature > 0; all of that traffic would be uncacheable. Treat 32.21% as an upper bound.

## 4. Q1 — Does Copperline actually save money?

**Yes — about $1,600/month net, clearing their bar. But do not quote the raw run percentage, and the whole case balances on one cohort they have said they may turn off.**

Raw run numbers `[measured]`: paid DeepSeek $0.02818102; verified savings $0.00712418; would-have-paid $0.03530520 — **20.18% gross, 15.13% net of a 25% fee**. The pricing chain reconciles independently: token counts on paid rows priced at DeepSeek list reproduce recorded cost to the tenth decimal. Copperline's churn trigger "invoice claims savings our accounting can't reproduce" is satisfiable today. `[measured]`

**The 20.18% is not transferable** and must not go in a deck. Simulated prompts were a few hundred tokens with near-uniform cost, so the dollar percentage just tracks the call hit rate. Copperline's real workload inverts this: the expensive traffic (6-10k-token intake extraction) is the least cacheable and the cheap traffic (CI fixtures) is the most, so a call-level hit rate always overstates dollars.

The honest number — measured per-cohort hit rates weighted onto their own stated spend lines `[extrapolated]`:

| Line | Spend | Hit rate applied | Gross savings |
|---|---|---|---|
| Intake extraction | $5,800 | 12.0% (their estimate; measured 20% at n=20 flagged as upside only) | $696 |
| CI eval fixtures | $1,400 | 81.6% [measured, n=87] | $1,142 |
| Support + claims | $900 | 26.7% [measured, n=90] | $240 |
| Doc-Q&A assistant | $700 | 8.3% [measured, n=24] | $58 |
| Engineers + ops | $700 | 3.8% [measured, n=105] | $27 |
| **Gross** | | | **$2,163/mo (22.8%)** |
| Brevitas fee (25%) | | | −$541/mo |
| **Net to Copperline** | | | **$1,622/mo (17.1%)** |

Against their success criteria (≥12-15% net, ≥$1,000/month): **clears it. They renew.** `[extrapolated]`

**Except.** Their sharpest doubt decides the deal: they said they would likely bypass the cache for CI evals, because cached replays stop regression evals from exercising the live model — which is the entire point of regression evals. Remove that line `[extrapolated]`:

- Gross: $2,163 − $1,142 = **$1,021/mo (10.7%)**; fee $255; **net $766/mo = 8.1%** — below their "worthwhile" floor and one bad month above their two-consecutive-months-under-8% churn trigger.

53% of the commercial case is a single cohort of machine traffic the customer has pre-announced they may delete. A further threat to that same cohort: the cache TTL is a 24-hour hard cap and Copperline's CI runs nightly — a nightly cadence against a 24h TTL is a coin-flip per fixture and could materially cut the 81.6% even if they don't bypass. `[extrapolated — not tested]`

One genuine piece of good news for the pitch: their own $900-1,200 gross guess was slightly pessimistic, because they under-counted intake's boring 12% verbatim retry/duplicate rate — ~$696/month of real, byte-identical savings on the biggest spend line. `[extrapolated from their own estimate]`

## 5. Q2 — Would Brevitas actually earn anything?

### Today: $0. Invoiced, ledgered, settled: zero. `[measured]`

`settle_billing_period` was run for real against the closed period. Result:

```
outcome: halted
halting_condition: unattested_billing_arrangement
gate_fee_microusd: 1781   recomputed_fee_microusd: 1781
```

The fee that *would* have been invoiced is $0.001781 — exactly 25% of the $0.00712418 verified savings `[measured]`. It sits behind the human attestation gate (202607280009/0030), which was not defeated: EXECUTE is granted only to `brevitas_attestor`, which no service holds, and forging it is precisely what that gate exists to prevent.

**Critical caveat — the sandbox is not production, and the difference is exactly the thing being measured.** Caestus has the **quarantined** migration 202607280028 (`billing_zero_spend_savings_anchor_id` + anchored-basis evidence) applied, while the repo carries it at `docs/quarantine/` labelled "not applied anywhere. Do not apply it." That is why the zero-spend concentration check passed here (anchored 105 / unanchored 0). On production's documented schema, the same rows compute zero-spend share = 1.00000 > 0.50 limit and halt at `zero_spend_concentration` — same recomputed fee, 1,781 microUSD, still $0. `[measured on caestus; production behavior derived from the same data]`

**Either way: $0. Two independent gates, same answer.** Cache replays carry `actual_cost_usd = 0` by construction, so the product's headline mechanism — the CI cohort at 81.6% hits — is precisely the traffic Brevitas cannot charge for under shipped rules. This is structural, not incidental.

The run also **reproduced the defect that quarantined 0028**: its anchor joins a replay to *any* earlier authoritative/priced row with the same (org, provider, model) and cost > 0 — no per-replay pairing, no price relationship. The cheapest paid row in this run was $0.0000187600, and all 105 replays anchored off that pool. A single sub-cent call unlocks an unbounded fee basis. `[measured]` Do not un-quarantine it.

### After the redesign (202607280031 — present on main, not in the pinned worktree, not applied to caestus; all figures below are arithmetic over real rows, not an observed settlement):

- **This simulated period still bills $0, for a third independent reason:** the $0.30 materiality floor per (org, provider, model, window). Paid spend was $0.0282 < $0.30, so no observed price exists, all 105 replays are unanchored, basis $0, and zero-spend concentration re-fires. `[measured arithmetic]`
- If the floor were cleared, 0031's basis = min(list-price counterfactual, tokens_saved × observed unit price): **$0.00273 vs $0.00712 — a 61.6% haircut on this corpus** `[measured arithmetic]`, because `tokens_saved` counts prompt tokens only and this corpus ran 6.87 output tokens per input token. This is the under-billing the owner authorized.
- **On Copperline's real, input-heavy traffic the haircut is far smaller** — the useful finding. Intake and CI fixtures are long-input/short-output, where retention pins near 100%; only chat-shaped traffic gets gutted. Estimated 0031 basis `[extrapolated]`:

| Line | Gross savings | ~Retention | Basis |
|---|---|---|---|
| Intake | $696 | ~100% | $696 |
| CI evals | $1,142 | ~100% | $1,142 |
| Support/claims | $240 | ~10% | $24 |
| Doc-Q&A | $58 | ~87% | $50 |
| Eng + ops | $27 | ~50% | $14 |
| **Basis** | | | **$1,926/mo** |
| **Fee at 25%** | | | **~$481/mo** |

The $0.30 floor ($9,500/month spend) and the 3.0 savings/spend ratio cap (measured ratio 0.25, scaled ~0.30) are both trivially cleared for real traffic. `[measured / extrapolated respectively]`

### What one customer is worth

| Scenario | Brevitas revenue |
|---|---|
| Today, production rules | **$0/mo** (structural) `[measured mechanism]` |
| If attested, under quarantined 0028 arithmetic | $541/mo ($255 with CI bypass) `[extrapolated]` |
| After 0031 ships | **~$481/mo** (~$196 with CI bypass) `[extrapolated]` |

Note: the anchoring chain 0031 depends on (`usage_log.savings_anchor_request_id`, `semantic_cache.origin_request_id`) exists nowhere in this simulation — neither column is on caestus and 0031 is not in the pinned worktree. **The redesign's billable path has zero end-to-end evidence behind it.** Everything in this section is arithmetic over real rows.

## 6. Q3 — What strained

What held cleanly `[all measured]`: attribution was flawless — 326/326 rows authoritative, priced, verified, proxy-sourced, correctly customer-attributed, zero duplicates, zero unattributed rows; exactly the two row shapes the design predicts (passthrough with cost / exact_cache with savings) and no third; pricing reconciles to provider list price independently; 27 truncated responses correctly refused; 105/106 available hits captured.

What strained:

1. **Latency.** Cache-hit path: p50 903ms / p95 2,084ms of pure Brevitas overhead (auth-context read + synchronous usage-receipt write to a cross-region Supabase pooler, from a laptop). `[measured, this configuration only]` That is ~9x Copperline's 100ms bar. A co-located deploy would be materially faster, but nothing here proves it passes. The miss-path comparison (6,439ms proxied vs 2,998ms direct) is confounded (concurrent vs sequential) and must not be quoted. `usage_log` has no latency column, so none of this is corroborable from the ledger. A 903ms cache *hit* is also an argument for the customer's in-house Redis alternative, where a hit is sub-millisecond.
2. **The sandbox schema diverges from production in exactly the billing dimension under test** (quarantined 0028 applied). Any production billing conclusion must be re-derived against wyfz's actual schema.
3. **Never exercised:** the 0031 anchoring chain end-to-end; the Supabase cache backend (sqlite used instead — caestus's `semantic_cache` lacks `origin_request_id`); the service-key mint path; temperature > 0 traffic; production-scale volume, cache eviction, TTL expiry, or 100-seat concurrency; availability under load (Copperline's churn trigger (d) is availability, not savings).
4. **Settlement required mutating the sandbox** (billing window back-dated one period, row timestamps shifted) after headline measurements were taken.

## 7. Bottom line

**Would it work?** Yes. The proxy, cache, attribution, and pricing chain all behaved exactly as designed at this volume. The receipts are honest to the tenth decimal.

**Would they save money?** Yes: ~$1,622/month net (17.1%) `[extrapolated]`, clearing their 12-15% bar — *if* they keep CI evals cached. If they bypass CI evals, as they said they probably would, net falls to $766/month (8.1%), below their floor and brushing their churn trigger. More than half the value sits in machine traffic with a plausible reason to be turned off.

**Would we make money?** Not today — $0, structurally, under shipped rules: the settlement run halted at the attestation gate, and production's zero-spend concentration rule halts the same period independently. After 0031 ships: **~$481/month from a 450-person, $9,500/month enterprise; ~$196/month in the CI-bypass case.** `[extrapolated]`

Three numbers to hold together:

- **~173 Copperline-sized enterprises for $1M ARR** ($83,333/mo ÷ $481). **~425 in the CI-bypass case.** `[extrapolated]`
- **~$0.0025 of revenue per proxied call**, each call carrying an auth read and a synchronous Postgres write. `[extrapolated]`
- **The customer's build-vs-buy line is the strongest argument in their brief and this simulation does not refute it:** a Redis exact-match layer is two engineer-weeks with no third-party data exposure, and at $1,622/month net it pays back in about a quarter — with sub-millisecond hits instead of 903ms.

**Recommendation.** Ship 0031: the current state is unbillable and 0028's anchoring defect is real and was reproduced here. But do not ship it expecting revenue — it makes the fee smaller, not larger. The genuine commercial problem is upstream of billing mechanics: 25% of exact-match savings, on enterprises whose expensive traffic is by nature unique, is a small number. The next argument to have is about the 25%, or about whether exact-match caching alone is the product.

### Limitations of this simulation (read before quoting anything above)

One day, 326 requests (5.1% of one customer-day), one provider and model, few-hundred-token prompts instead of 6-10k-token documents, temperature 0 everywhere, sqlite cache backend, sandbox schema that diverges from production in the billing layer, per-cohort hit rates largely the customer's own estimates round-tripped through a working cache, intake measured at n=20, the redesign never observed settling end-to-end, and latency measured from a laptop across regions. What this run proves is mechanism — the cache, attribution, pricing, and settlement gates behave as specified. Every dollars-per-month figure is an extrapolation and is tagged as such.
