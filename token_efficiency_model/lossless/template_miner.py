"""Cross-run prompt-template mining (brief CR2) — Drain3-based, lossless.

The cross-run problem: a customer's app is run repeatedly (daily pipelines, restarts,
many tickers/tenants). The prompt is a stable TEMPLATE with volatile SLOTS ("run
2026-07-01, ticker AAPL, ..."), and a volatile token early in the text breaks the
provider's byte-prefix cache from that point on — every run re-bills the whole prompt.

Method (published + released library, per house rule): Drain — the online log-template
miner (fixed-depth parse tree, O(len) per message; He et al.; maintained PyPI package
`drain3`, MIT). Recurring prompts cluster into `template + <*> slots`; the char offset
of the FIRST volatile slot is the stable/volatile boundary.

What the boundary is used for (both fail-safe):
  * always: advisory metadata — "your prompt goes volatile at offset N" (cache doctor).
  * flag-gated (BREVITAS_TEMPLATE_SPLIT=1): on Anthropic, split the block at the
    boundary into two text blocks with byte-identical concatenation, so the stable
    prefix can carry its own cache breakpoint. Gated until block-join semantics are
    verified byte-level per provider (token-count check in the acceptance run).

No drain3 installed / cluster not yet recurring / no volatile slot ⇒ boundary = full
length ⇒ behavior unchanged. Never raises into the request path.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Optional

_MAX_KEYS = 512
_TOKEN_RE = re.compile(r"\S+")


class PromptTemplateMiner:
    """One Drain miner per workload key (session/agent); LRU-bounded."""

    def __init__(self, min_cluster: int = 2, window_chars: int = 8000):
        self.min_cluster = min_cluster        # boundary trusted once seen >= this often
        self.window = window_chars            # analyze the head; volatile-early is the problem
        self._miners: "OrderedDict[str, object]" = OrderedDict()

    def _miner(self, key: str) -> Optional[object]:
        try:
            from drain3 import TemplateMiner
            from drain3.template_miner_config import TemplateMinerConfig
        except Exception:
            return None                       # fail-safe: feature off without drain3
        m = self._miners.get(key)
        if m is None:
            if len(self._miners) >= _MAX_KEYS:
                self._miners.popitem(last=False)
            cfg = TemplateMinerConfig()       # defaults, no masking: byte-faithful tokens
            m = TemplateMiner(config=cfg)
            self._miners[key] = m
        else:
            self._miners.move_to_end(key)
        return m

    def observe_boundary(self, key: str, text: str) -> int:
        """Feed one prompt occurrence; return the char offset where the STABLE template
        prefix ends (= start of the first volatile token), or len(text) when the
        template is fully stable, not yet recurring, or mining is unavailable."""
        if not text:
            return 0
        m = self._miner(key)
        if m is None:
            return len(text)
        head = text[: self.window]
        try:
            res = m.add_log_message(head)
        except Exception:
            return len(text)
        if not isinstance(res, dict) or res.get("cluster_size", 0) < self.min_cluster:
            return len(text)
        template = res.get("template_mined") or ""
        t_tokens = template.split()
        vol_idx = next((i for i, t in enumerate(t_tokens) if "<*>" in t), None)
        if vol_idx is None:
            return len(text)                  # recurring AND fully stable → nothing volatile
        spans = list(_TOKEN_RE.finditer(head))
        if vol_idx >= len(spans):
            return len(text)
        return spans[vol_idx].start()


# process-wide default (proxy/engine use); apps may create their own
default_miner = PromptTemplateMiner()
