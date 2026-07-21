import 'server-only';

import Stripe from 'stripe';
import { recoverySecretIsStrong } from '@/lib/billing/recovery-auth.mjs';

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
    publicUrl: (process.env.BREVITAS_PUBLIC_URL || 'http://localhost:3000').replace(/\/$/, ''),
    weeklyCapUsd,
    automaticTax: process.env.STRIPE_AUTOMATIC_TAX === 'true',
  };
}

export function billingIsConfigured(): boolean {
  const config = billingConfig();
  let safePublicUrl = false;
  try {
    const url = new URL(config.publicUrl);
    safePublicUrl = url.protocol === 'https:' || ['localhost', '127.0.0.1'].includes(url.hostname);
  } catch {
    safePublicUrl = false;
  }
  return Boolean(
    config.enabled &&
    config.secretKey &&
    config.webhookSecret &&
    recoverySecretIsStrong(config.recoverySecret) &&
    config.priceId &&
    config.meterEventName &&
    Number.isFinite(config.weeklyCapUsd) &&
    config.weeklyCapUsd > 0 &&
    config.weeklyCapUsd <= 100_000 &&
    safePublicUrl
  );
}

export function getStripe(): Stripe {
  const key = billingConfig().secretKey;
  if (!key) throw new Error('Stripe billing is not configured');
  stripeClient ??= new Stripe(key, {
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
