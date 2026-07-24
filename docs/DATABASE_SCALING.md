# Database scaling

Postgres is authoritative in every hosted environment. SQLite is a bounded local/test
fallback selected only with `BREVITAS_STORE=sqlite`; `make_store()` rejects it on Railway
and in production.

## Query contracts

Every Supabase/PostgREST query, RPC, and batch write passes through one dependency-observation
boundary. It records W5's fixed `postgres` dependency label, a finite outcome (`success`,
`timeout`, `unavailable`, or `error`), and elapsed seconds. Connection failures and HTTP 5xx are
`unavailable`; explicit timeouts are `timeout`; other exceptions/rejections are `error`. No path,
SQL, tenant, cursor, request ID, payload, or content is provided to telemetry, and metric failures
are swallowed so they cannot alter authoritative database behavior. W5 currently exposes no
fixed-cardinality report-truncation instrument; the bounded report response continues to expose its
existing `truncated` boolean without synthesizing a dynamic metric.

`004_database_scaling.sql` is ordered after the timestamped Supabase migrations through
`202607170003_durable_jobs.sql`; `202607170001_enterprise_tenancy.sql` specifically supplies
the usage tenant columns. The migration fails early when that prerequisite is absent. The
release owner must place/execute 004 as the next timestamped Supabase change rather than
applying this repository file out of order. It adds service-role-only RPCs:

- `usage_stats`, `usage_breakdown`, and `usage_grouped` aggregate in Postgres and always
  receive the API key plus the organization/legacy owner resolved from that key.
- `admin_usage_report` aggregates filtered administrative reports in Postgres and caps
  grouped output at 500 rows. Totals cover the full filtered set; `truncated` identifies a
  capped group list.
- `admin_usage_report_page` performs the endpoint's selected financial sort in SQL and
  keysets on `(selected_sort_value, immutable_group_hash)`. Its opaque cursor is bound to
  both sort field and direction, returns one look-ahead row, caps pages at 500, and rejects
  reuse after the ordering changes.
- `admin_key_repository_usage` replaces the previous full usage-log download with a
  grouped, capped query.
- `usage_page` orders by `(ts DESC, id DESC)`, uses the matching tuple as an opaque cursor,
  returns at most 200 requested rows plus one look-ahead row, and never performs offset
  scans. The identity `id` is the deterministic tie-breaker for equal timestamps.

The indexes follow the real predicates and sort order: organization, owner, or key followed
by `ts DESC, id DESC`, with tenant grouping indexes for pipeline, agent, run, and customer.
The ordinary index statements in 004 require an empty/small table or a controlled maintenance
window. For a large live table, pre-stage the same names using
`004_database_scaling.concurrent_indexes.sql`, executing every statement outside a
transaction. Check `pg_index.indisvalid` and `indisready`; an interrupted concurrent build
can leave an invalid same-name index that must be dropped concurrently before retry. Once
all indexes are valid, the idempotent statements in 004 are no-ops. Record this pre-stage in
the change ticket and still apply the timestamped 004 migration. Validate representative
plans with `EXPLAIN (ANALYZE, BUFFERS)` after `ANALYZE`. No migration is applied by this run.

### Endpoint integration contract for W1/W8

The existing `/v1/admin/stats/breakdown` route still accepts an unbounded `offset` and sorts
an already capped list in Python. W1 must replace `offset` with an opaque `cursor` query
parameter (empty by default, maximum 512 characters) and call:

```python
report = _store.get_admin_report_page(
    filters, sort=sort, direction=direction, cursor=cursor, limit=limit,
)
return {**report, "range": range}
```

The route must not sort, slice, or calculate offsets after this call. Its pagination object
is `{total, limit, next_cursor, has_more}`. Keep `get_admin_report()` for the unpaginated
billing-summary aggregation until billing gets its own account-level SQL contract.

W8 must replace dashboard offset state with a cursor stack: send the current opaque cursor,
push `next_cursor` on Next, pop the stack on Previous, and reset the stack whenever filters,
sort, direction, or range changes. The browser must never decode or synthesize cursors.

## Writes and imports

`record_usage()` remains synchronous. In particular, an authoritative receipt is inserted
before returning so the existing Postgres billing trigger runs in the same transaction.

`record_usage_batch()` accepts at most 100 rows. A stable non-empty `request_id` makes a row
idempotent through the existing partial unique index `(key_hash, request_id)`. Empty request
IDs remain append-only for backward compatibility. A definite HTTP 4xx rejection proves the
atomic bulk transaction did not commit and may be isolated row-by-row. A timeout/connection
failure is retried only when every row has a stable request ID. An ambiguous batch containing
an empty ID raises `AmbiguousUsageBatchError` and must be reconciled, never automatically
retried. Partial results identify `failed_records`, safe `retry_records`, or
`ambiguous_records`.

`BoundedUsageWriter` is for non-authoritative telemetry. It flushes at 100 rows by default,
every second by default, and on `close()`/context-manager shutdown. Capacity includes queued,
in-flight, and unresolved rows. Close blocks new adds and waits for active adds/flushes.
Authoritative rows bypass telemetry state and call the synchronous path directly. Partial or
ambiguous outcomes are retained without retrying successful append-only rows;
`failed_records` exposes them and `take_failed_records()` explicitly transfers them to a
durable dead-letter/reconciliation owner. Applications must invoke `close()` in their
shutdown hook; `atexit` is a last local-process safeguard, not graceful Railway termination.

The legacy SQLite importer streams with `fetchmany()` and bounded batches rather than
loading the source table into memory. Every imported row is forcibly
`authoritative=false`/`receipt_source=import`, even if a historical source claims it was
priced and authoritative. Imported `brevitas_fee_usd` is also forced to zero, so imports
cannot enqueue or represent collectible billing. It stops on any failed row and
is safe to rerun when source rows retain their generated import request IDs.

## Rollout and rollback

1. Back up staging and verify timestamped migrations through `202607170003` are recorded.
2. For a large table, pre-stage and validate concurrent indexes as described above.
3. Apply 004 through the timestamped Supabase flow and refresh the PostgREST schema cache.
4. Exercise tenant A and tenant B with identical timestamps and confirm no cross-tenant
   rows, duplicate cursor rows, or aggregate discrepancies.
5. Compare SQL totals with the billing ledger and run representative `EXPLAIN` plans.
6. Deploy application code and monitor RPC latency, errors, capped reports, and receipt
   duplicate/failure counts before production promotion.

W9's ephemeral migration test must apply timestamped migrations through 003, pre-stage the
concurrent indexes with transaction wrapping disabled, apply 004 twice, and assert every
index is valid/ready, every RPC is executable only by `service_role`, cursor pages have no
duplicates across equal sort values, and cross-tenant queries return no rows. It must also
verify 004 fails without the tenancy prerequisite, imported authoritative+priced fixtures
remain non-authoritative, the rollback removes functions/indexes without deleting usage or
billing rows, and 004 can be reapplied after rollback. CI should fail if the timestamped copy
and this canonical 004 contract drift.

Rollback is explicit in `api/migrations/004_database_scaling.rollback.sql`. First deploy code
that no longer calls the RPCs. Run the function drops, then run each `DROP INDEX CONCURRENTLY`
outside a transaction. Dropping these read-path functions/indexes does not delete usage or
billing data and does not remove the authoritative billing trigger.

## Legacy hosted-cache migration guard

`api/migrations/002_semantic_cache.sql` is deprecated and is no longer a schema bootstrap.
The canonical ordered path is
`supabase/migrations/202607170001_enterprise_tenancy.sql` followed by
`supabase/migrations/202607170002_cache_security.sql`. The API compatibility guard does not
create `semantic_cache`. When it encounters an old table, it adds transition columns, purges
only rows containing `response_json` or missing ciphertext, adds the no-plaintext constraint,
drops the three-argument plaintext lookup, and revokes service-role insert/update. Running
the guard after the canonical migration preserves valid encrypted rows and the bounded
security-definer write RPC.

W9's real-Postgres test requires a pgvector-enabled Supabase-compatible database and three
paths:

1. Fresh install: apply timestamped migrations in order and assert the table has a nullable,
   always-null `response_json`, required non-empty ciphertext/tenant namespace, bounded TTL
   and size constraints, and no service-role direct insert/update privilege.
2. Legacy upgrade: create the retired plaintext table/function/grants, insert sentinel
   plaintext, run the API guard, and prove the sentinel row and old function are gone before
   applying `202607170002_cache_security.sql`. Then store/read only ciphertext through the
   bounded five-argument RPC contract.
3. Reverse ordering safety: apply the canonical timestamped migration, insert a valid
   encrypted row through its RPC, run the API guard twice, and prove the encrypted row and
   RPC survive while a direct service-role plaintext write is denied.

CI must also reject reintroduction of `response_json jsonb not null`, a service-role
insert/update grant, or the legacy three-argument lookup in the API migration directory.

## Credential-safe compatibility audit writers

SQLite key-management methods keep key mutation and their content-free audit append in one
local transaction. `api_key.created` targets the opaque `api_keys.id` UUID, never `key_hash`;
revocation targets the same opaque ID. Events carry an allowlisted actor role, valid request
ID, empty details, committed outcome, and null actor-key field on fresh local schemas.

Hosted key mutation never performs direct `api_keys` or `audit_events` writes.
`SupabaseUsageStore.create_key` supports dashboard sessions only, generates the raw key once,
and sends only its digest/prefix plus tenant, actor, expiry, and middleware request ID to
`company_admin_create_dashboard_session_key` from timestamped migration 008. It returns the
raw secret once only after an `ok=true` tenant-matching result and discards the local secret
reference on every failure. `revoke_organization_key` calls only
`company_admin_revoke_dashboard_session_key` from migration 009; an already-revoked success
is idempotent. Hosted
`revoke_keys_by_type` fails closed because no bulk audited RPC exists.

Hosted key listing is available only through:

```python
store.list_organization_keys_page(
    organization_id,
    actor_user_id,
    cursor=cursor,
    limit=limit,
    request_id=request_id,
    actor_role=actor_role,
)
```

It calls only `company_admin_dashboard_keys_page` and returns
`{keys, next_cursor, has_more, limit}`. Limits clamp to 1–100. Every returned row must contain
exactly migration 009's safe metadata fields; credential/digest/fingerprint fields, excess
rows, invalid timestamps/UUIDs/scopes, unstable ordering, and cursor-boundary violations fail
closed. The opaque cursor HMAC covers version, organization, `dashboard_keys`, `(created,id)`
and is rejected when tampered, oversized, replayed across organizations/collections, or used
with a different replica secret. All API replicas therefore require the same independent
`COMPANY_ADMIN_CURSOR_SECRET` of at least 32 characters. Uncontextualized hosted
`list_organization_keys()` fails closed and never performs `GET api_keys`.

W1 `POST /v1/keys` must use the returned `api_key` rather than generate a second hosted secret,
and
must pass the middleware request ID, authenticated actor UUID, resolved finite company role,
tenant UUID, and an expiry no more than eight hours away. W9 source inspection must reject
any hosted `GET/PATCH api_keys`, direct audit insert, or bulk revocation path and must mock
the frozen 008/009 RPC argument objects exactly. Real-Postgres tests must cover member ownership,
cross-tenant denial, duplicate/failed creation without secret return, retry/no-op revocation,
and rollback of key changes when immutable audit append fails.

W1 `GET /v1/keys` signature is `cursor: str = Query("", max_length=512)` and
`limit: int = Query(50, ge=1, le=100)`. It passes the server-derived organization, actor,
finite role, and middleware request ID to `list_organization_keys_page` and returns that
method's result unchanged. W1 `DELETE /v1/keys/{key_id}` validates a UUID and calls
`revoke_organization_key` directly; it must remove the pre-list/inventory check. Both generic
denials and cross-tenant IDs map to the same 403 response, while RPC transport failures map
to 503. Long-lived service credentials remain under service-account lifecycle endpoints.

## Atomic device delivery contract

Timestamped migration `202607170010_device_delivery_idempotency.sql` retires the destructive
hosted `consume_bvx_device` grant. W1 must use this frozen store signature for both hosted and
local implementations:

```python
consume_device_request_idempotent(
    device_hash: str,
    expected_key_hash: str,
    request_id: str,
) -> dict | None
```

The database locks the approved, unexpired exchange, compares its stored SHA-256 digest with
`expected_key_hash`, freezes the approving user's active organization, activates exactly one
device API key, appends a content-free audit event, stores a recovery receipt, and deletes the
exchange in one transaction. A mismatch clears/quarantines the exchange, revokes any colliding
activation, and never mints. The receipt retains only device/key digests, KMS ciphertext,
activated-key owner/company metadata, the separate approving human ID, consumption time, original
request identity, and its original exchange expiry; it cannot outlive that expiry or 15 minutes
after consumption. Neither raw keys nor
digests enter audit targets, details, or logs.

On success the returned object has exactly `status="consumed"`, boolean `already_consumed`,
`device_hash`, `key_hash`, `encrypted_key`, `owner_id`, `organization_id`, and `consumed_at`.
The first commit returns `already_consumed=false`; any later call with the same device digest and
verified key digest returns the identical receipt with `already_consumed=true`, including when
the retry has a new middleware request ID, only after revalidating that the exact activated API
key still exists with the same key digest, owner and organization, `key_type=device`, no
revocation, no elapsed key expiry, and an active finite-role membership for that owner in the
receipt tenant. Replay independently requires the original approver to remain an active finite-role
member of that same company and verifies the immutable activation audit event still binds that
approver, request, and opaque API-key ID. Removing the approver therefore quarantines delivery even
when the activated billing owner remains active. Any deletion, mutation, revocation, expiry,
disablement, removal, missing/swapped approver, or audit mismatch erases the receipt ciphertext,
marks it quarantined, and fails closed. The original request ID remains
immutable activation and audit metadata. A timeout or connection loss from the first PostgREST
call is therefore safe to retry once with the same arguments. `get_device_request()` reads through
the service-role-only
`get_bvx_device_exchange` RPC so a retry can decrypt and verify the retained ciphertext before
requesting the receipt; request identity and expiry controls are never exposed by that lookup.

Every consume denial/quarantine appends `device_key.consume.denied` in the same transaction as
the quarantine/revocation. The event is content-free: empty details, finite denied outcome,
system actor, and only an opaque API-key UUID or internal receipt UUID target. Device/key digests
and ciphertext are never audit fields. SQLite routes all such branches through one transactional
helper; PostgreSQL uses the same `append_company_audit` boundary. An audit failure rolls back the
quarantine and revocation rather than leaving an unrecorded security mutation.
Migration 010 also remediates pre-constraint receipts before installing and explicitly validating
the named ciphertext/quarantine check: every already-quarantined row is forced to empty ciphertext,
including rows with a valid approver, and any pre-approver receipt is quarantined. Reapplying the
migration drops/recreates/validates the same constraint after this idempotent cleanup. The hosted
and local already-quarantined replay branches likewise clear ciphertext again before auditing and
returning denial.

Before approval, W1 must call the frozen company selector:

```python
resolve_device_approval_organization(
    owner_id: str,
    selected_organization_id: str = "",
) -> dict  # exactly {"id": UUID, "role": canonical_company_role}
```

W1 may pass only a company selection derived from its authenticated dashboard session, never a
free browser-supplied tenant. An omitted selector succeeds only when the owner has exactly one
active finite-role membership. Multiple active memberships raise `ValueError("company_selection_required")`
for W1 to map to 409; a foreign, inactive, missing, malformed, or invalid-role selection raises
`ValueError("company_access_denied")` for W1 to map to 403. W1 then passes the returned `id` as the
new final `organization_id` argument to `approve_device_request`. The approval transaction locks
and independently revalidates that exact owner/company membership before binding it, preventing a
disable/removal race between selection and approval.

W1 must also treat `member_organization(user_id)` as the route-level membership guard. Both stores
return a company only for an active membership with one of the four canonical company roles; local
legacy role aliases are canonicalized before return. Disabled, removed, foreign, malformed, and
invalid-role rows return `None`. The hosted lookup itself filters `status=active` and the finite role
set rather than trusting a row and checking only in application memory.

W9 must add migration 010 to the ordered immutable manifest and ephemeral Postgres test. Apply it
twice after 009, assert the receipt table has RLS and no direct public/anon/authenticated/service
role grants, and prove only `service_role` can execute the approval, lookup, and idempotent consume
RPCs. Tests must cover commit-then-timeout recovery without a duplicate key, a retry under a new
request ID, replay denial after key deletion/revocation/type/owner/tenant/expiry drift or member
disable/removal, independent approver removal/swap/missing checks while the billing owner remains
active, denial-audit presence and rollback atomicity for every quarantine path, digest mismatch
quarantine/revocation, activation conflicts, cross-tenant approval denial, original-expiry
purge, exact safe receipt fields, hardened `pg_catalog, public, pg_temp` search paths, opaque audit
targets, and the absence of execute permission on legacy `consume_bvx_device`. Rollback must first
deploy code that no longer calls migration-010 RPCs, wait for the maximum 15-minute receipt window,
then drop the RPCs/table and new exchange columns; it must never re-grant the destructive legacy
consume function during a live rollback.

The real-Postgres reapply fixture must additionally drop
`bvx_device_receipt_ciphertext_check`, place non-empty ciphertext on a quarantined receipt that has
a non-null approver, reapply migration 010, and assert the ciphertext is empty and the recreated
constraint has `pg_constraint.convalidated=true`.
