"""Tests for Lever 3 — delta transmission (Myers / VCDIFF / rsync) with verification."""

import random

from token_efficiency_model.lossless.delta import (
    apply_delta,
    encode_delta,
    myers_moves,
    rsync_ops,
)


def _doc(seed: int, size: int) -> bytes:
    r = random.Random(seed)
    words = ["alpha", "bravo", "charlie", "delta", "echo", "service", "timeout"]
    out, total = [], 0
    while total < size:
        out.append(" ".join(r.choice(words) for _ in range(8)) + "\n")
        total += len(out[-1])
    return "".join(out).encode("utf-8")


# --- Myers ----------------------------------------------------------------- #
def test_myers_roundtrip_small():
    a = b"the quick brown fox jumps over the lazy dog"
    b = b"the quick red fox leaps over the lazy dog"
    p = encode_delta(a, b, method="myers")
    assert apply_delta(a, p) == b


def test_myers_roundtrip_random_text():
    for seed in range(5):
        a = _doc(seed, 2000)
        b = _doc(seed + 100, 2000)
        p = encode_delta(a, b, method="myers")
        assert apply_delta(a, p) == b


def test_myers_small_edit_yields_small_delta():
    a = _doc(1, 4000)
    b = a[: len(a) // 2] + b"INSERTED LINE\n" + a[len(a) // 2 :]
    p = encode_delta(a, b, method="myers")
    assert apply_delta(a, p) == b
    assert p.wire_size() < 0.2 * len(b)  # mostly COPY ops, little literal


# --- rsync ----------------------------------------------------------------- #
def test_rsync_roundtrip_and_small_edit():
    a = _doc(2, 40_000)
    b = a[: len(a) // 2] + b"FIX: set MTU 1450\n" + a[len(a) // 2 :]
    p = encode_delta(a, b, method="rsync")
    assert p.method == "rsync"
    assert apply_delta(a, p) == b
    assert p.wire_size() < 0.2 * len(b)


def test_rsync_identical_is_all_copy():
    a = _doc(3, 20_000)
    p = encode_delta(a, a, method="rsync")
    assert apply_delta(a, p) == a
    assert p.wire_size() < 0.05 * len(a)


# --- auto + RUN ------------------------------------------------------------ #
def test_auto_picks_method_and_run_op():
    small = b"hello"
    assert encode_delta(b"hellish", small, method="auto").method == "myers"
    big_a = _doc(4, 20_000)
    assert encode_delta(big_a, big_a + b"x", method="auto").method == "rsync"
    # RUN compression for long identical-byte insert
    a = b"abc"
    b = b"abc" + b"=" * 64
    p = encode_delta(a, b, method="myers")
    assert any(op["op"] == "RUN" for op in p.ops)
    assert apply_delta(a, p) == b


# --- accuracy-first fail-safes --------------------------------------------- #
def test_base_drift_fails_safe():
    a = _doc(5, 3000)
    b = a + b"more\n"
    p = encode_delta(a, b, method="myers")
    drifted = _doc(999, 3000)  # receiver holds a different base
    assert apply_delta(drifted, p) is None  # never reconstruct a wrong state


def test_empty_base_is_full_send():
    b = b"brand new artifact"
    p = encode_delta(b"", b)
    assert p.method == "full"
    assert apply_delta(b"", p) == b
