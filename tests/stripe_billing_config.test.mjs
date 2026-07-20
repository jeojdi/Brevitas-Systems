import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

const { recoveryBearerAuthorized } = await import(
  new URL('../src/lib/billing/recovery-auth.mjs', import.meta.url)
)

test('checkout accepts no client amount and uses the server price', () => {
  const route = read('src/app/api/billing/checkout/route.ts')
  const config = read('src/lib/billing/config.ts')
  assert.match(route, /line_items: \[\{ price: config\.priceId \}\]/)
  assert.match(route, /payment_method_collection: 'always'/)
  assert.match(route, /client_reference_id: user\.id/)
  assert.doesNotMatch(route, /await req\.json|unit_amount|quantity:/)
  assert.match(route, /validateStripeCatalog\(\)/)
  assert.match(config, /unit_amount_decimal\?\.toString\(\) !== '0\.0001'/)
  assert.match(config, /usage_type !== 'metered'/)
  assert.match(config, /interval !== 'week'/)
  assert.match(config, /meter\.event_name !== config\.meterEventName/)
})

test('billing ledger fails safe against rounding, duplicates, and cap races', () => {
  const migration = read('supabase/migrations/20260716_stripe_billing.sql')
  const rateMigration = read('supabase/migrations/20260716_stripe_billing_rate_25pct.sql')
  assert.match(migration, /floor\(safe_fee \* 1000000\)/)
  assert.match(migration, /verified_savings_usd[^;]+\* 0\.25/s)
  assert.match(rateMigration, /verified_savings_usd[^;]+\* 0\.25/s)
  assert.match(rateMigration, /create or replace function public\.queue_brevitas_fee/)
  assert.match(migration, /unique references public\.usage_log/)
  assert.match(migration, /pg_advisory_xact_lock/)
  assert.match(migration, /committed \+ entry\.fee_microusd > p_cap_microusd/)
})

test('Stripe setup presents the 25% verified-savings model', () => {
  const setup = read('scripts/setup-stripe-billing.mjs')
  assert.match(setup, /25% of verified savings/)
  assert.match(setup, /nickname: '25% verified savings/)
  assert.match(setup, /stripe\.products\.update/)
  assert.match(setup, /stripe\.prices\.update/)
  assert.match(setup, /interval: 'week'/)
  assert.match(setup, /weekly_v2/)
})

test('billing status uses exact half-open Stripe weekly boundaries', () => {
  const status = read('src/app/api/billing/status/route.ts')
  const config = read('src/lib/billing/config.ts')
  const worker = read('api/billing_recovery.py')
  assert.match(status, /periodEndMs - periodStartMs === 7 \* 24 \* 60 \* 60 \* 1000/)
  assert.match(status, /\.gte\('occurred_at', new Date\(periodStartMs\)/)
  assert.match(status, /\.lt\('occurred_at', new Date\(periodEndMs\)/)
  assert.match(status, /weekly_safety_cap_usd/)
  assert.match(config, /BREVITAS_BILLING_WEEKLY_CAP_USD/)
  assert.match(worker, /BREVITAS_BILLING_WEEKLY_CAP_USD/)
  assert.match(config, /BREVITAS_BILLING_ENABLED/)
  assert.match(worker, /BREVITAS_BILLING_ENABLED/)
  assert.doesNotMatch(config, /BREVITAS_BILLING_MONTHLY_CAP_USD/)
  assert.doesNotMatch(worker, /BREVITAS_BILLING_MONTHLY_CAP_USD/)
})

test('usage accounting charges 25% of verified savings', () => {
  const server = read('api/server.py')
  assert.match(server, /BREVITAS_FEE_RATE = 0\.25/)
  assert.match(server, /fee = round\(verified \* BREVITAS_FEE_RATE, 10\)/)
})

test('Vercel sync endpoint is manual recovery only', () => {
  const sync = read('src/app/api/billing/sync/route.ts')
  assert.match(sync, /manually_resolve_billing_ledger_entry/)
  assert.match(sync, /manual_only: true/)
  assert.match(sync, /recoveryBearerAuthorized/)
  assert.doesNotMatch(sync, /getStripe|meterEvents|export const GET/)
})

test('manual billing auth rejects malformed and Unicode-confusable credentials safely', () => {
  const secret = 'recovery-secret_0123456789'
  assert.equal(recoveryBearerAuthorized(`Bearer ${secret}`, secret), true)
  assert.equal(recoveryBearerAuthorized(`bearer ${secret}`, secret), true)
  for (const malformed of [
    null,
    '',
    secret,
    `Basic ${secret}`,
    'Bearer',
    `Bearer  ${secret}`,
    `Bearer ${secret} trailing`,
    `prefix Bearer ${secret}`,
    'Bearer recovery-secret_012345678🔒',
    'Bearer recovery-secret_012345678𝟡',
  ]) {
    assert.doesNotThrow(() => recoveryBearerAuthorized(malformed, secret))
    assert.equal(recoveryBearerAuthorized(malformed, secret), false)
  }
  assert.doesNotThrow(() => recoveryBearerAuthorized('Bearer é', 'aa'))
  assert.equal(recoveryBearerAuthorized('Bearer é', 'aa'), false)
})

test('billing recovery uses database leases, reconciliation, and immutable records', () => {
  const migration = read('supabase/migrations/202607170004_billing_recovery.sql')
  const worker = read('api/billing_recovery.py')
  assert.match(migration, /for update skip locked/i)
  assert.match(migration, /lease_owner/)
  assert.match(migration, /lease_expires_at/)
  assert.match(migration, /prevent_billing_ledger_delete/)
  assert.match(worker, /run_billing_recovery_loop/)
  assert.match(worker, /def reconcile/)
  assert.match(worker, /billing_processing_lag/)
  assert.match(worker, /billing_entries_require_review/)
})

test('webhooks verify raw bodies and deduplicate event ids', () => {
  const webhook = read('src/app/api/billing/webhook/route.ts')
  assert.match(webhook, /constructEvent\(await request\.text\(\), signature/)
  assert.match(webhook, /stripe_webhook_events/)
  assert.match(webhook, /error\.code === '23505'/)
  assert.match(webhook, /event\.created/)
  assert.match(read('src/lib/billing/supabase.ts'), /lte\('stripe_subscription_event_created', eventCreated\)/)
})
