#!/usr/bin/env node
import Stripe from 'stripe'

const secretKey = process.env.STRIPE_SECRET_KEY || ''
const allowLive = process.argv.includes('--live')
const eventName = process.env.STRIPE_METER_EVENT_NAME || 'brevitas_fee_microusd'
const lookupKey = 'brevitas_verified_savings_fee_v1'

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
    metadata: { brevitas_billing_model: 'verified_savings_25pct' },
  }, { idempotencyKey: 'brevitas-billing-product-v1' })
  price = await stripe.prices.create({
    product: product.id,
    currency: 'usd',
    billing_scheme: 'per_unit',
    // Stripe decimal amounts are in cents: 0.0001 cent = USD 0.000001.
    unit_amount_decimal: '0.0001',
    recurring: { interval: 'month', usage_type: 'metered', meter: meter.id },
    lookup_key: lookupKey,
    tax_behavior: 'exclusive',
    nickname: '25% verified savings (micro-USD units)',
  }, { idempotencyKey: 'brevitas-billing-price-v1' })
}

if (price.recurring?.meter !== meter.id || price.unit_amount_decimal?.toString() !== '0.0001') {
  throw new Error('Existing Stripe lookup-key price does not match the Brevitas meter or micro-USD unit price.')
}

const productId = typeof price.product === 'string' ? price.product : price.product.id
await stripe.products.update(productId, {
  description: '25% of verified savings; no subscription or seat fee.',
  metadata: { brevitas_billing_model: 'verified_savings_25pct' },
})
await stripe.prices.update(price.id, {
  nickname: '25% verified savings (micro-USD units)',
})

console.log(`STRIPE_METER_EVENT_NAME=${eventName}`)
console.log(`STRIPE_PRICE_ID=${price.id}`)
console.log('Next: create a webhook for /api/billing/webhook, configure the customer portal, and set the remaining server secrets.')
