# Disaster-recovery infrastructure template

`resilience-policy.template.yaml` is a reviewed desired-state template, not a deployed resource.
It deliberately has no provider account, region name, storage bucket, key identifier, contact, or
credential. Replace every `TO_BE_SELECTED`/`*_REQUIRED` value in an approved staging change,
validate the result against `docs/enterprise/DISASTER_RECOVERY.md`, and obtain two-person approval
before applying it through the selected provider's pinned infrastructure tool.

The template coordinates Supabase PITR and logical backups with Redis Cloud settings. Redis is
non-authoritative and must be rebuilt from Postgres after loss. Do not restore a Redis snapshot as
the source of billing, tenant, job, or configuration truth.

Logical drills use only the `brevitas-ephemeral-postgres-v1` contract: an externally created empty
PostgreSQL 16 database with the declared compatibility roles/extensions. It is not a Supabase
project template. A source-bound deletion artifact is stored independently, hash-verified, and
replayed after raw restore verification but before readiness, even when it contains no tombstones.

Retention is a separate Railway service, not `api.worker`. Build it with
`retention-worker.Dockerfile` and the repository-only
`railway-retention-worker.json` template. Two replicas provide process failover, while
`compliance_retention_worker_cycle` holds one transaction-scoped Postgres advisory authority across
dry-run, bounded apply, and post-apply verification. Import `retention-alerts.yaml` only during an
approved staging change and scrape the private `/metrics` endpoint; do not assign this worker a
public domain.
