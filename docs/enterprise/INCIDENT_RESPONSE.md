# Incident response runbook

**INTERNAL DRAFT — LEGAL/SECURITY REVIEW REQUIRED — NOT PUBLISHED**

## Severity and clocks

| Severity | Example | Acknowledgement | Update cadence |
| --- | --- | --- | --- |
| P0 | Broad outage, critical integrity loss, confirmed/severe security event | Within 30 minutes | Customer updates every hour while active |
| P1 | Material degradation or contained high-impact failure | Within 4 hours | At each material change and agreed support cadence |

For a confirmed personal-data breach, notify Company A within **24 hours of confirmation**. Where
GDPR regulator notification is applicable, the controller's **72-hour** clock must be supported;
escalate immediately and do not delay Company A notice while final scope is investigated. Legal and
privacy owners decide regulator/data-subject notifications. The official GDPR text is
<https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng>.

## Roles (populate through the approved on-call system; no contacts here)

- Incident commander: owns severity, decisions, timeline, handoffs, and closure.
- Operations lead: contains impact, restores service, preserves command output and change evidence.
- Security/privacy lead: assesses confidentiality/integrity, breach confirmation, legal clocks, and
  evidence preservation.
- Communications lead: issues approved, accurate, content-free customer/status updates.
- Company A liaison: maintains the contractual channel and acknowledgement evidence.
- Scribe/evidence owner: records UTC events, opaque IDs, approvals, and follow-ups.
- Executive and legal approvers: authorize material customer/regulator statements and exceptions.

No single responder should both approve and execute destructive recovery. Use the on-call directory
and escalation system; never copy real phone numbers or emails into this repository.

## Response sequence

1. Acknowledge, open an opaque incident ID, start a UTC timeline, assign roles, and set severity.
2. Contain safely: stop risky writes, revoke/disable affected access, preserve immutable logs, and
   select a documented degraded mode. Do not destroy suspected evidence.
3. Assess affected tenants, data categories, regions, start/end times, integrity, availability, and
   whether personal data is involved. General incident telemetry remains content-free.
4. At confirmed personal-data breach time, record the confirmation rationale and start the 24-hour
   Company A notice and applicable 72-hour regulator support clocks.
5. Restore from authoritative Postgres using `DISASTER_RECOVERY.md`; reconcile tenants/billing/jobs
   and canary before reopening traffic.
6. Communicate on cadence even when there is no material change. Clearly label estimates.
7. Close only after monitoring is stable, affected access is rotated, customer commitments are met,
   and follow-ups have owners/dates. Hold a blameless review within five business days for P0.

## Content-free status template

> Incident `<opaque-id>` — `<investigating|identified|monitoring|resolved>` — UTC `<time>`<br>
> Impact: `<services/regions and aggregate impact; no names, emails, content, or credentials>`<br>
> Current action: `<containment/restoration/verification>`<br>
> Customer action: `<none or safe fixed instruction>`<br>
> Next update: `<UTC time, no later than one hour for P0>`

## Company A breach notice template

**DRAFT — PRIVACY/LEGAL APPROVAL REQUIRED**

- Opaque incident ID and confirmation UTC time
- Nature of the incident and personal-data categories (no raw records)
- Approximate affected population/record count, if known
- Likely consequences and containment/restoration steps
- Safe customer actions
- Brevitas privacy/security role channel from the approved contract directory
- Known gaps, next update UTC, and regulator-support status

Record delivery and acknowledgement evidence in the restricted incident system. Do not send from a
repository script.

## Post-incident record

Retain timeline, approvals, immutable audit references, recovery evidence, notification decisions,
and remediation verification under the applicable 400-day security/audit schedule. Support records
follow the 24-month schedule. Legal hold overrides ordinary deletion until explicitly released by
the authorized compliance role.
