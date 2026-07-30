import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { parseWaitlistAdmission } from '../src/lib/waitlist-admission.mjs'
import {
  parseWaitlistNetworkBudget,
  resolveClientNetwork,
  waitlistNetworkKey,
  WAITLIST_NETWORK_LIMIT,
  WAITLIST_NETWORK_WINDOW_SECONDS,
} from '../src/lib/waitlist-network.mjs'
import {
  expectedFreshMigrationOrder,
  expectedUpgradeMigrationOrder,
} from '../scripts/ci/verify-migrations.mjs'
import {
  loaderUnavailable,
  loadModule,
  stubModule,
} from './helpers/billing-route-loader.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')
const sharedLimitMigration = 'supabase/migrations/202607200010_shared_endpoint_rate_limits.sql'

const SALT = 'waitlist-network-salt-fixture'
const headersOf = values => new Headers(values)

// waitlistDatabase() requires both names; neither is used by the stubbed client.
process.env.SUPABASE_URL ||= 'https://local.invalid'
process.env.SUPABASE_SERVICE_ROLE_KEY ||= 'service-role-fixture'

const rpcCalls = []
/** name -> ({data, error}) answered for that RPC. */
let rpcResponses = {}

if (loaderUnavailable === false) {
  stubModule('next/server', {
    NextRequest: class NextRequest {},
    NextResponse: { json: (body, init) => Response.json(body, init) },
  })
  stubModule('@supabase/supabase-js', {
    createClient: () => ({
      rpc(name, args) {
        rpcCalls.push({ name, args })
        const answer = rpcResponses[name]
        if (!answer) return Promise.resolve({ data: null, error: { code: 'PGRST202' } })
        return Promise.resolve(answer)
      },
    }),
  })
}

const skip = loaderUnavailable

let waitlistServer = null
async function loadWaitlistServer() {
  waitlistServer ??= await loadModule('src/lib/waitlist-server.ts')
  return waitlistServer
}

const SIGNUP = {
  email: 'network-bucket@example.invalid',
  name: null,
  company: null,
  role: null,
  pipelineShape: null,
  monthlySpend: null,
  orchestrator: null,
  notes: null,
  designPartner: false,
}

/** Run one submission against a fixed RPC answer table. */
async function submitting(responses, networkKey) {
  const { submitWaitlistSignup } = await loadWaitlistServer()
  rpcResponses = responses
  rpcCalls.length = 0
  return submitWaitlistSignup(SIGNUP, networkKey)
}

/** Capture console.warn for the duration of `run`. */
async function capturingWarnings(run) {
  const warnings = []
  const original = console.warn
  console.warn = (...args) => { warnings.push(args) }
  try {
    await run()
  } finally {
    console.warn = original
  }
  return warnings
}

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

test('waitlist route and server handle no forwarding identity and log no submitted email', () => {
  const route = read('src/app/api/waitlist/route.ts')
  const server = read('src/lib/waitlist-server.ts')
  assert.doesNotMatch(route, /withRateLimit|RATE_LIMITS|rate-limiter/)
  // Header handling belongs to ONE module. Neither the route nor the database
  // layer may learn a client address: they exchange an opaque, salted key, so
  // no raw address (PII) can reach a log line, the limiter table, or PostHog.
  assert.doesNotMatch(route, /x-forwarded-for|x-real-ip|x-client-ip|forwarded/i)
  assert.doesNotMatch(server, /x-forwarded-for|x-real-ip|x-client-ip|forwarded/i)
  assert.match(route, /waitlistNetworkKey\(request\.headers\)/)
  assert.match(route, /\}, networkKey\);/)
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

// ---------------------------------------------------------------------------
// The network dimension the shared limiter never had (contract B).
//
// public.submit_waitlist_signup enforces a per-email window and ONE global
// 120/min window, so a single source that rotates the email on every request
// consumes the whole site allowance and locks out every legitimate signup.
// public.consume_waitlist_network_budget(p_network_key, p_limit,
// p_window_seconds) -> {"allowed", "retry_after_seconds"} is charged BEFORE it.
// ---------------------------------------------------------------------------

test('a client address is truncated to its /24 or /48 network, never kept whole', () => {
  assert.equal(resolveClientNetwork(headersOf({ 'x-real-ip': '203.0.113.47' })), 'v4:203.0.113.0/24')
  // Two hosts on one /24 share a bucket; a neighbouring /24 does not.
  assert.equal(
    resolveClientNetwork(headersOf({ 'x-real-ip': '203.0.113.9' })),
    resolveClientNetwork(headersOf({ 'x-real-ip': '203.0.113.250' })),
  )
  assert.notEqual(
    resolveClientNetwork(headersOf({ 'x-real-ip': '203.0.113.9' })),
    resolveClientNetwork(headersOf({ 'x-real-ip': '203.0.114.9' })),
  )
  // /48, so the /64 an ISP hands out cannot be rotated to mint fresh buckets.
  assert.equal(
    resolveClientNetwork(headersOf({ 'x-real-ip': '2001:0db8:0001:0002:0003:0004:0005:0006' })),
    'v6:2001:db8:1::/48',
  )
  assert.equal(
    resolveClientNetwork(headersOf({ 'x-real-ip': '2001:db8:1::feed' })),
    resolveClientNetwork(headersOf({ 'x-real-ip': '2001:db8:1:9999::1' })),
  )
  assert.notEqual(
    resolveClientNetwork(headersOf({ 'x-real-ip': '2001:db8:1::1' })),
    resolveClientNetwork(headersOf({ 'x-real-ip': '2001:db8:2::1' })),
  )
  // An IPv4-mapped client is an IPv4 client, bucketed with its real peers.
  assert.equal(
    resolveClientNetwork(headersOf({ 'x-real-ip': '::ffff:203.0.113.47' })),
    'v4:203.0.113.0/24',
  )
  // Proxy decorations must not fork the bucket.
  assert.equal(resolveClientNetwork(headersOf({ 'x-real-ip': '203.0.113.47:41234' })), 'v4:203.0.113.0/24')
  assert.equal(resolveClientNetwork(headersOf({ 'x-real-ip': '[2001:db8:1::1]:443' })), 'v6:2001:db8:1::/48')
  assert.equal(resolveClientNetwork(headersOf({ 'x-real-ip': 'fe80::1%eth0' })), 'v6:fe80:0:0::/48')
  // A forwarding chain is client-first.
  assert.equal(
    resolveClientNetwork(headersOf({ 'x-forwarded-for': '203.0.113.47, 70.41.3.18, 150.172.238.178' })),
    'v4:203.0.113.0/24',
  )
  // Vercel writes the first two itself, so they win over the forgeable one.
  assert.equal(
    resolveClientNetwork(headersOf({
      'x-vercel-forwarded-for': '203.0.113.47',
      'x-real-ip': '198.51.100.9',
      'x-forwarded-for': '192.0.2.1',
    })),
    'v4:203.0.113.0/24',
  )
  // Unresolvable input degrades to "no network", never to a partial key.
  for (const headers of [
    headersOf({}),
    headersOf({ 'x-real-ip': 'not-an-address' }),
    headersOf({ 'x-real-ip': '999.1.1.1' }),
    headersOf({ 'x-forwarded-for': '   ' }),
  ]) {
    assert.equal(resolveClientNetwork(headers), null)
  }
  assert.equal(resolveClientNetwork(null), null)
  assert.equal(resolveClientNetwork({}), null)
})

test('the bucket key is a salted hash of a network and never a reversible one', () => {
  const environment = { WAITLIST_NETWORK_SALT: SALT }
  const key = waitlistNetworkKey(headersOf({ 'x-real-ip': '203.0.113.47' }), environment)

  // Opaque hex, and no fragment of the address survives into it.
  assert.match(key, /^[0-9a-f]{64}$/)
  assert.doesNotMatch(key, /203|113|\./)

  // Deterministic per network, and every host of that /24 lands in one bucket.
  assert.equal(waitlistNetworkKey(headersOf({ 'x-real-ip': '203.0.113.47' }), environment), key)
  assert.equal(waitlistNetworkKey(headersOf({ 'x-real-ip': '203.0.113.9' }), environment), key)
  assert.notEqual(waitlistNetworkKey(headersOf({ 'x-real-ip': '198.51.100.9' }), environment), key)

  // SALTED: there are only 2^32 IPv4 addresses, so an unsalted digest of the
  // network is a lookup table away from the address it came from. Changing the
  // salt must change the key, and the key must not be any plain digest of the
  // network string.
  assert.notEqual(
    waitlistNetworkKey(headersOf({ 'x-real-ip': '203.0.113.47' }), { WAITLIST_NETWORK_SALT: `${SALT}-2` }),
    key,
  )
  for (const plain of ['v4:203.0.113.0/24', '203.0.113.0/24', '203.0.113.0', '203.0.113.47']) {
    assert.notEqual(createHash('sha256').update(plain).digest('hex'), key)
  }

  // Fail open, never throw: no salt, a salt too short to be one, or no
  // resolvable address all mean "charge the global window only".
  assert.equal(waitlistNetworkKey(headersOf({ 'x-real-ip': '203.0.113.47' }), {}), null)
  assert.equal(
    waitlistNetworkKey(headersOf({ 'x-real-ip': '203.0.113.47' }), { WAITLIST_NETWORK_SALT: 'short' }),
    null,
  )
  assert.equal(waitlistNetworkKey(headersOf({}), environment), null)
})

test('a network budget verdict fails closed on denial and is bounded', () => {
  assert.deepEqual(parseWaitlistNetworkBudget({ allowed: true, retry_after_seconds: 0 }), { status: 'allowed' })
  assert.deepEqual(
    parseWaitlistNetworkBudget({ allowed: false, retry_after_seconds: 42 }),
    { status: 'rate_limited', retryAfterSeconds: 42 },
  )
  // A denial is a denial even when the retry hint is missing or absurd: the
  // decision is never discarded, only the number is repaired.
  for (const hint of [{}, { retry_after_seconds: 0 }, { retry_after_seconds: 99999 }]) {
    assert.deepEqual(
      parseWaitlistNetworkBudget({ allowed: false, ...hint }),
      { status: 'rate_limited', retryAfterSeconds: WAITLIST_NETWORK_WINDOW_SECONDS },
    )
  }
  for (const invalid of [null, true, 'allowed', [], {}, { allowed: 'yes' }]) {
    assert.throws(() => parseWaitlistNetworkBudget(invalid), /Invalid waitlist network budget/)
  }
})

test('the network budget is charged before the global window, and denial stops the write', { skip }, async () => {
  const result = await submitting(
    { consume_waitlist_network_budget: { data: { allowed: false, retry_after_seconds: 90 }, error: null } },
    'network-key-fixture',
  )
  assert.deepEqual(result, { status: 'rate_limited', retryAfterSeconds: 90 })
  // Exactly one RPC: the signup RPC — which is what consumes the global window
  // and writes the row — must not have run at all.
  assert.deepEqual(rpcCalls.map(call => call.name), ['consume_waitlist_network_budget'])
  assert.deepEqual(rpcCalls[0].args, {
    p_network_key: 'network-key-fixture',
    p_limit: WAITLIST_NETWORK_LIMIT,
    p_window_seconds: WAITLIST_NETWORK_WINDOW_SECONDS,
  })
  assert.ok(WAITLIST_NETWORK_LIMIT < 120, 'a network must not be allowed the whole global allowance')
})

test('an admitted network still pays the global window afterwards', { skip }, async () => {
  const result = await submitting({
    consume_waitlist_network_budget: { data: { allowed: true, retry_after_seconds: 0 }, error: null },
    submit_waitlist_signup: { data: { ok: true, code: 'accepted', created: true }, error: null },
  }, 'network-key-fixture')
  assert.deepEqual(result, { status: 'accepted' })
  assert.deepEqual(
    rpcCalls.map(call => call.name),
    ['consume_waitlist_network_budget', 'submit_waitlist_signup'],
  )
})

test('a missing network RPC degrades to the global window and logs once', { skip }, async () => {
  // This must be the FIRST degrade in the process: the "log once" latch is
  // module-level, exactly as it is in production.
  const accepted = { data: { ok: true, code: 'accepted', created: true }, error: null }
  const warnings = await capturingWarnings(async () => {
    // 1. Migration not applied yet (Vercel deploys on push; wyfz migrations are
    //    hand-applied one file at a time).
    assert.deepEqual(
      await submitting({
        consume_waitlist_network_budget: { data: null, error: { code: 'PGRST202' } },
        submit_waitlist_signup: accepted,
      }, 'network-key-fixture'),
      { status: 'accepted' },
    )
    assert.deepEqual(
      rpcCalls.map(call => call.name),
      ['consume_waitlist_network_budget', 'submit_waitlist_signup'],
    )
    // 2. Grant not applied, 3. unparseable verdict, 4. no key at all.
    for (const [responses, networkKey] of [
      [{ consume_waitlist_network_budget: { data: null, error: { code: '42501' } }, submit_waitlist_signup: accepted }, 'network-key-fixture'],
      [{ consume_waitlist_network_budget: { data: { unexpected: true }, error: null }, submit_waitlist_signup: accepted }, 'network-key-fixture'],
      [{ submit_waitlist_signup: accepted }, null],
    ]) {
      assert.deepEqual(await submitting(responses, networkKey), { status: 'accepted' })
      assert.ok(rpcCalls.some(call => call.name === 'submit_waitlist_signup'))
    }
    // A null key must not even attempt the RPC.
    assert.deepEqual(rpcCalls.map(call => call.name), ['submit_waitlist_signup'])
  })

  assert.equal(warnings.length, 1, 'the degrade is announced once per process, not once per signup')
  const line = warnings[0].join(' ')
  assert.match(line, /Waitlist network budget unavailable/)
  // Never an address, never the derived key, never a lead field.
  assert.doesNotMatch(line, /network-key-fixture|@|\d+\.\d+\.\d+/)
})

test('the global window still fails closed when its own RPC is unavailable', { skip }, async () => {
  const { WaitlistUnavailableError } = await loadWaitlistServer()
  await assert.rejects(
    submitting({
      consume_waitlist_network_budget: { data: { allowed: true }, error: null },
      submit_waitlist_signup: { data: null, error: { code: 'PGRST202' } },
    }, 'network-key-fixture'),
    WaitlistUnavailableError,
  )
})

test('the route derives the key from the request and charges it end to end', { skip }, async () => {
  process.env.WAITLIST_NETWORK_SALT = SALT
  const { POST } = await loadModule('src/app/api/waitlist/route.ts')

  const post = (headers, responses) => {
    rpcResponses = responses
    rpcCalls.length = 0
    return POST(new Request('https://brevitassystems.com/api/waitlist', {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...headers },
      body: JSON.stringify({ email: 'Network.Bucket@Example.Invalid' }),
    }))
  }

  const accepted = { data: { ok: true, code: 'accepted', created: true }, error: null }
  const allowed = await post({ 'x-vercel-forwarded-for': '203.0.113.47' }, {
    consume_waitlist_network_budget: { data: { allowed: true }, error: null },
    submit_waitlist_signup: accepted,
  })
  assert.equal(allowed.status, 200)
  assert.deepEqual(
    rpcCalls.map(call => call.name),
    ['consume_waitlist_network_budget', 'submit_waitlist_signup'],
  )
  // The key that reached the database is exactly the salted network hash — not
  // the address, and not anything containing it.
  const expectedKey = waitlistNetworkKey(
    headersOf({ 'x-vercel-forwarded-for': '203.0.113.47' }),
    { WAITLIST_NETWORK_SALT: SALT },
  )
  assert.equal(rpcCalls[0].args.p_network_key, expectedKey)
  assert.match(rpcCalls[0].args.p_network_key, /^[0-9a-f]{64}$/)
  assert.equal(JSON.stringify(rpcCalls).includes('203.0.113'), false)

  // A neighbouring host of the same /24 pays the same bucket...
  await post({ 'x-vercel-forwarded-for': '203.0.113.250' }, {
    consume_waitlist_network_budget: { data: { allowed: true }, error: null },
    submit_waitlist_signup: accepted,
  })
  assert.equal(rpcCalls[0].args.p_network_key, expectedKey)

  // ...and a denial is a 429 with the bucket's own Retry-After, with no write.
  const denied = await post({ 'x-vercel-forwarded-for': '203.0.113.47' }, {
    consume_waitlist_network_budget: { data: { allowed: false, retry_after_seconds: 120 }, error: null },
  })
  assert.equal(denied.status, 429)
  assert.equal(denied.headers.get('retry-after'), '120')
  assert.deepEqual(rpcCalls.map(call => call.name), ['consume_waitlist_network_budget'])

  // With no salt the signup still works — the bucket is skipped, not enforced
  // against an empty key, and never 500s.
  delete process.env.WAITLIST_NETWORK_SALT
  const unsalted = await post({ 'x-vercel-forwarded-for': '203.0.113.47' }, {
    submit_waitlist_signup: accepted,
  })
  assert.equal(unsalted.status, 200)
  assert.deepEqual(rpcCalls.map(call => call.name), ['submit_waitlist_signup'])
})
