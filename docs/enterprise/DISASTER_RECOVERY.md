# Disaster recovery and continuity runbook

**INTERNAL DRAFT — APPROVAL AND STAGING VALIDATION REQUIRED — NOT A CUSTOMER COMMITMENT**

## Objectives and authority

Brevitas targets a **15-minute critical-data RPO**, **4-hour service RTO**, and **1-hour internal
restoration target**. The customer-facing external API target is documented separately from these
recovery objectives. No repository command provisions infrastructure, changes DNS, restores a live
database, or applies a migration. Every staging or production action requires an approved change,
incident commander, operations lead, security lead, and evidence owner. Production operations use
two-person approval.

Postgres is authoritative. Redis is coordination infrastructure only. Raw synchronous prompts and
responses are not persisted by default, so they are not backup inputs. Names, emails, prompts, and
responses must not enter general recovery telemetry or status messages.

## Protected topology and schedule

| Control | Required state | Evidence schedule |
| --- | --- | --- |
| Supabase Postgres/Auth | Team or Enterprise, PITR enabled, 14-day recovery window, Supavisor for application pooling | Daily automated configuration check; monthly evidence review |
| Encrypted logical backup | Daily at 02:15 UTC, streamed `pg_dump` custom format through age encryption, immutable private storage | Every run produces ciphertext hash, table counts, and content-free evidence |
| Logical backup retention | 35 days, then guarded deletion of ciphertext and sidecars | Daily lifecycle report; monthly sample |
| Restore exercise | Separately created isolated PostgreSQL 16 database; raw table verification, deletion replay, then smoke verification | Quarterly and after material schema/backup changes |
| Tabletop | P0 outage plus personal-data-breach scenarios | Twice yearly |
| Redis Cloud | Paid multi-zone, TLS, AOF every second; never authoritative | Quarterly configuration evidence and failover exercise |

The independent logical backup must not use the same credentials, administrative account, or
storage failure domain as the primary database. Its encryption recipient is public; the matching
age identity is KMS-wrapped or otherwise controlled by the approved managed-key system and is
injected only into the isolated restore runner. The final provider adapter must use workload
identity, immutable key versions, least privilege, audit logs, and two-person recovery-key access.
Never commit or pass a DSN, age identity, key, or token as a command-line literal.

## Logical backup procedure

1. Confirm PITR is enabled and its oldest recovery point covers 14 days. Capture content-free
   provider evidence; do not copy customer data into the ticket.
2. Use a dedicated direct Postgres backup connection, not the Supavisor transaction-pool endpoint.
   The credential comes from a managed runtime secret named by `--database-url-env`.
3. Run an offline plan first:

   ```bash
   scripts/dr/backup-logical.sh --environment staging --source-id staging-us \
     --output-dir /restricted/backup-staging --dry-run
   ```

4. Under an approved staging change, inject `BACKUP_DATABASE_URL` and the public
   `BREVITAS_BACKUP_AGE_RECIPIENT`, then use `--apply --confirm BACKUP:staging-us`. Production also
   requires `--allow-production` and approval. The script streams the dump into encryption; no
   plaintext dump lands on disk.
5. Upload the `.dump.age`, `.manifest.json`, and `.backup-evidence.json` set to private immutable
   storage. Store the manifest hash in the restricted evidence system. Failed or incomplete sets
   are quarantined and alerted; they do not satisfy the daily backup control.
6. Exercise retention in dry-run mode. Applying deletion requires
   `--apply --confirm PRUNE:<source>:35D`; production also requires `--allow-production`. Pruning
   validates the ciphertext, manifest, immutable evidence sidecar, source/environment, and manifest
   backup timestamp. File copy/touch/mtime changes never make a backup older or newer.

The manifest hashes the encrypted artifact and records exact per-table counts for `public` and
`auth` tables. The script exports one read-only repeatable-read snapshot and imports that same
snapshot into every count and `pg_dump`, so concurrent writes cannot create a false mismatch.
Counts and schema names are evidence; row content is never evidence. Storage object versioning/object
lock protects the manifest and ciphertext from correlated modification.

## PITR recovery procedure (14-day window)

Use PITR when the required recovery point falls inside the 14-day window and provider recovery is
the fastest safe path. Supabase documents backups and PITR at
<https://supabase.com/docs/guides/platform/backups>.

1. Declare an incident, freeze writes or place the API in safe read-only/degraded mode, and record
   the last known-good UTC transaction time plus a conservative recovery timestamp. Never guess
   local time.
2. Confirm the recovery timestamp is newer than the oldest available point and meets the
   15-minute critical-data RPO. Record the observed gap. If it does not, escalate immediately and
   select the independent logical backup path.
3. Review Supabase's current PITR procedure and warnings in the provider console. Capture the
   project reference and timestamps—not the database URL. Obtain two-person approval.
4. Restore into an isolated target whenever the provider supports it. If only in-place recovery is
   possible, preserve forensic evidence, confirm that writes are stopped, and record the rollback
   decision before initiating the provider action.
5. Use an isolated verification runner to check migration level, constraints/index validity,
   table counts, latest immutable audit event, billing ledger continuity, queued-job expiry, and
   tenant separation. Do not send provider requests or customer alerts from verification.
6. Rotate credentials that could have been exposed, reconnect API/workers with a controlled canary,
   reconcile billing and tenant counts, then restore traffic. DNS changes require separate approval.

## Independent logical restore

The logical restore target is a separately created, empty **PostgreSQL 16** database in explicit
`ephemeral-postgres` mode. It is not a fresh Supabase project and the repository scripts do not
provision one. The target image must make `pgcrypto` and `vector` available. Bootstrap creates only
those extensions, the `anon`, `authenticated`, and `service_role` compatibility roles, and a
source/evidence-bound `brevitas_restore` control schema; the encrypted archive creates the `public`
and `auth` application objects. Because the logical archive deliberately omits provider ownership
and ACLs, `ready_at` means ready for isolated integrity/application verification only—not approved
for public traffic. Managed Supabase production recovery remains provider-controlled PITR under the
preceding procedure and preserves/rebuilds provider IAM through the provider-supported path.

Generate a deletion artifact from the authoritative source after the chosen backup. It contains
content-free completed tombstones, is bound to the backup manifest hash/source, and must have an
issuance time strictly newer than the backup. Store it independently of the backup failure domain,
protect it immutably, and copy its SHA-256 and opaque evidence reference through an independent
channel. The gate applies including zero tombstones: even an empty artifact must be verified and
replayed; absence of
deletions is evidence, not permission to skip the gate.

```bash
scripts/dr/export-deletion-artifact.sh --environment production --source-id production-us \
  --backup-manifest /restricted/backup.manifest.json \
  --expected-manifest-sha256 <sha256-from-independent-backup-evidence> \
  --evidence-reference evidence:deletions:opaque-id \
  --output-dir /independent/deletion-evidence --dry-run
```

After an operator separately creates the empty database, bind it to both independent hashes before
decrypting anything:

```bash
scripts/dr/bootstrap-restore-target.sh --environment staging \
  --target-id restore-drill-2026q3 --target-mode ephemeral-postgres \
  --expected-database-name brevitas_restore_2026q3 \
  --source-environment production --source-id production-us \
  --expected-manifest-sha256 <sha256-from-independent-backup-evidence> \
  --expected-deletion-artifact-sha256 <sha256-from-independent-deletion-evidence> \
  --deletion-evidence-reference evidence:deletions:opaque-id --dry-run
```

Bootstrap apply mode requires `RESTORE_DATABASE_URL` and exact confirmation
`BOOTSTRAP:production-us:restore-drill-2026q3`. It fails unless the database name is exact, the
server major is 16, and no `public`/`auth` application tables exist.

```bash
scripts/dr/restore-logical.sh --environment staging --target-id restore-drill-2026q3 \
  --target-mode ephemeral-postgres --expected-database-name brevitas_restore_2026q3 \
  --source-environment production --source-id production-us \
  --manifest /restricted/backup.manifest.json \
  --encrypted-backup /restricted/backup.dump.age \
  --expected-manifest-sha256 <sha256-from-independent-evidence-system> \
  --backup-evidence-reference evidence:backup:opaque-id \
  --deletion-artifact /independent/deletions.json \
  --expected-deletion-artifact-sha256 <sha256-from-independent-deletion-evidence> \
  --deletion-evidence-reference evidence:deletions:opaque-id \
  --evidence-dir /restricted/dr-evidence --dry-run
```

An approved apply additionally needs `RESTORE_DATABASE_URL`, the managed-secret
`BREVITAS_BACKUP_AGE_IDENTITY`, `--apply`, and the exact
`--confirm RESTORE:production-us:restore-drill-2026q3`. It never logs either secret. The chain
preflights the exact bootstrap control, streams plaintext directly from `age` to `pg_restore`,
performs exact raw table-level verification, records `raw_verified_at`, replays every deletion through the
restore-only RPC, verifies per-tombstone replay evidence, and only then records `ready_at`.
Production does not have the restore control schema, so the replay RPC fails closed there.
Verification evidence binds source/destination, manifest hash/reference, deletion artifact
hash/reference, raw verification, replay verification, and readiness. Never derive either expected
hash from the artifact being restored. Keep ingress disabled throughout. After isolated repository
readiness verification:

1. run the locked migration verifier without applying new production migrations;
2. test tenant A/B isolation, authenticated API health, billing reconciliation, durable-job
   lease recovery, and content-free audit append behavior;
3. compare source and restored manifests, evidence timestamps, and critical maximum IDs/counts;
4. record whether internal restoration completed within 1 hour and service restoration within
   4 hours; and
5. destroy the isolated copy under a separately approved staging cleanup after evidence capture.

## Redis loss and recovery

Redis Cloud must use paid multi-zone replication, TLS, and AOF every second. During Redis loss,
the API fails closed or degrades safely for coordination-dependent operations; it does not treat a
local cache or surviving Redis key as authoritative. Restore Postgres first, replace Redis through
an approved provider change, start it empty, and let bounded cache/rate/lease state repopulate.
Requeue durable work only from authoritative Postgres rows and reconcile before reopening writes.
Redis recovery-point claims must not be combined with the 15-minute critical-data RPO.

Review the current Redis Cloud high-availability and persistence behavior before every exercise:
<https://redis.io/docs/latest/operate/rc/databases/configuration/high-availability/> and
<https://redis.io/docs/latest/operate/rc/databases/configuration/data-persistence/>.

## Evidence and failure handling

Every quarterly exercise records owner role, incident/change ID, UTC start/end, chosen point,
observed RPO/RTO, hashes, table-verification evidence, smoke results, exceptions, and remediation
owners. Keep restore/tabletop evidence with security and administrative evidence for 400 days.
Alert on missing daily backup, failed encryption, missing manifest, hash mismatch, retention drift,
PITR disabled/window below 14 days, restore test overdue, or restoration over target. Alerts contain
only fixed categories, environment, timestamps, and opaque IDs.

If any integrity or table check fails, keep traffic closed, preserve artifacts, open a P0, and
escalate to the security and database roles. Never repair an evidence manifest to match an
unexpected restore.
