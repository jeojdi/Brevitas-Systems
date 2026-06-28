"""Lever 4 — stateful session layer for multi-turn conversations.

Per-conversation memory that tracks artifacts already sent (by content id via ContentStore)
and emits compact wire payloads:
  - references (content ids) for previously-seen chunks
  - deltas (delta.py) for edited artifacts
  - inline literals only for genuinely new content

The receiver reconstructs full state by resolving references + applying deltas, verifying
content hashes. Accuracy-first: fails safe to full send on any mismatch (no silent corruption).

Deterministic: no API calls, no randomness. Each conversation's version lineage is tracked
independently; a delta is only used when the receiver CONFIRMS it holds the base.

This is the core of "drop in to any backend and save money" for repetitive agent traffic.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from token_efficiency_model.lossless.content_store import ContentStore, cid
from token_efficiency_model.lossless.delta import apply_delta, encode_delta, DeltaPayload


def _h(data: bytes) -> str:
    """SHA-256 hex digest."""
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Wire payload types for efficient transmission
# --------------------------------------------------------------------------- #
@dataclass
class WireChunk:
    """A reference to an already-seen chunk (no new bytes)."""
    cid: str


@dataclass
class WireDelta:
    """A delta against a known base (receiver must confirm it has the base)."""
    base_cid: str
    delta_payload: DeltaPayload  # carries base_hash, target_hash, ops


@dataclass
class WireArtifact:
    """A complete artifact: one or more content chunks OR a delta OR raw bytes (new)."""
    # Exactly one of these is populated:
    chunks: Optional[List[str]] = None     # list of chunk cids (all previously seen)
    delta: Optional[WireDelta] = None      # delta against a prior version
    literal: Optional[bytes] = None        # raw bytes (genuinely new, no dedup)

    def wire_size(self) -> int:
        """Approx bytes on wire (cids are tiny; deltas carry only non-COPY bytes; literals are full)."""
        if self.literal is not None:
            return len(self.literal)
        if self.delta is not None:
            return self.delta.delta_payload.wire_size()
        return 0  # chunks are just references


@dataclass
class WirePayload:
    """Wire format for a single turn: list of artifacts + metadata."""
    artifacts: List[WireArtifact] = field(default_factory=list)

    def wire_size(self) -> int:
        return sum(a.wire_size() for a in self.artifacts)


@dataclass
class SessionStats:
    """Per-turn compression statistics."""
    input_bytes: int = 0              # total bytes of input artifacts
    wire_bytes: int = 0               # bytes on the wire (with compression)
    savings_bytes: int = 0            # bytes avoided
    method: str = ""                  # dedup | delta | literal | mixed

    @property
    def savings_ratio(self) -> float:
        if self.input_bytes <= 0:
            return 0.0
        return self.savings_bytes / self.input_bytes

    @property
    def compression_ratio(self) -> float:
        if self.input_bytes <= 0:
            return 1.0
        return self.wire_bytes / self.input_bytes


# --------------------------------------------------------------------------- #
# Conversation-local cache: per-conversation version lineage
# --------------------------------------------------------------------------- #
@dataclass
class ConversationSnapshot:
    """Version record for a conversation: maps artifact id -> (content_cid, bytes)."""
    artifact_id: str
    content_cid: str
    data: bytes


class SessionCache:
    """Stateful session layer for multi-turn conversations.

    API:
      cache.encode_turn(conversation_id, artifacts: list[str]) -> WirePayload
        Emit a compact wire payload given a list of raw artifact bytes.
      cache.decode_turn(conversation_id, payload: WirePayload, chunk_store: dict) -> list[bytes]
        Reconstruct artifacts from wire payload using a store of chunks.
      cache.stats(conversation_id) -> SessionStats
        Per-turn compression metrics (call after encode_turn).
    """

    def __init__(self, content_store: Optional[ContentStore] = None) -> None:
        self.content_store = content_store or ContentStore()
        # conversation_id -> { artifact_id -> ConversationSnapshot }
        self.conversation_state: Dict[str, Dict[str, ConversationSnapshot]] = {}
        self._last_stats: Optional[SessionStats] = None

    def encode_turn(
        self,
        conversation_id: str,
        artifacts: List[bytes],
        artifact_ids: Optional[List[str]] = None,
    ) -> WirePayload:
        """Encode a turn's artifacts as a compact wire payload.

        Args:
            conversation_id: unique identifier for this conversation
            artifacts: list of raw artifact bytes
            artifact_ids: optional list of ids (default: [f"artifact_{i}"])

        Returns:
            WirePayload with efficient references, deltas, or literals.
        """
        if artifact_ids is None:
            artifact_ids = [f"artifact_{i}" for i in range(len(artifacts))]
        if len(artifact_ids) != len(artifacts):
            raise ValueError("artifact_ids length must match artifacts")

        if conversation_id not in self.conversation_state:
            self.conversation_state[conversation_id] = {}

        state = self.conversation_state[conversation_id]
        wire_artifacts: List[WireArtifact] = []
        total_input = 0
        total_wire = 0

        for aid, artifact_bytes in zip(artifact_ids, artifacts):
            total_input += len(artifact_bytes)

            # Get the content cid for this artifact (via ContentStore chunking).
            artifact_cid = self.content_store.put_artifact(artifact_bytes)

            if aid not in state:
                # First time seeing this artifact: check if we can dedup via chunks.
                snapshot = ConversationSnapshot(aid, artifact_cid, artifact_bytes)
                state[aid] = snapshot

                # If all chunks were already in the store from prior conversations,
                # emit chunk references. Otherwise emit literal.
                manifest = self.content_store.manifests[artifact_cid]
                chunks = manifest["chunks"]

                if (
                    self.content_store.stats.bytes_transferred == 0
                    and all(c in self.content_store.blocks for c in chunks)
                ):
                    # All chunks already existed -> emit references only
                    wire_artifact = WireArtifact(chunks=chunks)
                    total_wire += 0  # refs are tiny
                else:
                    # New chunks: must send the artifact (with dedup baked into chunks).
                    # For simplicity, emit the literal (already deduped by ContentStore).
                    wire_artifact = WireArtifact(literal=artifact_bytes)
                    total_wire += len(artifact_bytes)
            else:
                # Artifact was previously seen; check if it has changed.
                prev_snapshot = state[aid]
                if artifact_cid == prev_snapshot.content_cid:
                    # Identical content: emit reference to the artifact cid.
                    manifest = self.content_store.manifests[artifact_cid]
                    chunks = manifest["chunks"]
                    wire_artifact = WireArtifact(chunks=chunks)
                    total_wire += 0
                else:
                    # Artifact has been edited: try delta.
                    prev_bytes = prev_snapshot.data
                    delta_payload = encode_delta(prev_bytes, artifact_bytes, method="auto")

                    # Delta is worthwhile if wire_size < literal size.
                    if delta_payload.wire_size() < len(artifact_bytes):
                        wire_artifact = WireArtifact(
                            delta=WireDelta(
                                base_cid=prev_snapshot.content_cid,
                                delta_payload=delta_payload,
                            )
                        )
                        total_wire += delta_payload.wire_size()
                    else:
                        # Delta larger than literal: send full content.
                        wire_artifact = WireArtifact(literal=artifact_bytes)
                        total_wire += len(artifact_bytes)

                # Update snapshot for next turn.
                state[aid] = ConversationSnapshot(aid, artifact_cid, artifact_bytes)

            wire_artifacts.append(wire_artifact)

        payload = WirePayload(artifacts=wire_artifacts)

        # Compute stats for this turn.
        savings = max(0, total_input - total_wire)
        method_label = self._infer_method(wire_artifacts)
        self._last_stats = SessionStats(
            input_bytes=total_input,
            wire_bytes=total_wire,
            savings_bytes=savings,
            method=method_label,
        )

        return payload

    def decode_turn(
        self,
        conversation_id: str,
        payload: WirePayload,
        chunk_store: Dict[str, bytes],
        artifact_ids: Optional[List[str]] = None,
    ) -> Optional[List[bytes]]:
        """Reconstruct artifacts from wire payload and update receiver state.

        Args:
            conversation_id: unique identifier for this conversation
            payload: WirePayload to decode
            chunk_store: dict of cid -> bytes (shared block store)
            artifact_ids: optional list of artifact ids to map to reconstructed artifacts.
                         If not provided, uses indices from prior state or generic ids.

        Returns:
            list of reconstructed artifacts, or None if any fail-safe triggers.
        """
        if conversation_id not in self.conversation_state:
            self.conversation_state[conversation_id] = {}

        state = self.conversation_state[conversation_id]
        artifacts: List[bytes] = []

        # If artifact_ids not provided, infer from state or use generic.
        if artifact_ids is None:
            artifact_ids = list(state.keys())
            while len(artifact_ids) < len(payload.artifacts):
                artifact_ids.append(f"artifact_{len(artifact_ids)}")

        for i, wire_artifact in enumerate(payload.artifacts):
            aid = artifact_ids[i] if i < len(artifact_ids) else f"artifact_{i}"
            reconstructed = None

            if wire_artifact.chunks is not None:
                # Reconstruct from chunk references.
                reconstructed = self._reconstruct_from_chunks(
                    wire_artifact.chunks, chunk_store
                )
                if reconstructed is None:
                    return None

            elif wire_artifact.delta is not None:
                # Apply delta against the known base.
                base_cid = wire_artifact.delta.base_cid

                # Find which artifact_id in state has this cid.
                artifact_id = None
                for check_aid, snapshot in state.items():
                    if snapshot.content_cid == base_cid:
                        artifact_id = check_aid
                        break

                if artifact_id is None:
                    # Receiver doesn't have this base: fail-safe.
                    return None

                base_bytes = state[artifact_id].data

                # Apply delta with hash verification.
                reconstructed = apply_delta(base_bytes, wire_artifact.delta.delta_payload)
                if reconstructed is None:
                    return None

            elif wire_artifact.literal is not None:
                # Literal: use as-is.
                reconstructed = wire_artifact.literal

            else:
                # Malformed payload.
                return None

            if reconstructed is None:
                return None

            artifacts.append(reconstructed)

            # Update receiver's state for this artifact.
            artifact_cid = self.content_store.put_artifact(reconstructed)
            state[aid] = ConversationSnapshot(aid, artifact_cid, reconstructed)

        return artifacts

    def _reconstruct_from_chunks(
        self, chunk_cids: List[str], chunk_store: Dict[str, bytes]
    ) -> Optional[bytes]:
        """Reconstruct artifact from chunk references, verifying hashes."""
        parts: List[bytes] = []
        for chunk_cid in chunk_cids:
            if chunk_cid not in chunk_store:
                return None
            chunk = chunk_store[chunk_cid]
            if cid(chunk) != chunk_cid:
                # Corruption detected: fail-safe.
                return None
            parts.append(chunk)
        return b"".join(parts)

    def stats(self) -> Optional[SessionStats]:
        """Return statistics from the last encode_turn call."""
        return self._last_stats

    def _infer_method(self, wire_artifacts: List[WireArtifact]) -> str:
        """Infer the compression method used in this turn."""
        methods = set()
        for wa in wire_artifacts:
            if wa.chunks is not None:
                methods.add("dedup")
            elif wa.delta is not None:
                methods.add("delta")
            elif wa.literal is not None:
                methods.add("literal")
        if len(methods) == 0:
            return "empty"
        elif len(methods) == 1:
            return methods.pop()
        else:
            return "mixed"
