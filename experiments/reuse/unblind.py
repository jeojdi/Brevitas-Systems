"""Unblinding. Run only after every number in the blinded analysis is final.

`analysis.py` computes everything over opaque labels and never opens `arm_mapping.json`. This
script applies the mapping, stamps the unblinding time into the mapping file, and emits the
tables the report needs.

Run: python3 -m experiments.reuse.unblind [results_blinded.json] [out.json]
"""

from __future__ import annotations

import json
import sys

from .analysis import verdict_for
from .harness.ledger import ArmBlinding

ROOT = "experiments/reuse"


def main() -> int:
    blinded_path = sys.argv[1] if len(sys.argv) > 1 else f"{ROOT}/results_blinded.json"
    out_path = sys.argv[2] if len(sys.argv) > 2 else f"{ROOT}/results_unblinded.json"

    with open(blinded_path, encoding="utf-8") as handle:
        blinded = json.load(handle)

    blinding = ArmBlinding(f"{ROOT}/arm_mapping.json")
    label_to_arm = blinding.unblind()
    print(f"unblinded: {label_to_arm}")

    def arm(label: str) -> str:
        return label_to_arm.get(label, label)

    # Primary metric: arm C over arm B, at 2% perturbation, cold cache, best family.
    primary = [
        s for s in blinded["pairwise_savings"]
        if arm(s["baseline_label"]) == "B" and arm(s["candidate_label"]) == "C"
    ]
    for row in primary:
        row["baseline_arm"] = "B"
        row["candidate_arm"] = "C"

    cold_2pct = [
        s for s in primary if s["perturbation"] == 0.02 and s["temperature"] == "cold"
    ]
    warm_2pct = [
        s for s in primary if s["perturbation"] == 0.02 and s["temperature"] == "warm"
    ]

    best_cold = max(cold_2pct, key=lambda s: s["marginal_saving"], default=None)
    best_warm = max(warm_2pct, key=lambda s: s["marginal_saving"], default=None)

    gates = blinded["gates"]
    health = {arm(label): value for label, value in gates["label_health"].items()}

    result = {
        "label_to_arm": label_to_arm,
        "primary_metric": {
            "definition": "marginal dollar saving of arm C over arm B, 2% perturbation, cold "
                          "cache, best-performing family",
            "value": best_cold["marginal_saving"] if best_cold else None,
            "family": best_cold["family"] if best_cold else None,
            "verdict": verdict_for(best_cold["marginal_saving"]) if best_cold else "not measured",
            "shadow_free_value": best_cold["marginal_saving_shadow_free"] if best_cold else None,
            "grader_pass_delta_pp": best_cold["grader_pass_delta_pp"] if best_cold else None,
        },
        "h1_warm": {
            "value": best_warm["marginal_saving"] if best_warm else None,
            "family": best_warm["family"] if best_warm else None,
        },
        "h2_cold": {
            "value": best_cold["marginal_saving"] if best_cold else None,
            "family": best_cold["family"] if best_cold else None,
        },
        "by_family_perturbation_temperature": sorted(
            primary, key=lambda s: (s["family"], s["temperature"], s["perturbation"])
        ),
        "arm_health": health,
        "gates": {k: v for k, v in gates.items() if k != "label_health"},
        "decay_curves": {
            "|".join([arm(part) if part in label_to_arm else part for part in key.split("|")]): value
            for key, value in blinded["decay_curves"].items()
        },
        "cells": {
            key: {**cell, "arm": arm(cell["arm_label"])}
            for key, cell in blinded["cells"].items()
        },
        "n_records": blinded["n_records"],
        "n_cells": blinded["n_cells"],
    }

    with open(out_path, encoding="utf-8", mode="w") as handle:
        json.dump(result, handle, indent=1, sort_keys=True)

    print(f"\nPRIMARY METRIC (2% perturbation, cold, best family)")
    if best_cold:
        print(f"  family          : {best_cold['family']}")
        print(f"  marginal saving : {best_cold['marginal_saving']:+.1%}")
        print(f"  shadow-free     : {best_cold['marginal_saving_shadow_free']:+.1%}")
        print(f"  verdict         : {verdict_for(best_cold['marginal_saving'])}")
        print(f"  grader delta    : {best_cold['grader_pass_delta_pp']:+.1f} pp")
    else:
        print("  no cold 2% cells present")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
