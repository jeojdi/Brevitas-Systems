"""Tests for the auto-router — strategy choice by estimated cost + repeat detection."""

from token_efficiency_model.lossless.router import BrevitasRouter, MIN_CACHEABLE


def _big(tokens=2000):
    return " ".join(["brand"] * tokens)


def test_small_context_passthrough():
    r = BrevitasRouter(provider="openai")
    d = r.decide("s1", ["short brand note"], "write a tweet")
    assert d.strategy == "passthrough"


def test_deepseek_repeating_context_prefers_cache():
    """Strong cache (DeepSeek) + repeated context -> cache_only is cheaper."""
    r = BrevitasRouter(provider="deepseek")
    ctx = [_big()]
    # repeat the SAME stable context several times (marketing-agent pattern)
    for _ in range(5):
        d = r.decide("sess", ctx, "write campaign brief variant")
    assert d.repeat_rate > 0.5
    assert d.strategy == "cache_only"


def test_deepseek_unique_context_prefers_retrieve():
    """Strong cache but context changes every call -> retrieval cheaper."""
    r = BrevitasRouter(provider="deepseek")
    last = None
    for i in range(5):
        last = r.decide("sess", [_big() + f" unique-{i}"], "question")
    assert last.repeat_rate == 0.0
    assert last.strategy == "retrieve"


def test_openai_partial_repeat_prefers_retrieve():
    """OpenAI (50% cached): when context only sometimes repeats, retrieval (0.6x) beats the
    blended cache_only cost. (For FULLY-repeating context, cache_only wins even on OpenAI,
    since 0.5x < 0.6x — assuming the provider cache actually activates.)"""
    r = BrevitasRouter(provider="openai")
    # alternate context so repeat_rate stays low/partial
    for i in range(6):
        d = r.decide("sess", [_big() + f" v{i % 2}"], "q")
    assert d.repeat_rate < 0.6
    assert d.strategy == "retrieve"


def test_cost_estimates_present_and_ordered():
    r = BrevitasRouter(provider="deepseek")
    ctx = [_big()]
    for _ in range(4):
        d = r.decide("sess", ctx, "q")
    assert d.est_cost_cache_only > 0 and d.est_cost_retrieve > 0
    chosen = min(d.est_cost_cache_only, d.est_cost_retrieve)
    assert (chosen == d.est_cost_cache_only) == (d.strategy == "cache_only")
