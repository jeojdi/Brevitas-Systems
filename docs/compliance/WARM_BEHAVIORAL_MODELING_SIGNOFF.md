# Decision 4 — Behavioral-Modeling Classification & Sign-off

**Status: DRAFT — awaiting James's signature. Migration `202608100003_warm_customer_state_hazard.sql` must not be applied to any remote environment until the sign-off block below is completed.**

## What this decision covers

Phase 1 of predictive warming introduces `warm_customer_state`: per-(organization, end-customer, provider) rows holding **decayed hour-of-week arrival statistics, a P(alive) activity estimate, a human-vs-machine regime label, and an activity-phase estimate from which a coarse timezone can be inferred**. This is materially finer profiling than the previously shipped plaintext hour histogram on `warm_prefixes`, so it requires a documented classification decision and DPA alignment before processing begins (per the governance research adopted in the plan: EDPB Guidelines 1/2024 balancing factors, EDPB 01/2025 pseudonymization, CCPA 11 CCR 7050/7051).

## Classification decision (proposed)

1. **The data is personal data**, pseudonymized, processed by Brevitas as an Article 28 **processor** on the org's documented instruction. It is never anonymous: EDPB 01/2025 holds that hashed/keyed identifiers remain personal data. It is classified **Confidential** under the SOC 2 data schedule.
2. **Contents are metadata only.** Arrival timestamps, decayed count maps, token counts, dollar amounts, regime/phase labels. No prompt or completion content, no names, no free text. The telemetry prohibition (RETENTION_AND_PRIVACY.md) applies unchanged.
3. **Lawful basis** is the org's (controller's) legitimate interest: purpose = cost reduction with byte-identical outputs; necessity = cannot be achieved with less data than timing metadata; balancing = pseudonymized keys, coarse features, no visibility to the data subject, and no Article 22 effect (the data subject's outputs are unchanged; no decision about the person is made). Warming is an enumerated processing activity in the DPA, and the per-org warming opt-in (`warm_credentials` consent row) is the controller's documented instruction.
4. **Timezone-phase inference is accepted** as part of this classification on the grounds that it is derived solely from request timing already in our possession, stored as a coarse phase bucket, never joined to location data, and used only to schedule cache warming. It is disclosed in the DPA processing description.
5. **Tenant isolation and erasure**: rows are keyed per (org, end customer); never pooled across orgs; erasure = row deletion plus a suppression-list entry consulted by the scheduler; org-level aggregates are documented as aggregates. The 13-month behavioral horizon and 30-day post-erasure deadline in the retention schedule apply.

## Required before first remote apply

- [ ] DPA template updated to enumerate: "predictive cache warming, including modeling of end-customer usage timing (arrival patterns, activity rhythms, activity-phase estimates) from request metadata."
- [ ] Trust-page sentence live (already drafted in the plan doc) — must be literally true at enablement.
- [ ] This document signed.

## Sign-off

| Role | Name | Decision | Date |
|---|---|---|---|
| Founder / controller-facing owner | James Yang | ☐ APPROVED ☐ CHANGES | |
| Drafted by | Claude (session record) | proposed | 2026-08-10 |

*Sign by replacing the checkbox and dating the row; the migration's header references this file.*
