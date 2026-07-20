# SOC 2 readiness and evidence plan

**INTERNAL DRAFT — AUDITOR/LEGAL REVIEW REQUIRED — NOT A CERTIFICATION OR PUBLIC CLAIM**

Brevitas targets SOC 2 readiness immediately, SOC 2 Type I before broad enterprise production,
and SOC 2 Type II only after a six-month evidence period. Until an independent report is issued,
do not describe Brevitas as SOC 2 certified, compliant, audited, or attested.

The initial control scope covers Vercel website/dashboard/auth/billing routes; Railway API, workers,
and private compressor; Supabase Postgres/Auth; Redis Cloud coordination; provider integrations;
managed secrets/KMS; source control and CI; monitoring; incident response; data rights; and vendors.

| Control area | Minimum evidence |
| --- | --- |
| Access/change | Quarterly access review, least-privilege roles, immutable admin audit, reviewed PR and blocking release gates |
| Availability | SLO/alert review, daily backup reports, quarterly restore evidence, incident/tabletop records |
| Confidentiality | KMS/key-version audits, secret scan, redaction tests, tenant isolation, encrypted backup evidence |
| Processing integrity | Billing reconciliation, durable-job lease recovery, database migration tests, restore table checks |
| Privacy | Approved retention schedule, DSR export/deletion evidence, legal holds, DPA/SCC/subprocessor reviews |

Evidence contains opaque IDs and fixed result categories, not names, emails, prompts, responses,
credentials, or database contents. Store evidence in an access-controlled immutable system with
owner, control, period, source hash, reviewer, exception, and remediation fields. Review evidence
monthly during the six-month Type II period and escalate missing/late evidence.

Launch gates include completed staging provisioning, migrations, secret configuration,
1,000-concurrency and failure tests, backup restoration, tenant/billing reconciliation, production
credential rotation, and canary rollout. These are operational gates; repository readiness alone
does not satisfy them.

This program does not claim HIPAA, PCI storage, or FedRAMP support. Stripe handles payment-card
processing; Brevitas must not store card data. Scope, control descriptions, and publication require
independent auditor and legal review.
