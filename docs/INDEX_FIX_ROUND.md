# Index Fix Round — making the learned policy profitable (BINDING SPEC, queued behind Phase 1.5)

## STATUS: PORTED — migration `202608100010_warm_index_convergence.sql`

The converged policy is in the shipped scheduler. `scripts/warm_replay_sim.py`'s
`LearnedIndexFixedPolicy` (commit 722688a) was the binding reference for every
number; where this document and the simulator disagreed, the simulator won, because
it is the thing that converged and this document predates F5–F7.

**What landed.** F1–F4 as specified, plus the three additions the convergence run
proved necessary: **F5** the median/MAD periodicity fast-path (3-gap engagement,
2-missed-arrival falsification), **F6** the median-floored chain, and **F7** the
shared-cache-key skip. The cold-start hold is stronger than F4 describes: it holds
v1 semantics until `roi_min_arrivals`, not merely until the hazard row exists.

**Flags are unchanged and still default-off.** Every line of the delta is reachable
only when both `BREVITAS_WARM_INDEX` and `BREVITAS_WARM_HAZARD_V2` are on and the
pair is not frozen. Four new env knobs (`BREVITAS_WARM_HAZARD_PRIOR_HOURS` 2.0,
`BREVITAS_WARM_HAZARD_EXPOSURE_MAJORITY_HOURS` 48.0,
`BREVITAS_WARM_PERIOD_MAX_DISPERSION` 0.20, `BREVITAS_WARM_PERIOD_MISS_LIMIT` 1.5)
default to the converged simulator's values, so an operator who turns the pair on
and configures nothing gets the policy the benchmark measured.

**Schema.** Two columns, no new table: `warm_customer_state.recent_gaps` (F5's
evidence) and `warm_decision_log.p_eff` (below). F7 needed no storage —
`warm_prefixes.last_touch_at` is already stamped on every arrival and every warmed
ping, which is exactly the simulator's `cache_warm_until`. Three decisions join the
vocabulary: `skipped_organic`, `skipped_abandon`, `skipped_shared_warm`.

### Where the shipped scheduler differs from the simulator, and what each costs

1. **`I_max`'s read fraction is recovered, not read.** The simulator uses each
   provider's true `read_cost_fraction`; the claim recovers `f = b/(1+b)` from the
   per-provider break-even it already gates on, so the gate and the economics cannot
   drift. Exact for providers whose break-even is derived (deepseek). For
   `anthropic` the break-even is the operator's calibrated 0.11 rather than a derived
   one, so `f` is 0.0991 against a true 0.10 and the abandon horizon runs **~1.2%
   long** (3484s against 3450s). Cost: a handful of keep-alives per churned anthropic
   arm per week, bounded by one `c_belief` each — sub-cent at current volumes. It is
   the same approximation 202608100003's chain truncation already carried, in the
   same direction, and it is now stated rather than inherited silently.
2. **`spent_unknown` does not stamp the shared-key clock.** The simulator's settle
   always warms; `warm_ping_settle` advances `last_touch_at` only on `warmed`,
   because a `spent_unknown` ping may never have reached the provider. A sibling is
   therefore re-pinged rather than skipped on warmth nobody can prove. Conservative
   in the spend direction: costs at most one extra keep-alive per ambiguous settle.
3. **F7 excludes the arm's own row.** The simulator's `cache_warm_until` conflates
   own and sibling touches; it never matters there because a row's touch and its
   `next_due_at` move together, so at the moment an arm becomes due its own warmth
   has exactly the safety margin left and the comparison is an equality. Excluding
   self is equivalent on every state a writer can produce and removes the failure
   mode on states only a fixture can build. Zero dollar impact.
4. **Gated arms are logged.** The simulator counts a gated arm; the claim writes a
   `warm_decision_log` row with a null index. Strictly more information, no
   behavioural difference.
5. **The candidate window is still pre-ranked and truncated** at `claim_limit * 4`
   by the Phase-1 approximate index (`n_chain = 0`, `p_alive = 1`) before the loop
   rescores, where the simulator scores every eligible arm and then sorts. Pre-existing
   from 202608100002, not introduced here; it can only matter when more than
   `4 * claim_limit` arms are due in one tick.
6. **`p_eff` is recorded rather than inferred.** F1 makes the ROI floor's probability
   and the index's probability different numbers on purpose, which broke the Phase-1
   identity `index = p_return * p_alive / b - 1 - n_chain`. Rather than leave the
   index unreconstructible from its own log row, the effective probability is a
   column. No simulator counterpart — the simulator has no log.
7. **Numeric detail.** `warm_conv_survival` clamps its exponent at −50 (the same
   clamp every other `exp()` in this schema carries; outcome-identical to twenty
   digits), spells `log1p(x)` as `ln(1+x)`, and implements Python's half-to-**even**
   rounding for `n_chain` because Postgres's `round()` is half-away-from-zero. Measured
   cross-backend agreement on identical state: **3e-8** on every scored quantity.
   Measured Python-vs-simulator agreement: **1e-9**, the residual being (1).
8. **Sibling warmth is forgotten when a row is pruned.** The simulator remembers
   `cache_warm_until` forever; the claim derives it from live `warm_prefixes` rows.
   Prefix expiry is 7 days and the longest TTL is 4h, so the forgotten warmth is
   always long stale. Unreachable in practice.


**Trigger:** the 21-day synthetic-company benchmark (scripts/warm_replay_sim.py, `--synthetic company --report learning`) measured the shipped learned-index policy at **−$1.89 net vs v1's +$0.007** on the evaluation window. The simulator did its job; the flags stay off in production until this round lands and the benchmark flips. Diagnosis is precise (learning-test agent report, 2026-08-10); fixes below are exhaustive against it.

## The four defects and their fixes

**F1 — p_return must be conditioned on current silence (the dominant bug: 68% of pings fired at customers silent >6h).**
The index currently uses the unconditional hourly hazard ("how often does this customer arrive at this hour") instead of the survival-conditioned probability ("given they have been silent for `s` seconds, what is P(arrival within the next TTL window)"). Fix in `warm_index_components` (api/store.py) and the PG claim body: `p_ret = P(arrival in (s, s+W] | silent for s)` computed from the discrete hazard via the survival function — multiply bucket hazards along the elapsed-silence path. The hazard state already stores what's needed; only the query changes. Port identically into the simulator's `LearnedIndexPolicy` so the benchmark measures the shipped math.

**F2 — hard organic-suppression gate (the critic's original must-address, never wired into the index path; cost $1.33 on one self-refreshing customer).**
Before scoring, if the arm's EWMA inter-arrival < provider TTL, the customer is self-refreshing: skip with a new decision `skipped_organic`. This is a GATE, not a multiplier — it does not wait for control-arm data. (The learned organic_multiplier stays stubbed at 1.0 until control data exists; the gate removes the known-catastrophic region of the stub.)

**F3 — I_max abandon rule (keepalive economics, in the plan since Part 1, absent from the shipped index).**
If current silence `s` exceeds the break-even horizon `I_max = ttl_seconds × (w/(read_fraction) − 1)` — computed from the same provider constants the index already reads — sustaining is provably worse than letting the cache lapse and re-warming on the next real arrival: skip with decision `skipped_abandon`. Also fix `n_chain` semantics: expected remaining chain pings must be derived from the survival curve of the *conditional* return distribution (F1), never `E_gap − chain_age` (which decreases toward zero exactly as a session dies — inverted incentive).

**F4 — cold-start prior weight (policy inert for a customer's first week; v1 diluted after three).**
Reduce the shrinkage prior from ~8 pseudo-hours of exposure to ~2 (tunable env `BREVITAS_WARM_HAZARD_PRIOR_HOURS`, default 2.0), and cap per-bucket exposure decay so a customer's own evidence reaches majority weight within ~2 active days. Re-verify the flat-prior v1-equivalence tests still hold (they must — equivalence is defined at *flat priors*, not at prior-weight settings).

## Acceptance gates (all mandatory)

1. `--synthetic company --report learning`: **learned-index ≥ v1heuristic net dollars** on the eval window, and learned-index ≥ 0 absolute.
2. The three short scenarios (`cron`, `bursty`, `churned`): learned-index ≥ v1heuristic on each (fixes F4's inertness).
3. Silence histogram in the learning report: pings at silence > I_max ≈ 0; pings at silence > 6h ≈ 0.
4. All existing gates: full pytest, verify-migrations (checksums recomputed — the four Phase-1 migrations are still uncommitted-editable… **NOTE: after Phase 1.5 commits, edits require a new migration instead**), harness both legs, v1-equivalence suite.
5. Senior review pass on the diff.

## ⚠️ GATES 1 AND 2 ARE INSUFFICIENT — measured 2026-08-11

Both gates benchmark the learned index against `v1heuristic`, and `v1heuristic` was
itself measured against a **never-warm** floor. But the shipped engine has never been
never-warm: `engine.py:411-417` already selects `ttl="1h"` for observed session gaps in
(300s, 3600s]. So both gates are passable by a policy that loses money against what
production already delivers with zero pings — and the shipped policy does exactly that.

Adding the honest `ttl-1h-only` baseline to the simulator (commit `bf8b16e`) measures,
on the evaluation window:

| trace | v1heuristic | learned-index-fixed | **ttl-1h-only** | learned vs 1h |
|---|---|---|---|---|
| cron | 2.904 | 3.954 | **4.524** | −0.570 (−13%) |
| bursty | 1.130 | 3.146 | **8.425** | −5.279 (−63%) |
| churned | 0.864 | 0.974 | **0.970** | +0.004 (+0%) |
| company 8c/21d | 0.007 | 7.022 | **15.778** | −8.756 (−55%) |
| company100 30d | 0.056 | 148.501 | **375.845** | −227.34 (−60%) |
| company100 182d | 0.179 | 1207.83 | **2905.98** | −1698.15 (−58%) |

The lift over v1 was real. It was measured against the wrong floor.

And because production runs the tier *and* the warmer together, the decision-relevant
number is worse: **warming on top of the tier is net-negative.** company100/182d goes
$2,905.98 → $1,816.80 (−$1,089.18); 770,390 pings costing $1,503 bought $1.84 of
savings over the tier alone. Once the hour holds the entry open, a keep-alive ping buys
warmth the customer's own next arrival already had. It still wins on exactly one cohort,
`power` (dense bursty), +$8.68 of a $2,906 pot.

**Gate 1 and gate 2 must add `AND learned-index ≥ ttl-1h-only` before any warming flag
is promoted.** As written they cannot distinguish a profitable policy from a
money-losing one.

**One measurement can still move this.** 1h refresh-on-read is provider-doc-asserted,
not measured in-house (`ANTHROPIC_CACHE_MAP.md` lists the 1h exact TTL as unswept).
Under the opposite hypothesis (`--long-refresh-on-read anthropic=0`) the verdict FLIPS
on the 8-customer trace (learned $7.022 vs tier $6.634) while the tier still wins at
100 customers. That ~$0.02 probe is now the highest-value measurement outstanding.
Caveats held: `cache_blocked_until` is unmodelled so `ttl-1h-only` is optimistic, and
every trace is synthetic because there has been no billable production traffic since
2026-07-17.

**Separate finding, about the engine not the warmer:** its own `gap > 3600 ⇒ no cache
write` refusal costs $256.97 over 6 months (+8.8%). 65,157 of 65,298 refusals are that
branch — bursty customers whose gap EWMA mixes minute-scale intra-burst gaps with
hour-scale inter-burst gaps, lands above 3600s, and is then denied a write on the dense
burst that follows.

## Process

One Fable spec-check (this file is the spec; the lead validates it against the Phase 1.5-final code state), one Opus implementer (all four fixes are one coherent change to the same functions), simulator re-run, verifier, senior review. Same house rules as every round (dual store paths, compliance wiring, default-off flags unchanged).
