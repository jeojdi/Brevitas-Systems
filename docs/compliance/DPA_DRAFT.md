# Data Processing Addendum

**DRAFT — LEGAL REVIEW REQUIRED — NOT PUBLISHED — NOT AN EXECUTED AGREEMENT**

This outline is for counsel to turn into an executed DPA between the customer (including Company A,
as applicable) and Brevitas. Bracketed items require contract-specific completion.

## 1. Roles, scope, and instructions

The customer is controller/business and Brevitas is processor/service provider for personal data
processed to provide the contracted routing, optimization, administration, support, security, and
billing services. Brevitas processes only documented instructions, the agreement, and applicable
law; it notifies the customer if an instruction appears unlawful unless prohibited. The parties
must complete the subject matter, duration, nature/purpose, data categories, data-subject groups,
authorized users, regions, and special-category restrictions in an annex.

Raw synchronous prompts/responses are not persisted by default. Encrypted asynchronous payloads
expire after 1 hour by default and never later than 24 hours. Semantic cache is disabled by default
and never exceeds 24 hours when enabled. General telemetry excludes names, emails, prompts, and
responses. The complete draft schedule is `RETENTION_AND_PRIVACY.md`.

## 2. Confidentiality and security

Brevitas will limit access to authorized personnel bound by confidentiality; maintain tenant
isolation, least privilege, encryption in transit/at rest, managed KMS interfaces, secret rotation,
content-free immutable administrative audit, secure development/release gates, monitoring, incident
response, and tested continuity controls; and periodically test effectiveness. Exact technical and
organizational measures belong in a signed security annex and must match verified production state.

## 3. Subprocessors

Brevitas may use only reviewed subprocessors in `SUBPROCESSORS_DRAFT.md`, under written data
protection terms no less protective for applicable processing. The signed DPA must choose specific
or general authorization, notice period `[30 days proposed]`, objection/remediation process, and
current register URL. Brevitas remains responsible as required by applicable law and contract.

## 4. Assistance and data rights

Brevitas will reasonably assist with access, correction, deletion, restriction, objection,
portability, DPIAs, consultations, and verified inquiries. The operational target for export or
deletion is within 30 days; primary deletion is within 30 days and rotating-backup expiry within
35 days, subject to documented legal holds and required financial-record preservation. Requests are
tenant-scoped, verified, audited, and executed through `DATA_RIGHTS.md` controls.

For CCPA/CPRA, counsel must add the required service-provider/contractor restrictions: no sale or
sharing, no processing outside specified business purposes, no combining except as permitted,
appropriate verification/monitoring, and assistance with consumer requests. California guidance:
<https://oag.ca.gov/privacy/ccpa>.

## 5. Security incidents

Brevitas notifies Company A within 24 hours after confirming a personal-data breach and provides
available information needed for the customer's obligations, without admission of liability. It
supports the controller's applicable GDPR regulator notification within 72 hours, supplies updates
as investigation develops, mitigates harm, preserves evidence, and documents decisions. Legal must
define incident, recipient, channel, clock, content, law-enforcement delay, and costs in the signed
agreement. The operational runbook is `../enterprise/INCIDENT_RESPONSE.md`.

## 6. International transfers and residency

Initial processing is colocated in one US region. If a contract requires EU residency, Brevitas
must deploy the approved EU topology before processing that tenant. For restricted EEA transfers,
the parties will execute the then-current EU Standard Contractual Clauses (typically controller-to-
processor Module Two where roles match), complete annexes/docking/optional clauses, select governing
law and forum, conduct a transfer impact assessment, and add supplementary measures. Do not claim
SCC coverage until executed. Counsel must also address UK/Swiss mechanisms where applicable.

Official GDPR text: <https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng>.
European Commission SCC materials:
<https://commission.europa.eu/law/law-topic/data-protection/international-dimension-data-protection/standard-contractual-clauses-scc_en>.

## 7. Return, deletion, audits, and conflict

At termination, Brevitas returns or deletes personal data on instruction, subject to law, the
contract-term-plus-30-day configuration schedule, and backup rotation. It provides appropriate
compliance information and audit support under negotiated confidentiality, frequency, scope, and
cost limits without exposing other tenants or weakening security. Mandatory law and executed SCCs
control over conflicting terms.

## Annex placeholders

- Parties, effective date, contacts, signatures: `[LEGAL TO COMPLETE]`
- Processing details and special-category prohibition/approval: `[LEGAL/CUSTOMER TO COMPLETE]`
- Technical and organizational measures with verified system evidence: `[SECURITY TO COMPLETE]`
- Approved subprocessors, locations, functions, transfer mechanisms: `[PRIVACY TO COMPLETE]`
- SCC modules/annexes and transfer impact assessment: `[LEGAL TO COMPLETE]`
- Deletion/return certification and audit terms: `[LEGAL TO COMPLETE]`

Counsel must review this DPA, the SCC package, incident notice, and seven-year financial retention
before signature or publication. This draft does not promise HIPAA, PCI storage, or FedRAMP support.
