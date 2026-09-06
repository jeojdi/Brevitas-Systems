"""Tests for the pre-registered analysis, against synthetic ledgers with known answers.

§10 of the brief: write the analysis script, test it against synthetic ledgers with known answers,
then hash it and record the hash, and run the experiment only after that. These are those tests.

Run: python3 -m experiments.reuse.test_analysis
"""

from __future__ import annotations

import json
import os
import tempfile

from .analysis import analyse, verdict_for

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def record(**kw):
    base = {
        "family": "f1", "perturbation": 0.02, "arm_label": "arm_aaaa", "temperature": "cold",
        "seed": 0, "run_size": 10, "cost_usd_headline": 1.0, "cost_usd_shadow_free": 1.0,
        "cost_usd_executed": 1.0, "shadow_usd": 0.0, "storage_usd": 0.0,
        "ops_total": 10, "ops_served": 0, "model_ops_served": 0,
        "reuse_count_weighted": 0.0, "reuse_cost_weighted": 0.0,
        "ceiling_count_weighted": 0.0, "ceiling_cost_weighted": 0.0,
        "achieved_over_ceiling": 1.0, "grader_passed": True, "grader_score": 1.0,
        "cache_read_fraction": 0.8, "cross_run_hit_fraction": 0.0,
        "position_max_deviation": 0.05, "n_inert_perturbations": 0, "n_perturbed_artifacts": 2,
        "shadow_exact_match_rate": 1.0, "shadow_n": 0, "retries": 0,
        "model_versions": ["m-1"], "first_op_cache_read_tokens": 100,
    }
    base.update(kw)
    return base


def write(records, path):
    with open(path, "w", encoding="utf-8") as handle:
        for r in records:
            handle.write(json.dumps(r) + "\n")


def test_known_saving():
    """A ledger built so the true marginal saving is exactly 40%."""
    rows = []
    for seed in range(3):
        rows.append(record(seed=seed, arm_label="arm_base", cost_usd_headline=1.0,
                           cost_usd_shadow_free=1.0))
        rows.append(record(seed=seed, arm_label="arm_cand", cost_usd_headline=0.6,
                           cost_usd_shadow_free=0.6, ops_served=6, model_ops_served=6,
                           reuse_count_weighted=0.6, cross_run_hit_fraction=1.0))
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "l.jsonl")
        write(rows, path)
        result = analyse(path)
        primary = [s for s in result["primary_candidates"]
                   if s["baseline_label"] == "arm_base" and s["candidate_label"] == "arm_cand"]
        check("recovers a known 40% marginal saving", primary and abs(primary[0]["marginal_saving"] - 0.40) < 1e-9,
              f"{primary[0]['marginal_saving']:.6f}" if primary else "missing")
        check("pools the 3 seeds into one cell", result["n_cells"] == 2, str(result["n_cells"]))
        check("emits both directions of the pair", len(result["pairwise_savings"]) == 2)


def test_negative_saving_is_reported():
    """Arm C costing MORE must produce a negative number, not a clamped zero."""
    rows = [record(arm_label="arm_base", cost_usd_headline=1.0, cost_usd_shadow_free=1.0),
            record(arm_label="arm_cand", cost_usd_headline=1.5, cost_usd_shadow_free=1.5,
                   ops_served=5)]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "l.jsonl")
        write(rows, path)
        result = analyse(path)
        got = [s["marginal_saving"] for s in result["pairwise_savings"]
               if s["baseline_label"] == "arm_base"][0]
        check("negative marginal saving reported as negative", abs(got + 0.5) < 1e-9, f"{got:.4f}")


def test_bands():
    check("band: 9% is dead", verdict_for(0.09).startswith("dead"))
    check("band: 10% is not dead", not verdict_for(0.10).startswith("dead"))
    check("band: 24% is feature", verdict_for(0.24).startswith("not a standalone"))
    check("band: 26% is live", verdict_for(0.26).startswith("live"))
    check("band: negative is dead", verdict_for(-0.5).startswith("dead"))


def test_gates_fire():
    # arm B health: a label whose cache-read fraction is below 0.50 must fail.
    rows = [record(arm_label="arm_weak", cache_read_fraction=0.20),
            record(arm_label="arm_ok", cache_read_fraction=0.80)]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "l.jsonl")
        write(rows, path)
        health = analyse(path)["gates"]["label_health"]
        check("arm B health gate fails a weak cache", health["arm_weak"]["passes_arm_b_health"] is False)
        check("arm B health gate passes a tuned cache", health["arm_ok"]["passes_arm_b_health"] is True)

    # arm C health: below 95% of ceiling at 0% perturbation must fail.
    rows = [record(arm_label="arm_c", perturbation=0.0, ops_served=5, achieved_over_ceiling=0.80)]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "l.jsonl")
        write(rows, path)
        health = analyse(path)["gates"]["label_health"]
        check("arm C health gate fails below 95% of ceiling", health["arm_c"]["passes_arm_c_health"] is False)

    rows = [record(arm_label="arm_c", perturbation=0.0, ops_served=5, achieved_over_ceiling=0.97)]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "l.jsonl")
        write(rows, path)
        health = analyse(path)["gates"]["label_health"]
        check("arm C health gate passes at 97% of ceiling", health["arm_c"]["passes_arm_c_health"] is True)


def test_inert_perturbation_gate():
    rows = [record(n_inert_perturbations=1)]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "l.jsonl")
        write(rows, path)
        gate = analyse(path)["gates"]["perturbations_semantic"]
        check("inert perturbation fails the semantic gate", gate["passes"] is False)


def test_cold_truncation_gate():
    rows = [record(family="f1", temperature="warm"), record(family="f1", temperature="warm",
                                                            perturbation=0.10)]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "l.jsonl")
        write(rows, path)
        gate = analyse(path)["gates"]["cold_cells_not_truncated"]
        check("missing cold cells fail the truncation gate", gate["passes"] is False,
              json.dumps(gate["per_family"]))


def test_model_version_gate():
    rows = [record(model_versions=["m-1"]), record(model_versions=["m-2"], seed=1)]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "l.jsonl")
        write(rows, path)
        gate = analyse(path)["gates"]["single_model_version"]
        check("a mid-matrix model change is flagged", gate["passes"] is False,
              str(gate["versions"]))


def test_no_pooling_across_families():
    rows = [record(family="f1", cost_usd_headline=1.0, arm_label="arm_base"),
            record(family="f2", cost_usd_headline=9.0, arm_label="arm_base")]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "l.jsonl")
        write(rows, path)
        cells = analyse(path)["cells"]
        check("families are never pooled", len(cells) == 2, str(len(cells)))
        values = sorted(c["cost_headline_mean"] for c in cells.values())
        check("family costs stay separate", values == [1.0, 9.0], str(values))


def test_ceiling_rows_labelled():
    rows = [record(perturbation=0.0, ops_served=8, arm_label="arm_c", reuse_count_weighted=1.0),
            record(perturbation=0.02, ops_served=6, arm_label="arm_c", reuse_count_weighted=0.6)]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "l.jsonl")
        write(rows, path)
        curves = analyse(path)["decay_curves"]
        curve = list(curves.values())[0]
        check("0% row flagged as a ceiling", curve["0.0"]["is_ceiling_row"] is True)
        check("2% row not flagged as a ceiling", curve["0.02"]["is_ceiling_row"] is False)


def test_headline_only_at_2pct_cold():
    rows = [record(perturbation=0.0, temperature="cold", arm_label="arm_base", cost_usd_headline=1.0),
            record(perturbation=0.0, temperature="cold", arm_label="arm_c", cost_usd_headline=0.01),
            record(perturbation=0.02, temperature="cold", arm_label="arm_base", cost_usd_headline=1.0),
            record(perturbation=0.02, temperature="cold", arm_label="arm_c", cost_usd_headline=0.7)]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "l.jsonl")
        write(rows, path)
        result = analyse(path)
        savings = {round(s["marginal_saving"], 4) for s in result["primary_candidates"]}
        check("the 99% ceiling row is excluded from primary candidates", 0.99 not in savings,
              str(sorted(savings)))
        check("the 2% row is a primary candidate", 0.30 in savings, str(sorted(savings)))


def main() -> int:
    print("=== recovery of known values ===")
    test_known_saving()
    test_negative_saving_is_reported()
    print("\n=== decision bands ===")
    test_bands()
    print("\n=== gates ===")
    test_gates_fire()
    test_inert_perturbation_gate()
    test_cold_truncation_gate()
    test_model_version_gate()
    print("\n=== reporting rules ===")
    test_no_pooling_across_families()
    test_ceiling_rows_labelled()
    test_headline_only_at_2pct_cold()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all analysis tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
