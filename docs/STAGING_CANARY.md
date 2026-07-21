# Approved staging API journey canary

The journey canary is a bounded, API-only mutating supplement to release smoke testing. It starts
with a pre-provisioned confirmed user; it is not a public-user onboarding, browser, CLI, worker
failover, or billing end-to-end test. Run the existing `release:staging-smoke` first; it is
read-only and checks routing, readiness, authentication failure, fixture isolation, and the
billing recovery parser. Run this canary only through the **Approved staging API journey canary**
workflow after a reviewer approves the protected `staging` environment.

The script has no URL input. It can contact only:

- `https://staging-api.brevitassystems.com`
- `https://staging.brevitassystems.com`
- `https://api.stripe.com`

It refuses forks, non-`main` refs, non-manual events, missing confirmation, production-marked
targets, non-test Stripe keys, unapproved provider/model pairs, and incomplete credentials before
making a request. Do not copy its guard variables into a local shell.

## Protected environment configuration

Configure required reviewers and these GitHub `staging` environment values:

| Name | Kind | Requirement |
| --- | --- | --- |
| `STAGING_CANARY_USER_TOKEN` | secret | Short-lived Supabase access token for a dedicated confirmed canary user that is an owner or admin of exactly the intended staging workspace. Refresh it immediately before the run. |
| `STAGING_CANARY_PROVIDER_API_KEY` | secret | Restricted BYOK credential for the selected low-cost provider. Give it a low provider-side budget and no unrelated permissions. |
| `STAGING_CANARY_PROVIDER` | variable | One of `openai`, `anthropic`, `groq`, `deepseek`, or `grok`. |
| `STAGING_CANARY_MODEL` | variable | The repository-pinned low-cost model for that provider. Arbitrary models fail closed. |
| `STAGING_CANARY_STRIPE_SECRET_KEY` | secret | Stripe `sk_test_...` key for the staging test account. Required when billing mode is `required`. |
| `STAGING_CANARY_STRIPE_WEBHOOK_SECRET` | secret | `whsec_...` secret for the staging test webhook endpoint. |
| `STAGING_BILLING_RECOVERY_SECRET` | secret | Independent recovery second factor; never a user identity. |
| `STAGING_CANARY_RECOVERY_ENTRY_ID` | variable | Optional positive ID of a deliberately seeded staging `review`/`dead` ledger entry. Used only for `resolve-test-ledger`. |

Accepted provider/model pairs are deliberately narrow: `openai/gpt-4o-mini`,
`anthropic/claude-haiku-4-5-20251001`, `groq/llama-3.1-8b-instant`,
`deepseek/deepseek-chat`, and `grok/grok-3-mini`.

## Evidence collected

One approved run performs this bounded sequence:

1. authenticates the pre-provisioned canary and selects/bootstraps its staging workspace;
2. mints and validates a dashboard-session key;
3. creates a unique one-day service account and receives its one-time key;
4. stores the BYOK credential, makes exactly one real streaming provider request, and requires the
   configured route plus a complete SSE result;
5. enforces a 64-token provider output ceiling, a 64 KiB stream ceiling, a 4,096-character result
   ceiling, one fixed short prompt, and a low-cost model allowlist;
6. verifies attributed usage and duplicate suppression with one repeated request ID;
7. submits the same compression-only durable job twice, observes a stable job ID, and waits at most
   45 polls for normal worker completion with two attempts maximum; it does not kill a worker,
   expire a lease, or exercise reclaim on another worker;
8. creates an uncompleted Stripe test-mode Checkout session, verifies it through the fixed Stripe
   API, signs an unmatched test invoice event, and observes that an immediate replay is reported as
   a duplicate; the unmatched event deliberately performs no subscription or invoice mutation;
9. proves that the billing user and recovery second factor are independently insufficient, then
   reaches parser validation with both factors;
10. optionally returns a pre-seeded staging ledger entry to `pending` and requires its immutable
    recovery audit ID; and
11. expires the Checkout session, cancels an incomplete job, revokes the service account and
    dashboard key, and proves both credentials are rejected.

The unmatched webhook customer intentionally prevents a synthetic invoice from altering a real
billing account. The immediate duplicate response does not prove crash durability. Its inbox row
and append-only administration audit evidence remain for operator inspection.
The canary reuses the workspace and customer routing label, so retries do not create unbounded
customer fixtures. Service-account rows remain as revoked audit evidence and their provider
credential is deleted by the key lifecycle trigger.

## Running the gate

From GitHub Actions, choose **Approved staging journey canary**, type exactly
`RUN MUTATING STAGING CANARY`, keep billing mode at `required`, and select:

- `parser-only` for normal release candidates; or
- `resolve-test-ledger` only after an operator has seeded and reviewed the entry identified by
  `STAGING_CANARY_RECOVERY_ENTRY_ID`.

`billing_mode=skip` is for diagnosing a known Stripe staging outage. It is reported as skipped and
must not be treated as complete billing evidence or production approval.

No live canary is run by CI, pull requests, or this repository change. The operator still must
inspect the GitHub run, Stripe test dashboard, worker/recovery telemetry, append-only audit row,
and cleanup result.

The result reports these exclusions explicitly and a passing run must not be described as proving
them:

- public `/signup`, email confirmation, or email delivery;
- browser rendering, browser interaction, or dashboard navigation;
- installation or behavior of a packaged/released CLI artifact;
- worker death, lease expiry, cross-worker reclaim, or recovery after process loss;
- matched Stripe subscription or invoice state mutation;
- billing end to end: completed Checkout, subscription activation, metered usage delivery, invoice
  creation/payment, portal behavior, and reconciliation are not exercised; or
- backup restore/PITR, production DNS, production deployment provenance, rollback execution,
  RPO/RTO, paging, or on-call response.
