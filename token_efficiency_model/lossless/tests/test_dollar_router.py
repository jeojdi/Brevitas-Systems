"""Dollar-router (brief b0) — cache-adjusted routing regression tests.

The headline regression: an APPEND-ONLY agent conversation (every turn adds messages,
never edits old ones) must be recognized as cache-friendly. The old whole-context-hash
router scored it repeat_rate=0 forever and always chose retrieve — the root cause of
the measured −69% tokens / −23% dollars gap on DeepSeek.
"""
from __future__ import annotations

import time

from token_efficiency_model.lossless.provider_cache import _RATES
from token_efficiency_model.lossless.router import (CACHE_DISCOUNT, BrevitasRouter,
                                                    _SessionState)


def _seg(tag: str, n_words: int = 600) -> str:
    # ~1200 tokens per segment ("xN" ≈ 2 tokens each) — a single segment must
    # already exceed MIN_CACHEABLE (1024) so no test accidentally hits passthrough
    return " ".join(f"{tag}{i}" for i in range(n_words))


A, B, C, D = _seg("a"), _seg("b"), _seg("c"), _seg("d")


# --------------------------------------------------------------------------- #
# THE P1 regression: append-only conversations must go cache_only
# --------------------------------------------------------------------------- #
def test_append_only_conversation_prefers_cache_on_deepseek():
    r = BrevitasRouter(provider="deepseek", epsilon=0.0)
    r.decide("s", [A], "q1")                       # turn 1: cold
    r.decide("s", [A, B], "q2")                    # turn 2: prefix A repeats
    d3 = r.decide("s", [A, B, C], "q3")            # turn 3: prefix A,B repeats (lcp ≈ 2/3)
    d4 = r.decide("s", [A, B, C, D], "q4")         # turn 4: lcp ≈ 3/4
    assert d3.repeat_rate > 0.6
    assert d3.strategy == "cache_only", d3.reason
    assert d4.strategy == "cache_only", d4.reason
    assert d4.cache_hit_prob > 0.7


def test_append_only_on_anthropic_accounts_for_write_premium():
    r = BrevitasRouter(provider="anthropic", epsilon=0.0)
    r.decide("s", [A], "q1")
    d2 = r.decide("s", [A, B], "q2")
    # BOTH arms pay the 1.25x write premium on fresh content (the engine marks the
    # retrieve layout for caching too, so its unmeasured price is 0.6*1.25 = 0.75).
    # lcp=0.5: cache_only = 0.5*0.10 + 0.5*1.25 = 0.675 < 0.75 → caching wins.
    assert d2.strategy == "cache_only", d2.reason
    assert d2.est_cost_cache_only < d2.est_cost_retrieve
    d3 = r.decide("s", [A, B, C], "q3")
    # lcp≈2/3: 0.667*0.10 + 0.333*1.25 ≈ 0.48 → caching keeps winning.
    assert d3.strategy == "cache_only", d3.reason


def test_fully_changing_context_prefers_retrieve():
    r = BrevitasRouter(provider="deepseek", epsilon=0.0)
    r.decide("s", [A], "q1")
    d = r.decide("s", [B], "q2")   # nothing repeats
    assert d.repeat_rate == 0.0
    assert d.strategy == "retrieve"


# --------------------------------------------------------------------------- #
# TTL: an expired provider cache re-bills as a write
# --------------------------------------------------------------------------- #
def test_ttl_expiry_resets_cache_prediction():
    r = BrevitasRouter(provider="anthropic", epsilon=0.0)
    r.decide("s", [A, B], "q1")
    st = r._sessions["s"]
    st.last_ts = time.time() - 400          # anthropic TTL is 300s
    d = r.decide("s", [A, B], "q2")         # identical context, but cache expired
    assert d.repeat_rate == 0.0
    assert d.cache_hit_prob == 0.0


# --------------------------------------------------------------------------- #
# learned retrieval keep-fraction replaces the 0.6 prior
# --------------------------------------------------------------------------- #
def test_observed_keep_fraction_reprices_retrieve_arm():
    r = BrevitasRouter(provider="deepseek", epsilon=0.0)
    r.decide("s", [A, B, C], "q1")
    base = r.decide("s", [A, B, C], "q2")   # identical: cache_only clearly wins
    assert base.strategy == "cache_only"
    # observed retrieval keeps only 5% → retrieve arm becomes ~0.05x
    r.observe_retrieval("s", 10_000, 500)
    d = r.decide("s", [A, B, C], "q3")
    assert d.est_cost_retrieve < base.est_cost_retrieve
    assert d.strategy == "retrieve", d.reason  # 0.05 < lcp-based cache cost (~0.26)


# --------------------------------------------------------------------------- #
# observation blending + exploration
# --------------------------------------------------------------------------- #
def test_observed_hit_rate_blends_into_prediction():
    r = BrevitasRouter(provider="openai", epsilon=0.0)
    r.decide("s", [A, B], "q1")
    # report usage CONSISTENT with the router's estimate — prompt_tokens is also the
    # ground truth for the learned tokenizer correction; a fake low count would
    # (correctly!) shrink the estimated context below the cacheable minimum.
    est = r._sessions["s"].last_est
    r.observe_usage("s", est, 0)            # provider cached NOTHING
    r.observe_usage("s", est, 0)
    d = r.decide("s", [A, B], "q2")         # identical context: lcp=1.0, obs=0.0
    assert abs(d.cache_hit_prob - 0.5) < 0.05


def test_exploration_only_on_near_ties_and_cold_sessions():
    # epsilon=1.0 forces exploration whenever eligible; wide tie ratio makes all
    # comparisons "near ties" — so a cold session MUST explore...
    r = BrevitasRouter(provider="deepseek", epsilon=1.0, explore_tie_ratio=100.0, seed=7)
    r.decide("s", [A, B], "q1")
    d = r.decide("s", [A, B], "q2")
    assert d.explored is True
    # ...but a session with enough real observations must NOT explore.
    r2 = BrevitasRouter(provider="deepseek", epsilon=1.0, explore_tie_ratio=100.0, seed=7)
    r2.decide("s", [A, B], "q1")
    for _ in range(3):
        r2.observe_usage("s", 1000, 900)
    d2 = r2.decide("s", [A, B], "q2")
    assert d2.explored is False


def test_cache_discount_export_synced_with_rates():
    assert CACHE_DISCOUNT["deepseek"] == _RATES["deepseek"]["cache_read"] == 0.259
    assert CACHE_DISCOUNT["anthropic"] == _RATES["anthropic"]["cache_read"]


# --------------------------------------------------------------------------- #
# inter-run gap tracking (drives the Anthropic TTL-tier choice, cross-run lever)
# --------------------------------------------------------------------------- #
def test_session_gap_tracks_spacing():
    r = BrevitasRouter(provider="anthropic", epsilon=0.0)
    r.decide("g", [A, B], "q1")
    r._sessions["g"].last_ts -= 600            # pretend last call was 10 min ago
    r.decide("g", [A, B], "q2")
    gap = r.session_gap("g")
    assert 590 <= gap <= 620, f"gap EWMA should be ~600s, got {gap}"
    assert r.session_gap("unknown-session") == -1.0
