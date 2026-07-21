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
- Webhook payloads trigger reconciliation but never order billing state. The handler retrieves the current Stripe subscription, follows the billing account's subscription to Stripe's authoritative `latest_invoice`, and persists snapshots through a monotonic compare-and-set revision. Event ID, type, and second-level timestamp are diagnostic only. A CAS winner re-reads Stripe before acknowledging the inbox event and retries if the resource moved; a missing deleted subscription is accepted only as a signed terminal canceled tombstone.
- Each claimed webhook has a 60-second Postgres lease and a non-overlapping 20-second runtime heartbeat. Before every canonical billing snapshot write, the handler checks renewal and then uses a lease-aware CAS wrapper that renews and locks the inbox row in the same PostgreSQL transaction as the billing-account mutation; it renews once more before acknowledgement. Renewal, completion, and failure cleanup reject expired or reclaimed ownership, so a resumed serverless invocation cannot write or acknowledge after lease loss. The abort signal is cooperative: it does not retract a Stripe request already issued, and renewal errors leave the event retryable rather than pretending the Stripe call was canceled.
- A different-ID occupying subscription never triggers automatic cancellation. Stripe cannot atomically honor a database fencing token, so automatic cancellation has an unavoidable race with portal or webhook lifecycle changes. The handler instead throws `StripeDuplicateSubscriptionReviewError`, records `duplicate Stripe subscription requires manual review` in the durable webhook inbox, and returns retryable HTTP 500. An operator must inspect Stripe and terminalize either the candidate or incumbent. The retried signed event then safely no-ops when the candidate is terminal, or promotes it through canonical CAS when the incumbent is terminal. Alert on this inbox error; never mark the event processed manually before Stripe has one unambiguous surviving subscription.
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
| `BREVITAS_BILLING_ENABLED` | `false` | Exact-value launch gate; must be `true` before any billing writer or control can run |
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

Use `POST /api/billing/sync` only after an operator has reconciled the stable identifier and amount in Stripe. Authenticate the human with a Supabase access token in `Authorization: Bearer <user-access-token>`. The server resolves that actor's active company and requires its canonical `billing:manage` permission (`company_owner` or `billing_admin`). As a separate second factor, send the dedicated `BILLING_RECOVERY_SECRET` value in `X-Billing-Recovery-Secret`; `CRON_SECRET` and a recovery secret used as the bearer identity are not accepted. Generate the secret from at least 32 random bytes (for example, `openssl rand -base64 32`). The deployment check accepts only 32–256-byte ASCII tokens with a high-diversity encoded value; weak, repeated, malformed, and Unicode-confusable values make billing configuration and the route fail closed. Never log or reuse this value. The body is bounded to 4 KiB:

```json
{
  "entry_id": 123,
  "resolution": "reported",
  "note": "Stripe aggregate and invoice preview confirm acceptance"
}
```

Allowed resolutions are `reported`, `dead`, and `pending`. `pending` is an explicit attestation that Stripe did not accept the prior event; it clears the prior outbound marker so the continuous worker can safely send it. The scoped database RPC re-resolves the actor's active membership and `billing:manage` permission in the mutation transaction, matches the ledger row to that company, and appends immutable evidence containing company, actor, database-derived role, request ID, prior/requested status, note, and committed/denied outcome. The endpoint has no `GET` handler, does not scan the ledger, and does not submit usage to Stripe.

The route never accepts actor, company, or role headers. The actor comes from Supabase token verification; active company and role come from server-only database functions. A company switch or permission change between the HTTP check and the mutation fails closed when the RPC reauthorizes. Recovery notes are retained financial evidence: include the Stripe reconciliation basis, but never include secrets, prompts, responses, or other customer content.

The route authenticates the Supabase actor and authorizes the canonical active
company before it reads or compares the recovery header. It then consumes an
atomic counter in `shared_endpoint_rate_limits`: at most five attempts per
actor/company in a fixed 15-minute window and 60 attempts globally per minute.
The counter stores only a SHA-256 actor/company identity, is shared by every
Vercel instance, and does not trust IP or forwarding headers. A denied attempt
returns `429` with a bounded `Retry-After`; an unavailable/malformed limiter or
weak recovery-secret configuration returns `503` without attempting recovery.
Successful attempts count too, so the manual control remains deliberately
low-volume.

Migration `supabase/migrations/202607200010_shared_endpoint_rate_limits.sql`
installs this limiter before
`202607200011_compliance_billing_isolation.sql`,
`202607200012_stripe_webhook_lease_renewal.sql`,
`202607200013_billing_control_rate_limits.sql`, and
`202607200014_billing_checkout_session_reservations.sql`, followed by
`202607200015_provider_outbound_ambiguity.sql`,
`202607200016_durable_onboarding.sql`, and the final
`202607200017_billing_customer_owner_fencing.sql`, in both release manifests.
Apply the complete manifest through `202607200017` before deploying
the dependent sync route. The route
fails closed with `503` when the RPC is absent.
On loopback PostgreSQL after the release migration chain, run:

```bash
DATABASE_URL=postgresql://... \
  bash scripts/ci/run-billing-recovery-shared-limit-test.sh
```

That fixture races 12 independent database sessions and requires exactly five
admissions and seven denials, then proves the global ceiling and company
partitioning. It is not authorized to run against staging or production.

### Checkout and Customer Portal admission

Checkout and Customer Portal authenticate the Supabase user and authorize the
server-owned active company before consuming the server-only
`consume_billing_control_attempt` RPC. The shared PostgreSQL counter is keyed
by a SHA-256 of the verified actor, company, and exact operation; it never reads
or trusts an IP or forwarding header. Checkout allows five admitted attempts
per actor/company in five minutes, Portal allows 30 per actor/company per
minute, and both share a global ceiling of 120 attempts per minute. Every
admitted attempt increments the fixed-window counter. A denial returns `429`
with a database-derived `Retry-After` bounded to 300 seconds. A missing,
errored, legacy, or malformed limiter returns `503` with `Cache-Control:
no-store` before Stripe, billing-account, or analytics work.

Migration `supabase/migrations/202607200013_billing_control_rate_limits.sql`
must be applied before deploying the dependent Checkout and Portal routes. On
loopback PostgreSQL after applying the release chain, run:

```bash
DATABASE_URL=postgresql://... \
  bash scripts/ci/run-billing-control-shared-limit-test.sh
```

The fixture races 12 independent sessions, requires exactly five admissions
and seven denials, and verifies company/operation partitioning, the global
ceiling, successful-attempt counting, hashed storage, and end-user denial. It
is not authorized to run against staging or production.

Checkout creation additionally requires migration
`supabase/migrations/202607200014_billing_checkout_session_reservations.sql`.
It installs one service-owned reservation row per company and four server-only
RPCs. A request claims a five-minute lease, but lease takeover retains the same
monotonic generation and therefore the same Stripe idempotency key:
`brevitas-checkout-<organization>-generation-<generation>`. The route searches
at most 100 open sessions for the exact Stripe customer before creating. An
additional, legacy, wrong-company, or wrong-generation open subscription
session, a truncated Stripe page, or multiple matches moves the reservation to
manual review; it never guesses which URL is safe.

After a crash between Stripe creation and database persistence, the next lease
owner recovers the exact company/generation session and atomically saves its ID
to both reservation and billing-account state. An expired or replaced token
cannot persist, advance, release, or return a Checkout URL. The route performs
a final live-token database CAS even when reusing an already-persisted open
session. Only the live token may advance after retrieving that exact persisted
session and observing an immutable terminal Stripe status. A generation older
than 23 hours is recovery-only; if no exact open session can be proved, it
requires operator review rather than risking reuse of an expired Stripe
idempotency key.

The loopback migration runner invokes
`scripts/ci/migration-checkout-session-reservation-assertions.sql` to prove
competing tokens, lease takeover without generation change, stale-token
exclusion, session overwrite rejection, exact-session generation advance, and
account-occupying subscription blocking. The fixture rolls back its data and
is not authorized against staging or production.

## Atomic billing-identity maintenance rollout

Migrations `202607200004` through `202607200006` change the callable billing identity from a user UUID to an organization UUID. Apply them only with the machine-enforced maintenance procedure. First deploy the target commit with `BREVITAS_BILLING_ENABLED=false` to every Next.js instance and billing worker, and quiesce the worker. The exact-value gate prevents worker initialization and makes Checkout, webhook, Customer Portal, and manual-recovery controls return retryable HTTP 503 before request parsing, authentication/rate-limit storage, Stripe/Supabase calls, event claiming, or ledger mutation. Stripe therefore retains webhook delivery responsibility during the window. The billing status endpoint is read-only and does not mutate billing state.

Run `bash scripts/ci/apply-billing-identity-migrations.sh` with `DATABASE_URL` plus:

- `BREVITAS_BILLING_ENABLED=false`
- `BREVITAS_BILLING_MIGRATION_PHASE=api-worker-quiesced`
- `BREVITAS_BILLING_MAINTENANCE_SHA` set to the full deployed maintenance commit
- `BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL` set to the deployed Vercel `https://.../api/version` endpoint
- `BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL` set, for the required private worker topology, to the exact loopback end of an authenticated tunnel such as `http://127.0.0.1:43119/version` (an already-approved public deployment would require `https://.../version`; do not expose this worker to create one)
- `BREVITAS_BILLING_MIGRATION_EXPECTED_HOST` and `BREVITAS_BILLING_MIGRATION_EXPECTED_DATABASE` set to the reviewed target

Keep the authoritative worker private. Use Railway's authenticated SSH configuration and standard OpenSSH local forwarding; do not use `railway tcp-proxy` and do not create a public worker domain.

1. From an authenticated Railway CLI session, preview the exact SSH host configuration for the reviewed worker service and environment. Replace every angle-bracket placeholder; use a temporary, maintenance-specific alias:

   ```bash
   railway ssh config \
     --service <authoritative-billing-worker-service> \
     --environment <staging-or-production> \
     --alias brevitas-billing-maintenance
   ```

   Review the generated host, user, and identity settings, then install only that generated alias using the Railway CLI's displayed SSH-config instructions. Do not reuse an unrelated SSH host entry.
2. In a dedicated terminal, bind one unused local port in the range 1024–65535 only to loopback and forward it to the worker health listener. `<worker-health-port>` is the private port serving the worker health app, not the public API port:

   ```bash
   ssh -N \
     -L 127.0.0.1:43119:127.0.0.1:<worker-health-port> \
     brevitas-billing-maintenance
   ```

3. In the migration terminal, verify the expected worker contract and export the exact loopback endpoint:

   ```bash
   curl --fail --silent --show-error --max-time 8 \
     http://127.0.0.1:43119/version
   export BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL=http://127.0.0.1:43119/version
   ```

4. Keep the SSH process open while running the guarded migration. When it finishes, stop `ssh`, remove the generated `brevitas-billing-maintenance` alias from the operator's SSH config, and confirm no listener remains on port 43119.

Plain HTTP is accepted only for an explicit `127.0.0.1:<port>/version` worker tunnel; the dashboard and any already-approved public worker endpoint require verified HTTPS on the default port. Credentials, query strings, fragments, redirects, IP-literal public URLs, arbitrary private HTTP, incorrect paths, and dashboard/worker endpoints on the same origin are rejected.

Before the first PostgreSQL connection, the command makes credential-free, read-only `GET` requests with an eight-second timeout, no redirect following, and a 4 KiB response ceiling. It requires exact `dashboard` and `worker` service identities and an exact full-SHA match from both deployed version contracts. These values remain self-reported deployment identity, not signed artifacts or cryptographic provenance; they prevent an obvious mixed-version maintenance run but do not prove what bytes are executing.

The gate rejects a mismatched DSN, unsupported connection options, missing prerequisite migrations, failed deployed-version checks, or enabled/unquiesced billing. After the version checks, it classifies the database from catalog state. A fully completed company-scoped `202607200006` postcondition is validated and all three files are skipped. A normal pre-`202607200006` or partially completed `202607200004`/`202607200005` state resumes the ordered files. A company-scoped but incomplete state is treated as schema drift and fails closed without replaying an earlier identity. Migration `202607200004` explicitly drops and recreates its same-signature reconciliation functions inside its transaction, because PostgreSQL cannot rename input parameters with `CREATE OR REPLACE`.

Each file contains its own PostgreSQL `BEGIN`/`COMMIT`, and `psql` uses `ON_ERROR_STOP`; therefore a failed file rolls back completely. An earlier completed file may remain after a later file fails, but the system remains fail-closed under maintenance. Do not claim the three-file sequence is one nested transaction. Rerun the same guarded procedure, verify that it either completes the pending sequence or reports the validated skip, keep billing disabled, deploy the new API and worker, run staging billing tests, and only then re-enable billing.

## Sandbox setup

1. Apply `supabase/migrations/20260716_stripe_billing.sql`, `supabase/migrations/20260716_stripe_billing_rate_25pct.sql`, and `supabase/migrations/202607170004_billing_recovery.sql`, use the atomic billing-identity maintenance procedure above for migrations `202607200004` through `202607200006`, then continue through `supabase/migrations/202607200017_billing_customer_owner_fencing.sql` in release-manifest order before deploying the dependent webhook, Checkout, Portal, manual recovery, onboarding, or provider request paths.
   Existing installations that already have Stripe billing still need the company-authorization and recovery-scope forward migrations. Historical ledger entries are not repriced or deleted, and the unscoped three-argument manual recovery RPC is removed.
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
