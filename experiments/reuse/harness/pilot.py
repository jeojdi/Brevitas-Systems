"""End-to-end pilot of one cell, on the cheapest model.

Validates the whole path before any matrix cell is run: generation, planning, live metering,
dependency recording, perturbation, invalidation, store hits, grading and costing. Nothing here
is a result; it is a wiring check.

Run: python3 -m experiments.reuse.harness.pilot [family] [size] [perturbation]
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

from ..families import chain as chain_family, dag as dag_family, wide as wide_family
from . import perturb
from .costmodel import Cost, arm_c_total, op_cost, shadow_rate_for_bound
from .runner import run_once
from .store import ReuseStore

MODEL = "claude-haiku-4-5-20251001"
FAMILIES = {"wide": wide_family, "chain": chain_family, "dag": dag_family}


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "wide"
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    frac = float(sys.argv[3]) if len(sys.argv) > 3 else 0.30
    family = FAMILIES[name]
    seed = 0
    tmp = tempfile.mkdtemp(prefix="reuse-pilot-")
    store_path = os.path.join(tmp, "store.sqlite")
    cwd = "/private/tmp"

    print(f"family={name} size={size} perturbation={frac:.0%} model={MODEL}")
    artifacts = family.generate(seed, size)
    key0 = family.answer_key(artifacts)
    print(f"generated {len(artifacts)} artifacts, {len(key0)} ground-truth entries")

    # ---- day one: populate the store -------------------------------------------------
    store = ReuseStore(store_path)
    day1 = run_once(
        family=family, artifacts=artifacts, arm="C", model=MODEL, run_id="pilot-day1",
        cwd=cwd, store=store, write_store=True,
    )
    g1 = family.grade(day1.outputs, key0, artifacts)
    print(
        f"day1  arm C(pop): ops={day1.ops_executed} served={day1.ops_served} "
        f"cost=${day1.cost.total:.5f} read_frac={day1.cache_read_fraction:.3f} "
        f"grade={g1['score']:.3f} passed={g1['passed']}"
    )
    reference_costs = dict(day1.per_op_cost)

    # ---- perturb ----------------------------------------------------------------------
    positions = day1.graph.artifact_positions()
    perturbed, records = perturb.apply(artifacts, positions, frac, seed)
    key1 = family.answer_key(perturbed)
    semantic = perturb.verify_answers_changed(key0, key1, records)
    pos_report = perturb.position_report(records, positions)
    print(
        f"perturbed {len(records)} artifacts; answers changed for {semantic['n_answers_changed']} "
        f"keys; all_semantic={semantic['all_perturbations_semantic']}; "
        f"max_stratum_dev={pos_report['max_share_deviation']:.3f}"
    )
    for r in records[:4]:
        print(f"   - {r.artifact}: {r.mutator} :: {r.detail} (depth={r.depth})")

    changed = {r.artifact for r in records}
    ceiling = day1.graph.ceiling(changed, cost_of=reference_costs)
    print(
        f"theoretical ceiling: count={ceiling['count_weighted']:.3f} "
        f"cost={ceiling['cost_weighted']:.3f} "
        f"({ceiling['ops_reusable']}/{ceiling['ops_total']} ops reusable)"
    )

    # ---- day two, arm B ----------------------------------------------------------------
    armb = run_once(
        family=family, artifacts=perturbed, arm="B", model=MODEL, run_id="pilot-day2-B",
        cwd=cwd, store=None,
    )
    gb = family.grade(armb.outputs, key1, perturbed)
    print(
        f"day2  arm B    : ops={armb.ops_executed} cost=${armb.cost.total:.5f} "
        f"read_frac={armb.cache_read_fraction:.3f} grade={gb['score']:.3f} passed={gb['passed']}"
    )

    # ---- day two, arm C ----------------------------------------------------------------
    armc = run_once(
        family=family, artifacts=perturbed, arm="C", model=MODEL, run_id="pilot-day2-C",
        cwd=cwd, store=store, write_store=True, shadow_rate=0.25, shadow_seed=seed,
        reference_costs=dict(armb.per_op_cost),
    )
    gc = family.grade(armc.outputs, key1, perturbed)
    achieved = armc.ops_served / max(1, len(armc.graph))
    gate = achieved / ceiling["count_weighted"] if ceiling["count_weighted"] else 0.0
    print(
        f"day2  arm C    : ops={armc.ops_executed} served={armc.ops_served} "
        f"cost=${armc.cost.total:.5f} grade={gc['score']:.3f} passed={gc['passed']} "
        f"achieved/ceiling={gate:.3f}"
    )

    # ---- costing -----------------------------------------------------------------------
    shadow_rate = shadow_rate_for_bound(0.01, 0.95, max(1, armc.ops_served))
    totals = arm_c_total(
        executed_cost=armc.cost,
        served_hits_cost_if_recomputed=armc.served_cost_if_recomputed,
        shadow_rate=shadow_rate,
        store_bytes=store.storage_bytes(),
        runs_amortised_over=30,
    )
    marginal_headline = (armb.cost.total - totals["headline_usd"]) / armb.cost.total if armb.cost.total else 0.0
    marginal_free = (armb.cost.total - totals["shadow_free_usd"]) / armb.cost.total if armb.cost.total else 0.0
    evidence = store.cross_run_evidence()

    print()
    print(f"marginal saving (shadow+storage included) : {marginal_headline:+.1%}")
    print(f"marginal saving (shadow-free, optimistic) : {marginal_free:+.1%}")
    print(f"  shadow rate to bound staleness at 1%    : {shadow_rate:.3f}")
    print(f"  storage amortised per run               : ${totals['storage_usd']:.6f}")
    print(f"  cross-run hit fraction                  : {evidence['cross_run_fraction']:.3f}")
    print(f"  shadow comparisons                      : {len(armc.shadow_records)}")
    for s in armc.shadow_records[:3]:
        print(f"    {s['op_id']}: exact_match={s['exact_match']}")

    out = {
        "family": name, "size": size, "perturbation": frac, "model": MODEL,
        "ceiling": ceiling, "achieved_over_ceiling": gate,
        "perturbation_semantic": semantic, "position_report": pos_report,
        "arm_b": {"cost": armb.cost.to_json(), "ops": armb.ops_executed,
                  "read_fraction": armb.cache_read_fraction, "grade": gb["score"],
                  "passed": gb["passed"]},
        "arm_c": {"cost": armc.cost.to_json(), "ops": armc.ops_executed,
                  "served": armc.ops_served, "grade": gc["score"], "passed": gc["passed"],
                  "totals": totals},
        "marginal_headline": marginal_headline, "marginal_shadow_free": marginal_free,
        "cross_run": evidence,
        "shadow_records": armc.shadow_records,
        "model_versions": sorted(armb.model_versions | armc.model_versions),
    }
    os.makedirs("experiments/reuse/pilot", exist_ok=True)
    with open(f"experiments/reuse/pilot/pilot_{name}_{int(frac*100)}.json", "w", encoding="utf-8") as h:
        json.dump(out, h, indent=2, default=str)
    day1.graph.save(f"experiments/reuse/pilot/graph_{name}.json")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
