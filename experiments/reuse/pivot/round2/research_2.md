# DATA & CONTEXT INFRASTRUCTURE — BUDGET MAP
**Researched live 2026-08-19. WebSearch quota was exhausted at session start; everything below comes from direct fetches of primary pages (vendor price sheets, provider docs, press releases, analyst pages) on 2026-08-19.**

---

## 0. HEADLINE VERDICT

**The data/context *infrastructure* budget is real but small, slow-consolidating, and the smallest line in enterprise AI infra: $1.5B worldwide in 2025 against $37B total genAI spend — 4.1%.** Roughly 56% of the surrounding infra market goes to incumbents (Databricks/Snowflake/MongoDB/Datadog), leaving an AI-native independent slice on the order of **$650M globally, split 20+ ways, for storage + retrieval + orchestration combined.**

**Meanwhile the same underlying function — "give the model the right context, and only the context this user may see" — is being paid for at 100x that scale, but through two different budget lines: an application seat (Glean $300M ARR alone; M365 Copilot at $18–21/user/mo) and a security/risk control (Cyera $150M+ ARR at a $12B valuation).**

So: **the budget exists, but not where the founder would build it.** Retrieval-as-infrastructure is a 4%-of-bill line being actively absorbed by every model provider. Retrieval-as-authority ("what can this agent see?") is a fast-growing control budget with a security cheque signer.

---

## 1. SIZE THE PRIZE (Rule 2, done first)

### Measured / best-available third-party estimate

[Menlo Ventures, *2025: The State of Generative AI in the Enterprise*](https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/) — survey-based VC estimate, not audited, but the only bottoms-up breakdown that isolates this layer:

| Layer | 2025 spend | Note |
|---|---|---|
| **Total enterprise genAI** | **$37.0B** | 3.2x YoY |
| Applications | $19.0B | 51% |
| **Infrastructure** | **$18.0B** | 49%; $9.2B in 2024 → 2.0x |
| — Foundation model APIs | $12.5B | |
| — Model training infra | $4.0B | |
| — **AI infra (storage, retrieval, orchestration)** | **$1.5B** | *the entire budget in scope* |

Report's exact words: *"AI infrastructure ($1.5 billion) manages the storage, retrieval, and orchestration of data that connects LLMs to enterprise systems."* And: *"Incumbents hold 56% of the market as many AI app builders continue building on the data platforms they've trusted for years"* — naming **Databricks, Snowflake, MongoDB, Datadog**.

**Derived (my inference):** $1.5B × ~44% non-incumbent ≈ **$660M** of world spend available to AI-native data/context vendors, and that pool also has to cover orchestration (LangChain/Temporal/agent frameworks), not just retrieval. Split across the ≥20 named vector/retrieval vendors plus five hyperscalers, **no single independent retrieval sub-segment plausibly exceeds ~$150–300M globally today.**

**Ratio worth internalising: the retrieval layer collects $0.12 for every $1.00 spent on the model APIs it feeds.**

### Growth: partially unknown — stating it rather than inferring
- Infra layer overall: $9.2B (2024) → $18B (2025), **2.0x**. Measured.
- **The $1.5B retrieval/storage/orchestration line has no disclosed 2024 comparator.** Its growth rate is *unknown*. Do not let anyone quote a growth figure for it.
- Adoption direction is favourable but shallow: [Menlo 2024](https://menlovc.com/perspective/2024-the-state-of-generative-ai-in-the-enterprise/) measured **RAG adoption at 51%, up from 31%**. But Menlo 2025 walks it back: *"Prompt design remains the dominant technique, followed by retrieval-augmented generation (RAG)"* and fine-tuning, tool calling, **context engineering** and RL are *"still niche and used primarily by frontier teams."* Enterprise architectures are described as *"surprisingly simple."* This corroborates the brief's 16%/27% true-agent figure.

### Vendor/analyst claim that contradicts the above — flagged, not adopted
[MarketsandMarkets, Dec 2025](https://www.marketsandmarkets.com/Market-Reports/vector-database-market-112683009.html): vector DB market **$2,652.1M (2025) → $8,945.7M (2030), 27.5% CAGR**, 25 named players led by *Microsoft, Elastic, MongoDB, Google, AWS*.

**These two numbers cannot both be true.** M&M's 2025 figure for vector databases alone is 1.8x Menlo's figure for storage + retrieval + *orchestration* combined. My read: M&M attributes incumbent database/search license revenue to "vector" wherever a vector feature exists (note that its top-five vendor list is entirely incumbents, not vector-native startups). **Treat $2.65B as a marketing-grade number and $1.5B as the spendable one.** If you must plan, plan on Menlo.

---

## 2. WHO SIGNS THE CHEQUE

Four distinct buyers, four different budgets, four different sizes:

| Buyer | Budget line | Typical deal | Size signal |
|---|---|---|---|
| **Platform/infra engineering lead** | Cloud & data infra | $50–$5k/mo self-serve, then committed | Pinecone Standard **$50/mo min**, Enterprise **$500/mo min**; turbopuffer **$16 / $256 / ≥$4,096/mo** tiers |
| **App/AI engineering lead** | Rolled into the model API bill | usage line items | OpenAI/Bedrock/Gemini retrieval SKUs — *no separate procurement event* |
| **CIO / head of workplace tech** | Employee productivity software (per seat) | **$100K–$500K mid-market; >$5M Fortune 500** (Glean, per Sacra) | Glean **$300M ARR**; M365 Copilot **$18–21/user/mo** |
| **CISO / data risk owner** | Security & compliance | 7-figure enterprise | Cyera **>$150M ARR, $12B valuation** |

**The critical structural fact: the infra buyer and the cheque-signing buyer are not the same person.** The $1.5B is bought by engineers on a credit card or a small committed spend. The nine-figure cheques for the *same function* are signed by a CIO (as seats) or a CISO (as risk).

---

## 3. WHAT'S BEING ABSORBED — THE PROVIDER PRICE SHEETS (all fetched today)

Every frontier provider now ships managed retrieval. This is Rule 1 territory, and it is already past tense.

| Provider | Retrieval SKU | Price (2026-08-19) |
|---|---|---|
| **[Google Gemini](https://ai.google.dev/gemini-api/docs/pricing)** | File Search | **Indexing embeddings $0.15/1M tokens only.** Retrieved doc tokens billed as normal input. Storage and query-time embedding not charged. |
| **[OpenAI](https://developers.openai.com/api/docs/pricing)** | File search | **$0.10/GB/day** (1 GB free) = ~$3.04/GB/mo, **+ $2.50/1k tool calls** |
| **[AWS Bedrock](https://aws.amazon.com/bedrock/pricing/)** | Knowledge Bases | **$5.00/GB raw data/mo**, **$1.00/1k Retrieve calls**, **$4.00/1k agentic Retrieve** — with *"multimodal document parsing, embeddings generation, and re-ranking included at no extra charge"* |
| **[Azure AI Search](https://azure.microsoft.com/en-us/pricing/details/search/)** | now branded **Foundry IQ** | Agentic retrieval **first 50M tokens/mo free**; semantic ranker **first 1k requests/mo free** |
| **[Anthropic](https://platform.claude.com/docs/en/about-claude/pricing)** | *no retrieval SKU at all* | Memory tool **GA, free, client-side storage you own**; context editing free; server-side compaction free; **web fetch $0**; web search $10/1k |

**Independent price sheets for comparison:**

| Vendor | Storage | Query |
|---|---|---|
| **[Pinecone](https://www.pinecone.io/pricing/)** | **$0.33/GB/mo** | $16–18 per M read units; writes $4–4.50/M; embed $0.16/M tok; rerank $2/1k |
| **[Elastic serverless](https://www.elastic.co/pricing/serverless-search)** | **$0.047/GB/mo retained** | $0.09–0.14/VCU-hr; Agent Builder $0.025/execution |
| **[Chroma](https://www.trychroma.com/)** | object-storage based, "up to 10x cheaper" (vendor claim) | $5 free credits |
| **[LlamaParse](https://www.llamaindex.ai/pricing)** | — | 1,000 credits = $1.25; **from 1 credit/page ≈ $0.00125/page** |
| **[Reducto](https://reducto.ai/pricing)** | — | **$0.015/credit**, 15k free, 20% batch discount |

### Three findings that fall straight out of this table

1. **Providers are not price-dumping — they're bundling at a premium and winning anyway.** OpenAI charges **~9x Pinecone** and **~64x Elastic** per GB-month of storage. Bedrock charges **15x Pinecone**. Buyers take it. **What this budget pays for is not price and not rigor; it is one line item inside a contract that already exists.** That is the single most decision-relevant thing in this report.
2. **The commodity components are already free.** Bedrock gives away parsing, embeddings *and* reranking. Gemini gives away storage. Anthropic gives away memory, context editing and compaction. Azure gives away the first 50M agentic-retrieval tokens. **Anything a startup would sell as "the retrieval primitive" is already a zero-price line on at least one provider's sheet.**
3. **The meter is de-tokenising here too**, exactly as the brief describes: retrieval is billed per **call** ($1 / $2.50 / $4 per 1k), per **GB-day**, per **execution** ($0.025), per **page**, per **session-hour** ($0.08). Tokens are becoming the minority meter in this layer.

### The framework layer is closing too
- **[MCP](https://modelcontextprotocol.io/)** (spec rev 2026-07-28) is a free open standard for agent↔data connection, supported by Claude, ChatGPT, VS Code, Cursor. The plumbing has no price.
- **[Fivetran + dbt completed their merger 2026-06-01](https://www.getdbt.com/blog/fivetran-dbt-labs-complete-merger-to-create-the-data-infrastructure-for-trusted-ai-agents)** (announced 2025-10-13), 100,000 data teams, positioned explicitly as *"the data infrastructure layer that makes agents trustworthy… from data movement and transformation to the governed context needed for reasoning and action"* — and are shipping an **open-source "Agents Schema" standard**, *"a customer-owned context layer that works within existing security and governance policies."* **An open, free, vendor-neutral "context layer" standard is being pushed by the pipeline incumbents right now.**
- **Snowflake, 2026-08-17: ["Building an Internal Context Layer for AI Agents at Snowflake"](https://www.snowflake.com/en/blog/)**, alongside 2026-08-18's *"Governed AI for Every Builder: Enterprise Controls in Snowflake CoCo."* The data platform is building the context layer in-house and selling governance on top.

**Verdict on Rule 1 for this space: FAIL, decisively, for every retrieval, embedding, parsing-adjacent, memory, and "context layer" primitive.**

---

## 4. WHAT BUYERS *DO* BUY — AND WHERE THE REAL MONEY IS

The money did not disappear. It moved packaging.

### 4a. Context sold as an application seat — LARGE, GROWING ~90–100%/yr
**Glean.** From [glean.com/press](https://www.glean.com/press) and [Sacra](https://sacra.com/c/glean/):
- **$300M ARR** announced **2026-05-28**, headline verbatim: *"Glean Surpasses $300M ARR: Unrivaled **Enterprise Context** Fuels AI Adoption"*
- $100M ARR (Feb 2025) → $208M (end 2025, +89% YoY) → **$300M (May 2026)**
- **$7.2B valuation** (Series F, June 2025, $150M led by Wellington; confirmed via [Reuters/Wikipedia](https://en.wikipedia.org/wiki/Glean_Technologies)); ~$768M raised total
- Contracts **$100K–$500K mid-market, >$5M Fortune 500**; $1M+ segment nearly tripled 2025→2026
- 2026 product line reads as governance, not retrieval: *"Enterprise Agent Development Lifecycle, Codifying How Enterprises **Build, Govern, and Measure** AI Agents"* (2026-05-12)

**Glean alone books ~20% of the entire measured worldwide retrieval-infrastructure budget — by selling the same function as an application, to a CIO, per seat.**

**Microsoft 365 Copilot**: **$18/user/mo** (annual, discounted from $21) as an add-on; $32/user/mo bundled into Business Premium. What you're buying is *"AI-powered chat connected to your work data"* via **Microsoft Graph grounding + 100+ connectors**. Permissions-aware retrieval over the whole corpus is a *bundled feature of a seat*, not a product. ([source](https://www.microsoft.com/en-us/microsoft-365/copilot/business))

### 4b. Context sold as a security control — LARGE, FASTEST-GROWING, HIGHEST MULTIPLE
**Cyera**, [press release 2026-06-10](https://www.cyera.com/press-releases/cyera-raises-600-million-at-12-billion-valuation-to-continue-building-the-trust-layer-for-the-ai-era) + [TechCrunch 2026-06-02](https://techcrunch.com/2026/06/02/cyera-eyes-12b-valuation-at-80x-arr-multiple-despite-operating-losses/):
- **$600M raised at $12B valuation**, quadrupled in 18 months; prior round $9B (Jan 2026)
- **>$150M ARR at ~80x**; *"tripled ARR three years in a row"*; ~1/5 of the Fortune 500; 1,500 employees / 18 countries
- Positioning verbatim: **"the trust layer for the AI era."** Their framing of the problem is *"enterprises lack visibility into what their AI can see and do"*, with the stat: *"In 2026, 68% of organizations cannot tell the difference between human activity and AI agent activity inside their own systems."*
- Operating at a loss; TechCrunch notes Cyera disputes some figures. Flagged.

**Compare the multiples honestly.** Cyera: ~80x ARR on a security cheque. Glean: $7.2B on ~$208–300M ARR ≈ 24–35x on a CIO seat cheque. Vector-native infrastructure: no vendor discloses ARR at all — which is itself information.

**Microsoft Purview** is the Rule-1 counterweight to anyone selling permissions/oversharing controls. [Purview's AI docs (rev 2026-06-25)](https://learn.microsoft.com/en-us/purview/ai-microsoft-purview) ship **DSPM for AI**, oversharing controls, sensitivity-label-aware retrieval (EXTRACT rights enforced before an AI app may return data), DLP against third-party genAI, insider-risk "Risky AI usage" templates, audit of every prompt/response, eDiscovery and retention — **and it explicitly covers ChatGPT Enterprise, Anthropic Claude (Enterprise), and Microsoft Foundry, not just Copilot.** Microsoft is already the cross-provider AI data governance plane inside its own estate, sold on an existing E5/Purview licence.

### 4c. The one live migration to watch
**Oso** (formerly authorization-as-a-service, the canonical "permissions-aware retrieval" primitive) now sells **agent authorization at $15/user/mo**, where a "user" is *"a human invoking agents,"* and the feature list is **PII leakage detection, risky tool-usage detection, MCP & CLI tool inventory, SIEM integration, EDR agent inventory, custom data retention** ([osohq.com/pricing](https://www.osohq.com/pricing)). **A permissions-infrastructure company has repriced itself as a security monitoring product with a per-human seat.** That is the entire thesis of this report in one price page.

---

## 5. WHAT BUYERS REFUSE TO BUY

Measured or strongly evidenced:
1. **A separate line item for retrieval when the provider bundles one.** Demonstrated by willingness to pay 9–15x provider markups on storage.
2. **Sophistication.** Menlo 2025: context engineering is *"niche… primarily frontier teams."* The market is still buying prompt design and basic RAG.
3. **Standalone permissions/authorization infrastructure** — Oso's repricing and Microsoft's bundling both point the same way.
4. **A new "context layer" abstraction** — Fivetran/dbt is giving one away as an open standard, Snowflake is building its own, MCP is free.
5. **(Inference, moderate confidence)** Anything requiring a *new* procurement event under ~$500K, because the natural home for this spend is inside an existing cloud/model/M365 contract.

---

## 6. HOW CONTESTED

**Extremely — and asymmetrically.** Vector/retrieval storage alone, verified today: Pinecone, Chroma, turbopuffer, LanceDB, Weaviate, Qdrant, Zilliz/Milvus, Vespa, Elastic, MongoDB, Redis, Postgres/pgvector, Oracle, Databricks, Snowflake Cortex Search, **plus** OpenAI File Search, Gemini File Search, Bedrock Knowledge Bases, Azure AI Search/Foundry IQ, S3 Vectors. **≥20 suppliers for a $1.5B pool that also has to cover orchestration.** MarketsandMarkets counts 25 players.

Contrast: **no vector-native vendor publishes an ARR figure.** Public scale claims are downloads and logos — Pinecone *"9,000+ companies and 800,000+ developers"*; Chroma *"15M+ monthly downloads, 27k GitHub stars."* Adoption is enormous; revenue is not disclosed. In a market where Glean, Cyera and Fivetran all publish revenue milestones, silence is a data point.

Also telling: **Pinecone's own repositioning.** As of 2026-08-06 its lead product is **Nexus** ("Nexus GA: It's the Knowledge, Not the Models"), pitched as *"the trusted knowledge engine for agents… Database stores and retrieves data. Nexus compiles it into something agents can **trust**"* ([pinecone.io/product](https://www.pinecone.io/product/)). The category leader has moved its own messaging from *storage* to *trust* — same direction as Cyera's "trust layer," Fivetran's "trusted AI agents," Snowflake's "Governed AI," Glean's "Build, Govern, and Measure."

**Five independent vendors, no coordination, all pivoted their top-line message to trust/governance within twelve months. That convergence is the strongest signal in this report.**

---

## 7. THE STRUCTURAL PRINCIPLE, TESTED

The brief asks whether "independence from the provider is itself the product" holds here.

**Partially — and with an important correction.** In data-context, the thing a provider cannot credibly certify is not model quality; it is **"what did your AI see, and was it allowed to see it?"** But the relevant conflicted party is usually **the cloud/workplace vendor, not the model vendor** — the corpus lives in SharePoint/Drive/S3/Snowflake, and Microsoft both hosts the data and sells the AI that reads it. That is precisely why Cyera, positioned as an independent "trust layer" spanning clouds and AI apps, gets 80x ARR while Purview ships comparable capability bundled.

**So the independence premium in this budget is real, but it is priced by a CISO, spans cloud estates, and is about access and visibility — not about retrieval quality or cost.** An independent party that could attest *"agent X retrieved documents Y under identity Z, and nothing outside entitlement"* sits at a genuine control point (it can allow, deny, and must be passed through). An independent party that retrieves *better or cheaper* sits in a 4%-of-bill optimisation line that four providers are bundling to zero.

---

## 8. RULE SCORECARD

| Rule | Verdict |
|---|---|
| **1. Ships free in the framework/provider?** | **FAIL for the whole infra layer.** Memory: free (Anthropic). Parsing/embeddings/reranking: free (Bedrock). Storage: free (Gemini). First 50M agentic-retrieval tokens: free (Azure). Connection protocol: free (MCP). Context layer standard: free and open (Fivetran/dbt Agents Schema). |
| **2. Size the prize.** | **$1.5B world total for storage+retrieval+orchestration, 4.1% of genAI spend, 56% to incumbents → ~$660M contestable, ≥20 suppliers.** Growth of the line: *unknown, not disclosed.* This number was cheap to get and, again, it came in under the threshold. |
| **3. Statable without the founder's code?** | **N/A — this is a market map, not a candidate.** But note: the sizing was obtainable in one session of price-sheet fetches, before any design. |

---

## 9. THE FINDING, STATED PLAINLY

**This budget does exist — but the infrastructure packaging of it is a 4%-of-bill line under active absorption, and it is not venture-scale for a new entrant.** Do not build retrieval, a vector store, a context layer, a memory layer, a permissions-aware retrieval primitive, or a document pipeline. Each is priced at or near zero by at least one party the customer already pays.

**The venture-scale money in "data and context" is being paid, at 10–20x the infra line, to whoever holds authority over what an AI is allowed to see** — sold to a CIO as a seat (Glean $300M ARR, $7.2B) or to a CISO as risk (Cyera $150M ARR, $12B, ~80x). Both of those are control-point budgets, exactly matching the pattern in the brief. The retrieval mechanics are the commodity underneath them.

---

## 10. CONFIDENCE & GAPS

**High confidence (primary sources fetched today):** all provider and vendor pricing; Purview's AI capability set; MCP status; Fivetran/dbt merger terms and positioning; Cyera's round; Glean's $300M ARR headline and press cadence; Pinecone's Nexus repositioning; Snowflake's context-layer/governance posts.

**Medium confidence:** Menlo's $37B / $18B / $1.5B decomposition — a VC survey estimate, not audited, and Menlo restated 2024 total genAI spend between its own reports ($13.8B in the 2024 edition, $11.5B in the 2025 edition). It is nonetheless the only source that isolates this layer, and its direction is corroborated by every price sheet above. Glean's ARR ladder is Sacra's reconstruction outside the $300M figure, which is Glean's own.

**Low confidence / unverified:** my derived ~$660M contestable figure (arithmetic on Menlo's 56% incumbent claim, which the report applies to the whole infra layer, not the retrieval sub-line — it may be a poor proxy). Pinecone/Chroma/Weaviate/Qdrant revenue: **no public figures exist**; I found none and did not estimate.

**Not closed (WebSearch quota exhausted before I could):** Databricks and Snowflake 2026 run-rates (newsroom pages returned navigation only); MongoDB Q2 FY2027 Atlas growth (investor site timed out twice); turbopuffer per-GB rates (calculator is client-rendered; docs/pricing 404s); OpenAI's ChatGPT Enterprise "company knowledge" adoption (openai.com returns 403 to this fetcher); Glean's net retention and the June 2026 context survey internals. **None of these would change the headline — they would refine the incumbent share and the enterprise-search competitive read.**

**Sources:** [Menlo 2025 State of GenAI](https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/) · [Menlo 2024](https://menlovc.com/perspective/2024-the-state-of-generative-ai-in-the-enterprise/) · [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) · [OpenAI pricing](https://developers.openai.com/api/docs/pricing) · [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) · [Anthropic memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) · [AWS Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) · [Azure AI Search pricing](https://azure.microsoft.com/en-us/pricing/details/search/) · [Pinecone pricing](https://www.pinecone.io/pricing/) · [Pinecone product/Nexus](https://www.pinecone.io/product/) · [Pinecone blog](https://www.pinecone.io/blog/) · [turbopuffer pricing](https://turbopuffer.com/pricing) · [Chroma](https://www.trychroma.com/) · [Elastic serverless pricing](https://www.elastic.co/pricing/serverless-search) · [LlamaIndex pricing](https://www.llamaindex.ai/pricing) · [Reducto pricing](https://reducto.ai/pricing) · [Glean press](https://www.glean.com/press) · [Glean $300M ARR](https://www.glean.com/press/glean-surpasses-300m-arr-unrivaled-enterprise-context-fuels-ai-adoption) · [Sacra: Glean](https://sacra.com/c/glean/) · [Glean Technologies (Wikipedia)](https://en.wikipedia.org/wiki/Glean_Technologies) · [Cyera $12B round](https://www.cyera.com/press-releases/cyera-raises-600-million-at-12-billion-valuation-to-continue-building-the-trust-layer-for-the-ai-era) · [TechCrunch on Cyera](https://techcrunch.com/2026/06/02/cyera-eyes-12b-valuation-at-80x-arr-multiple-despite-operating-losses/) · [Microsoft Purview for AI](https://learn.microsoft.com/en-us/purview/ai-microsoft-purview) · [M365 Copilot pricing](https://www.microsoft.com/en-us/microsoft-365/copilot/business) · [Fivetran/dbt merger](https://www.getdbt.com/blog/fivetran-dbt-labs-complete-merger-to-create-the-data-infrastructure-for-trusted-ai-agents) · [Oso pricing](https://www.osohq.com/pricing) · [Snowflake blog](https://www.snowflake.com/en/blog/) · [MCP](https://modelcontextprotocol.io/) · [MarketsandMarkets vector DB report](https://www.marketsandmarkets.com/Market-Reports/vector-database-market-112683009.html)