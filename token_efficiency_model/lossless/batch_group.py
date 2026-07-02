"""Batch-level prefix grouping — the "pathfinder gate" (brief CR1, lossless).

The race this fixes: a provider cache entry only becomes readable after the FIRST
request carrying that prefix finishes prefill. When a fleet fires N requests sharing a
large identical prefix in the same instant (18 agents, 500-document batch jobs), all N
miss the cache and all N pay the full write price for the same bytes — the cache
existed, but everyone arrived before anyone had filled it.

Gateway adaptation of published work (verified in docs/research/sweep/CR1):
  * BatchLLM (arXiv:2412.03594) — identify GLOBAL common prefixes across a batch and
    schedule sharers together so the shared prefix is computed once.
  * PRISM (arXiv:2605.08581) — align request admission with exact-prefix cache
    retention (schedule requests to hit the prefixes currently held).

Mechanism here (scheduling only — request CONTENT is never touched, so strictly
lossless; it trades a bounded hold on burst siblings for the cached price):
  1. signature(): hash of the request's leading stable bytes (system + all but the
     final message), only when big enough to be cacheable at all.
  2. First in-flight request per signature = PATHFINDER: passes through immediately.
  3. Same-signature requests arriving while the pathfinder is in flight WAIT (bounded
     by max_wait) until the pathfinder's prefill has written the provider cache, then
     proceed — reading at ~0.1-0.5x instead of re-writing at 1.0-1.25x.
  4. After release, the signature is WARM for ~0.8x the provider cache TTL: siblings
     pass through untouched. Warm expiry ⇒ next request is a new pathfinder.

Safety: interactive traffic never has an identical-prefix sibling in flight (turn N+1
starts after turn N returns), so it is never held — "batch-ish" traffic is detected by
the concurrency itself. Timeouts, exceptions and kill-switch all fail open.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Optional, Tuple

_MAX_SIGS = 4096


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


class BatchGroupGate:
    def __init__(self, max_wait: float = 15.0, min_chars: int = 4000):
        self.max_wait = max_wait          # hard cap on how long a sibling is held
        self.min_chars = min_chars        # ~1000 tokens: below any cacheable minimum
        self._inflight: dict[str, asyncio.Event] = {}
        self._warm: dict[str, float] = {}          # signature -> warm-until (epoch s)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- signature
    def signature(self, body: dict) -> Optional[str]:
        """Hash of the leading stable bytes (system + all but the final message).
        None when too small to be cacheable — the gate then stays out of the way.
        Computed AFTER optimization so it reflects the bytes actually sent."""
        if not isinstance(body, dict):
            return None
        msgs = body.get("messages") or []
        if not msgs:
            return None
        parts = [_text(body.get("system"))]
        for m in msgs[:-1]:
            if isinstance(m, dict):
                parts.append(f"{m.get('role', '')}\x00{_text(m.get('content'))}")
        # non-final blocks INSIDE the final message are stable context too (the
        # "big document + question in one turn" pattern, incl. Anthropic's
        # alternating-role constraint) — same rule as apply_anthropic_cache
        last = msgs[-1] if isinstance(msgs[-1], dict) else {}
        lc = last.get("content")
        if isinstance(lc, list) and len(lc) >= 2:
            for blk in lc[:-1]:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(f"lastblk\x00{blk.get('text', '')}")
        stable = "\x1e".join(parts)
        if len(stable) < self.min_chars:
            return None
        return hashlib.sha256(stable[:16384].encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ gate
    async def acquire(self, sig: str) -> Tuple[str, float]:
        """Returns (role, waited_seconds). role: 'pathfinder' (caller MUST release()),
        'grouped' (was held until the pathfinder finished), 'free' (warm/no contention)."""
        now = time.time()
        if self._warm.get(sig, 0.0) > now:
            return "free", 0.0
        async with self._lock:
            ev = self._inflight.get(sig)
            if ev is None:
                self._inflight[sig] = asyncio.Event()
                return "pathfinder", 0.0
        t0 = time.time()
        try:
            await asyncio.wait_for(ev.wait(), timeout=self.max_wait)
        except asyncio.TimeoutError:
            pass                                    # fail open: never block forever
        return "grouped", time.time() - t0

    def release(self, sig: str, warm_ttl: float = 240.0) -> None:
        """Pathfinder's prefill is done (first token or full response): wake siblings
        and mark the signature warm. Idempotent; safe in finally blocks."""
        ev = self._inflight.pop(sig, None)
        if ev is not None:
            ev.set()
        if len(self._warm) >= _MAX_SIGS:            # cheap prune: drop expired first
            now = time.time()
            for k in [k for k, v in self._warm.items() if v <= now][:_MAX_SIGS // 4] \
                    or list(self._warm)[:_MAX_SIGS // 4]:
                self._warm.pop(k, None)
        self._warm[sig] = time.time() + warm_ttl
