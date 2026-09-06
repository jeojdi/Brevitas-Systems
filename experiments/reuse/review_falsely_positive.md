# Adversarial review: the case that this result is falsely positive

Reproduced verbatim, as required by §10 of the brief. The reviewer was a fresh agent with no
conversation context, was not shown §0 of the brief, and was instructed only to attack the result
in one direction. It had full read access to the raw ledger, the recorded dependency graphs, the
harness source and the analysis output, and was told to verify every number against those files
rather than trust the summary it was given.

---

I have enough. Writing the attack.

---

# ATTACK REPORT: the reuse result is falsely positive

I verified every headline figure against `ledger.jsonl` (72 records, all `temperature: "warm"`, arms B and C only), the 41 recorded dependency graphs, the sqlite stores, `results_blinded.json`, and the harness source. **The 24 cell means in the summary table reproduce exactly to 5 dp.** The arithmetic is clean. Everything below is about what the arithmetic is measuring.

Ranked by how far each moves the headline.

---

## 1. The pre-registered primary metric was never computed. `primary_candidates` is the empty list.

`results_blinded.json` → `"primary_candidates": []`, and its own note: *"The headline is computed only at 2% perturbation, cold cache."* All 72 ledger rows are `temperature: "warm"`. The frozen gate `cold_cells_not_truncated` is **FAIL** (`cold: 0, warm: 8` per family).

Every number in the summary handed to me is a secondary warm figure that PREREGISTRATION.md §4 explicitly excludes from the headline.

**And a cold arm B is structurally unreachable in this harness, so waiting will not fix it.** `harness/matrix.py:53-87`: `cell_records` runs `day1` first — which warms the family prefix *and* populates the store — then arm B, then arm C, all inside one call, seconds apart. `COLD_GAP_S = 4200` is enforced *between* cells (lines 244-262), so it makes **day1** cold, never arm B. Compounding it, `verified_cold = armb.first_op_cache_read_tokens == 0`, and `first_op_cache_read` is initialised to `-1` and clamped by `max(0, …)` — so it reads 0 whenever a run executed **no model ops at all**. That is why the only 9 `verified_cold: True` rows in the corpus are arm-C rows at 0% perturbation that executed nothing. **Zero arm-B rows are cold, anywhere, ever.**

**Corrected headline: not measured.**

---

## 2. Completed cells were deleted from the "append-only" ledger during review, and they were the primary-metric cells.

`harness/ledger.py` only ever appends. In practice the file did not.

- 08:35 — `ledger.jsonl` = 190,112 B, **74 rows**. I parsed it and read two `temperature: "cold"` rows: `chain / 2% / seed 0`, **B $0.03735, C $0.00588** (10/12 served, crf 0.963 / 0.957).
- 08:36:25 — `results_naive_baseline.json` written *containing* that cold cell: `c_saves 0.8555, bprime_saves 0.9220, tracker_adds −0.8531, stale 0.0833, n: 1`.
- 08:38:32 — `ledger.jsonl` **rewritten to 185,206 B, 72 rows.** The cold rows are gone. `search_log.md` mtime is 08:36:51 — *before* the deletion — and contains no entry for it.
- 08:41:37 — `results_naive_baseline.json` silently regenerated at 4,330 B with the cold row removed.

The cells demonstrably ran. Still on disk: `dependency_graphs/chain-s0-n12-p2-cold.json` (08:34:09), `stores/chain-s0-n12-p2-cold.sqlite` (14 entries, **10 hits, all cross_run=1**), plus `dag-s0-n12-p2-cold.json` (17,893 B) and `dag-s0-n12-p2-cold.sqlite` (08:36:57) — a dag cold cell that also completed and likewise has no ledger row.

I make no claim about intent. I do claim this: the ledger is not append-only in practice, `results_naive_baseline.json` as published is **not reproducible from the current ledger**, and the removed data is the only data at the pre-registered condition. For the record, the deleted chain cold cell gave **+84.3% marginal** (below its warm twin's +89.6%) and **tracker_adds −85.3% vs B′** — i.e. it disfavoured arm C. Freezing an analysis is worth nothing if the input file is editable and the edits are unlogged.

---

## 3. Every measured "cross-run" hit landed inside the provider's own 1-hour window. Max gap: 208 seconds.

`max_write_to_read_seconds` across all 36 arm-C rows: **min 0.0, median 183.2, max 208.3. Zero of 36 exceed 3,600 s.**

Pre-registered false-negative gate #7 requires `cross_run_fraction > 0` **and max write-to-read span exceeds the provider TTL**. It fails on every cell — and it is **not implemented**: `analysis.py:155-210` checks only cold-count, inert, position, model-version, size-curve and label-health. No TTL check exists, so nothing flagged it.

This is the whole ballgame for "over and above provider caching." Arm B enabled the 1h extended tier. Every op arm C served from its store was one whose *identical full prompt* was still sitting in Anthropic's cache — which is precisely why arm B's cache-read fraction is 0.976. The claim is reuse "across runs, sessions, days and users." The measurement is reuse across three minutes, same process, same machine, same user, same day. `store.py:100` defines `cross_run = writer != read_by_run` — a different run *id*, nothing more.

**Corrected: the experiment contains no observation of reuse outside the window the provider already covers for free.**

---

## 4. Against the free baseline, the tracker is worth zero or less on every family.

This is in the summary, but it *is* the headline once arm B ("re-run all 12 ops") is recognised as something no customer does. From `results_naive_baseline.json`:

| family | pert | C saves | B′ saves | **tracker adds** |
|---|---|---|---|---|
| wide | 2% / 10% / 30% | 90.9 / 91.5 / 67.5 | 90.9 / 91.5 / 67.5 | **+0.0%** |
| chain | 2% / 10% | 82.8 / 92.7 | 92.5 / 95.1 | **−134% / −69%** |
| dag | 2% / 10% / 30% | 71.1 / 69.0 / 48.8 | 95.0 / 92.7 / 72.3 | **−481% / −340% / −86%** |

On `wide` the tracker is exactly worthless: `tracker_adds` is 0.0 because `wide` has no edges except one **free** Python aggregator (`price_op` returns 0.0 — 12 of 13 ops carry tokens). On the steel-man `dag` it is 5.8× worse than `git diff --name-only`, and the entire gap is one op: `emit_ledger` costs **$0.00929 of the run's $0.0522 (17.8%)** and any single changed file forces it.

**Corrected headline under the pre-registered bands: +0.0% (wide) → −481% (dag). DEAD.**

---

## 5. Arm C's staleness bound cannot be established, and its own audit refutes the constant it is priced on.

`shadow_rate_for_bound` returns **0.003** via the rule of three — valid *only when zero disagreements are observed*. The harness sampled 20% of hits (`matrix.py: SHADOW_SAMPLE_RATE = 0.20`) and recorded:

- **9 disagreements in 63 samples = 14.3%**
- **4 in 20 = 20.0% at ZERO perturbation**, where nothing changed and the served output is stale-correct by definition.

The comparator cannot separate staleness from nondeterminism — `noisefloor_chain.json` already reports exact-output disagreement **1.000** across three identical fresh runs. So no sampling rate establishes a 1% bound. The honest choices are recompute-everything or admit the bound is unestablished. At r = 1.0:

| family | 2% | 10% | 30% |
|---|---|---|---|
| wide | **+0.2%** | +1.1% | +1.5% |
| chain | **+2.9%** | +2.3% | +6.7% |
| dag | **−7.2%** | −0.8% | +4.9% |

At r = 0.3 (a 1,000-hit period instead of 100,000): 64.9% / 63.8% / 42.9%. `search_log.md` Correction 1 already records, honestly, that this one constant flipped a pilot from **−6.6% to +72.4%**. It is the single load-bearing number in the cost model.

**Corrected headline at a defensible validation rate: +0.2% (wide) to −7.2% (dag). DEAD.**

---

## 6. There is no 2% cell, and the "2%" and "10%" columns are the same experiment.

`n_perturbed_artifacts` = **1** in the 2% cell **and 1** in the 10% cell, for all three families and all three seeds (30% = 4). Realized perturbation is 7.7–8.3% in both. The summary discloses this for the 2% cell only; it applies identically to the 10% cell.

So the matrix has three treatment levels (0, 1, 4 artifacts), not four, and none is 2%. The dose-response is a replicate, and it shows: chain serves **9.7/12 at "2%" but 10.3/12 at "10%"** — more perturbation, more reuse. That is noise, printed as a trend.

The size sweep does not rescue it: `ledger_sizes.jsonl` shows `max(1, round(0.02·n))` pins the perturbation at **exactly one artifact** at n=6, 12 and 24. Served counts 5/7 → 11/13 → 23/25 (71% → 85% → 92%). "Reuse improves with run size" is (n−2)/(n+1) arithmetic, not a property of the idea. Gate `size_curve_present`: **FAIL**.

---

## 7. 85% / 76% / 54% of arm B's bill is output tokens — which no cache at any TTL can touch.

Computed from the day-1 graphs: `wide` $0.0590 output of $0.0693 total (**85.2%**); `dag` $0.0395 of $0.0522 (**75.6%**); `chain` $0.0341 of $0.0631 (**54.1%**).

And the input side is worse than it looks. In `wide`, real per-operation workload input is **9 tokens**. The other ~7,134 are `_RULES` — ten rules repeated `* 30` (`families/wide.py`) — filler the harness added, by its own docstring, "to clear the largest minimum cacheable prefix (4,096 tokens for haiku-4-5)."

So the framing "does reuse beat provider caching?" is answered on a workload where caching is structurally barred from the dominant cost term and the term it *can* address is 99.9% synthetic padding. The measurement reduces to "not doing the work costs less than doing the work" — true, and true of any memoisation, with the provider cache as an irrelevant bystander. Compounding this, **arm A is in zero ledger rows.** Pre-registered FP gate #2 ("arm A appears in exactly 1 row; `analysis.py` asserts") is unmet and the assertion does not exist. The experiment never establishes that provider caching saves anything at all — and the one A-vs-B measurement that exists (`pilot/arm_probe.json`) has **arm A at $0.1174 of input beating arm B at $0.1268** on the same three turns.

---

## 8. The "early cutoff, 1.33" claim is not in the ledger, and the mechanism it names is measurably broken.

**Every one of the 72 ledger rows has `achieved_over_ceiling = 1.000` exactly. Max = 1.0.** The 1.333 figure comes from `pilot/pilot_dag_30.json` — a **pilot**, at size 8, at **30%** perturbation, not the matrix at 0%. That same record reads `"n_answers_changed": 0, "all_perturbations_semantic": false`: **both of its perturbations were inert.** "Early cutoff fired" there because the perturbation changed nothing semantically.

Why it can never fire reliably: 6 of the 9 recorded shadow disagreements are `extract_facts` outputs that differ **only by a ` ```json ` fence** the model emits nondeterministically —

```
SERVED: {"sources":[2],"sinks":[4],"sanitizers":[],"secrets":[]}
FRESH : ```json\n{"sources":[2],"sinks":[4],"sanitizers":[],"secrets":[]}\n```
```

The dag family's designed mechanism (`families/dag.py`: "a change that alters no analysis-relevant fact re-runs exactly one operation, produces a byte-identical output, and stops dead") depends on byte-identical re-derivation of exactly that op. Measured byte-identity rate: roughly **50%**. Early cutoff fired **zero times** in the real matrix. The summary's health-gate narrative is drawn from a pilot artifact.

Relatedly, `achieved_over_ceiling` passes at 1.000 even in the chain@30% cells where `reuse_cost_weighted = 0.000` and `cross_run_hit_fraction = 0.00` — it is 0/0 forced to 1. **The arm-C health gate cannot fail.**

---

## 9. Four of six computed gates FAIL, two more are unimplemented, and the failures are on the false-positive side.

`results_blinded.json.gates`: `cold_cells_not_truncated` **FAIL** · `perturbations_semantic` **FAIL** (2 inert) · `position_uniform` **FAIL** (0.750 vs 0.15) · `size_curve_present` **FAIL**. Passing: `single_model_version`, `label_health`.

PREREGISTRATION §5: *"**Blocking** means the report's verdict is void in the stated direction if the check fails."* `perturbations_semantic` and `position_uniform` are both **false-positive guards** — void in the positive direction. Unimplemented: FP#2 (arm A row) and FN#10 (second model tier — everything is haiku-4-5, the cheapest model, never re-run on a frontier model "whichever way the verdict goes").

---

## 10. The chain@30% cell exists, is not "—", and arm C loses on one seed.

Ledger: B = 0.04354 / 0.06191 / 0.04993 (mean **0.05179**); C = 0.04097 / **0.07519** / 0.02883 (mean **0.04833**). Marginal **+6.7%**, not "~0%" behind a dash. On seed 1 arm C cost **21% more** than arm B. Given the noise floor (cost CV 0.21, spread 39.5%), ±1 s.e. on a 3-seed mean is ≈±12 points — so +6.7% is indistinguishable from zero *or* from negative. This is the only cell where a change reaches a graph root, i.e. the only cell resembling the "top 5% of PRs carry 86% of rebuild work" tail the external evidence flags. It lands in the **dead** band with unresolved sign, and the summary renders it as a dash.

---

## 11. Costs arm C carries and is not charged for

**(a) Reuse cannibalises the provider cache — measured, and it gets worse as reuse improves.** Arm C executes fewer ops, so it amortises the 2.0× 1h cache write over fewer 0.1× reads. Cost per executed model op:

| family | pert | B | C | ratio |
|---|---|---|---|---|
| dag | 2% | $0.00409 | $0.00949 | **2.32×** |
| dag | 10% | $0.00422 | $0.00895 | **2.12×** |

Arm C's cache-read fraction on dag falls 0.978 → 0.894. It is inside the metered dollars, so it does not change the arithmetic — but it means the two mechanisms are *substitutes*, and the anti-synergy deepens as hit rate rises. At a hypothetical 99% hit rate the surviving 1% pays near-full cache-write rates on every call.

**(b) Day-one population is excluded** (`runner.py` argues both arms pay it — fair in steady state). But day1 costs 20–45% more than the repeat ($0.0631 vs $0.0439 on chain), and the headline is an N→∞ limit. Total cost of ownership over N repeat runs:

| family | pert | N=1 | N=2 | N=5 | N=10 | N=∞ (reported) |
|---|---|---|---|---|---|---|
| wide | 2% | **44.7%** | 60.3% | 76.2% | 83.5% | 92.4% |
| dag | 2% | **31.9%** | 42.6% | 53.3% | 58.2% | 64.1% |
| chain | 30% | **3.7%** | 4.8% | 5.8% | 6.2% | 6.7% |

---

## 12. Quality is unmeasured in both directions, so "grader delta +0.0 pp" is not a safety result.

`wide` and `dag` graders return **1.000 for both arms in all 24 cells** — saturated, incapable of registering a regression. `chain`, the only unsaturated grader, is noise: three identical fresh runs scored **0.2 / 0.0 / 1.0**, grader-outcome disagreement **1.000**. The reported −33.3 pp and +66.7 pp are pass-rate deltas on n=3 with a coin-flip instrument. Meanwhile the three `chain` shadow mismatches are substantive: **5348 vs 5332, 5847 vs 6247, 3505 vs 1505** — arm C froze one nondeterministic draw of a task the base model gets right maybe 20–50% of the time, and the harness has no instrument that would notice.

---

## Summary-vs-ledger discrepancies

| Summary claim | Ledger |
|---|---|
| primary metric at "cold cache" | **0 cold rows**; `primary_candidates: []`; cold arm B structurally unreachable |
| "arm C health … reached **1.33** because early cutoff fired" | **max `achieved_over_ceiling` = 1.000** across all 72 rows. 1.333 is from `pilot_dag_30.json`, at 30%, with both perturbations flagged non-semantic |
| chain 30% shown as `—  —  ~0%` | cell ran: **+6.7%**, one seed at **−21%** |
| arm A "context row only" | **zero rows** |
| dag 10% grader delta +0.0 pp | +0.0 pp on pass rate; **−2.6 pp** on score |
| "B′ stale-serve exposure 7.7%" on wide | 1 of 13 ops — the **free** Python aggregator; `tracker_adds` is exactly 0.0 because it costs $0 |
| "2%" and "10%" as distinct levels | **`n_perturbed_artifacts = 1` in both**, all families, all seeds |

Confirmed correct: all 24 B/C cell means (5 dp), all ops-served counts, pooled arm-B cache-read fraction **0.9757** ≈ 0.976, grader deltas as pass-rate deltas, realized 7.7%, position 0.750, 2 inert perturbations, arm-B tuning 0.918 / 17,491 tokens / 7.9×, and the full noise floor.

---

# Strongest single argument

**The experiment never left the provider's cache window, and inside that window the only thing it could have measured is memoisation of an identical job.**

Maximum write-to-read span across all 36 reuse cells: **208 seconds**, against a 3,600-second TTL that arm B had switched on. Zero cells cleared it. The pre-registered gate that would have caught this — "max write-to-read span exceeds the provider TTL" — was never implemented in `analysis.py`. The one condition under which cross-run reuse could beat provider caching is a repeat that arrives *after* the provider forgot, and that condition has **no data**: it is the cold cells, `primary_candidates` is empty, the harness cannot produce a cold arm B by construction, and the two cold cells that did complete had their ledger rows deleted, unlogged, mid-review.

What remains is a 3-minute-old memoisation store beating a full re-run on a workload where 54–85% of the bill is output tokens no cache can touch and 99.9% of the cacheable input is padding the harness inserted to reach the caching floor. Set against the cheapest thing a real customer already has — a changed-file list — the dependency tracker adds **+0.0% on wide, −134% on chain, −481% on dag**. Charge it a validation rate its own audit can support (14.3% disagreement observed, 20.0% at zero perturbation, where the rule of three demands zero) and it becomes **+0.2% / +2.9% / −7.2%**.

**Should this be reported as positive? No.** Not as positive, and not as negative either — it should be reported as **inconclusive with the primary metric unmeasured**, plus one finding that is genuinely solid and genuinely negative: *dependency-tracked transitive invalidation is worth ≤ 0 relative to a changed-file list on all three families, and is worst on the family designed to make it win.* That B′ result stands on its own — it is computed from recorded graphs, needs no cold cells, and survives every correction above.

Reporting +92.4% would mean reporting an N→∞, warm-cache, zero-validation, single-model, one-changed-document, three-minute-old number against a baseline nobody runs, drawn from a matrix in which four of six blocking gates failed, as though it were a pre-registered result. The pre-registered bands were "under 10% = dead." **Every defensible correction lands in that band.**