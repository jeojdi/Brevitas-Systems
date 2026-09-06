"""The pre-committed matrix, resumable, blinded.

Scheduling note on cold cells. "Cold" means the provider cache for that family's shared prefix has
expired, and it is verified from `cache_read_input_tokens == 0` on the run's first operation, never
from the clock. The brief forbids faking it by altering the prefix, since that would change arm B's
workload, so the only honest way to make a cell cold is to leave the prefix untouched for longer
than the TTL. That serialises: every warm cell of a family re-warms its prefix.

The scheduler therefore tracks the last touch per family and refuses to start a cold cell until the
gap exceeds `COLD_GAP_S`. Cells that cannot run yet are deferred, not skipped, and the run is
resumable so an interruption costs nothing.

Run: python3 -m experiments.reuse.harness.matrix [--phase warm|cold] [--model M] [--seeds N]
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

from ..families import chain as chain_family, dag as dag_family, wide as wide_family
from . import perturb
from .costmodel import (
    DEPLOYMENT_HITS_PER_PERIOD, STALENESS_BOUND, arm_c_total, price_sheet_hash,
    shadow_rate_for_bound,
)
from .ledger import ArmBlinding, Ledger
from .runner import run_once
from .store import ReuseStore

FAMILIES = {"wide": wide_family, "chain": chain_family, "dag": dag_family}
PERTURBATIONS = (0.0, 0.02, 0.10, 0.30)
COLD_GAP_S = 4200  # 70 minutes, comfortably beyond the 1h extended TTL that arm B enables
ROOT = "experiments/reuse"
SHADOW_SAMPLE_RATE = 0.20  # same rate for every family, so precision does not differ by family


def cell_records(
    *, family_name: str, size: int, seed: int, pert: float, temperature: str,
    model: str, blinding: ArmBlinding, store_path: str, cwd: str, shadow: bool,
    gap_before_repeat_s: float = 0.0,
) -> list[dict[str, Any]]:
    """Run one (family, size, seed, perturbation, temperature) point across arms B and C."""
    family = FAMILIES[family_name]
    artifacts = family.generate(seed, size)
    key0 = family.answer_key(artifacts)
    run_tag = f"{family_name}-s{seed}-n{size}"

    # ---- day one: a shared first run that populates the store ------------------------------
    store = ReuseStore(store_path)
    day1 = run_once(
        family=family, artifacts=artifacts, arm="C", model=model,
        run_id=f"{run_tag}-day1", cwd=cwd, store=store, write_store=True,
    )
    reference = dict(day1.per_op_cost)

    # ---- perturb ---------------------------------------------------------------------------
    positions = day1.graph.artifact_positions()
    mutator = getattr(family, "mutate", None)
    perturbed, records = perturb.apply(artifacts, positions, pert, seed, family_mutator=mutator)
    key1 = family.answer_key(perturbed)
    semantic = perturb.verify_answers_changed(
        key0, key1, records, answer_key_of=family.answer_key,
        artifacts=artifacts, perturbed_artifacts=perturbed,
    )
    pos_report = perturb.position_report(records, positions)
    changed = {r.artifact for r in records}
    ceiling = day1.graph.ceiling(changed, cost_of=reference)

    os.makedirs(f"{ROOT}/dependency_graphs", exist_ok=True)
    day1.graph.save(f"{ROOT}/dependency_graphs/{run_tag}-p{int(pert*100)}-{temperature}.json")

    # ---- the TTL gap, if this is a cold cell -------------------------------------------------
    # This has to sit HERE, between the populating run and the repeat runs, not between cells.
    # Both adversarial reviewers found the original placement independently: day1 warms the family
    # prefix and arm B ran seconds later inside the same call, so a between-cell gap made *day1*
    # cold and arm B never. A cold arm B was structurally unreachable, which is why the primary
    # metric had no data.
    if gap_before_repeat_s > 0:
        time.sleep(gap_before_repeat_s)

    out: list[dict[str, Any]] = []

    # ---- arm B -------------------------------------------------------------------------------
    armb = run_once(
        family=family, artifacts=perturbed, arm="B", model=model,
        run_id=f"{run_tag}-p{int(pert*100)}-{temperature}-B", cwd=cwd, store=None,
    )
    gb = family.grade(armb.outputs, key1, perturbed)

    # ---- arm C, on exactly the same provider configuration -----------------------------------
    armc = run_once(
        family=family, artifacts=perturbed, arm="C", model=model,
        run_id=f"{run_tag}-p{int(pert*100)}-{temperature}-C", cwd=cwd, store=store,
        write_store=True, shadow_rate=SHADOW_SAMPLE_RATE if shadow else 0.0, shadow_seed=seed,
        reference_costs=dict(armb.per_op_cost),
    )
    gc = family.grade(armc.outputs, key1, perturbed)

    shadow_rate = shadow_rate_for_bound()
    totals = arm_c_total(
        executed_cost=armc.cost,
        served_hits_cost_if_recomputed=armc.served_cost_if_recomputed,
        shadow_rate=shadow_rate,
        store_bytes=store.storage_bytes(),
        runs_amortised_over=30,
    )
    evidence = store.cross_run_evidence()
    store.close()

    common = {
        "family": family_name, "perturbation": pert, "temperature": temperature, "seed": seed,
        "run_size": size, "model": model, "price_sheet_sha256": price_sheet_hash(),
        "n_perturbed_artifacts": len(records),
        "perturbed_artifacts": [r.to_json() for r in records],
        "n_inert_perturbations": semantic["n_inert_perturbations"],
        "position_max_deviation": pos_report["max_share_deviation"],
        "position_report": pos_report,
        "ceiling_count_weighted": ceiling["count_weighted"],
        "ceiling_cost_weighted": ceiling["cost_weighted"],
        "day1_cost_usd": day1.cost.total,
        "deployment_hits_per_period": DEPLOYMENT_HITS_PER_PERIOD,
        "staleness_bound": STALENESS_BOUND,
    }

    out.append({
        **common,
        "arm_label": blinding.label("B"),
        "cost_usd_headline": armb.cost.total,
        "cost_usd_shadow_free": armb.cost.total,
        "cost_usd_executed": armb.cost.total,
        "cost_breakdown": armb.cost.to_json(),
        "shadow_usd": 0.0, "storage_usd": 0.0,
        "ops_total": len(armb.graph), "ops_served": 0, "model_ops_served": 0,
        "model_ops_executed": armb.model_ops_executed,
        "reuse_count_weighted": 0.0, "reuse_cost_weighted": 0.0,
        "achieved_over_ceiling": 1.0,
        "grader_passed": bool(gb["passed"]), "grader_score": gb["score"],
        "cache_read_fraction": armb.cache_read_fraction,
        "first_op_cache_read_tokens": armb.first_op_cache_read_tokens,
        # A run that executed no model calls has no first operation, so a zero here means "no
        # request was made", not "the cache was cold". Requiring at least one executed model op
        # stops the flag reading True for arm C rows that served everything from the store.
        "verified_cold": armb.model_ops_executed > 0 and armb.first_op_cache_read_tokens == 0,
        "cross_run_hit_fraction": 0.0,
        "shadow_n": 0, "shadow_exact_match_rate": 0.0,
        "retries": armb.retries, "model_versions": sorted(armb.model_versions),
        "wall_seconds": armb.wall_seconds,
    })

    served_total = armc.ops_served
    achieved = served_total / max(1, len(armc.graph))
    cost_weighted_reuse = (
        armc.served_cost_if_recomputed / (armc.served_cost_if_recomputed + armc.cost.total)
        if (armc.served_cost_if_recomputed + armc.cost.total) else 0.0
    )
    shadow_exact = (
        sum(1 for s in armc.shadow_records if s["exact_match"]) / len(armc.shadow_records)
        if armc.shadow_records else 0.0
    )

    out.append({
        **common,
        "arm_label": blinding.label("C"),
        "cost_usd_headline": totals["headline_usd"],
        "cost_usd_shadow_free": totals["shadow_free_usd"],
        "cost_usd_executed": totals["executed_usd"],
        "cost_breakdown": armc.cost.to_json(),
        "shadow_usd": totals["shadow_usd"], "storage_usd": totals["storage_usd"],
        "shadow_rate_applied": totals["shadow_rate"],
        "ops_total": len(armc.graph), "ops_served": served_total,
        "model_ops_served": armc.model_ops_served,
        "model_ops_executed": armc.model_ops_executed,
        "tool_ops_served": armc.tool_ops_served,
        "reuse_count_weighted": achieved,
        "reuse_cost_weighted": cost_weighted_reuse,
        "achieved_over_ceiling": (
            achieved / ceiling["count_weighted"] if ceiling["count_weighted"] else 1.0
        ),
        "grader_passed": bool(gc["passed"]), "grader_score": gc["score"],
        "cache_read_fraction": armc.cache_read_fraction,
        "first_op_cache_read_tokens": armc.first_op_cache_read_tokens,
        "verified_cold": armc.model_ops_executed > 0 and armc.first_op_cache_read_tokens == 0,
        "cross_run_hit_fraction": evidence["cross_run_fraction"],
        "max_write_to_read_seconds": evidence["max_write_to_read_seconds"],
        "store_bytes": store_bytes_safe(store_path),
        "shadow_n": len(armc.shadow_records),
        "shadow_exact_match_rate": shadow_exact,
        "shadow_records": armc.shadow_records,
        "retries": armc.retries, "model_versions": sorted(armc.model_versions),
        "wall_seconds": armc.wall_seconds,
    })
    return out


def store_bytes_safe(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="warm", choices=("warm", "cold"))
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--size", type=int, default=12)
    ap.add_argument("--families", default="wide,chain,dag")
    ap.add_argument("--perturbations", default="")
    ap.add_argument("--ledger", default=f"{ROOT}/ledger.jsonl")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    perts = (
        tuple(float(x) for x in args.perturbations.split(",")) if args.perturbations
        else PERTURBATIONS
    )
    families = args.families.split(",")
    blinding = ArmBlinding(f"{ROOT}/arm_mapping.json")
    ledger = Ledger(args.ledger)
    cwd = "/private/tmp"
    os.makedirs(f"{ROOT}/stores", exist_ok=True)

    # Pre-committed cell order. Deliberately not sorted by anything result-dependent.
    matrix = []
    for family in families:
        for pert in perts:
            for seed in range(args.seeds):
                for arm in ("B", "C"):
                    matrix.append({
                        "family": family, "perturbation": pert, "seed": seed,
                        "arm_label": blinding.label(arm), "temperature": args.phase,
                        "run_size": args.size,
                    })

    pending = ledger.pending_cells(matrix)
    points = sorted({(c["family"], c["perturbation"], c["seed"]) for c in pending})
    print(
        f"phase={args.phase} model={args.model} size={args.size} "
        f"{len(points)} points pending ({len(pending)} ledger cells)"
    )

    process_start = time.time()
    last_touch: dict[str, float] = {}
    deferred: list[tuple] = []
    queue = list(points)
    done = 0

    while queue:
        point = queue.pop(0)
        family_name, pert, seed = point

        if args.phase == "cold":
            # The warm matrix ran immediately before this phase, so a family's prefix is hot even
            # on its first cold point. Seeding last_touch with the process start time forces the
            # wait for the first point too; without it the first cold cell of every family runs
            # against a warm cache and is recorded verified_cold=False.
            gap = time.time() - last_touch.get(family_name, 0.0)
            if gap < COLD_GAP_S:
                if all(p[0] == family_name for p in queue) and not queue:
                    wait = COLD_GAP_S - gap
                    print(f"  waiting {wait / 60:.0f} min for {family_name} prefix to expire")
                    time.sleep(min(wait + 30, COLD_GAP_S))
                else:
                    queue.append(point)  # rotate to another family and come back
                    deferred.append(point)
                    if len(deferred) > len(points) * 3:
                        wait = COLD_GAP_S - gap
                        print(f"  all families hot; sleeping {wait / 60:.0f} min")
                        time.sleep(max(60, min(wait + 30, COLD_GAP_S)))
                        deferred.clear()
                    continue

        store_path = f"{ROOT}/stores/{family_name}-s{seed}-n{args.size}-p{int(pert*100)}-{args.phase}.sqlite"
        started = time.time()
        try:
            rows = cell_records(
                family_name=family_name, size=args.size, seed=seed, pert=pert,
                temperature=args.phase, model=args.model, blinding=blinding,
                store_path=store_path, cwd=cwd, shadow=True,
                gap_before_repeat_s=(COLD_GAP_S if args.phase == "cold" else 0.0),
            )
        except Exception as exc:  # a failed point must not lose the rest of the matrix
            print(f"  FAILED {point}: {type(exc).__name__}: {exc}")
            continue

        for row in rows:
            if args.tag:
                row["tag"] = args.tag
            ledger.append(row)
        last_touch[family_name] = time.time()
        done += 1
        b, c = rows[0], rows[1]
        saving = (
            (b["cost_usd_headline"] - c["cost_usd_headline"]) / b["cost_usd_headline"]
            if b["cost_usd_headline"] else 0.0
        )
        print(
            f"  [{done}/{len(points)}] {family_name} p={pert:.0%} seed={seed} "
            f"({time.time() - started:.0f}s) "
            f"B=${b['cost_usd_headline']:.5f} C=${c['cost_usd_headline']:.5f} "
            f"served={c['ops_served']}/{c['ops_total']} "
            f"cold={c['verified_cold']} grade B/C={b['grader_score']:.2f}/{c['grader_score']:.2f}"
        )

    print(f"phase {args.phase} complete: {done} points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
