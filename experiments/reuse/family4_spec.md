# family4_spec.md — the steel-man workload

Deliverable 4. §5 of the brief requires that, before the matrix runs, a separate agent be given
one instruction: design the workload on which cross-run dependency-tracked reuse performs *best*,
subject only to the constraint that it be a task a real customer would plausibly pay to run. That
agent must not see §0, §12 or §13 of the brief, and must not be told anything about an expected
outcome.

This family exists because the brief's author chose families 1 to 3, and that choice alone could
determine the verdict. If the idea fails even on a workload designed by someone trying to make it
succeed, the negative result is strong. If it succeeds there, the beachhead has been located,
which is a more useful output than a verdict.

## Eliciting prompt, reproduced verbatim

The agent was a fresh subagent with no conversation context. It received a neutral description of
the system under test (operation, inputs of an operation, reuse key, transitive composition,
cross-run store, perturbed repeat run) followed by:

> YOUR TASK
>
> Design the workload on which this system performs BEST.
>
> You are trying to make it succeed. Find the shape of work where content-addressed cross-run
> reuse with transitive invalidation delivers the most value, and specify it precisely.
>
> Two hard constraints:
>
> 1. It must be a task a real customer would plausibly pay to run, repeatedly. Not a synthetic
>    microbenchmark. Name the customer and the reason they run it more than once.
>
> 2. It must have a PROGRAMMATIC grader: a deterministic function that takes the run's final
>    output and returns pass/fail or a numeric score, with no LLM in the loop. Ground truth must be
>    exact and free to obtain [...]
>
> Also specify: roughly how many operations one run involves, the shape of the dependency graph,
> and what it concretely means to perturb 2% of the inputs on this workload [...]

It was told nothing about provider prompt caching, nothing about an expected outcome, and nothing
about the two failure-mode checklists.

## What it designed

**EVIDENCE-LEDGER: nightly cross-module security & license audit of a seeded monorepo**

- operations per run: **6810**
- dependency shape: **A wide-shallow DAG with a log-depth reduce tail; total depth ~16 levels, maximum width 2,000.**

### Customer

A compliance-automation vendor (think Semgrep/Endor/Vanta-shaped) runs an "evidence-grade" audit for each customer's monorepo: taint paths from untrusted input to SQL sinks, hardcoded secrets, copyleft licenses linked into redistributed binaries, and *reachable* vulnerable dependencies. The output is a signed findings ledger that gates CI and is filed as SOC2/PCI evidence, so every finding must be reproducible and attributable to specific file:line evidence — a summary is worthless, the ledger is the product. They run it on every PR (~200/day/customer) and nightly on main (365/yr), against a repo of ~2,000 first-party source files plus ~300 vendored third-party packages. Between consecutive runs 1-3% of files change, and roughly 40% of those diffs are cosmetic (formatting, comments, local renames) that change no analysis-relevant fact. The vendored dependency corpus is near-identical across all of the vendor's customers. Today a full-fidelity run costs real money in model calls per invocation and cannot be run per-PR at that price; the whole business case for this product is that the marginal run is cheap. That is exactly the condition content-addressed reuse creates, and it is why they pay for it repeatedly rather than once.

### Why reuse wins here, in the designer's own words

Cost is concentrated in 2,000 single-file model calls plus 300 single-package license calls, so leaf invalidation scales with the number of changed files rather than repo size — nothing in the pipeline ever reads more than one artifact at the leaf layer. Above that, three mechanisms compound. (1) Early cutoff: facts are extracted into a prose-free canonical JSON schema, so the 40% of diffs that are comments/formatting/local renames re-run exactly one `extract_facts` op each, produce a byte-identical output, and stop dead — every descendant key is unchanged and still hits. (2) Bounded blast radius: cross-module dataflow is a fixed 3-round layered propagation over an import DAG with out-degree 2, so one changed module dirties at most 7 flow ops, and aggregation is a balanced binary reduce tree so it dirties log2(320) ≈ 9 reduce nodes rather than a monolithic summarizer. (3) Cross-tenant collision: the 1,220 ops over `vendor/` and the pinned advisory snapshot depend only on third-party bytes identical across every customer of the compliance vendor, so the second tenant gets 300 license-classification model calls free. Net effect at the reference scale: 2% perturbation executes ~460 of 6,810 ops (93% reuse), and the customer runs this ~200 times a day per repo with 1-3% diffs, which is precisely the regime where the store pays for itself many times over per day.

### Perturbation semantics

2% = 40 of the 2,000 first-party source files, chosen by a seeded RNG, plus 6 of 300 vendored package versions bumped in `requirements.lock` and `METADATA.json`, plus 2 advisory JSON files edited (one moving a range boundary so a VULNDEP finding flips). The 40 files split by a fixed distribution: 16 COSMETIC (comment text rewritten and blank lines shuffled with total line count preserved, local variables renamed) — the file's content hash changes so `read_file` and `extract_facts` must re-run, but the canonical prose-free `FileFacts` output is byte-identical, so early cutoff fires and nothing downstream is invalidated; 12 LOCAL_SEMANTIC (a PURE function added or deleted, a literal changed) — `FileFacts` changes, invalidating that file's `module_summary`, its module's 3 `flow_round` ops, rounds 2-3 for its ~2 importers, that package's `package_rollup`, and the 9-node path up the reduce tree; 8 DEFECT_TOGGLE (a sanitizer inserted into or removed from a taint chain, a new SOURCE→SINK chain planted, a secret literal added or removed) — same invalidation plus `truth.json` itself changes, so findings appear and disappear and a system serving stale cached answers fails G2 visibly; 4 EDGE_CHANGE (one import added or removed from a module's dependency set, keeping the graph acyclic) — this changes the *shape* of the DAG, so the affected `flow_round` op's key changes structurally rather than only by upstream content, which is the case naive fingerprinting schemes get wrong. The prompt templates, model id, sampling params, `OP_VERSION` table, and advisory server behaviour are all held fixed, so nothing else may legitimately invalidate. Expected outcome: ~460 of 6,810 ops execute (93% reuse), with `required_min` ≈ 390 ops that provably must run and `required_max` ≈ 540 bounding what may run; anything outside that band is a grader failure, not a performance result.

### Grader

`grade.py`, fully deterministic, no LLM. Inputs: `ledger_A.json` (cold run output), `ledger_B.json` (warm run on the perturbed corpus), `ledger_C.json` (cold control on the perturbed corpus), the three `metrics_*.json` files, and generator-emitted `truth_v0.json`, `truth_v1.json`, `required_v1.json`. A finding is a true positive only if its `id` (computed by the published sha256 formula) matches a truth record AND every field of its `evidence` object matches exactly — for TAINT that includes the ordered `path` array of `file:line` strings, so guessing is impossible. G1: F1(ledger_A, truth_v0) ≥ 0.90. G2: F1(ledger_B, truth_v1) ≥ F1_A − 0.01 — this is the stale-answer gate. G3 (hard pass/fail, the invalidation-correctness check): the generator emits `required_min` (ops that provably must re-run, i.e. the DAG closure over mutated artifacts, pruned at cosmetically-mutated files whose facts are provably unchanged) and `required_max` (the same closure ignoring early cutoff); the grader asserts `required_min ⊆ executed_op_ids(B) ⊆ required_max`, failing on unsound reuse in one direction and phantom invalidation / non-hermetic ops in the other, and reports `|required_min| / |executed|` as invalidation precision. G4 (diagnostic, non-gating): `jcs(ledger_B) == jcs(ledger_C)` byte-for-byte. Final score: 0.0 if any of G1-G3 fails, else `reuse_yield = 1 − usd_cost_B / usd_cost_C` in [0,1], with `token_yield` and `op_yield` reported alongside. Ground truth is free and exact because the corpus and every planted defect are emitted by a seeded pure-function generator, and the mutator recomputes truth for the perturbed corpus by construction.

## How it was implemented, and what was changed

Implemented as the `dag` family in `families/dag.py`, preserving the three mechanisms the designer
relied on:

- **bounded blast radius** — per-file fact extraction is fan-in 1, so one changed file dirties its
  own extraction and a bounded set of descendants rather than the whole run.
- **early cutoff** — facts are extracted into a canonical, prose-free JSON form, so a change that
  alters no analysis-relevant fact re-runs exactly one operation, produces byte-identical output,
  and stops dead. This was observed firing in the pilot: arm C served 16 of 20 operations where the
  conservative ceiling predicted 12, because the re-run extraction reproduced its stored output.
- **narrow aggregation** — the roll-up is a balanced binary reduce tree, so one changed leaf dirties
  log2(n) nodes rather than a monolithic summariser.

Two deviations, both recorded before the first matrix cell:

1. **Scale.** The reference design is 6810 operations per run; the full matrix at
   that scale is far beyond budget. The implementation is a fixed-factor reduction, and reuse is
   reported as a function of run size so the scaling is visible rather than assumed.

2. **Scoring.** The designer's grader defines `reuse_yield = 1 - usd_cost_B / usd_cost_C` where its
   `ledger_C` is a *cold control* — a run with no reuse and no provider cache. That is the arm A
   comparison the brief forbids as a headline, because it credits the reuse layer with savings the
   provider was already giving away. The correctness gates (G1 finding-F1, G2 the stale-answer gate,
   G3 the invalidation-soundness bound `required_min ⊆ executed ⊆ required_max`) are preserved in
   spirit; the cost comparison is against tuned arm B instead.
