# Pre-registration: does cross-run reuse of agent computation save real money?

Written **before** the first non-pilot run. Everything in §0 of the brief that permits design
changes requires them to be made and recorded first; this is that record. The decision rule in
§11 of the brief is reproduced here verbatim and is **not** modified — the brief forbids that,
and nothing below touches it.

Status at time of writing: harness self-tests pass, arm controls verified from live usage
fields, no matrix cell has been run, no result has been observed.

---

## 1. What changed from the brief, and why

### 1.1 Metering path: `claude -p`, not a metered API key

The brief requires token counts from the API response `usage` object and forbids tokenizer
estimates. The operator asked that the experiment avoid consuming metered API keys and run on
the assistant's own inference where possible. These are compatible, because
`claude -p --output-format json` returns the genuine per-request usage block:

```
usage.input_tokens                              usage.cache_read_input_tokens
usage.output_tokens                             usage.cache_creation_input_tokens
usage.cache_creation.ephemeral_5m_input_tokens  usage.cache_creation.ephemeral_1h_input_tokens
modelUsage.<version>                            total_cost_usd
```

So §8.2 and §8.3 are satisfied exactly: real metering, real per-class token split, exact model
version string per call. Cost is computed from metered tokens times recorded prices, with the
CLI's own `total_cost_usd` retained as an independent cross-check.

**Deviation to declare in the report:** billing is against a subscription rather than per-token
API billing, so `total_cost_usd` is a reference price rather than an invoice. The primary metric
is a *ratio* of two costs computed from the same recorded price sheet, so it is unaffected by
which account is billed.

### 1.2 Harness-supplied tool layer

Every call runs with `--disallowed-tools '*'`. The agent's tools are the harness's own Python
implementations. This is not a simplification, it is the requirement in §4: dependency edges are
recorded live at the moment the prompt is assembled, because reconstructing them from a
transcript afterwards loses edges, and missed edges look like independence, which overstates
reuse.

**Cost:** a fixed additive per-call overhead from the residual system prompt. It is measured by
`model.measure_overhead`, recorded in the run record, and reported, because it inflates both arm
B and arm C by the same additive amount and therefore *dilutes* the marginal saving toward zero.
A reader must be able to correct for it.

### 1.3 Fixing the §12/§13 asymmetry

§0 states the two failure-mode lists are deliberately symmetric and that if one side has more
teeth, that is a defect to fix and report. An independent auditor, blind to §0, scored both
lists. Its verdict:

> Not symmetric, on four independent axes, three of which point toward an inflated headline.
> List 1 (falsely positive): 8/10 items are pure arithmetic over data already collected, 0/10
> state a threshold, 0/10 cite a gate, 5/10 are trivially satisfiable.
> List 2 (falsely negative): 1/10 has a gate and a threshold, 5/10 require new budget, 2/10 are
> unfalsifiable prose.

The structural point it makes is the sharpest one: **the protocol armed the check that would
have fired anyway.** An experimenter hoping arm C wins is already motivated to run List 2's
checks, since each one raises the headline if it fires. List 1's checks lower the headline, cost
nothing, and had no gate at all.

Two fixes, both adopted here:

**Fix A — every List 1 item gets a mechanical threshold and blocking status.** See §5.

**Fix B — a configuration search log.** The auditor observed that List 2 items 2, 4, 6, 7, 9 and
10 each authorise trying a *different configuration* when the current one looks bad — finer
granularity, different families, bigger runs, more sessions, a looser correctness bar, a pricier
model — six knobs that all pull the headline up, with no pre-registration and no
multiple-comparison control. So: **every configuration change is appended to `search_log.md`
with its justification and timestamp before its result is observed.** The report states how many
configurations were tried. A headline drawn from configuration *k* is reported alongside the
headline from configuration 1.

### 1.4 Scale

The steel-man family as designed is 6,810 operations per run; the full matrix at that scale is
far beyond any sane budget. Runs are scaled down by a fixed factor per family, and **reuse is
reported as a function of run size** at 3 sizes per family, which is the check false-negative
item 6 demands anyway (is the curve still climbing at the largest size?). The scale factor is
recorded per family and stated in the report.

---

## 2. Arms, verified from usage rather than configuration

| arm | control | verified |
|---|---|---|
| A | `DISABLE_PROMPT_CACHING=1` | `cache_creation=0, cache_read=0` over 3 turns |
| B | `ENABLE_PROMPT_CACHING_1H=1` | `eph_1h=60,411`, `eph_5m=0`, cache-read fraction 58.7% on later turns |
| C | identical to B, plus the reuse layer | store hits recorded with writer/reader run ids |

Arm C sits on **exactly** the same provider configuration as arm B. If it did not, the
comparison would be measuring a TTL change rather than the reuse layer.

Arm A is a context row only. It appears in exactly one labelled row of the report and is used in
no other computation. `analysis.py` enforces this.

### 2.1 Facts about provider caching that bear on the design

Fetched live 2026-08-17 from provider docs, recorded in `recon/recon.json`. These are inputs,
not predictions:

- Anthropic's 5-minute TTL is **refreshed on read**, so an entry touched more often than every
  5 minutes stays warm indefinitely at no extra cost.
- Anthropic cache scope is **workspace-level and reusable across users and sessions**. The
  provider already delivers cross-user and cross-session prefix reuse. The gap a cross-run store
  addresses is therefore **time**, not scope — narrower than §3 of the brief assumes.
- Minimum cacheable prefix is per-model and not monotonic: 4,096 tokens for `claude-haiku-4-5`,
  1,024 for `claude-sonnet-5`, 512 for `claude-opus-5`. Below the floor a request silently does
  not cache and `cache_creation_input_tokens` is 0. Any per-operation prompt below the floor is
  uncacheable **by the provider's design**, which is a real ceiling on arm B and must not be
  mistaken for under-tuning.
- A cold provider cache only costs arm B the **first** write of a run; the run then self-warms.
  This bounds how much room H2 can have, and it is measured rather than assumed.

---

## 3. Decision rule — reproduced verbatim, unmodified

Primary metric: **marginal dollar saving of arm C over arm B, at 2% perturbation, cold cache, on
whichever family performs best.**

| marginal saving | verdict |
|---|---|
| under 10% | dead. Provider caching already captures the value. |
| 10% to 25% | not a standalone business. Possibly a feature. |
| over 25% | live. Proceed to the commercial questions. |

Both health gates must pass or the result is void in that direction.

- **Arm B health (guards a false positive):** cache-read token fraction above 50% on multi-turn
  loops, verified from response usage fields.
- **Arm C health (guards a false negative):** at 0% perturbation, arm C must achieve at least
  95% of the theoretical reuse ceiling implied by the recorded dependency graph.
- **Correctness gate (both directions):** arm C grader pass rate within 2 percentage points of
  arm B, after accounting for the noise floor.

Primary quantity is `(cost_B − cost_C) / cost_B`. `(cost_A − cost_C) / cost_A` appears in exactly
one labelled context row.

---

## 4. Matrix, pre-committed

```
families        : 4 to 6, selected by the scenario sweep (§6), spanning chain / wide / DAG
                  and including the most favourable case identified
perturbation    : 0%, 2%, 10%, 30%
arms            : A, B, C   (A: one seed per family at 0% only)
temperature     : warm repeat, cold repeat   (arm A ignores temperature)
seeds           : 3
run sizes       : 3 per family, for the size-scaling curve
```

Cold means a real wall-clock gap exceeding 1 hour, the longest TTL enabled, **verified from
`cache_read_input_tokens == 0` on the first operation of the repeat run**, not from the clock.
Coldness is never faked by altering the prefix, which would change arm B's workload.

**No optional stopping.** The ledger is resumable and therefore makes peeking easy; peeking and
stopping on a conclusive-looking partial number invalidates the result. The only permitted early
stop is at a declared checkpoint boundary covering all arms and families equally, and the report
must say so. Checkpoint boundaries: after all seeds of {all families × all perturbations × arms
B,C × warm}, and again after the same set cold.

Model tiers: develop and debug on `claude-haiku-4-5`, full matrix on `claude-sonnet-5`, re-run
the best family on `claude-opus-5` regardless of the verdict's direction — false-negative item 10
and its symmetric risk that a cheap model overstates savings.

---

## 5. Gates, thresholds and blocking status

Fix A from §1.3. Every item in both lists now has a mechanical check. **Blocking** means the
report's verdict is void in the stated direction if the check fails.

### Guarding a false positive (§12)

| # | check | threshold | blocking |
|---|---|---|---|
| 1 | arm B cache-read fraction, from usage | ≥ 0.50 on multi-turn; and arm B must be the cheapest of ≥3 tuning configurations actually measured | yes |
| 2 | headline computed against arm B | arm A appears in exactly 1 row; `analysis.py` asserts | yes |
| 3 | perturbations are semantic | ≥ 95% of perturbed artifacts change the grader answer key | yes |
| 4 | 0% is a ceiling | headline computed only at 2%; every 0% row emitted with a `CEILING` label by the table generator | yes |
| 5 | dollars not tokens | cost from recorded prices; token-only comparison tables forbidden | yes |
| 6 | cost-weighted reuse | both count- and cost-weighted emitted; analysis fails if either missing | yes |
| 7 | no pooling across families | per-family always; pooled figures forbidden in the headline | yes |
| 8 | perturbation position uniform | max stratum share deviation < 0.15 and mean blast fraction within 20% of population mean | yes |
| 9 | shadow cost included | headline arm C cost **includes** the shadow rate needed to bound staleness at the measured level; shadow-free arm C is secondary | yes |
| 10 | storage cost included | measured store bytes × published storage price, amortised per run, added to arm C | yes |

### Guarding a false negative (§13)

| # | check | threshold | blocking |
|---|---|---|---|
| 1 | arm C reaches its ceiling | ≥ 95% of conservative ceiling at 0% perturbation | yes |
| 2 | tracking granularity | reuse reported at both span-level and file-level; the finer figure is the headline | yes |
| 3 | canonicalisation | same logical input hashed twice through the full path must match; run-local values rejected at encode time | yes (self-test) |
| 4 | families not chosen unfavourably | families selected by a blind sweep over 24 domains, incl. the most favourable identified | yes |
| 5 | perturbation position uniform | same check as false-positive 8, opposite direction | yes |
| 6 | enough operations per run | 3 run sizes per family; report whether the reuse curve is still climbing at the largest | yes |
| 7 | store genuinely cross-run | `cross_run_fraction` > 0 and max write-to-read span exceeds the provider TTL | yes |
| 8 | cold cells not truncated | cold cell count equals warm cell count per family; asserted by `analysis.py` | yes |
| 9 | correctness bar is grader outcome | exact match reported as diagnostic only; the bar is grader outcome | yes |
| 10 | more than one model tier | best family re-run on a frontier model whichever way the verdict goes | yes |

---

## 6. Families

Selected by a 24-domain blind scenario sweep plus 4 adversarial critics, ranked by expected
cost-weighted saving **over a tuned provider cache**, not by raw reuse rate. Required to span
deep chain, wide independent and layered DAG, and to include the single most favourable case, so
that a negative result there is strong and a positive one locates the beachhead.

The steel-man family (§5 of the brief, family 4) was designed by an agent that saw only a neutral
description of the system and the two constraints (real customer, programmatic grader). It saw
nothing of §0, §12 or §13, and was told nothing about an expected outcome. Its spec and eliciting
prompt are reproduced in `family4_spec.md`.

Final family list is appended to this file before the first non-pilot run, in §8.

---

## 7. Honesty controls

- **Blinding.** Arms are written to the ledger as `arm_XXXX`, mapping in `arm_mapping.json`.
  `analysis.py` never opens that file. Unblinding is timestamped into the mapping file and
  happens only after every number is final.
- **Frozen analysis.** `analysis.py` is written, tested against synthetic ledgers with known
  answers, and hashed before the experiment runs. The hash is recorded here in §8. Any post-hoc
  change is recorded with its reason and the pre-registered version's output reported alongside.
- **Adversarial review, both directions.** Two independent reviewers, neither shown §0, receive
  the raw ledger. One argues the result is falsely positive, the other falsely negative. Both
  write-ups are included verbatim. If either finds a defect that changes the verdict, the verdict
  changes.
- **Negative space.** The report lists every cell not run, every check not performed, and every
  question the harness cannot answer.

A null result is a successful outcome. So is a positive one. If the marginal saving is under 10%,
the report says so in the first sentence. If it is over 25%, the report says that in the first
sentence and does not hedge it.

---

## 8. Frozen values

Filled in before the first non-pilot run.

- `analysis.py` sha256: `801c6e2bbd8e34c813f3c9b264878036fb6b9c4acfcdbaed61cf99a787b9dd43`
- `test_analysis.py` sha256: `bedbccff27f17150f760c7b9c980c437168b5c6319e890f9a5120bdf2d9e8ac4` (22 tests, all passing at freeze)
- Price sheet sha256: `3375f58aac5a945846ea964903d2446e30ef7c41017dd1bdb2a87d96dff2e5f7` (fetched 2026-08-17 from provider docs)
- Frozen at: 2026-08-17T07:27:48-04:00
- Matrix cells run at freeze time: 0
- Configuration count at freeze: 1, plus 2 pre-matrix corrections recorded in `search_log.md`
- Family list: `wide`, `chain`, `dag` — see below

### Family selection, and why these three

A 24-domain blind scenario sweep (44 agents) specified how each workload is really run, researched
its real between-run change rate, and specified the provider-cache baseline a competent engineer
would build. Four adversarial critics then scored the specs for straw-man baselines and invented
change rates, and a blind synthesis ranked them by expected saving **over a tuned provider cache**.

The sweep's own measurements are the reason the three implemented families are the right span:

| finding from the sweep | value |
|---|---|
| scenarios whose real change rate is ≤2% (the brief's assumption) | **4 of 24** |
| median real change rate | **9.0%** |
| scenarios where the change rate was sourced rather than estimated | 15 of 24 |

The domains separate into three archetypes by where changes land, and the estimated marginal saving
tracks that split almost perfectly:

- **leaf-independent, long cadence** (contract-clauses, compliance-mapping, financial-close;
  estimated 38-45%) → implemented as **`wide`**
- **layered DAG with early cutoff** (the steel-man family; security-scan, listings) → **`dag`**
- **root-change dominated** (ci-triage 46% churn, api-contract-diff 58%, log-rca 65%; estimated
  3-6%) → implemented as **`chain`**, the extreme case where a root change invalidates everything

The three families are archetypes chosen to span the observed space, not the three highest-scoring
domains. Choosing only the top-ranked domains would have been the exact defect false-negative item
4 warns about, in reverse.
