"""Post-hoc arm B-prime: what a changed-file list alone would have achieved.

NOT part of the pre-registered analysis. Added after the warm matrix, recorded in `search_log.md`,
and reported alongside the frozen result rather than replacing it. `analysis.py` is unmodified and
its recorded sha256 still holds.

Why it is necessary. The brief's arm B re-runs the whole workload every time. That is the right
baseline for the question "does reuse beat provider caching", but it is not the cheapest thing a
competent customer would actually do. Every domain in the 24-scenario sweep already ships a coarse
incumbent that costs nothing: `git diff --name-only HEAD@{1}`, SARIF partialFingerprints, a
translation memory, a flake registry. So the commercially decisive question is not

    does arm C beat arm B?                    (re-run everything)

but

    does arm C beat arm B-prime?              (re-run only what directly changed)

because arm B-prime requires no dependency graph, no content addressing and no store - it is a
one-line changed-file query. Whatever arm C wins over arm B-prime is what the dependency tracker
itself is worth; everything else is worth a shell command.

Arm B-prime is deliberately UNSOUND: it recomputes only operations whose own artifacts changed and
serves everything else, with no transitive invalidation. So this script also reports how many
operations it would serve that arm C correctly invalidated - the stale-serve exposure that is the
price of the shell command.

Computed entirely from the recorded dependency graphs (deliverable 2), which is what they exist
for: reuse can be recomputed under a different policy without re-running anything.

Run: python3 -m experiments.reuse.analysis_naive
"""

from __future__ import annotations

import glob
import json
import os
import statistics
from collections import defaultdict
from typing import Any

from .harness.costmodel import op_cost
from .harness.graph import RunGraph

ROOT = "experiments/reuse"


def price_op(op, model_version: str) -> float:
    t = op.tokens or {}
    if not t:
        return 0.0
    return op_cost(
        model_version=model_version,
        input_tokens=int(t.get("input", 0)),
        output_tokens=int(t.get("output", 0)),
        ephemeral_5m_tokens=int(t.get("cache_write_5m", 0)),
        ephemeral_1h_tokens=int(t.get("cache_write_1h", 0)),
        cache_read_tokens=int(t.get("cache_read", 0)),
    ).total


def analyse_graph(path: str, changed: set[str], model_version: str) -> dict[str, Any]:
    graph = RunGraph.load(path)
    costs = {op.op_id: price_op(op, model_version) for op in graph.ops}
    total = sum(costs.values())

    # arm C: transitive invalidation, the sound policy.
    dirty_c = graph.invalidated(changed)
    cost_c = sum(costs[o] for o in dirty_c)

    # arm B-prime: only operations whose OWN artifacts changed. No propagation.
    dirty_bprime = {op.op_id for op in graph.ops if any(a in changed for a in op.artifacts)}
    cost_bprime = sum(costs[o] for o in dirty_bprime)

    # What B-prime serves that C says is invalid: the stale-serve exposure.
    stale_exposure = dirty_c - dirty_bprime

    return {
        "ops": len(graph.ops),
        "cost_full_run": total,
        "cost_arm_c": cost_c,
        "cost_arm_bprime": cost_bprime,
        "n_dirty_c": len(dirty_c),
        "n_dirty_bprime": len(dirty_bprime),
        "n_stale_exposure": len(stale_exposure),
        "stale_exposure_fraction": len(stale_exposure) / max(1, len(graph.ops)),
        "saving_c_over_full": (total - cost_c) / total if total else 0.0,
        "saving_bprime_over_full": (total - cost_bprime) / total if total else 0.0,
        # The number the whole commercial question turns on: what the dependency tracker adds
        # over a changed-file list. Negative means the shell command is cheaper (because it
        # under-invalidates, which is exactly why it is also unsound).
        "tracker_value_over_bprime": (
            (cost_bprime - cost_c) / cost_bprime if cost_bprime else 0.0
        ),
    }


def main() -> int:
    rows = [json.loads(l) for l in open(f"{ROOT}/ledger.jsonl", encoding="utf-8")]
    by_cell: dict[tuple, dict] = {}
    for r in rows:
        if r["ops_served"] <= 0 and r["perturbation"] > 0:
            continue  # keep one row per cell; the reuse arm carries the perturbation record
        by_cell[(r["family"], r["perturbation"], r["seed"], r["temperature"])] = r

    results: dict[tuple, list[dict]] = defaultdict(list)
    for (family, pert, seed, temp), r in sorted(by_cell.items()):
        path = f"{ROOT}/dependency_graphs/{family}-s{seed}-n{r['run_size']}-p{int(pert*100)}-{temp}.json"
        if not os.path.exists(path):
            continue
        changed = {p["artifact"] for p in r.get("perturbed_artifacts", [])}
        version = (r.get("model_versions") or ["claude-haiku-4-5-20251001"])[0]
        results[(family, pert, temp)].append(analyse_graph(path, changed, version))

    print("Arm B-prime: re-run only operations whose own artifacts changed (a changed-file list).")
    print("Arm C: transitive invalidation (the dependency tracker).")
    print("'tracker adds' is what arm C saves OVER arm B-prime. Negative means the shell command")
    print("is cheaper, which it achieves by under-invalidating - see the stale-serve column.\n")
    print(f"{'family':<7}{'pert':>6}{'temp':>6}{'ops':>6}"
          f"{'C saves':>9}{'B-prime saves':>15}{'tracker adds':>14}{'stale-serve':>13}")
    print("-" * 78)

    summary = []
    for (family, pert, temp), group in sorted(results.items()):
        if not group:
            continue
        row = {
            "family": family, "perturbation": pert, "temperature": temp,
            "ops": statistics.fmean([g["ops"] for g in group]),
            "c_saves": statistics.fmean([g["saving_c_over_full"] for g in group]),
            "bprime_saves": statistics.fmean([g["saving_bprime_over_full"] for g in group]),
            "tracker_adds": statistics.fmean([g["tracker_value_over_bprime"] for g in group]),
            "stale": statistics.fmean([g["stale_exposure_fraction"] for g in group]),
            "n": len(group),
        }
        summary.append(row)
        print(f"{family:<7}{pert:>6.0%}{temp:>6}{row['ops']:>6.0f}"
              f"{row['c_saves']:>+9.1%}{row['bprime_saves']:>+15.1%}"
              f"{row['tracker_adds']:>+14.1%}{row['stale']:>+13.1%}")

    with open(f"{ROOT}/results_naive_baseline.json", "w", encoding="utf-8") as h:
        json.dump({"rows": summary, "note": __doc__}, h, indent=1)
    print(f"\nwrote {ROOT}/results_naive_baseline.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
