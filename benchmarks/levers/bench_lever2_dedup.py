"""Benchmark — Lever 2 (content-addressed dedup, IPFS + LBFS).

Metrics (the papers' own dimensions):
  * dedup savings %      : bytes avoided vs naive full-resend across agents
  * incremental transfer : new bytes needed to send an *edited* artifact (LBFS Fig.1 property)
  * reconstruction       : MUST be exactly lossless (1.0) — accuracy-first gate

Scaled local benchmark (no GPU/API). Run:
    python benchmarks/levers/bench_lever2_dedup.py
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from token_efficiency_model.lossless.content_store import ContentStore, RabinChunker, cid


def make_doc(seed: int, size: int) -> bytes:
    r = random.Random(seed)
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
             "hotel", "india", "juliet", "service", "timeout", "retry", "cache"]
    out, total = [], 0
    while total < size:
        line = " ".join(r.choice(words) for _ in range(10)) + "\n"
        out.append(line)
        total += len(line)
    return "".join(out).encode("utf-8")


def scenario_shared_context(n_agents=6, shared_size=40_000, private_size=6_000):
    """N agents each receive (shared spec/files) + (agent-private notes). Classic A2A."""
    shared = make_doc(100, shared_size)
    store = ContentStore(RabinChunker(avg_bits=10, min_size=512, max_size=16_384))
    baseline_bytes = 0
    roots = []
    payloads = []
    for a in range(n_agents):
        private = make_doc(200 + a, private_size)
        payload = shared + b"\n--- agent-private ---\n" + private
        payloads.append(payload)
        baseline_bytes += len(payload)          # naive: every agent ships the whole thing
        roots.append(store.put_artifact(payload))

    dedup_bytes = store.stats.bytes_stored      # unique bytes actually held
    recon_ok = all(store.get_artifact(r) == p for r, p in zip(roots, payloads))
    return {
        "scenario": "shared_context_multi_agent",
        "n_agents": n_agents,
        "baseline_bytes": baseline_bytes,
        "dedup_bytes": dedup_bytes,
        "savings_pct": round(100 * (1 - dedup_bytes / baseline_bytes), 2),
        "reconstruction_ok": recon_ok,
    }


def scenario_edited_artifact(size=60_000):
    """One agent edits a file and passes it to the next (LBFS locality)."""
    store = ContentStore(RabinChunker(avg_bits=10, min_size=512, max_size=16_384))
    base = make_doc(300, size)
    store.put_artifact(base)
    mid = len(base) // 2
    edited = base[:mid] + b"\nFIX: set MTU 1450 on LB-X to stop the timeout\n" + base[mid:]
    root = store.put_artifact(edited)
    recon_ok = store.get_artifact(root) == edited
    return {
        "scenario": "single_edit_locality",
        "artifact_bytes": len(edited),
        "incremental_bytes": store.stats.bytes_transferred,
        "incremental_pct": round(100 * store.stats.bytes_transferred / len(edited), 2),
        "reconstruction_ok": recon_ok,
    }


def main():
    results = [scenario_shared_context(), scenario_edited_artifact()]

    # accuracy-first gates + scaled targets
    shared, edit = results
    checks = {
        "lossless_reconstruction": shared["reconstruction_ok"] and edit["reconstruction_ok"],
        "shared_context_savings>=60%": shared["savings_pct"] >= 60.0,
        "single_edit_incremental<=20%": edit["incremental_pct"] <= 20.0,
    }
    passed = all(checks.values())

    report = {"lever": 2, "results": results, "checks": checks, "passed": passed}
    out_path = os.path.join(os.path.dirname(__file__), "results_lever2.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

    print("=== Lever 2 — content-addressed dedup ===")
    for r in results:
        print(json.dumps(r, indent=2))
    print("checks:", json.dumps(checks, indent=2))
    print("PASSED" if passed else "FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
