"""Dedup/delta wiring (brief b3, P2). Provider channel = cache-stable ordering
(lossless reorder); receiver channel = CID+delta measurement with verified lossless
round-trip. Deterministic; no network."""
from __future__ import annotations

from token_efficiency_model.lossless.dedup_delta import DedupDeltaLayer


def test_classify_unchanged_edited_new():
    layer = DedupDeltaLayer()
    c1 = layer.classify("s", [("a.py", "print(1)"), ("b.py", "x=2")])
    assert c1 == {"a.py": "new", "b.py": "new"}
    c2 = layer.classify("s", [("a.py", "print(1)"),          # unchanged
                              ("b.py", "x=3"),                # edited
                              ("c.py", "y=9")])               # new
    assert c2 == {"a.py": "unchanged", "b.py": "edited", "c.py": "new"}


def test_stable_order_preserves_history_and_is_lossless():
    layer = DedupDeltaLayer()
    layer.stable_order("s", [("a", "AAA"), ("b", "BBB"), ("c", "CCC")])
    # next turn arrives in a DIFFERENT order with one edit + one new file
    out = layer.stable_order("s", [("c", "CCC"), ("b", "BBB2"), ("a", "AAA"), ("d", "DDD")])
    ids = [aid for aid, _ in out]
    # historical order (a, b, c) first, then new (d) — regardless of input order
    assert ids == ["a", "b", "c", "d"]
    # lossless: same set of (id, text) pairs, nothing dropped or altered
    assert dict(out) == {"a": "AAA", "b": "BBB2", "c": "CCC", "d": "DDD"}


def test_stable_order_prefix_is_byte_identical_across_turns():
    layer = DedupDeltaLayer()
    t1 = layer.stable_order("s", [("a", "A"), ("b", "B")])
    t2 = layer.stable_order("s", [("b", "B"), ("a", "A"), ("z", "Z")])
    # the a,b prefix (unchanged files) keeps identical order+content ⇒ provider cache hit
    assert [x[0] for x in t2][:2] == [x[0] for x in t1]


def test_measure_redundancy_lossless_roundtrip_and_savings():
    layer = DedupDeltaLayer()
    big = "def f():\n" + "    pass\n" * 500        # ~4KB artifact
    # turn 1: brand new -> little/no dedup, but MUST be lossless
    r1 = layer.measure_redundancy("s", [("f.py", big)])
    assert r1["lossless"] is True
    # turn 2: identical artifact -> receiver channel needs ~no bytes (dedup ref)
    r2 = layer.measure_redundancy("s", [("f.py", big)])
    assert r2["lossless"] is True
    assert r2["savings_ratio"] > 0.5, "unchanged artifact should dedup heavily"


def test_measure_redundancy_delta_on_small_edit():
    layer = DedupDeltaLayer()
    base = "line\n" * 400
    layer.measure_redundancy("s", [("doc", base)])
    edited = base + "one new line\n"
    r = layer.measure_redundancy("s", [("doc", edited)])
    assert r["lossless"] is True
    assert r["savings_ratio"] > 0.5, "a tiny edit should transmit as a small delta"
