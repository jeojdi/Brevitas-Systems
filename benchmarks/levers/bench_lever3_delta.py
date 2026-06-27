"""Benchmark — Lever 3 (delta transmission: Myers / VCDIFF / rsync).

Metrics:
  * delta size %     : wire bytes of the delta vs full artifact (lower is better)
  * reconstruction   : MUST be exactly lossless (accuracy-first gate)
  * drift fail-safe  : a drifted base MUST be rejected (no silent wrong state)

Scaled local benchmark. Run:
    python benchmarks/levers/bench_lever3_delta.py
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from token_efficiency_model.lossless.delta import apply_delta, encode_delta


def make_file(seed: int, lines: int) -> bytes:
    r = random.Random(seed)
    toks = ["def", "return", "x", "y", "service", "cache", "value", "retry", "=", "+"]
    return ("\n".join(" ".join(r.choice(toks) for _ in range(7)) for _ in range(lines))).encode()


def scenario_code_edit():
    base = make_file(10, 200)              # ~ small source file
    lines = base.split(b"\n")
    lines[100] = b"    return cached_value  # edited"
    target = b"\n".join(lines)
    p = encode_delta(base, target, method="myers")
    ok = apply_delta(base, p) == target
    return {
        "scenario": "code_edit_myers",
        "full_bytes": len(target),
        "delta_bytes": p.wire_size(),
        "delta_pct": round(100 * p.wire_size() / len(target), 2),
        "method": p.method,
        "reconstruction_ok": ok,
    }


def scenario_large_blob():
    base = make_file(20, 3000)             # ~large artifact
    mid = len(base) // 2
    target = base[:mid] + b"\nFIX: restore 64GB nodes\n" + base[mid:]
    p = encode_delta(base, target, method="rsync")
    ok = apply_delta(base, p) == target
    return {
        "scenario": "large_blob_rsync",
        "full_bytes": len(target),
        "delta_bytes": p.wire_size(),
        "delta_pct": round(100 * p.wire_size() / len(target), 2),
        "method": p.method,
        "reconstruction_ok": ok,
    }


def scenario_multiturn_plan():
    """Agent revises a plan over 6 turns; only deltas cross the wire after turn 1."""
    state = make_file(30, 150)
    full_total = len(state)
    delta_total = len(state)               # turn 1 is a full send
    ok = True
    for t in range(1, 6):
        lines = state.split(b"\n")
        lines[t * 20 % len(lines)] = f"  step {t}: revised".encode()
        new_state = b"\n".join(lines)
        p = encode_delta(state, new_state, method="auto")
        ok = ok and (apply_delta(state, p) == new_state)
        full_total += len(new_state)
        delta_total += p.wire_size()
        state = new_state
    return {
        "scenario": "multiturn_plan_revision",
        "turns": 6,
        "full_bytes": full_total,
        "delta_bytes": delta_total,
        "delta_pct": round(100 * delta_total / full_total, 2),
        "reconstruction_ok": ok,
    }


def main():
    results = [scenario_code_edit(), scenario_large_blob(), scenario_multiturn_plan()]

    # drift fail-safe check
    a = make_file(40, 100)
    b = a + b"\nextra"
    p = encode_delta(a, b, method="myers")
    drift_safe = apply_delta(make_file(41, 100), p) is None

    checks = {
        "all_lossless": all(r["reconstruction_ok"] for r in results),
        "code_edit_delta<=15%": results[0]["delta_pct"] <= 15.0,
        "large_blob_delta<=15%": results[1]["delta_pct"] <= 15.0,
        "multiturn_delta<=50%": results[2]["delta_pct"] <= 50.0,
        "drift_fails_safe": drift_safe,
    }
    passed = all(checks.values())

    report = {"lever": 3, "results": results, "checks": checks, "passed": passed}
    with open(os.path.join(os.path.dirname(__file__), "results_lever3.json"), "w") as f:
        json.dump(report, f, indent=2)

    print("=== Lever 3 — delta transmission ===")
    for r in results:
        print(json.dumps(r, indent=2))
    print("checks:", json.dumps(checks, indent=2))
    print("PASSED" if passed else "FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
