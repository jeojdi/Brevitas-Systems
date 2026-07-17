# Stripe billing setup

Brevitas uses Stripe Checkout and Stripe Billing meters for its pricing model: 25% of verified savings, billed monthly. Card details stay on Stripe-hosted pages.

## Safety model

- The browser sends no product, price, quantity, amount, or return URL. The server selects the only allowed Stripe Price.
- A database trigger caps every ledger entry at 25% of positive verified savings and floors it to whole micro-dollars.
- Only usage created after an `active` or `trialing` subscription begins enters the append-only ledger. There is no retroactive charge.
- Each Stripe meter event has a stable ledger identifier and API idempotency key.
- An ambiguous Stripe response moves the entry to `review` and is never retried automatically. This intentionally prefers undercharging to a duplicate charge.
- `BREVITAS_BILLING_MONTHLY_CAP_USD` is required. A transaction-locked database function prevents concurrent workers from crossing it.
- Signed webhooks use Stripe's raw request body, and processed event IDs are deduplicated.
- A duplicate-subscription race is detected and the second subscription is immediately canceled without proration.

## Sandbox setup

1. Apply `supabase/migrations/20260716_stripe_billing.sql` and `supabase/migrations/20260716_stripe_billing_rate_25pct.sql` to the Supabase project.
   Existing installations that already have Stripe billing need only the `20260716_stripe_billing_rate_25pct.sql` migration. Historical ledger entries are not repriced.
2. Use a Stripe sandbox secret key to create the meter, product, and micro-dollar metered Price:

   ```bash
   STRIPE_SECRET_KEY=sk_test_... npm run billing:setup
   ```

   Copy the printed `STRIPE_PRICE_ID` and `STRIPE_METER_EVENT_NAME` to the server environment.
3. In Stripe's sandbox Customer Portal settings, enable payment-method updates, invoice history, and subscription cancellation. Do not enable plan switching; Brevitas exposes one server-selected usage Price.
4. Create a webhook endpoint at `https://brevitassystems.com/api/billing/webhook` for only:

   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.paid`
   - `invoice.payment_failed`

5. Set all variables listed in `.env.example`. `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `SUPABASE_SERVICE_ROLE_KEY`, and `CRON_SECRET` are server-only secrets.
6. Keep `STRIPE_AUTOMATIC_TAX=false` until Stripe Tax registrations and product tax behavior have been reviewed. Then enable it in both sandbox and live mode.
7. Deploy, enroll a sandbox customer from Dashboard → Savings, generate priced verified usage, and invoke the sync endpoint through the scheduler.

For local webhook testing:

```bash
stripe listen --forward-to localhost:3000/api/billing/webhook
stripe trigger checkout.session.completed
```

Stripe's meter aggregation is asynchronous. Compare the Brevitas ledger's `reported` total with the upcoming Stripe invoice before the first live billing cycle.

## Go-live checklist

- Repeat `npm run billing:setup -- --live` with a live secret key; sandbox and live objects are separate.
- Use the live Price ID, webhook signing secret, and Customer Portal configuration.
- Set a conservative `BREVITAS_BILLING_MONTHLY_CAP_USD`; raising it requires an explicit deployment change.
- Confirm Stripe account branding, statement descriptor, support contact, tax settings, invoice emails, retry rules, and cancellation behavior.
- Test successful checkout, cancellation, payment failure, webhook replay, concurrent sync calls, cap enforcement, and a simulated ambiguous meter-event failure.
- Review every `review`, `capped`, or `expired` ledger row manually. Never change one back to `pending` without first confirming in Stripe that its identifier was not accepted.
