## VERDICT

**The budget is real, but it is not a new budget line — and the venture window on it closed between February and July 2026.**

Agent identity is being bought, at scale, by named buyers, with published prices. It fails your test anyway, for a reason that is the *mirror image* of the caching failure: last round the number was too small to reach; this round the number is large (~$25B) and every path to it is already owned. Six platform vendors bought a control point in the agent-identity request path inside six months. The primitive is a MUST in the MCP spec, free. And the one meter that would make this venture-scale — a price per agent — **does not exist anywhere in the market.**

---

## 1. WHO SIGNS THE CHEQUE

Three different people, which is itself the finding. There is no single owner.

| Signer | What they sign | Budget it comes from | Evidence |
|---|---|---|---|
| **M365 / workplace IT buyer** | Microsoft Agent 365, **$15.00/user/month** (annual), GA **1 May 2026**. Includes Entra Agent ID. Also inside M365 E7 at $99/user/month | Productivity seats — *not* security | MEASURED (Microsoft security page + SAMexpert, agreeing) |
| **CISO / identity team** | Okta for AI Agents, CyberArk (now PANW), SailPoint | IAM slice of security budget | MEASURED (vendor SKU pages) |
| **Platform/cloud engineering** | AWS Bedrock AgentCore Identity — metered component, consumption-based | Cloud bill | MEASURED (AWS pricing) |

**At most enterprises, nobody signs.** Okta's own June 2026 CISO survey (N=306, six markets, via Apprize360): 21% use shared credentials or broad-permission service accounts for agents, 20% leave it to deployment teams ad hoc, and **25% simply treat agents exactly as human identities** — i.e. the incumbent solution is "reuse the IAM licence we already pay for," which costs zero incremental dollars.

**The most damning single fact about ownership:** Okta's CISO report — published by the vendor with maximum incentive to demonstrate a funded programme — contains 81% "deeply concerned," 47% "can identify all agents," 46% "can control access," and **not one budget or spending-intent statistic.** A concern number without a spend number, from the vendor that wants the spend number, is a slide, not a programme.

---

## 2. SIZE AND GROWTH — MEASURED

**Top-down:**
- Worldwide information security spending 2026: **$244B, +11.6%** constant currency (Gartner, Forecast Analysis, 5 Feb 2026)
- IAM as share of security budget: **6%** at companies under $400M revenue, **12%** above $5B (IANS Research / Artico Search CISO Compensation & Budget Survey)
- → IAM envelope ≈ **$15–29B**

**The agent/non-human slice of that: nobody has measured it.** Published estimates run **$4.15B (2025)** to **$21.39B (2026)** — a 5x spread from market-research firms. A 5x spread *is* the answer: the category has not been sized because it has not been separately purchased.

**Bottom-up, from revealed prices — this is the honest number.** The entire independent pure-play category sold for roughly **$1.6B of disclosed consideration** (Astrix ~$400M + Entro ~$200M + Oasis $1B, plus Natoma and Permiso undisclosed). Even at aggressive strategic multiples, that implies **tens of millions of combined ARR** — not billions — being paid *specifically* for agent/NHI identity by independent vendors. Oasis sold for $1B four months after a $120M Series B **with no ARR ever disclosed by either party**; SecurityWeek confirms no revenue figure was given. These were option-value purchases, not revenue purchases.

**Growth of the true pure-play line — the cleanest measurement available:**

> **Okta Q1 FY2027 (28 May 2026): revenue $765M, +11% YoY. Subscription revenue $750M, +11%. cRPO $2.499B, +12%.**

Okta's CEO frames the entire company around this thesis — *"AI agents are rapidly becoming a new workforce inside every organization, creating a wave of identities that must be secured."* The pure-play identity leader, telling exactly this story, is growing 11% and its next-twelve-months backlog is growing 12%. **The agent narrative is not showing up in the numbers of the company best positioned to capture it.** For contrast, CyberArk's last standalone year (FY2025) was $1.440B ARR, +23% — driven by PAM and Venafi, not agents.

---

## 3. THE PRICING FINDING THAT KILLS THE TAM STORY

**No vendor in this market charges per agent.** I checked every published price:

- **Microsoft** — per *human* user ($15/user/mo). Entra Agent ID has **no standalone price at all**; it is not purchasable on its own.
- **AWS** — per vCPU-hour, GB-hour, invocation. Identity is a metered sub-component of AgentCore, not a subscription.
- **Descope** — agentic identity (monthly active consents / monthly active tokens) is **bundled into standard CIAM tiers, 2,000 each free**, paid tiers from $249/mo.
- **WorkOS** — **no AI-agent SKU exists on the pricing page at all**; 1M MAU free.
- **Okta** — two SKUs exist (Okta for AI Agents Standard; Core for FedRAMP/HIPAA, which excludes ISPM and PAM), **no published price**, i.e. negotiated as attach to an enterprise agreement.

The industry's entire TAM narrative rests on "machine identities outnumber humans 109:1" (Palo Alto Networks 2026 Identity Security Landscape). **The industry has then deliberately declined to bill on that unit.** Agent count drives revenue nowhere. A 109:1 ratio with a per-human meter produces exactly zero incremental dollars per agent.

---

## 4. WHAT THEY BUY TODAY / WHAT THEY REFUSE TO BUY

**Buy:** agent and NHI *discovery and inventory*; credential vaulting and short-lived credentials replacing long-lived tokens; governance workflows, audit trails, and a revocation kill switch; a runtime gateway. Always **attached to an IAM, PAM, data-security or productivity platform they already own.**

**Refuse to buy:** a standalone agent-identity product. Four independent proofs:
1. Every standalone vendor of scale **exited rather than scaled** (§5).
2. Descope and WorkOS give agent auth away inside a general auth tier — the developer market will not pay a premium for the word "agent."
3. Microsoft **will not sell** Entra Agent ID standalone at any price; it is a bundle sweetener for a $15/user/mo seat.
4. Okta **will not publish** a price — which in enterprise security means it is a discount lever inside a renewal, not a line item.

---

## 5. HOW CONTESTED — the census

Every notable independent was acquired in a **six-month window**:

| Date | Acquirer | Target | Price |
|---|---|---|---|
| 11 Feb 2026 (closed) | **Palo Alto Networks** | CyberArk | ~$25B |
| 4 May 2026 announced, 29 Jun closed | **Cisco** | Astrix Security | ~$400M reported (Calcalist); had raised $85M |
| 27 May 2026 | **Snowflake** | Natoma (enterprise MCP gateway/identity, 2 yrs old) | undisclosed |
| 15 Jun / 29 Jun 2026 | **SailPoint** | Entro Security | ~$200M reported (Calcalist) |
| 28 Jul 2026 | **Cyera** | Oasis Security | $1B (~$700M cash + shares) |
| 30 Jul 2026 | **Okta** | Permiso Security | undisclosed |

Cisco's framing: *"extending Zero Trust to the agentic workforce."* Snowflake's: *"the control plane for the agentic enterprise."* These are **control-point acquisitions**, exactly the pattern in your brief — and all six control points in the agent request path are now taken: network (Cisco), privileged access (PANW), IdP (Okta), governance (SailPoint), data (Cyera), data-plane MCP gateway (Snowflake).

Survivors are small: Aembit (**34 employees**, $59.6M raised); Veza ($108M Series D at $808M, Apr 2025). Context on how hot the adjacency is: Cyera raised $600M at a **$12B valuation on ~$150M ARR**.

**This space is not contested. It has been bought.**

---

## 6. RULE 1 TEST — FAILS, HARD

The primitive ships free in the framework the customer already runs, and it is **normative**:

The MCP authorization spec (live, verified today) **MUST**-requires OAuth 2.1, RFC 9728 Protected Resource Metadata, RFC 8707 resource indicators, RFC 9207 issuer identification, audience-bound token validation, scope challenges and step-up authorization. Every one of "give the agent an identity, scope it, bind the token to an audience, challenge for more, log it" is a free, mandatory part of the protocol.

Agent-specific delegation is not even a differentiator waiting to be built: the entire extension surface is **two documents** — Enterprise-Managed Authorization (stable) and Client Credentials (draft). And IETF **WIMSE has published zero RFCs** as of August 2026, with its charter making **no mention of AI agents**. There is no standards gap to own; there is a standards vacuum that the IdPs are filling for free.

Confirming behaviour: Anthropic did not build agent identity — it **featured Okta as an identity provider** (18 June 2026). The model providers are outsourcing this to IdPs, and the IdPs are giving it away to win the platform position.

---

## 7. THE INDEPENDENCE PRINCIPLE — TESTED HERE, DOES NOT HOLD

Your structural principle is *live* in this market: Okta's 25 June 2026 release markets it as *"the first independent and neutral identity platform to bring AI agent governance to highly regulated environments."* Independence is an active sales argument.

**And it is producing 11% growth.** The neutrality argument is already owned by a $3B-revenue incumbent, is already being made, and is not accelerating that incumbent's numbers. In identity, independence is table stakes for an IdP, not a wedge — because the thing being certified (did this agent have permission?) is something the customer's own logs answer, not something requiring an adversarial third party. The independence argument only monetises where the *provider has an incentive to lie about itself* — model degradation, benchmark integrity, capability regression. That is a model-assurance claim, not an identity claim.

**Where to test the independence principle instead** (flagging, not proposing): I surfaced the OCC's revised **Model Risk Management guidance (Bulletin 2026-13)** and an active commercial AI-assurance push from PwC, EY and the AI Verify Foundation's Global AI Assurance Sandbox. That is a compliance/attestation budget with a regulator forcing the spend. Caveat before anyone gets excited: Gartner sizes total AI-regulation compliance spend at only **~$1B by 2030**, which is small — size it properly before designing anything.

---

## 8. FACT / CLAIM / INFERENCE

**Measured fact (primary sources, verified live today):** all pricing above; Okta Q1 FY27 financials; Gartner $244B/+11.6%; IANS 6%/12% IAM share; the M&A census dates and disclosed values; MCP spec normative requirements; WIMSE zero-RFC status; Okta CISO survey methodology and percentages; the absence of any budget statistic in that survey; the absence of a per-agent price anywhere.

**Vendor claim (directionally useful, self-interested):** 109:1 machine-to-human ratio and 9-in-10 identity breach rate (Palo Alto Networks); 24% can control agent actions / 31% feel capable (Cisco, acquisition rationale); 23% have a formal enterprise-wide agent identity policy (Strata).

**Low confidence / do not cite:** "agentic AI security market $1.65B in 2026 → $13.52B by 2032" and the "$11.3B NHI market" figures — these trace to SEO content farms (axis-intelligence.com), not to the analyst houses they name. The $4.15B-vs-$21.39B market-research spread is likewise unreliable in both directions. Astrix ~$400M and Entro ~$200M are press reports (Calcalist), not disclosed terms.

**My inference:** (a) tens of millions, not billions, of ARR currently changes hands specifically for agent identity from independent vendors — derived from disclosed consideration against plausible multiples, not from any disclosed revenue; (b) Okta's 11%/12% growth against a fully agentic pitch is the strongest available evidence that agent identity is currently an *attach* motion inside existing IAM renewals rather than incremental spend; (c) the deliberate absence of per-agent pricing across all five vendors with published prices is a considered industry choice, not an oversight.

**Methodology limitation, stated plainly:** the session's WebSearch quota was exhausted before I began; all of the above was gathered by direct fetches of primary vendor, standards and IR pages plus browser-driven Bing. I could not reach Gartner's paywalled IAM segment forecast, the CSA January 2026 NHI survey full text (N=228), or Okta's Q2 FY2027 results (not yet reported at 19 Aug 2026). None of those would move the verdict; Okta's Q2 print is the one worth watching.

---

## 9. THE ONE SURVIVING CRACK — reported, not recommended

Nobody prices per agent because **nobody can independently count and attest what agents actually did.** Microsoft counts humans. AWS counts vCPU-hours. Okta publishes nothing. The evidence layer under agent authorisation is the one place where a control budget could theoretically form.

**It is thin, and Rule 1 probably kills it too:** CloudTrail has shipped hash-chained signed logs since 2015, the MCP spec makes audit-relevant metadata mandatory and free, and Okta's Agent Gateway already *"logs each call."* If this is pursued at all, the honest first step is Rule 2 again — find someone who has *paid money* for independent agent-action attestation, before writing a line of anything.

## 10. RECOMMENDATION

**Do not enter agent identity.** Not because the budget is absent — it is the largest and best-owned budget you have looked at — but because it fails all three tests simultaneously: the primitive is free and normative in MCP (Rule 1); the reachable independent slice is tens of millions of ARR against a $25B envelope that platforms have already fenced (Rule 2); and every remaining position requires being Microsoft, Cisco, Snowflake, Palo Alto, Okta, SailPoint or Cyera.

The finding worth carrying forward to the next candidate: **the six acquisitions above are all control-point purchases, and they were made by data, network and productivity platforms — not by identity companies.** The buyers with money were the ones who already owned the path the request travels. That is the pattern from your brief, confirmed independently in a second market.

---

**Sources:** [MCP Authorization spec](https://modelcontextprotocol.io/specification/draft/basic/authorization) · [MCP ext-auth](https://github.com/modelcontextprotocol/ext-auth) · [Microsoft Entra Agent ID](https://www.microsoft.com/en-us/security/business/identity-access/microsoft-entra-agent-id) · [SAMexpert Entra licensing guide](https://samexpert.com/entra-id-licensing-guide) · [Okta Q1 FY2027 results](https://investor.okta.com/news-and-events/news-releases/news-details/2026/Okta-Announces-First-Quarter-Fiscal-Year-2027-Financial-Results/default.aspx) · [Okta press room](https://www.okta.com/press-room/press-releases/) · [Okta for AI Agents](https://www.okta.com/products/govern-ai-agent-identity/) · [Okta Global CISO Insights 2026](https://www.okta.com/newsroom/articles/global-ciso-insights-2026/) · [Cisco / Astrix](https://blogs.cisco.com/news/cisco-announces-intent-to-acquire-astrix-security) · [SecurityWeek: Cyera / Oasis](https://www.securityweek.com/cyera-acquiring-oasis-security-in-1-billion-deal/) · [IANS Research on AI agent identity](https://www.iansresearch.com/resources/all-blogs/post/security-blog/2026/02/24/ai-agents-are-creating-an-identity-security-crisis-in-2026) · [PANW 2026 Identity Security Landscape](https://www.paloaltonetworks.com/idira/idira-identity-security-landscape) · [IETF WIMSE](https://datatracker.ietf.org/wg/wimse/about/) · [Descope pricing](https://www.descope.com/pricing) · [WorkOS pricing](https://workos.com/pricing)