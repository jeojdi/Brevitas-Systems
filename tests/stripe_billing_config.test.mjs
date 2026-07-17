import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

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
})

test('usage accounting charges 25% of verified savings', () => {
  const server = read('api/server.py')
  assert.match(server, /BREVITAS_FEE_RATE = 0\.25/)
  assert.match(server, /fee = round\(verified \* BREVITAS_FEE_RATE, 10\)/)
})

test('meter failures require review instead of automatic retry', () => {
  const sync = read('src/app/api/billing/sync/route.ts')
  assert.match(sync, /\.eq\('status', 'pending'\)/)
  assert.match(sync, /status: 'review'/)
  assert.match(sync, /Never\s+.*auto-retry/is)
  assert.match(sync, /timingSafeEqual/)
})

test('webhooks verify raw bodies and deduplicate event ids', () => {
  const webhook = read('src/app/api/billing/webhook/route.ts')
  assert.match(webhook, /constructEvent\(await request\.text\(\), signature/)
  assert.match(webhook, /stripe_webhook_events/)
  assert.match(webhook, /error\.code === '23505'/)
  assert.match(webhook, /event\.created/)
  assert.match(read('src/lib/billing/supabase.ts'), /lte\('stripe_subscription_event_created', eventCreated\)/)
})
