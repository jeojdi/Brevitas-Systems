"""Dedup/delta wiring for the live path (brief b3, pain point P2).

Honest scope — two distinct channels, because an LLM provider is a black box:

  A. PROVIDER channel (Claude/OpenAI/DeepSeek): the model needs real text; it cannot
     reconstruct a VCDIFF/CID payload. So the provider-DOLLAR win from dedup/delta is
     NOT "send a diff" — it is CACHE-STABLE ORDERING: across turns, artifacts the agent
     re-sends UNCHANGED are kept byte-identical AND in a fixed order, so a change to one
     file doesn't shift the others past a cache breakpoint and bust the provider prefix
     cache. `stable_order()` provides that; it is strictly lossless (a pure reordering of
     the context block, content untouched).

  B. RECEIVER channel (Brevitas SDK -> self-hosted Brevitas gateway, both run our code):
     here we CAN send references + deltas and reconstruct exactly. `SessionCache`
     (session_cache.py) already implements IPFS-CID dedup + Myers/VCDIFF/rsync delta with
     hash-verified reconstruction and fail-safe to full send. `measure_redundancy()`
     reports the real bytes that channel would save, with a verified lossless round-trip.

This module is glue only — the algorithms live in content_store.py / delta.py /
session_cache.py (all paper-grounded). Nothing here invents an algorithm.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .content_store import cid
from .session_cache import SessionCache

_MAX_SESSIONS = 1024


@dataclass
class _ArtifactState:
    order: List[str] = field(default_factory=list)   # artifact-id in first-seen order
    cids: Dict[str, str] = field(default_factory=dict)  # artifact-id -> last content cid


class DedupDeltaLayer:
    """Per-session artifact tracker producing cache-stable ordering (provider channel)
    and real dedup measurement (receiver channel)."""

    def __init__(self) -> None:
        self._sessions: "OrderedDict[str, _ArtifactState]" = OrderedDict()
        self._cache = SessionCache()
        # persistent receiver mirror: accumulates prior-turn state so delta payloads
        # (which reference a base the receiver already holds) reconstruct correctly —
        # exactly the real sender→receiver channel.
        self._mirror = SessionCache(content_store=self._cache.content_store)

    def _state(self, session_id: str) -> _ArtifactState:
        st = self._sessions.get(session_id)
        if st is None:
            if len(self._sessions) >= _MAX_SESSIONS:
                self._sessions.popitem(last=False)
            st = _ArtifactState()
            self._sessions[session_id] = st
        else:
            self._sessions.move_to_end(session_id)
        return st

    def classify(self, session_id: str, artifacts: List[Tuple[str, str]]) -> Dict[str, str]:
        """Classify each (artifact_id, text) as 'unchanged' | 'edited' | 'new' vs the
        session's last-seen version. Updates state. Pure bookkeeping (no transmission)."""
        st = self._state(session_id)
        out: Dict[str, str] = {}
        for aid, text in artifacts:
            c = cid(text.encode("utf-8"))
            if aid not in st.cids:
                out[aid] = "new"
                st.order.append(aid)
            elif st.cids[aid] == c:
                out[aid] = "unchanged"
            else:
                out[aid] = "edited"
            st.cids[aid] = c
        return out

    def stable_order(self, session_id: str,
                     artifacts: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
        """Return artifacts reordered for provider cache stability: previously-seen
        artifacts first, in their ORIGINAL first-seen order (so the cached prefix is
        preserved even when one of them was edited or a new one arrives), then any
        brand-new artifacts appended. Lossless: same set, only reordered.

        NOTE: only reorder when it cannot change model semantics — the caller passes
        artifacts that are order-independent context blocks (e.g. attached files), not
        conversational turns.
        """
        st = self._state(session_id)
        self.classify(session_id, artifacts)  # ensure order/cids updated
        by_id = {aid: (aid, text) for aid, text in artifacts}
        ordered: List[Tuple[str, str]] = []
        for aid in st.order:                 # stable historical order first
            if aid in by_id:
                ordered.append(by_id.pop(aid))
        for aid, text in artifacts:          # then anything not yet in history
            if aid in by_id:
                ordered.append(by_id.pop(aid))
        return ordered

    def measure_redundancy(self, session_id: str,
                           artifacts: List[Tuple[str, str]]) -> dict:
        """RECEIVER-channel measurement: how many bytes the CID+delta wire format would
        save this turn, with a verified lossless round-trip. Reports transport savings —
        NOT provider-dollar savings (see module docstring). Fail-safe: on any
        reconstruction mismatch, reports zero savings and lossless=False."""
        ids = [a for a, _ in artifacts]
        blobs = [t.encode("utf-8") for _, t in artifacts]
        payload = self._cache.encode_turn(session_id, blobs, ids)
        stats = self._cache.stats()
        # verify lossless round-trip against the PERSISTENT receiver mirror (it holds
        # prior turns' bases, so deltas resolve — like the real receiver channel)
        chunk_store = dict(self._cache.content_store.blocks)
        decoded = self._mirror.decode_turn(session_id, payload, chunk_store, ids)
        lossless = decoded is not None and decoded == blobs
        return {
            "input_bytes": stats.input_bytes,
            "wire_bytes": stats.wire_bytes if lossless else stats.input_bytes,
            "savings_bytes": stats.savings_bytes if lossless else 0,
            "savings_ratio": stats.savings_ratio if lossless else 0.0,
            "method": stats.method,
            "lossless": lossless,
        }
