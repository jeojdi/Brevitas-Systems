# Operational readiness release gate

**INTERNAL CONTROL — EXTERNAL EXECUTION AND APPROVAL REQUIRED**

The operational readiness gate blocks staging and production releases unless current, content-free
evidence exists for disaster recovery, observability, paging, on-call ownership, and rollback. It
does not create a backup, configure PITR, provision monitoring, send an alert, perform a restore, or
rehearse a rollback. A passing repository test proves only that the validator fails and passes the
expected documents. It is not live operational proof.

The evidence contract is
[`operational-readiness-evidence.schema.json`](operational-readiness-evidence.schema.json). The
enforcing validator is `scripts/ci/operational-readiness.mjs`. Do not commit a populated evidence
document: provider references, receipts, approvals, and operational timing belong in the restricted
evidence system.

## What the gate recalculates and requires

The validator rejects unknown fields, placeholder/local/manual evidence, mutable references,
future timestamps, stale observations, environment mismatches, and build mismatches. It requires:

- a provider receipt showing PITR enabled with at least a 14-day window, observed within 24 hours;
- an encrypted, object-locked logical backup in an independent failure domain, no more than 26
  hours old, with at least 35 days' retention and distinct manifest/ciphertext digests;
- a restore into an isolated PostgreSQL 16 destination, raw table verification, deletion-artifact
  replay (including an explicit zero-tombstone replay), and all application verification results;
- recovery timestamps from which it calculates critical-data RPO (at most 15 minutes), internal
  database restoration (at most 60 minutes), and service RTO (at most 240 minutes); it does not
  accept operator-entered objective booleans or durations;
- a named external monitoring provider and telemetry backend, an observation no more than 15
  minutes old, at least two distinct API replica IDs, two worker replica IDs, and one compressor
  replica ID, plus imported overview and per-replica dashboard receipts;
- a test alert sent, delivered, and acknowledged through paging or incident management in the
  correct order, with an immutable provider receipt no more than 30 days old;
- a current on-call schedule, distinct primary/secondary/escalation owner roles, and provider
  evidence no more than 24 hours old;
- a successful staging rollback rehearsal for the exact candidate build SHA, a different rollback
  SHA, ordered deployment/rollback/verification timestamps, and provider evidence; and
- distinct operations-lead and security-lead approval identities and immutable approval receipts.

Restore evidence expires after 90 days. Rollback evidence expires after 30 days, although its exact
candidate-SHA binding normally makes it single-release evidence. The finalized envelope expires
after 60 minutes. Every nested timestamp must be at or before the envelope's `generated_at`.

The validator checks the evidence envelope and its bindings; it cannot independently query a
provider or establish that a provider receipt is genuine. Protected-environment access, immutable
provider records, independent SHA-256 capture, and two-person approval remain mandatory trust
controls. Never convert a screenshot, dry-run output, repository test, or self-authored assertion
into a provider receipt.

## Operator procedure

1. Deploy the candidate build to staging and record its exact lowercase 40-character Git SHA.
2. Execute every live action in the external-actions list below. Export only content-free receipts
   to the restricted evidence system and independently record each receipt digest.
3. Assemble a JSON envelope matching the schema in a restricted workspace outside this repository.
   Populate provider names, opaque `evidence:` references, SHA-256 digests, UTC timestamps, replica
   IDs, role names, and opaque approver IDs. Do not include URLs with query strings, credentials,
   contacts, customer data, prompts, responses, database rows, or raw telemetry.
4. Validate locally from the exact checkout:

   ```bash
   node scripts/ci/operational-readiness.mjs \
     --environment staging \
     --build-sha "$(git rev-parse HEAD)" \
     --evidence-file /restricted/evidence/staging-operational-readiness.json
   ```

   Use `--environment production` only for a production-bound envelope. A production envelope's
   restore source is production and its isolated destination is staging. A staging envelope's
   isolated destination is `test`.
5. Store the finalized content-free envelope as the protected GitHub environment variable
   `OPERATIONAL_READINESS_EVIDENCE_JSON` in the matching `staging` or `production` environment.
   Restrict variable updates and environment approval to the evidence-owner/approver roles. Do not
   put the document in a repository variable shared by both environments.
6. Run **Operational readiness evidence gate** from `main` for the target environment. The workflow
   supplies `github.sha`; an envelope for any other build fails. Preserve the workflow run ID and
   conclusion in the change record.
7. Make the reusable workflow job a required release/deployment check in the external deployment
   pipeline. The manual workflow alone does not block a deployment. Keep production environment
   reviewers enabled and require the separate infrastructure preflight and authenticated staging
   smoke gates as well.

The CLI may instead read a named environment variable with `--evidence-env NAME`. It accepts exactly
one evidence source, rejects symlinks, limits documents to 256 KiB, and never prints the document.

## External actions still required

Repository changes cannot complete or truthfully claim any of these actions:

- Enable and verify Supabase PITR and its oldest available recovery point for each environment.
- Schedule the encrypted logical backup, store ciphertext/manifest/evidence in independently
  administered immutable storage, enable object lock/versioning, and verify daily success and
  retention. `scripts/dr/backup-logical.sh` creates the content-free local artifact set but does not
  upload or make it immutable.
- Create the isolated restore database, run the approved restore, replay the independently protected
  deletion artifact, execute tenant/billing/job/audit/health checks, measure the timestamps, retain
  the immutable receipts, and destroy the copy under a separate approved cleanup.
- Contract and configure a monitoring provider, deploy the OpenTelemetry collector, import both
  `observability/grafana/enterprise-overview.json` and
  `observability/grafana/per-replica-health.json`, verify every expected live replica, and retain
  provider receipts.
- Configure alert rules and a real paging/incident route. Send a safe synthetic test alert, confirm
  provider delivery, have the assigned responder acknowledge it, and retain the delivery receipt.
- Populate the external on-call schedule and escalation policy with real people mapped to the
  required repository-safe roles; verify coverage and handoffs. Do not put contacts in evidence.
- Deploy the exact candidate to staging, roll back to a distinct known-good build, verify service,
  redeploy or close the rehearsal under the approved change, and retain platform receipts.
- Obtain distinct operations and security approvals after all control evidence is collected.
- Configure the workflow as an actual required deployment check. This repository cannot change
  GitHub environment protection, branch rules, Railway/Vercel deployment policy, or on-call policy.

If any evidence is unavailable, failed, stale, or unverifiable, the release remains blocked. An
exception document is not accepted by this validator; emergency change authority belongs in the
external incident/change process and must not be represented as a normal readiness pass.
