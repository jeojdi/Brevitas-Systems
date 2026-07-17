<wizard-report>
# PostHog post-wizard report

The wizard completed a deep integration of PostHog across the Brevitas Systems codebase. PostHog was already initialised for the dashboard and marketing pages via `public/analytics.js`; this run extended that foundation with a shared server-side PostHog helper (`src/lib/posthog-server.ts`) and nine new events across five files covering the full user journey from waitlist signup through active platform use. The wizard's additional `instrumentation-client.ts` bootstrap was removed after review to prevent double-counting and preserve the shared bootstrap's stricter privacy controls.

The reverse proxy in `next.config.ts` was updated to add the missing `/ingest/array/:path*` → `us-assets.i.posthog.com` route, ensuring autocapture asset loading works correctly through the proxy.

A follow-up verification on 2026-07-16 executed the real `public/analytics.js` bootstrap in a controlled DOM and confirmed SDK loading through `/ingest/static/array.js`, autocapture, pageviews, exception capture, input masking, URL cleanup, and secret-property removal. It also added five correlated server-side billing events. These events flush before their short-lived Next.js route handlers return and are emitted only after Stripe or billing state succeeds.

## Events instrumented

| Event name | Description | File |
|---|---|---|
| `waitlist_joined` | Fires server-side when a new contact is successfully inserted into the waitlist table. | `src/app/api/waitlist/route.ts` |
| `api_key_created` | Fires when a user successfully creates a new Brevitas API key from the dashboard. | `dashboard/src/components/ApiKeys.jsx` |
| `api_key_revoked` | Fires when a user revokes an existing Brevitas API key from the dashboard. | `dashboard/src/components/ApiKeys.jsx` |
| `playground_message_sent` | Fires each time a user sends a message in the Playground, capturing mode and turn index. | `dashboard/src/components/Playground.jsx` |
| `playground_cache_hit` | Fires when the server returns a cache hit, capturing the cache kind and similarity score. | `dashboard/src/components/Playground.jsx` |
| `playground_mode_changed` | Fires when a user switches the Playground model backend between free and bring-your-own-key modes. | `dashboard/src/components/Playground.jsx` |
| `device_connected` | Fires when a user successfully approves a bvx CLI device-auth connection. | `dashboard/src/components/DeviceConnect.jsx` |
| `password_reset_requested` | Fires when a user submits a password-reset request and the link is sent successfully. | `dashboard/src/components/Auth.jsx` |
| `password_updated` | Fires when a user successfully sets a new password via the recovery flow. | `dashboard/src/components/Auth.jsx` |
| `billing_checkout_started` | Fires when a signed-in customer successfully requests a new or reusable Stripe Checkout session. | `src/app/api/billing/checkout/route.ts` |
| `billing_portal_opened` | Fires when a signed-in customer successfully opens the Stripe billing portal. | `src/app/api/billing/portal/route.ts` |
| `billing_checkout_completed` | Fires after a completed Stripe Checkout is validated and its subscription state is persisted. | `src/app/api/billing/webhook/route.ts` |
| `billing_subscription_updated` | Fires after a Stripe subscription create, update, or delete event is persisted. | `src/app/api/billing/webhook/route.ts` |
| `billing_invoice_updated` | Fires after a paid or failed Stripe invoice outcome is persisted. | `src/app/api/billing/webhook/route.ts` |

Pre-existing events (`login_completed`, `signup_started`, `signup_submitted`, `dashboard_tab_viewed`, `account_signed_out`, `analytics_preference_changed`) were left intact and are included in the dashboard insights.

## New files created

| File | Purpose |
|---|---|
| `src/lib/posthog-server.ts` | Singleton `posthog-node` client for server-side event capture in API routes. |
| `tests/posthog_integration.test.mjs` | In-process verification of the website bootstrap, privacy controls, event plan, and server flush configuration. |

## Next steps

We've built a dashboard and five insights to monitor user behaviour as events start flowing in:

- **Dashboard**: [Analytics basics (wizard)](https://us.posthog.com/project/514471/dashboard/1856760)
- [Account signup funnel (wizard)](https://us.posthog.com/project/514471/insights/FrRnzIjl) — signup_started → signup_submitted → login_completed conversion
- [Waitlist signups over time (wizard)](https://us.posthog.com/project/514471/insights/mGZErMAy) — daily waitlist_joined count
- [Dashboard tab engagement (wizard)](https://us.posthog.com/project/514471/insights/kchoMm2Q) — dashboard_tab_viewed broken down by tab name
- [Playground usage trend (wizard)](https://us.posthog.com/project/514471/insights/7s6IX23Q) — playground_message_sent and playground_cache_hit over time
- [API key lifecycle (wizard)](https://us.posthog.com/project/514471/insights/eIghcLlU) — api_key_created vs api_key_revoked over time

## Verify before merging

- [x] Run a full production build (`npm run build`) and fix any lint or type errors introduced by the generated code.
- [x] Run the active test suites and lint checks.
- [x] Document `NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN`, the UI host, ingestion host, and asset host in `.env.example` and the deployment guide.
- [ ] Wire source-map upload (`posthog-cli sourcemap` or your bundler's upload step) into CI so production stack traces de-minify.
- [ ] Confirm the returning-visitor path also calls `identify` — the current implementation identifies on session load via `App.jsx`, which covers returning visitors correctly as long as Supabase restores the session on page refresh.
- [ ] Add the five billing events to the PostHog dashboard once PostHog project access is available; live PostHog UI verification was blocked during the follow-up run.

### Agent skill

We've left an agent skill folder in your project at `.claude/skills/integration-nextjs-app-router/`. You can use this context for further agent development when using Claude Code. This will help ensure the model provides the most up-to-date approaches for integrating PostHog.

</wizard-report>
