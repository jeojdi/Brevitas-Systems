"""Tests for Lever 2 — content-addressed store + LBFS content-defined chunking."""

import os

from token_efficiency_model.lossless.content_store import ContentStore, RabinChunker, cid


def _doc(seed: int, size: int) -> bytes:
    # deterministic pseudo-random-ish text with line structure
    import random

    r = random.Random(seed)
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    lines = []
    total = 0
    while total < size:
        line = " ".join(r.choice(words) for _ in range(8)) + "\n"
        lines.append(line)
        total += len(line)
    return "".join(lines).encode("utf-8")


# --- cid ------------------------------------------------------------------- #
def test_cid_deterministic_and_sha256():
    import hashlib

    data = b"hello world"
    assert cid(data) == cid(data)
    assert cid(data)[1:] == hashlib.sha256(data).hexdigest()[:32]
    assert cid(b"a") != cid(b"b")


# --- chunker --------------------------------------------------------------- #
def test_chunking_is_deterministic_and_lossless():
    chunker = RabinChunker(avg_bits=6, min_size=64, max_size=4096)  # small for tests
    data = _doc(1, 20_000)
    a = chunker.split(data)
    b = chunker.split(data)
    assert [bytes(x) for x in a] == [bytes(x) for x in b]  # deterministic
    assert b"".join(a) == data  # lossless: chunks concatenate to the original
    assert len(a) > 1  # actually split


def test_chunk_sizes_within_bounds():
    chunker = RabinChunker(avg_bits=6, min_size=64, max_size=4096)
    data = _doc(2, 40_000)
    chunks = chunker.split(data)
    # only the final chunk may be < min_size
    for c in chunks[:-1]:
        assert len(c) >= 64
        assert len(c) <= 4096


def test_lbfs_locality_edit_changes_only_local_chunks():
    """LBFS's key property (Fig. 1): inserting text changes only the local chunk(s);
    all other chunk hashes are unchanged."""
    chunker = RabinChunker(avg_bits=6, min_size=64, max_size=4096)
    base = _doc(3, 30_000)
    # insert a paragraph in the middle
    mid = len(base) // 2
    edited = base[:mid] + b"\nINSERTED PARAGRAPH ABOUT MTU MISMATCH ON LB-X\n" + base[mid:]

    base_ids = {cid(c) for c in chunker.split(base)}
    edited_ids = {cid(c) for c in chunker.split(edited)}

    shared = base_ids & edited_ids
    # the vast majority of chunks should be identical across the edit
    assert len(shared) / len(base_ids) > 0.7


# --- store ----------------------------------------------------------------- #
def test_store_roundtrip_lossless():
    store = ContentStore(RabinChunker(avg_bits=6, min_size=64, max_size=4096))
    data = _doc(4, 25_000)
    root = store.put_artifact(data)
    assert store.get_artifact(root) == data  # exact reconstruction


def test_identical_artifacts_dedup_fully():
    store = ContentStore(RabinChunker(avg_bits=6, min_size=64, max_size=4096))
    data = _doc(5, 25_000)
    store.put_artifact(data)
    blocks_after_first = len(store.blocks)
    store.put_artifact(data)  # same artifact again
    assert len(store.blocks) == blocks_after_first  # zero new blocks
    assert store.stats.bytes_transferred == 0


def test_edited_artifact_transfers_only_changed_chunks():
    store = ContentStore(RabinChunker(avg_bits=6, min_size=64, max_size=4096))
    base = _doc(6, 30_000)
    store.put_artifact(base)
    mid = len(base) // 2
    edited = base[:mid] + b"\nSMALL EDIT\n" + base[mid:]
    store.put_artifact(edited)
    # only a small fraction of the edited artifact is new bytes
    assert store.stats.bytes_transferred < 0.25 * len(edited)


def test_missing_chunk_fails_safe():
    store = ContentStore(RabinChunker(avg_bits=6, min_size=64, max_size=4096))
    data = _doc(7, 25_000)
    root = store.put_artifact(data)
    # simulate a dropped block
    victim = next(iter(store.blocks))
    del store.blocks[victim]
    assert store.get_artifact(root) is None  # no silent partial reconstruction
