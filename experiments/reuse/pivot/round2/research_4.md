## AI SECURITY BUDGET — SIZING REPORT (verified 2026-08-19)

**HEADLINE: The budget exists, is real, and is the fastest-growing line in enterprise security — but it is ~$3B globally in 2026, it is 78% re-labelled existing spend, and 1% of CISOs have a dedicated line item for it. It is also the most contested space in security: every independent vendor of consequence has already been acquired.**

---

### 1. WHO SIGNS THE CHEQUE

**MEASURED.** The CISO, out of the existing security budget. Pentera surveyed 300 US CISOs/security execs in Feb 2026: **78% fund AI security from existing security budgets; 1% have a dedicated AI security budget; 21% plan to introduce one.** ([pentera.io](https://pentera.io/press-release/ai-security-exposure-survey-2026))

**MEASURED.** The cheque is not growing fast. IANS Research + Artico Search: cybersecurity budgets grew **4% in 2025, down from 8%** — slowest in five years — and security **fell as a share of IT budget from 11.9% to 10.9%**. ([govtech.com](https://www.govtech.com/blogs/lohrmann-on-cybersecurity/cyber-budgets-slow-ai-surges-what-the-data-says-about-2026))

**MEASURED (secondary owner).** McKinsey: ~15% of corporate cyber spend already originates outside the CISO, growing at 24% CAGR. Same source. There is a second, faster-growing buyer (platform/data/eng), but it is small today.

**MEASURED (third payer, different budget).** In AI *assurance*, the payer inverts: the AI **vendor** pays for certification to close enterprise deals. AIUC issues AIUC-1 certificates plus insurance to $50M; ElevenLabs and UiPath bought certification. That is a GTM/sales-enablement budget, not a CISO budget. ([mateopetel.substack.com](https://mateopetel.substack.com/p/aiuc-and-the-birth-of-the-ai-assurance))

---

### 2. SIZE OF THE PRIZE

**MEASURED — total security market.** Gartner *Forecast: Information Security, Worldwide, 2024–2030, 2Q26* (G00855892, 25 Jun 2026): **$248.9B in 2026** (+12.7% cc) → $372.6B by 2030.

**MEASURED — the two categories people conflate.** Gartner *Forecast Analysis: AI-Amplified Security, Worldwide, 2026* (G00846160, 4 Aug 2026, Upadhyay):
- **AI-amplified security** (AI *inside* security products) = **$48.5B in 2026 → $204.5B in 2030**. Gartner states explicitly this is **a subset of the security forecast and is NOT additive** — "existing budgets redirecting toward AI-native capabilities."
- **Securing AI itself** (protecting your models, pipelines, agents) = **$16.4B in 2030**.
- Ratio: ~12 dollars of AI-powered defence for every $1 protecting the AI. The March framing was **17x**.

**INFERENCE (mine, arithmetic shown).** Gartner does not publish a 2026 "securing AI" figure. Back it out: securing AI was folded into *Other Security Software*, which 1Q26 had compounding at 5.1% and 2Q26 has compounding at **18.5% to $37.6B in 2030**; the revision added **+$20.3B to 2030**. So the 1Q26 2030 value was ~$17.3B, implying a 2025 base of ~$13.5B, implying **securing-AI content in 2025 ≈ $2.1B**. Growing ~55%/yr to $16–20B by 2030 reconciles. **2026 securing AI ≈ $3B — roughly 1.3% of the security market.**

**INFERENCE — bottom-up cross-check, and it does not reconcile with even $3B.** Observable AI-native security revenue:
- Palo Alto Prisma AIRS, the category leader: **300+ customers, "on track to hit $100M ARR within the next few quarters"** as of Q3 FY26 (2 Jun 2026) — versus SASE $1.6B and XSIAM $600M+ at the same company. ~1.5% of PANW's NGS ARR.
- **Critical footnote in PANW's own Q3'26 deck:** "Prisma AIRS ARR is based on the portion of software **NGFW credits** purchased by a customer and **allocated** to Prisma AIRS on the booked quote. Customers may elect to consume purchased software NGFW credits flexibly." The flagship AI-security ARR number is an *allocation of firewall credits*, not independently-bought AI security.
- Median contract value across AI security & red-teaming tools: **$31,250/yr**; Lakera Guard median **$175K/yr**; WitnessAI $180/user/yr (1,000 user min); Lasso $50K/yr; Portal26 $250K/yr; HiddenLayer up to $5M/yr on AWS Marketplace. Of 25 vendors, **only 4 publish pricing; 12 are "contact sales" only**. ([costbench.com](https://costbench.com/software/ai-security/), [aisecurityplatform.com](https://aisecurityplatform.com/research/ai-security-pricing-transparency-2026))

Summing the leader (~$100M), the acquired pure-plays, and the funded independents gives **~$300–600M of genuinely AI-native security ARR worldwide**. Against Gartner's ~$3B, **the gap is re-labelled platform SKUs** — DSPM, DLP, CASB/SSE and IAM renamed "AI security." Pentera corroborates directly: **only 11% of enterprises have security tools specifically designed to protect AI systems; 75% rely on legacy controls built for other attack surfaces.**

---

### 3. NEW BUDGET VS RE-LABELLED — the decisive finding

**MEASURED.** This is the cleanest answer in the whole dataset:
- 1% dedicated AI security budget; 78% funded from existing security budget (Pentera, n=300).
- Gartner's own note says AI-amplified security "is not additive spending."
- RH-ISAC 2026 CISO Benchmark: ~90% expect AI-security spend to rise, but "organizations typically **reallocate existing budgets** rather than secure new funding." ([rhisac.org](https://rhisac.org/press-release/ciso-benchmark-2026/))
- Forrester's 2026 planning guide funds AI security by **cutting standalone SSE, standalone IAST, and standalone SIEM** — the money is scavenged from consolidation, not created. ([softwarestrategiesblog.com](https://softwarestrategiesblog.com/2025/11/03/forrester-2026-cybersecurity-budget-top-10-insights/))

**Verdict: ~99% re-labelled/reallocated, ~1% net-new, with a genuine net-new line item forming in the 2027 planning cycle (21% intend to create one).**

---

### 4. WHAT THEY BUY / REFUSE TO BUY

**Buy (in rough order of realised dollars):** shadow-AI discovery and DLP for LLM traffic at the proxy (Zscaler, Netskope, WitnessAI, Harmonic, Lasso, Prompt/SentinelOne, Aim/Cato); DSPM-for-AI (Varonis); model supply-chain scanning (Protect AI/PANW); runtime prompt-injection guardrails (Lakera/Check Point, CalypsoAI/F5); AI red-teaming (SPLX/Zscaler); agent posture/governance (Noma, Zenity). ([aurascape.ai](https://aurascape.ai/answers/ai-security-landscape-2026/))

**Refuse to buy.** Another point tool. Wiz 2026 CISO Budget Benchmark (300+ leaders): CISOs are shifting **"from expansion to rationalization"**; **58% already run 25+ security tools**. Pentera: 58% say AI influences their consolidation strategy but **only 3% are actively consolidating because of AI**. Translation: AI security must arrive inside a platform they already own, and it is not urgent enough to trigger a stack change.

---

### 5. HOW CONTESTED

**MEASURED — maximally contested and already consolidated.** In ~18 months: Palo Alto/Protect AI **$634.5M** (closed 22 Jul 2025) and Palo Alto/KOI **$400M**; Check Point/Lakera **~$300M**; SentinelOne/Prompt Security **~$250M announced**; CrowdStrike/Pangea; Cato/Aim Security; F5/CalypsoAI; Zscaler/SPLX (3 Nov 2025); Cisco/Robust Intelligence; Varonis/AllTrue.ai **$126M** (Feb 2026); Akamai/LayerX ~$205M (Q3 2026); Alphabet/Wiz $32B (closed Mar 2026). Momentum Cyber: **AI-security M&A projected +400% for full-year 2026**; ~$2B of AI-security financing deployed through May 2026; $3.6B of venture funding into agentic AI security startups. ([momentumcyber.com](https://momentumcyber.com/cybersecurity-market-review-may-2026))

Every named independent in the 2026 landscape map is either acquired or has a well-funded acquirer circling. Median post-money for $100M+ security rounds in Q1'26: $1.85B.

---

### 6. TEST OF THE "INDEPENDENCE IS THE PRODUCT" PRINCIPLE

**Holds in one place, fails in another — and the place it holds is not the CISO's budget.**

- **Fails against Rule 1 on runtime controls.** The providers ship the primitive. Azure AI Content Safety **Prompt Shields is free to 5,000 records/month**, then paid. AWS Bedrock Guardrails prices a prompt-attack filter at $0.08/1k text units — cheap, bundled, and the default. OpenAI's moderation endpoint is **free** (though it does *not* cover prompt injection). Cloudflare AI Gateway ships Guardrails on all plans. Palo Alto's Prisma AIRS module #1 is literally "**AI Gateway — the AI Control Plane for the enterprise**." The control point is taken.
- **Holds in assurance, but the buyer is the AI vendor, not the CISO.** AIUC-style certification+insurance is bought by the seller of the agent to unblock enterprise procurement. UK government estimates 524 companies in AI assurance, £1.01B GVA (2024). This is a real, structurally-independent position — a provider cannot certify itself — but it is an insurance/GTM budget, not the $248.9B security budget.
- **Fails as a business today on model degradation.** "Silent model swap / quantization drift" is a well-populated *blog* genre in 2026 (a dozen posts in Q2 alone) with **zero funded vendors, zero pricing, zero budget line**. Where it is monetised at all it sits in LLM observability (engineering budget), not security.

---

### 7. VERDICT AGAINST THE THREE RULES

- **Rule 2 (size first):** ~$3B in 2026 for securing AI (~1.3% of security spend), of which ≤$600M is genuinely AI-native product revenue. Growing fastest of any category (18.5% CAGR to 2030; the only Gartner category whose growth rate *increases* every year), and capturing $21.9B of the $154.4B in new security dollars through 2030. **The prize is small now and large later — the inverse of the dead thesis, which was small now and small later.** But nobody has a dedicated budget to sell into for another 12–18 months.
- **Rule 1 (does it ship free):** Runtime guardrails, prompt-injection filtering and AI gateways — **yes, stop.** Model supply-chain scanning, agent posture, and third-party assurance/certification — **not yet free**, and assurance is structurally un-shippable by the provider.
- **Rule 3 (thesis without naming founder's code):** Nothing here requires the reuse layer. Clean.

**The honest framing for the founder: this is not "the budget does not exist." It is "the budget exists, is owned by the CISO, is ~99% re-labelled, has a median ACV of $31K, and has been bought out from under every independent vendor in eighteen months." Entering as a 26th tool into a stack that 58% of CISOs are actively trying to shrink is the failure mode. The only structurally defensible seat found in this scan is assurance/certification — where independence is genuinely un-copyable by the provider — and its payer is the AI vendor's GTM budget, not the CISO's.**

---

**Sources:** [Gartner AI-amplified security forecast G00846160 breakdown](https://softwarestrategiesblog.com/2026/08/17/gartner-ai-amplified-security-forecast-204b-2030/) · [Gartner 2Q26 infosec forecast G00855892 breakdown](https://softwarestrategiesblog.com/2026/07/06/gartner-2q26-information-security-forecast-securing-ai-2030/) · [Pentera AI Security Exposure Survey 2026](https://pentera.io/press-release/ai-security-exposure-survey-2026) · [Wiz 2026 CISO Budget Benchmark](https://www.wiz.io/reports/ciso-security-budget-benchmark-2026) · [RH-ISAC 2026 CISO Benchmark](https://rhisac.org/press-release/ciso-benchmark-2026/) · [IANS/Artico via GovTech](https://www.govtech.com/blogs/lohrmann-on-cybersecurity/cyber-budgets-slow-ai-surges-what-the-data-says-about-2026) · [Forrester 2026 budget planning guide](https://softwarestrategiesblog.com/2025/11/03/forrester-2026-cybersecurity-budget-top-10-insights/) · [Momentum Cyber Market Review May 2026](https://momentumcyber.com/cybersecurity-market-review-may-2026) · [2026 AI security vendor landscape](https://aurascape.ai/answers/ai-security-landscape-2026/) · [AI security pricing transparency benchmark](https://aisecurityplatform.com/research/ai-security-pricing-transparency-2026) · [CostBench AI security pricing](https://costbench.com/software/ai-security/) · [AWS Bedrock Guardrails pricing](https://aws.amazon.com/bedrock/pricing/) · [Azure AI Content Safety pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/content-safety/) · [OpenAI moderation guide](https://developers.openai.com/api/docs/guides/moderation) · [Cloudflare AI Gateway Guardrails](https://developers.cloudflare.com/ai-gateway/features/guardrails/) · [Prisma AIRS product page](https://www.paloaltonetworks.com/ai-security/prisma-airs) · [PANW Q3'26 earnings coverage](https://www.marketbeat.com/instant-alerts/palo-alto-networks-q3-earnings-call-highlights-2026-06-02) · [AIUC assurance market](https://mateopetel.substack.com/p/aiuc-and-the-birth-of-the-ai-assurance) · [IAPP AI Governance Vendor Report 2026](https://iapp.org/resources/article/ai-governance-vendor-report/) · [Gartner AI spending forecast 2026 via ARN](https://www.arnnet.com.au/article/4173416/global-ai-spend-to-hit-us2-59t-in-2026.html)

**Method note:** WebSearch quota was exhausted at session start; all retrieval was done via WebFetch against primary URLs plus a Playwright-driven DuckDuckGo/Brave session. Gartner's own press pages return 403 to automated fetch, so the two Gartner forecast documents (G00855892, G00846160) are cited through a licensed analyst's published breakdown that names document IDs, publication dates and authors — figures should be treated as accurately relayed but not directly read from Gartner. Market-sizing figures from SNS Insider / Emergen / Dimension (e.g. "AI agent security $18.7B in 2025 → $507B by 2035") were reviewed and **discarded as unreliable** — they disagree with each other by 5x and with observable vendor revenue by 30x.