# Retention and privacy schedule

**DRAFT — LEGAL REVIEW REQUIRED — NOT PUBLISHED — NOT LEGAL ADVICE**

Storage limitation is the default. A system owner must map each store to one row below, implement
automated expiry, and produce content-free deletion evidence. A contract may shorten a period.
Extending one requires documented purpose, legal approval, customer terms where applicable, and a
new control review. An authorized legal hold pauses deletion only for the narrow covered scope.

| Data | Retention |
| --- | --- |
| Raw synchronous prompts/responses | Never persist by default |
| Encrypted queued payload/result | 1 hour default; 24 hours maximum |
| Semantic cache content | Disabled by default; 24 hours maximum when enabled |
| Content-free usage metadata | 13 months |
| API operational logs | 30 days |
| Security and administrative audit logs | 400 days |
| Support records | 24 months |
| Customer configuration and identifiers | Contract term plus 30 days |
| Billing ledger, invoices, and tax records | 7 years, subject to counsel review |
| Supabase PITR | 14 days |
| Separate encrypted logical backups | 35 days |
| Account deletion from primary systems | Within 30 days |
| Deletion from rotating backups | Within 35 days |

The seven-year billing/tax period is a preservation exception, not permission to retain unrelated
customer content. Retained financial rows are access-restricted, purpose-limited, and minimized.
Immutable security/administrative audit and billing records remain content-free and use opaque IDs.

## Telemetry prohibition

Names, emails, prompts, responses, request/response bodies, queued content, semantic-cache content,
provider credentials, session tokens, complete URLs, database DSNs, and raw data exports must not
enter general telemetry. General logs, traces, metrics, alerts, dashboards, and incident status use
only allowlisted opaque request/job/tenant IDs, fixed operation/result categories, status codes,
durations, aggregate counts, and approved provider/model identifiers. Data-rights exports never
source content from general telemetry because general telemetry contains no such content.

## Enforcement and review

- Queue/result cleanup runs frequently enough to meet the 1-hour default and hard 24-hour maximum.
- Semantic cache is opt-in, tenant-scoped, encrypted, and TTL-capped at 24 hours.
- Log/export destinations enforce lifecycle rules independently of application cleanup.
- PITR is configured to a 14-day window; encrypted logical backup sets are pruned at 35 days with
  `scripts/dr/prune-logical-backups.py` under explicit confirmation. Age comes only from a validated
  immutable manifest timestamp; copy/touch/mtime metadata is ignored.
- Deletion creates a backup tombstone whose expiry is no later than 35 days after request receipt;
  every restore must verify an independently protected artifact newer than its backup and replay
  all completed tombstones before readiness. A zero-tombstone artifact is still verified.
- Control owners review retention configuration quarterly and after a new data store, vendor,
  product feature, legal requirement, or contract.

## Authoritative database retention job

The dedicated `scripts/dr/retention-worker.py` Railway service schedules retention daily at
**03:15 UTC**, after the daily logical backup. It is separate from `api.worker`. Two process replicas
may run for failover, but `public.compliance_retention_worker_cycle(...)` admits only one transaction
through a Postgres advisory authority and performs a dry-run, one bounded apply when needed, and a
post-apply dry-run before releasing that authority. It repeats bounded cycles until the backlog is
zero, with retries, jitter, graceful shutdown, and content-free `/live`, `/ready`, and `/metrics`
state. The default batch is 5,000 and the hard per-class cap is 10,000.

`scripts/dr/retention.sh` remains the explicit operator recovery command. Run its database-connected
`--dry-run` first, review content-free candidate counts, then apply with exact confirmation and a new
opaque run UUID through `public.compliance_run_retention(uuid,text,integer,boolean)`. Import
`infra/dr/retention-alerts.yaml` in staging and alert on a missed success or
continuous backlog beyond 24 hours, schema-contract failure, legal-hold evaluation failure,
financial-ledger invariant failure, and repeated worker errors.

The job enforces:

- non-financial `usage_log` deletion after 13 months;
- immutable `audit_events`, completed data-rights requests/tombstones, released holds, and prior
  retention-run evidence deletion after 400 days;
- `support_records` deletion after 24 months when that optional table exposes the required
  `organization_id` and `created_at` contract; schema drift fails closed;
- preservation of every usage row referenced by `billing_ledger`, regardless of age. Automated
  financial deletion remains disabled until counsel approves the seven-year endpoint and the
  invoice/tax reconciliation path; and
- preservation of every tenant row covered by an unexpired active legal hold.

Audit and tombstone tables reject ordinary mutation, including service-role mutation. Candidate
queries and mutations both stop at the per-class batch cap; applied evidence stores the exact
candidate and deleted counts so an idempotent replay returns the same content-free result. The migration
uses a non-callable security-definer maintenance helper that transactionally takes the table lock,
temporarily disables only the named delete trigger, performs one bounded delete, and re-enables it.
There is no caller-settable session bypass. Applied runs insert immutable content-free evidence;
replaying the same run UUID and exact actor/batch is idempotent, while conflicting reuse fails.
Released hold rows are retained while any other active hold exists for that tenant. Because
retention-run evidence is global/content-free rather than tenant-bound, no such evidence is removed
while any active tenant hold exists anywhere; this broad preservation rule fails safely.

Export metrics only for candidate/deleted counts, duration, result category, and opaque run ID.
Alert on RPC failure, schema-contract failure, any count above the configured cap, a nonzero backlog
for more than 24 hours, a missed daily run, legal-hold evaluation failure, or financial-ledger
preservation failure. Names, emails, row values, and deletion content never enter those metrics or
alerts.

GDPR requires purpose limitation, data minimization, storage limitation, security, erasure, and
portability. See the official text: <https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng>. CCPA access,
deletion, correction, and opt-out/limit obligations must be assessed for the applicable business
role; see California guidance: <https://oag.ca.gov/privacy/ccpa>.
