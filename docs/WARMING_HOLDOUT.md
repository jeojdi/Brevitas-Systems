# Cache-warming control arm (holdout)

Phase 0, item 4 of `docs/RL_PREDICTIVE_WARMING_PLAN.md`. Instrumentation only,
**off by default**.

## Why it exists

Every warming number we can compute today is an attribution estimate. When a
customer returns inside the TTL window and gets a cache read, the receipt looks
identical whether our ping kept the entry alive or the customer would have
returned anyway. The organic-return share is unobservable from warmed traffic,
so "incremental savings" can be asserted but not measured — and that is the
number the fee is computed from.

Withholding a random share of the pings that were about to happen fixes this
without any attribution model: the difference in what the withheld prefixes cost
is the causal effect of warming, whatever the organic rate turns out to be.

## The unit is (organization, prefix, UTC day)

Not per customer. Provider prompt caches are scoped to the API credential — that
is, to the organization — so a prefix held out for one customer is kept warm for
free by any sibling customer that shares it. A per-customer holdout measures
near-zero lift even where warming works perfectly.

Assignment is a pure function of the key:

```
bucket = uint32(sha256(lower(org_id) || lower(prefix_hash) || 'YYYY-MM-DD')[0:4])
held_out ⟺ bucket < fraction × 2³²
```

No state is stored and nothing has to be erased — the inputs are an org id and a
hash the row already carries. It is stable within a day across ticks, replicas
and restarts, and redrawn each day so assignment stays independent of how a
prefix aged. The formula lives in exactly two places, which must stay identical:
`public.warm_due_claim` (migration `202608100001_warm_holdout_arm.sql`) and
`api/store.py:warm_holdout_bucket` / `warm_is_held_out`.

## Where the coin is flipped

Last thing before the reservation, after the ROI floor, the per-customer daily
ping cap and the daily budget have all passed. The row was therefore certain to
be pinged, so the two arms differ by nothing but the draw. Randomizing any
earlier would fill the control arm with rows that were never going to be warmed.

A held-out row:

- reserves nothing and spends nothing — the ledger is not touched;
- consumes no per-customer ping cap and takes no claim token;
- advances `next_due_at` by the same TTL horizon `warm_ping_settle` would have
  applied, so the recorded counterfactual is "this ping did not happen", not
  "this ping was rescheduled";
- leaves `warm_pings`, `warm_misses` and `consecutive_misses` alone.
  `consecutive_misses` is the stop-loss's evidence that pings are failing to
  convert; a day with no ping is no evidence either way, and incrementing it
  would retire control prefixes for a ping nobody sent.

It writes one `warm_decision_log` row with `decision = 'holdout'` and
`propensity = fraction`. Claimed rows in the same tick record
`propensity = 1 − fraction`, so an IPS estimator has both arms' action
probabilities without inferring either. While the arm is off, both stay null:
with no randomization there is no propensity, and `1.0` would read as one.

## Configuring it

`BREVITAS_WARM_HOLDOUT_PCT` — a **percent**, read by the warming worker.
Default `0`, which disables the arm entirely: no digest is computed, no branch
is taken, and the claim loop behaves exactly as it did before. Anything
unparseable, negative, infinite or NaN also reads as `0`.

The plan caps the disclosed share at ~5%.

```
BREVITAS_WARM_HOLDOUT_PCT=5     # 5% of (org, prefix, day) units held out
```

## Disclosure

`GET /v1/warming` reports `holdout_fraction` (a fraction in `[0, 1]`) to every
member of the org, spend-redacted views included. It is not a spend field: an
org is entitled to know what share of its prefixes Brevitas deliberately does
not warm, and that disclosure is also what makes the fee defensible.

One caveat to know when reading it: the fraction is process configuration, not
stored state. `warm_status` reports the value in the environment of the API
server answering the request, while the value actually applied is the warming
worker's. If the two deployments disagree, the authoritative record of what was
really held out is `warm_decision_log` — `decision = 'holdout'` rows, each
stamped with the fraction that was in force when the draw was made.

## Known residuals

- **Shared budget and cap.** A held-out row does not consume the daily budget or
  the per-customer ping cap, so the org's *other* prefixes can receive slightly
  more pings than they would have in a full-treatment world. Charging a cap for
  a ping we did not send would be worse (it suppresses real pings). At a ~5%
  share the leak is second-order, but it means the estimate is of the effect
  under the realized budget policy, not under an unconstrained one.
- **The arms share a stop-loss.** A prefix retired by the stop-loss leaves
  candidacy for both arms. Because assignment happens *after* eligibility, on
  any given day the arms are still exchangeable; the censoring is common to the
  unit, not differential within a day.
