# Credential security and managed-KMS operations

## Security boundary

Provider credentials and durable-job keys are encrypted with `brevitas.security.EnvelopeCipher`.
Each write generates a random 256-bit AES-GCM data-encryption key (DEK). A deployment-supplied
managed-KMS adapter wraps that DEK; only wrapped key material is stored. The envelope is
`bvt-envelope:v1:<base64url-json>` and authenticates all of this metadata:

- schema and envelope version;
- managed-KMS provider, key ID, immutable key version, and wrapping algorithm;
- data algorithm (`AES-256-GCM`); and
- a SHA-256 digest of the caller-supplied encryption context.

The encryption context must bind the ciphertext to its purpose and authoritative record, for
example `{"purpose": "provider-credential", "organization_id": <opaque UUID>}`. It is supplied
again on decrypt but is not stored in the envelope. Copying a credential to another tenant or
changing any authenticated metadata makes decryption fail.

`ExternalManagedKMS` is an adapter boundary, not a KMS implementation. Deployment composition
must connect it to a real cloud/HSM-managed SDK using workload identity. SDK exceptions are
collapsed to content-free errors. Do not export a KMS key, put a master key in an environment
variable, or describe `LocalTestKMS` as managed. `LocalTestKMS` is an explicit development/test
facility and refuses Railway/production environments.

Production startup fails closed when a managed adapter, provider, key ID, or version is missing.
The API must never read or create `api/.secret_key`. Undecryptable values must never be treated as
plaintext. KMS downtime fails writes and uncached decrypts closed; callers should return a generic
dependency-unavailable response and emit only a fixed error type/metric.

API and worker readiness also require fresh evidence from an active KMS round trip whenever KMS is
configured (and KMS is mandatory in production). The probe generates a random 256-bit DEK, wraps
and immediately unwraps it under the configured immutable key version with the fixed
`brevitas.kms-readiness.v1` operational context, compares the result, and wipes mutable plaintext
buffers.
It never selects customer data, persists its wrapped key, populates the DEK cache, or returns KMS
metadata/errors in the health payload. Successful evidence is cached briefly and single-flight;
errors, an exceeded deadline, invalid bounds, or evidence older than the maximum age fail readiness
closed. `/v1/health/live` and the worker `/live` endpoint remain process-only.
Python cannot forcibly stop a provider SDK call already blocked in native/network code. The monitor
therefore permits only one dedicated in-flight probe and keeps readiness closed after its wait
deadline; the deployment adapter must also configure the SDK's own connect/read/operation timeout.

## Configuration contract

These are server-only managed secrets or non-secret settings. None may use a `NEXT_PUBLIC_` or
`VITE_` prefix.

| Variable | Production contract |
| --- | --- |
| `BREVITAS_KMS_REQUIRED` | `true`; production enforces this even if omitted |
| `BREVITAS_KMS_PROVIDER` | Adapter identifier such as the selected managed-KMS vendor |
| `BREVITAS_KMS_KEY_ID` | Stable alias/resource ID for the credential KEK |
| `BREVITAS_KMS_KEY_VERSION` | Immutable active version; aliases such as `latest`, `current`, `active`, `default`, or `alias/...` are rejected |
| `BREVITAS_KMS_ALGORITHM` | Provider wrapping algorithm recorded in envelopes |
| `BREVITAS_DATA_KEY_CACHE_TTL_SECONDS` | `1..900`; default `300` |
| `BREVITAS_DATA_KEY_CACHE_MAX_ENTRIES` | `1..1024`; default `256` |
| `BREVITAS_KMS_READINESS_TIMEOUT_SECONDS` | Active probe deadline, `0.05..10`; default `1` |
| `BREVITAS_KMS_READINESS_MAX_AGE_SECONDS` | Maximum age of successful probe evidence, `1..300`; default `30` |
| `BREVITAS_LOCAL_KMS_KEY` | Base64 256-bit key, development/test only; forbidden in production |

The unwrapped-DEK cache is per process, TTL bounded, LRU size bounded, lock protected, and wiped on
expiry/eviction/clear on a best-effort basis. It is an availability optimization, never an
authoritative key store. Clear it during graceful shutdown and after a key is disabled. A cached
DEK may allow a previously accessed record to decrypt only until its short TTL expires during a
KMS outage.

The production adapter is `brevitas.security.google_cloud_kms.GoogleCloudKMS`, using the pinned
Google Cloud KMS SDK and Application Default Credentials. It targets the configured immutable key
version for encryption, uses the caller context as Cloud KMS additional authenticated data, checks
CRC32C integrity in both directions, disables SDK retries, and enforces a bounded operation
timeout. Prefer Workload Identity Federation and grant only key-scoped encrypt/decrypt access.
Never log adapter request or response objects.

Provider, key ID, version, and algorithm metadata are ASCII/length bounded before use. The
adapter-returned key ID and immutable version must exactly match the requested values. When
`BREVITAS_KMS_ALGORITHM` names a specific algorithm rather than `provider-default`, the adapter's
returned algorithm must also match exactly or the write fails before ciphertext is produced.

## Legacy migration and rotation

`LegacyFernetDecryptor` is decrypt-only and must receive old keys explicitly from managed secret
injection. Plaintext fallback is forbidden. `decrypt_with_metadata()` marks legacy or non-active
key versions as `needs_rotation`; `rotate_envelopes()` supports bounded dry-runs and application
through a caller-supplied persistence callback.

Use this controlled workflow in staging before production:

1. Inventory ciphertext formats and counts without selecting or logging plaintext. Quarantine
   plaintext/unknown records for individual customer remediation; do not auto-accept them.
2. Grant the service decrypt permission to the old key version and encrypt/decrypt permission to
   the new version. Keep both versions enabled during the migration window.
3. Configure the new immutable `BREVITAS_KMS_KEY_VERSION` and explicitly inject old Fernet keys
   only if a Fernet migration is required. Run a bounded dry-run and reconcile inspected,
   current, migration, and failure counts.
4. Re-encrypt in small batches. The database callback must use a transaction and optimistic
   compare-and-swap on the original ciphertext so concurrent credential changes cannot be lost.
   Store only the replacement envelope; never write plaintext to a temporary table or file.
5. Verify every active record reports the new key version. Exercise provider authentication in
   staging, review content-free KMS error metrics, and test rollback by keeping the old KMS
   version decrypt-only.
6. Clear process caches, remove the legacy decryptor, deploy, and verify no legacy decrypt metric
   appears for a full rollback window. Only then schedule disabling the old version under
   two-person approval. Destruction follows the retention policy and must not be immediate.

The callable rotation helper uses batch position rather than database/customer IDs in its error
surface. It catches only expected envelope/KMS failures and returns counts. Production orchestration
must checkpoint progress, cap rate, stop on an elevated failure rate, and record an immutable admin
audit event containing only key IDs/versions, counts, actor, approval, and timestamps.

Emergency rotation follows the same workflow but first revokes affected provider credentials,
opens an incident, and blocks new use. Do not perform a real rotation from a developer workstation
or this repository run.

## Retired and exposed key material

`api/.secret_key` and `api/brevitas.db` were committed at `ea9c6dd` and are still reachable from
`main` (blobs `ae18fc3b` and `dc674de5`), so every clone and fork holds them permanently. The file
held a symmetric Fernet data-encryption key that an earlier `api/server.py` used to wrap
`provider_config.provider_api_key`, and the committed SQLite copy contains one ciphertext that the
committed key opens. `.gitignore` was tightened afterwards, which stops future commits but does not
purge history.

Assessed exposure, so a reviewer who finds the blobs has an answer:

- The key was tracked for about two hours on 2026-06-11 (`ea9c6dd` 16:03 UTC to `da590ce` 17:59
  UTC). `api/` first became Railway-deployable at `400062b` 18:15 UTC and moved to the Dockerfile at
  `cc3e979`; neither tree contains `api/.secret_key`. No deployment ever ran with the file present,
  so no customer credential was encrypted under it.
- The disclosed material is development data: the single decryptable ciphertext is the literal test
  value `sk-ant-test-...`, the other provider rows are `ollama` with an empty key column, and the
  ten `api_keys` rows are SHA-256 hashes named `smoke-test`, `default`, and `test`.

Required response, none of which this repository can perform:

1. Treat the committed key as burned. Never set it as `BREVITAS_LOCAL_KMS_KEY`, never inject it
   through `legacy_keys`, and never restore `api/.secret_key` reading. Current code already reads no
   secret file.
2. Run the read-only inventory before concluding no re-encryption is needed: count
   `provider_config.provider_api_key`, `ai_jobs.payload` and `ai_jobs.result` values whose value does
   not start with `bvt-envelope:v1:`. The timeline above predicts zero. Only if that count is
   non-zero does the "Legacy migration and rotation" workflow apply, and in that case also force a
   customer-side rotation of every affected provider key.
3. History rewrite (`git filter-repo --invert-paths` plus force-push) is a diligence decision, not
   containment: it invalidates every clone and open pull request, and it does not remove the blobs
   from GitHub's fork network without a GitHub Support garbage-collection request. Record the
   decision either way.
4. `LegacyFernetDecryptor` and `credential_cipher_from_environment(legacy_keys=...)` stay. They are
   the documented migration bridge for a future rotation and are unrelated to this key; removing
   them would delete a capability without reducing any exposure.

The secret scanners cannot catch this class. `.github/workflows/security.yml` runs TruffleHog with
`--only-verified`, and a raw base64 symmetric key or a SQLite blob is unverifiable by construction,
so filename/entropy rules are the only gate that would have fired. `tests/secret_material_hygiene.test.mjs`
enforces the repository-side half.

## Logging and telemetry

`brevitas.security.redact()` recursively handles mappings, headers, lists/tuples, URLs, bytes, and
exception messages/attributes/causes. It removes authentication headers, cookies, common provider
tokens, JWTs, URL userinfo, fragments, and non-allowlisted query values. Both recursion depth and
collection size are bounded. Supplying `safe_fields` switches mappings to allowlist mode, but a
secret-named field is still redacted even if allowlisted.

Mapping keys and URL query names are untrusted too. Keys containing bearer credentials, provider
tokens, JWTs, credential assignments, control characters, or credential-bearing URLs collapse to
`[REDACTED_KEY]`; secret query names and all non-allowlisted query values are removed. Never assume
redacting only values is sufficient for SDK exception dictionaries or attacker-controlled headers.

Redaction is defense in depth. Structured application logs and span attributes should first use a
small content-free allowlist (request/job ID, fixed provider, operation, result, duration, status),
then call the recursive redactor at the formatter/export boundary. Names, emails, prompts,
responses, exception `repr`, HTTP request/response bodies, complete URLs, SDK objects, raw/wrapped
keys, ciphertext, and encryption context values do not belong in general telemetry. Alerts use
fixed categories and counts only.

## Browser credential boundary

The website and dashboard accept only Supabase's public `sb_publishable_...` key or a legacy JWT
whose role is exactly `anon`. A `sb_secret_...` value or JWT with `service_role` fails before the
Supabase browser client is created. The browser source references only
`NEXT_PUBLIC_SUPABASE_ANON_KEY`/`VITE_SUPABASE_ANON_KEY`; service-role and provider-owned server
environment secrets are prohibited.

Dashboard-session Brevitas credentials exist only in memory. The cache is capped at 128 entries,
uses LRU eviction, and has a 15-minute absolute TTL. It does not use local storage, IndexedDB,
cookies, Supabase tables, telemetry, or error messages. Long-lived organization service accounts
are created only by an explicit administrator action.

Credential discovery/minting is single-flight per user and globally capped at 128 in-flight
users. Concurrent callers validate a stale key and mint its replacement only once, preventing an
out-of-order response from caching a credential superseded by another mint. All transport,
validation, mint, streaming, and event/callback error messages pass through browser redaction or a
fixed generic message.

The authentication shell must call the exported `clearSessionKeyCache()` on every signed-out/auth
loss event and before switching users. The call is safe and idempotent: it clears all cached values
and advances an invalidation generation, so a mint or validation response already in flight cannot
repopulate the cache after sign-out. An invalidated caller receives a fixed cancellation error and
must wait for a newly authenticated session before retrying.

Bring-your-own provider keys entered by a user necessarily transit the authenticated browser-to-API
request once; they must not return in an API response. Browser error handling redacts credential
shapes as a final safeguard. Brevitas-managed provider credentials remain server-side and must
never be embedded into JavaScript, HTML, analytics config, source maps, or public environment
variables.

## Verification and audit evidence

Before release, retain results for:

- KMS unavailable at startup, wrap, and uncached unwrap;
- active KMS readiness success, error, timeout, stale evidence, and recovery while liveness stays up;
- envelope/ciphertext/metadata tamper and tenant-context swap;
- legacy decrypt, dry-run, re-encrypt, rollback window, and new-version verification;
- cache TTL, maximum size, LRU eviction, and shutdown clear;
- recursive redaction of nested headers, URLs, sequences, and exceptions;
- rejection of Supabase service credentials in browser config and absence of server secret
  variable names from browser sources/bundles; and
- constant-time API/internal-token comparison paths.

Managed-KMS provider audit logs, rotation approvals, deployment evidence, and access reviews are
SOC 2 evidence. They contain key resource identifiers and operator identities, not plaintext keys.
