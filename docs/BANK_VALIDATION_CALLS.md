# Ten calls: does anyone actually REQUIRE self-hosted or open-weight models?

**Why this exists.** Two 14-agent research workflows (2026-08-31) both ended on the same question and
both declared it unanswerable by search: *has any bank RFP or third-party-risk questionnaire ever
required self-hosted or open-weight models?* 28 agents found zero instances. That is not proof of
absence — it is proof that the answer is not on the public web. Ten conversations settle it.

**The rule, set BEFORE the calls so the result is binding.** If 8+ of 10 say the requirement has never
appeared in writing, the direction is closed. Write the outcome down either way.

---

## What is already established — do not re-ask these

Going in cold and asking "do banks worry about data privacy?" wastes the call. These are settled:

- **SR 11-7 was rescinded 2026-04-17** (SR 26-2 / OCC Bulletin 2026-13 / FDIC FIL-15-2026). The
  replacement says verbatim: *"Generative AI and agentic AI models are novel and rapidly evolving.
  As such, they are not within the scope of this guidance."* Citing SR 11-7 marks you as out of date.
- **Zero of ~13 named Western tier-1 banks run open weights as a primary stack.** JPMorgan LLM Suite is
  OpenAI + Anthropic in-perimeter (~250k employees). BBVA put 120,000 employees on ChatGPT Enterprise.
- **The two Western banks that genuinely self-host bought CLOSED weights** — HSBC/Mistral (2025-12-01),
  BNP Paribas, ABN AMRO (2026-08-11).
- **Air-gapped no longer implies open weights** — Gemini GA on Google Distributed Cloud air-gapped
  since 2025-08-27, authorized to US Top Secret.
- **DORA Art. 30** requires disclosed processing locations, audit rights and exit plans. It names
  "change to in-house solutions" as one *permitted* exit, not a required state.

Referencing two of these in the first minute is what earns the rest of the call.

---

## Who actually holds the answer

Ask for the role, don't assume it. The decision is usually split:

| Role | What they own | Will they know? |
|---|---|---|
| Third-party risk management (TPRM) | The vendor questionnaire itself | **Yes — they own the document** |
| CISO office / security architecture | Data-handling standard, approved-service list | Yes |
| Model risk management | Model inventory, validation | Post-rescission, may no longer touch LLMs |
| Head of financial crime technology | The AML/fraud stack and its budget | Knows the buy, not the standard |
| Cloud / AI platform lead | What actually got deployed | Yes, on deployment; no, on requirements |

**TPRM is the highest-value target** — the question is about a document they personally maintain.

---

## The question set

Rules of construction: every question is answerable from a document they have in front of them, not
from opinion; the disqualifying question is phrased so "no" is the easy and face-saving answer.

**Anchor (2 min).** "I'm trying to kill an idea, not sell one. I had assumed regulated buyers were
forced toward self-hosted open models. Everything I can find says the opposite — including that SR 11-7
was rescinded in April and generative AI is explicitly out of scope now. I want to check that against
someone who actually owns the paperwork."

1. When you last assessed an AI or LLM vendor, what did the questionnaire ask about **where the model
   runs**? Can you tell me the actual section heading?
2. Does your TPRM questionnaire contain **any** question about model weights — open versus proprietary?
   Yes or no is a complete answer.
3. Who signs off on model hosting — TPRM, the CISO office, model risk, or the platform team?
4. **[DISQUALIFIER]** Has a requirement for self-hosted or open-weight models ever appeared **in
   writing** in one of your RFPs, standards or questionnaires? *I expect the answer is no — most people
   tell me contractual controls cover it — but I'd rather hear it directly.*
5. What did you actually deploy: which model, running where, under what agreement?
6. What made in-tenant frontier acceptable? Was there a specific clause or control that unlocked it?
7. Is there any data class that still **cannot** go to an in-tenant frontier model? What is it, and what
   happens to that work today?
8. Since the April rescission put generative AI out of model-risk scope, what do you validate LLM
   systems against now — anything, or nothing yet?
9. Does financial-crime AI spend come out of the **software** line or the **operations/headcount** line?
10. When you looked at Verafin's agentic tools or the FIS/Anthropic agent, what did you ask that they
    couldn't answer?
11. If a company my size showed up today, what's the first gate we fail?
12. Who else should I be asking?

**Q4 is the whole call.** Q7 finds the residual segment if one exists. Q10 is where a real gap would
surface. Q11 tells you whether any answer matters.

---

## Reaching people (public professional channels only)

Do not scrape or cold-mail personal addresses. The paths that work:

- **Practitioner bodies with open membership or events**: ACAMS (AML practitioners), FS-ISAC, BPI/BITS.
  ACAMS chapter events are the densest concentration of exactly these roles.
- **People who already speak publicly** — conference speakers and bylined authors on bank AI
  infrastructure are reachable because being reachable is part of the role. Money20/20, Sibos,
  AI in Finance Summit, the Evident AI Index team (they survey bank AI adoption and publish it).
- **Warm paths through the mapped network** (~1,415 connections, portfolio referral map 2026-08-19).
  The intermediaries that actually convert: former colleagues now inside a bank, portfolio companies of
  shared investors, and vendor employees willing to introduce a prospect.
- **Vendor-adjacent**: people at Unit21, Hawk, Bretton AI and Verafin talk to these buyers daily and
  will often answer the requirements question directly — a cheaper first call than the bank itself.

*(A verified named-candidate list is being assembled by a separate research pass; merge it here.)*

---

## Recording the result

One row per call: date, role, institution type, **Q4 answer (yes / no / has-never-come-up)**, the
residual data class from Q7 if any, and the first gate from Q11. Ten rows decide it.
