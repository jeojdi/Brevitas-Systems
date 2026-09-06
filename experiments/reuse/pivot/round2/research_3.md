## VERDICT

**The budget exists, but it is small, it is shrinking in its largest market, and its legal forcing function was removed four months ago.** Do not build here.

Three dated events between April and July 2026 destroyed the premise of this research question. I found them in primary sources, not commentary.

---

## 1. THE PREMISE IS OUT OF DATE — SR 11-7 NO LONGER EXISTS

**On 17 April 2026, the OCC, Federal Reserve and FDIC rescinded SR 11-7.** This is not a revision. It is a rescission plus replacement with non-enforceable guidance. Verbatim from [OCC Bulletin 2026-13](https://www.occ.gov/news-issuances/bulletins/2026/bulletin-2026-13.html) (I pulled the page and the attached guidance PDF directly):

> "This bulletin rescinds … OCC Bulletin 2011-12, 'Sound Practices for Model Risk Management: Supervisory Guidance on Model Risk Management'"

> **"This guidance does not set forth enforceable standards or prescriptive requirements; accordingly, non-compliance with this guidance will not result in supervisory criticism against a banking organization."**

> **"Generative AI and agentic AI models are novel and rapidly evolving. As such, they are not within the scope of this guidance."**

> "this guidance is expected to be most relevant to banking organizations with over $30 billion in total assets."

Also rescinded: the *Model Risk Management* booklet of the Comptroller's Handbook, OCC 2011-12, OCC 2021-19 (BSA/AML model risk), and the FDIC's FIL-22-2017 adoption. The Fed's parallel letter is SR 26-2.

**What specifically died, from the replacement text I extracted from the guidance PDF:**

| SR 11-7 (2011) | SR 26-2 / OCC 2026-13 (2026) |
|---|---|
| "Critical to effective validation is independence from model development and use"; independence reinforced via compensation | "The quality of validation process depends on the rigor and effectiveness of the review **rather than on organizational structure** of the banking organization['s] risk management function" |
| Validate every model **at least annually** | Annual cadence eliminated; frequency is risk-based judgement |
| Comprehensive model inventory required | No inventory mandate; materiality-based |
| Applied broadly (FDIC at $1bn) | Aimed at **>$30bn** institutions |
| Enforceable-in-practice supervisory expectation | Explicitly **non-enforceable**, no supervisory criticism |
| Predates GenAI | GenAI and agentic AI **explicitly out of scope** |

Vendor models: "validation of vendor products, **either by internal or outside parties**" — permissive, not a third-party mandate.

The agencies said they "plan to issue in the near future a request for information that addresses model risk management generally and considers, in particular, banks' use of AI, including generative AI and agentic AI." *I could not verify that any such RFI has issued as of 2026-08-19* — the OCC bulletin index 404'd on me. Treat the AI perimeter in US banking as **announced but unwritten**.

**Consequence for Rule 2:** the founder's brief assumed a regulation that had already been repealed. The specific obligation that would have created a third-party validation market — mandatory, annual, independent validation — is the exact clause that was deleted.

---

## 2. THE EU AI ACT HIGH-RISK DEADLINE SLIPPED ~16 MONTHS, ONE MONTH AGO

[Regulation (EU) 2026/1744](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=OJ:L_202601744) ("Digital Omnibus on AI"), adopted 8 July 2026, published in the OJ 24 July 2026, **entered into force 27 July 2026**:

- Annex III standalone high-risk: 2 Aug 2026 → **2 December 2027**
- Annex I product-embedded high-risk (incl. medical devices, lifts, toys): 2 Aug 2027 → **2 August 2028**
- Unchanged: prohibitions (Feb 2025), GPAI chapter (Aug 2025), Article 50 transparency (with a 4-month transitional period for marking)

The Commission's stated reason was that standards are not ready. **This is the second time the AI Act's binding date has moved, and it moved because compliance tooling did not exist — not because it existed and worked.** Anyone who built for 2 August 2026 spent a year selling into a date that evaporated three weeks before it arrived.

**Critically: the AI Act does not create a third-party assurance market anyway.** Under Article 43, third-party conformity assessment by a notified body is mandatory only for Annex III point 1 (biometric identification/categorisation), and even there the provider may elect internal control. Everything else in Annex III is **self-assessment**.

**Same pattern in the US states:** Colorado's AI Act (SB 24-205) was **repealed before it ever took effect** — SB 26-189 signed 14 May 2026, replacing it with a narrower ADMT transparency regime effective 1 January 2027. Federally, EO 14365 (11 Dec 2025) plus a DOJ AI Litigation Task Force are actively attacking state AI laws on preemption grounds.

---

## 3. SIZE THE PRIZE (RULE 2)

**The entire global AI governance software market is under half a billion dollars.**

[Gartner, 17 February 2026](https://www.biztechreports.com/news-archive/2026/2/17/global-ai-regulations-fuel-billion-dollar-market-for-ai-governance-platforms-gartner-february-18-2026): spending "expected to reach **$492 million in 2026 and surpass $1 billion by 2030**", against "$1 billion in total compliance spend"; large enterprises will run an average of ten GRC tools by 2028, up from eight in 2025. *(Caveat: Gartner's own headline and the syndicated body text disagree on whether the label is "AI governance" or "AI data governance." Gartner's press page 403'd me directly. Treat $492M as ±the width of that ambiguity — it does not change the order of magnitude.)*

For calibration: **$492M is roughly 7% of what Stripe paid for OpenRouter.** A ~20% CAGR to $1B by 2030 is a fine business for a $50M-outcome company and a bad one for a venture-scale bet, and the whole pot is already contested by IBM, SAS, Microsoft Purview, ServiceNow, OneTrust, Diligent, Archer, MetricStream, Credo AI, Holistic AI, Fairly AI, ValidMind and Monitaur — the category graduated from a Gartner Market Guide to a **Magic Quadrant in 2026** (per Holistic AI's own site), which is the signal that a category is crowded and about to consolidate, not that it is opening.

**The MRM-software slice specifically (my inference, flagged as such):** the new guidance targets institutions >$30bn in assets — on the order of 300 banks worldwide. At $150k–$1M ACV that is a **$45M–$300M SAM**, and the $30bn threshold just *shrank* the addressable population relative to SR 11-7. Chartis publishes RiskTech quadrants for MRM but no public dollar sizing — I pulled and text-extracted their 2024 MRM Quadrant Update and it contains **no market size figure at all**.

**Comparable funding, as a market-size proxy:** Credo AI has raised **$41.3M total** (July 2024, $21M Series B); ValidMind **$8.1M seed** (March 2024); Monitaur **$6M Series A** (May 2024). Holistic AI is undisclosed and small. After five-plus years and the loudest regulatory tailwind in the history of enterprise software, **no independent AI-governance vendor has raised a growth round**. That is the market telling you its size.

The UK's £1.01bn "AI assurance market" figure ([DSIT, Nov 2024](https://www.gov.uk/government/publications/assuring-a-responsible-future-for-ai), reaffirmed June 2026) is **gross value added across 524 firms** — overwhelmingly consultancies and law firms billing hours, not software revenue. Do not read it as a software TAM.

---

## 4. WHO SIGNS, AND WHAT IS HAPPENING TO THEIR BUDGET

**Banking:** Head of Model Risk Management, reporting to the CRO, second line of defence. It is a *cost centre inside a cost centre*, and it is being cut.

Risk.net's benchmarking study of **44 banks**, published July 2026, is unambiguous and its headlines are the finding:
- ["Model risk managers are being asked to do more with less"](https://www.risk.net/benchmarking/model-risk/7963859/model-risk-managers-are-being-asked-to-do-more-with-less) (20 Jul 2026) — expanding AI workload on **flat resources**
- ["Model risk managers see growing regulatory divergence"](https://www.risk.net/benchmarking/model-risk/7963894/model-risk-managers-see-growing-regulatory-divergence) (24 Jul 2026) — banks expect **easing of supervisory scrutiny in the US, tightening in Europe**
- ["From gatekeeper to coach: model risk bids to reinvent itself"](https://www.risk.net/benchmarking/model-risk/7963704/from-gatekeeper-to-coach-model-risk-bids-to-reinvent-itself) (8 Jul 2026) — the function is looking for a reason to exist
- ["Lower-risk models face excessive reviews, banks say"](https://www.risk.net/benchmarking/model-risk/7963863/lower-risk-models-face-excessive-reviews-banks-say) (27 Jul 2026) — "LLM-as-judge offers model testing at scale, **but few lenders use it to facilitate autonomous sign-off**"

A Moody's/Risk.net survey of **79 MRM leaders (Jan–Mar 2026)** found mature governance already self-reported by 58% of retail, 52% of commercial, 67% of investment banks; ~20% of commercial banks run >1,000 models; many run fewer than 50. **The function considers itself already built.** You are selling into a mature, budget-flat, self-satisfied buyer whose regulator just told them they will not be criticised for non-compliance.

**Insurance:** Chief Risk Officer / Chief Actuary. Obligations are guidance-shaped, not rule-shaped: the NAIC AI Model Bulletin (~24–29 jurisdictions adopted as of mid-2026), NYDFS Insurance Circular Letter No. 7 (11 July 2024), Colorado's ECDIS governance regulation plus a quantitative-testing regulation for life insurers. All impose *reporting*, none impose independent third-party validation.

**Healthcare:** no owner and no mandate. ONC HTI-1's DSI source-attribute requirements bind **certified health-IT developers** (compliance from 31 Dec 2024), not health systems. The only spend number I found is a Black Book survey of 182 hospitals reporting a median **4.2% of the combined IT + quality/safety budget** going to AI governance — but I could only reach it through a secondary site of uncertain provenance. **Treat as unverified.**

---

## 5. WHAT THEY BUY, AND WHAT THEY REFUSE TO BUY

**Buy:** model inventory and registry; documentation automation; regulatory-report generation; workflow and evidence trails; policy packs mapped to EU AI Act / NIST AI RMF / ISO 42001; shadow-AI discovery. Chartis's read of the category: *"automating labor-intensive processes, such as documentation, compliance reporting and inventory maintenance, is a key differentiator."* Buyers are purchasing **paperwork throughput**, not rigour.

**Refuse to buy:**
1. **Autonomous judgement.** Banks have LLM-as-judge available and "few lenders use it to facilitate autonomous sign-off." The signature is the product; they will not outsource it.
2. **Pre-deployment third-party certification.** See §6.
3. **Anything that adds review volume.** Their live complaint is that *low-risk models already face excessive reviews*. A tool that finds more things to validate is negatively valued.

Note the vendor repositioning: **ValidMind's homepage no longer mentions SR 11-7 at all**, and now leads with SR 26-2, EU AI Act, OSFI E-23 and SS1/23. The incumbents rewrote their pitch within four months. Their claims ("70% validation time reduction", "90% cost savings") are **vendor claims**, unaudited.

---

## 6. THE STRUCTURAL PRINCIPLE, TESTED: "INDEPENDENCE IS THE PRODUCT"

The founder asked whether a third party must satisfy a validation requirement. **I looked for a mandate and found four natural experiments. Three failed and the fourth is trivially small.**

| Test | Result |
|---|---|
| **US banking** | SR 11-7's independence requirement was the clause **deleted** on 17 Apr 2026: quality depends on rigour "rather than on organizational structure." Vendor models may be validated "by internal **or** outside parties." **No mandate.** |
| **EU AI Act Art. 43** | Notified body required **only** for Annex III(1) biometrics, and internal control is an option even there. Everything else self-assessed. And it is deferred to Dec 2027 / Aug 2028. **No mandate.** |
| **Healthcare (CHAI assurance labs)** | The flagship independent-assurance experiment. Proposed in JAMA 2023, endorsed by the White House Sept 2024, **quietly collapsed by early 2025**; confirmed dead Feb 2026. CEO Brian Anderson: *"Our initial hypothesis was that the pre-procurement use case was the one that would be most interesting to our doctors and nurses. **It wasn't.**"* Buyers wanted post-deployment monitoring, not independent pre-purchase certification. CHAI is now a registry-and-playbooks body with no lab. **Demand for independent assurance was tested and did not exist.** |
| **NAIC third-party model framework** | The one live attempt to regulate third-party AI/model vendors directly. Exposed 9 Dec 2025; **23 comment letters** called it unworkable (APCIA: vendors would exit; AHIP: OpenAI/Google would refuse to register). The 8 July 2026 revision **downgraded mandatory registration to a voluntary registry** and narrowed scope to P&C pricing/underwriting. Liability stayed with the carrier's filing actuary. Comments closed 5 Aug 2026. **The mandate was attempted and defeated within seven months.** |
| **NYC Local Law 144** | The only in-force US law requiring a genuinely *independent* auditor (AEDT bias audits, enforced since 5 Jul 2023). Annual, per-tool, low four-to-five figures, weakly enforced. **A services line, not a company.** |

**Finding on the structural principle: the logic is sound and the market is not.** It is true that a provider cannot credibly certify itself. It is also true, repeatedly and expensively demonstrated, that **nobody has yet been willing to pay a third party to say so** — because in every regime that exists, liability stays with the deployer, and a deployer who owns the liability wants the evidence in-house where they control it. The principle holds logically and fails commercially. That distinction is the finding.

---

## 7. EVIDENCE GRADING

**Measured / primary (high confidence):**
- OCC Bulletin 2026-13 and its guidance PDF, read directly — rescissions, non-enforceability, GenAI exclusion, $30bn threshold, "rather than on organizational structure", vendor validation "internal or outside parties"
- Regulation (EU) 2026/1744 via EUR-Lex — 8 Jul adopted, 24 Jul published, 27 Jul in force, 2 Dec 2027 / 2 Aug 2028
- EU AI Act Art. 43 conformity-assessment structure
- Colorado SB 26-189 signed 14 May 2026, effective 1 Jan 2027; EO 14365 of 11 Dec 2025
- Risk.net July 2026 benchmarking headlines (44 banks); Moody's/Risk.net 79-leader survey
- Credo AI $41.3M / ValidMind $8.1M / Monitaur $6M funding totals
- DSIT: 524 UK assurance firms, £1.01bn GVA
- CHAI's current site: registry and playbooks, no lab

**Vendor / analyst claim (treat as marketing):**
- Gartner $492M (2026) → >$1B (2030), 20% regulatory-expense reduction, "3.4x more likely" — analyst forecast, category label internally inconsistent, primary page inaccessible to me
- ValidMind's 70%/90% savings figures; Holistic AI's Gartner MQ placement
- Chartis quadrant positions (their MRM report contains **no** market-size number)

**My inference (labelled):**
- $45M–$300M MRM-software SAM from ~300 institutions × $150k–$1M ACV
- "No independent AI-governance vendor has raised a growth round" as evidence of market size
- The read that Gartner's MQ launch signals consolidation rather than expansion

**Unverified / could not confirm:**
- The promised interagency AI/GenAI RFI — announced in the bulletin, **not confirmed issued** as of 2026-08-19 (OCC bulletin index 404'd)
- Black Book 182-hospital 4.2% figure — reached only through a secondary site of uncertain provenance
- Several sources surfaced in search (henecorp.ai, actuary.info, swept.ai, privacyterms.io, aigovernancecore.com, regalert.today) have the texture of AI-generated content farms. I used them only where a primary source or a reputable outlet corroborated the claim.

*Methodological note: this session's web-search budget was exhausted at the first call; all of the above was gathered by driving a browser against Bing and fetching primary documents directly, including binary PDF text extraction of the OCC guidance and the Chartis report.*

---

## 8. BOTTOM LINE AGAINST THE THREE RULES

- **Rule 2 (size first):** done, and the number is disqualifying. **$492M global in 2026 for the whole category**; the MRM-software slice is $45M–$300M; the buyer's headcount is flat; the US regulator just made compliance optional. This is not a venture-scale budget, and unlike last round the number was cheap to get and available first — again.
- **Rule 1 (does it ship free):** partially, and worse. The threat is not a framework shipping the primitive free — it is the **regulator withdrawing the requirement entirely**, which is the same outcome with no vendor to compete against. Meanwhile Microsoft Foundry, Bedrock and Vertex ship evaluation and guardrail tooling into the platform, and IBM/SAS/ServiceNow/Microsoft own the GRC seat at the enterprises that would buy.
- **Rule 3 (thesis without the founder's code):** passes trivially — this space has nothing to do with the caching assets. Which is exactly why it was worth checking, and why the honest answer has to be reported rather than softened.

**Direct answers to the brief:**
- *Who signs?* Head of MRM under the CRO (banking); CRO/Chief Actuary (insurance); nobody with a line item (healthcare).
- *How big and growing?* ~$492M globally, ~20% CAGR to ~$1B by 2030; the banking sub-budget is **flat-to-declining in the US** and tightening only in Europe.
- *What do they buy?* Inventory, documentation automation, regulatory reporting, evidence trails.
- *What do they refuse?* Autonomous sign-off, pre-deployment third-party certification, and anything that increases review volume.
- *How contested?* Extremely — IBM, SAS, Microsoft, ServiceNow, OneTrust and six-plus specialists chasing under half a billion dollars, now with a Gartner MQ.
- *Is there a third-party assurance mandate?* **No.** It was deleted in US banking (Apr 2026), confined to biometrics and deferred in the EU (Jul 2026), collapsed in healthcare (CHAI), and defeated in insurance (NAIC, Jul 2026). The only in-force example is NYC LL144, which is a consulting line item.

**Recommendation: kill this direction and spend the next probe on a budget that is growing and has a control point.** The money in this space in 2026 is going to *securing* AI, not *governing* it — and unlike governance, that budget is expanding, has an unambiguous owner (the CISO), and sits at a point requests must pass through.

Sources: [OCC Bulletin 2026-13](https://www.occ.gov/news-issuances/bulletins/2026/bulletin-2026-13.html) · [OCC guidance PDF](https://www.occ.gov/news-issuances/bulletins/2026/bulletin-2026-13a.pdf) · [Fed SR 26-2](https://www.federalreserve.gov/supervisionreg/srletters/SR2602.htm) · [FDIC release](https://www.fdic.gov/news/press-releases/2026/agencies-issue-revised-model-risk-guidance) · [Sullivan & Cromwell analysis](https://www.sullcrom.com/insights/memo/2026/April/OCC-Fed-FDIC-Issue-Revised-Guidance-Model-Risk-Management) · [Regulation (EU) 2026/1744](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=OJ:L_202601744) · [EU AI Act timeline](https://artificialintelligenceact.eu/implementation-timeline/) · [EC regulatory framework](https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai) · [Risk.net model risk benchmarking](https://www.risk.net/benchmarking/model-risk) · [Moody's/Risk.net MRM survey](https://www.moodys.com/web/en/us/insights/banking/the-evolving-model-risk-management-landscape-in-banking.html) · [Gartner AI governance forecast (syndicated)](https://www.biztechreports.com/news-archive/2026/2/17/global-ai-regulations-fuel-billion-dollar-market-for-ai-governance-platforms-gartner-february-18-2026) · [NAIC Third-Party Data and Models WG](https://content.naic.org/committees/h/third-party-data-models-wg) · [NAIC comment-letter analysis](https://actuary.info/insights/naic-third-party-framework-comment-letters-industry-pushback-2026) · [CHAI assurance labs collapse](https://distilinfo.com/2026/02/22/chais-ai-healthcare-promise-meets-reality/) · [Fierce Healthcare on CHAI](https://www.fiercehealthcare.com/ai-and-machine-learning/inside-chais-failed-assurance-labs) · [CHAI](https://www.chai.org/) · [DSIT AI assurance market](https://www.gov.uk/government/publications/assuring-a-responsible-future-for-ai) · [NYC DCWP AEDT](https://www.nyc.gov/site/dca/about/automated-employment-decision-tools.page) · [Credo AI](https://www.credo.ai/) · [ValidMind](https://validmind.com/) · [Holistic AI](https://www.holisticai.com/)