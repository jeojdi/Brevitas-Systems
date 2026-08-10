# Index Fix Round — making the learned policy profitable (BINDING SPEC, queued behind Phase 1.5)

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

## Process

One Fable spec-check (this file is the spec; the lead validates it against the Phase 1.5-final code state), one Opus implementer (all four fixes are one coherent change to the same functions), simulator re-run, verifier, senior review. Same house rules as every round (dual store paths, compliance wiring, default-off flags unchanged).
