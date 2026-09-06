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


def _seg(tag: str, n_words: int = 900) -> str:
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
    # DeepSeek V4 cache hits cost only 2% of fresh input, so switching layouts
    # is worthwhile only for an even smaller measured retrieval result.
    r.observe_retrieval("s", 10_000, 100)
    d = r.decide("s", [A, B, C], "q3")
    assert d.est_cost_retrieve < base.est_cost_retrieve
    assert d.strategy == "retrieve", d.reason  # 0.01 < the warm full-prefix cost (0.02)


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
    assert CACHE_DISCOUNT["deepseek"] == _RATES["deepseek"]["cache_read"] == 0.02
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


def test_cache_write_requires_enough_observed_reuse_to_break_even():
    r = BrevitasRouter(provider="anthropic", epsilon=0.0)
    r.decide("roi", [A, B], "q1")
    allowed, reason = r.cache_write_allowed("roi")
    assert not allowed and reason.startswith("reuse_unproven")

    r.decide("roi", [A, B], "q2")
    assert r.cache_write_allowed("roi") == (True, "break_even_supported")

    # A 1-hour write has a full 1x premium and needs two 0.9x reads.
    allowed, reason = r.cache_write_allowed("roi", "1h")
    assert not allowed and reason == "reuse_unproven:1/2"
    r.decide("roi", [A, B], "q3")
    assert r.cache_write_allowed("roi", "1h") == (True, "break_even_supported")


def test_repeated_unrecovered_cache_writes_trigger_cooldown():
    r = BrevitasRouter(provider="anthropic", epsilon=0.0)
    r.decide("loss", [A, B], "q1")
    r.decide("loss", [A, B], "q2")
    r.observe_usage("loss", 2400, 0, cache_write_tokens=2000)
    r.observe_usage("loss", 2400, 0, cache_write_tokens=2000)
    allowed, reason = r.cache_write_allowed("loss")
    assert not allowed and reason == "negative_roi_cooldown"


def test_cross_run_reuse_can_qualify_for_one_hour_tier():
    r = BrevitasRouter(provider="anthropic", epsilon=0.0)
    r.decide("hourly", [A, B], "q1")
    r._sessions["hourly"].last_ts -= 600
    r.decide("hourly", [A, B], "q2")
    assert r.cache_write_allowed("hourly", "1h")[0] is False
    r._sessions["hourly"].last_ts -= 600
    r.decide("hourly", [A, B], "q3")
    assert r.cache_write_allowed("hourly", "1h") == (True, "break_even_supported")


# --------------------------------------------------------------------- warm prefix
# The provider's cache is keyed by prefix CONTENT and shared across every session in
# the workspace, but reuse evidence used to be keyed by SESSION. So a session's first
# call always scored "never repeated" — even for a prefix another agent had written
# seconds earlier — and the gate withheld cache_control. On Anthropic that marker
# authorises the 0.10x READ as well as the write, so withholding it forfeited the
# discount instead of avoiding the premium: measured -30.84% (E1).

def test_warm_prefix_from_another_session_licenses_the_first_call():
    r = BrevitasRouter(provider="anthropic", epsilon=0.0)
    # agent-1 pays the write for this prefix
    r.decide("agent-1", [A, B], "q1", tenant_key="acme")
    # agent-2 arrives cold on its OWN session but the identical prefix is warm
    r.decide("agent-2", [A, B], "q1", tenant_key="acme")
    assert r.cache_write_allowed("agent-2") == (True, "warm_prefix_observed")


def test_a_genuinely_novel_prefix_is_still_refused_on_the_first_call():
    r = BrevitasRouter(provider="anthropic", epsilon=0.0)
    r.decide("agent-1", [A, B], "q1", tenant_key="acme")
    r.decide("agent-2", [C, D], "q1", tenant_key="acme")   # shares nothing
    allowed, reason = r.cache_write_allowed("agent-2")
    assert not allowed and reason.startswith("reuse_unproven")


def test_warm_prefix_evidence_does_not_cross_tenants():
    r = BrevitasRouter(provider="anthropic", epsilon=0.0)
    r.decide("agent-1", [A, B], "q1", tenant_key="acme")
    r.decide("agent-2", [A, B], "q1", tenant_key="globex")  # same bytes, other tenant
    allowed, reason = r.cache_write_allowed("agent-2")
    assert not allowed and reason.startswith("reuse_unproven")


def test_negative_roi_cooldown_still_outranks_warm_prefix_evidence():
    r = BrevitasRouter(provider="anthropic", epsilon=0.0)
    r.decide("payer", [A, B], "q1", tenant_key="acme")
    r.decide("loser", [A, B], "q1", tenant_key="acme")
    r.observe_usage("loser", 2400, 0, cache_write_tokens=2000)
    r.observe_usage("loser", 2400, 0, cache_write_tokens=2000)
    assert r.cache_write_allowed("loser") == (False, "negative_roi_cooldown")


def test_a_stale_sighting_no_longer_counts_as_warm():
    r = BrevitasRouter(provider="anthropic", epsilon=0.0)
    r.decide("agent-1", [A, B], "q1", tenant_key="acme")
    r.decide("agent-2", [A, B], "q1", tenant_key="acme")
    # push the sighting outside the 5-minute tier window
    r._sessions["agent-2"].warm_prefix_elsewhere_ts -= 601
    allowed, reason = r.cache_write_allowed("agent-2")
    assert not allowed and reason.startswith("reuse_unproven")
    # the 1h tier tolerates an older sighting, but not an arbitrarily old one
    r._sessions["agent-2"].warm_prefix_elsewhere_ts = time.time() - 1800
    assert r.cache_write_allowed("agent-2", "1h") == (True, "warm_prefix_observed")
    r._sessions["agent-2"].warm_prefix_elsewhere_ts = time.time() - 4000
    assert r.cache_write_allowed("agent-2", "1h")[0] is False


def test_a_sessions_own_earlier_sighting_is_not_warm_prefix_evidence():
    """Same-session repetition is what repeat_observations already measures; counting
    it twice here would let a single un-repeated call license its own write."""
    r = BrevitasRouter(provider="anthropic", epsilon=0.0)
    r.decide("solo", [A, B], "q1", tenant_key="acme")
    assert r._sessions["solo"].warm_prefix_elsewhere_ts == 0.0
    allowed, reason = r.cache_write_allowed("solo")
    assert not allowed and reason.startswith("reuse_unproven")


def test_prefix_sightings_are_bounded():
    r = BrevitasRouter(provider="anthropic", epsilon=0.0, max_prefix_sightings=16)
    for i in range(50):
        r.decide(f"s{i}", [_seg(f"uniq{i}"), B], "q", tenant_key="acme")
    assert len(r._prefix_seen) <= 16
