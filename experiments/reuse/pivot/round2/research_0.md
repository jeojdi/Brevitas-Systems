## EVAL & QUALITY BUDGET — VERDICT

**The budget exists, is ~10–50× larger as a share of bill than the dead caching thesis, and has no independent owner. It is a line item inside the observability budget, and that budget's owner is Datadog/Dynatrace. Every independent player that cleared an exit was absorbed by a provider or an infra incumbent — including, decisively, by OpenAI.**

### Rule 2 first: the size

Measured list prices (Aug 2026), eval/observability cost as % of the model spend on the same call:

| Vendor | Unit price | small step (2k/300) | typical (10k/1k) | heavy (60k/2k) |
|---|---|---|---|---|
| Datadog Agent Obs | $3.50/10k spans | 3.3% | **0.78%** | 0.17% |
| Arize AX Pro | $50/mo ÷ 50k spans | 9.5% | 2.2% | 0.48% |
| Braintrust Pro | $1.50/1k scores | 14.3% | 3.3% | 0.71% |
| Galileo Pro | $100/mo ÷ 50k traces | 19.0% | 4.4% | 0.95% |
| Patronus | $10/1k evaluator calls | 95.2% | 22.2% | 4.8% |

*(model side = Sonnet-class $3/$15 per MTok; my arithmetic on their published prices)*

**Measured fact:** eval/observability lands at **0.2%–20% of the model bill** — one to two orders of magnitude above the 0.137%–0.36% figures that killed the reuse thesis. This is a real budget, not a rounding error. That is the honest good news.

**Inference (from public disclosure):** Dynatrace ARR was **$2.136B** (Q1 FY27, 5 Aug 2026) and says Arize adds **~200bps to ARR growth** → **implied Arize ARR ≈ $43M**, at **$915M ≈ 21× ARR**. So the category leader, after ~5 years, is a ~$43M ARR business. Venture-scale *exit*, yes. Venture-scale *compounding standalone*, no.

### Who signs the cheque

The **AI engineering / platform team**, from the engineering-tools or observability budget. Not risk, not compliance. Evidence: Arize's own framing is "Built for AI Engineers"; it sold to an **APM vendor**; Datadog sells evals as a SKU adjacent to APM. No vendor in this set sells to a CISO or a General Counsel — the ones who tried moved out (below).

### The exits — every one is absorption

| Company | Outcome | Date | Terms |
|---|---|---|---|
| **Arize** | → **Dynatrace** | 13 Aug 2026 | **$915M** ($815M cash + replacement equity) |
| **Weights & Biases / Weave** | → CoreWeave | 2025 | ~$1.7B |
| **Promptfoo** | → **OpenAI** | 9 Mar 2026 | undisclosed |
| **Patronus** | pivoted *out* of eval | 25 Jun 2026 | $50M B (Greenfield, Lightspeed, **Datadog**, Samsung) |
| **OpenAI Evals** | **shut down** | announced 3 Jun 2026 | read-only 31 Oct 2026, dead 30 Nov 2026 |
| Braintrust | still standalone | 17 Feb 2026 | $80M B, ICONIQ (a16z, Greylock, Elad Gil) |
| Galileo | no round since Series B | Oct 2024 | ~22 months stale |

### Rule 1: fires at maximum strength

The eval primitive ships free, at feature parity, from four directions:
- **Langfuse** — self-host free, explicitly *"full feature parity with paid Cloud plans"*; LLM-as-judge, datasets, human annotation all included, no separate eval charge.
- **Opik (Comet)** — OSS, *"core AI observability and evaluation feature set is included free in the source code,"* 30+ judge metrics.
- **Phoenix (Arize)** — free OSS.
- **Promptfoo** — *"Free Forever… All LLM evaluation features."* **Now owned by OpenAI.** 350k+ developers, 130k MAU, 25%+ of the Fortune 500.

**The provider both killed its own paid eval product and bought the free one.** OpenAI's official migration path off its deprecated Evals platform is *"Moving from OpenAI Evals to Promptfoo."* Per Rule 1, generic eval tooling: **stop.**

### What buyers refuse to buy

- **Per-seat eval.** Braintrust, Arize, Galileo all charge $0 for seats — unlimited users on every tier. Only LangSmith holds a seat price ($39), and it just re-based onto compute units (LCU $1.50 / LSU $1.00).
- **Full-coverage LLM-judge at list price.** The vendors say so themselves: LangSmith Tuned Evaluators (18 Aug 2026) sells *"reducing evaluation cost by 82%"*; Galileo Luna sells *"96% lower cost."* **A category whose vendors compete on being 82–96% cheaper within one product cycle is deflating, not growing.**
- **Tooling at all, if sophisticated.** Uber built in-house on top of Arize tracing: *"the hardest part of evals isn't the tooling"* — it's *"designing the defaults, ownership models, and feedback loops."* The tool is the cheap half.
- **Self-serve, upmarket.** Humanloop has removed its paid self-serve tier entirely: free trial → Enterprise-only.

### Is anyone buying *independent* assurance? Mostly no — and the counter-evidence is brutal

This is the sharpest test of your structural principle, and **in eval it fails**:

- **Patronus** was the flagship independent-assurance play (FinanceBench, Lynx, Percival). It raised $50M in June 2026 **to leave**. Hero copy is now *"Simulating the World's Intelligence… human-aligned AGI."* It sells training environments to labs — it became a supplier to the party it used to audit.
- **Promptfoo** was the most-adopted independent evaluator and red-teamer on earth. **It is now owned by a model provider.** The independence that was supposedly the product was the thing that got sold.
- **Arize** independence ended at an APM vendor.

Where independent assurance *does* clear a cheque, it is not software: **accredited certification bodies** for **ISO 42001** (Schellman is the first ANAB-accredited CB; buyers are cloud/fintech/healthcare/SaaS). That is a people-heavy audit-services business, not venture-scale. And its regulatory forcing function **slipped**: the Digital Omnibus (adopted 19 Nov 2025, political agreement 7 May 2026, in force 27 Jul 2026) pushed EU AI Act high-risk obligations from **2 Aug 2026 → 2 Dec 2027** (Annex III) and **2 Aug 2028** (embedded products). The mandate is 15–24 months out, not now.

**One live seam, stated as a budget observation, not a product:** Gartner published a **Magic Quadrant for AI Governance Platforms in 2026** (Holistic AI = Challenger; Credo AI a Forrester Wave Leader Q3 2025). *Governance* is a named category with a named budget owner (CDO/risk/compliance). *Evaluation* is not — it has no MQ, no owner, and no cheque of its own. Note also that the vendors are voting with their feet toward **control**: Galileo's homepage is now *"Don't just monitor AI failures. **Stop them**"* — offline evals becoming production **guardrails**; Promptfoo gives eval away and monetizes **red-team probes**; Braintrust's July 2026 standard is about *"supervising"* agents. Consistent with your Portkey/Chronosphere pattern: measurement is free, **interdiction is paid**.

### How contested

Maximally. ~10 funded vendors + 4 OSS at parity + 3 incumbents bundling (Datadog, Dynatrace-Arize, CoreWeave-W&B) + the framework vendor bundling (LangSmith) + **the model provider owning the free standard**. Datadog has hedged both ways — it invested in Braintrust's Series A *and* Patronus's Series B *and* sells the SKU itself.

### Bottom line against your three rules

- **Rule 1:** fails outright for generic eval tooling — free from OSS *and* from OpenAI.
- **Rule 2:** the prize is real (0.2–20% of bill) but the leader is ~$43M ARR and prices are falling 82–96% per cycle. Growing usage, shrinking revenue per unit.
- **Rule 3:** passes trivially — none of this needs your existing code, which is also why none of it is yours by right.

**Honest answer:** this budget exists but is not independently ownable. It is being annexed into observability (Dynatrace/Datadog) and security (Promptfoo→OpenAI, Galileo→guardrails). The *independent assurance* budget you hoped to find is real but sits in accredited certification, is services-shaped, and its regulatory trigger just slipped to Dec 2027. If anything here is worth a second look, it is the seam OpenAI just created by buying the world's most-used independent AI red-teamer — but that is a 2027 regulatory bet, not a 2026 engineering-budget bet.

Sources: [Braintrust pricing](https://www.braintrust.dev/pricing) · [Braintrust Series B](https://www.braintrust.dev/blog/announcing-series-b) · [LangSmith pricing](https://www.langchain.com/pricing-langsmith) · [LangSmith Tuned Evaluators](https://www.langchain.com/blog/introducing-langsmith-tuned-evaluators-starting-with-perceived-error) · [Arize pricing](https://arize.com/pricing/) · [Dynatrace–Arize $915M](https://ir.dynatrace.com/news-events/press-releases/detail/435/dynatrace-to-acquire-ai-observability-leader-arize) · [Dynatrace Q1 FY27 ARR](https://ir.dynatrace.com/news-events/press-releases/detail/434/dynatrace-reports-fourth-quarter-and-fiscal-2026-financial-results) · [Galileo pricing](https://galileo.ai/pricing) · [Galileo positioning](https://galileo.ai/) · [Patronus Series B](https://www.patronus.ai/blog/announcing-our-50m-series-b) · [Patronus homepage](https://www.patronus.ai/) · [OpenAI deprecations](https://developers.openai.com/api/docs/deprecations) · [Promptfoo → OpenAI](https://www.promptfoo.dev/blog/promptfoo-joining-openai/) · [Promptfoo pricing](https://www.promptfoo.dev/pricing/) · [Langfuse pricing](https://langfuse.com/pricing) · [Opik](https://www.comet.com/site/products/opik/) · [Datadog price list](https://www.datadoghq.com/pricing/list/) · [W&B pricing](https://wandb.ai/site/pricing/) · [Humanloop pricing](https://humanloop.com/pricing) · [Uber evals case study](https://arize.com/blog/how-uber-evaluates-ai-agents-at-production-scale/) · [EU AI Act / Digital Omnibus](https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai) · [Schellman ISO 42001](https://www.schellman.com/services/iso-certifications/iso-42001) · [Holistic AI](https://www.holisticai.com/) · [Credo AI](https://www.credo.ai/)