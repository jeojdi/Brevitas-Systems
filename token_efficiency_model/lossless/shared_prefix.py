"""Shared-prefix promotion for multi-agent pipelines (brief b9, P1-for-fleets).

The measured gap: in a multi-agent pipeline every agent has a DISTINCT system prompt,
so the shared context (a brief, a 10-K fact sheet, a codebase) sits BEHIND the differing
prefix. Provider prefix caches match from token 0, so the shared context — the big,
identical, expensive part — never caches across agents. (This is why the marketing
5-agent A/B saved only ~5% while single-agent multi-turn saved 70-88%.)

The fix, strictly lossless: hoist the byte-identical shared block to the FRONT of every
agent's request so it becomes the cacheable leading prefix, with the per-agent
role/instruction following it. The model receives the exact same information —
"reference material, then your role, then your task" is a standard, valid layout — so
no answer changes; only message ORDER changes, and only for content proven identical
across agents.

Two ways to identify the shared block:
  * explicit: register_shared(pipeline_id, text) — the app declares it once.
  * automatic: content seen from >=2 DISTINCT agents in the same pipeline is promoted
    on subsequent calls (no promotion until we've actually observed it shared, so a
    single-agent session is never reordered).

Nothing here is provider-specific and no algorithm is invented — it's a lossless
message-ordering transform that lets the existing caching lever reach shared context.
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

_MAX_PIPELINES = 1024


def _norm(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class _PipelineState:
    # content-hash -> set of distinct agent labels that have sent it
    seen: Dict[str, Set[str]] = field(default_factory=dict)
    # content-hash -> the canonical message (first seen) for reconstruction
    canonical: Dict[str, dict] = field(default_factory=dict)
    # explicitly-registered shared hashes (always promoted)
    explicit: Set[str] = field(default_factory=set)


class SharedPrefixLayer:
    """Promote a pipeline's shared context to a leading, byte-identical prefix."""

    def __init__(self, min_agents: int = 2) -> None:
        self.min_agents = min_agents
        self._pipelines: "OrderedDict[str, _PipelineState]" = OrderedDict()

    def _state(self, pipeline_id: str) -> _PipelineState:
        st = self._pipelines.get(pipeline_id)
        if st is None:
            if len(self._pipelines) >= _MAX_PIPELINES:
                self._pipelines.popitem(last=False)
            st = _PipelineState()
            self._pipelines[pipeline_id] = st
        else:
            self._pipelines.move_to_end(pipeline_id)
        return st

    def register_shared(self, pipeline_id: str, text: str) -> None:
        """Explicitly declare a block as shared across this pipeline's agents."""
        st = self._state(pipeline_id)
        st.explicit.add(_h(text))

    def _is_shared(self, st: _PipelineState, h: str) -> bool:
        return h in st.explicit or len(st.seen.get(h, ())) >= self.min_agents

    def layout(self, pipeline_id: str, agent: str, messages: List[dict],
               natural_cached_tokens: float = 0.0, min_gain_tokens: int = 500,
               min_gain_frac: float = 0.02, count_tokens=None) -> List[dict]:
        """Return messages with shared context hoisted to a stable leading prefix —
        CACHE-AWARE (b9 v2). Only reorders when promoting the shared block would cache
        MORE tokens than the provider already caches in the natural order:

            promote  iff  L_reorder  >  L_natural + max(min_gain_tokens, min_gain_frac·total)

        where L_reorder = tokens in the promoted shared block (the new leading cacheable
        prefix) and L_natural = the caller's estimate of the currently-cached prefix
        (observed_cache_hit_rate × total_input_tokens). This is the "Don't Break the
        Cache" (arXiv 2601.06007) safety invariant — never shrink an already-cached
        prefix — with CacheWeaver's objective of maximizing the longest cached prefix.

        Lossless either way: same message set, only reordered, only proven-shared content
        moved, volatile last message never moved. `count_tokens` defaults to a cheap
        word estimate if not supplied."""
        if not messages:
            return messages
        st = self._state(pipeline_id)
        if count_tokens is None:
            count_tokens = lambda t: max(1, int(len((t or "").split()) * 1.3))

        # observe: record which agent sent each content block (all but the volatile last)
        for m in messages[:-1]:
            text = _norm(m.get("content"))
            if not text:
                continue
            h = _h(text)
            st.seen.setdefault(h, set()).add(agent)
            st.canonical.setdefault(h, m)

        shared_idx = []
        rest_idx = []
        for i, m in enumerate(messages[:-1]):
            h = _h(_norm(m.get("content")))
            (shared_idx if self._is_shared(st, h) else rest_idx).append((i, m, h))

        if not shared_idx:
            return messages  # nothing proven shared yet → don't reorder

        # stable order for the shared prefix: by content hash (identical across agents),
        # so every agent emits the SAME leading bytes and the provider caches them.
        shared_sorted = sorted(shared_idx, key=lambda t: t[2])
        promoted = [m for _, m, _ in shared_sorted]

        # CACHE-AWARE GATE: only reorder if the promoted shared block would be a LARGER
        # cacheable prefix than what the provider already caches in the natural order.
        l_reorder = sum(count_tokens(_norm(m.get("content"))) for m in promoted)
        total = sum(count_tokens(_norm(m.get("content"))) for m in messages)
        threshold = natural_cached_tokens + max(min_gain_tokens, int(min_gain_frac * total))
        if l_reorder <= threshold:
            return messages  # reorder would not beat the existing/likely cache → leave it

        remainder = [m for _, m, _ in rest_idx]
        return promoted + remainder + [messages[-1]]


# process-wide default layer (bounded); apps may create their own
_default = SharedPrefixLayer()


def layout(pipeline_id: str, agent: str, messages: List[dict],
           natural_cached_tokens: float = 0.0, count_tokens=None) -> List[dict]:
    return _default.layout(pipeline_id, agent, messages,
                           natural_cached_tokens=natural_cached_tokens,
                           count_tokens=count_tokens)


def register_shared(pipeline_id: str, text: str) -> None:
    _default.register_shared(pipeline_id, text)
