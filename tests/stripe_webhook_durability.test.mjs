import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  processWebhookInbox,
  WebhookLeaseLostError,
} from '../src/lib/billing/webhook-inbox.mjs'
import { StripeDuplicateSubscriptionReviewError } from '../src/lib/billing/subscription-policy.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

function fakeInbox() {
  let now = 0
  let row = null
  const calls = { complete: [], fail: [], renew: [] }
  return {
    advance(seconds) { now += seconds },
    row() { return row && { ...row } },
    calls,
    claim(owner) {
      if (!row) {
        row = { status: 'processing', owner, expires: now + 60, attempts: 1 }
        return 'claimed'
      }
      if (row.status === 'processed') return 'processed'
      if (row.expires > now) return 'busy'
      row = { ...row, owner, expires: now + 60, attempts: row.attempts + 1 }
      return 'claimed'
    },
    complete(owner) {
      calls.complete.push(owner)
      if (row?.status !== 'processing' || row.owner !== owner || row.expires <= now) return false
      row = { ...row, status: 'processed', owner: null, expires: null }
      return true
    },
    fail(owner, error) {
      calls.fail.push(owner)
      if (row?.status !== 'processing' || row.owner !== owner || row.expires <= now) return false
      row = { ...row, owner: null, expires: now, lastError: error?.name || 'unknown' }
      return true
    },
    renew(owner) {
      calls.renew.push(owner)
      if (row?.status !== 'processing' || row.owner !== owner || row.expires <= now) return false
      row = { ...row, expires: now + 60 }
      return true
    },
  }
}

function manualHeartbeat() {
  const scheduled = []
  return {
    schedule(callback) {
      const entry = { active: true, callback }
      scheduled.push(entry)
      return () => { entry.active = false }
    },
    async tick() {
      let entry
      while ((entry = scheduled.shift()) && !entry.active) {}
      assert.ok(entry, 'expected a scheduled webhook heartbeat')
      entry.callback()
      await new Promise(resolve => setImmediate(resolve))
    },
  }
}

function run(store, owner, apply, overrides = {}) {
  return processWebhookInbox({
    claim: async () => store.claim(owner),
    renew: async () => store.renew(owner),
    apply,
    complete: async () => store.complete(owner),
    fail: async error => store.fail(owner, error),
    heartbeatIntervalMs: 20_000,
    ...overrides,
  })
}

test('claim-before-apply failure is retried, then completed duplicates are inert', async () => {
  const store = fakeInbox()
  let applications = 0

  await assert.rejects(
    run(store, 'worker-1', async () => {
      applications += 1
      throw new Error('simulated crash before billing state applied')
    }),
    /simulated crash/,
  )
  assert.equal(store.row().status, 'processing')
  assert.equal(store.row().expires, 0)

  assert.deepEqual(await run(store, 'worker-2', async () => { applications += 1 }), {
    kind: 'processed',
  })
  assert.equal(store.row().attempts, 2)
  assert.equal(applications, 2)

  assert.deepEqual(await run(store, 'worker-3', async () => { applications += 1 }), {
    kind: 'duplicate',
  })
  assert.equal(applications, 2, 'a completed duplicate must not reapply business state')
})

test('cleanup failure remains retryable after lease expiry and is never acknowledged busy', async () => {
  const store = fakeInbox()
  let cleanupErrors = 0
  await assert.rejects(
    run(
      store,
      'crashed-worker',
      async () => { throw new Error('business mutation failed') },
      {
        fail: async () => { throw new Error('database unavailable during cleanup') },
        reportCleanupError: () => { cleanupErrors += 1 },
      },
    ),
    /business mutation failed/,
  )
  assert.equal(cleanupErrors, 1)
  assert.deepEqual(await run(store, 'early-retry', async () => assert.fail('busy claim applied')), {
    kind: 'busy',
  })

  store.advance(61)
  assert.deepEqual(await run(store, 'recovery-worker', async () => {}), { kind: 'processed' })
  assert.equal(store.row().attempts, 2)
})

test('concurrent delivery stays busy until the owning delivery completes', async () => {
  const store = fakeInbox()
  let releaseApply
  const applyGate = new Promise(resolve => { releaseApply = resolve })
  const first = run(store, 'worker-1', async () => applyGate)

  // Let the first invocation acquire its durable claim before racing the next.
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(await run(store, 'worker-2', async () => assert.fail('concurrent apply')), {
    kind: 'busy',
  })
  releaseApply()
  assert.deepEqual(await first, { kind: 'processed' })
})

test('slow application renews its live lease before the fixed claim window expires', async () => {
  const store = fakeInbox()
  const heartbeat = manualHeartbeat()
  let releaseApply
  const applyGate = new Promise(resolve => { releaseApply = resolve })
  const first = run(store, 'slow-worker', async () => applyGate, {
    scheduleHeartbeat: heartbeat.schedule,
  })

  await new Promise(resolve => setImmediate(resolve))
  await heartbeat.tick()
  store.advance(50)
  await heartbeat.tick()
  store.advance(20)
  assert.equal(store.claim('overlap-worker'), 'busy', 'renewal must outlive the original 60s claim')

  releaseApply()
  assert.deepEqual(await first, { kind: 'processed' })
  assert.ok(store.calls.renew.length >= 3, 'heartbeat plus final acknowledgement fence must renew')
})

test('lost ownership aborts the runtime fence before another database business write', async () => {
  const store = fakeInbox()
  const heartbeat = manualHeartbeat()
  let releaseApply
  const applyGate = new Promise(resolve => { releaseApply = resolve })
  let businessWrites = 0
  const first = run(store, 'stale-worker', async lease => {
    await applyGate
    await lease.fence()
    businessWrites += 1
  }, {
    scheduleHeartbeat: heartbeat.schedule,
  })

  await new Promise(resolve => setImmediate(resolve))
  store.advance(61)
  assert.equal(store.claim('takeover-worker'), 'claimed')
  await heartbeat.tick()
  releaseApply()

  await assert.rejects(first, error => error instanceof WebhookLeaseLostError)
  assert.equal(businessWrites, 0)
  assert.equal(store.row().owner, 'takeover-worker')
  assert.equal(store.row().status, 'processing')
  assert.deepEqual(store.calls.complete, [])
  assert.deepEqual(store.calls.fail, ['stale-worker'])
})

test('renewal exception fails closed without acknowledgement or a later business write', async () => {
  const store = fakeInbox()
  const heartbeat = manualHeartbeat()
  let releaseApply
  const applyGate = new Promise(resolve => { releaseApply = resolve })
  let businessWrites = 0
  const processing = run(store, 'exception-worker', async lease => {
    await applyGate
    await lease.fence()
    businessWrites += 1
  }, {
    renew: async () => { throw new Error('renewal database unavailable') },
    scheduleHeartbeat: heartbeat.schedule,
  })

  await new Promise(resolve => setImmediate(resolve))
  await heartbeat.tick()
  releaseApply()

  await assert.rejects(
    processing,
    error => error instanceof WebhookLeaseLostError &&
      error.cause?.message === 'renewal database unavailable',
  )
  assert.equal(businessWrites, 0)
  assert.deepEqual(store.calls.complete, [])
  assert.deepEqual(store.calls.fail, ['exception-worker'])
  assert.equal(store.row().expires, 0, 'owner-scoped failure cleanup keeps the event retryable')
})

test('a delivery reclaimed after the final renewal rejects stale completion and failure cleanup', async () => {
  const store = fakeInbox()
  const processing = run(store, 'old-worker', async () => {}, {
    complete: async () => {
      store.advance(61)
      assert.equal(store.claim('new-worker'), 'claimed')
      return store.complete('old-worker')
    },
  })

  await assert.rejects(processing, error => error instanceof WebhookLeaseLostError)
  assert.equal(store.row().owner, 'new-worker')
  assert.equal(store.row().status, 'processing')
  assert.deepEqual(store.calls.complete, ['old-worker'])
  assert.deepEqual(store.calls.fail, ['old-worker'])
})

test('duplicate subscriptions remain retryable durable manual review work', async () => {
  const store = fakeInbox()
  await assert.rejects(
    run(store, 'review-worker', async () => {
      throw new StripeDuplicateSubscriptionReviewError()
    }),
    error => error instanceof StripeDuplicateSubscriptionReviewError,
  )

  assert.equal(store.row().status, 'processing')
  assert.equal(store.row().lastError, 'StripeDuplicateSubscriptionReviewError')
  assert.equal(store.row().expires, 0)
  assert.deepEqual(await run(store, 'post-review-worker', async () => {}), {
    kind: 'processed',
  })
})

test('route and migration enforce non-2xx busy handling and owner-scoped completion', () => {
  const route = read('src/app/api/billing/webhook/route.ts')
  const migration = read('supabase/migrations/202607200001_stripe_webhook_durability.sql')
  const renewalMigration = read(
    'supabase/migrations/202607200012_stripe_webhook_lease_renewal.sql',
  )

  assert.match(route, /result\.kind === 'busy'[\s\S]+status: 503/)
  assert.match(route, /Retry-After': '5'/)
  assert.match(route, /mark_stripe_webhook_event_processed/)
  assert.match(route, /renew_stripe_webhook_event_lease/)
  assert.match(route, /heartbeatIntervalMs: WEBHOOK_HEARTBEAT_INTERVAL_MS/)
  assert.equal((route.match(/await lease\.fence\(\)/g) || []).length, 2)
  assert.match(route, /StripeDuplicateSubscriptionReviewError/)
  assert.match(route, /duplicate Stripe subscription requires manual review/)
  assert.doesNotMatch(route, /subscriptions\.cancel\(|brevitas-webhook-cancel/)
  assert.doesNotMatch(route, /stripe_webhook_events'\)\.delete|releaseEvent/)
  assert.match(migration, /status = 'processing'[\s\S]+lease_owner = p_lease_owner/)
  assert.match(migration, /current_event\.lease_expires_at[\s\S]+return 'busy'/)
  assert.match(migration, /status = 'processed'[\s\S]+processed_at = clock_timestamp\(\)/)
  assert.match(migration, /Simulate a process death after claim and before business state was applied/)
  assert.match(renewalMigration, /lease_owner = p_lease_owner[\s\S]+lease_expires_at > renewal_time/)
  assert.match(renewalMigration, /lease_owner = p_lease_owner[\s\S]+lease_expires_at > completion_time/)
  assert.match(renewalMigration, /stale webhook owner failed a reclaimed event/)
  assert.match(renewalMigration, /expired webhook lease was resurrected/)
  assert.match(
    renewalMigration,
    /renew_stripe_webhook_event_lease\([\s\S]+return public\.compare_and_set_stripe_subscription_snapshot\(/,
  )
  assert.match(
    renewalMigration,
    /renew_stripe_webhook_event_lease\([\s\S]+return public\.compare_and_set_stripe_invoice_snapshot\(/,
  )
})
