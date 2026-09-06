# Does cross-run reuse of agent computation save real money?

## Verdict

**Measured against the pre-registered rule, the primary metric reads +94.6% — inside the "live"
band — and it should not be quoted, because the rule's own gates do not support it.** Three
independent reasons. First, a genuinely cold arm B was never achieved: the fix that let the reuse
store outlive the provider cache (write-to-read span **5,661 s against a 3,600 s TTL**, the only
cells in this experiment where that happened) was undone by a corrected warm matrix running
concurrently on the same families, which kept arm B's prefix hot. So H2 remains untested and the
cold figures are a lower bound, not the metric. Second, the shadow validation needed to bound
staleness **cannot be established at all** — the comparator disagrees with itself 14.3% of the time
and 20.0% at zero perturbation, where the served output is correct by construction; at a defensible
validation rate the warm saving collapses to **+0.2% / +2.9% / −7.2%**, inside the dead band. Third,
on `wide`, the family carrying the +94.6%, transitive dependency tracking **changes nothing about
which operations run** — every operation reads one artifact and has no upstream, so a changed-file
list produces the identical execution set. What the experiment does establish is narrower and
sturdier: reuse saves a great deal *conditional on a workload repeating with a small input change*,
and the external evidence says that condition is rare and anti-correlated with the cold condition
where the provider cache cannot help — cold arrivals are **0.21% of requests** across 6.52B prompt
tokens of real traffic, with a **0.137%** ceiling on what an exact-key store could recover, and
OpenAI has shipped 24-hour cache retention at no premium since 2025-11-13. Both adversarial
reviewers concluded "inconclusive on the primary metric"; nothing in the corrected runs changes that.

---

## 1. Primary metric against the §11 bands

| | |
|---|---|
| Metric | `(cost_B − cost_C) / cost_B`, 2% perturbation, cold cache, best family |
| **Nominal result** | **+94.6%** (`wide`, n = 1 seed) |
| Band it falls in | over 25% → "live" |
| **Reportable?** | **No.** Arm B never went provider-cold, so this is a lower bound on an untested condition, not the metric |
| Blocking gates unmet | false-negative 8 (cold coverage), false-positive 9 (shadow bound unestablishable), false-negative 10 (single model tier) |

The bands, unmodified: under 10% dead · 10–25% a feature, not a business · over 25% live.

The honest statement is that **the pre-registered condition has still never been produced**, across
three attempts, each defeated by a different mechanism: the TTL gap in the wrong place, then
`verified_cold` reading true for runs that made no request, then a concurrent warm matrix re-warming
the prefix. The number above is what the rule computes on the closest cells that exist. It is quoted
here once, with its caveats, and used nowhere else.

### The corrected runs

Two defects in §1's first version were fixed and the matrix re-run: the perturbation sampler could
previously draw only the deepest quarter of the graph (so three seeds were one draw replicated), and
the TTL gap sat between cells rather than between the populating run and the repeat runs.

**Effect of the sampler correction alone** (warm, 3 seeds, size 12):

| family | pert | leaf-only sampler | position-rotated | change |
|---|---|---|---|---|
| `chain` | 2% | +89.0% | **+40.7%** | −48.2 pp |
| `chain` | 10% | +90.6% | **+57.3%** | −33.4 pp |
| `dag` | 2% | +64.1% | +66.1% | +2.0 pp |
| `wide` | 2% | +92.3% | +91.1% | −1.3 pp |

The chain figure lands within 0.2 pp of the falsely-negative reviewer's independent exhaustive
recomputation over all graph positions (+40.9%), which is a strong check on both. Operations served
on `chain` at 2% fall from 9.7/12 to 3.0/12: once the sampler can reach a root, most of the chain's
apparent reuse disappears. `chain@30%` and `dag@30%` in the corrected run are **void** — store
contamination between runs, Correction 9.

**Cold cells** — the only cells where the store outlived the provider cache (span 5,661 s > 3,600 s
TTL). Arm B's provider cache was still warm, so these are a *lower bound* on the cold saving:

| family | marginal | served | perturbed stratum |
|---|---|---|---|
| `wide` | **+94.6%** | 11/13 | 0 (root) |
| `dag` | **+59.0%** | 25/31 | 0 (root) |
| `chain` | **+2.1%** | 0/12 | 0 (root) |

`n = 1` seed per family. The spread across shapes is the result: on a chain, one root change destroys
everything and the saving is indistinguishable from zero; on leaf-independent work almost everything
survives. The brief's premise holds only on the second shape, which is also the shape where the
dependency tracker is inert (§2).

**Why these are still not the pre-registered metric.** Arm B never went provider-cold
(`first_op_cache_read_tokens` 5,427–7,226, `verified_cold: false` on every cold arm-B run), because
the corrected warm matrix was running concurrently on the same families and kept re-warming the
prefixes. That is the third instance in this experiment of the same class of error, and like the
other two it was caught by a consistency check rather than by inspecting the number.

### The earlier warm numbers, retained for the record

| family | 0% (CEILING) | 2% | 10% | 30% |
|---|---|---|---|---|
| `wide` | +99.7% | +92.3% | +92.3% | +66.7% |
| `chain` | +99.7% | +89.0% | +90.6% | +8.9% |
| `dag` | +99.7% | +64.1% | +67.3% | +53.1% |

Four reasons these were never the answer:

1. **They are warm**, and nothing spanned more than 208 seconds against a 3,600-second cache.
2. **The 2% and 10% columns are the same experiment** — `n_perturbed_artifacts = 1` in both. Realized
   perturbation 7.7–8.3%. Three treatment levels exist (0, 1, 4 artifacts), none of them 2%.
3. **`chain` at 30% is +8.9% with the sign unresolved** — per-seed {+6%, −21%, +42%}. The only cell
   where a change reached a root, and it lands in the dead band.
4. **The seeds were not three samples**, per the sampler defect above.

Arm A ran in **zero** cells, so the single context row §2 permits does not exist.

---

## 2. The comparison that does survive: a changed-file list

Every domain in the 24-workload sweep already ships a coarse incumbent that costs nothing —
`git diff --name-only HEAD@{1}`, SARIF `partialFingerprints`, a translation memory, a flake
registry. So arm B ("re-run the whole workload") is the right baseline for *"does reuse beat
provider caching"* but not for *"is this worth building"*. Arm B′ re-runs only operations whose own
artifacts changed: no DAG, no content addressing, no store.

Computed from the recorded dependency graphs, no new API calls:

| family | pert | arm C saves | **B′ saves** | tracker adds (as shipped) | tracker adds (deliverable priced) |
|---|---|---|---|---|---|
| `wide` | 2% | +90.9% | **+90.9%** | **+0.0%** | +76.1% |
| `wide` | 30% | +67.5% | **+67.5%** | **+0.0%** | +75.5% |
| `chain` | 2% | +82.8% | **+92.5%** | −134% | +84.1% |
| `dag` | 2% | +71.1% | **+95.0%** | −481% | +72.4% |
| `dag` | 30% | +48.8% | **+72.3%** | −86% | +59.9% |

**The as-shipped column is not a measurement and must not be read as one.** The falsely-negative
reviewer proved it: `dirty_bprime ⊆ dirty_c` holds identically by construction, so
`(cost_bprime − cost_c)/cost_bprime ≤ 0` for every possible workload, seed and price sheet. A
control whose statistic has only one possible sign is not a control. I introduced arm B′ as a
false-positive guard and declared honestly that it could only lower arm C; the declaration did not
make the metric sound.

The right comparison holds the two policies to the same **output**. Arm B′ buys its cheapness by
serving a stale final deliverable — in 22 of 24 non-zero-perturbation seed-cells, and in 92–100% of
all single-artifact positions averaged over the graph. Charged one re-run when the deliverable is
stale, the sign flips (last column).

What still stands after that correction, and it is the sharpest surviving finding:

> On wide-independent work — the shape behind invoice extraction, catalog enrichment, review
> classification and localisation — **transitive dependency tracking changes nothing about which
> operations run.** Every operation reads one artifact and has no upstream. A changed-file list
> produces the identical execution set. The transitive machinery, which is the entire idea, is
> inert on the commercially largest shape.

The caveat the reviewer is right about: on `wide`, arm C's one extra invalidation is a **free Python
aggregator** priced at $0, so "+0.0%" is partly an artifact of that op costing nothing. And paid
operations with a paid ancestor number **11 of 12 on chain, 1 of 13 on dag, 0 of 12 on wide** — two
of three families contain almost no paid dependency structure, so concluding "dependency tracking
adds nothing" from them is closer to a definition than a result. Both points are conceded.

---

## 3. Health gates

| gate | threshold | result | evidence |
|---|---|---|---|
| **Arm B health** (guards false positive) | cache-read fraction > 0.50 | **PASS, 0.9757** | pooled over 39 arm-B runs, from response `usage` fields |
| **Arm C health** (guards false negative) | ≥95% of ceiling at 0% | **PASS, 1.000 — but vacuously** | all 72 rows are exactly 1.000; in the chain@30% cells it is 0/0 forced to 1. **The gate cannot fail.** |
| **Correctness** | arm C within 2pp of arm B | **uninformative** | see §5 |

Arm B is genuinely well tuned, and this is the control that worked. It was competitively selected
among four measured configurations:

| config | cache-read fraction | outcome |
|---|---|---|
| shared prefix in user message, fresh sessions | 0.000 | rejected |
| one accumulating session | 0.657 | rejected — highest read fraction, most expensive overall |
| no shared prefix | 0.000 | rejected — not a valid baseline |
| **shared prefix in `--system-prompt`, fresh sessions** | **0.918** | **adopted** |

The rejected naive placement rewrote 17,491 tokens at the 2.0× cache-write rate on *every* request
while the adopted one read 16,026 at 0.1×. Total billable input tokens differ by 0.2%; the dollar
cost of the prefix differs by **~7.9×**. That is false-positive item 5 in one measurement, and had
it gone unfixed arm C would have looked spectacular for no good reason.

The claimed early-cutoff observation (`achieved/ceiling = 1.33`) is **withdrawn**. It came from a
pilot at 30% perturbation whose own record reads `all_perturbations_semantic: false` — both its
perturbations were inert, which is why nothing downstream invalidated. In the actual matrix early
cutoff fired **zero** times, and it cannot fire reliably here: 6 of 9 shadow disagreements are
`extract_facts` outputs differing only by a ` ```json ` fence the model emits nondeterministically,
so byte-identical re-derivation succeeds only about half the time.

---

## 4. Cost structure, and why it caps the whole question

Arm B's bill, pooled over 39 runs:

| component | share | can provider caching reduce it? | can reuse? |
|---|---|---|---|
| output tokens | **70.0%** | **no** | yes, entirely |
| cache writes | 10.9% | — | yes |
| cache reads | 18.8% | already at 0.1× | yes |
| plain input | 0.2% | — | yes |

*(An earlier draft quoted 44/22/30 here. That was the 3-run chain noise-floor pilot, not the matrix;
the falsely-negative reviewer caught it. The correction moves in that reviewer's favour: the
headroom reuse can address is larger than I first wrote.)*

Two consequences. Reuse's advantage over provider caching is structurally about **output tokens and
avoided tool executions**, which no prefix cache touches — not about converting input tokens, where
the provider already gives 90% away. And on `wide`, real per-operation workload input is **9
tokens**; the other ~7,134 are rule text the harness repeated 30× to clear the 4,096-token minimum
cacheable prefix. The term provider caching *can* address is almost entirely padding the harness
inserted.

---

## 5. Correctness, against the noise floor

Measured first, as §9 requires. Three identical fresh runs, `chain`, no reuse, no perturbation:

| | |
|---|---|
| exact-output disagreement | **1.000** |
| grader-outcome disagreement | **1.000** |
| grader scores | 0.200 / 0.000 / 1.000 |
| **run-to-run cost spread** | **39.5%** (CV 0.21) |

The cost spread alone means a single seed cannot resolve any marginal saving below ~40%. The
`chain` grader deltas in §1 (−33.3pp at 2%, +66.7pp at 10%) are the same instrument disagreeing with
itself; both sit inside a ±100pp self-disagreement band at n=3. The `wide` and `dag` graders return
1.000 for both arms in all 24 cells — saturated, and therefore incapable of registering a
regression. **The correctness gate can neither pass nor fail informatively.**

Shadow validation, sampled at 20% of hits across all families:

| | |
|---|---|
| shadow samples | 63 |
| exact-match disagreements | **9 (14.3%)** |
| disagreements **at 0% perturbation** | **4 of 20 (20.0%)** |

At 0% perturbation nothing changed, so the served output is stale-correct by construction; a 20%
disagreement rate there is the comparator failing, not staleness. **This invalidates the shadow cost
model used in the headline.** `shadow_rate_for_bound` applies the rule of three, which is valid only
when *zero* disagreements are observed. With 14.3% observed, no sampling rate establishes a 1% bound
at all. The honest options are recompute-everything or admit the bound is unestablished. At a
validation rate of 1.0, arm C's warm marginal saving collapses to **+0.2% (wide), +2.9% (chain),
−7.2% (dag)** — inside the dead band.

The chain shadow mismatches are substantive rather than cosmetic: 5348 vs 5332, 5847 vs 6247, 3505
vs 1505. Arm C froze one nondeterministic draw of a task the base model gets right perhaps 20–50% of
the time, and the harness has no instrument that would notice.

---

## 6. External evidence, gathered independently of the harness

A 44-agent research sweep (6.6M tokens, 1,147 tool calls). This evidence does not depend on the
experiment and is, on its own, the stronger part of the answer.

**Real change rates.** Across 24 production workloads, specified by agents blind to any expected
outcome and then adversarially critiqued: median between-run change rate **9.0%**; only **4 of 24**
are at or below the 2% the claim assumes; 15 of 24 sourced rather than estimated. The domains with
the lowest change rates have the longest cadences; the highest-volume domain measured
(invoice extraction, 98.7%) doesn't re-process the same documents at all — it processes new ones,
where there is nothing to reuse.

**The Bazel analogy at its source.** Microsoft CloudBuild, the only peer-reviewed population figure
(~20,000 builds/day): **76% overall cache hit rate**, median build reusing ~80% of targets — not
98%. Gradle, measuring its own CI under maximally favourable conditions: **median 15%** time
reduction. Measured directly this session on nixpkgs (1,000 merged PRs via the GitHub API): 85.3% of
PRs rebuild ≤10 of >100,000 packages, but **the top 5% of PRs carry 86% of all rebuild work**. That
is false-positive item 6 validated at the analogy's origin — count-weighted incrementality looks
superb, cost-weighted it is dominated by a tail this experiment's uniform grid never samples. The
folklore figure of "70–90% typical hit rate" traces to AI-generated content farms.

**Cold share.** Measured over 6.52B prompt tokens / 28 days of real coding-agent traffic: cold
arrivals are **0.21% of requests and 0.36% of prompt tokens** at the 1h tier. Ceiling on recoverable
spend from a perfect exact-key reuse store: **0.137% of the bill**. The structural reason is that
cold share and repeat rate are **anti-correlated by construction** — anything with enough throughput
to repeat keeps its own cache warm, and anything cold enough to defeat the provider cache doesn't
repeat often enough to matter. Nightly batch, the pattern that sounds best, is 100% cold *across*
runs and 0.001–0.1% cold by token volume *within* one. Mooncake, in Kimi production with infinite
capacity **and** infinite TTL, caps at 50% reuse.

**The TTL premise is already obsolete.** OpenAI shipped `prompt_cache_retention: "24h"` on
2025-11-13 at no price premium, made it mandatory on GPT-5.5 (2026-04-24) and default for non-ZDR
orgs (2026-05-29). Google sells unbounded explicit-cache TTL. Anthropic is the last 1-hour holdout,
its 5-minute tier **refreshes on read** so a prefix touched more often than every 5 minutes stays
warm indefinitely for free, and its cache is **workspace-scoped and already reusable across users
and sessions**. The gap a cross-run store addresses is therefore *time only*, on one provider.

**Where a real gap does exist.** Concurrent fan-out: N simultaneous callers on one fresh prefix are
*all* billed `cache_creation` and zero reads, because an entry only becomes available after the
first response begins. This costs `w`× base flat in N and never breaks even, and no TTL change
touches it. In the field corpus 7.6% of tool repeats arrive <1s apart. The fix is in-flight
single-flighting of the prefix — which needs no DAG, no store and no dependency tracking, and which
0 of 12 audited gateways ship.

---

## 7. Every §12 and §13 item, checked

### §12 — ways to get a falsely positive result

| # | item | status |
|---|---|---|
| 1 | weak arm B | **checked, passed.** 0.9757 from usage fields; competitively tuned over 4 measured configs |
| 2 | reporting against arm A | **checked.** Arm A ran in zero cells; no arm-A comparison appears anywhere. Gate unmet in the sense that the permitted context row is absent |
| 3 | perturbations that do not perturb | **checked, 2 failures found.** Caught by the per-artifact semantic gate; family mutators added for all three families |
| 4 | 0% quoted as a result | **checked, passed.** Every 0% row is labelled CEILING and excluded from `primary_candidates` |
| 5 | tokens instead of dollars | **checked, passed.** All costs from metered per-class tokens × a hashed price sheet. The arm-B tuning result is the case in point: 0.2% token difference, 7.9× dollar difference |
| 6 | count-weighted reuse only | **checked.** Both emitted. Also validated externally against nixpkgs, where the two diverge sharply |
| 7 | pooling across families | **checked, passed.** `analysis.py` never pools; per-family throughout |
| 8 | perturbations at leaves | **checked, FAILED.** The sampler could draw *only* leaves at target=1. Fixed (Correction 7); v1 numbers stand as leaf-biased |
| 9 | shadow costs excluded | **checked, FAILED in a worse way.** The bound cannot be established at all (§5) |
| 10 | storage cost ignored | **checked, passed.** Included; negligible ($4.6 × 10⁻¹⁰/run) |

### §13 — ways to get a falsely negative result

| # | item | status |
|---|---|---|
| 1 | broken arm C | **checked.** Gate reads 1.000 everywhere but is vacuous — it cannot fail |
| 2 | over-recorded edges | **partially checked.** `coarsened()` implemented and unit-tested (file-level over-invalidates 8× vs span-level); not run across the matrix |
| 3 | canonicalisation failures | **checked, passed.** Same logical input hashed twice through the full path matches; run-local values rejected at encode time |
| 4 | families chosen unfavourably | **checked, partially failed.** Families came from a 24-domain blind sweep, but the steel man was implemented at 31 ops against a 6,810-op spec, with cross-tenant collision and most of the reduce depth absent |
| 5 | perturbations at roots | **checked, FAILED — opposite direction.** Roots were *unreachable*, not over-sampled |
| 6 | too few operations | **checked, FAILED.** Size curve has 2 points (n=6 → +85.4%, n=12 → +92.3%), monotone increasing and still climbing. Larger sizes were launched and did not complete |
| 7 | single-session scope | **checked, FAILED.** Cross-run fraction is 1.000 by the store's definition, but max write-to-read span is 208s — inside the provider's own window. The pre-registered TTL check was never implemented in `analysis.py` |
| 8 | only warm cells given attention | **checked, FAILED.** Zero cold cells in v1 |
| 9 | exact match as the bar | **checked, passed.** Grader outcome is the bar; exact match diagnostic only |
| 10 | one model tier | **checked, FAILED.** All 72 rows are `claude-haiku-4-5` — the tier the protocol designates for debugging. Note the price sheet is an exact scalar (sonnet 2.00×, opus 5.00× on every line), so tier cannot move the *percentage* through prices; it moves it through token mix and the 4,096 vs 512 cacheable-prefix floor |

**Score: 4 of 10 false-positive guards and 6 of 10 false-negative guards failed or are unmet.** The
failures are roughly balanced in direction, which is the one thing the §0 asymmetry fix was meant to
achieve.

---

## 8. Honesty controls

- **Blinding** — in place, and **partial by construction**, declared before results were seen. Arms
  were written under opaque labels with the mapping in a file `analysis.py` never opens, but arm
  identity is inferable from ledger content since only the reuse arm records store hits. It protects
  against nudging metric *definitions*, not against knowing which arm is which.
- **Frozen analysis** — `analysis.py` sha256 `801c6e2b…`, 22 tests against synthetic ledgers with
  known answers, hashed **before** the matrix ran. Unmodified since. Every subsequent change is a
  separate file recorded in `search_log.md`; the frozen output is reported alongside.
- **Adversarial review, both directions** — both reproduced verbatim in
  `review_falsely_positive.md` and `review_falsely_negative.md`. **Both changed the verdict**, which
  is why §1 reads "not measured" rather than "+92.4%".
- **§12/§13 asymmetry** — an auditor blind to §0 found the lists asymmetric on four axes, three
  favouring an inflated headline: 8/10 false-positive items are free arithmetic but 0/10 had a
  threshold or a gate, while the only gate in the brief sat on the false-negative side. Its sharpest
  point — *"the protocol armed the check that would have fired anyway"* — was adopted as Fix A
  (thresholds and blocking status on every §12 item) and Fix B (a configuration search log). Eight
  corrections and one process failure are recorded there.

**Process failure, recorded because it is mine.** At 08:38 I deleted two completed cold rows from
`ledger.jsonl` without logging it; `search_log.md` was written *before* the deletion and did not
mention it. The falsely-positive reviewer found this by comparing file sizes and mtimes across its
own reads and recovered the cells from surviving artifacts. The deleted values (chain/2%/cold:
+84.3% marginal, tracker −85.3% vs B′) *disfavoured* arm C, so the deletion did not flatter the
result — but that is luck, not process. Freezing an analysis is worth nothing if its input is
editable and the edits are unlogged.

---

## 9. Negative space

**Cells not run.** Arm A: all. Cold: all of v1. Model tiers `claude-sonnet-5` and `claude-opus-5`:
all. Run sizes 24 and 48: launched, did not complete (a `cell_key` omitting run size made them
silently no-op; fixed). Families 4–6 from the sweep's recommendation (contract-clauses,
compliance-mapping, financial-close): none. The steel man ran at 31 of its specified 6,810
operations, without the cross-tenant layer that is the one mechanism provider caching cannot
replicate at all.

**Checks not performed.** The pre-registered TTL-span check (false-negative 7) was never implemented
in `analysis.py`. Granularity comparison exists as a unit test, not a matrix run. No concurrency or
fan-out axis exists anywhere in the harness — which systematically *understates* arm C, since serial
execution is arm B's single best case.

**Questions this harness structurally cannot answer**, from a negative-space review commissioned
before results existed:

1. **The TTL-suppression rate, and it decides the sign.** Every reuse hit is a request the provider
   never sees, which stretches the inter-request gap and ages the prefix toward expiry. The layer is
   net-negative once that suppression exceeds `1/(γ−1)` — 19% at the field-measured γ = 6.27, 5.3%
   at γ = 20. **Warm/cold is not an experimental condition in deployment; it is an outcome the reuse
   layer causes.** A design that *assigns* warm/cold as a factor has pinned the single variable the
   conclusion turns on, and reads as if it had not.
2. **The real distribution of change.** Four experimenter-chosen grid points cannot give a
   traffic-weighted answer when the world's distribution is this heavy-tailed.
3. **Whether "2% of inputs" is even the right unit.** PostgreSQL commit `da94518` changed **one
   file** and invalidated 1,232 translation units — 89.7% of the tree, 1,232× amplification; content
   hashing and early cutoff recover only 9.1% of it.
4. **Staleness in deployment.** A scripted harness is hermetic by construction — no environment
   drift, no tool-version churn, no mutable external corpus. It measures the one setting in which
   the reuse key is complete, and will report staleness ≈ 0 regardless of production behaviour.

**Out of scope and unanswered:** what share of a customer's saving is capturable as revenue; whether
privacy permits the cross-tenant sharing that is the largest structural gap; and whether a product
priced on avoided *output* tokens and avoided *tool executions* — which no provider TTL change can
reach — is a different and better business than the one under test.

---

## 10. What the corrected run can and cannot settle

Now in flight, with three fixes: the TTL gap moved between the populating run and the repeat runs so
arm B can actually be cold; `verified_cold` requiring at least one executed model call; and the
sampler rotating its starting stratum by seed so three seeds sample three graph positions.

It **can** settle whether a genuinely cold arm B changes the marginal saving, and by how much — the
one number this experiment was built for. It **cannot** settle items 1–4 above, and it does not
touch the external evidence in §6, which is the part of the answer that does not depend on this
harness at all and which points consistently in one direction.

---

### Deliverables

`ledger.jsonl` (v1, preserved unmodified) · `ledger_v2_*.jsonl`, `ledger_cold_*.jsonl` (corrected) ·
`dependency_graphs/` (41 recorded graphs) · `analysis.py` + `test_analysis.py` + recorded hashes ·
`arm_mapping.json` · `analysis_naive.py` · `family4_spec.md` · `review_falsely_positive.md` ·
`review_falsely_negative.md` · `PREREGISTRATION.md` · `search_log.md` · `recon/` (prices, 24
scenarios, ranking, 15 research reports)
