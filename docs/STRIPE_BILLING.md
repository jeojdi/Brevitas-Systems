# Stripe billing setup

Brevitas uses Stripe Checkout and Stripe Billing meters for its pricing model: 25% of verified savings, billed every seven days. Card details stay on Stripe-hosted pages.

Each customer's first seven-day period begins when their Stripe subscription starts; this is an account-specific rolling week, not a Monday-to-Sunday calendar bucket. The dashboard's current estimate uses those exact Stripe boundaries. Weekly usage history is a separate Monday-UTC analytics view.

## Safety model

- The browser sends no product, price, quantity, amount, or return URL. The server selects the only allowed Stripe Price.
- A database trigger caps every ledger entry at 25% of positive verified savings and floors it to whole micro-dollars.
- Only usage created after an `active` or `trialing` subscription begins enters the append-only ledger. There is no retroactive charge.
- Each Stripe meter event has a stable ledger identifier and API idempotency key.
- An ambiguous Stripe response is reconciled against Stripe's customer/period meter aggregate. It moves to `review` unless acceptance can be proved. A stale send may be replayed only inside Stripe's identifier-deduplication window, with the same identifier and idempotency key.
- `BREVITAS_BILLING_WEEKLY_CAP_USD` is required. A transaction-locked database function prevents concurrent workers from crossing it.
- Cap totals and reconciliation use the exact seven-day Stripe-anchored period containing each ledger occurrence. Historical boundaries are derived from Stripe's validated UTC timestamps using fixed 604,800-second offsets, so results do not depend on calendar months, daylight-saving changes, or the worker's timezone. Historical usage is never counted against whichever period happens to be current when recovery runs.
- Postgres atomically claims one row per cycle with `FOR UPDATE SKIP LOCKED`. Lease owner/expiry state is authoritative across Railway replicas; Redis is not involved in billing correctness.
- Signed webhooks use Stripe's raw request body, and processed event IDs are deduplicated.
- A duplicate-subscription race is detected and the second subscription is immediately canceled without proration.
- `billing_ledger` is an immutable financial record: SQL deletion and changes to ledger identity, usage source, owner, occurrence time, amount, and creation time are blocked. State transitions remain possible and records are retained for seven years, subject to counsel.

## Continuous Railway processor

Apply `supabase/migrations/202607170004_billing_recovery.sql`, then integrate the callable processor into the continuously running Railway worker. The Vercel route is not a scheduler: `/api/billing/sync` is an authenticated manual recovery control and never calls Stripe.

The worker-runtime integration is deliberately narrow so billing can share the existing shutdown event without owning `api/worker.py`:

```python
from api.billing_recovery import (
    billing_recovery_is_configured,
    build_billing_recovery_processor_from_env,
    run_billing_recovery_loop,
)

if billing_recovery_is_configured():
    billing_processor = build_billing_recovery_processor_from_env(telemetry=otel_billing_adapter)
    billing_task = asyncio.create_task(
        run_billing_recovery_loop(
            billing_processor,
            stop,
            health_reporter=update_billing_health,
        ),
        name="billing-recovery",
    )
```

Add `billing_task` to the worker's drained tasks. `run_billing_recovery_loop` stops claiming work when `stop` is set and releases only rows whose outbound Stripe request never began. An interrupted or crashed request remains `sending` until its lease expires, then another replica reconciles it before any safe replay. Claims are one-at-a-time, and catalog validation/reconciliation renew the current row lease before external requests and every Stripe page. Completion is fenced by both row id and lease owner. The configured Price→Meter, event name, sum aggregation, and customer/value mappings are validated before the outbound marker and meter-event POST; a wrong catalog can never be marked reported.

W1 worker integration contract:

1. Construct the billing processor only after the worker's shared `stop` event exists. If billing variables are partly configured, fail readiness rather than silently disabling billing.
2. Start exactly one `run_billing_recovery_loop(processor, stop)` task per Railway worker process and include it in the same drained task set as job consumers.
3. On SIGTERM/SIGINT, set `stop` first and await the billing task. Do not close shared clients or cancel the task before the wait. The loop also defers task cancellation until its current bounded `to_thread` call returns, so a requests session is never closed under an active HTTP call.
4. Keep `BREVITAS_WORKER_DRAIN_SECONDS` at least 120 seconds and greater than the configured billing HTTP timeout plus database completion time. A stop interrupts reconciliation between pages; at most the current 30-second-bounded HTTP request must finish.
5. W1's integration test must start the task with mocked store/Stripe clients, set the shared stop event during a blocked send, assert the client remains open until the send finishes, then assert the task drains, releases only never-started claims, and closes once.

### Worker readiness snapshots

`run_billing_recovery_loop` accepts a synchronous `health_reporter(BillingLoopHealth)` callback for W1's in-process readiness state. Snapshots contain only bounded operational fields:

- `running`
- `initial_validation_succeeded`
- `catalog_valid`
- `last_success_monotonic`
- `last_error_monotonic`
- `consecutive_errors` (saturates at 1,000,000)

The loop emits `running=true` immediately, but stays fail-closed until a real, timeout-bounded Stripe `validate_contract` verifies credentials plus the configured Price/Meter contract and the Supabase billing-health RPC succeeds. It performs no claims before both checks pass. After startup, every cycle—including an idle cycle with no claim—calls the cached/bounded Stripe validator and the Supabase health RPC. The cache may avoid Stripe network I/O until expiry, but a cycle is successful only when both checks pass. Every completely successful processing/health cycle resets `consecutive_errors` and advances `last_success_monotonic`; catalog, credential, Stripe outage, store, and loop failures set catalog validity appropriately, increment errors, and advance `last_error_monotonic`. Recovery resets the counter without erasing the last error time, allowing W1 to calculate staleness. The final snapshot always has `running=false`.

Reporter exceptions are caught and cannot stop billing. Snapshots never contain credentials, identifiers, exception messages, customer data, prompts, or responses. This callback is independent of the W5 `BillingTelemetry` adapter; keep the existing metrics/traces adapter unchanged. W1 readiness should require `running`, `initial_validation_succeeded`, `catalog_valid`, a fresh `last_success_monotonic`, and zero consecutive errors.

Recommended Railway worker variables:

| Variable | Default | Bound / purpose |
| --- | ---: | --- |
| `BREVITAS_BILLING_ENABLED` | `false` | Explicit launch gate; must be `true` before checkout or recovery can run |
| `BREVITAS_BILLING_POLL_SECONDS` | `5` | 1–60 seconds; frequent processing without a Vercel cron |
| `BREVITAS_BILLING_LEASE_SECONDS` | `120` | 15–900 seconds |
| `BREVITAS_BILLING_HTTP_TIMEOUT_SECONDS` | `10` | 1–30 seconds for Stripe/Supabase |
| `BREVITAS_BILLING_RECONCILIATION_MAX_PAGES` | `20` | 1–100; exhausting the bound stays `unknown` |
| `BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER` | unset/false | Set `true` only after proving this worker is the sole writer for the event name |
| `BREVITAS_BILLING_LAG_ALERT_SECONDS` | `300` | page when the oldest pending row crosses this age |
| `BREVITAS_BILLING_REVIEW_ALERT_COUNT` | `1` | ticket threshold |
| `BREVITAS_BILLING_DEAD_ALERT_COUNT` | `1` | page threshold |
| `BREVITAS_BILLING_WORKER_ID` | `billing` | optional human-readable prefix only; hostname, PID, and fresh UUID entropy are always appended |

The processor's `BillingTelemetry` interface emits content-free counters for claimed/reported/reconciled/review/dead rows, lease loss, duration, pending age, and stale leases. Supply the shared OpenTelemetry adapter when integrating the worker. The default structured-log adapter contains no customer IDs, Stripe IDs, names, email addresses, prompts, or responses.

### Recovery state machine

| State | Meaning | Recovery |
| --- | --- | --- |
| `pending` | Never submitted to Stripe | Eligible for an atomic lease and cap check |
| `sending` | Leased or outbound outcome may be ambiguous | Reclaim after expiry; reconcile before replay |
| `reported` | Stripe accepted or aggregate reconciliation proved acceptance | Terminal |
| `review` | Acceptance cannot be proved safely | Alert and manual reconciliation |
| `dead` | Definitive permanent rejection or missing billable account | Page and manual correction |
| `capped` | Weekly safety cap stopped submission | Terminal; operator-visible |
| `expired` | Never-sent usage left Stripe's reporting window | Terminal; operator-visible |

The worker never infers that an event is absent from an asynchronous Stripe aggregate. Before an exact-equality proof it validates that the Price is active USD weekly per-unit metered pricing at the configured micro-dollar rate and that the Meter is active, uses `sum`, and has the expected customer/value mappings. It follows Stripe `starting_after` pagination until `has_more=false`; repeated/invalid cursors or the configured page bound remain `unknown`.

Catalog validation is cached for at most five minutes per processor and remains lease-checked on every use. Price/Meter mismatch is a global, recoverable deployment error—not a customer-row failure. A never-started claim is released back to `pending` without consuming an attempt, and the processor publishes a failing catalog-health metric plus a page alert. If a reclaimed row already has an ambiguous outbound marker, it is fenced in `review` for reconciliation; it is never moved to `dead` solely because today's catalog is wrong. Neither path calls Stripe's meter-event endpoint. `dead` remains reserved for definitive row-specific rejection or a missing billable account.

An exact aggregate is accepted as proof only when `BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER=true`. That flag is an operational assertion that no Stripe Dashboard action, script, legacy Vercel invocation, or other service can emit `STRIPE_METER_EVENT_NAME`; the Railway billing processor must be the sole writer. Keep the flag false until staging confirms this ownership. With it false—or when extra events make the aggregate differ—reconciliation remains `unknown`, and the row is safely replayed with the same identifier inside the 23-hour window or moved to `review`.

### Manual recovery endpoint

Use `POST /api/billing/sync` only after an operator has reconciled the stable identifier and amount in Stripe. Authenticate with `BILLING_RECOVERY_SECRET` (legacy `CRON_SECRET` is accepted during migration). The body is bounded to 4 KiB:

```json
{
  "entry_id": 123,
  "resolution": "reported",
  "note": "Stripe aggregate and invoice preview confirm acceptance"
}
```

Allowed resolutions are `reported`, `dead`, and `pending`. `pending` is an explicit attestation that Stripe did not accept the prior event; it clears the prior outbound marker so the continuous worker can safely send it. The endpoint has no `GET` handler, does not scan the ledger, and does not submit usage to Stripe.

W8 administration integration contract: the bearer secret is a temporary break-glass machine credential, not a substitute for company authorization. W8 must expose a server-only `authorizeBillingRecovery(request)` that returns a verified `{ actorId, companyId, role }`; W4's route must then require `company_owner`, `company_admin`, or a dedicated `billing_admin` role. The database resolution RPC must be extended in the same release to accept the verified actor/company identifiers and append an immutable administrative audit event containing action, ledger id, prior/new state, reason, request id, and timestamp. It must never accept actor/role headers from the caller. Until that W8 integration and audit test land, keep the endpoint restricted to the recovery secret and treat it as an explicit launch risk.

## Sandbox setup

1. Apply `supabase/migrations/20260716_stripe_billing.sql`, `supabase/migrations/20260716_stripe_billing_rate_25pct.sql`, and `supabase/migrations/202607170004_billing_recovery.sql` to the Supabase project.
   Existing installations that already have Stripe billing need the rate migration and billing-recovery migration. Historical ledger entries are not repriced or deleted.
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

5. Set all billing variables in Vercel and the Railway worker. `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `SUPABASE_SERVICE_ROLE_KEY`, and `BILLING_RECOVERY_SECRET` are server-only secrets. Do not log them.
6. Keep `STRIPE_AUTOMATIC_TAX=false` until Stripe Tax registrations and product tax behavior have been reviewed. Then enable it in both sandbox and live mode.
7. Deploy, enroll a sandbox customer from Dashboard → Savings, generate priced verified usage, and run the continuous Railway worker processor. Keep the Vercel endpoint for manual recovery only.
8. Keep `BREVITAS_BILLING_ENABLED=false` until the migration checks, signed webhook, and authoritative worker readiness are all green. Set it to `true` on Vercel and the billing worker only as the final launch step.

For local webhook testing:

```bash
stripe listen --forward-to localhost:3000/api/billing/webhook
stripe trigger checkout.session.completed
```

Stripe's meter aggregation is asynchronous. Compare the Brevitas ledger's `reported` total with the upcoming Stripe invoice before the first live billing cycle.

## Go-live checklist

- Repeat `npm run billing:setup -- --live` with a live secret key; sandbox and live objects are separate.
- Use the live Price ID, webhook signing secret, and Customer Portal configuration.
- Set a conservative `BREVITAS_BILLING_WEEKLY_CAP_USD`; raising it requires an explicit deployment change.
- Confirm Stripe account branding, statement descriptor, support contact, tax settings, invoice emails, retry rules, and cancellation behavior.
- Test successful checkout, cancellation, payment failure, webhook replay, concurrent worker claims, worker crash/lease expiry, cap enforcement, and simulated Stripe timeout/reconciliation paths.
- Review every `review`, `capped`, or `expired` ledger row manually. Never change one back to `pending` without first confirming in Stripe that its identifier was not accepted.

## Migration rollback

Pause all billing workers and verify that no `dead` row needs resolution. The migration includes the exact ordered rollback commands in its final comment block. Remove the recovery functions and delete-prevention trigger, then restore the original status constraint and `claim_billing_ledger_entry` from `20260716_stripe_billing.sql`. Keep the added columns and all ledger rows until the seven-year financial-retention period expires. Rollback must never delete, reprice, or resend a ledger row.
