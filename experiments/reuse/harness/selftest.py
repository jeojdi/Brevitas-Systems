"""Harness self-tests.

These exist because the most likely way this experiment produces a falsely negative result is a
broken harness that looks like a broken idea. Each test names the failure mode it guards.

Run: python3 -m experiments.reuse.harness.selftest
"""

from __future__ import annotations

import json
import os
import random
import tempfile

from .canon import CanonError, canonical, reuse_key, sha256_text
from .graph import Op, RunGraph
from .perturb import apply as perturb_apply, position_report, select
from .store import ReuseStore

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


# ---------------------------------------------------------------------------------------
# False-negative item 3: canonicalisation failures silently destroy reuse.
# ---------------------------------------------------------------------------------------

def test_canonicalisation() -> None:
    a = {"b": 1, "a": [3, 2], "c": {"z": None, "y": "x"}}
    b = {"c": {"y": "x", "z": None}, "a": [3, 2], "b": 1}
    check("canon: key order irrelevant", canonical(a) == canonical(b))
    check("canon: list order significant", canonical({"a": [1, 2]}) != canonical({"a": [2, 1]}))

    # The same logical operation performed in two different isolated run directories must
    # produce the same key. Isolation (requirement 8.6) would otherwise destroy all reuse.
    k1 = reuse_key(
        op_type="read_file",
        args={"path": "/scratch/run_a/src/pay.py"},
        model=None,
        artifact_hashes={"src/pay.py": "deadbeef"},
        upstream_output_hashes=[],
        run_root="/scratch/run_a",
    )
    k2 = reuse_key(
        op_type="read_file",
        args={"path": "/scratch/run_b/src/pay.py"},
        model=None,
        artifact_hashes={"src/pay.py": "deadbeef"},
        upstream_output_hashes=[],
        run_root="/scratch/run_b",
    )
    check("canon: run root stripped, cross-run key stable", k1 == k2, k1[:12])

    # Round-tripping through JSON must not change the key. Re-serialised request bodies are a
    # classic silent reuse killer.
    payload = {"prompt": "hello", "nested": {"n": 2, "m": [1, {"k": "v"}]}}
    once = sha256_text(canonical(payload))
    twice = sha256_text(canonical(json.loads(json.dumps(payload))))
    check("canon: json round-trip stable", once == twice)

    leaked = False
    try:
        canonical({"path": "/Users/someone/secret/file.py"})
    except CanonError:
        leaked = True
    check("canon: absolute home path rejected", leaked)

    leaked = False
    try:
        canonical({"ts": "2026-08-17T04:00:00Z"})
    except CanonError:
        leaked = True
    check("canon: timestamp rejected", leaked)


# ---------------------------------------------------------------------------------------
# The dependency tracker itself: transitivity is the whole idea.
# ---------------------------------------------------------------------------------------

def _chain_graph(n: int = 12) -> RunGraph:
    g = RunGraph("chain")
    prev: list[str] = []
    for i in range(n):
        op = Op(
            op_id=f"op{i:03d}",
            kind="model" if i % 2 else "tool",
            op_type="model_call" if i % 2 else "read_file",
            args={"i": i},
            model={"id": "m", "temperature": 0} if i % 2 else None,
            artifacts={f"art{i:03d}": sha256_text(str(i))},
            upstream=list(prev),
        )
        op.output_hash = sha256_text(f"out{i}")
        g.add(op)
        prev = [op.op_id]
    return g


def test_transitivity() -> None:
    g = _chain_graph(12)

    root = g.invalidated({"art000"})
    check("graph: root change invalidates whole chain", len(root) == 12, f"{len(root)}/12")

    leaf = g.invalidated({"art011"})
    check("graph: leaf change invalidates only the leaf", leaf == {"op011"}, str(sorted(leaf)))

    mid = g.invalidated({"art006"})
    check("graph: mid change invalidates suffix", len(mid) == 6, f"{len(mid)}/12")

    # Key composition: changing an upstream output hash must change the downstream key.
    before = g.get("op005").reuse_key
    g2 = _chain_graph(12)
    g2.ops[4].output_hash = sha256_text("different")
    g3 = RunGraph("rebuilt")
    for op in g2.ops:
        g3.add(Op(**{k: v for k, v in op.to_json().items() if k != "reuse_key"}))
    check("graph: upstream output change changes downstream key", g3.get("op005").reuse_key != before)

    ceil0 = g.ceiling(set(), cost_of={op.op_id: 1.0 for op in g.ops})
    check("graph: 0% perturbation ceiling is 1.0", ceil0["count_weighted"] == 1.0)

    # Cost weighting must be able to disagree with count weighting, or reporting both is
    # pointless (false-positive item 6).
    costs = {op.op_id: (10.0 if op.kind == "model" else 0.01) for op in g.ops}
    c = g.ceiling({"art011"}, cost_of=costs)
    check(
        "graph: cost-weighted differs from count-weighted",
        abs(c["cost_weighted"] - c["count_weighted"]) > 1e-9,
        f"cost={c['cost_weighted']:.4f} count={c['count_weighted']:.4f}",
    )


def test_early_cutoff() -> None:
    g = _chain_graph(12)
    observed = {op.op_id: op.output_hash for op in g.ops}  # every recompute matched
    conservative = g.invalidated({"art000"})
    ec = g.invalidated_early_cutoff({"art000"}, observed)
    check(
        "graph: early cutoff stops propagation when output unchanged",
        len(ec) == 1 and len(conservative) == 12,
        f"ec={len(ec)} conservative={len(conservative)}",
    )


def test_granularity() -> None:
    """False-negative item 2: over-recording edges invalidates ops that should have survived."""
    g = RunGraph("gran")
    for i in range(8):
        op = Op(
            op_id=f"op{i}",
            kind="model",
            op_type="model_call",
            args={"i": i},
            model={"id": "m"},
            artifacts={f"file.py#func{i}": sha256_text(str(i))},
            upstream=[],
        )
        op.output_hash = sha256_text(f"o{i}")
        g.add(op)
    fine = g.invalidated({"file.py#func3"})
    coarse_map = {f"file.py#func{i}": "file.py" for i in range(8)}
    coarse = g.coarsened(coarse_map).invalidated({"file.py"})
    check(
        "granularity: file-level over-invalidates vs span-level",
        len(fine) == 1 and len(coarse) == 8,
        f"fine={len(fine)} coarse={len(coarse)}",
    )


# ---------------------------------------------------------------------------------------
# Sampler uniformity over graph position (false-positive 8 / false-negative 5).
# ---------------------------------------------------------------------------------------

def test_sampler_uniformity() -> None:
    g = _chain_graph(200)
    positions = g.artifact_positions()
    check("perturb: positions recorded for every artifact", len(positions) == 200)

    deviations = []
    depth_means = []
    for seed in range(12):
        artifacts = {a: f'value "Acme Corp" n={i} some_symbol_name\n' for i, a in enumerate(sorted(positions))}
        _new, records = perturb_apply(artifacts, positions, 0.10, seed)
        report = position_report(records, positions)
        deviations.append(report["max_share_deviation"])
        depth_means.append(report["mean_depth"])

    worst = max(deviations)
    check(
        "perturb: stratified sampler is position-uniform",
        worst < 0.15,
        f"worst stratum share deviation {worst:.3f} over 12 seeds",
    )

    population_mean_depth = sum(p["depth"] for p in positions.values()) / len(positions)
    observed = sum(depth_means) / len(depth_means)
    check(
        "perturb: mean perturbation depth tracks the population",
        abs(observed - population_mean_depth) / max(population_mean_depth, 1) < 0.15,
        f"observed {observed:.1f} vs population {population_mean_depth:.1f}",
    )


def test_perturbation_is_semantic() -> None:
    rng = random.Random(0)
    text = 'record = {"customer": "Acme Corp", "total_amount": 1420, "line_items": 3}\n'
    changed = 0
    for seed in range(20):
        artifacts = {"a": text}
        positions = {"a": {"depth": 0, "blast_count": 1, "blast_fraction": 1.0}}
        new, records = perturb_apply(artifacts, positions, 1.0, seed)
        if records and new["a"] != text:
            changed += 1
            stripped_old = "".join(text.split())
            stripped_new = "".join(new["a"].split())
            check(
                f"perturb: seed {seed} changes more than whitespace",
                stripped_old != stripped_new,
            ) if seed < 3 else None
    check("perturb: mutators fire on realistic content", changed == 20, f"{changed}/20")
    _ = rng


def test_determinism() -> None:
    g = _chain_graph(50)
    positions = g.artifact_positions()
    artifacts = {a: f'x = 1\nname = "Acme Corp"\nsome_symbol = {i}\n' for i, a in enumerate(sorted(positions))}
    first = perturb_apply(artifacts, positions, 0.10, 7)
    second = perturb_apply(artifacts, positions, 0.10, 7)
    check("perturb: identical seed gives identical result", first[0] == second[0])
    check(
        "perturb: identical seed gives identical record",
        [r.to_json() for r in first[1]] == [r.to_json() for r in second[1]],
    )
    other = perturb_apply(artifacts, positions, 0.10, 8)
    check("perturb: different seed gives different result", first[0] != other[0])


# ---------------------------------------------------------------------------------------
# Cross-run scope of the store (false-negative item 7).
# ---------------------------------------------------------------------------------------

def test_store_cross_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "store.sqlite")
        store = ReuseStore(path)
        store.put(
            reuse_key="k1", op_type="model_call", kind="model", output="hello",
            output_hash="h", tokens={"input": 10}, run_id="run1", family="f1", now=1000.0,
        )
        store.close()

        # A different process entirely, which is the point.
        store2 = ReuseStore(path)
        hit = store2.get("k1", read_by_run="run2", now=1000.0 + 86400)
        check("store: survives process boundary", hit is not None)
        check("store: hit flagged cross-run", bool(hit and hit["cross_run"]))
        evidence = store2.cross_run_evidence()
        check("store: cross-run fraction is 1.0", evidence["cross_run_fraction"] == 1.0)
        check(
            "store: write-to-read span recorded across a day",
            evidence["max_write_to_read_seconds"] >= 86400,
            f"{evidence['max_write_to_read_seconds']:.0f}s",
        )
        check("store: storage bytes accounted", store2.storage_bytes() == 5)
        store2.close()


def main() -> int:
    print("=== canonicalisation ===")
    test_canonicalisation()
    print("\n=== dependency tracking ===")
    test_transitivity()
    test_early_cutoff()
    test_granularity()
    print("\n=== perturbation ===")
    test_sampler_uniformity()
    test_perturbation_is_semantic()
    test_determinism()
    print("\n=== store ===")
    test_store_cross_run()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all harness self-tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
