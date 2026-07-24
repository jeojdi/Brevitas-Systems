import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { parseWaitlistAdmission } from '../src/lib/waitlist-admission.mjs'
import {
  expectedFreshMigrationOrder,
  expectedUpgradeMigrationOrder,
} from '../scripts/ci/verify-migrations.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')
const sharedLimitMigration = 'supabase/migrations/202607200010_shared_endpoint_rate_limits.sql'

test('shared endpoint limiter is integrated before its dependent routes and later migrations', () => {
  assert.ok(expectedFreshMigrationOrder.includes(sharedLimitMigration))
  assert.ok(expectedUpgradeMigrationOrder.includes(sharedLimitMigration))
  assert.ok(expectedFreshMigrationOrder.indexOf(sharedLimitMigration) <
    expectedFreshMigrationOrder.indexOf('supabase/migrations/202607200011_compliance_billing_isolation.sql'))
  assert.ok(expectedUpgradeMigrationOrder.indexOf(sharedLimitMigration) <
    expectedUpgradeMigrationOrder.indexOf('supabase/migrations/202607200011_compliance_billing_isolation.sql'))
  assert.match(read('scripts/ci/migration-fresh-manifest.txt'), /202607200010/)
  assert.match(read('scripts/ci/migration-upgrade-manifest.txt'), /202607200010/)
})

test('one atomic server-only RPC enforces shared global and normalized identity windows', () => {
  const migration = read(sharedLimitMigration)
  assert.match(migration, /create table if not exists public\.shared_endpoint_rate_limits/)
  assert.match(migration, /alter table public\.shared_endpoint_rate_limits enable row level security/)
  assert.match(migration,
    /revoke all on table public\.shared_endpoint_rate_limits[\s\S]+anon[\s\S]+authenticated[\s\S]+service_role/)
  assert.match(migration, /returns jsonb[\s\S]+language plpgsql[\s\S]+security definer/)
  assert.match(migration, /set search_path = pg_catalog, public, extensions, pg_temp/)
  assert.match(migration, /v_global_window interval := interval '1 minute'/)
  assert.match(migration, /v_global_limit integer := 120/)
  assert.match(migration, /v_identity_window interval := interval '10 minutes'/)
  assert.match(migration, /v_identity_limit integer := 3/)
  assert.match(migration, /lower\(pg_catalog\.btrim\(p_email\)\)/)
  assert.match(migration, /encode\(digest\(v_email, 'sha256'\), 'hex'\)/)
  assert.match(migration, /pg_advisory_xact_lock/g)
  assert.match(migration, /'waitlist\.global'/)
  assert.match(migration, /'waitlist\.identity'/)
  assert.equal((migration.match(/on conflict \(endpoint_scope, identity_hash\) do update/g) || []).length, 4)
  assert.match(migration, /request_count < v_global_limit/)
  assert.match(migration, /request_count < v_identity_limit/)
  assert.ok(migration.indexOf('pg_advisory_xact_lock') < migration.indexOf('insert into public.waitlist'))
  assert.match(migration,
    /grant execute on function public\.submit_waitlist_signup[\s\S]+to service_role/)
  assert.doesNotMatch(migration, /grant execute[\s\S]+to (?:anon|authenticated)/i)
})

test('admission result parsing is bounded, duplicate-safe, and fails closed', () => {
  assert.deepEqual(
    parseWaitlistAdmission({ ok: true, code: 'accepted', created: true }),
    { status: 'accepted' },
  )
  assert.deepEqual(
    parseWaitlistAdmission({ ok: true, code: 'accepted', created: false }),
    { status: 'accepted' },
  )
  assert.deepEqual(
    parseWaitlistAdmission({ ok: false, code: 'rate_limited', retry_after_seconds: 37 }),
    { status: 'rate_limited', retryAfterSeconds: 37 },
  )
  for (const invalid of [
    true,
    null,
    {},
    { ok: false, code: 'rate_limited', retry_after_seconds: 0 },
    { ok: false, code: 'rate_limited', retry_after_seconds: 601 },
    { ok: true, code: 'unexpected', created: true },
  ]) {
    assert.throws(() => parseWaitlistAdmission(invalid), /Invalid shared waitlist admission/)
  }
})

test('waitlist route trusts no forwarding identity and emits no submitted-email logs', () => {
  const route = read('src/app/api/waitlist/route.ts')
  const server = read('src/lib/waitlist-server.ts')
  assert.doesNotMatch(route, /withRateLimit|RATE_LIMITS|rate-limiter/)
  assert.doesNotMatch(route, /x-forwarded-for|x-real-ip|x-client-ip|forwarded/i)
  assert.doesNotMatch(server, /x-forwarded-for|x-real-ip|x-client-ip|forwarded/i)
  assert.doesNotMatch(route, /console\.(?:log|warn|error)\([^;]*(?:\{\s*email|,\s*error\b)/)
  assert.doesNotMatch(route, /New waitlist signup saved/)
  assert.doesNotMatch(route, /already on the list/)
  assert.match(route, /status: 429/)
  assert.match(route, /'Retry-After': String\(admission\.retryAfterSeconds\)/)
  assert.match(route, /status: 503[\s\S]+\'Retry-After\': '5'/)
  assert.match(server, /throw new WaitlistUnavailableError\(\)/)
  assert.match(server, /parseWaitlistAdmission\(data\)/)
})

test('separate-session test covers cross-instance races and bounded global behavior', () => {
  const runner = read('scripts/ci/run-waitlist-shared-limit-test.sh')
  const assertions = read('scripts/ci/waitlist-shared-rate-limit-assertions.sql')
  assert.match(runner, /worker_pids=\(\)/)
  assert.match(runner, />"\$\{result_directory\}\/\$\{worker_index\}\.txt" &/)
  assert.match(runner, /accepted_count[\s\S]+-ne 3/)
  assert.match(runner, /limited_count[\s\S]+-ne 9/)
  assert.match(assertions, /case v_index[\s\S]+Shared-Limit-Identity[\s\S]+SHARED-LIMIT-IDENTITY/)
  assert.match(assertions, /for v_index in 1\.\.120 loop/)
  assert.match(assertions, /shared-limit-global-overflow@example\.invalid/)
  assert.match(assertions, /identity_hash like '%@%'/)
})
