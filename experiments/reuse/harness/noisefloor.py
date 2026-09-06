"""Noise floor: how much do two fresh runs disagree with each other anyway?

§9 of the brief requires this before anything is measured about reuse. A model call is a sample
from a distribution, not a function, so two fresh runs of the same task will not match. Any
staleness figure is meaningless until the rate at which the system disagrees with *itself* is
known.

This also measures cost variance between identical fresh runs, which turns out to matter as much
as the correctness floor: if run-to-run cost varies by more than the marginal saving being
claimed, a single-seed comparison of arm B against arm C measures noise.

Run: python3 -m experiments.reuse.harness.noisefloor [family] [size] [repeats]
"""

from __future__ import annotations

import json
import os
import statistics
import sys

from ..families import chain as chain_family, dag as dag_family, wide as wide_family
from .runner import run_once

MODEL = os.environ.get("REUSE_MODEL", "claude-haiku-4-5-20251001")
FAMILIES = {"wide": wide_family, "chain": chain_family, "dag": dag_family}


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "wide"
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    repeats = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    family = FAMILIES[name]
    artifacts = family.generate(0, size)
    key = family.answer_key(artifacts)

    print(f"noise floor: family={name} size={size} repeats={repeats} model={MODEL}")
    runs = []
    for r in range(repeats):
        res = run_once(
            family=family, artifacts=artifacts, arm="B", model=MODEL,
            run_id=f"noise-{name}-{r}", cwd="/private/tmp", store=None,
        )
        g = family.grade(res.outputs, key, artifacts)
        runs.append({
            "repeat": r,
            "cost": res.cost.total,
            "input_usd": res.cost.input_usd,
            "output_usd": res.cost.output_usd,
            "write_usd": res.cost.cache_write_usd,
            "read_usd": res.cost.cache_read_usd,
            "read_fraction": res.cache_read_fraction,
            "score": g["score"],
            "passed": g["passed"],
            "outputs": {k: v for k, v in res.outputs.items()},
            "per_item": g.get("per_item", {}),
        })
        print(
            f"  run {r}: cost=${res.cost.total:.5f} "
            f"(in={res.cost.input_usd:.5f} out={res.cost.output_usd:.5f} "
            f"write={res.cost.cache_write_usd:.5f} read={res.cost.cache_read_usd:.5f}) "
            f"read_frac={res.cache_read_fraction:.3f} score={g['score']:.3f} passed={g['passed']}"
        )

    # ---- correctness floor -------------------------------------------------------------
    op_ids = sorted(set().union(*[set(r["outputs"]) for r in runs]))
    exact_disagree = 0
    for op in op_ids:
        values = {r["outputs"].get(op, "") for r in runs}
        if len(values) > 1:
            exact_disagree += 1
    items = sorted(set().union(*[set(r["per_item"]) for r in runs]))
    outcome_disagree = 0
    for item in items:
        values = {r["per_item"].get(item) for r in runs}
        if len(values) > 1:
            outcome_disagree += 1

    costs = [r["cost"] for r in runs]
    scores = [r["score"] for r in runs]

    floor = {
        "family": name, "size": size, "repeats": repeats, "model": MODEL,
        "exact_output_disagreement_rate": exact_disagree / len(op_ids) if op_ids else 0.0,
        "grader_outcome_disagreement_rate": outcome_disagree / len(items) if items else 0.0,
        "score_mean": statistics.fmean(scores),
        "score_stdev": statistics.stdev(scores) if len(scores) > 1 else 0.0,
        "cost_mean": statistics.fmean(costs),
        "cost_stdev": statistics.stdev(costs) if len(costs) > 1 else 0.0,
        "cost_cv": (statistics.stdev(costs) / statistics.fmean(costs)) if len(costs) > 1 and statistics.fmean(costs) else 0.0,
        "cost_min": min(costs), "cost_max": max(costs),
        "cost_spread_pct": (max(costs) - min(costs)) / statistics.fmean(costs) if statistics.fmean(costs) else 0.0,
        "runs": [{k: v for k, v in r.items() if k != "outputs"} for r in runs],
    }

    print()
    print(f"exact-output disagreement between fresh runs : {floor['exact_output_disagreement_rate']:.3f}")
    print(f"grader-outcome disagreement between runs     : {floor['grader_outcome_disagreement_rate']:.3f}")
    print(f"grader score mean/stdev                      : {floor['score_mean']:.3f} / {floor['score_stdev']:.3f}")
    print(f"cost mean/stdev, coefficient of variation    : ${floor['cost_mean']:.5f} / ${floor['cost_stdev']:.5f} / {floor['cost_cv']:.3f}")
    print(f"cost spread across identical fresh runs      : {floor['cost_spread_pct']:.1%}")
    if floor["cost_spread_pct"] > 0.10:
        print(
            "  NOTE: run-to-run cost spread exceeds 10%. Any marginal saving smaller than this "
            "cannot be resolved by a single seed, and seeds must be pooled before comparing arms."
        )

    os.makedirs("experiments/reuse/pilot", exist_ok=True)
    with open(f"experiments/reuse/pilot/noisefloor_{name}.json", "w", encoding="utf-8") as h:
        json.dump(floor, h, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
