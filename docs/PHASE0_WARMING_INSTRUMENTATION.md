# Phase 0 warming instrumentation — build report

Status as of **2026-08-09**. Phase 0 of `docs/RL_PREDICTIVE_WARMING_PLAN.md` §4.2:
make the warming loop *observable* before anything is allowed to make it
*smarter*. One money-leak fix, four instrumentation components, one evaluator.

**The rule the whole phase runs under:** nothing here changes what the warming
loop does by default. Every component is either pure logging, or gated behind an
env flag that is OFF unless you set it. Warm spend stays never-billable
(`usage_log` rows with `strategy="cache_warm"` keep `baseline == actual`,
`measured_savings = 0`, `verified_savings_usd` forced to 0 —
`api/server.py:4961-4964`, untouched). The reserve-then-settle budget ledger,
its advisory-lock claims and its claim-token fencing are untouched.

Migrations added (in apply order):

| Migration | What it adds |
|---|---|
| `202608080001_warm_spent_unknown_settle.sql` | the `spent_unknown` settle outcome (money-leak fix) |
| `202608090001_warm_instrumentation_tables.sql` | `warm_decision_log`, `warm_ttl_observations` + writers |
| `202608090002_warm_reward_join.sql` | `usage_log.warm_prefix_hash`, the reward-join producer, `organic_counterfactual` |
| `202608100001_warm_holdout_arm.sql` | the (org, prefix, day) control arm inside `warm_due_claim` |

Every one of them wires its new tables and columns into
`compliance_delete_tenant`, `compliance_delete_subject`,
`compliance_export_tenant`, `compliance_export_subject` and
`compliance_run_retention` **in the same migration** — the 202607280016 lesson.
Compliance functions enumerate tables by hand and fail *silently* on ones they
don't know about, so a new table that isn't wired in on day one is a table that
quietly survives an erasure request.

Every RPC change is mirrored in the SQLite dev path in `api/store.py` in the same
change. Postgres/SQLite drift is a known past incident class here.

---

## 1. `spent_unknown` — the money leak, fixed first

**Purpose.** A warm ping that the provider *may have accepted* must never settle
as $0 spend. `warm_ping_settle` books ledger spend only on the `warmed` outcome;
every other outcome released the reservation and booked nothing. That is correct
exactly when the provider cannot have charged — but `api/worker.py:643-651` sent
*all* httpx transport failures down that path, including read timeouts, resets
and protocol errors that only happen **after** the POST body was written, i.e.
after the provider may already have accepted, cached and billed the ping.

**Why it ships before anything else.** Understated warm spend is not cosmetic.
The settlement fee is `25% × max(verified_savings − warm_spend, 0)`, and
`billing_period_settlement_evidence.warm_spend_usd` reads this ledger. Every
unbooked dollar of warm spend raises the fee ceiling and **overcharges the
organization**. Any policy that raises ping volume amplifies it, so it is fixed
before Phase 1 can raise ping volume.

**Behavior.** The new `spent_unknown` outcome releases the reservation and books
the **full reservation** as spent, ignoring any caller-supplied price. There is
no receipt to price the ping, and the reservation is `warm_due_claim`'s
observer-priced upper bound that `daily_budget_usd` already admitted — the only
defensible number, and deriving it inside the function means a buggy caller
cannot book less than it reserved. Conservative by construction: warm spend may
be overstated, never understated. Overstating warm spend only ever lowers *our*
fee.

**Env flags.** None. This is a correctness fix, on unconditionally.

**How to verify.** `python3 -m pytest tests/test_cache_warming_worker.py -x -q`
(ambiguous-transport-failure cases) and
`tests/test_warm_store.py` for the settle arm. In SQL,
`scripts/ci/migration-cache-warming-assertions.sql` asserts the outcome exists
and books the reservation.

---

## 2. `warm_decision_log` — the counterfactual

**Purpose.** `warm_due_claim` scores every due prefix and then `continue`s past
the ones it won't warm — below the ROI floor, over the per-customer daily ping
cap, over the daily budget. Only claimed rows were ever visible again, so every
*denial* — precisely the rows a learned policy has to reason about — was
unrecoverable. `public.warm_decision_log` records one row per candidate the claim
loop actually evaluates, claimed or denied, with the belief snapshot that
produced the verdict: `p_return`, ROI floor, reserve, prefix tokens, EWMA
inter-arrival, arrival count, pings-today, RNG seed, propensity, and later the
settle outcome and realized net.

**Scope, precisely.** Rows the candidate query filters out (stopped, stop-loss,
cold credential) and rows past `exit when v_claimed >= p_claim_limit` are never
*evaluated*, and so are not logged. The log means "what the scorer decided", not
"what existed". That distinction matters when you read it as training data.

**Why the seed is in there.** Knapsack-coupled Thompson sampling makes per-arm
propensities non-computable arm-locally — a naively logged propensity would be
fiction and would bias any off-policy evaluation gate. Logging the RNG seed plus
the candidate set plus the posterior snapshot lets propensities be re-estimated
by Monte Carlo replay instead of asserted.

**Privacy.** Metadata only — org id, customer id, provider, prefix *hash*, token
counts, dollars, timestamps. No prompt or response content, here or in any other
Phase 0 table. It is per-customer behavioral evidence, so it is erased with the
subject and covered by the retention sweep.

**Env flags.** None to enable (it writes whenever the claim loop runs at all,
which itself requires `BREVITAS_WARMING`). Retention horizon:
`BREVITAS_WARM_RETENTION_DAYS` (default 365, clamped 1–365).

**How to verify.** `python3 -m pytest tests/test_warm_store.py -x -q`;
`scripts/ci/migration-warm-instrumentation-assertions.sql` under the migration
harness. Live: one row per evaluated candidate per tick, with `decision` in
`pinged` / below-floor / cap-denied / budget-denied / `holdout`.

---

## 3. `warm_ttl_observations` — the TTL sensor

**Purpose.** Every warming schedule today guesses provider TTL with a hard-coded
floor, while two free sensors already exist in the data and get thrown away: a
warm ping's own receipt says whether the entry was still alive (cache-read
tokens) or had to be rewritten (cache-creation tokens), and so does every real
customer arrival (`warm_prefix_observe`'s `p_cache_read`). Paired with the gap
since the prefix was last touched, each is a **censored observation** of the
provider's real TTL. `public.warm_ttl_observations` persists both, via
`warm_ttl_observe`, bucketed by `warm_ttl_tier`.

**Why it matters.** This is the raw material for Phase 1's hazard model, and it
is also the drift detector: if a provider silently changes TTL or pricing, this
table sees it before a customer does.

**Env flags.** None. Same retention knob as above.

**How to verify.** Same assertions file; after a warming cycle, rows should
appear from both sensors (ping receipts and organic arrivals) with alive/expired
labels and the gap that produced them.

---

## 4. Reward join + `organic_counterfactual` — what a ping actually earned

**Purpose.** A warm ping writes a `usage_log` row keyed
`warm:{prefix_hash[:16]}:{cycle_ts}` (`api/worker.py:771`); the customer arrival
it was bought for writes an ordinary receipt. Nothing connected them — the
arrival row carried no prefix identity, so "what did this ping earn" was
unanswerable from the database, and `warm_decision_log.realized_net_usd` had no
producer. This adds the join key and the producer.

**Two pieces:**

- **`usage_log.warm_prefix_hash`** — nullable, the sha256 that
  `brevitas/warming.py:extract_warm_prefix` already computes for every observable
  request. It is deliberately **not** written by the receipt INSERT. That insert
  is a money path: naming a column PostgREST doesn't know is a 400 that drops the
  *whole receipt*, not one field, which on an authoritative row is lost revenue.
  The hash is stamped afterwards by `public.warm_usage_stamp_prefix`, an
  analytics-only UPDATE that fails closed to null and can never cost a receipt.
- **`warm_decision_log.organic_counterfactual`** — the honesty column. If the
  customer's own two most recent real arrivals on that prefix were closer
  together than the provider TTL, the entry was self-refreshing and the ping
  bought nothing the traffic wasn't already buying. Those rows are credited
  **nothing** and charged the full ping cost. An attribution error must
  understate what warming earns, never overstate it.

**Billing impact: none.** This is analytics over `usage_log` plus two analytics
columns of `warm_decision_log`. It cannot ping, spend, bill or settle.

**Env flags.**

| Flag | Default | Meaning |
|---|---|---|
| `BREVITAS_WARM_REWARD_JOIN` | `true` (ON) | enable the hourly join loop |
| `BREVITAS_WARM_REWARD_JOIN_INTERVAL_SECONDS` | hourly | cadence |
| `BREVITAS_WARM_REWARD_JOIN_LOOKBACK_HOURS` | — | how far back a cycle scores |
| `BREVITAS_WARM_REWARD_JOIN_LIMIT` | — | rows per cycle |

This one defaults **ON**, unlike everything else, and the reasoning is written at
`api/worker.py:878-889`: it's read-only over the money tables and writes two
analytics columns, so the "new behavior ships OFF" rule that guards the warming
loop doesn't apply — but it's still one env var from silent, because a job nobody
can stop is its own hazard. It also only runs when warming is on at all. Cycles
are independent: the RPC only ever fills a *null* `realized_net_usd`, so a missed
hour is picked up by the next one as long as the row is still inside the lookback
window.

**How to verify.** `python3 -m pytest tests/test_cache_warming_worker.py -x -q`;
`scripts/ci/migration-warm-reward-join-assertions.sql`. Live: after an hour of
warming, `warm_decision_log.realized_net_usd` should be non-null on closed pings,
and self-refreshing prefixes should carry `organic_counterfactual = 1` with zero
credit.

---

## 5. Flag-gated holdout — the only attribution-independent number

**Purpose.** Every warming number we can compute is an attribution estimate.
`warm_prefixes.warm_hits` credits a cache read that followed a ping, and the
reward join prices it — but a customer who would have returned inside the TTL
window anyway produces an identical row. The organic-return share is unobservable
from warmed traffic alone, so "incremental savings" can be asserted but not
measured. That is the number Brevitas bills on. A fixed-probability holdout is
the one estimator that needs no attribution model: withhold a random share of the
pings that were about to happen, and the difference in what those prefixes cost
*is* the causal effect.

**The unit is (org, prefix, UTC day)** — not (org, customer, prefix). Provider
prompt caches are scoped to the credential, i.e. to the organization, so a prefix
held out for one customer is kept warm for free by any sibling customer sharing
it, and a per-customer holdout would measure near-zero lift even where warming
works perfectly. Assignment is a pure hash of the key: stable within a day across
ticks, replicas and restarts, redrawn each day so the arms stay exchangeable as
prefixes age, with **no state to store and nothing to erase** (the inputs are an
org id and a hash the row already carries).

**Env flags.**

| Flag | Default | Meaning |
|---|---|---|
| `BREVITAS_WARM_HOLDOUT_PCT` | `0` (OFF) | control-arm share as a **percent** (`5` = 5%) |

Anything unparseable, negative, out of range, infinite or NaN reads as **0** —
the failure direction that keeps warming behaving exactly as it did before the
arm existed. The *applied* fraction is always the worker's; the API server only
*advertises* what its own environment says, which is why `warm_status` reports
it. What was actually held out is `warm_decision_log` rows with
`decision = 'holdout'`, with the fraction in force stamped into `propensity`.

**Do not switch this on yet.** It stays at 0 until the holdout disclosure clause
is approved and in the terms/DPA template — see
`docs/RL_PREDICTIVE_WARMING_PLAN.md` → *Holdout disclosure draft*. Full design
detail in `docs/WARMING_HOLDOUT.md`.

**How to verify.** `python3 -m pytest tests/test_warm_store.py tests/test_warming_api.py -x -q`;
`scripts/ci/migration-warm-holdout-assertions.sql`. Set the flag to 5 in a dev
environment and confirm ~5% of otherwise-claimable candidates log as `holdout`
and receive no ping, and that the Postgres and SQLite bucket functions agree on
the same key.

---

## 6. Replay simulator + hindsight oracle — the promotion currency

**Purpose.** Arrivals are exogenous to warming: whether a customer comes back
does not depend on whether we pinged. That is a stronger property than standard
off-policy evaluation gets, and it means logged traces + the TTL posterior + the
price sheet compute the **exact** net savings of any candidate policy, and the
hindsight-optimal schedule is computable directly from logs. **Fraction of
hindsight-oracle net dollars** becomes the metric that promotes a policy
(Baleen's methodology), with absolute net dollars always reported alongside and
the ratio suppressed when the oracle denominator is below a floor — it's unstable
for tiny orgs.

**Status: landed.** `scripts/warm_replay_sim.py` with a green `--selftest`
(50 asserted invariants across three synthetic customer scenarios); run
`python3 scripts/warm_replay_sim.py --selftest` to verify.

**Validation requirement, non-negotiable.** Simulator marginals must be validated
against real pre-2026-07-17 `usage_log` traffic before any promotion decision
trusts it. BurstGPT-seeded synthetics are necessary, not sufficient. There has
been zero authoritative production traffic since 2026-07-17, so the simulator is
the *only* evaluator available until traffic returns — which is exactly why it
must not be trusted on synthetics alone.

**Env flags.** Offline tool; no production flag.

---

## What remains before Phase 1

Phase 0 is instrumentation. Phase 1 is the first time the index actually changes
what gets warmed, and it needs these, per plan §4.3:

1. **Index claim-ordering** — replace FIFO claim order in `warm_due_claim` with
   the analytic index, plus a λ admission bar, inside the existing advisory-lock
   RPC. **One migration must change the Postgres RPC and the SQLite mirror
   together**, with a concurrency test. The harness history says these drift when
   changed separately, and this one sits on the claim path.
2. **Decayed hierarchical hazards** replacing the 168-bucket lifetime histograms,
   so a prefix's belief tracks recent behavior instead of averaging over its
   whole life.
3. **P(alive)** replacing the stop-loss heuristic — a real survival probability
   per (customer, prefix), which is what makes "stop warming a dead prefix" a
   decision rather than a counter.
4. **Periodicity fast-path** — the cheap detector for the strongly periodic arms
   (nightly batch jobs, business-hours traffic) that don't need the full model.

Also gating Phase 1, from the decisions section: `warm_customer_state` needs the
privacy classification sign-off before it ships, and the per-customer budget knob
(`warm_customer_budget` + dashboard UI) lands with it. The structural
anti-overspend guarantee — a ledger-enforced cap of `warm_spend ≤ β × trailing
control-verified savings` per (org, provider) — is Phase 1 work and depends on
the holdout being live, which depends on the disclosure approval.

## Test status

Targeted suites for the touched paths:
`tests/test_cache_warming.py`, `tests/test_cache_warming_worker.py`,
`tests/test_warm_store.py`, `tests/test_warming_api.py` — run with
`python3 -m pytest tests/<file> -x -q`.

SQL assertions for the new migrations:
`scripts/ci/migration-warm-instrumentation-assertions.sql`,
`scripts/ci/migration-warm-reward-join-assertions.sql`,
`scripts/ci/migration-warm-holdout-assertions.sql`, plus the existing
`migration-cache-warming-assertions.sql`, run by
`scripts/ci/run-migration-tests.sh` (needs a local postgres; on macOS use
`brew postgresql@17`).

Nothing in Phase 0 has been applied to any remote database. All migrations are
local files awaiting the normal apply path.
