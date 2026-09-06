## Where the money actually went in 2026

**Method note.** WebSearch quota was exhausted at the start of this task, so everything below was gathered through Google News RSS indexes and direct page fetches, verified against publication + headline + date. I could not get past paywalls at The Information or Gartner's newsroom (403), and I flag those gaps explicitly. I mark every claim as **[measured]** (a disclosed transaction, filing, or survey figure), **[vendor]** (a company's own claim or a third-party estimator with undisclosed methodology), or **[inference]** (mine).

---

### The macro shape

Two independent surveys agree on something that matters more than any individual funding round.

Menlo Ventures' bottoms-up enterprise survey puts total enterprise generative-AI spend at **$37B in 2025**, split $19B applications / $18B infrastructure. Inside that $18B infrastructure layer: **foundation model APIs $12.5B, model training infrastructure $4.0B, and "AI infrastructure — storage, retrieval, orchestration" just $1.5B** **[measured]**. That $1.5B is the entire tooling layer — vector databases, retrieval, orchestration, and whatever observability spend exists — at roughly 4% of enterprise genAI budget and about 12% of what enterprises hand directly to model providers.

a16z's survey of 100 Global 2000 executives (Jan 2026) reports average enterprise LLM spend rising from ~$4.5M to ~$7M over two years, with respondents projecting ~$11.6M next year, and 81% now running three or more model families **[measured]**. Notably, that survey contains **no line item for observability, evals, gateways, orchestration, or guardrails at all** **[measured]** — not a low number, an absent category. CIOs were not asked because CIOs do not budget it that way.

Gartner's headline "$2.5T worldwide AI spending in 2026" (Jan 15, 2026) is not a useful software TAM — it is dominated by servers and services, and I could not retrieve the segment breakdown. Two Gartner forecasts are more informative for this question: **AI platforms and models market to grow 63% in 2026** (Jul 20, 2026), and **only 40% of organizations deploying AI will use AI observability by 2028** (May 12, 2026) **[measured]**. The second number is a ceiling statement about the observability category from its friendliest analyst.

**[inference]** The tooling layer is not a budget line. It is a rounding error attached to the model-API line, and the categories below get funded only when they can be reclassified into a budget the CFO already recognizes.

---

### Category by category

**LLM observability and evaluation — crowded, and now almost entirely gone.**

The category consolidated completely in nineteen months, and not one buyer was another eval company:

| Target | Buyer | Price | Date |
|---|---|---|---|
| Weights & Biases | CoreWeave | $1.7B | Mar 2025 |
| Langfuse | ClickHouse | undisclosed | Jan 2026 |
| Promptfoo | OpenAI | undisclosed | Mar 2026 |
| Galileo | Cisco (into Splunk) | undisclosed | Apr 2026 |
| Deepchecks (team/tech) | Check Point | acqui-hire | May 2026 |
| Arize | Dynatrace | **$915M** | Aug 2026 |

All **[measured]**. The Arize deal closed four days before this research; Dynatrace funded it with $1.25B in convertible senior notes and **its stock fell on the announcement** **[measured]**.

The buyers are an APM vendor, a network vendor's log platform, a database company, a neocloud, a security vendor, and a model lab. **[inference]** Every one of them bought a feature to attach to an existing contract. None bought a business. Corroborating this: The Information ran "Revenue Lags at AI Evaluation Startups" in April 2025 **[measured — headline; figures paywalled]**, and in the sixteen months since, no vendor in the category has disclosed ARR to refute it. The only revenue figures that exist anywhere are third-party estimates with undisclosed methodology — Arize ~$12.4M, W&B ~$13.6M, Langfuse ~$1.1M **[vendor]** — one to two orders of magnitude below the prices paid.

Braintrust is the last well-capitalized independent: **$80M Series B, Feb 2026, led by ICONIQ with a16z, Greylock, Elad Gil and Basecase; customers Notion, Replit, Cloudflare, Ramp, Dropbox; valuation undisclosed** **[measured]**. It also disclosed a breach in May 2026 requiring every customer to rotate keys **[measured]** — an unhelpful event for a vendor selling trust. LangSmith survives inside LangChain (~$1.3B valuation Oct 2025, ~$16M ARR estimated **[vendor]**).

**Verdict: crowded-but-unpaid.** The exits were real; the revenue never was.

**Gateways and routers — the largest number on the board, and it is not a gateway thesis.**

- **OpenRouter: $1.3B valuation in May 2026 → Stripe acquisition at >$7B, Aug 16–17, 2026** **[measured]**. Roughly 5.4x in three months.
- **Portkey: $15M Series A (Elevation Capital) in Feb 2026 → acquired by Palo Alto Networks, announced Apr 30, closed Jun 1–2, at a "$700M-class" price per The New Stack** **[measured — price is one outlet's characterization, not a disclosed figure]**. Series A to exit in three months.
- LiteLLM: open source, no revenue, and the subject of what CloudSEK and others called **the largest AI supply-chain breach of 2026, exposing 2,500+ companies, Aug 11–12, 2026** **[measured]**.
- Cloudflare AI Gateway: free/bundled. nexos.ai: €30M (Oct 2025), positioned on *shadow-AI governance*, not cost. Requesty: $3M.

The most revealing single datapoint in this entire research: **OpenRouter charges roughly a 5.5% fee; Stripe's own take rate is 0.36%** **[measured — Business Model Analyst, Aug 17, 2026]**. Stripe did not buy a router. It bought a payment rail with a fifteen-times-higher take rate that happens to be denominated in tokens. Palo Alto did not buy a router either; it bought a security control plane and folded it into Prisma. **[inference]** Nobody paid for "route my LLM calls more cheaply." Two incumbents paid, very large sums, to own the *metering and control point* — one framing it as payments, one as security.

**Verdict: quiet-but-paid, under two different names, neither of which is "gateway."**

**Guardrails, AI security, and agent governance — this is where the enterprise money is.**

The volume here dwarfs everything else combined. Acquisitions in 2026 alone: Palo Alto→Koi ($400M, Feb), Palo Alto→Portkey (~$700M, Apr), Cisco→Astrix ($400M, May), Cisco→Galileo (Apr), Okta→Permiso (~$200M, Jul), OpenAI→Promptfoo (Mar), F5→SurePath (Jun), Torq→Jit (~$70M, May), Cyera→Ryft ($100–130M, Apr), Silverfort→Fabrix (Apr), Snowflake→Natoma (May), Zscaler→(San Mateo startup, May), Databricks→two startups (Mar), Accenture→Dragos/runZero/NetRise (>$4B, Jun) **[all measured]**.

Primary rounds: **Cyera $600M at a $12B valuation** (Jun 2026, 4x in 18 months), Glow $180M at $1.2B out of stealth (Jul), Obsidian at $1.1B (Aug 4, Reuters), Onyx $113M (Jul), Neo $100M (Jul), Socket $60M, Mindgard €26M, NeuralTrust €17.2M **[measured]**. StartupHub counted **three AI-security companies raising $270M in a single week** (Aug 10) and **five AI-agent-governance transactions in seven days** (Aug 3) **[measured]**. ServiceNow is reportedly raising $4B in debt partly to fund an AI-security push **[measured — reported, not confirmed]**.

**[inference]** This is not a new budget. It is the CISO budget, which already existed, is already large, is already defended at board level, and does not require anyone to prove ROI in dollars saved. That is the entire reason it is paying.

**Cost management and FinOps for AI — loudest pain, no established vendor, and organizing around the wrong axis for a savings pitch.**

Demand-side evidence is unambiguous and recent: **Gartner predicts AI coding costs will surpass the average developer's salary by 2028** (Jun 24, 2026); **Gartner expects one in five firms to pull back on AI by 2028 as costs mount** (Aug 16, 2026); CIO.com ran "The inference bill nobody budgeted for" (Apr 2026) and "Beware of AI costs hidden in plain sight" (Jun 2026); Spiceworks "Token shock" (May 2026); an Economic Times/MarketScale finding that **60% of agentic AI costs go to response refinement and most enterprises are already over budget** (Jul 2026) **[all measured as published claims]**.

Supply side is thin and telling. **DoiT acquired Attribute (est. $65M, Aug 2026)**, launched an "AI tokenomics" product in Jul 2026, and became a founding member of a **"Tokenomics Foundation" to define the standard for AI cost attribution** (Aug 4, 2026). **1Password entered AI cost management** in Jul 2026. **Cloudflare** shipped cost control keyed on identity (Aug 2026). PointFive raised $60M (Jun); StitcherAI $3M (May) **[all measured]**.

**[inference]** Every entrant already owns a control point and a billing relationship — a cloud reseller, an identity vendor, a network. None is winning this as a standalone measurement product. More important for anyone considering this space: **the emerging standard is attribution, not reduction.** "Who spent this" is being standardized; "spend less" is not being standardized by anyone. Those are different products with different buyers — attribution sells to finance and platform ops, reduction sells to no one with a budget.

**Agent orchestration and durable execution — genuinely quiet, and the quiet is bearish.**

Temporal raised **$146M at a flat valuation in March 2025** **[measured]**; a16z published an "Investing in Temporal" post dated Feb 17, 2026, but the page 404s and I could not retrieve terms **[unknown]**. I found **zero** 2026 funding news for Inngest or Restate in the Google News index **[measured — absence of indexed news, not proof of absence]**. Menlo puts **agent platforms at $750M of the $19B application layer (4%)** and finds **only 16% of enterprise deployments are true agents**, with most still fixed-sequence or routing workflows **[measured]**.

**Verdict:** a flat round for the category leader is the single most informative number here. This is not quiet-but-paid; it is quiet because the workloads that need it barely exist yet.

**Data and context layers — the cautionary tale of the last cycle.**

**Pinecone: ~$26.6M estimated ARR against a $750M valuation** set in 2023 **[vendor estimate]**. LlamaIndex ~$10.9M **[vendor]**. Weaviate took a *corporate strategic* investment from Ricoh (Jun 2026) rather than a priced growth round **[measured]**. Qdrant $50M (Mar 2026), SurrealDB $23M (Feb 2026) **[measured]**. Meanwhile the analytics database that absorbed an observability company — **ClickHouse — raised $400M at a $15B valuation** (Jan 2026, Dragoneer) **[measured]**.

**[inference]** The standalone vector store got funded ahead of demand and then became a feature of pgvector, ClickHouse, Snowflake and Databricks. It is the closest available analogue for what happens to a horizontal AI-infrastructure primitive that does not attach to an existing budget line.

---

### The pattern underneath all of it

**[inference, but strongly supported]** Sort the 2026 exits by buyer, not by target, and one rule explains all of them: **every acquirer already owned a budget line the CFO recognizes.** Security (Palo Alto, Cisco, Okta, Check Point, F5, Zscaler, CrowdStrike, Accenture). Observability/APM (Dynatrace, Cisco/Splunk). Payments (Stripe). Cloud billing (DoiT). Data platform (ClickHouse, Snowflake, Databricks). Compute (CoreWeave). Model provider (OpenAI).

Not one company in this map grew into a standalone "AI tooling" budget, because — per both the Menlo breakdown and the a16z CIO survey — **that budget line does not exist.** Products in this space get bought at good prices, but they get bought as *features that reclassify into an existing line*, and they get bought before they prove revenue. Nineteen months, seven observability/eval companies, zero disclosed ARR figures, one $915M price tag.

**[inference] Bearing directly on the experiment that prompted this:** the caching-as-savings thesis is dead on the demand side too, and for a reason independent of the measured cold-share ceiling. The market is not organizing around *reducing* token spend — it is organizing around *attributing* it (Tokenomics Foundation), *billing* it (Stripe/OpenRouter at >$7B), and *controlling who may spend it* (Palo Alto/Portkey, Okta/Permiso, Cyera at $12B). Three adjacencies from the experiment map onto paid territory rather than unpaid: the per-operation metered content-addressed graph is an attribution artifact, not a savings artifact, and attribution is the thing being standardized right now; the concurrent fan-out single-flight gap (0 of 12 gateways) sits in the one layer that just cleared $700M and $7B exits; and the staleness-vs-nondeterminism problem — the shadow comparator disagreeing with itself 20% of the time at zero perturbation — is precisely the unsolved problem Dynatrace just paid $915M to get a piece of. I flag all three as inference, not as validated demand.

---

## Bottom line

**Established (measured, multiple sources):**
- Enterprise genAI tooling — retrieval, storage, orchestration, observability combined — was **~$1.5B of a $37B market in 2025**, ~4%. Model APIs alone were $12.5B. There is no "AI tooling" budget line in either the Menlo or a16z enterprise surveys.
- **LLM observability/eval consolidated completely in 19 months**: W&B→CoreWeave $1.7B, Langfuse→ClickHouse, Promptfoo→OpenAI, Galileo→Cisco/Splunk, Deepchecks→Check Point, Arize→Dynatrace $915M. Zero of these buyers were peers; zero targets disclosed ARR.
- **Gateways produced the two largest prices in the map — Stripe/OpenRouter >$7B and Palo Alto/Portkey ~$700M — but neither was bought as a gateway.** Stripe bought a 5.5% take rate on token flow against its own 0.36%. Palo Alto bought a security control plane.
- **AI security and agent governance is the one category with unambiguous, large, recurring enterprise payment**, because it draws on the pre-existing CISO budget: Cyera at $12B, five governance transactions in one week, $270M raised by three companies in another.
- **Agent orchestration is quiet and slow.** Temporal raised flat in Mar 2025; agent platforms are $750M of a $19B application layer; only 16% of enterprise deployments are true agents.
- **Cost pain is real, loud, and analyst-confirmed** — AI coding cost to exceed developer salary by 2028; one in five firms pulling back by 2028 on cost.

**Contested:**
- Whether any observability/eval vendor ever had meaningful revenue. The Information said no in Apr 2025; nobody has published a number since; the $915M Arize price argues one way and Dynatrace's falling stock argues the other.
- Whether the AI-cost category is a market or a feature. DoiT, 1Password and Cloudflare all entered in a six-week window in mid-2026 — but all three already owned a billing or identity control point, and none is a standalone cost vendor.
- Whether Braintrust stays independent. Best logo list in the category (Notion, Replit, Cloudflare, Ramp, Dropbox), $80M from ICONIQ, every peer sold, and a May 2026 breach disclosure.
- Whether Gartner's 40%-by-2028 observability adoption forecast is a floor or a ceiling.

**Unknown (I could not verify):**
- **All ARR figures for private tooling vendors.** Every one I have (LangChain $16M, Pinecone $26.6M, Arize $12.4M, W&B $13.6M, LlamaIndex $10.9M, Langfuse $1.1M, Portkey $5M) is a GetLatka estimate with no disclosed methodology. Treat as order-of-magnitude only. What is solid is the *relationship*: these estimates are 1–2 orders of magnitude below the prices paid, which means the prices were strategic, not multiples.
- **OpenRouter's actual revenue.** The Information's "OpenRouter Financials Suggest Steep Price" (Jul 29, 2026) is paywalled; the headline implies the $7B is rich relative to revenue, but I have no figures.
- **Gartner's $2.5T segment breakdown** (403 on the newsroom). Do not use $2.5T as a software TAM proxy — it is hardware- and services-dominated.
- **Temporal's Feb 2026 a16z round terms** (a16z page 404s), and Inngest/Restate's current state — absence from the news index is weak evidence.
- **Portkey's $700M price** is one outlet's characterization ("$700M-class," The New Stack), not a disclosed figure. Palo Alto did not disclose.
- **Job-posting and procurement data.** I could not obtain primary hiring or contract-award data; the only signals available were LinkedIn's "fastest-growing roles" lists, which are too coarse to separate these categories.

**Sources fetched directly:** [Menlo Ventures — 2025 State of Generative AI in the Enterprise](https://menlovc.com/2025-the-state-of-generative-ai-in-the-enterprise/) · [a16z — Leaders, gainers and unexpected winners in the Enterprise AI arms race](https://a16z.com/leaders-gainers-and-unexpected-winners-in-the-enterprise-ai-arms-race/) · [Pulse 2.0 — Braintrust $80M Series B](https://pulse2.com/braintrust-80-million-series-b-raised-to-power-production-ai-infrastructure/)

**Sources verified by headline, publication and date via Google News indexes** (direct URLs were Google redirects): Forbes and CRN on Dynatrace/Arize $915M (Aug 13–14, 2026); TechCrunch and SiliconANGLE on Stripe/OpenRouter (Aug 16, 2026); Business Model Analyst on the 5.5% vs 0.36% take rates (Aug 17, 2026); PR Newswire, CRN and The New Stack on Palo Alto/Portkey (Apr 30 – Jun 2, 2026); Cisco Blogs, Network World and SiliconANGLE on Cisco/Galileo (Apr 9, 2026); SiliconANGLE and Reuters on ClickHouse/Langfuse and the $400M/$15B round (Jan 16, 2026); Reuters and Calcalist on CoreWeave/W&B, Cyera $600M/$12B, Cisco/Astrix, Okta/Permiso, DoiT/Attribute; SecurityWeek on OpenAI/Promptfoo (Mar 2026); Gartner press releases (Jan 15, May 12, Jun 24, Jul 20, Jul 27, 2026); TechCrunch on Temporal's flat $146M (Mar 31, 2025); The Information "Revenue Lags at AI Evaluation Startups" (Apr 14, 2025) and "OpenRouter Financials Suggest Steep Price" (Jul 29, 2026); CloudSEK and CX Today on the LiteLLM supply-chain breach (Aug 11–12, 2026); VentureBeat on 1Password's AI cost management entry (Jul 14, 2026).