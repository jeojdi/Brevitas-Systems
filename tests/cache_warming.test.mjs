import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const sql = readFileSync(new URL('../supabase/migrations/202607280001_cache_warming.sql', import.meta.url), 'utf8')
const multiSql = readFileSync(new URL('../supabase/migrations/202607280003_multi_provider_warming.sql', import.meta.url), 'utf8')

test('warming tables are RLS-enabled and revoked from every PostgREST role', () => {
  for (const table of ['warm_credentials', 'warm_prefixes', 'warm_budget_ledger']) {
    assert.match(sql, new RegExp(`alter table public\\.${table} enable row level security`))
    assert.match(sql, new RegExp(`revoke all on table public\\.${table} from public, anon, authenticated, service_role`))
  }
  assert.doesNotMatch(sql, /create policy/)
  assert.doesNotMatch(sql, /grant [^;]*on table public\.warm_/s)
})

test('warming state is written only through security definer RPCs', () => {
  for (const rpc of [
    'warm_prefix_observe',
    'warm_due_claim',
    'warm_ping_settle',
    'warm_credentials_upsert',
    'warm_credentials_purge',
    'warm_read_status',
    'purge_warm_state',
  ]) {
    assert.match(sql, new RegExp(`create or replace function public\\.${rpc}\\(`))
    assert.match(sql, new RegExp(`grant execute on function public\\.${rpc}\\([^)]*\\)\\s+to service_role`))
    assert.match(sql, new RegExp(`revoke all on function public\\.${rpc}\\([^)]*\\)\\s+from public, anon, authenticated`))
  }
  const definers = sql.match(/security definer/g) ?? []
  assert.ok(definers.length >= 8, 'every RPC and trigger function is security definer')
  assert.doesNotMatch(sql, /security invoker/)
})

test('every warming function pins its search_path', () => {
  const created = sql.match(/create or replace function/g) ?? []
  const pinned = sql.match(/set search_path = pg_catalog, public/g) ?? []
  assert.equal(pinned.length, created.length)
})

test('prefix hashes are constrained to lowercase sha-256 hex', () => {
  assert.match(sql, /prefix_hash text not null check \(prefix_hash ~ '\^\[0-9a-f\]\{64\}\$'\)/)
  assert.match(sql, /p_prefix_hash !~ '\^\[0-9a-f\]\{64\}\$'/)
})

test('prefixes expire within seven days of the last arrival', () => {
  assert.match(sql, /expires_at <= last_seen_at \+ interval '7 days'/)
  assert.match(sql, /expires_at > created_at/)
  assert.match(sql, /enforce_warm_prefixes_absolute_bound/)
  assert.match(sql, /for each statement execute function public\.enforce_warm_prefixes_absolute_bound/)
})

test('warming cannot be enabled without recorded consent and a nonzero budget', () => {
  assert.match(sql, /constraint warm_credentials_enabled_requires_consent/)
  assert.match(sql, /not enabled or \(consent_at is not null and daily_budget_usd > 0\)/)
  assert.match(sql, /enabling warming requires a consenting actor/)
  assert.match(sql, /enabling warming requires a nonzero daily budget/)
})

test('claiming is fenced by a transaction advisory lock with a lease contract', () => {
  assert.match(sql, /pg_try_advisory_xact_lock\(\s*hashtextextended\('brevitas\.warming\.due_claim\.v1', 0\)/)
  assert.match(sql, /'status', 'lease_unavailable'/)
  assert.match(sql, /pg_advisory_xact_lock\(\s*hashtextextended\('brevitas\.warm_prefixes\.write_bound\.v1', 0\)/)
})

test('the ROI gate parenthesizes its CASE so the IF survives plpgsql parsing', () => {
  // Unparenthesized, plpgsql terminates the IF expression at the CASE's own
  // THEN and CREATE FUNCTION fails, rolling back the whole migration.
  assert.match(sql, /if v_p_return < \(case when v_row\.arrival_count < p_roi_min_arrivals\s+then p_roi_min_p else p_roi_break_even_p end\) then/)
  assert.doesNotMatch(sql, /if [^;()]*< case when/)
})

test('observation serializes per organization, never fleet-wide', () => {
  assert.match(sql, /hashtextextended\('brevitas\.warm_prefixes\.write_bound\.v1:'\s*\|\| p_organization_id::text \|\| ':' \|\| p_provider, 0\)/)
})

test('the absolute-bound trigger is estimate-gated and its eviction order is indexed', () => {
  // The common far-below-cap insert must not take the global lock or sort
  // the whole table; only the over-cap regime pays for eviction.
  assert.match(sql, /greatest\(relation\.reltuples, 0\)/)
  assert.match(sql, /warm_prefixes_bound_idx\s+on public\.warm_prefixes \(last_seen_at desc, prefix_hash desc\)/)
})

test('claims reserve at least the observer-priced worst case per ping', () => {
  assert.match(sql, /ping_reserve_usd numeric\(18,10\) not null default 0/)
  assert.match(sql, /p_ping_reserve_usd numeric default null/)
  assert.match(sql, /ping_reserve_usd = coalesce\(p_ping_reserve_usd, prefix\.ping_reserve_usd\)/)
  assert.match(sql, /v_reserve := greatest\(\s*v_row\.ping_reserve_usd,\s*round\(p_reserve_usd_per_mtok \* v_row\.prefix_tokens \/ 1000000\.0, 10\)\)/)
})

test('claims lease past a worker batch and settle is fenced by the claim token', () => {
  assert.match(sql, /claim_token uuid/)
  assert.match(sql, /p_claim_lease_seconds integer default 900/)
  assert.match(sql, /coalesce\(p_claim_lease_seconds, 0\) not between 60 and 7200/)
  assert.match(sql, /greatest\(\s*p_claim_lease_seconds, p_safety_margin_seconds, 60\)/)
  assert.match(sql, /'claim_token', v_token/)
  assert.match(sql, /p_claim_token uuid default null/)
  const fenced = sql.match(/and \(p_claim_token is null or prefix\.claim_token = p_claim_token\)/g) ?? []
  assert.equal(fenced.length, 2, 'warmed and prefix_invalid settles are both token-fenced')
})

test('keep-existing-key updates in place instead of proposing an empty insert row', () => {
  // Postgres evaluates table CHECK constraints on the proposed insert row
  // before ON CONFLICT resolution, so '' must never travel through the upsert.
  assert.match(sql, /if p_credential_ciphertext = '' then/)
  assert.match(sql, /update public\.warm_credentials cred\s+set enabled = p_enabled,/)
  assert.doesNotMatch(sql, /case when p_credential_ciphertext <> ''\s+then p_credential_ciphertext else cred\.credential_ciphertext end/)
  assert.match(sql, /warming requires a stored provider credential/)
})

test('claims filter on ROI, budget, and stop-loss with caller-supplied constants', () => {
  assert.match(sql, /prefix\.consecutive_misses < p_stop_loss/)
  assert.match(sql, /coalesce\(prefix\.ewma_interarrival_s <= p_max_gap_seconds, true\)/)
  assert.match(sql, /p_roi_min_arrivals/)
  assert.match(sql, /then p_roi_min_p else p_roi_break_even_p end/)
  assert.match(sql, /v_reserved \+ v_spent \+ v_reserve > v_row\.daily_budget_usd/)
})

test('money columns are numeric(18,10) and never negative', () => {
  assert.match(sql, /daily_budget_usd numeric\(18,10\) not null default 0/)
  assert.match(sql, /reserved_usd numeric\(18,10\) not null default 0 check \(reserved_usd >= 0\)/)
  assert.match(sql, /spent_usd numeric\(18,10\) not null default 0 check \(spent_usd >= 0\)/)
})

test('prefixes carry the tenant-composite customer foreign key', () => {
  assert.match(sql, /foreign key \(organization_id, customer_id\)\s+references public\.customers\(organization_id, id\) on delete cascade/)
})

test('the migration is one transaction with plain indexes and a privilege contract', () => {
  assert.match(sql, /^begin;$/m)
  assert.match(sql, /^commit;\s*$/m)
  assert.doesNotMatch(sql, /concurrently/i)
  for (const index of ['warm_prefixes_due_idx', 'warm_prefixes_expiry_idx', 'warm_prefixes_org_idx']) {
    assert.match(sql, new RegExp(index))
  }
  assert.match(sql, /\$privilege_contract\$/)
  assert.match(sql, /unsafe % privilege contract for public\.%: %/)
  assert.match(sql, /refuses to expose warming data without RLS/)
})

test('multi-provider warming admits exactly the provably profitable providers', () => {
  // The honesty rule in schema form: only providers where a keep-alive ping
  // provably costs less than the return it saves may hold warming state.
  for (const table of ['warm_credentials', 'warm_prefixes', 'warm_budget_ledger']) {
    assert.match(multiSql, new RegExp(`drop constraint if exists ${table}_provider_check`))
    assert.match(multiSql, new RegExp(`add constraint ${table}_provider_check\\s+check \\(provider in \\('anthropic', 'openai', 'deepseek'\\)\\)`))
  }
  const admitted = multiSql.match(/\('anthropic', 'openai', 'deepseek'\)/g) ?? []
  assert.equal(admitted.length, 8, 'three constraints, four RPC checks, one jsonb key allowlist')
  for (const excluded of ['groq', 'fireworks', 'mistral', 'together', 'xai', 'openrouter', 'perplexity']) {
    assert.doesNotMatch(multiSql, new RegExp(`provider in \\([^)]*'${excluded}'`))
  }
  assert.match(multiSql, /measurement only/)
  assert.match(multiSql, /can never net positive/)
  assert.match(multiSql, /nothing to warm/)
})

test('multi-provider warming supersedes the frozen RPC provider checks in place', () => {
  for (const rpc of [
    'warm_prefix_observe',
    'warm_due_claim',
    'warm_ping_settle',
    'warm_credentials_upsert',
    'warm_credentials_purge',
  ]) {
    assert.match(multiSql, new RegExp(`create or replace function public\\.${rpc}\\(`))
    assert.match(multiSql, new RegExp(`grant execute on function public\\.${rpc}\\([^)]*\\)\\s+to service_role`))
    assert.match(multiSql, new RegExp(`revoke all on function public\\.${rpc}\\([^)]*\\)\\s+from public, anon, authenticated`))
  }
  const created = multiSql.match(/create or replace function/g) ?? []
  const pinned = multiSql.match(/set search_path = pg_catalog, public/g) ?? []
  assert.equal(pinned.length, created.length)
  assert.doesNotMatch(multiSql, /security invoker/)
  assert.doesNotMatch(multiSql, /concurrently/i)
  assert.match(multiSql, /^begin;$/m)
  assert.match(multiSql, /^commit;\s*$/m)
  assert.match(multiSql, /\$privilege_contract\$/)
  assert.match(multiSql, /202607280003 refuses to expose warming data without RLS/)
})

test('per-provider break-even arrives as a trailing DEFAULT arg, not a column', () => {
  // The nine-argument 202607280001 signature must be dropped first or the
  // ten-argument call would be ambiguous; a null map keeps flat behaviour.
  assert.match(multiSql, /drop function if exists public\.warm_due_claim\(\s*integer, numeric, integer, numeric, numeric, integer, integer, integer, integer\s*\)/)
  assert.match(multiSql, /p_roi_break_even_by_provider jsonb default null/)
  assert.match(multiSql, /jsonb_typeof\(p_roi_break_even_by_provider\) <> 'object'/)
  assert.match(multiSql, /v_break_even := coalesce\(\s*\(p_roi_break_even_by_provider ->> v_row\.provider\)::numeric,\s*p_roi_break_even_p\)/)
  assert.match(multiSql, /then p_roi_min_p else v_break_even end\) then/)
  assert.match(multiSql, /grant execute on function public\.warm_due_claim\(\s*integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb\s*\)\s+to service_role/)
  assert.doesNotMatch(multiSql, /alter table [^;]*add column/i)
})
