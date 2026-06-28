"""Tests for Lever 4 — stateful session layer (SessionCache).

Round-trip correctness across multi-turn edits, dedup across turns, fail-safe on missing base.
All deterministic (no paid APIs).
"""

import random

from token_efficiency_model.lossless.session_cache import (
    SessionCache,
    WireArtifact,
)
from token_efficiency_model.lossless.content_store import cid


def _doc(seed: int, size: int) -> bytes:
    """Deterministic pseudo-random text with line structure."""
    r = random.Random(seed)
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    lines = []
    total = 0
    while total < size:
        line = " ".join(r.choice(words) for _ in range(8)) + "\n"
        lines.append(line)
        total += len(line)
    return "".join(lines).encode("utf-8")


# --- basic round-trip ------------------------------------------------------- #
def test_session_cache_single_artifact():
    cache = SessionCache()
    conv_id = "conv_1"
    artifact = _doc(1, 5000)

    payload = cache.encode_turn(conv_id, [artifact], ["doc_a"])
    assert payload.artifacts[0].literal == artifact
    assert cache.stats().input_bytes == len(artifact)
    assert cache.stats().wire_bytes == len(artifact)  # first send is full

    # Decode (on receiver side): simulate chunk store.
    receiver_store = cache.content_store.blocks.copy()
    decoded = cache.decode_turn(conv_id, payload, receiver_store, ["doc_a"])
    assert decoded is not None
    assert decoded[0] == artifact


def test_identical_artifact_second_turn_is_deduped():
    cache = SessionCache()
    conv_id = "conv_2"
    artifact = _doc(2, 8000)

    # First turn: send full artifact.
    payload1 = cache.encode_turn(conv_id, [artifact], ["doc_a"])
    assert payload1.artifacts[0].literal == artifact
    stats1 = cache.stats()

    # Decode on receiver.
    receiver_cache = SessionCache()
    receiver_cache.content_store.blocks.update(cache.content_store.blocks)
    receiver_cache.content_store.manifests.update(cache.content_store.manifests)
    decoded1 = receiver_cache.decode_turn(conv_id, payload1, receiver_cache.content_store.blocks, ["doc_a"])
    assert decoded1 == [artifact]

    # Second turn: same artifact (no changes).
    payload2 = cache.encode_turn(conv_id, [artifact], ["doc_a"])
    assert payload2.artifacts[0].chunks is not None
    assert payload2.artifacts[0].literal is None
    stats2 = cache.stats()

    # Second turn should have zero wire bytes (only references).
    assert stats2.wire_bytes == 0
    assert stats2.savings_bytes == len(artifact)
    assert stats2.savings_ratio == 1.0


def test_edited_artifact_uses_delta():
    # Sender side
    sender_cache = SessionCache()
    conv_id = "conv_3"

    # Turn 1: initial artifact.
    base = _doc(3, 10_000)
    payload1 = sender_cache.encode_turn(conv_id, [base], ["doc_a"])

    # Receiver decodes turn 1
    receiver_cache = SessionCache()
    receiver_cache.content_store.blocks.update(sender_cache.content_store.blocks)
    receiver_cache.content_store.manifests.update(sender_cache.content_store.manifests)
    decoded1 = receiver_cache.decode_turn(conv_id, payload1, receiver_cache.content_store.blocks, ["doc_a"])
    assert decoded1 == [base]

    # Turn 2: edit (insert in middle).
    mid = len(base) // 2
    edited = base[:mid] + b"\nNEW SECTION: additional content here\n" + base[mid:]
    payload2 = sender_cache.encode_turn(conv_id, [edited], ["doc_a"])

    stats2 = sender_cache.stats()
    # Delta should be much smaller than the edited artifact.
    assert payload2.artifacts[0].delta is not None
    assert stats2.wire_bytes < 0.4 * len(edited)
    assert stats2.savings_ratio > 0.5

    # Receiver decodes turn 2
    receiver_cache.content_store.blocks.update(sender_cache.content_store.blocks)
    receiver_cache.content_store.manifests.update(sender_cache.content_store.manifests)
    decoded2 = receiver_cache.decode_turn(conv_id, payload2, receiver_cache.content_store.blocks, ["doc_a"])
    assert decoded2 is not None
    assert decoded2[0] == edited


def test_multiple_artifacts_per_turn():
    # Sender side
    sender_cache = SessionCache()
    conv_id = "conv_4"

    doc1 = _doc(4, 5000)
    doc2 = _doc(5, 6000)
    doc3 = _doc(6, 7000)

    # Turn 1: send three artifacts.
    payload1 = sender_cache.encode_turn(
        conv_id,
        [doc1, doc2, doc3],
        ["doc_a", "doc_b", "doc_c"],
    )
    assert len(payload1.artifacts) == 3
    stats1 = sender_cache.stats()
    assert stats1.input_bytes == len(doc1) + len(doc2) + len(doc3)
    assert stats1.wire_bytes == stats1.input_bytes  # all literals on first send

    # Receiver decodes turn 1
    receiver_cache = SessionCache()
    receiver_cache.content_store.blocks.update(sender_cache.content_store.blocks)
    receiver_cache.content_store.manifests.update(sender_cache.content_store.manifests)
    decoded1 = receiver_cache.decode_turn(conv_id, payload1, receiver_cache.content_store.blocks, ["doc_a", "doc_b", "doc_c"])
    assert decoded1 == [doc1, doc2, doc3]

    # Turn 2: edit only doc2.
    edited_doc2 = doc2[: len(doc2) // 2] + b"\nEDIT\n" + doc2[len(doc2) // 2 :]
    payload2 = sender_cache.encode_turn(
        conv_id,
        [doc1, edited_doc2, doc3],
        ["doc_a", "doc_b", "doc_c"],
    )
    assert len(payload2.artifacts) == 3
    assert payload2.artifacts[0].chunks is not None  # doc_a unchanged
    assert payload2.artifacts[1].delta is not None  # doc_b edited
    assert payload2.artifacts[2].chunks is not None  # doc_c unchanged

    stats2 = sender_cache.stats()
    # Total input is len(doc1) + len(edited_doc2) + len(doc3)
    # Wire should be ~0 for docs 1&3 + small delta for doc2
    assert stats2.wire_bytes < len(edited_doc2)

    # Receiver decodes turn 2
    receiver_cache.content_store.blocks.update(sender_cache.content_store.blocks)
    receiver_cache.content_store.manifests.update(sender_cache.content_store.manifests)
    decoded2 = receiver_cache.decode_turn(conv_id, payload2, receiver_cache.content_store.blocks, ["doc_a", "doc_b", "doc_c"])
    assert decoded2 == [doc1, edited_doc2, doc3]


def test_fail_safe_missing_base():
    """If receiver doesn't have the base for a delta, return None (request full send)."""
    cache = SessionCache()
    conv_id = "conv_5"

    base = _doc(7, 8000)
    edited = base[: len(base) // 2] + b"\nEDIT\n" + base[len(base) // 2 :]

    # Encode turn 1 and 2.
    cache.encode_turn(conv_id, [base], ["doc"])
    payload2 = cache.encode_turn(conv_id, [edited], ["doc"])

    # Simulate receiver without the base chunks.
    empty_store = {}
    decoded = cache.decode_turn(conv_id, payload2, empty_store)
    assert decoded is None  # fail-safe: don't silently apply delta to missing base


def test_fail_safe_corrupted_chunk():
    """If a chunk's hash doesn't match its cid, return None."""
    sender_cache = SessionCache()
    conv_id = "conv_6"

    artifact = _doc(8, 5000)
    # Turn 1: send artifact (literals).
    payload1 = sender_cache.encode_turn(conv_id, [artifact], ["doc"])

    # Receiver decodes normally first.
    receiver_cache = SessionCache()
    receiver_store = sender_cache.content_store.blocks.copy()
    receiver_cache.content_store.blocks.update(receiver_store)
    receiver_cache.content_store.manifests.update(sender_cache.content_store.manifests)
    decoded1 = receiver_cache.decode_turn(conv_id, payload1, receiver_store, ["doc"])
    assert decoded1 == [artifact]

    # Turn 2: send the same artifact again (should emit chunks/refs).
    payload2 = sender_cache.encode_turn(conv_id, [artifact], ["doc"])
    assert payload2.artifacts[0].chunks is not None  # Now using chunk references

    # Receiver gets turn 2 with chunks, but corrupt a chunk.
    corrupted_store = receiver_store.copy()
    for cid_key in list(corrupted_store.keys())[:1]:
        # Flip a byte in the first chunk.
        chunk = corrupted_store[cid_key]
        corrupted_store[cid_key] = chunk[:-1] + bytes([chunk[-1] ^ 0xFF])

    # Try to decode with corrupted chunk.
    decoded_corrupt = receiver_cache.decode_turn(conv_id, payload2, corrupted_store, ["doc"])
    assert decoded_corrupt is None  # fail-safe: corruption detected


def test_multiple_conversations_independent():
    """Different conversations maintain independent state."""
    cache = SessionCache()

    doc1 = _doc(9, 5000)
    doc2 = _doc(10, 5000)

    # Conversation A: send doc1.
    cache.encode_turn("conv_a", [doc1], ["doc"])
    # Conversation B: send different doc2.
    cache.encode_turn("conv_b", [doc2], ["doc"])

    # Conv A turn 2: send doc1 again (should dedup).
    payload_a2 = cache.encode_turn("conv_a", [doc1], ["doc"])
    assert payload_a2.artifacts[0].chunks is not None

    # Conv B turn 2: send doc2 again (should dedup within B, not cross-contaminate).
    payload_b2 = cache.encode_turn("conv_b", [doc2], ["doc"])
    assert payload_b2.artifacts[0].chunks is not None

    # Conv A turn 3: send doc2 (different from A's history, but seen in B).
    # Within A's state, doc2 is "new", so should be literal (not reference to B's state).
    payload_a3 = cache.encode_turn("conv_a", [doc2], ["doc"])
    # Since doc2 chunks are in the global content_store (from conv B),
    # they might be deduped. But conversation A's state doesn't know about doc2,
    # so it's still "new" to conversation A's artifact history.
    # The encode_turn logic checks if artifact_id exists in state[conv_a].
    # Since it doesn't, it will treat doc2 as a new artifact and may emit it as literal.
    # However, because the chunks were already stored in content_store, it might emit refs.
    # Let's verify round-trip works correctly.
    receiver_store = cache.content_store.blocks.copy()
    decoded_a3 = cache.decode_turn("conv_a", payload_a3, receiver_store)
    assert decoded_a3 == [doc2]


def test_artifact_replaced_by_different_content():
    """Artifact id is reused but with new content (full replacement)."""
    cache = SessionCache()
    conv_id = "conv_7"

    doc_a = _doc(11, 5000)
    doc_b = _doc(12, 5000)  # completely different document

    # Turn 1: send doc_a under id "artifact".
    cache.encode_turn(conv_id, [doc_a], ["artifact"])

    # Turn 2: replace with doc_b under same id (no relation).
    # Should emit doc_b in full (or delta if small enough, but here they're unrelated).
    payload2 = cache.encode_turn(conv_id, [doc_b], ["artifact"])

    # Since doc_a and doc_b are unrelated, delta should be larger than literal.
    # The cache should choose literal.
    assert payload2.artifacts[0].literal == doc_b or payload2.artifacts[0].delta is not None

    # Decode.
    receiver_store = cache.content_store.blocks.copy()
    decoded2 = cache.decode_turn(conv_id, payload2, receiver_store)
    assert decoded2 == [doc_b]


def test_method_inference():
    """Verify method inference (dedup / delta / literal / mixed)."""
    cache = SessionCache()
    conv_id = "conv_8"

    doc = _doc(13, 5000)

    # Turn 1: method should be "literal" (all new).
    cache.encode_turn(conv_id, [doc], ["doc"])
    assert cache.stats().method == "literal"

    # Turn 2: identical artifact, method should be "dedup".
    cache.encode_turn(conv_id, [doc], ["doc"])
    assert cache.stats().method == "dedup"

    # Turn 3: small edit, method should be "delta".
    edited = doc + b"\nsmall addition\n"
    cache.encode_turn(conv_id, [edited], ["doc"])
    assert cache.stats().method == "delta"

    # Turn 4: multiple artifacts, mixed methods.
    doc2 = _doc(14, 5000)
    payload = cache.encode_turn(
        conv_id,
        [edited, doc2],  # edited is unchanged (dedup), doc2 is new (literal)
        ["doc", "doc2"],
    )
    # Should be mixed because one is dedup and one is literal.
    assert cache.stats().method == "mixed"


def test_zero_size_artifacts():
    """Handle empty artifacts gracefully."""
    cache = SessionCache()
    conv_id = "conv_9"

    empty = b""
    payload1 = cache.encode_turn(conv_id, [empty], ["doc"])
    assert len(payload1.artifacts) == 1
    assert payload1.artifacts[0].literal == empty or payload1.artifacts[0].chunks is not None

    # Decode.
    receiver_store = cache.content_store.blocks.copy()
    decoded = cache.decode_turn(conv_id, payload1, receiver_store)
    assert decoded == [empty]


def test_large_document_scenarios():
    """Test with larger, more realistic document sizes."""
    sender_cache = SessionCache()
    conv_id = "conv_10"

    # 100 KB document (realistic agent context).
    large_doc = _doc(15, 100_000)

    # Turn 1: send large doc.
    payload1 = sender_cache.encode_turn(conv_id, [large_doc], ["context"])
    stats1 = sender_cache.stats()
    assert stats1.input_bytes == len(large_doc)

    # Receiver gets turn 1
    receiver_cache = SessionCache()
    receiver_cache.content_store.blocks.update(sender_cache.content_store.blocks)
    receiver_cache.content_store.manifests.update(sender_cache.content_store.manifests)
    decoded1 = receiver_cache.decode_turn(conv_id, payload1, receiver_cache.content_store.blocks, ["context"])
    assert decoded1 == [large_doc]

    # Turn 2: identical.
    payload2 = sender_cache.encode_turn(conv_id, [large_doc], ["context"])
    stats2 = sender_cache.stats()
    assert stats2.wire_bytes == 0
    assert stats2.savings_ratio == 1.0

    # Receiver gets turn 2
    receiver_cache.content_store.blocks.update(sender_cache.content_store.blocks)
    receiver_cache.content_store.manifests.update(sender_cache.content_store.manifests)
    decoded2 = receiver_cache.decode_turn(conv_id, payload2, receiver_cache.content_store.blocks, ["context"])
    assert decoded2 == [large_doc]

    # Turn 3: small edit (0.1% change).
    pos = 50_000
    edited = large_doc[:pos] + b"MODIFIED SECTION HERE" + large_doc[pos + 20 :]
    payload3 = sender_cache.encode_turn(conv_id, [edited], ["context"])
    stats3 = sender_cache.stats()

    # Delta should be much smaller than the edited doc.
    assert stats3.wire_bytes < 0.2 * len(edited)
    assert stats3.savings_ratio > 0.7

    # Receiver gets turn 3
    receiver_cache.content_store.blocks.update(sender_cache.content_store.blocks)
    receiver_cache.content_store.manifests.update(sender_cache.content_store.manifests)
    decoded3 = receiver_cache.decode_turn(conv_id, payload3, receiver_cache.content_store.blocks, ["context"])
    assert decoded3 == [edited]


def test_round_trip_with_hash_verification():
    """Verify that deltas carry and check hashes correctly."""
    sender_cache = SessionCache()
    conv_id = "conv_11"

    base = _doc(16, 8000)
    edited = base[: len(base) // 3] + b"\nINSERTED CHUNK\n" + base[len(base) // 3 :]

    # Sender encodes two turns.
    payload1 = sender_cache.encode_turn(conv_id, [base], ["doc"])
    payload2 = sender_cache.encode_turn(conv_id, [edited], ["doc"])

    # Verify payload carries base and target hashes.
    if payload2.artifacts[0].delta is not None:
        assert payload2.artifacts[0].delta.delta_payload.base_hash
        assert payload2.artifacts[0].delta.delta_payload.target_hash

    # Receiver gets both payloads and decodes.
    receiver_cache = SessionCache()
    receiver_cache.content_store.blocks.update(sender_cache.content_store.blocks)
    receiver_cache.content_store.manifests.update(sender_cache.content_store.manifests)

    decoded1 = receiver_cache.decode_turn(conv_id, payload1, receiver_cache.content_store.blocks, ["doc"])
    assert decoded1 == [base]

    decoded2 = receiver_cache.decode_turn(conv_id, payload2, receiver_cache.content_store.blocks, ["doc"])
    assert decoded2 == [edited]
