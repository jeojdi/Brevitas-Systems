# External API Service Level Agreement

**DRAFT — LEGAL/FINANCE/OPERATIONS REVIEW REQUIRED — NOT PUBLISHED — NO CURRENT COMMITMENT**

## Proposed availability

The proposed external API SLA is **99.9% monthly availability**. Brevitas's internal availability
SLO is **99.95%** and is not a contractual guarantee. Critical-data RPO is 15 minutes, service RTO
is 4 hours, and the internal restoration target is 1 hour; counsel must decide whether any belongs
in a signed order form. P0 acknowledgement is 30 minutes with customer updates every hour; P1
acknowledgement is 4 hours.

Monthly availability is proposed as `(eligible minutes - unavailable eligible minutes) / eligible
minutes × 100`. An unavailable minute requires Brevitas's external API to fail valid requests above
the contract's agreed threshold from at least two approved probes. Scheduled maintenance, partial
feature impairment, latency thresholds, regional scope, rounding, evidence priority, and credits
remain `[LEGAL/OPERATIONS TO COMPLETE]`.

## Proposed exclusions

Exclude only the portion directly caused by and contemporaneously documented as:

- an upstream provider outage outside Brevitas's reasonable control;
- invalid, expired, revoked, rate-limited, or unavailable customer credentials;
- customer configuration or customer-caused failure, including unsupported integration changes;
- customer systems, networks, or instructions; or
- approved scheduled maintenance/emergency security maintenance within negotiated limits.

Exclusions are not automatic: Brevitas must preserve monitoring/timeline evidence and demonstrate
causation. Vendor failure remains Brevitas's responsibility where architecture, retry/degradation,
capacity, or vendor management reasonably should have prevented the impact. In all excluded cases,
Brevitas must still degrade safely: do not leak data, cross tenants, duplicate authoritative billing,
accept unauthenticated traffic, persist forbidden content, or treat Redis/local state as authority.
Provide accurate incident updates and recovery effort even when a credit exclusion applies.

## Claim and remedy placeholders

- Customer claim window and required evidence: `[COUNSEL TO COMPLETE]`
- Service credit tiers/caps and sole-remedy language: `[COUNSEL/FINANCE TO COMPLETE]`
- Maintenance windows and notice: `[OPERATIONS/COUNSEL TO COMPLETE]`
- Measurement source, dispute process, and force majeure: `[COUNSEL TO COMPLETE]`
- Support channels, support hours, and severity authority: `[SUPPORT TO COMPLETE]`

The incident runbook, status template, and recovery objectives are internal operational controls,
not incorporated unless an executed agreement says so. Legal counsel must review the SLA, DPA,
exclusions, remedies, and seven-year financial retention together before publication.

No part of this draft claims HIPAA, PCI storage, FedRAMP, or uninterrupted service.
