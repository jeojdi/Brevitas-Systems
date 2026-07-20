# Data access, export, and deletion runbook

**DRAFT — LEGAL/PRIVACY REVIEW REQUIRED — NOT PUBLISHED — NOT LEGAL ADVICE**

Target verified export or deletion completion within 30 days. Account data must leave primary
systems within 30 days and rotating backups within 35 days, except narrow active legal holds and
the minimized billing/invoice/tax records retained for 7 years subject to counsel. Identity and
authority are verified through the approved administrative API; repository scripts do not accept a
name, email, or customer-supplied tenant ID as authority.

## Intake and approval

1. Create an opaque `data_subject_requests` record through the authenticated administrative API.
   Record request type, authoritative tenant, UTC receipt/due times, verification state, approver
   role, and evidence ID. Do not put request content in general logs.
2. Verify requester authority and scope; resolve duplicate/conflicting requests; search for active
   or pending-create legal holds. Hold creation and release are separate two-person action requests;
   neither request endpoint directly mutates a hold.
3. Identify preservation exceptions. Billing ledger, invoice, and tax rows may be retained for the
   seven-year counsel-reviewed period; immutable security/admin audits remain for 400 days. Both are
   content-free, access restricted, and unavailable for ordinary product use after deletion.
4. A second, distinct verified compliance administrator approves the request only after scope and
   exceptions are recorded. The submitter cannot approve their own request; both roles and opaque
   actor IDs are immutably audited.

The workflow has three distinct scopes:

- **Tenant offboarding (`tenant`)** removes tenant configuration, customers, credentials, and
  memberships. A member whose last organization is the offboarded tenant is converted to a unique,
  banned, non-login `auth.users` placeholder and loses profile/session/identity PII. A member who
  still belongs to another organization keeps their auth identity and profile unchanged.
- **Member data-subject request (`member` plus subject UUID)** exports or deletes only that member's
  relationship and scoped metadata in the named tenant. Other memberships remain usable; only an
  otherwise unshared identity is anonymized.
- **End-customer data-subject request (`customer` plus subject UUID)** exports or deletes only the
  authoritative customer row and customer-linked metadata/cache namespace in the named tenant. It
  does not offboard the tenant or its members.

All subject UUIDs are checked against the authoritative tenant at intake and execution. A
cross-tenant member/customer request fails rather than widening scope.

## Implemented database contract and remaining API gate

Ordered migration `202607170007_compliance_workflows.sql`—not company-administration migration
005—adds:

- `public.data_subject_requests`: opaque request UUID, tenant UUID, `export|delete`,
  `tenant|member|customer` scope with a required member/customer subject UUID, fixed status,
  received/requested/due/completed timestamps, approver/evidence opaque IDs, and a constraint that
  `due_at <= requested_at + interval '30 days'`;
- `public.legal_holds`: tenant/scope, active state, fixed reason code, approval/release timestamps,
  nullable expiry, and compliance-only privileges. Ordinary tenant administrators cannot read,
  create, update, or release holds;
- `public.legal_hold_actions`: immutable action UUID, authoritative tenant, `create|release`, target
  hold, scope/reason/expiry snapshot, and requester, with one permitted `pending` → `approved`
  transition recording the distinct approver. A database trigger rejects request-field mutation,
  deletion, and truncation. Service role can read tenant-scoped status but cannot directly mutate;
- `public.compliance_request_legal_hold_action(...)`: accepts only a verified
  `brevitas_admin` actor. Create requests validate and record proposed fields; release requests
  derive their immutable fields from the locked active target. Exact same-requester replay returns
  the existing row, while cross-tenant or mismatched replay fails;
- `public.compliance_approve_legal_hold_action(uuid,uuid,text,text)`: transactionally locks the
  action and target, denies the requester and competing approvers, commits the create/release only
  for a second distinct verified `brevitas_admin`, and makes same-approver replay idempotent. Both
  request and approval append content-free immutable audit events;
- `public.backup_deletion_tombstones`: immutable request/tenant IDs and creation/expiry timestamps
  with expiry exactly 35 days after request receipt (including a past deadline for overdue work);
- `public.compliance_export_tenant(uuid,uuid,text)`: transactionally locks and rechecks the approved
  request/tenant/hold state, marks it processing, returns scoped portable JSON records, excludes
  security-authenticator material and general telemetry, and appends an immutable migration-005
  start event. Authorized provider credentials are a different, sensitive category: they are
  application/KMS-decrypted and included only in the age-encrypted export;
- `public.compliance_export_subject(uuid,uuid,text)`: applies the same controls to only the approved
  member or end-customer subject within the authoritative tenant;
- `public.compliance_complete_export(uuid,uuid,text,text,text,integer,text)`: locks the same request
  and accepts only the age artifact digest, signed-attestation digest, portable record count, and
  plaintext-record digest after successful streaming verification. Missing/undecryptable content or
  encryption failure leaves a recoverable non-completed request, never a ciphertext-only or false
  completion;
- `public.compliance_delete_tenant(uuid,uuid,text)`: transactionally locks and rechecks the same
  state, revokes access first, deletes/anonymizes tenant-scoped primary data in dependency order,
  preserves only approved billing/tax and immutable content-free audits, creates a backup tombstone,
  marks completion, and appends the immutable audit event; and
- `public.compliance_delete_subject(uuid,uuid,text)`: deletes only an approved member/customer
  subject, enforces tenant membership, holds, billing-ledger preservation, cache cleanup, immutable
  audit, and the same backup-tombstone deadline; and
- optional `support_records` deletion uses only the exact transactional adapters
  `compliance_delete_support_records(uuid)` and
  `compliance_delete_support_subject(uuid,text,uuid)`. Each returns the exact scoped
  `brevitas.support-erasure.v1` proof with zero remaining rows. A missing/failing/malformed adapter
  rolls back all changes and the request is never marked completed; and
- `public.compliance_replay_deletion_tombstone(...)`: works only in an explicitly bootstrapped
  isolated restore database after raw verification and refuses production/ordinary databases that
  lack the exact source/hash/reference control record; and
- service-role execution only, fixed search paths, least privileges, tenant UUID checks, idempotent
  retries, and database assertions for cross-tenant denial, holds, timing, billing/audit preservation,
  and rollback refusal once evidence exists.

`api/compliance_admin.py` implements data-request submit/status/approve plus the frozen hold-action
surface `POST /v1/admin/compliance/hold-actions`,
`GET /v1/admin/compliance/hold-actions/{action_id}`, and
`POST /v1/admin/compliance/hold-actions/{action_id}/approve`. There is no direct hold-create or
hold-release route. The dependency-injected principal must be a verified `brevitas_admin`; tenant
and actor authority exist only on that principal and are absent from every body. The Supabase
adapter scopes both status resources by derived tenant plus request/action UUID and calls only the
transactional request/approval RPCs. W1 must mount and configure this fail-closed router from its
verified Supabase application-metadata authority; until then the mount remains a launch gate.

### Pending hold safety policy

A pending `create` is preservation intent. Before the second approval, it immediately blocks a
matching `export` or `delete` workflow (`all` matches both) and conservatively excludes every row in
that tenant from retention. This closes the request-to-approval deletion gap. If the proposed hold
has an expiry, pending protection ends at that timestamp and approval then refuses the expired
request. A pending `release` never weakens or expires the active hold; only its distinct-admin
approval changes `legal_holds.active`. Approved creates are enforced by the active hold, and an
approved release is the only administrative path that resumes deletion/retention. The released
hold and both approved action records remain immutable evidence for 400 days after release; the
bounded retention authority then removes the action records and hold in one transaction. Pending
actions and active holds are never retention candidates. Restore-only tombstone replay remains
separately source/hash-bound to an isolated verified restore database; it is not exposed by the
administrative router.

Audit actors are opaque `system` or `brevitas_admin` IDs accepted by migration 005; audit details
remain `{}` and immutable. Deletion must fail as one transaction if any table fails; export uses the
explicit processing/finalize boundary because artifact encryption is outside Postgres. The
executable workflow fails closed if any table or RPC signature is absent.
The matching `scripts/dr/202607170007_compliance_workflows.rollback.sql` refuses rollback once any
request, hold action, hold, or tombstone exists; compliance evidence must never be dropped to force
a rollback.

Optional legacy/platform tables are handled deliberately during identity cleanup. `profiles` is
deleted for an unshared user. `billing_events` keeps the financial row but clears its session ID;
`billing_accounts` keeps required Stripe/invoice evidence but clears ephemeral checkout state; and
`legal_acceptances` remains minimized legal evidence linked only to the non-login UUID shell. Each
optional path is catalog-checked. Tenant cleanup removes both the exact runtime namespace
`sha256("<organization_uuid>:unattributed")` and every customer namespace.

Tenant exports include organization/member/customer records, full portable usage and billing
fields, service accounts, installations/devices, invitation and administrative-audit relationships,
API key and device-delivery metadata, repository relationships, provider configuration, durable job
payload/results and lifecycle metadata, and enabled semantic-cache content/derived metadata. For
identities that tenant deletion may anonymize, the export also includes the user/application
profile, authentication identity, non-secret session/MFA/one-time-token lifecycle metadata, legacy
billing events, legal acceptance, and billing-owner relationship. Member/customer exports select
the matching relationships and encrypted content without widening tenant scope. If the optional
`support_records` store exists, exports fail closed unless
`compliance_export_support_records(uuid)` and
`compliance_export_support_subject(uuid,text,uuid)` provide its explicitly scoped portable rows.
SQL emits encrypted values only as transient `encrypted_content` envelopes with the exact
application context:

Portable usage records enumerate every persisted receipt/accounting field, including tiered cache
write tokens and `cache_attributable`. They exclude `key_hash` as a security authenticator and
`usage_raw` because that field is not a portable receipt and may contain unclassified raw input.

- durable jobs: `purpose`, job UUID, organization UUID, and `payload|result` field;
- semantic cache: `purpose`, tenant namespace, exact hash, and model identity; and
- provider credentials: `purpose=provider_credential` and key hash.

`scripts/dr/portable-export.py` passes each envelope over stdin to the absolute, non-symlink
executable named by `BREVITAS_COMPLIANCE_DECRYPT_COMMAND`. The deployment implementation uses the
same application/managed-KMS adapters and must echo the context and ciphertext SHA-256 bindings.
Any unavailable KMS, wrong context, malformed plaintext, timeout, or undecryptable row aborts the
pipeline. The portable stream ends with a record-count/plaintext-digest proof and contains no
ciphertext-only record. `verify-portable-export.py` validates that proof after age decryption.

Security-authenticator exclusions are per-field, not blanket row exclusions: raw API keys, API-key
hashes, password hashes, confirmation/recovery/one-time-token hashes, MFA secrets/WebAuthn
credentials, invitation token hashes/email lookup hashes, actor-key hashes, and encrypted
device-delivery keys are omitted; their non-secret lifecycle/relationship metadata is included.
Device authorization/receipt identifiers are included as metadata. This classification is a
**launch-blocking counsel review item** and must be recorded as counsel-approved before relying on
the exclusion for a production response. Authorized provider credentials are sensitive customer
configuration, not excluded security authenticators; they, job content, and cache content must
decrypt successfully and enter only the age-encrypted portable export.

## Guarded execution

Start with an offline dry-run; it reads no environment credential and makes no connection:

```bash
scripts/dr/tenant-data.sh --action delete \
  --scope tenant \
  --request-id 00000000-0000-4000-8000-000000000001 \
  --tenant-id 00000000-0000-4000-8000-000000000002 \
  --environment staging --target-id staging-us \
  --evidence-dir /restricted/dsr-evidence --dry-run
```

Approved apply mode requires a managed `COMPLIANCE_DATABASE_URL`, an opaque actor ID, `--apply`,
and exact `--confirm DELETE:<target>:<request-uuid>`. Production additionally requires
`--allow-production`. The script preflights all schema/RPC capabilities and rejects an absent,
tenant-mismatched, unapproved, or held request. An approved request remains processable after its
30-day deadline: the database appends immutable `compliance.deadline_breached` audit evidence, the
script emits a content-free deadline-breach alert/evidence field, and processing continues urgently.
After deletion it requires completed status and a tombstone expiring no later than 35 days after
request receipt, even when that backup deadline has already passed.

Export uses the same controls with `--action export` and exact `EXPORT:` confirmation. It also
requires the public `BREVITAS_EXPORT_AGE_RECIPIENT`, explicit verification identity
`BREVITAS_EXPORT_AGE_IDENTITY`, independent `BREVITAS_EXPORT_EVIDENCE_HMAC_KEY`, and the managed
decryption command. It streams portable JSONL directly into an encrypted artifact. No plaintext
file is created. The script immediately decrypts the artifact into a streaming verifier; a wrong
recipient/identity or corrupt age file fails before database finalization. Store exports in a
restricted delivery system. The script compares the encrypted artifact digest before and after age
verification and again against the exclusively created signed sidecar before finalization, so a
changed artifact fails closed. Communicate
the decryption secret through a separate verified channel, expire the artifact promptly, and record
delivery/expiry evidence. Never export content from logs, traces, metrics, alerts, or dashboards.

Member and end-customer execution uses the same command with `--scope member|customer` and a
required authoritative `--subject-id` UUID. The script dispatches to the subject RPCs and verifies
the stored request scope/subject before work. These are executable subject-rights paths; they are
not aliases for a tenant-wide operation.

Before database finalization, the same run creates a 24-hour
`brevitas.export-attestation.v1` sidecar signed with HMAC-SHA256. It binds exact request/tenant/scope/
subject/actor/target/environment, received/due/deadline status, artifact name/digest/expiry,
portable record count/plaintext digest, zero ciphertext-only records, and the telemetry prohibition.
The HMAC key is independent of the artifact directory. Finalization stores the artifact digest,
attestation digest, record count, and plaintext digest in the request row.

Export crash recovery is idempotent only with that proof. If encryption and attestation committed
locally but database finalization did not, rerun the identical request/actor: the script verifies all
sidecar fields/signature, rejects substitution or an expired 24-hour artifact, decrypts and verifies
age again, then resumes finalization. If database finalization committed but local completion
evidence creation failed, the same checks recreate a deterministic evidence document. Existing
completion evidence must match **every** field exactly; extra, missing, or changed fields fail.
A missing sidecar/finalized artifact, bad signature, digest/context/count mismatch, stale artifact,
or actor/deadline substitution fails closed and requires a new approved request.
The evidence directory must resolve without symlinks, be owned by the current UID, and grant no
group/other permissions. New artifacts and evidence use atomic no-replace publication. Age
verification and HMAC create/verify operate on one `O_NOFOLLOW` file descriptor and compare its
device/inode/size/time/digest plus the final path, so concurrent path substitution fails closed.

## Backup deletion and restored copies

Logical and PITR backups are immutable during their short rotation; individual rows are not edited
in place. After each logical backup, `export-deletion-artifact.sh` creates a source/manifest-bound
artifact of completed tombstones with an issuance time strictly newer than the backup. Store it in
an independently protected failure domain and obtain its SHA-256/reference through an independent
evidence channel. Before any restored copy is ready, verify raw table counts, replay every artifact
tombstone transactionally—including overdue entries—verify per-request replay evidence, and then
set readiness, including zero tombstones. An empty artifact is still replay-verified. The isolated
target is PostgreSQL 16 in
`ephemeral-postgres` mode, not a fresh Supabase project. Its readiness is for isolated verification,
not public traffic; see the DR runbook.

Natural rotation removes deleted data no later than 35 days after request receipt. A live legal
hold blocks primary deletion and therefore prevents creation of a completed tombstone. During
restore, an already completed authoritative tombstone is newer proof than any stale hold state in
the older backup and must be replayed. The restore-only RPC is source/evidence bound and unavailable
in production.

## Evidence and exceptions

Restricted evidence includes request/tenant/actor opaque IDs, authority/approval result, fixed hold
and exception codes, start/end timestamps, encrypted export hash, primary completion, tombstone
expiry, deadline-breached status, audit event ID, and verifier. It excludes names, emails, prompts/responses,
credentials, raw rows, and export content. Failed or overdue requests alert privacy/security with
fixed categories and are escalated; they are never silently marked complete.
