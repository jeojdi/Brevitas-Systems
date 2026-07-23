# Release security gates

Repository readiness is enforced by six GitHub Actions workflows. All third-party actions
are pinned to full commit SHAs, checkout credentials are not persisted, and workflow-wide
permissions are read-only. Branch protection must require the workflows before merge; repository
files cannot configure that GitHub setting themselves.

## Atomic key boundary

Organization API-key creation/revocation and its immutable audit append commit in one PostgreSQL
RPC transaction. Migration 008 installs atomic dashboard-session creation and replacement;
migration 009 adds bounded tenant-authorized listing, installs a dashboard-session-only revoke RPC,
and removes the older generic revoke surface. Listing returns metadata only: no key digest,
fingerprint, or raw credential can leave `api_keys`. `SupabaseUsageStore` calls only these RPCs for
key mutation, and `tests/release_security.test.mjs` rejects a return to separate `api_keys` and
`audit_events` requests. The migration integration test also forces audit insertion failures and
proves the associated key mutation rolls back.

## Blocking security workflow

`.github/workflows/security.yml` runs locked builds/tests plus the following independent gates:

- `npm audit --audit-level=high` for both lockfiles and `pip-audit --strict` against the hashed
  Python runtime lock. High and critical dependency findings fail the workflow.
- Repository-owned Semgrep rules, all severity `ERROR`, run by a hash-locked scanner. Any match
  fails the workflow.
- TruffleHog over the relevant commit history. Its action, scanner version and scanner image digest
  are pinned; any verified credential fails the workflow.
- Trivy scans of the built API and compressor images. Fixable high or critical OS/library
  findings fail; unfixed upstream findings are reported but do not block.

Root and dashboard packages use `npm ci --ignore-scripts`. Python tests install
`scripts/ci/python-test.lock` with `--require-hashes`; production dependency audits use separate
API and compressor runtime locks. The `pip-audit` scanner and its dependencies have their own
hashed tooling lock, so the audit implementation cannot float between runs. Regenerate all locks
intentionally with `uv==0.8.4` and the Linux/Python 3.11 resolver command recorded in each lock
header, review the version diff, reinstall with `--require-hashes`, and audit it before merge. The
five source inputs are `scripts/ci/python-{runtime,test,compressor,audit,sast}.in`; do not hand-edit
resolved requirements or hashes. When the resolver itself is upgraded, review and update its
version here in the same change before regenerating the locks.

The ordinary API lock uses the PyPI advisory service. The compressor audit uses OSV because its
CPU-only Torch wheel has a PEP 440 local version (`+cpu`) that PyPI's audit endpoint does not
represent. Installation still enforces every distribution hash before either image is built.

The SAST policy has two narrow reviewed exceptions: the RLM engine's restricted-namespace REPL
implementation and the root layout's static `jsonLd` serialization. The exclusions are rule/path
specific; they do not suppress other rules or findings in those files.

Container base image `FROM` lines must include immutable `@sha256:` digests. Updating a base
requires reviewing the upstream image provenance and the Trivy result in the same change.
Release Dockerfiles install only hash-locked Python artifacts and do not resolve mutable operating
system packages during the build.

## Migration gate

`scripts/ci/migration-fresh-manifest.txt` is the complete deployable Supabase chain;
`scripts/ci/migration-upgrade-manifest.txt` is its exact enterprise suffix for the known production
baseline. The verifier inventories every SQL file under `supabase/migrations/` and rejects an
unlisted, duplicated, reordered, rollback, or `api/migrations/` entry. Same-date `20260716` files
therefore have deterministic explicit order. CI starts an ephemeral digest-pinned PostgreSQL 16 +
pgvector service, parses the database URI, opens its first session read-only, and refuses DDL unless
`inet_server_addr()` proves that the server actually reached is IPv4 or IPv6 loopback. It then:

1. builds the documented 12-file production baseline through
   `20260716_stripe_billing_rate_25pct.sql`, inserts representative key/usage/billing state, and
   proves database scaling fails before its tenancy prerequisite;
2. applies the exact 30-file upgrade suffix `202607170001` through `202607200017`, including a
   legacy plaintext-cache fixture before canonical cache migration 002;
3. pre-stages production-upgrade indexes outside a transaction before migration 006;
4. verifies legacy keys and usage gain tenant identity, the raw browser-key table and plaintext
   cache are removed, and historical billing evidence is unchanged;
5. verifies pgvector/RLS/grants, fixed search
   paths, DB-clock TTL normalization, plaintext rejection, tenant/model lookup isolation, and the
   shared cap under concurrent PostgreSQL sessions;
6. verifies service-role-only RPC permissions, tenant isolation, equal-sort cursor stability,
   valid/ready indexes, billing lease recovery/month boundaries/ledger immutability, and immutable
   administration audit events, compliance RPC isolation, legal holds, tenant-scoped export,
   erasure idempotence, immutable backup tombstones, financial/audit preservation, atomic
   dashboard-key replacement/revocation with opaque audit evidence, metadata-only paginated key
   listing, removal of the generic migration-008 revoke RPC, replay-safe device delivery with
   approver/key drift quarantine, actor-bound active-company membership choices capped at 100,
   server-owned active-company persistence/fallback, the receipt-accounting
   fields/permissions/billing-trigger contract, receipt-bound durable onboarding evidence, and
   transactionally current billing-owner attribution for Stripe customer persistence;
7. applies migrations 010–013 twice, including a simulated quarantined pre-constraint receipt
   whose ciphertext must be erased before the named constraint validates; applies the `20260720`
   suffix with the guarded billing-identity maintenance procedure and failure-injected rollback
   checks, including Checkout generation reservations followed by durable provider-outbound
   ambiguity fencing, durable onboarding, and billing-owner/customer persistence fencing; then
   rolls back/reapplies the cache RPC/constraint layer, database-scaling read path, and only the
   receipt-accounting validation layer while
   proving encrypted cache and authoritative usage/billing/audit row counts are unchanged; and
8. resets only the verified loopback database, applies all 42 forward Supabase files as an
   isolated fresh install, reapplies migrations 010–013 and `202607200001`–`202607200017`, and reruns the forward-contract
   assertions.

The production billing-identity maintenance command additionally checks the deployed Vercel
dashboard version and the private authoritative worker version before its first PostgreSQL
connection. Operators reach the private worker only through an authenticated, loopback-bound
Railway tunnel; the worker is not made public. Both endpoints must self-report the exact maintenance
SHA and expected service identity through bounded, non-redirecting read-only requests. This is a
self-reported version agreement control, not cryptographic artifact provenance.

The deployable chain never substitutes `api/migrations/001_persistent_stores.sql`,
`002_semantic_cache.sql`, or `003_receipt_accounting.sql`. The database-scaling concurrent-index
companion and explicit rollback files are operator/test procedures outside both forward manifests.
CI additionally applies retired API migration 002 to a disposable legacy-cache fixture, asserts it
only fails closed and removes plaintext, and then applies canonical Supabase migration 002. This is
a compatibility safety test, not a production migration instruction or manifest entry.

Migration 012 is the deployable, upgrade-safe alignment for canonical API migration 003. It adds
the 5-minute and 1-hour cache-write token partitions plus the `cache_attributable` flag without
running `api/migrations/003_receipt_accounting.sql` as a second production migration. It verifies
the complete persisted receipt schema and existing fee trigger, validates detailed cache tiers,
and limits `service_role` to receipt read/append while retaining RLS. Its tested rollback removes
only the new validation constraint: populated receipt columns, usage rows, billing evidence,
trigger, and hardened permissions remain intact.
Migration 013 adds the server-owned active-company preference. Its service-role-only selection RPC
locks and validates the verified actor's live membership before changing the preference; its
resolver repairs stale choices to another deterministic active membership. Browser roles have no
direct table or RPC access, and successful explicit switches append content-free audit evidence.
The migration-007 export functions reference the later fields through catalog-safe JSON access,
assert their final types at execution time, include them in portable usage records, and continue to
exclude the legacy `usage_raw` content field.

CI then creates a custom-format dump of the migrated source and restores it transactionally into a
second digest-pinned PostgreSQL 16 + pgvector server on loopback port 5433. The disposable CI dump
is intentionally raw and never leaves the job; production logical-backup procedures still require
encryption. Before restore, CI builds a source table-count manifest and an independently hashed,
source-bound zero-tombstone deletion artifact. Negative controls prove replay refuses a missing or
mismatched restore control, an incorrect artifact hash, and an incorrect evidence reference. The
target is bootstrapped with the required roles, extensions, and immutable source/evidence binding;
after restore, the DR verifier checks the dump hash and exact raw counts, persists raw verification,
replays even the empty deletion artifact, and only then marks the isolated target ready. The final
assertion requires `raw_verified_at < replay_verified_at <= ready_at` and zero replay-evidence rows
for zero tombstones. This workflow never connects to staging or production.

`202607170006_database_scaling.sql` is generated byte-for-byte from the canonical
`api/migrations/004_database_scaling.sql` body with a source SHA-256 header. After changing the
canonical file, run `npm run release:migrations:sync` and then
`npm run release:migrations:check`. CI rejects drift, missing/duplicate timestamps, incomplete
rollback coverage, or destructive rollback instructions. No migration is applied to staging or
production by these workflows.

`scripts/ci/migration-frozen-checksums.txt` pins the final bodies of migrations 007 and 009–017,
plus migration 007's PostgreSQL contract assertions. The verifier rejects any body drift, missing
pin, path substitution, or reordered checksum entry; an owning worker must explicitly refreeze a
changed artifact and its checksum together.

## Approved staging smoke

Run the credential-free infrastructure preflight first. `.github/workflows/release-preflight.yml`
accepts only the fixed `staging` or `production` profiles and validates public DNS, verified HTTPS,
Vercel plus Cloud Run/Railway routing, and the exact liveness/readiness contract using five
read-only GETs. It checks each application's self-reported full SHA against the workflow SHA. It
rejects redirects, missing or
mismatched full SHAs, legacy health, and degraded readiness. This detects identity disagreement; it
does not cryptographically bind served bytes or images to that SHA. See `docs/RELEASE_PREFLIGHT.md`.

`.github/workflows/staging-smoke.yml` has no automatic trigger. It runs only by
`workflow_dispatch` from `main`, in the non-fork canonical repository, under the GitHub `staging`
environment. Configure that environment with required reviewers and these secrets:

- `STAGING_TENANT_A_API_KEY`, `STAGING_TENANT_B_API_KEY`
- `STAGING_TENANT_A_JOB_ID`, `STAGING_TENANT_B_JOB_ID` (seeded non-sensitive fixtures)
- `STAGING_TENANT_A_CUSTOMER_ID`, `STAGING_TENANT_B_CUSTOMER_ID` (fixture routing IDs)
- `STAGING_BILLING_USER_TOKEN`, `STAGING_BILLING_RECOVERY_SECRET`

The script accepts only repository-allowlisted HTTPS staging origins. It refuses localhost, IP
literals, ports, paths, credentials, production names and arbitrary domains before placing a
secret in a request. It checks liveness, missing-auth rejection, symmetric tenant job isolation,
dependency readiness (including authoritative Postgres, coordination Redis and the private
compressor, plus fresh active KMS evidence through overall API readiness), billing authentication,
and the manual-recovery two-factor/parser path. The recovery
smoke proves that the recovery secret alone and the user bearer alone are both rejected; only the
user bearer plus the separate `X-Billing-Recovery-Secret` header reaches body validation. Tenant A
and B must use distinct keys, job IDs and customer IDs; the smoke tests both correct tuples plus
every crossed A/B key-customer combination against both jobs, requiring 403/404 responses that do
not echo either tenant fixture. The recovery body is deliberately invalid `{}` and must return
`400`, so the smoke cannot mutate a ledger. Add a new
staging hostname to the code allowlist only after DNS and environment ownership review.

Never run this smoke locally or against production. Provisioning, live migrations, load tests,
credential rotation and production rollout remain separately approved operational steps.

After this read-only fixture smoke passes, run the separately protected mutating journey canary.
It exercises pre-provisioned authentication, workspace selection, dashboard and service-key
lifecycle, one bounded real BYOK stream, usage/job idempotency, worker completion, Stripe test
Checkout, signed webhook replay, and the two-factor recovery boundary. Its workflow has fixed
hosts and no URL input. Setup, hard bounds, cleanup behavior, optional seeded-ledger recovery, and
the evidence that remains operator-owned are documented in `docs/STAGING_CANARY.md`. It does not
exercise public signup/email delivery, a browser, a released CLI, worker death/reclaim, matched
subscription or invoice mutation, or billing end to end.
