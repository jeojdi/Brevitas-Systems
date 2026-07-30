import 'server-only';

import Stripe from 'stripe';
import { recoverySecretIsStrong } from '@/lib/billing/recovery-auth.mjs';
import {
  billingConfigIsComplete,
  resolveBillingPublicUrl,
} from '@/lib/billing/config-predicate.mjs';

/**
 * The ONE Stripe API version this system speaks, everywhere.
 *
 * Three surfaces talk to Stripe and each one used to choose its own version:
 * this SDK client (which silently followed whatever the pinned `stripe`
 * package's default was), the Python recovery worker (which sent no
 * `Stripe-Version` at all and therefore inherited the *account* default, a
 * value that can be changed from the Stripe dashboard without a deploy), and
 * scripts/ci/staging-canary.mjs (which hard-coded '2025-06-30.basil').
 *
 * '2026-06-24.dahlia' is not an arbitrary pick — it is the only version this
 * code is empirically known to work against, because two of its shape changes
 * are load-bearing here:
 *   - subscription period boundaries. In dahlia `current_period_start` /
 *     `current_period_end` are ABSENT from the top level of a subscription and
 *     exist only on the subscription *item*. src/lib/billing/stripe-state.mjs
 *     reads the item-level fields, so on an older (basil-era) version, where
 *     the top-level fields still exist and the item-level ones may not, every
 *     subscription webhook would throw StripeSubscriptionPeriodError and
 *     `period_tracking_valid` would never become true.
 *   - invoice→subscription linkage moved to `invoice.parent
 *     .subscription_details`, which is what the canonical-persistence path
 *     reads.
 * So this constant must not be "upgraded" casually: changing it means
 * re-validating those two shapes first (see docs/STRIPE_TEST_PLAN.md S6).
 *
 * The two other sites are pinned to this same literal and
 * tests/stripe_api_version_pin.test.mjs fails if any of the three drifts:
 *   - api/billing_recovery.py            STRIPE_API_VERSION
 *   - scripts/ci/staging-canary.mjs      STRIPE_API_VERSION
 */
export const STRIPE_API_VERSION = '2026-06-24.dahlia';

let stripeClient: Stripe | null = null;
let validatedPrice: Promise<void> | null = null;

export function billingConfig() {
  const weeklyCapUsd = Number(process.env.BREVITAS_BILLING_WEEKLY_CAP_USD || 0);
  return {
    enabled: process.env.BREVITAS_BILLING_ENABLED === 'true',
    secretKey: process.env.STRIPE_SECRET_KEY || '',
    webhookSecret: process.env.STRIPE_WEBHOOK_SECRET || '',
    recoverySecret: process.env.BILLING_RECOVERY_SECRET || '',
    priceId: process.env.STRIPE_PRICE_ID || '',
    meterEventName: process.env.STRIPE_METER_EVENT_NAME || 'brevitas_fee_microusd',
    // BREVITAS_PUBLIC_URL. Resolution lives in the injectable predicate module:
    // a deployed surface (NODE_ENV=production or any VERCEL_ENV) gets no
    // localhost default, so a missed Vercel variable fails the configuration
    // predicate instead of pointing a paying customer's Checkout success_url at
    // http://localhost:3000.
    publicUrl: resolveBillingPublicUrl(process.env),
    weeklyCapUsd,
    automaticTax: process.env.STRIPE_AUTOMATIC_TAX === 'true',
  };
}

export function billingIsConfigured(): boolean {
  const config = billingConfig();
  // The conjunction itself lives in ./config-predicate.mjs so that the whole
  // truth table — including the deployed-vs-local BREVITAS_PUBLIC_URL condition
  // and the cap boundaries — is executable under `node --test`; this module
  // imports `server-only` and cannot be loaded there.
  //
  // The recovery-secret strength condition is additionally restated here. It is
  // the one condition whose failure is completely silent (a weak secret does not
  // 401 anything: it 503s Checkout and the webhook), so this file states that
  // part of its own security contract in-place and greppably. Both checks are
  // the same call and both must hold — the conjunction can only get stricter.
  return recoverySecretIsStrong(config.recoverySecret) &&
    billingConfigIsComplete(config, process.env);
}

export function getStripe(): Stripe {
  const key = billingConfig().secretKey;
  if (!key) throw new Error('Stripe billing is not configured');
  stripeClient ??= new Stripe(key, {
    // Explicit, not inherited: without this the effective version is whatever
    // the installed `stripe` package happens to default to, so a routine
    // dependency bump would silently move every request off the validated
    // shapes described on STRIPE_API_VERSION.
    apiVersion: STRIPE_API_VERSION,
    appInfo: { name: 'Brevitas Systems', version: '1.0.0' },
  });
  return stripeClient;
}

export async function validateStripeCatalog(): Promise<void> {
  validatedPrice ??= (async () => {
    const config = billingConfig();
    const stripe = getStripe();
    const price = await stripe.prices.retrieve(config.priceId);
    const meterId = price.recurring?.meter;
    if (
      !price.active ||
      price.type !== 'recurring' ||
      price.currency !== 'usd' ||
      price.billing_scheme !== 'per_unit' ||
      price.unit_amount_decimal?.toString() !== '0.0001' ||
      price.recurring?.interval !== 'week' ||
      // interval_count is absent on a 1-week price and MUST be rejected when it
      // is anything else: a {week, interval_count: 2} price otherwise passes
      // every validator, subscriptions get 14-day anchors, and
      // billing_period_for_occurrence then refuses each fee row into 'review'
      // (202607170004_billing_recovery.sql:78-82 ->
      // 202607200006_company_billing_authorization.sql:386-389).
      (price.recurring?.interval_count ?? 1) !== 1 ||
      price.recurring?.usage_type !== 'metered' ||
      !meterId
    ) {
      throw new Error('Stripe Price does not match the Brevitas micro-dollar metered billing contract');
    }
    const meter = await stripe.billing.meters.retrieve(meterId);
    if (meter.status !== 'active' || meter.event_name !== config.meterEventName) {
      throw new Error('Stripe Price is attached to the wrong billing meter');
    }
  })();
  try {
    await validatedPrice;
  } catch (error) {
    validatedPrice = null;
    throw error;
  }
}
