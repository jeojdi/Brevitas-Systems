# Adversarial review: the case that this result is falsely negative

Reproduced verbatim, as required by §10 of the brief. The reviewer was a fresh agent with no
conversation context, was not shown §0 of the brief, and was instructed only to attack the result
in one direction. It had full read access to the raw ledger, the recorded dependency graphs, the
harness source and the analysis output, and was told to verify every number against those files
rather than trust the summary it was given.

---

# ATTACK REPORT — the reuse result is falsely negative

All numbers below re-derived from `/Users/jamesyang/Documents/GitHub/Brevitas-Systems/experiments/reuse/ledger.jsonl` (snapshot at 08:40, 72 rows), the recorded graphs in `dependency_graphs/`, and `harness/perturb.py`. Verification scripts: `…/scratchpad/attack.py`, `attack2.py`, `attack3.py`.

## First: the ledger does not support the summary

Three summary claims fail against the raw file.

1. **There are ZERO cold rows.** `{'warm': 72}`. The primary metric is pre-registered as *"2% perturbation, **cold cache**, best family"* (`PREREGISTRATION.md` §3). It has never been measured. The two cold rows quoted to me existed at 08:36 and are gone at 08:40 — `matrix_cold.log` reads `all families hot; sleeping 70 min`, and `claude -p` processes are live right now. The ledger shrank 74→72 rows *between two of my reads*. Every number in the brief is a peek at a running matrix, which §4 of the pre-registration forbids by name ("No optional stopping").

2. **Arm B's cost mix is misreported.** Summary says "output 44%, cache writes 22%, cache reads 30%". Matrix truth over all 36 arm-B runs: **output 71.5%, writes 9.9%, reads 18.3%, input 0.24%**. The quoted 44/22/30 is the 3-run `pilot/noisefloor_chain.json` on a different family at a different run size (47.7/21.4/30.5). This matters directionally: output tokens are the one component provider caching cannot reduce *at all* and reuse eliminates *entirely*. The real headroom is 71.5%, not 44%.

3. **`chain 30%` is not "~0%".** Ratio-of-means +6.7%, mean-of-marginals +8.9%, per-seed {+5.9%, −21.5%, +42.3%}. This cell serves 0/12 ops in all three seeds, so it is a *measured null* — and it says the paired B-vs-C cost noise band at n=3 is **±32 pp**. That is the correct error bar to attach to every cell, and it is nowhere in the summary.

4. **`verified_cold` is a vacuous flag.** All 9 rows with `verified_cold: True` are arm C rows that executed zero model calls. `first_op_cache_read_tokens == 0` because there *was no first op*. The gate cannot distinguish a cold cache from an absent request.

Everything else in the B-vs-C table reproduces exactly.

---

## Findings, ranked by how far they move the headline

### 1. The pre-registered decision rule returns **LIVE**, and the negative verdict comes from an instrument added afterwards. (moves the headline from "dead/feature" to **+92.4%, LIVE**)

The frozen rule: `(cost_B − cost_C)/cost_B` at 2%, best family. Bands: <10% dead, 10–25% feature, **>25% live**.

Measured, warm, n=3 seeds: **wide +92.4%, chain +89.6%, dag +64.1%.** The *worst* family is 2.6× the live threshold; the best is 3.7×. Even the 30% cells — 15× the assumed change rate — return +53% to +67%.

Against this, the pre-registration was violated in four ways, each of which removes a chance for the result to be tested where it would matter:

- **Model tier.** §4 mandates: *"develop and debug on `claude-haiku-4-5`, full matrix on `claude-sonnet-5`, re-run the best family on `claude-opus-5` regardless of the verdict's direction."* Every one of the 72 rows is `claude-haiku-4-5-20251001`. The full matrix ran on the tier the protocol designates for debugging. (In fairness: the price sheet is exactly proportional — sonnet is 2.00× haiku on every line item, opus 5.00× — so tier cannot move the *percentage* through prices. It moves it through token mix, output length, and the min-cacheable-prefix floor, which is 4,096 tok on haiku vs 512 on opus. None of that was measured.)
- **Arm A never ran.** `arm_a6b1` appears in `arm_mapping.json` and in zero ledger rows.
- **Families.** §4 pre-commits "4 to 6 families … including the most favourable case identified." Three ran. See finding 5.
- **Run sizes.** §4 pre-commits "3 per family". `pilot/size_24.log` and `pilot/size_48.log` each contain one line — `2 points pending` — and produced nothing. See finding 6.

**Corrected headline: the pre-registered instrument, run as specified, has not produced a negative number anywhere. It produced +92.4%.**

### 2. The B′ metric is arithmetically incapable of favouring the tracker. (moves −481% → **+72.4%**)

In `analysis_naive.py`:

```python
dirty_c      = graph.invalidated(changed)                      # transitive closure
dirty_bprime = {op.op_id for op in graph.ops if any(a in changed for a in op.artifacts)}
...
"tracker_value_over_bprime": (cost_bprime - cost_c) / cost_bprime
```

`graph.invalidated()`'s first branch adds *every* op that reads a changed artifact. So `dirty_bprime ⊆ dirty_c` identically, costs are non-negative, therefore **`tracker_value_over_bprime ≤ 0` for every possible input.** The same holds for `saving_bprime_over_full ≥ saving_c_over_full`. "B′ saves more than C" is a theorem about the code, not a measurement. No experiment could have come out any other way.

The stale-serve column is reported *beside* the cost, never *inside* it. Price it and the sign flips. I recomputed with one charge: if B′ serves a stale **final deliverable**, the run is wrong and must be redone (`bp_eff = cost_bprime + cost_full_run`):

| family | pert | tracker adds, as shipped | tracker adds, deliverable priced |
|---|---|---|---|
| chain | 2% | −134.4% | **+84.1%** |
| chain | 10% | −69.3% | **+29.9%** |
| dag | 2% | −481.3% | **+72.4%** |
| dag | 10% | −339.7% | **+71.1%** |
| dag | 30% | −85.6% | **+59.9%** |
| wide | 2/10/30% | +0.0% | **+91.7 / +92.2 / +75.5%** |

The charge is not aggressive. **B′ serves a stale terminal op in 22 of the 24 measured non-zero-perturbation seed-cells.** Position-averaged over every possible single-artifact change: P(stale deliverable) = **92.3% chain, 100% wide, 100% dag**. B′ does not "save 95% with 12.9% exposure" — B′ ships yesterday's answer nearly every time anything changes, and its saving *is* the work it skipped. On `dag 2%`, C's entire excess over B′ is $0.01286, of which $0.00929 (72%) is the single `ledger` op — the signed findings ledger that `family4_spec.md` says *"is the product; a summary is worthless."*

**The comparison is not merely mis-priced. It is ill-posed:** it ranks two policies on cost while holding output identical, when the whole difference between them is that one produces a correct output and the other does not.

### 3. "The tracker adds nothing on wide-independent work" is an artefact of one op priced at $0. (moves +0.0% → **+76.1%**)

`wide` has 13 ops: 12 paid model leaves with `upstream: []`, and one `assemble_ledger` aggregator with `tokens: {}` — a free local Python function. `price_op` returns `0.0` for it.

So on `wide`, `dirty_c \ dirty_bprime = {ledger}`, cost difference $0.00000, "tracker adds +0.0%". The one op the tracker correctly invalidates is the one op priced at nothing. Its stale-serve exposure of 7.7% is that single op — i.e. **100% of the deliverable.**

Price the aggregator as the *same pipeline's* dag equivalent ($0.01146, 17% of a dag run) and, holding the graph identical:

- naive tracker value: +0.0% → **−290.3%** (B′ looks *even better* — the metric is broken, not the tracker)
- deliverable-priced tracker value: **+76.1%** (at one-leaf pricing, +85.7%)

And the conclusion is circular anyway. Paid ops with a paid ancestor, per family: **chain 11 of 12, dag 1 of 13, wide 0 of 12.** Two of the three families were built with essentially no paid dependency structure. Concluding "dependency tracking adds nothing" from families that contain no paid dependencies is a definition, not a result.

### 4. The perturbation sampler cannot select a root. Ever. At 2% or 10% it cannot select outside the deepest quarter. (moves chain +89.6% → +40.9%, and B′ exposure 11.1% → **49.4%**)

`harness/perturb.py::select()`, with `N_STRATA = 4`:

```python
per_stratum = target / N_STRATA
for index in range(N_STRATA):
    want = per_stratum + carry
    take = int(want); carry = want - take
```

With `target = 1`, `per_stratum = 0.25`: takes are 0, 0, 0, 1. The carry only reaches 1.0 at stratum 3. Verified over 200 seeds on every family:

| target | strata ever reachable |
|---|---|
| 1 (the 2% and 10% cells) | **[3] only** |
| 2 | [1, 3] |
| 3 | [1, 2, 3] |
| 4 (the 30% cell) | [0, 1, 2, 3] |

**Stratum 0 — `module/000.mod`, `module/001.mod`, `module/seed.mod`, the roots — is unreachable for any perturbation of ≤3 artifacts.** The observed draws confirm it: chain 2% picked module/010, module/010, module/009; dag 10% picked `file011` in all three seeds; wide 10% picked `doc/0011` in all three seeds. The "3 seeds" are one draw replicated. Effective n per perturbed cell is ≈1, so every reported standard deviation is wrong.

`search_log.md` Correction 5 diagnoses this as *"arithmetic, not a sampler defect… position uniformity is simply not measurable from a single draw."* That explains the *0.75 deviation figure*. It does not explain, and misses, the structural fact that three of four strata are unreachable by construction. The experiment therefore samples only two regimes — deepest-quarter (2%, 10%) and total wipeout (30%, which hit a root 3/3 and served 0/12) — and never the middle, which is where dependency tracking earns its keep.

Exhaustive re-computation over *all* single-artifact positions, equally weighted:

| family | C marginal, reported @2% | C marginal, position-averaged | B′ stale %ops, reported | position-averaged |
|---|---|---|---|---|
| chain | +89.6% | **+40.9%** | 11.1% | **49.4%** |
| wide | +92.4% | +91.7% | 7.7% | 7.7% |
| dag | +64.1% | **+73.0%** | 12.9% | 12.9% |

This cuts both ways and I state it plainly: **correcting the sampler lowers the chain C-vs-B headline from +89.6% to +40.9%** — still far above the 25% live band. But it simultaneously raises B′'s stale exposure on chain 4.5×, from 11.1% to 49.4% of all operations. The single number the summary uses to argue B′ is a cheap acceptable substitute is measured at the one graph position most favourable to B′ that the sampler is capable of drawing.

### 5. The steel-man family was specced at 6,810 ops, implemented at 31, with its two strongest mechanisms removed.

`family4_spec.md` records what the blind designer built. `families/` contains `chain.py`, `dag.py`, `wide.py` — the implementation is a "fixed-factor reduction" of ~220×. Three specific deletions:

- **Cross-tenant collision.** The design has 1,220 of 6,810 ops (18% of every run) over `vendor/` and a pinned advisory snapshot, depending only on third-party bytes *identical across every customer of the compliance vendor* — "the second tenant gets 300 license-classification model calls free." There is no vendor layer, no second tenant, and no multi-tenant dimension anywhere in the harness. This is the one mechanism provider caching cannot replicate at all and it was never built.
- **Early cutoff.** The design's perturbation is 40% cosmetic diffs, which re-run one op, reproduce byte-identical output, and stop dead. `search_log.md` **Correction 4 deliberately removed this**: the mutator was rewritten to "confine edits to the six graded fields and verify against the family's own parser before returning; 16/16 across three seeds now move an answer." A fix for two inert perturbations eliminated the exact class of change the steel-man's #1 mechanism exists to exploit. The one time early cutoff *did* fire, arm C's achieved/ceiling hit **1.33**.
- **Reduce depth.** Designed as a balanced binary reduce tree over 320 nodes (~9 levels). Implemented as 3 rollups + 3 reduces — all free tool ops costing $0.

The designer's own prediction — 93% reuse at 2% perturbation, at 200 runs/day/customer against a repo where "1–3% of files change" — was never tested. §5 of the brief required family 4 precisely so the author's family choice could not determine the verdict. It determined the verdict.

### 6. The run-size curve is the pre-registered answer to "runs too small", and it has two points — both trending the wrong way for the negative verdict.

| run size | ops | served | marginal |
|---|---|---|---|
| n=6 (2 seeds) | 7 | 5 (71%) | **+85.4%** |
| n=12 (3 seeds) | 13 | 11 (85%) | **+92.3%** |

Monotone increasing in both served fraction and marginal, and it stops at the two smallest sizes in the design. The mechanism is in the ledger: `max(1, round(n·0.02))` floors at 1 artifact, so realized perturbation *falls* as n grows (7.7% realized at n=12; 2.1% at n=48; 2.0% at n=2,000). Extrapolating the same graph shape: n=24 → 92% served, n=48 → 96%, n=2,000 (the steel-man's scale) → 98%. The experiment measured reuse at the scale where it is structurally weakest and never ran the two cells that would have shown the curve.

### 7. Concurrent fan-out never exercised — and serial execution is arm B's single best case.

`grep -rn "concurr|fan.out|parallel|ThreadPool|asyncio" harness/*.py` → nothing. Every run is serial (`wall_seconds` 54–103s for 12–31 ops).

This is not neutral. The brief's own external evidence states it: *"N simultaneous callers on one fresh prefix are ALL billed cache_creation and zero reads."* Serial `wide` means arm B pays one 7,138-token write at 2.00× and eleven reads at 0.10× = 3.1 price-units of prefix. Concurrent fan-out — which is how anyone actually runs 12 independent leaf extractions — makes that 12 writes = 24.0 units, a 7.7× increase, while arm C (2 ops executing) pays 4.0. Modelled on the recorded token counts:

| | arm B | arm C | marginal | $ saved/run |
|---|---|---|---|---|
| serial (as measured) | $0.06307 | $0.02518 | +60.1% | $0.038 |
| concurrent fan-out | $0.21225 | $0.03874 | **+81.7%** | **$0.174** |

**+21.6 pp on the marginal and 4.6× on absolute dollars, from running the fan-out workload the way fan-out workloads run.**

### 8. The correctness penalty charged to arm C is measured grader noise.

`pilot/noisefloor_chain.json`, three *identical fresh* chain runs: `grader_outcome_disagreement_rate: 1.0`, scores 0.200 / 0.000 / 1.000. The grader disagrees with itself 100% of the time.

The summary's grader column: chain 2% **−33.3 pp**, chain 10% **+66.7 pp**. Same instrument, adjacent cells, opposite signs, both ≤ 2/3 of a single pass/fail flip at n=3. Both are inside a self-disagreement band of ±100 pp.

Worse: at 0% perturbation arm C serves 12/12 **byte-identical stored outputs** and still scores differently from arm B (chain seed 2: B 0.417, C 0.167). Any nonzero grader delta at 0% perturbation is arm B's nondeterminism, by definition. The pre-registered correctness gate — "arm C grader pass rate within 2 pp of arm B" — is a 2 pp threshold on an instrument with a 100 pp noise floor. It cannot pass or fail informatively in either direction.

### 9. What I could *not* find (stated so the attack is honest)

- **Arm C is not over-charged.** Shadow recomputation is 2.6% of C's cost ($0.000142 vs $0.00552 on chain 2%); storage is $4.6 × 10⁻¹⁰/run. Removing both changes nothing.
- **Arm B is genuinely well-tuned.** 105,357 tokens written at the 1h tier, 0 at 5m; pooled cache-read fraction 0.9757, matching the claimed 0.976. The B-1/B-4 tuning finding is real and correctly directed.
- **Model tier cannot move the percentage through prices.** The sheet is an exact scalar: sonnet 2.00× and opus 5.00× haiku on *every* line item. Tier matters only through token mix and the 4,096 vs 512 cacheable-prefix floor — neither of which was measured, but I will not claim the number would move without evidence.
- **`analysis.py` is genuinely frozen** and its 22 tests exist. The manipulation is not in the frozen path; it is entirely in `analysis_naive.py`, which was added afterwards and is where the verdict now comes from.

---

## Single strongest argument

**The experiment's pre-registered instrument returned +64% to +92% — between 2.6× and 3.7× the threshold the authors themselves committed to as "live" — and the negative verdict is supplied instead by a post-hoc comparison whose metric is a proof, not a measurement.**

`tracker_value_over_bprime = (cost_bprime − cost_c)/cost_bprime`, where `dirty_bprime ⊆ dirty_c` holds identically by construction. This quantity **cannot be positive for any workload, any perturbation, any seed, any price sheet.** Arm B′ was introduced as a "false-positive control that can only lower the apparent value of the system," and the direction was declared honestly. But a control that can only move one way, on a statistic that can only have one sign, is not a control — it is the answer written down before the data.

And the direction that comparison hides is the whole product. B′ buys its cheapness by shipping the stale final deliverable in 22 of 24 measured seed-cells, and in 92–100% of all possible single-artifact changes when averaged over the graph positions the sampler is structurally unable to draw. Charge that at one re-run — the mildest possible price — and the tracker's value against the changed-file list goes from **−481% to +72%** on the DAG, from **−134% to +84%** on the chain, and from **+0.0% to +76%** on wide once its aggregator is priced as a model call instead of a free Python function.

## Should this be reported as negative?

**No. It should be reported as inconclusive, with a strong positive signal on everything that was actually measured.**

Concretely, this is what the ledger supports today:

- **Primary metric, as pre-registered, is unmeasured** — zero cold rows exist. What exists is warm: **+92.4% (wide) / +89.6% (chain) / +64.1% (dag)** at 2%, all above the "live" band, on a matrix that is still running and was peeked at in violation of §4.
- **Position-corrected**, spanning every graph position rather than the deepest quarter: **+91.7% wide, +73.0% dag, +40.9% chain** — still live on all three.
- **Against B′, correctly priced**: **+72% to +92%**, not −86% to −481%.
- Every unrun arm of the design points the same way: bigger runs (curve is monotone up, 85.4%→92.3%), concurrent fan-out (+21.6 pp, 4.6× absolute), the frontier tiers the protocol mandated, and the steel-man family with the cross-tenant layer that provider caching cannot touch at all.

The defensible negative claim is much narrower than the one being made, and it is worth stating because it is probably true: **the external evidence — 0.21% cold arrivals over 6.52B prompt tokens, a 0.137% ceiling on recoverable spend, OpenAI's default 24h retention, Anthropic's refresh-on-read and workspace-scoped cache — is a serious commercial case against the *market*, and it does not depend on this experiment at all.** That case should be reported on its own evidence. What should not be reported is that the harness measured the mechanism and found it worthless. It measured the mechanism, found +64% to +92%, and then reached the opposite conclusion through a statistic that could not have returned any other sign.