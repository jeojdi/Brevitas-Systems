# Restore and tabletop evidence template

**INTERNAL TEMPLATE — NO CUSTOMER DATA OR SECRETS — NOT FOR PUBLICATION**

This Markdown worksheet is not release evidence and cannot satisfy the operational release gate.
For a release-bound restore exercise, translate independently retained receipts into the strict
[`operational-readiness-evidence.schema.json`](operational-readiness-evidence.schema.json) envelope
and validate it as described in
[`OPERATIONAL_READINESS_GATE.md`](OPERATIONAL_READINESS_GATE.md). Never mark this template complete
without executing the external actions.

- Exercise/change ID (opaque):
- Scenario: PITR / logical restore / Redis loss / regional outage / breach tabletop
- Incident commander role:
- Operations lead role:
- Security/privacy lead role:
- Evidence owner role:
- Backup source ID and source environment:
- Isolated destination ID and destination environment:
- Independent backup evidence reference:
- Expected manifest SHA-256 and independently verified source:
- Restore target mode (`ephemeral-postgres`), PostgreSQL major (16), and exact database name:
- Independent deletion evidence reference, artifact SHA-256, and independently verified source:
- Deletion artifact issued strictly after backup: yes/no
- Raw table verification timestamp and result:
- Deletion replay timestamp, tombstone count (including zero), and result:
- Readiness timestamp (must be after raw verification and replay):
- UTC start, last known-good point, selected recovery point, database ready, service ready, end:
- Observed critical-data RPO (target: 15 minutes):
- Observed internal restoration time (target: 1 hour):
- Observed service RTO (target: 4 hours):
- PITR window observed (required: 14 days):
- Logical backup age and retention (required: daily / 35 days):
- Ciphertext and manifest SHA-256 references:
- Table-level verification evidence path/hash:
- Tenant isolation, billing reconciliation, job recovery, audit append, and health results:
- Redis rebuilt from authoritative Postgres: yes/no/not applicable
- Decisions, exceptions, remediation owner roles, and due dates:
- Approver roles and approval timestamps:

Do not record a DSN, credential, customer name/email, prompt/response, raw export, database row, or
real personal contact. Attach only restricted, content-free evidence.
