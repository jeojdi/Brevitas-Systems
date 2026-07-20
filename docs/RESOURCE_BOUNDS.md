# Resource bounds

All retained state has both a positive lifetime and a finite size. Environment
values are parsed by `ResourceBounds.from_env()`: zero and negative numbers clamp
to the safe minimum, values above the repository ceiling clamp down, and malformed
values fail startup. No value such as `0`, `-1`, `none`, or `forever` disables a
limit.

The common implementation is `brevitas.resource_bounds.BoundedTTLMap`, a
thread-safe TTL/LRU map with independent key, value, entry-count, and aggregate
byte limits. It removes expired entries before deterministic least-recently-used
eviction. The map owns a detached copy of every stored value and returns detached
snapshots, with configurable copiers for session objects; retained size accounting
therefore cannot be invalidated through a caller alias. An optional `on_remove`
finalizer runs exactly once outside the map lock for TTL, replacement, count/byte
eviction, explicit discard, and clear. A configurable resource identity prevents
a mutation/replacement from closing a client still owned by the replacement.
Sync and async finalizers are settled before return; failures and exception text
are suppressed and no background task is left running. `require_size` and
`extend_bounded_list` check bytes before content is
encrypted or retained.

## Policy defaults

| Resource | Default | Absolute ceiling | Environment variable |
| --- | ---: | ---: | --- |
| Request body | 2 MiB | 16 MiB | `BREVITAS_REQUEST_MAX_BYTES` |
| Request list items | 512 | 2,000 | `BREVITAS_REQUEST_MAX_ITEMS` |
| Semantic-cache TTL | 1 hour | 24 hours | `BREVITAS_CACHE_TTL_SECONDS` |
| Semantic-cache rows | 10,000 | 1,000,000 | `BREVITAS_CACHE_MAX_ENTRIES` |
| Semantic-cache raw response | 1 MiB | 8 MiB | `BREVITAS_CACHE_MAX_ENTRY_BYTES` |
| SQLite semantic candidates | 256 | 2,048 | `BREVITAS_CACHE_CANDIDATE_LIMIT` |
| Process-registry TTL | 1 hour | 24 hours | `BREVITAS_REGISTRY_TTL_SECONDS` |
| Process-registry entries | 10,000 | 100,000 | `BREVITAS_REGISTRY_MAX_ENTRIES` |
| Process-registry value | 2 MiB | 16 MiB | `BREVITAS_REGISTRY_MAX_VALUE_BYTES` |
| Session prior content | 1 hour | 24 hours | `BREVITAS_SESSION_TTL_SECONDS` |
| Session prior items | 128 | 2,000 | `BREVITAS_SESSION_MAX_ITEMS` |
| Session retained bytes | 2 MiB | 16 MiB | `BREVITAS_SESSION_MAX_BYTES` |
| Individual session item | 256 KiB | 4 MiB | `BREVITAS_SESSION_MAX_ITEM_BYTES` |
| Redis job stream entries | 100,000 | 1,000,000 | `BREVITAS_REDIS_STREAM_MAX_ENTRIES` |
| Redis job stream TTL | 1 hour | 24 hours | `BREVITAS_REDIS_STREAM_TTL_SECONDS` |
| Encrypted job payload/result TTL | 1 hour | 24 hours | `BREVITAS_JOB_PAYLOAD_TTL_SECONDS`, `BREVITAS_JOB_RESULT_TTL_SECONDS` |
| Raw job payload/result | 1 MiB / 2 MiB | 8 MiB / 16 MiB | `BREVITAS_JOB_MAX_PAYLOAD_BYTES`, `BREVITAS_JOB_MAX_RESULT_BYTES` |
| Demo session TTL/count | 1 hour / 100 | 24 hours / 1,000 | `BREVITAS_DEMO_SESSION_TTL_SECONDS`, `BREVITAS_DEMO_MAX_SESSIONS` |
| Demo document/history | 8 MiB / 2 MiB | 32 MiB / 16 MiB | `BREVITAS_DEMO_DOCUMENT_MAX_BYTES`, `BREVITAS_DEMO_HISTORY_MAX_BYTES` |
| Vercel rate-limit entries/TTL | 50,000 / 1 hour | 250,000 / 24 hours | `BREVITAS_WEB_RATE_LIMIT_MAX_ENTRIES`, `BREVITAS_WEB_RATE_LIMIT_TTL_MS` |

Semantic response caching remains disabled unless a tenant opts in and
`BREVITAS_CACHE_ENABLED=true`. Semantic matching is separately disabled by
default. The SQL migration rejects non-positive or over-24-hour cache rows,
rejects ciphertext over 16 MiB, and applies an absolute one-million-row trigger.
Normal hosted writes use `semantic_cache_store_bounded`, which atomically upserts
and evicts oldest rows at the configured lower limit. A transaction advisory lock
serializes expired-row purge, upsert, and eviction across replicas; direct
service-role inserts and updates are revoked. Local SQLite performs the same
oldest-first eviction in one immediate transaction. Responses are canonically
serialized exactly once and sized before encryption. Envelope associated data
binds purpose, tenant digest, exact request hash, and provider/model identity, so
cross-row or cross-tenant ciphertext swaps cannot decrypt. SQLite semantic scans
order newest-first and read no more than the positive candidate cap. The optional
`encryption_cipher` interface accepts
the managed-KMS `EnvelopeCipher` (`encrypt_text`/`decrypt_text`) without changing
the database schema. The default Fernet compatibility path encrypts and verifies
the same canonical context digest, so production factory-created hosted caches do
not lose swap protection. PostgreSQL derives `created_at` from its own clock and
accepts only a clamped TTL in the bounded RPC; a BEFORE trigger normalizes direct
owner writes and strips `response_json`, backed by a `response_json IS NULL`
constraint.

`BrevitasSession` expires individual prior outputs, evicts oldest content by both
item count and UTF-8 bytes, and rejects an oversized output before retention.
Interactive chat and demo applications limit file counts, stream only a bounded
upload prefix into memory, limit PDF extracted text, expire whole session
registries, and remeasure each mutable session after bounded-history updates.
Their registries close owned Brevitas clients on every removal path, expose an
explicit session-delete endpoint, and clear deterministically on FastAPI shutdown
and process exit. CLI/fixed/agent demo clients close in `finally` paths, including
provider errors and partially consumed generators.

The Next.js process-local limiter denies new identities when its bounded map is
full instead of evicting an active enforcement record. This map is defense in
depth only: Vercel/WAF controls must enforce the shared public limit because a
process-local map cannot coordinate across functions.

## Complete inventory and ownership contracts

| Surface | Required enforcement |
| --- | --- |
| `brevitas.semantic_cache` SQLite/Supabase | Positive TTL capped at 24 hours; raw response and row count capped; oldest-first cleanup; encrypted content only |
| `brevitas.session` prior content | TTL, item count, per-item bytes, aggregate bytes |
| `brevitas.chat`, `brevitas.compare`, `brevitas.demos`, `brevitas.webchat` | Document bytes, file count, history items/bytes, whole-session TTL/count/bytes |
| `src/lib/rate-limiter.ts` | Entry TTL/count/key length; deny at capacity; finite cleanup interval and block duration |
| `api.distributed_limits` Redis RPM/TPM keys and active leases | One-minute counter TTL; positive capped lease TTL; concurrency cardinality cap; 128-byte opaque identity; production fails closed without Redis |
| `brevitas.provider_reliability` circuit registry | Existing TTL/LRU state count and in-flight-aware fail-closed capacity |
| `brevitas.proxy._routers`, `brevitas.proxy._sessions` | Use `BoundedTTLMap.get_or_create` with registry TTL/count/bytes; session sizing must use retained content bytes |
| `api.server._valid_key_cache`, `_auth_context_cache` | Bounded TTL/LRU maps; revocation TTL remains short; hash/opaque IDs only |
| `api.server._proxy_windows`, `_proxy_active` | Bounded identity map plus per-window timestamp count; shared authoritative admission remains Redis |
| `api.server._POSTHOG_CACHE` | Positive TTL, key count, and response bytes; never retain prompt, response, name, or email |
| `api.server._seq_streams` | TTL and registry count; persist only content-free quality statistics if durability is needed |
| `api.server` compressor status/single-flight | Positive probe TTL, exactly one cached status and one in-flight future; cancel executor on shutdown |
| `api.jobs` payload/result | Call `require_size` before encryption; default one-hour and maximum 24-hour expiry; purge terminal rows |
| `api.jobs.RedisJobDispatcher` | `XADD MAXLEN` plus positive stream `EXPIRE`; stream carries opaque job IDs only; Postgres remains authoritative |
| `api.jobs.InMemoryJobStore` | Bound rows and idempotency index together; remove both on expiry; reject at capacity rather than lose accepted work |
| Dashboard in-memory API-key sessions | Positive browser-session TTL and maximum entries; delete on sign-out; raw keys never enter persistent storage |
| Server PostHog SDK queue | Existing `maxQueueSize`; shutdown flush must have a finite deadline and discard safely after it |

Owners integrating a mutable object should pass a content-aware `sizer`, then call
`mutate` with a content-aware copier. It serializes updates, remeasures before
commit, and preserves the prior value when a mutation is rejected. Authoritative coordination
must reject work when a bound cannot be atomically enforced. Non-authoritative
caches may skip a write and recompute.

## Migration rollback

Rollback should disable cache writes first, then drop
`semantic_cache_store_bounded`, both cache triggers and their functions, and the
four new check constraints. Do not restore deleted plaintext
`response_json` values. Retain `response_ciphertext` and `tenant_namespace` while
the application version using them may still run. A rollback that removes the
24-hour or size constraints reopens unbounded retention and requires security
approval.
