"""Tests for Lever 1 — provider-native caching (breakpoint placement + honest savings)."""

from token_efficiency_model.lossless.provider_cache import (
    apply_anthropic_cache,
    apply_openai_cache,
    count_cache_control,
    count_tokens,
    savings_from_usage,
)


def _long(n_words: int) -> str:
    return " ".join(["lorem"] * n_words)


# --- breakpoint placement honors the >=1024-token guard -------------------- #
def test_no_breakpoint_when_prefix_below_min():
    body = {
        "system": "short system prompt",
        "messages": [{"role": "user", "content": "hi"}],
    }
    plan = apply_anthropic_cache(body)
    assert plan.breakpoints == 0
    # system left as a plain string (not converted) since not cached
    assert isinstance(body["system"], str)


def test_breakpoint_added_when_prefix_large():
    big = _long(2000)  # well over 1024 tokens
    body = {
        "system": big,
        "messages": [
            {"role": "user", "content": "earlier turn " + _long(1500)},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "the new volatile question"},
        ],
    }
    plan = apply_anthropic_cache(body)
    assert plan.breakpoints >= 1
    assert plan.cached_prefix_tokens >= 1024
    # the volatile last user message must NOT be marked
    last = body["messages"][-1]
    assert isinstance(last["content"], str) or "cache_control" not in last["content"][-1]


def test_tools_block_is_cached_when_large():
    body = {
        "tools": [{"name": "f", "description": _long(2000)}],
        "messages": [{"role": "user", "content": "q"}],
    }
    plan = apply_anthropic_cache(body)
    assert "tools" in plan.positions
    assert "cache_control" in body["tools"][-1]


def test_at_most_four_breakpoints():
    body = {
        "system": _long(2000),
        "messages": (
            [{"role": "user", "content": _long(1500)} for _ in range(8)]
            + [{"role": "user", "content": "final"}]
        ),
    }
    plan = apply_anthropic_cache(body)
    assert plan.breakpoints <= 4


def test_top_level_anthropic_cache_control_is_caller_owned():
    body = {"cache_control": {"type": "ephemeral"},
            "messages": [{"role": "user", "content": "hello"}]}
    assert count_cache_control(body) == 1


def test_openai_gpt56_cache_key_and_explicit_breakpoint():
    body = {
        "model": "gpt-5.6",
        "messages": [
            {"role": "system", "content": _long(1800)},
            {"role": "user", "content": "question"},
        ],
    }
    plan = apply_openai_cache(body, tenant_key="tenant-a", explicit_breakpoint=True)
    assert plan.supported and plan.key_added and plan.breakpoint_added
    assert body["prompt_cache_key"].startswith("brevitas:tenant-a:")
    assert body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert body["messages"][0]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"}


def test_openai_cache_fields_fail_closed_on_older_models():
    body = {"model": "gpt-4o", "messages": [
        {"role": "system", "content": _long(1800)},
        {"role": "user", "content": "question"},
    ]}
    assert apply_openai_cache(body, tenant_key="tenant").supported is False
    assert "prompt_cache_key" not in body


# --- honest savings from real usage ---------------------------------------- #
def test_anthropic_savings_from_cache_read():
    # 9000 tokens read from cache, 1000 fresh -> big discount
    usage = {"input_tokens": 1000, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 9000}
    s = savings_from_usage(usage, "anthropic")
    # uncached = 10000; actual = 1000*1 + 9000*0.1 = 1900 -> 81% savings
    assert s.cached_tokens == 9000
    assert abs(s.savings_pct - 81.0) < 0.5


def test_anthropic_cache_write_costs_more_no_phantom_savings():
    usage = {"input_tokens": 0, "cache_creation_input_tokens": 10000,
             "cache_read_input_tokens": 0}
    s = savings_from_usage(usage, "anthropic")
    assert s.savings_pct < 0  # writing the cache costs 1.25x; honest = negative on turn 1


def test_openai_cached_tokens_savings():
    usage = {"prompt_tokens": 10000, "prompt_tokens_details": {"cached_tokens": 8000}}
    s = savings_from_usage(usage, "openai")
    # actual = 2000*1 + 8000*0.5 = 6000; uncached = 10000 -> 40% savings
    assert s.cached_tokens == 8000
    assert abs(s.savings_pct - 40.0) < 0.5


def test_openai_gpt56_cache_write_is_not_fresh_or_free():
    usage = {"prompt_tokens": 2000, "prompt_tokens_details": {
        "cached_tokens": 0, "cache_write_tokens": 2000}}
    s = savings_from_usage(usage, "openai", model="gpt-5.6")
    assert s.detail["cache_write"] == 2000
    assert s.input_fresh == 2000
    assert s.savings_pct == -25.0


def test_no_cache_means_no_savings():
    assert savings_from_usage({"prompt_tokens": 500, "prompt_tokens_details": {}}, "openai").savings_pct == 0.0


def test_deepseek_cache_discount_is_real_rate_not_90pct():
    """DeepSeek V4 Flash cache hits cost 2% of fresh input.

    At 80% cached, the current official rate yields 78.4% input-cost savings.
    """
    usage = {"prompt_tokens": 10000, "prompt_tokens_details": {"cached_tokens": 8000}}
    ds = savings_from_usage(usage, "deepseek").savings_pct
    oa = savings_from_usage(usage, "openai").savings_pct
    assert abs(ds - 78.4) < 0.5    # 2000*1 + 8000*0.02 = 2160 -> 78.4% saved
    assert abs(oa - 40.0) < 0.5    # 2000*1 + 8000*0.5   = 6000 -> 40% saved
    assert ds > oa


def test_output_tokens_dilute_savings():
    """Output is NEVER cached, so adding output lowers TOTAL savings vs input-only."""
    base = savings_from_usage(
        {"prompt_tokens": 10000, "prompt_tokens_details": {"cached_tokens": 8000}}, "deepseek")
    without = savings_from_usage(
        {"prompt_tokens": 10000, "prompt_tokens_details": {"cached_tokens": 8000},
         "completion_tokens": 1000}, "deepseek")
    assert without.output_tokens == 1000
    assert without.savings_pct < base.savings_pct               # output dilutes the total
    assert without.input_savings_pct == base.input_savings_pct  # input-only unchanged


# --------------------------------------------------------------------------- #
# per-model rates (billing-grade): the model actually called sets the ratios
# --------------------------------------------------------------------------- #
def test_rates_for_model_overrides_provider_row():
    from token_efficiency_model.lossless.provider_cache import rates_for
    assert rates_for("openai", "gpt-4.1-mini")["cache_read"] == 0.25   # 4.1 family: 25%
    assert rates_for("openai", "gpt-4o-mini")["cache_read"] == 0.50    # 4o family: 50%
    assert rates_for("openai", "")["cache_read"] == 0.50               # provider fallback
    assert rates_for("deepseek", "deepseek-chat")["cache_read"] == 0.02
    assert rates_for("deepseek", "deepseek-v4-pro")["cache_read"] == 1 / 120
    assert rates_for("anthropic", "claude-sonnet-4-5")["cache_write"] == 1.25
    assert rates_for("unknown", "mystery-model")["cache_read"] == 0.50  # safe default


def test_savings_from_usage_uses_model_rates():
    from token_efficiency_model.lossless.provider_cache import savings_from_usage
    usage = {"prompt_tokens": 1000, "completion_tokens": 0,
             "prompt_tokens_details": {"cached_tokens": 1000}}
    s41 = savings_from_usage(usage, "openai", model="gpt-4.1")
    s4o = savings_from_usage(usage, "openai", model="gpt-4o")
    assert s41.actual_cost < s4o.actual_cost      # 25% vs 50% cached price


# --------------------------------------------------------------------------- #
# per-model cache minimums + TTL tier (cross-run lever, provider-docs-verified)
# --------------------------------------------------------------------------- #
def test_anthropic_min_tokens_per_model():
    from token_efficiency_model.lossless.provider_cache import anthropic_min_tokens
    assert anthropic_min_tokens("claude-haiku-4-5-20251001") == 4096
    assert anthropic_min_tokens("claude-opus-4-5") == 4096
    assert anthropic_min_tokens("claude-haiku-3-5") == 2048
    assert anthropic_min_tokens("claude-fable-5") == 512
    assert anthropic_min_tokens("claude-sonnet-4-5") == 1024   # default tier


def test_haiku45_prompt_below_4096_not_marked():
    from token_efficiency_model.lossless.provider_cache import apply_anthropic_cache
    body = {"model": "claude-haiku-4-5-20251001",
            "messages": [{"role": "user", "content": "word " * 3000},   # ~3000 tok < 4096
                         {"role": "user", "content": "q"}]}
    plan = apply_anthropic_cache(body)
    assert plan.breakpoints == 0, "below the model's real minimum: markers are inert"


def test_ttl_1h_markers_and_plan():
    from token_efficiency_model.lossless.provider_cache import apply_anthropic_cache
    body = {"model": "claude-sonnet-4-5",
            "system": "s " * 2000,
            "messages": [{"role": "user", "content": "context " * 2000},
                         {"role": "user", "content": "q"}]}
    plan = apply_anthropic_cache(body, ttl="1h")
    assert plan.ttl == "1h" and plan.breakpoints > 0
    marks = []
    for blk in body["system"]:
        if "cache_control" in blk:
            marks.append(blk["cache_control"])
    for m in body["messages"]:
        for blk in (m["content"] if isinstance(m["content"], list) else []):
            if isinstance(blk, dict) and "cache_control" in blk:
                marks.append(blk["cache_control"])
    assert marks and all(cc.get("ttl") == "1h" for cc in marks)


def test_savings_tier_accurate_1h_write_premium():
    from token_efficiency_model.lossless.provider_cache import savings_from_usage
    base = {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 1000}
    u5 = dict(base, cache_creation={"ephemeral_5m_input_tokens": 1000,
                                    "ephemeral_1h_input_tokens": 0})
    u1h = dict(base, cache_creation={"ephemeral_5m_input_tokens": 0,
                                     "ephemeral_1h_input_tokens": 1000})
    s5 = savings_from_usage(u5, "anthropic")
    s1h = savings_from_usage(u1h, "anthropic")
    assert s1h.actual_cost > s5.actual_cost, "1h writes bill 2x vs 5m 1.25x"
