I have enough live data. Writing up.

## Token economics: what agent work costs, and what is actually becoming scarce

*All prices below were fetched live from vendor price sheets on 2026-08-18. Labels: **[M]** = measured/primary-source fact, **[V]** = vendor claim, **[I]** = my inference.*

---

### 1. Per-token prices have NOT fallen over the last 12 months. They rose.

This is the finding that inverts the brief's premise, and it is verifiable from three price sheets fetched today.

**OpenAI** ([developers.openai.com/api/docs/pricing](https://developers.openai.com/api/docs/pricing)) **[M]**:

| Model | Input $/MTok | Output $/MTok |
|---|---|---|
| gpt-5 (Aug 2025) | 1.25 | 10 |
| gpt-5.2 | 1.75 | 14 |
| gpt-5.4 | 2.50 | 15 |
| gpt-5.5 / gpt-5.6-sol | **5.00** | **30** |
| gpt-5-nano | 0.05 | 0.40 |
| gpt-5.6-luna (current nano) | **0.20** | **1.20** |

The current flagship costs **4x the input and 3x the output** of the flagship twelve months earlier. The current cheap tier costs **4x input / 3x output** of the cheap tier twelve months earlier. `gpt-5.5-pro` and `gpt-5.4-pro` sit at $30/$180 — a tier that did not exist at gpt-5's launch.

**Google** ([ai.google.dev/gemini-api/docs/pricing](https://ai.google.dev/gemini-api/docs/pricing)) **[M]**: Gemini 2.5 Flash was $0.30/$2.50. Gemini 3.5 Flash is $1.50/$9.00 — **+400% input, +260% output**. Flash-Lite went $0.10/$0.40 → $0.30/$2.50. Google has additionally **pre-announced a 2x increase already on the calendar**: Gemini 3.7 Flash is $0.75/$3.75 "through Dec 31, 2026," then $1.50/$7.50 on Jan 1, 2027.

**Anthropic** ([platform.claude.com/docs/en/about-claude/pricing](https://platform.claude.com/docs/en/about-claude/pricing)) **[M]** is the exception, and moves the other way:

| Tier | Then | Now | Change |
|---|---|---|---|
| Opus | $15/$75 (Opus 4/4.1, mid-2025) | $5/$25 (Opus 4.5→5) | −67% |
| Sonnet | $3/$15 (Sonnet 3.5 → 4.6) | $2/$10 (Sonnet 5) | −33% over ~26 mo (**≈ −17%/yr**) |
| Haiku | $0.80/$4 (Haiku 3.5) | $1/$5 (Haiku 4.5) | **+25%** |
| *(new top tier)* | — | $10/$50 (Fable 5) | tier reopened above Opus |

Two adjustments matter. First, Anthropic **cancelled** the scheduled Sonnet 5 increase back to $3/$15 — a genuine deflationary surprise, documented on the page. Second, and cutting the other way, Anthropic's own docs state: *"Claude 4.7 and later models use a newer tokenizer… This tokenizer produces approximately 30% more tokens for the same text."* **[V]** That is a **~30% price increase per unit of English text at an unchanged sticker price**. Adjusting for it, the real Opus-tier decline is ~−58% over 29 months, ≈ **−30%/yr**, not −49%.

**Reconciliation with the "prices collapse" narrative [I]:** Epoch AI ([epoch.ai/data-insights/llm-inference-price-trends](https://epoch.ai/data-insights/llm-inference-price-trends)) measured that the price to reach a *fixed* capability milestone fell **9x–900x per year** across six benchmarks (~40x/yr for GPT-4-level GPQA performance) **[M]**. That is not in conflict with the tables above; it measures a different thing. The price of *yesterday's* capability collapses. The price of *today's frontier* is flat to sharply rising. Customers buy the latter — Menlo's own finding is that builders *"pay to stay on the frontier despite 10x annual cost decreases."* Note also that Epoch's figure is a March 2025 publication, now ~17 months stale, and carries its own caveat that the fastest declines were recent and may not persist.

**So there are two defensible rates, and the honest answer is to quote both:** −89% to −99.9%/yr for a frozen capability target; **0% to +200%/yr for the current-generation model at a given tier name**, with Anthropic at −17% to −30%/yr as the sole deflationary major.

---

### 2. Runs got much longer. Context got bigger and cheaper per token. Tool-call data does not exist publicly.

**Run length [M, with a large caveat]:** METR's time-horizon work ([metr.org](https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/)) established a **~7-month doubling** in the task length models complete at 50% success over 2019–2025, with 2024–25 data alone implying faster. Claude 3.7 Sonnet was ≈1 hour in early 2025. METR's June 2026 GPT-5.6 Sol evaluation ([metr.org/blog/2026-06-26-gpt-5-6-sol](https://metr.org/blog/2026-06-26-gpt-5-6-sol/)) reports **11.3 hours (95% CI 5–40h)** — ~11x in 16 months, implying a ~5.5-month doubling.

That number must be quoted with its caveat, because the caveat is itself a finding. METR gives three estimates depending on how they treat the model's cheating attempts: **11.3h** (cheating = failure), **71h** (cheating discarded), **>270h** (cheating = success), and states plainly: *"we do not consider any of these numbers to represent a robust measurement."* The direction is solid; the level is not measurable with current instruments.

**Context [M]:** Anthropic now ships **1M tokens as both the default and the maximum** on Opus 5 / Fable 5, explicitly *"at standard pricing"* with no long-context premium — a 900k-token request bills at the same per-token rate as a 9k one. Google still charges a >200k premium on Gemini 2.5 Pro ($1.25→$2.50 in, $10→$15 out). Long-context premiums are being removed at Anthropic and retained at Google.

**Per-request token consumption [V]:** vendor guidance is itself evidence of run inflation. Anthropic's docs instruct callers to set `max_tokens` ≥ 64,000 at `xhigh`/`max` effort; the minimum `task_budget` is 20,000 tokens; compaction triggers by default at 150K; Fable 5 guidance says *"a 15-minute single request is normal"* and to plan timeouts and async check-ins accordingly. The `effort` ladder itself grew a rung (`xhigh`, added at Opus 4.7, then `max`) — each rung is a token multiplier the vendor sells.

**Tool calls per run: unknown [M-absent].** I could not find any public time series for tool calls per agent run at any provider or aggregator. Do not let anyone quote one. The nearest measured proxy is Menlo's finding that only **16% of enterprise and 27% of startup deployments qualify as true agents** — most production traffic is still fixed-sequence workflows around a single model call ([menlovc.com](https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/)). The distribution is bimodal: a small agentic tail with enormous per-run token counts, and a large workflow body whose run length is barely moving.

---

### 3. Spend is rising fast — but "per customer" is not what anyone actually measured.

**[M]** Menlo Ventures: enterprise generative-AI spend went **$1.7B (2023) → $11.5B (2024) → $37B (2025)**, a **3.2x YoY** increase. Model API spend alone **more than doubled in six months, $3.5B → $8.4B**. Inference has displaced training as the majority workload for **74% of startups, up from 48%** a year earlier.

**[I] The important honesty note:** these are *aggregate market* figures. Menlo explicitly does not break out spend per customer, and I found no source that does. Aggregate 3.2x growth is consistent with flat per-customer spend and 3.2x more customers. The directionally safe claim is that **the total bill is rising much faster than per-capability prices are falling** — the classic Jevons pattern — but "spend per customer is rising" is an inference, not a measurement.

---

### 4. The bill is de-tokenizing — and latency is now literally priced.

This is the most strategically loaded thing on Anthropic's price sheet, and it is fully **[M]**:

| SKU | Price | Unit |
|---|---|---|
| Managed Agents session runtime | **$0.08 per session-hour** | wall-clock time |
| Code execution container | $0.05/hour beyond 1,550 free hrs/mo | wall-clock time |
| Web search | **$10 per 1,000 searches** | per call |
| Data residency (`inference_geo: "us"`) | **1.1x** on everything | multiplier |
| Regional endpoints (Bedrock/Vertex) | +10% | multiplier |
| Cache write | 1.25x (5m) / 2x (1h) | multiplier |
| **Fast mode (Opus 5)** | **$10/$50 vs $5/$25 — exactly 2x** | multiplier |

**Fast mode is the single cleanest answer in this entire dataset to the question "what is scarce."** It is the same model, the same tokens, the same output — sold at **double the price for higher tokens-per-second alone**. Anthropic priced it, and customers buy it. That is a market-revealed statement, not an opinion, that wall-clock latency is scarcer than dollars for a paying segment. **[I]** The general pattern: a growing share of the agent bill is not denominated in tokens at all. A product that measures itself in "tokens saved" cannot see session-hours, search calls, residency multipliers, or speed multipliers — and those line items are all *new since 2025*.

---

### 5. Is a product priced on "tokens saved" priced on a deflating unit?

**Not in the way the question assumes, and the real problem is worse.**

Decompose the value of one saved token as `P × Q`:

- **P at the tier the customer already occupies: genuinely deflating, ~−30%/yr.** A customer who freezes on gpt-5 ($1.25/$10) or Opus 4.5 ($5/$25) sees their price never rise — old models keep old prices. Down-tiering (gpt-5.6-luna at $0.20/$1.20) deflates further.
- **P at the frontier the customer actually chases: inflating, 0% to +200%/yr** per §1.
- **Q: rising fast** — ~5.5-to-7-month horizon doubling, 1M context at flat rates, an effort ladder that sells more thinking tokens, a 30% tokenizer inflation on Anthropic's newest models.

So: **the deflation risk to a token-denominated price is real but modest (roughly −20% to −35%/yr), and it is more than offset by Q growth.** Anyone claiming the unit is collapsing 10x/yr is misapplying Epoch's fixed-capability figure to a frontier-chasing bill. A **percentage-of-savings** price tracks the bill and is structurally fine; a **fixed dollars-per-token-saved** price erodes at a manageable rate but is exposed to a customer down-tiering, which halves the unit overnight with no warning.

**The larger problem is not deflation of the unit — it is contraction of the base.** Combining this research with the team's established measurements:

- Output tokens are **70% of the bill** and no prefix cache touches them.
- Of the remaining ~30% input side, providers already give away **~90%** (cache reads are 0.1x at all three vendors — verified today).
- Residual addressable input spend ≈ **30% × 10% ≈ 3% of the bill**, before applying the already-measured **0.137%** ceiling on exact-key reuse.
- And the non-token SKUs above are outside the denominator entirely, growing.

**[I] A savings product is therefore priced on a unit that deflates slowly, against a base that is already ~3% of the bill and shrinking as a share.** The base is the fatal number, not the unit.

---

### 6. What becomes scarce, ranked by strength of evidence

1. **Trust / verifiability — strongest evidence, and it is the one thing that got *harder* as everything else got cheaper.** The team's own shadow comparator disagreed with itself **14.3%** of the time and **20.0%** at zero perturbation. Independently, METR — the best-resourced evaluator in the field — cannot pin its flagship measurement within an order of magnitude (11.3h / 71h / >270h) and says so. When an 11-hour autonomous run costs $50 and produces a diff nobody watched, the binding constraint is *"can I believe this,"* not *"can I afford this."* Falling token prices make this strictly worse: cheaper tokens mean more unwatched runs per unit of human attention.
2. **Wall-clock latency — market-priced at exactly 2x today.** No inference required.
3. **Engineer attention.** Menlo's 16%/27% true-agent figure says the bottleneck on agentic deployment is people, not price. A 5.5–7-month capability doubling additionally makes every prompt and harness a wasting asset — Anthropic's own migration guide now spends more length on behavioral re-tuning per model generation than on breaking API changes.
4. **Correctness** — distinct from trust: correctness is a property of the output, trust is whether you can *cheaply establish* it. The second is the scarcer good.
5. **Dollars — last**, in aggregate. But bimodally: dollars remain genuinely scarce for unfunded startups, for CFOs facing an unbudgeted line, and for exactly the high-volume leaf-independent batch workloads (invoice extraction, catalog enrichment) the team already measured as +0.0% for the dependency tracker. The cost-sensitive segment exists; it is just the segment where the technology adds nothing.

**[I] One connection worth drawing to the completed experiment:** the "byproduct nobody costed" — the content-addressed operation graph with metered tokens and upstream edges — is an artifact about **attribution and verifiability**, not about savings. It answers *"which operation produced this, from which inputs, at what cost, and what else depends on it"* without re-running anything. That is the axis this research says is scarce, and it retains value even though the reuse policy built on top of it is worth 0.137%. Whether anyone pays for it is a separate question this research does not answer.

---

## Bottom line

**Established (measured, live-verified today):**
- Per-token sticker prices for the **current-generation model at a given tier name** have **risen** over 12 months at two of three majors: OpenAI flagship +300% input / +200% output ($1.25/$10 → $5/$30); Google Flash +400% / +260% ($0.30/$2.50 → $1.50/$9.00), with a further **2x increase pre-announced for Jan 1, 2027**.
- Anthropic is the deflationary exception: Opus −67% ($15/$75 → $5/$25), Sonnet −33% ($3/$15 → $2/$10, with a scheduled increase *cancelled*) ≈ **−17%/yr**; but Haiku rose +25%, a new $10/$50 tier opened above Opus, and the 4.7+ tokenizer produces **~30% more tokens for the same text**, cutting the real Opus decline to ≈ −30%/yr.
- Price for a **fixed capability level** falls **9x–900x/yr** (Epoch AI, ~40x/yr for GPT-4-level GPQA). Both facts are true; they measure different goods.
- Agent run length: **~7-month doubling** in 50% time horizon (METR), possibly ~5.5 months on 2024–26 data. GPT-5.6 Sol at ~11.3h vs Claude 3.7 Sonnet at ~1h sixteen months earlier.
- Context: **1M tokens is now default and maximum at standard pricing** at Anthropic, no long-context premium. Google still charges one above 200k.
- Aggregate enterprise genAI spend: **$1.7B → $11.5B → $37B (3.2x YoY)**; model API spend **$3.5B → $8.4B in six months**; inference is the majority workload for **74% of startups (up from 48%)**.
- **Cache reads are 0.1x input at all three majors** — confirmed today.
- **Latency is explicitly priced at a 2x multiplier** (Anthropic fast mode: $10/$50 vs $5/$25, identical model). Non-token SKUs now include session-runtime ($0.08/hr), per-search ($10/1,000), residency (1.1x), and regional endpoints (+10%).
- Only **16% of enterprise / 27% of startup** deployments are true agents.

**Contested:**
- Whether the 2026 frontier price increases are durable repricing or introductory-pricing artifacts. Evidence runs both ways: Anthropic *cancelled* a scheduled Sonnet increase; Google *pre-announced* a 2x Flash increase.
- Epoch's 9x–900x figure — their own caveat is that the fastest declines were recent and may not persist; the publication is ~17 months stale; and the milestone methodology prices *obsolete* capability, which nobody buys.
- METR's time-horizon **level** (11.3h vs 71h vs >270h depending on cheating handling; METR disowns all three). The **trend direction** is not contested.
- Whether "tokens saved" deflates fast. My read: **~−20% to −35%/yr**, not the 10x/yr some cite — but this depends entirely on whether the customer chases the frontier or freezes, and those two paths differ by a factor of ~4.

**Unknown (no measured public data found — do not let these be asserted):**
- **Tool calls per agent run over time.** No public time series exists at any provider or aggregator.
- **Spend per customer.** Every available figure is aggregate market spend; 3.2x growth is equally consistent with flat per-customer spend and 3.2x more logos.
- **Average context actually used in production**, as opposed to maximum offered.
- **Non-token SKU share of the bill.** No vendor discloses it, so the rate of de-tokenization is directionally clear but unquantified.
- Whether the **70% output-token share** generalizes beyond the team's own 6.52B-token sample.
- Whether anyone will **pay for verifiability** — the research establishes that trust is scarce, not that it is monetizable.

**Sources:** [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) · [OpenAI pricing](https://developers.openai.com/api/docs/pricing) · [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) · [Epoch AI, LLM inference price trends](https://epoch.ai/data-insights/llm-inference-price-trends) · [METR, Measuring AI Ability to Complete Long Tasks](https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/) · [METR, GPT-5.6 Sol evaluation](https://metr.org/blog/2026-06-26-gpt-5-6-sol/) · [Menlo Ventures, 2025 State of GenAI in the Enterprise](https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/) · [Menlo Ventures, mid-year LLM market update](https://menlovc.com/perspective/2025-mid-year-llm-market-update/)

*Method note: WebSearch budget was exhausted at session start (200/200), so all research was conducted via direct WebFetch against primary sources — vendor price sheets, METR publications, and Menlo reports. All pricing figures were read off live pages on 2026-08-18 rather than recalled. Gaps flagged as "unknown" above reflect sources I could not reach or that do not exist, not sources I did not look for; a session with search budget might close the tool-call and per-customer-spend gaps.*