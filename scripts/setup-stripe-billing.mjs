#!/usr/bin/env node
import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { config } from 'dotenv'
import Stripe from 'stripe'

// Node does not auto-load .env (only Next.js does), so read it here like the other
// scripts (scripts/build-dashboard.mjs) — .env.local wins over .env.
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
for (const name of ['.env.local', '.env']) {
  const path = resolve(root, name)
  if (existsSync(path)) config({ path, quiet: true })
}

const secretKey = process.env.STRIPE_SECRET_KEY || ''
const allowLive = process.argv.includes('--live')
const eventName = process.env.STRIPE_METER_EVENT_NAME || 'brevitas_fee_microusd'
const lookupKey = 'brevitas_verified_savings_fee_weekly_v2'

if (!secretKey) {
  console.error('Set STRIPE_SECRET_KEY to a Stripe sandbox secret key first.')
  process.exit(1)
}
if (secretKey.startsWith('sk_live_') && !allowLive) {
  console.error('Refusing to change live Stripe configuration without an explicit --live flag.')
  process.exit(1)
}

const stripe = new Stripe(secretKey, { appInfo: { name: 'Brevitas Stripe setup', version: '1.0.0' } })

const meters = await stripe.billing.meters.list({ limit: 100 })
let meter = meters.data.find(item => item.event_name === eventName && item.status === 'active')
if (!meter) {
  meter = await stripe.billing.meters.create({
    display_name: 'Brevitas verified-savings fee (micro-USD)',
    event_name: eventName,
    default_aggregation: { formula: 'sum' },
    customer_mapping: { type: 'by_id', event_payload_key: 'stripe_customer_id' },
    value_settings: { event_payload_key: 'value' },
  }, { idempotencyKey: `brevitas-meter-${eventName}` })
}

const prices = await stripe.prices.list({ lookup_keys: [lookupKey], active: true, limit: 10 })
let price = prices.data[0]
if (!price) {
  const product = await stripe.products.create({
    name: 'Brevitas verified-savings billing',
    description: '25% of verified savings; no subscription or seat fee.',
    metadata: {
      brevitas_billing_model: 'verified_savings_25pct',
      brevitas_billing_interval: 'week',
    },
  }, { idempotencyKey: 'brevitas-billing-product-weekly-v2' })
  price = await stripe.prices.create({
    product: product.id,
    currency: 'usd',
    billing_scheme: 'per_unit',
    // Stripe decimal amounts are in cents: 0.0001 cent = USD 0.000001.
    unit_amount_decimal: '0.0001',
    recurring: { interval: 'week', usage_type: 'metered', meter: meter.id },
    lookup_key: lookupKey,
    tax_behavior: 'exclusive',
    nickname: '25% verified savings (micro-USD units)',
  }, { idempotencyKey: 'brevitas-billing-price-weekly-v2' })
}

// This guard is the only thing standing between a hand-edited or legacy
// lookup-key price and a catalog both runtime validators will reject
// (src/lib/billing/config.ts validateStripeCatalog, api/billing_recovery.py
// StripeRestBillingGateway.validate_contract). It must therefore assert the
// same contract they do — every field, in the same direction. `interval_count`
// is the subtle one: it defaults to 1, so a 2-week price still satisfies
// `interval === 'week'` while anchoring subscriptions to a 14-day period, which
// billing_period_for_occurrence then rejects on every single fee row.
if (
  price.active !== true ||
  price.type !== 'recurring' ||
  price.currency !== 'usd' ||
  price.billing_scheme !== 'per_unit' ||
  price.recurring?.meter !== meter.id ||
  price.recurring?.interval !== 'week' ||
  (price.recurring?.interval_count ?? 1) !== 1 ||
  price.recurring?.usage_type !== 'metered' ||
  price.unit_amount_decimal?.toString() !== '0.0001'
) {
  throw new Error('Existing Stripe lookup-key price does not match the weekly Brevitas meter contract.')
}

const productId = typeof price.product === 'string' ? price.product : price.product.id
await stripe.products.update(productId, {
  description: '25% of verified savings; no subscription or seat fee.',
  metadata: {
    brevitas_billing_model: 'verified_savings_25pct',
    brevitas_billing_interval: 'week',
  },
})
await stripe.prices.update(price.id, {
  nickname: '25% verified savings (micro-USD units)',
})

// ── Credit packs (B9): one-time top-ups for credit-based pricing ──
// Each price's metadata.brevitas_credit_micro is the credits it grants (micro-USD, 1
// unit = $0.000001). These packs grant credits 1:1 with their dollar price; adjust the
// list or the ratio to offer bonus credits.
const creditPackDollars = [10, 50, 100]
const existingProducts = await stripe.products.list({ limit: 100 })
let creditProduct = existingProducts.data.find(
  item => item.metadata?.brevitas_billing_model === 'credits',
)
if (!creditProduct) {
  creditProduct = await stripe.products.create({
    name: 'Brevitas credits',
    description: 'Prepaid credits drawn down per gateway request.',
    metadata: { brevitas_billing_model: 'credits' },
  }, { idempotencyKey: 'brevitas-credits-product' })
}
const creditPackPriceIds = []
for (const dollars of creditPackDollars) {
  const lookup = `brevitas_credit_pack_${dollars}`
  const existing = await stripe.prices.list({ lookup_keys: [lookup], active: true, limit: 1 })
  let packPrice = existing.data[0]
  if (!packPrice) {
    packPrice = await stripe.prices.create({
      product: creditProduct.id,
      currency: 'usd',
      unit_amount: dollars * 100,
      lookup_key: lookup,
      nickname: `$${dollars} credit pack`,
      tax_behavior: 'exclusive',
      metadata: { brevitas_credit_micro: String(dollars * 1_000_000) },
    }, { idempotencyKey: `brevitas-credit-pack-${dollars}` })
  }
  creditPackPriceIds.push(packPrice.id)
}

console.log(`STRIPE_METER_EVENT_NAME=${eventName}`)
console.log(`STRIPE_PRICE_ID=${price.id}`)
console.log(`STRIPE_CREDIT_PACK_PRICE_IDS=${creditPackPriceIds.join(',')}`)
console.log('Next: create a webhook for /api/billing/webhook, configure the customer portal, and set the remaining server secrets.')
