"""Auto-router — picks the cheapest lossless strategy per request in CACHE-ADJUSTED
input-cost units (the "dollar router", design brief b0).

Strategies compared per request:
  * "cache_only" : send full context, rely on provider prefix caching.
  * "retrieve"   : send only the relevant retrieved subset (fails safe to full).
  * "passthrough": context too small to bother.

What grounds the model (no hand-rolled guesses):
  * Provider prefix caches match the LONGEST COMMON PREFIX of the request against
    recently-seen requests (provider docs; "Don't Break the Cache", arXiv 2601.06007).
    So repeat detection here is per-message LCP against the previous call — an
    append-only agent conversation is correctly recognized as ~fully cache-friendly.
    (The old whole-context hash called any append "no repeat" — the root cause of
    token-savings ≠ dollar-savings on caching providers.)
  * Cost rates come from provider_cache._RATES (cache_read / cache_write / output
    relative to fresh input; e.g. DeepSeek V4 Flash read 0.02, Anthropic read 0.10 with a
    1.25× write premium) — one table, synced with the savings accounting.
  * Provider caches expire: TTL per provider (Anthropic ~5 min per docs; conservative
    defaults elsewhere). An expired prefix re-bills as a write.
  * The retrieval arm's keep-fraction is LEARNED from observed retrieval results
    (EWMA), replacing the fixed 0.6 guess; `retrieve_keep_frac` is only the prior.
  * Cold-start exploration (ε-greedy on near-ties until real cache observations
    arrive) prevents the trap where a mis-modeled cache_only arm is never tried and
    therefore never corrected (standard bandit practice; S6 routing literature).

The retrieve arm is priced cache-aware: the b1 append-only retrieved layout keeps the
kept subset byte-stable turn-over-turn, so it caches like any stable prefix and is
priced with the same predicted cached fraction as cache_only (v1 priced it as
never-cached, which made the router avoid retrieval exactly where it composes with
caching). Token estimates are corrected per-session by the observed
actual/estimated ratio from provider usage (cl100k ≠ other providers' tokenizers).
"""

from __future__ import annotations

import hashlib
import math
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .provider_cache import _DEFAULT_RATES, _RATES, count_tokens, rates_for

MIN_CACHEABLE = 1024     # provider minimum cacheable prefix (tokens)

# Conservative provider cache lifetimes (seconds). Anthropic documents ~5 minutes
# (refreshed on use); OpenAI evicts within minutes; DeepSeek's disk cache persists
# much longer. An expired prefix is priced as a fresh write.
CACHE_TTL_S = {"anthropic": 300, "openai": 300, "deepseek": 3600, "default": 300}

# Backward-compat export: cached-token price fraction per provider (reads from the
# single _RATES source of truth; kept because external code/tests import this name).
CACHE_DISCOUNT = {p: r["cache_read"] for p, r in _RATES.items()}
CACHE_DISCOUNT["default"] = _DEFAULT_RATES["cache_read"]


@dataclass
class RouteDecision:
    strategy: str                 # cache_only | retrieve | passthrough
    reason: str
    est_cost_cache_only: float    # relative input-cost units (same model both arms)
    est_cost_retrieve: float
    repeat_rate: float            # LCP fraction vs previous call (0..1)
    provider_cache_discount: float
    cache_hit_prob: float = 0.0   # predicted cached fraction used in the cost model
    explored: bool = False        # True when ε-exploration overrode the greedy choice


@dataclass
class _SessionState:
    msg_hashes: List[str] = field(default_factory=list)
    msg_tokens: List[int] = field(default_factory=list)
    last_ts: float = 0.0
    # observed REAL cache performance from provider usage: EWMA of cached/prompt fraction
    obs_hit: float = -1.0          # -1 = no observation yet
    obs_count: int = 0
    # observed retrieval keep fraction (optimized/baseline), EWMA; -1 = none yet
    keep_frac: float = -1.0
    # token-count correction: EWMA of (actual provider prompt_tokens / our estimate).
    # cl100k over/under-counts other providers' tokenizers by 10-25%; the provider's
    # own usage field is ground truth, so we learn the ratio instead of guessing.
    last_est: int = 0
    tok_ratio: float = -1.0        # -1 = no observation yet
    # layout actually sent last turn ("cache_only"/"retrieve"/...) — switching layouts
    # busts the provider prefix cache once, so the arms are priced layout-sticky
    last_strategy: str = ""
    # observed inter-arrival gap between calls (seconds, EWMA) — drives the Anthropic
    # cache-TTL tier choice for spaced/repeated runs (cross-run lever)
    gap_ewma: float = -1.0
    # Cache economics. We require observed prefix reuse before paying an
    # Anthropic cache-write premium, and temporarily stop writing after repeated
    # paid writes fail to produce reads.
    repeat_observations: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_net_units: float = 0.0
    cache_negative_writes: int = 0
    cache_blocked_until: float = 0.0


class _BoundedSessionDict:
    """LRU-bounded session dict to prevent unbounded memory growth."""

    def __init__(self, max_sessions: int = 1024):
        self.max_sessions = max_sessions
        self._sessions: OrderedDict[str, _SessionState] = OrderedDict()

    def setdefault(self, session_id: str, default: _SessionState) -> _SessionState:
        """Get or create a session state, evicting LRU if needed."""
        if session_id in self._sessions:
            self._sessions.move_to_end(session_id)
            return self._sessions[session_id]
        if len(self._sessions) >= self.max_sessions:
            self._sessions.popitem(last=False)
        self._sessions[session_id] = default
        return default

    def __getitem__(self, session_id: str) -> _SessionState:
        if session_id in self._sessions:
            self._sessions.move_to_end(session_id)
            return self._sessions[session_id]
        raise KeyError(session_id)

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._sessions


@dataclass
class BrevitasRouter:
    provider: str = "openai"
    # model actually being called (set per-request by the engine); rates are per-MODEL
    # where known (gpt-4.1 caches at 25%, gpt-4o at 50%...), else per-provider fallback
    model: str = ""
    # PRIOR for the retrieval keep fraction, used until real observations arrive
    retrieve_keep_frac: float = 0.6
    # max concurrent sessions (LRU eviction when exceeded)
    max_sessions: int = 1024
    # cold-start exploration: only on near-ties (cost ratio <= explore_tie_ratio),
    # only until explore_until_obs real cache observations exist for the session
    epsilon: float = 0.1
    explore_until_obs: int = 3
    explore_tie_ratio: float = 1.25
    seed: Optional[int] = None

    _sessions: _BoundedSessionDict = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self._sessions, dict):
            self._sessions = _BoundedSessionDict(self.max_sessions)
        self._rng = random.Random(self.seed)

    # ------------------------------------------------------------------ rates
    def _rates(self) -> Dict[str, float]:
        return rates_for(self.provider, self.model)

    def _discount(self) -> float:
        return self._rates()["cache_read"]

    def _ttl(self) -> float:
        return CACHE_TTL_S.get(self.provider.lower(), CACHE_TTL_S["default"])

    # ------------------------------------------------------------- observation
    def observe_usage(self, session_id: str, prompt_tokens: int, cached_tokens: int,
                      cache_write_tokens: int = 0, cache_write_5m_tokens: int = 0,
                      cache_write_1h_tokens: int = 0) -> None:
        """Feed back REAL provider usage so the router learns the provider's actual
        cache-hit rate (advertised discounts don't always activate). EWMA of
        cached/prompt fraction."""
        if prompt_tokens <= 0:
            return
        st = self._sessions.setdefault(session_id, _SessionState())
        hit = max(0.0, min(1.0, cached_tokens / prompt_tokens))
        st.obs_hit = hit if st.obs_hit < 0 else 0.5 * st.obs_hit + 0.5 * hit
        st.obs_count += 1
        # learn the tokenizer correction from ground truth (provider's own count)
        if st.last_est > 0:
            ratio = prompt_tokens / st.last_est
            if 0.25 <= ratio <= 4.0:   # ignore wild mismatches (request was transformed)
                st.tok_ratio = ratio if st.tok_ratio < 0 else 0.5 * st.tok_ratio + 0.5 * ratio

        if self.provider.lower() == "anthropic":
            write = max(0, int(cache_write_tokens or 0))
            write_5m = max(0, int(cache_write_5m_tokens or 0))
            write_1h = max(0, int(cache_write_1h_tokens or 0))
            if write_5m + write_1h > write:
                write_5m = write_1h = 0
            unspecified = write - write_5m - write_1h
            read_gain = (1.0 - self._rates()["cache_read"]) * max(0, cached_tokens)
            write_premium = ((self._rates()["cache_write"] - 1.0)
                             * (unspecified + write_5m) + write_1h)
            st.cache_read_tokens += max(0, cached_tokens)
            st.cache_write_tokens += write
            st.cache_net_units += read_gain - write_premium
            if write > 0 and cached_tokens <= 0:
                st.cache_negative_writes += 1
            elif cached_tokens > 0:
                st.cache_negative_writes = 0
            if st.cache_negative_writes >= 2 and st.cache_net_units < 0:
                # Match the tier that incurred the premium; retrying a failed
                # one-hour write after only five minutes would simply repay it.
                cooldown = 3600.0 if write_1h > 0 else self._ttl()
                st.cache_blocked_until = time.time() + cooldown

    def observed_cache(self, session_id: str) -> tuple[float, int]:
        """Return (observed cache-hit fraction, observation count) for a session.
        Used by the cache-aware b9 gate to estimate the currently-cached prefix so it
        never reorders in a way that shrinks an already-good provider cache."""
        st = self._sessions._sessions.get(session_id) if session_id in self._sessions else None
        if st is None or st.obs_hit < 0:
            return 0.0, 0
        return st.obs_hit, st.obs_count

    def cache_write_allowed(self, session_id: str, ttl: str = "") -> tuple[bool, str]:
        """Return whether an Anthropic cache write has evidence it can break even.

        A 5-minute write premium needs one later read to recover; a 1-hour write
        needs two. We require that many observed prefix repetitions before the
        first paid write and cool down a prefix after two writes without a read.
        """
        st = self._sessions._sessions.get(session_id) if session_id in self._sessions else None
        if st is None:
            return False, "reuse_unproven"
        now = time.time()
        if st.cache_blocked_until > now:
            return False, "negative_roi_cooldown"
        read_gain = max(0.000001, 1.0 - self._rates()["cache_read"])
        write_premium = 1.0 if ttl == "1h" else max(0.0, self._rates()["cache_write"] - 1.0)
        hits_to_break_even = max(1, math.ceil(write_premium / read_gain))
        if st.repeat_observations < hits_to_break_even:
            return False, f"reuse_unproven:{st.repeat_observations}/{hits_to_break_even}"
        return True, "break_even_supported"

    def observe_retrieval(self, session_id: str, baseline_tokens: int,
                          optimized_tokens: int) -> None:
        """Feed back a real retrieval result so the retrieve arm's keep fraction is
        measured, not guessed."""
        if baseline_tokens <= 0:
            return
        frac = max(0.0, min(1.0, optimized_tokens / baseline_tokens))
        st = self._sessions.setdefault(session_id, _SessionState())
        st.keep_frac = frac if st.keep_frac < 0 else 0.5 * st.keep_frac + 0.5 * frac

    # ------------------------------------------------------------------ LCP
    def _observe(self, session_id: str, stable_context: Sequence[str]) -> float:
        """Per-message longest-common-prefix fraction vs the PREVIOUS call — the same
        matching rule provider prefix caches apply. Returns tokens-in-matched-prefix /
        total-stable-tokens (0..1). Also refreshes the stored fingerprint and applies
        the provider cache TTL (expired prefix ⇒ 0)."""
        st = self._sessions.setdefault(session_id, _SessionState())
        hashes = [hashlib.sha256(c.encode("utf-8")).hexdigest() for c in stable_context]
        tokens = [count_tokens(c) for c in stable_context]

        matched = 0
        for h_new, h_old in zip(hashes, st.msg_hashes):
            if h_new != h_old:
                break
            matched += 1
        total = sum(tokens)
        lcp_frac = (sum(tokens[:matched]) / total) if total > 0 else 0.0

        now = time.time()
        expired = st.last_ts > 0 and (now - st.last_ts) > self._ttl()
        if st.last_ts > 0:
            gap = now - st.last_ts
            st.gap_ewma = gap if st.gap_ewma < 0 else 0.5 * st.gap_ewma + 0.5 * gap
        st.msg_hashes, st.msg_tokens, st.last_ts = hashes, tokens, now
        effective_lcp = 0.0 if expired else lcp_frac
        # Cross-run repetition is still evidence for the 1-hour cache tier even
        # when the default 5-minute cache has expired. Routing uses effective_lcp;
        # the ROI gate uses the raw content repetition history.
        if lcp_frac > 0:
            st.repeat_observations += 1
        else:
            st.repeat_observations = 0
        return effective_lcp

    # ------------------------------------------------------------------ decide
    def decide(self, session_id: str, stable_context: Sequence[str],
               volatile_query: str = "") -> RouteDecision:
        """Choose the cheapest strategy for this request in cache-adjusted cost units."""
        ctx_tokens = sum(count_tokens(c) for c in stable_context)
        q_tokens = count_tokens(volatile_query)
        lcp_frac = self._observe(session_id, stable_context)
        rates = self._rates()
        read, write = rates["cache_read"], rates["cache_write"]

        st = self._sessions[session_id]
        # tokenizer correction: scale our cl100k estimate by the learned actual/estimate
        # ratio for this session (ground truth = provider usage), so threshold decisions
        # near MIN_CACHEABLE use real token counts, not another tokenizer's.
        st.last_est = ctx_tokens + q_tokens
        if st.tok_ratio > 0:
            ctx_tokens = int(ctx_tokens * st.tok_ratio)
            q_tokens = int(q_tokens * st.tok_ratio)

        if ctx_tokens < MIN_CACHEABLE:
            return RouteDecision("passthrough", "context below cacheable minimum",
                                 0.0, 0.0, round(lcp_frac, 3), read)
        # Predicted cached fraction: LCP prediction, blended 50/50 with the observed
        # hit rate once real observations exist (observation corrects both optimistic
        # and pessimistic predictions).
        if st.obs_hit >= 0.0 and st.obs_count >= 2:
            p = 0.5 * lcp_frac + 0.5 * st.obs_hit
            why_basis = f"lcp {lcp_frac:.0%} + observed hit {st.obs_hit:.0%}"
        else:
            p = lcp_frac
            why_basis = f"lcp {lcp_frac:.0%} x read {read:.0%}/write {write:.2f}x"

        # cache_only: cached prefix at read rate; the rest is (re)written at the
        # provider's write rate (1.25x on Anthropic); volatile tail is fresh input.
        # Always gets the predicted discount — provider caches persist (DeepSeek's disk
        # cache for hours), so the full prefix stays warm even across a retrieve turn.
        cache_only = ctx_tokens * (p * read + (1 - p) * write) + q_tokens
        # retrieve: kept subset. The b1 append-only layout makes the kept subset cache
        # like any stable prefix, so it EARNS the same discount — but only once it is
        # the incumbent layout (switching layouts re-bills the prefix as a write) AND
        # its keep fraction is MEASURED from real retrievals. A discount is never
        # awarded to the 0.6 prior: guesses compete at full write price (v1 behavior),
        # measured composition gets credit. This is the honest, lossless-biased middle
        # between v1 (retrieval never cached ⇒ router avoids it where it composes) and
        # symmetric pricing (retrieve strictly dominant ⇒ cache-busting flapping).
        keep = st.keep_frac if st.keep_frac >= 0 else self.retrieve_keep_frac
        p_retr = p if (st.last_strategy == "retrieve" and st.keep_frac >= 0) else 0.0
        retrieve = ctx_tokens * keep * (p_retr * read + (1 - p_retr) * write) + q_tokens

        if retrieve < cache_only:
            strat, why = "retrieve", f"retrieval cheaper ({why_basis})"
        else:
            strat, why = "cache_only", f"caching cheaper ({why_basis})"

        # Cold-start exploration: on near-ties, before enough real observations,
        # occasionally try the other arm so the model can be corrected by data.
        explored = False
        lo, hi = sorted([cache_only, retrieve])
        if (st.obs_count < self.explore_until_obs and lo > 0
                and hi / lo <= self.explore_tie_ratio
                and self._rng.random() < self.epsilon):
            strat = "retrieve" if strat == "cache_only" else "cache_only"
            why += " +explore"
            explored = True

        st.last_strategy = strat   # incumbent layout for next turn's sticky pricing
        return RouteDecision(strat, why, round(cache_only, 1), round(retrieve, 1),
                             round(lcp_frac, 3), read, round(p, 3), explored)

    def session_gap(self, session_id: str) -> float:
        """Observed inter-arrival gap (s, EWMA) for a session; -1 if unknown. Used to
        choose the Anthropic cache-TTL tier: runs spaced past the 5-minute TTL but
        within an hour are cheaper on the 1h tier (2x write once, then 0.1x reads
        that refresh the hour for free — provider docs)."""
        st = self._sessions._sessions.get(session_id) if session_id in self._sessions else None
        return st.gap_ewma if st is not None else -1.0

    def note_strategy(self, session_id: str, strategy: str) -> None:
        """Correct the incumbent layout when the engine falls back (e.g. retrieval
        bailed and the FULL context was sent) so sticky pricing tracks what was
        actually transmitted, not what was decided."""
        if session_id in self._sessions:
            self._sessions[session_id].last_strategy = strategy
