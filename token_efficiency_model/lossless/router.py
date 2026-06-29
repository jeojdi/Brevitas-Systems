"""Auto-router — picks the cheapest lever per request, automatically.

The benchmarks showed the right lever depends on (a) whether the bulk context REPEATS across
calls, (b) the provider's prompt-cache discount, (c) how large the context is, and (d) the
task. This router estimates the amortized input cost of each available lever per request and
picks the cheapest:

  * "cache_only" : keep the full context, rely on provider prefix caching (best when the
                   context repeats AND the provider caches strongly, e.g. DeepSeek/Anthropic).
  * "retrieve"   : send only the relevant retrieved chunks (best when the context is LARGE —
                   e.g. a whole PDF/textbook — because only a slice is relevant per question;
                   beats caching once the doc is big enough). LOSSLESS: fails safe to full.
  * "compress"   : LLMLingua-2 prompt compression (best for lossy-tolerant tasks — marketing
                   copy, boilerplate). Only considered when `allow_lossy=True`. LOSSY.
  * "passthrough": context too small to bother.

Decision is by estimated amortized input cost — no guessing. The retrieval estimate models a
real token BUDGET (you keep ~k chunks, not a flat fraction), so retrieval correctly wins on
big documents where a flat-fraction model would underrate it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .provider_cache import count_tokens
from .task_router import classify_task, _DEFAULT_RATES as _COMPRESS_RATES

# cached-token price as a fraction of fresh input price (kept in sync with provider_cache._RATES)
CACHE_DISCOUNT = {
    "anthropic": 0.10,   # cache read ~10% of input
    "deepseek": 0.259,   # cache-hit $0.07 vs $0.27 fresh -> ~26% (NOT 90%)
    "openai": 0.50,      # cached input ~50%
    "groq": 1.00,        # no caching
    "default": 0.50,
}
MIN_CACHEABLE = 1024     # provider minimum cacheable prefix (tokens)

# A "broad" question needs the WHOLE document (summarize / rank / overview / compare across it).
# Retrieval (a few similar chunks) is unsafe for these — it gives the model a partial view and the
# answer degrades. So we force cache_only (send full context, let the provider cache discount it)
# and accept a smaller token saving in exchange for a correct answer. Specific lookups stay on
# retrieval. This is the per-question "mix" of the caching and retrieval levers.
_BROAD_QUERY = re.compile(
    r"\b(summar(y|ize|ise)|overview|outline|table of contents|tl;?dr|abstract|"
    r"top \w+|most important|main (idea|point|theme|topic|algorithm|concept)s?|"
    r"key (point|idea|takeaway|theme|concept)s?|entire (book|document|text|pdf)|"
    r"whole (book|document|text|pdf)|across the (book|document|text)|throughout|"
    r"list all|all the (main|key|major)|compare|contrast|overall|in general|high.?level|"
    r"what('?s| is) (this|the) (book|document|pdf|text|paper) about|"
    r"how many (chapters|sections|parts)|structure of)\b", re.I)


def classify_scope(query: str) -> str:
    """Return 'broad' (needs the whole document) or 'local' (a specific lookup)."""
    return "broad" if _BROAD_QUERY.search(query or "") else "local"


@dataclass
class RouteDecision:
    strategy: str                 # cache_only | retrieve | compress | passthrough
    reason: str
    task: str                     # classified task of the volatile query
    costs: Dict[str, float]       # estimated amortized input cost per candidate lever
    repeat_rate: float            # 0..1 how often this session's context has repeated
    provider_cache_discount: float

    # back-compat fields (older callers read these directly)
    @property
    def est_cost_cache_only(self) -> float:
        return self.costs.get("cache_only", 0.0)

    @property
    def est_cost_retrieve(self) -> float:
        return self.costs.get("retrieve", 0.0)


@dataclass
class _SessionState:
    last_prefix_hash: str = ""
    calls: int = 0
    repeats: int = 0
    # observed REAL cache performance from provider usage: EWMA of cached/prompt fraction
    obs_hit: float = -1.0          # -1 = no observation yet
    obs_count: int = 0


@dataclass
class BrevitasRouter:
    provider: str = "openai"
    # fraction of a large context that retrieval typically keeps when the doc is small-ish
    retrieve_keep_frac: float = 0.6
    # hard token budget retrieval keeps regardless of context size (~k chunks). For a big doc
    # this is what makes retrieval cheap: keep ~this many tokens, not a flat fraction.
    retrieve_budget_tokens: int = 3000
    # consider the LOSSY compression lever (LLMLingua-2). Off by default (accuracy-first).
    allow_lossy: bool = False
    # per-task keep ratio for the compress lever (shared with TaskCompressionRouter)
    compress_rates: Dict[str, float] = field(default_factory=lambda: dict(_COMPRESS_RATES))

    _sessions: Dict[str, _SessionState] = field(default_factory=dict)

    def _discount(self) -> float:
        return CACHE_DISCOUNT.get(self.provider.lower(), CACHE_DISCOUNT["default"])

    def observe_usage(self, session_id: str, prompt_tokens: int, cached_tokens: int) -> None:
        """Feed back REAL provider usage so the router learns the provider's actual cache
        hit rate (advertised discounts don't always activate). Call after each cache_only
        request with the response's usage. EWMA of cached/prompt fraction."""
        if prompt_tokens <= 0:
            return
        st = self._sessions.setdefault(session_id, _SessionState())
        hit = max(0.0, min(1.0, cached_tokens / prompt_tokens))
        st.obs_hit = hit if st.obs_hit < 0 else 0.5 * st.obs_hit + 0.5 * hit
        st.obs_count += 1

    def _observe(self, session_id: str, stable_context: Sequence[str]) -> float:
        """Update repeat tracking; return this session's running repeat rate."""
        st = self._sessions.setdefault(session_id, _SessionState())
        h = hashlib.sha256("".join(stable_context).encode("utf-8")).hexdigest()
        st.calls += 1
        if h == st.last_prefix_hash:
            st.repeats += 1
        st.last_prefix_hash = h
        return st.repeats / max(1, st.calls - 1) if st.calls > 1 else 0.0

    def decide(self, session_id: str, stable_context: Sequence[str],
               volatile_query: str = "", task_hint: Optional[str] = None) -> RouteDecision:
        """Choose the cheapest lever for this request (cache_only | retrieve | compress)."""
        ctx_tokens = sum(count_tokens(c) for c in stable_context)
        q_tokens = count_tokens(volatile_query)
        repeat_rate = self._observe(session_id, stable_context)
        disc = self._discount()
        task = classify_task(volatile_query, task_hint)

        if ctx_tokens < MIN_CACHEABLE:
            return RouteDecision("passthrough", "context below cacheable minimum", task,
                                 {"passthrough": float(ctx_tokens + q_tokens)},
                                 repeat_rate, disc)

        st = self._sessions[session_id]
        # Effective cache multiplier for cache_only. If we have REAL observations of how much
        # the provider actually caches (obs_hit), use them — advertised discounts don't always
        # activate (e.g. OpenAI underdelivered in tests). Else fall back to the repeat model.
        if st.obs_hit >= 0.0 and st.obs_count >= 2:
            eff_mult = (1 - st.obs_hit) * 1.0 + st.obs_hit * disc
            why_basis = f"observed cache hit {st.obs_hit:.0%}"
        else:
            eff_mult = repeat_rate * disc + (1 - repeat_rate) * 1.0
            why_basis = f"repeat {repeat_rate:.0%} x discount {disc:.0%}"

        # --- candidate costs (amortized input tokens for this call) ------------------- #
        costs: Dict[str, float] = {}
        costs["cache_only"] = ctx_tokens * eff_mult + q_tokens
        # retrieval keeps ~min(flat-fraction, hard budget) tokens — budget dominates on big docs
        kept = min(ctx_tokens * self.retrieve_keep_frac, float(self.retrieve_budget_tokens))
        costs["retrieve"] = kept + q_tokens
        if self.allow_lossy:
            comp_keep = self.compress_rates.get(task, 0.6)
            costs["compress"] = ctx_tokens * comp_keep + q_tokens

        strat = min(costs, key=costs.get)
        reasons = {
            "cache_only": f"caching cheapest ({why_basis})",
            "retrieve": f"retrieval cheapest — keep ~{int(kept)} of {ctx_tokens} ctx tokens ({why_basis})",
            "compress": f"compression cheapest for task={task} (keep {self.compress_rates.get(task, 0.6):.0%}, lossy; {why_basis})",
        }

        # SCOPE GUARD (accuracy-first): a broad/whole-document question can't be answered from a
        # retrieved slice or a compressed prompt — it needs the full context. Override to cache_only
        # so the model sees everything (the provider's cache still discounts the re-send). We trade
        # some token savings for a correct answer. Specific lookups keep the cheaper lever.
        scope = classify_scope(volatile_query)
        if scope == "broad" and strat in ("retrieve", "compress"):
            chosen, reason = "cache_only", (
                "broad/whole-document question — keep FULL context (cache, don't retrieve); "
                f"retrieval would only show a slice. {why_basis}")
        else:
            chosen, reason = strat, reasons[strat]

        return RouteDecision(chosen, reason, task,
                             {k: round(v, 1) for k, v in costs.items()},
                             round(repeat_rate, 3), disc)
