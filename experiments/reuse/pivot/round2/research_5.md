# Inference Procurement: Budget Size, Owner, and Contestedness

**Research date 2026-08-19. Every claim tagged [MEASURED] (primary doc or dated report), [VENDOR] (interested party's own number), or [INFERENCE] (my reasoning).** Note: this session's WebSearch quota was exhausted before I started; all findings below come from direct fetches of primary docs, publisher pages, and Google News RSS. Two intended checks failed (Gartner newsroom 403, openai.com 403) and are marked.

---

## HEADLINE ANSWER

**"Inference procurement" is not yet a budget. It is a purchasing *act* performed inside two budgets that already exist and are already owned:** (1) the cloud committed-spend budget, owned by procurement + cloud FinOps, already fully instrumented by the hyperscalers; and (2) a token-cost-management budget forming right now under FinOps/CFO, currently spent on **visibility**, not on procurement.

**There is no evidence of a budget for a commercially intermediating party between the enterprise and the provider — and there is a structural reason, not just a timing reason: the hyperscalers already are that intermediary, and they pay the customer to use them.** Buying Claude through Bedrock/Foundry draws down an existing EDP/MACC commitment the enterprise has already promised to spend. Microsoft's own doc states that for Azure-benefit-eligible marketplace offers, "100% of the pretax purchase amount also contributes toward your commitment" [MEASURED]. A new intermediary is not a discount — it is a *new* vendor, a *new* PO, a *new* security review, and spend that no longer burns down the commit. It starts at a negative.

That is the Rule-2 answer, and it was cheap to get.

---

## 1. WHO SIGNS THE CHEQUE

**Not procurement.** Procurement appears in the 2026 record as a *bottleneck being routed around*, not a buyer: Anthropic launched Claude Marketplace in March 2026 with coverage framed explicitly as targeting "AI procurement bottlenecks" (InfoWorld, 2026-03-09) [MEASURED, headline-level].

The actual signing chain, by instrument:

| Instrument | Who actually executes | Evidence |
|---|---|---|
| Azure PTU reservation | Cloud platform / FinOps role with reservation-purchase rights | Azure doc explicitly warns "the Azure role and tenant policy requirements to purchase a reservation differ from those needed to create a Foundry deployment or resource" [MEASURED] |
| Bedrock Provisioned Throughput | Whoever owns the AWS account; MU pricing and limits are set "contact your AWS account manager" [MEASURED] |
| EDP / MACC commitment | Procurement + CFO, on a 1–3yr cycle, negotiated once | [MEASURED for MACC mechanics; INFERENCE for the cycle] |
| Token spend day to day | Platform/infra engineering; FinOps reports it | FinOps Foundation made token economics the FinOps X 2026 Day 1 keynote [MEASURED] |
| Escalation / approval | CFO | Bain: 42% of CFOs planning AI spend increases above 30% (Apr 2026); WSJ *"Tokenomics: A CFO's Guide to Governing the AI P&L"* (2026-05-05); Deloitte, EY, McKinsey, BCG all shipped token-economics-for-CFOs pieces in H1 2026 [MEASURED, headline-level] |

**[INFERENCE, high confidence]** The cheque-signer for anything new in this space is the platform/infra owner with FinOps as co-signer and CFO as approver. Selling this as a *procurement* product means selling to the one function in the chain that has no budget of its own and whose job is to say no.

**One live tell that no owner has consolidated yet:** the Linux Foundation announced intent to launch a **Tokenomics Foundation** on 2026-06-03 and launched it 2026-08-04, to "define the economics and ROI of AI value" [MEASURED]. Standards bodies form when there is no agreed unit of account. There is not one yet.

---

## 2. SIZE AND GROWTH OF THE MONEY

**The relevant line — what enterprises pay for model inference — is roughly $12.5B (2025), growing ~3x/yr.**

- Menlo Ventures: enterprises spent **$37B on generative AI in 2025**, up from $11.5B in 2024 (**3.2x**). Of that, infrastructure was $18B, and **foundation-model APIs specifically were $12.5B** [VENDOR — VC estimate, not audited].
- Provider share of that API spend: Anthropic 40%, OpenAI 27%, Google 21%, others 12% [VENDOR].
- **[INFERENCE]** At a similar multiple, 2026 enterprise model-API spend lands around **$35–45B**. Call it order-$40B.

Macro tide, for context only — do **not** confuse these with the addressable line:
- Gartner: **$2.5T** total worldwide AI spending in 2026, growing 47% (Jan and May 2026 releases; multiple outlets confirm the headline number — *Gartner newsroom returned 403, so I could not verify the segment split*) [MEASURED at headline level, UNVERIFIED at segment level]. This figure is dominated by servers, devices, and silicon. It is not a procurement budget anyone signs for inference.
- IDC: AI infrastructure spend ~$90B in Q4 2025 alone; **AI compute spend to $702B by 2029** [MEASURED, headline-level].

**What a toll on inference is actually worth today, measured:** OpenRouter — the only pure toll-taker in the space with public numbers — reportedly runs **~$140M/yr revenue at a 5.5% take rate on ~25T tokens/week**, and Stripe is reportedly acquiring it for **>$7B** (TechCrunch 2026-08-16; The Register 2026-08-17) [MEASURED, press-reported]. One outlet's framing is the whole lesson: *"Stripe Is Paying $7 Billion for a 5.5% Fee. Its Own Take Rate Is 0.36%."*

**[INFERENCE — this is the number that matters]** A toll on inference is a real business, and the market just priced it at 50x revenue. But OpenRouter's volume is overwhelmingly developer/startup/aggregator traffic, not Fortune-500 committed spend. The enterprise half of that $40B is already routed through Bedrock/Foundry/Vertex, where the hyperscaler is the toll-taker and *gives the money back* as commitment drawdown. **The toll slot for enterprise inference is occupied by parties who can pay customers to use it.**

---

## 3. WHAT THEY BUY TODAY (all [MEASURED] from primary docs unless noted)

**a) Committed spend, drawn down through the cloud.** MACC: 100% of pretax marketplace purchases of eligible offers count toward the Azure commitment. AWS EDP works analogously [INFERENCE for AWS — not publicly documented].

**b) Provisioned capacity — thin, short-dated, and explicitly *not* a capacity guarantee.**
- **Azure Foundry PTU:** billed $/PTU/hr on deployed capacity regardless of tokens consumed. Reservations available at **1-month or 1-year only** (no 3-year). Model-independent, region-scoped, deployment-type-scoped. The doc says, twice, in bold: **"Reservations don't guarantee capacity."** Also: *"Having PTU quota doesn't guarantee that capacity is available"*; *"Capacity availability changes throughout the day"*; *"Scaling down releases capacity permanently."* Microsoft's own guidance is to **deploy first, then buy the reservation** — because you may not be able to get what you paid for.
- **AWS Bedrock Provisioned Throughput:** Model Units, commitment of **none / 1 month / 6 months**; longer = cheaper; you cannot delete before term end. MU pricing is not published — "contact your AWS account manager."
- **Google Vertex Provisioned Throughput (GSUs):** exists; I could not extract terms (docs.cloud.google.com returned nav-only content twice).
- **Together AI** now sells token-priced Provisioned Throughput on open models with a 99% uptime SLA, claiming up to 90% savings vs proprietary APIs [VENDOR, via Futurum 2026-07-08].

**c) Provider-side capacity commitments — one vendor just *withdrew* the product.**
- **Anthropic Priority Tier is closed.** Live doc as of today: *"Priority Tier capacity commitments are no longer available for purchase."* Existing holders run to contract end. Structure was: input TPM + output TPM + duration (1/3/6/12 months) + a specific model version, targeting 99.5% uptime, overflow falls back to standard [MEASURED — this is the single most important fact I found].
- **OpenAI went the other way.** "OpenAI Guaranteed Capacity," announced ~2026-05-20: annual spend commitments of **1 to 3 years**, "discounts that scale according to duration," drawdown "across the portfolio of OpenAI products," eligible customers only. No published minimums, no published capacity quantities. The Register's coverage carries the sharpest critique on record: an enterprise-infrastructure CTO's line that *"Every hyperscaler solved 'reserve now, scale later' a decade ago"* and that enterprises need **"deterministic SLAs with penalty clauses,"** not the word "guaranteed" [MEASURED]. (*openai.com returned 403; I could not read OpenAI's own page.*)

**d) De-tokenised priced goods.** Anthropic's live service-tier doc confirms the bill is de-tokenising in a way that is now *contractually visible*: US-only inference (`inference_geo: "us"`) burns Priority capacity at **1.1 tokens per token** on Claude 4.6+, "because US-only inference is priced at 1.1x"; 1-hour cache writes burn at 2.00x, 5-minute at 1.25x, cache reads at 0.1x [MEASURED]. Data residency is a priced multiplier, not a feature.

**e) Governance/cost gateways — and this is where Rule 1 fires hardest.** In the four months to 2026-08-19 the following shipped or GA'd an AI/agent gateway with cost controls: **Snowflake Cortex AI Gateway** (Jul 28, + dynamic model routing Aug 18, framed explicitly as "prevent runaway enterprise costs"), **Databricks Unity AI Gateway** (GA Aug 4), **Palo Alto Networks** unified AI gateway (May 29), **Kong Agent Gateway** (Apr 23), **F5** (Aug 18), **A10 Networks** (Aug 13), **Citrix NetScaler MCP Gateway** (Jul 9), **Cequence** (Jul 30), **WSO2** (Mar 31), **Cloudflare** (Aug 5), **TrueFoundry**, **Tetrate Agent Router** with "token brokering" (Jul 15), plus **Concentrate AI** launching an LLM gateway with **free** enterprise controls (Jun 11) [all MEASURED, headline-level].

**If the customer runs Snowflake or Databricks, the gateway is now free and already inside their governance perimeter.** Both companies are simultaneously raising at $188–190B (Databricks, Jul–Aug 2026). This is exactly the "ships free in the framework the customer already runs" pattern the founder's Rule 1 names.

**f) Commitment risk transfer — exists, and it is an *insurance* product, not software.** **Archera** sells *insured commitments* for AWS/Azure/GCP: "For a monthly premium, we cover the cost of commitments you don't use," terms as short as 30 days, and it explicitly markets GPU/AI workload coverage. Priced as a premium, **not** a share of savings [VENDOR, from their own site]. By contrast **ProsperOps** — the largest autonomous commitment manager — shows **no AI/GPU/inference commitment coverage at all** on its site [MEASURED, absence].

**g) Cost visibility tooling.** CloudZero (native integrations to Anthropic, OpenAI, Bedrock, Vertex, Azure; token-level, cache-aware; **$15B total managed spend across all cloud**, and a claim that **80% of organizations miss their AI spend forecasts by 25% or more**) [VENDOR]. Harness, 1Password (entered Jul 2026), Flexera, ManageEngine, ServiceNow all pushing the same wedge.

---

## 4. WHAT THEY REFUSE TO BUY

1. **A percentage-of-inference-spend intermediary.** [INFERENCE, strongly supported] The enterprise's alternative is not "pay 0%" — it is "pay 0% *and* burn down a commitment I've already promised." A 5.5%-style layer is strictly dominated for any enterprise with an EDP/MACC. OpenRouter's take rate is viable precisely in the segment that has *no* cloud commitment.
2. **Standalone gateways as a paid line item.** [INFERENCE from the vendor list above] The category commoditized inside twelve months and is being absorbed into security and data platforms.
3. **Hedging.** [MEASURED, by absence] A dedicated news query for enterprises hedging AI compute via treasury returned exactly **one** result, a 2025 PwC treasury survey. The futures market forming right now (below) names its users as *AI builders, cloud providers, and investors* — not enterprise buyers of inference.
4. **Optimisation priced on measured savings.** [INFERENCE — and this is the sharpest constraint] If 80% of orgs miss their AI forecast by ≥25%, then a 5–10% saving is **below the measurement noise floor of the buyer's own books**. You cannot invoice against a number the customer cannot see. This is the same wall the dead thesis hit, and it is structural to the whole optimisation category, not specific to caching.

---

## 5. HOW CONTESTED

**Tooling layer: brutally contested and consolidating.** ~15 named vendors shipping overlapping AI gateways in four months; two platform incumbents (Snowflake, Databricks) giving it away; three exits already priced — Chronosphere → Palo Alto (closed 2026-01-29), OpenRouter → Stripe at >$7B (Aug 2026), and Portkey raising only a **$15M Series A** in Feb 2026 before the outcome the brief describes (*I could not independently verify the Portkey/PANW transaction in this pass*).

**Commercial/contractual layer: uncontested by startups, and structurally closed.** Nobody is competing to be the enterprise's inference *counterparty*, because you cannot resell capacity you do not own, and the two firms who own it have moved in opposite directions in one quarter (Anthropic withdrew sellable commitments; OpenAI added them without deterministic SLAs).

**Financial layer: being built by exchanges, not startups, right now.**
- **CME Group + Silicon Data**: first regulated **compute futures**, announced May 2026, **launching October 5, 2026**, referencing Silicon Data's GPU rental price index [MEASURED].
- **CFTC opened public comment on AI compute futures contracts, 2026-08-17** [MEASURED].
- **ICE + NATIVX**: energy-normalized compute futures (2026-07-01) [MEASURED].
- Bernstein notes crypto-style compute derivatives already trading ahead of the regulated venues (Jul 2026).

**[INFERENCE]** The reference-price infrastructure for compute risk is being built one layer *below* the enterprise — on GPU-hours, not tokens, and for suppliers, not buyers. There is currently no index, no unit of account, and no counterparty for **token/capacity** risk at the enterprise level. That is a genuine hole. It is also an insurance/derivatives business requiring a balance sheet and a regulator, not a software seed round.

---

## 6. THE ONE GAP THAT IS REAL, AND WHY IT IS PROBABLY NOT A SOFTWARE COMPANY

Stated plainly and without naming any existing code (Rule 3 clean):

> **Every provider sells a commitment. No provider sells a guarantee.** Azure: "Reservations don't guarantee capacity." Anthropic: capacity commitments withdrawn from sale. OpenAI: "guaranteed capacity" with no published SLA or penalty clause, and the public criticism is exactly that. AWS: MU pricing and limits available only via your account manager.

The enterprise pain — committed spend against non-guaranteed, dynamically-rationed, region-scoped capacity, with 1-month-to-1-year instruments and a $50,000/12-month refund ceiling on Azure reservations [MEASURED] — is real, documented, and unaddressed.

But the three ways to close it are:
- **Own silicon** (capital business — CoreWeave, Together, Nebius, the neoclouds),
- **Underwrite the risk** (insurance business — Archera's shape, needs a balance sheet and reserves),
- **Trade the risk** (exchange business — CME/ICE are doing it now, on GPU-hours).

**None of the three is a venture-scale software company started by one founder, and all three are already occupied by better-capitalised incumbents.** [INFERENCE]

---

## 7. TEST OF THE "INDEPENDENCE IS THE PRODUCT" PRINCIPLE, IN THIS DOMAIN

**It does not hold for procurement, and it does hold — weakly, and elsewhere — for assurance.**

- *Against, for procurement:* independence has negative commercial value here. The dependent parties (hyperscalers) pay you to route through them via commitment drawdown. An independent intermediary must be worth more than the drawdown it destroys. Nothing in the record suggests it is.
- *For, in assurance:* the only place I found an actual third-party-independence budget with a live cheque-signer is **AI assurance**, and it is government-stimulated, not enterprise-pulled: the UK DSIT **Trusted Third-Party AI Assurance Roadmap** (Sep 2025) and its follow-on **AI Assurance Consortium** with techUK leading the industry advisory group (Jun 2026) [MEASURED]. That is a compliance/risk budget of the type the brief says clears. It is also *not* inference procurement, and it is small today.

---

## 8. BOTTOM LINE FOR THE FOUNDER

1. **Budget exists?** For *inference procurement as a category with its own vendors* — **no.** That is the finding. What exists is a $12.5B-in-2025 (order-$40B-in-2026) inference *bill* sitting inside two pre-owned budgets, and a nascent token-cost-management budget whose current spend is on visibility, whose unit of account is being standardised by a Linux Foundation body that launched fifteen days ago, and whose measurement error (±25%) exceeds any plausible optimisation you could sell it.
2. **Who signs?** Platform/infra + FinOps, CFO approves, procurement gates. Not a procurement sale.
3. **What clears?** Governance and control budgets — and those are already being served for free by Snowflake, Databricks, Cloudflare, and the security incumbents. Rule 1 disqualifies the tooling layer.
4. **The one real gap** — commitment without capacity guarantee — is an insurance/derivatives shape, being built right now by CME, ICE, and CFTC, on the supply side, one layer below the enterprise.
5. **The 5.5% toll is real** and was just valued at >$7B — but it lives in the segment *without* cloud commitments. If a control point is the goal, the honest question is not "can I intermediate the enterprise's inference?" (the hyperscaler pays them not to) but **"which population of buyers has no EDP/MACC?"** That population is startups and AI-native companies, not large enterprises — which contradicts the premise of this brief and should be tested separately before anyone designs anything.

---

### Sources

Primary docs: [Azure Foundry provisioned throughput](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/provisioned-throughput) · [Azure PTU billing & reservations](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/provisioned-throughput-billing) · [Azure reservation exchanges & refunds](https://learn.microsoft.com/en-us/azure/cost-management-billing/reservations/exchange-and-refund-azure-reservations) · [Azure Consumption Commitment (MACC) benefit](https://learn.microsoft.com/en-us/marketplace/azure-consumption-commitment-benefit) · [AWS Bedrock Provisioned Throughput](https://docs.aws.amazon.com/bedrock/latest/userguide/prov-throughput.html) · [Anthropic service tiers (Priority Tier withdrawn)](https://platform.claude.com/docs/en/api/service-tiers) · [OpenAI flex processing](https://developers.openai.com/api/docs/guides/flex-processing)

Reporting/analysis: [The Register — OpenAI guaranteed capacity](https://www.theregister.com/ai-ml/2026/05/20/openai-wants-upfront-cash-for-guaranteed-ai-capacity/5243694) · [Menlo Ventures — State of GenAI in the Enterprise](https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/) · [FinOps X 2026 Day 1 keynote](https://www.finops.org/insights/finops-x-2026-day-1-keynote/) · [Linux Foundation press (Tokenomics Foundation)](https://www.linuxfoundation.org/press) · [SiliconANGLE — CME/Silicon Data compute futures](https://siliconangle.com/2026/05/12/cme-group-silicon-data-launch-ai-compute-futures-market/) · [Futurum — Together AI Provisioned Throughput](https://futurumgroup.com/insights/provisioned-throughput-redefines-open-model-inference-economics-and-predictability/) · [Archera](https://archera.ai/) · [ProsperOps](https://www.prosperops.com/) · [CloudZero AI cost management](https://www.cloudzero.com/blog/ai-cost-management/)

Headline-level via Google News RSS (not individually opened): Bloomberg/CNBC/TradingView on CFTC compute-futures review (Aug 17–18, 2026); Business Wire on ICE+NATIVX (Jul 1, 2026); TechCrunch on Stripe/OpenRouter (Aug 16, 2026); VentureBeat/CIO on Snowflake Cortex AI Gateway (Jul 28–Aug 18, 2026); Databricks Unity AI Gateway GA (Aug 4, 2026); InfoWorld on Claude Marketplace and procurement bottlenecks (Mar 9, 2026); Bain via Gulf Business on CFO AI spend (Apr 19, 2026); WSJ *Tokenomics: A CFO's Guide* (May 5, 2026); BCS/Wired-GOV on the UK AI Assurance Consortium (Jun 2026).