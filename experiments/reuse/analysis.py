"""Pre-registered analysis. Frozen and hashed before the experiment runs.

This script reads `ledger.jsonl` and NOTHING ELSE. It never opens `arm_mapping.json`; every
metric is computed over opaque labels, and the mapping is applied afterwards by `unblind.py`.

On the limits of blinding, stated plainly because overstating it would itself be a dishonesty:
arm identity is partially *inferable* from ledger content — only the reuse arm records store hits,
and only the no-cache arm records zero cache activity. So blinding here does not hide which arm is
which. What it does hide is which arm is which **while the metric definitions are being written
and debugged**, which is where the nudging risk actually lives. That is a real but partial control
and the report says so.

Run: python3 -m experiments.reuse.analysis <ledger.jsonl> [out.json]
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from typing import Any

SCHEMA_VERSION = "reuse-analysis-v1"

# Pre-registered decision bands, from §11 of the brief. Not modifiable after seeing results.
BANDS = ((0.10, "dead. Provider caching already captures the value."),
         (0.25, "not a standalone business. Possibly a feature."),
         (float("inf"), "live. Proceed to the commercial questions."))

PRIMARY_PERTURBATION = 0.02
PRIMARY_TEMPERATURE = "cold"

ARM_B_READ_FRACTION_MIN = 0.50
ARM_C_CEILING_MIN = 0.95
CORRECTNESS_TOLERANCE_PP = 2.0
POSITION_DEVIATION_MAX = 0.15


def verdict_for(saving: float) -> str:
    for threshold, text in BANDS:
        if saving < threshold:
            return text
    return BANDS[-1][1]


def load(path: str) -> list[dict[str, Any]]:
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # torn final line from an interrupted run
    return records


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _stdev(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Pool seeds within each (family, perturbation, label, temperature, size) cell.

    Seeds are pooled before arms are compared because run-to-run cost variance is real; comparing
    single seeds would measure sampling noise. Never pools across families (false-positive item 7).
    """
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        groups[(r["family"], r["perturbation"], r["arm_label"], r["temperature"],
                r.get("run_size", 0))].append(r)

    cells: dict[str, Any] = {}
    for (family, pert, label, temp, size), rows in sorted(groups.items(), key=lambda kv: str(kv[0])):
        costs = [r["cost_usd_headline"] for r in rows]
        cells[f"{family}|{pert}|{label}|{temp}|{size}"] = {
            "family": family, "perturbation": pert, "arm_label": label,
            "temperature": temp, "run_size": size, "n_seeds": len(rows),
            "cost_headline_mean": _mean(costs),
            "cost_headline_stdev": _stdev(costs),
            "cost_shadow_free_mean": _mean([r["cost_usd_shadow_free"] for r in rows]),
            "cost_executed_mean": _mean([r["cost_usd_executed"] for r in rows]),
            "shadow_usd_mean": _mean([r.get("shadow_usd", 0.0) for r in rows]),
            "storage_usd_mean": _mean([r.get("storage_usd", 0.0) for r in rows]),
            "ops_total_mean": _mean([float(r["ops_total"]) for r in rows]),
            "ops_served_mean": _mean([float(r["ops_served"]) for r in rows]),
            "model_ops_served_mean": _mean([float(r.get("model_ops_served", 0)) for r in rows]),
            "reuse_count_weighted": _mean([r["reuse_count_weighted"] for r in rows]),
            "reuse_cost_weighted": _mean([r["reuse_cost_weighted"] for r in rows]),
            "ceiling_count_weighted": _mean([r["ceiling_count_weighted"] for r in rows]),
            "ceiling_cost_weighted": _mean([r["ceiling_cost_weighted"] for r in rows]),
            "achieved_over_ceiling": _mean([r["achieved_over_ceiling"] for r in rows]),
            "grader_pass_rate": _mean([1.0 if r["grader_passed"] else 0.0 for r in rows]),
            "grader_score_mean": _mean([r["grader_score"] for r in rows]),
            "grader_score_stdev": _stdev([r["grader_score"] for r in rows]),
            "cache_read_fraction": _mean([r.get("cache_read_fraction", 0.0) for r in rows]),
            "first_op_cache_read_mean": _mean([float(r.get("first_op_cache_read_tokens", 0)) for r in rows]),
            "cross_run_hit_fraction": _mean([r.get("cross_run_hit_fraction", 0.0) for r in rows]),
            "position_max_deviation": max([r.get("position_max_deviation", 0.0) for r in rows] or [0.0]),
            "inert_perturbations": sum(r.get("n_inert_perturbations", 0) for r in rows),
            "n_perturbed": sum(r.get("n_perturbed_artifacts", 0) for r in rows),
            "shadow_exact_match_rate": _mean(
                [r["shadow_exact_match_rate"] for r in rows if r.get("shadow_n", 0) > 0]
            ),
            "shadow_n": sum(r.get("shadow_n", 0) for r in rows),
            "retries": sum(r.get("retries", 0) for r in rows),
            "model_versions": sorted({v for r in rows for v in r.get("model_versions", [])}),
        }
    return cells


def pairwise_savings(cells: dict[str, Any]) -> list[dict[str, Any]]:
    """Marginal saving for every ordered label pair, in every (family, perturbation, temp, size).

    Computed over labels, so the analysis cannot be steered toward a favoured arm while it is
    being written. Which pair is the primary metric is resolved at unblinding.
    """
    by_context: dict[tuple, dict[str, Any]] = defaultdict(dict)
    for cell in cells.values():
        key = (cell["family"], cell["perturbation"], cell["temperature"], cell["run_size"])
        by_context[key][cell["arm_label"]] = cell

    out: list[dict[str, Any]] = []
    for (family, pert, temp, size), labelled in sorted(by_context.items(), key=lambda kv: str(kv[0])):
        for base_label, base in labelled.items():
            for other_label, other in labelled.items():
                if base_label == other_label or base["cost_headline_mean"] <= 0:
                    continue
                out.append({
                    "family": family, "perturbation": pert, "temperature": temp, "run_size": size,
                    "baseline_label": base_label, "candidate_label": other_label,
                    "baseline_cost": base["cost_headline_mean"],
                    "candidate_cost": other["cost_headline_mean"],
                    "marginal_saving": (base["cost_headline_mean"] - other["cost_headline_mean"])
                                       / base["cost_headline_mean"],
                    "marginal_saving_shadow_free": (
                        base["cost_shadow_free_mean"] - other["cost_shadow_free_mean"]
                    ) / base["cost_shadow_free_mean"] if base["cost_shadow_free_mean"] else 0.0,
                    "grader_pass_delta_pp": (other["grader_pass_rate"] - base["grader_pass_rate"]) * 100.0,
                    "grader_variance_ratio": (
                        other["grader_score_stdev"] / base["grader_score_stdev"]
                    ) if base["grader_score_stdev"] else None,
                    "candidate_ops_served": other["ops_served_mean"],
                    "baseline_ops_served": base["ops_served_mean"],
                })
    return out


def gates(cells: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Every gate in §5 of the pre-registration, computed over labels."""
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cell in cells.values():
        by_label[cell["arm_label"]].append(cell)

    label_health = {}
    for label, group in sorted(by_label.items()):
        multi = [c for c in group if c["ops_total_mean"] > 1]
        served = [c for c in group if c["ops_served_mean"] > 0]
        zero_pert = [c for c in group if c["perturbation"] == 0.0 and c["ops_served_mean"] > 0]
        label_health[label] = {
            "cache_read_fraction": _mean([c["cache_read_fraction"] for c in multi]),
            "passes_arm_b_health": _mean([c["cache_read_fraction"] for c in multi]) >= ARM_B_READ_FRACTION_MIN,
            "records_store_hits": bool(served),
            "achieved_over_ceiling_at_0pct": _mean([c["achieved_over_ceiling"] for c in zero_pert]) if zero_pert else None,
            "passes_arm_c_health": (
                _mean([c["achieved_over_ceiling"] for c in zero_pert]) >= ARM_C_CEILING_MIN
            ) if zero_pert else None,
            "cross_run_hit_fraction": _mean([c["cross_run_hit_fraction"] for c in served]) if served else 0.0,
            "grader_pass_rate": _mean([c["grader_pass_rate"] for c in group]),
            "n_cells": len(group),
        }

    warm = defaultdict(int)
    cold = defaultdict(int)
    for cell in cells.values():
        if cell["ops_total_mean"] > 0:
            (warm if cell["temperature"] == "warm" else cold)[cell["family"]] += 1

    families = sorted({c["family"] for c in cells.values()})
    return {
        "label_health": label_health,
        "cold_cells_not_truncated": {
            "per_family": {f: {"warm": warm.get(f, 0), "cold": cold.get(f, 0)} for f in families},
            "passes": all(cold.get(f, 0) == warm.get(f, 0) for f in families),
        },
        "perturbations_semantic": {
            "total_perturbed": sum(c["n_perturbed"] for c in cells.values()),
            "total_inert": sum(c["inert_perturbations"] for c in cells.values()),
            "passes": sum(c["inert_perturbations"] for c in cells.values()) == 0,
        },
        "position_uniform": {
            "max_deviation": max([c["position_max_deviation"] for c in cells.values()] or [0.0]),
            "passes": max([c["position_max_deviation"] for c in cells.values()] or [0.0]) < POSITION_DEVIATION_MAX,
        },
        "single_model_version": {
            "versions": sorted({v for c in cells.values() for v in c["model_versions"]}),
            "passes": len({v for c in cells.values() for v in c["model_versions"]}) <= 1,
        },
        "size_curve_present": {
            "sizes_per_family": {f: sorted({c["run_size"] for c in cells.values() if c["family"] == f})
                                 for f in families},
            "passes": all(len({c["run_size"] for c in cells.values() if c["family"] == f}) >= 2
                          for f in families),
        },
    }


def decay_curves(cells: dict[str, Any]) -> dict[str, Any]:
    """Reuse and saving as a function of perturbation, per family and label. Never pooled."""
    curves: dict[str, Any] = defaultdict(dict)
    for cell in cells.values():
        if cell["ops_served_mean"] <= 0:
            continue
        key = f"{cell['family']}|{cell['arm_label']}|{cell['temperature']}|{cell['run_size']}"
        curves[key][str(cell["perturbation"])] = {
            "reuse_count_weighted": cell["reuse_count_weighted"],
            "reuse_cost_weighted": cell["reuse_cost_weighted"],
            "ceiling_count_weighted": cell["ceiling_count_weighted"],
            "ceiling_cost_weighted": cell["ceiling_cost_weighted"],
            "achieved_over_ceiling": cell["achieved_over_ceiling"],
            "is_ceiling_row": cell["perturbation"] == 0.0,
        }
    return dict(curves)


def analyse(path: str) -> dict[str, Any]:
    records = load(path)
    cells = aggregate(records)
    savings = pairwise_savings(cells)
    primary = [
        s for s in savings
        if s["perturbation"] == PRIMARY_PERTURBATION and s["temperature"] == PRIMARY_TEMPERATURE
    ]
    return {
        "schema": SCHEMA_VERSION,
        "n_records": len(records),
        "n_cells": len(cells),
        "cells": cells,
        "pairwise_savings": savings,
        "primary_candidates": sorted(primary, key=lambda s: -s["marginal_saving"]),
        "gates": gates(cells, records),
        "decay_curves": decay_curves(cells),
        "bands": [[b[0], b[1]] for b in BANDS],
        "note": (
            "All figures keyed by opaque arm label. 0% perturbation rows are marked "
            "is_ceiling_row and are a CEILING, not a result. The headline is computed only at "
            f"{PRIMARY_PERTURBATION:.0%} perturbation, {PRIMARY_TEMPERATURE} cache."
        ),
    }


def main() -> int:
    ledger = sys.argv[1] if len(sys.argv) > 1 else "experiments/reuse/ledger.jsonl"
    out = sys.argv[2] if len(sys.argv) > 2 else "experiments/reuse/results_blinded.json"
    result = analyse(ledger)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=1, sort_keys=True)
    print(f"{result['n_records']} records -> {result['n_cells']} cells -> {out}")
    for name, gate in result["gates"].items():
        if isinstance(gate, dict) and "passes" in gate:
            print(f"  gate {name}: {'PASS' if gate['passes'] else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
