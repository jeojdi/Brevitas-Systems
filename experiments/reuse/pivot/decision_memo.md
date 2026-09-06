# DECISION MEMO: what to do after the reuse thesis died

**To:** founder
**Date:** 2026-08-17
**Bottom line:** None of the eight is a company. Seven of them are dead or already shipped by someone else for free; one is a real feature inside a product you don't own. The single most valuable thing you built in the last few months is not the graph — it's the instrument that reads real provider receipts off real traffic and produces defensible dollar numbers. That transfers. The graph mostly does not. If you insist on a company, I name one bet below; it is adjacent to two of your candidates, it is **not** one of them, and it has not been adversarially attacked, so discount it accordingly.

---

## 1. Ranking, after applying the kills

| # | Candidate | Verdict | The kill that landed, and the markdown |
|---|---|---|---|
| 1 | **Agent execution graph / blast radius** | Feature, not company | **Marked down hard.** Braintrust already ships hill climbing (paired comparison against a recorded prior run) and `trialCount` (variance by repetition) inside the $249/mo SKU the buyer already pays. LangGraph already ships content-keyed node caching *and* time travel, free — so the "content-addressed op identity" you thought was yours is OSS. And your own +0.0% applies: eval corpora are leaf-independent by construction, which is the shape where the transitive tracker is provably inert. The go/no-go number (paid-op-with-paid-ancestor share on real traces) is still unmeasured, and the nearest proxies are 0/12 and 1/13. |
| 2 | **Cost attribution for agent fleets** | Feature, not company | **Marked down hard.** Revenium ships tool-call costing *and* per-customer margin analysis today with a free 100k-transaction tier — that is your 90-day wedge, already for sale. Vantage published an open Token Cost Allocation Specification with arbitrary tags, so the "no standard exists" gap closed while the proposal was being written. Price anchor is $200/mo. |
| 3 | **Provenance / attestation** | Feature, barely | **Marked down to near-zero.** AWS CloudTrail has shipped hash-chained, RSA-signed, previous-digest-linked operation logs with a CLI verifier since 2015, free. S3 Object Lock COMPLIANCE mode is the tamper-evidence form regulators have actually assessed (Cohasset, SEC 17a-4/FINRA). No mandate names cryptographic AI logs, and the AI Act's Article 12 obligations moved to Dec 2027 / Aug 2028. Only survivor: the retention inversion (7-year content-free lineage beside 90-day content deletion), which no buyer has been observed to ask for. |
| 4 | **Behavioural regression across model versions** | Dead as proposed | **Killed on the product, not the pain.** There were no live retirement clocks — "not sooner than" is a launch-time floor, not a notice. `count_tokens` is free with a published two-model diff recipe, and `/claude-api migrate` automates the param/prefill/effort fixes. Four of six report line-items are already first-party and free. Note for later: the *pain* is real and recurring; the *product* is a free endpoint. |
| 5 | **Replay debugging for nondeterministic agents** | Dead | **Killed twice.** The residual wedge (re-run step N K times against a measured noise floor) is Braintrust `trials` + hill climbing, shipped and documented. And the enabling defect is being fixed upstream: batch-variant kernels are the documented cause of temperature-0 nondeterminism, and vLLM ships `VLLM_BATCH_INVARIANT=1`. You'd be short-selling infrastructure quality. |
| 6 | **Wall-clock incrementality** | Dead | **Killed on provenance of its own headline number.** "Tool ops run 30–300s, measured" was a recon assumption from `scenarios.json`, self-rated Medium confidence, not a measurement. Meanwhile resume ships free in LangGraph, Inngest, Cloudflare Workflows and Claude Code itself, and Earthly — better positioned, hermetic substrate — publicly shut down because customers compared it to free. |
| 7 | **Single-flight concurrent fan-out** | Dead, and now *definitively* | **The cleanest kill in the packet, and it's a new measurement.** On 11,613 real billed requests / 2.6B prompt tokens, recoverable value at the window your own probe supports (≤1s) is **0.10%–0.34% of the bill** — *below the 0.137% cold-share ceiling that you already used to declare the original thesis dead*. 83% of sub-second bursts are N=2, where the arithmetic gives 46%, not the 73.6% quoted. And the free alternative is deleting the `cache_control` marker (N×1.0 instead of N×1.25). Also: LiteLLM main already has receipt-derived cache savings dollarization and cache-aware routing, which retires two of your standing audit findings. |
| 8 | **Compute-once over public corpora** | Dead | **Killed on price and on churn.** The public-derived-facts tier clears at $0 in every vertical (Socket, Context7, OSV, ecosyste.ms, CourtListener), because it's the acquisition surface for a private-data or per-seat product. And npm publishes ~16.4k new versions/day ≈ 8.5% of the registry per month — your 9% change rate reproduced on the corpus you picked as easiest. Dagster ships the hash-skip pipeline free. |

**Where a killer over-reached, for the record:** the fan-out kill's claim that the measurement instrument is worthless is wrong — it is the best thing in the packet. The pivot-2 kill is right that Braintrust ships variance control, but wrong to treat trials as equivalent to a *provider-side* comparator; trials compares your app to your app. Hold that thought.

---

## 2. The menu's shared defect

All eight candidates start from the sunk asset. Six of them route back to the same fact that killed the original thesis — **+0.0%: a changed-file list produces the identical execution set** — wearing different clothes. That is not eight independent bets. It is one bet, re-argued eight times, and it has already been decided.

Two facts from the research change the frame more than any candidate does:

**(a) The unit you priced in is dissolving.** Frontier prices are *rising*, not falling — OpenAI's flagship went $1.25/$10 → $5/$30 in twelve months, Gemini Flash +400% input. Only capability-adjusted prices collapse. More importantly the bill is **de-tokenizing**: Anthropic now sells session-runtime at $0.08/session-hour, web search at $10/1k calls, a 1.1x residency multiplier, and — the single most revealing line item anywhere — **fast mode at exactly 2x for the same model, same tokens, same output.** Latency is now a priced good with a market-revealed 100% premium. Any product that measures itself in "tokens saved" is structurally blind to the fastest-growing part of the invoice.

**(b) The absorption precedent says the surviving move is *up*, not *sideways within the commodity*.** Nobody who survived still sells the commoditized unit. Akamai sells security ($604M, +10%) while delivery declines (−6%). Gradle renamed itself Develocity and put **Governance** first on the page. Chronosphere sold *reduction of the incumbent's bill* to a security buyer for $3.35B. PolyScale — transparent database query caching, the closest structural analogue to your reuse layer — is a domain listed at $8,195. Meanwhile OpenRouter sold to Stripe at >$7B not as a router but as a payment rail with a 5.5% take rate, and Portkey sold to Palo Alto as a security control plane. **The money in 2026 went to whoever owns a control point or sits in the CISO budget.** You own neither, and none of the eight gets you one.

---

## 3. The bet

**None of the eight.** The one I would bet on is adjacent to #4 and #8 and is neither:

> **A cross-tenant provider-behaviour panel: continuous, standardized canary suites run against *pinned model IDs* across providers and endpoints, detecting and dating behaviour change that the provider does not announce and structurally cannot self-report.**

Why this and not the menu:

- **The pain is documented by the vendors themselves.** Anthropic's own docs state that at a fixed model ID, "the serving infrastructure around the model can change over time... Occasionally, infrastructure updates produce minor differences in observable behavior even when the model ID and weights have not changed." Their own postmortem records a routing bug affecting **16% of Sonnet 4 requests and ~30% of Claude Code users** — and says plainly that their internal evals "simply didn't capture the degradation users were reporting." Azure auto-upgrades Global Standard deployments via a property most teams never set. Gemini repoints `-latest` aliases on two weeks' email notice.
- **The cheapest DIY detector was just removed by fiat.** `temperature`, `top_p` and `top_k` return 400 on Claude 4.7+. Pin-temp-zero-and-diff-bytes is dead on the frontier. That is a real, dated tailwind.
- **Every funded vendor frames the diff as "you changed your app."** LangSmith, Braintrust, Datadog, Arize — all compare your version to your version, on your dataset. Nothing funded frames it as "they changed under you." The only people doing that are unfunded hobbyware sitting at 1–3 HN points.
- **It is the only thing here where cross-tenant scale is the product, not a cost structure.** A single customer with n=1 cannot separate a provider change from their own sampling noise. A panel across many customers and a public canary corpus can. Providers structurally cannot build it (self-reporting), and individual customers structurally cannot (no panel).
- **Your sunk asset is *not* load-bearing here** — which is the correct test. What transfers is the instrument, not the graph.

### 90-day wedge

Not a platform. One artifact and one service.

1. **Publish a dated, reproducible drift report.** Run a fixed canary suite every 3 hours against 3 providers × ~6 pinned model IDs × 2 endpoints for eight weeks. Publish the method, the per-suite noise floor, and every dateable change event with confidence bounds. Free. This is distribution and credibility, and you are unusually well qualified to be believed because you have publicly killed your own thesis with data before.
2. **Sell incident attribution during live incidents, at $10–15k a piece.** "Your agent got worse on Tuesday. Was it you or them?" That is the question with a pager attached, and today nobody can answer it. Each engagement feeds the panel.

The 90-day success test is not ARR. It is: **≥6 dateable change events at fixed model ID, and ≥2 teams paying cash for attribution during an incident they were already having.**

### First ten customers, precisely

Teams where a behaviour change at a pinned model ID is a *customer-visible incident*, not a quality metric:

1–2. AI coding-agent vendors (Cursor/Replit/Cognition class) — their users notice degradation within hours and blame them.
3–4. Voice/chat agent vendors in support and insurance (Sierra/Decagon/Coval-adjacent) — a tool-call format change breaks live calls, with contractual quality terms.
5. Vertical legal or financial AI (Harvey class) — output changes have client-facing consequences.
6–7. Two enterprises on Azure AI Foundry **Global Standard** deployments, where auto-upgrade is on by default and nobody set `versionUpgradeOption`. This is the most under-defended population in the entire research packet.
8. An agent platform running multi-provider routing (35%-of-F500 class) whose SLAs are downstream of provider behaviour.
9–10. **Gateways and routers themselves** (OpenRouter/Portkey/LiteLLM class). They would route away from a drifting endpoint if they could detect one. They are your best customers, your most likely acquirer, and your most likely competitor — in that order.

### Pricing

- Public feed: **$0** (distribution).
- Team: **$1,500/mo** — your own prompts as private canaries, alerting, per-endpoint attribution.
- Enterprise: **$40–75k/yr** — private suites, region/endpoint coverage, and a written attribution artifact usable in an internal incident review.

Anchor to the incident/reliability budget (PagerDuty + Datadog adjacency), **not** the $39/seat eval line. You cannot win a price fight against a $0 free tier in the eval aisle, and you don't have to: this is bought by whoever gets paged, not whoever owns quality metrics.

### The single riskiest assumption

**That a real behaviour change at a fixed model ID is separable from sampling noise at a sample cost below the price you can charge.** You measured 20.0% self-disagreement at zero perturbation and failed to build a comparator that separates cosmetic from substantive divergence — 6 of 9 observed disagreements were a nondeterministic ```json fence. That failure is not a footnote here; it is the central technical risk, and it is the same wall you already hit. Everything else is execution.

Second risk, commercial: the Downdetector problem. Free public signal may be so good that nobody pays for the private tier.

### Pre-committed kill gates

- Eight weeks of continuous canaries across three providers produce **zero** dateable change events → nothing to sell. Stop.
- Change events detected, but no team pays $10k for attribution during a live incident → it's content, not a company. Stop, publish, take the credibility.
- Artificial Analysis (the nearest existing occupant — continuous cross-provider benchmarking, already published, monetization visibly thin) or any gateway ships endpoint-level drift alerting → over.

**Honest probability this is venture-scale: ~15%.** Probability it is a real $1–3M/yr business plus a credible acquisition path into a gateway or observability vendor: maybe 40%. I am naming it because it is the best bet available, not because it is a good one, and because — unlike every candidate on the menu — it has not yet been shot at.

---

## 4. What transfers, and what to throw away

**Transfers (this is the whole inheritance):**

- **The instrument.** Reading provider usage receipts out of real agent transcripts to price real billed traffic — 6.52B prompt tokens, 1,818 transcripts, 11,613 deduped billed requests, live probes at ~$1 apiece. Almost nobody has this. It is why you could measure a competitor's category at 0.10–0.34% of a bill in an afternoon.
- **Provider-behaviour knowledge, with a short shelf life.** Refresh-on-read on the 5m tier, workspace scoping, cache_creation vs read billing, the 1h cliff, tokenizer +30%, sampling params 400ing, fast mode at 2x, session-hour billing. Perishable — six months, maybe — so spend it now.
- **Methodology, not numbers.** Measuring a per-workload noise floor before making a claim about it is a discipline no vendor practices. Keep the practice; stop quoting 20.0% as a constant (it is one instrument, one workload family, no n, no CI — and it exceeds your 14.3% overall rate, which is itself a red flag about the instrument).
- **The habit of publishing against interest.** This is worth more than any code you wrote. It is what makes a design partner take a call and what makes an acquirer trust a number.

**Throw away, without ceremony:**

- **The transitive dependency tracker.** +0.0% on leaf-independent work, worse than naive on chains and DAGs. Six of the eight candidates leaned on it and six died on the same fact. It is a technically correct artifact with no market.
- **The 45 recorded graphs.** They are simulator output with `"tokens": {}` empty and `wall_ms: 0`. They are not production telemetry and they do not constitute a head start.
- **The reuse store and cache-key engine.** Ceiling 0.137%. Done.
- **All savings copy.** "We cache your agent's computation and save you money" must never appear again, including as a secondary claim. It poisons every sale to a technical buyer.
- **"Receipts-grade."** Until an actual invoice has been reconciled, that word is a liability.
- **The belief that your differentiation is an algorithm.** It is an instrument and a habit.

---

## 5. A real business vs. a real feature — the test, applied

A **feature** is anything where: (a) the buyer already pays a vendor whose product it plugs into, (b) its value is capped by that vendor's price, (c) the vendor can ship it in one changelog entry, and (d) nothing compounds outside a single customer's tenant.

A **business** requires all three of: a budget line owned by someone who gets *paged*; value denominated in something the incumbent's meter cannot see; and something that compounds across customers that neither any single customer nor the platform can assemble alone.

Applying it, without softening:

- **Blast radius / execution graph:** feature. Fails (a), (c) and (d). Braintrust holds the traces, the datasets, the CI hook and the buyer.
- **Cost attribution:** feature. Fails (a) and (c) — Vantage published the schema you would have emitted.
- **Provenance:** feature, and a free one. Fails (c) catastrophically — the primitive shipped in 2015.
- **Model migration, replay, wall-clock, fan-out, public corpora:** not even features. They are already shipped, or free, or measured at zero. There is no unoccupied slot to fill.
- **The drift panel:** *possibly* a business, on exactly one hinge — whether the cross-tenant panel compounds. If it does, no platform can copy it (self-reporting is disqualifying) and no customer can build it (n=1). If it doesn't, it is a status page with a blog, which is a fine content asset and not a company. That hinge is testable in eight weeks for a few thousand dollars of inference.

---

## 6. The decision you actually have to make

You are not choosing among eight pivots. You are choosing between two games, and you should choose deliberately rather than drift:

**Game A — take the credibility and stop.** Publish the field measurements (they are genuinely novel: the burst analysis, the 97.47% field hit rate, the TTL cliff at 1h, the cold-share anti-correlation). Use them as the strongest technical résumé in this niche, and either do $15–40k engagements or join a company that already owns a control point. This is the highest-*certainty* outcome available and it is not a failure; it is the correct read of a market where the platform gives away 90% of the input side and the survivors all moved up into governance.

**Game B — make the drift bet**, on the terms above, with the kill gates pre-committed in writing *before* you start, funded by attribution engagements rather than by capital.

What you should not do is Game C: build any of the eight because the code is already half-written. That is the sunk-cost move, and your own pivot-2 memo already named it — "sunk assets are the worst reason to pick a market." That sentence is the most valuable line in 40,000 words of proposals. Act on it.