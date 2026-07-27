"""Cross-provider measurement parity: receipt shapes, price rows, reseller guard."""
import json

import pytest

from brevitas.receipts import (
    SSEUsageParser,
    calculate_costs,
    canonical_provider,
    model_price,
    normalize_usage,
)


# --------------------------------------------------------------------------- #
# Receipt normalization: every provider's cached-token shape must land.
# --------------------------------------------------------------------------- #

def test_deepseek_hit_miss_receipt():
    receipt = normalize_usage({
        "prompt_tokens": 12, "prompt_cache_hit_tokens": 5,
        "prompt_cache_miss_tokens": 7, "completion_tokens": 3,
    }, "deepseek")
    assert receipt.cached_input_tokens == 5
    assert receipt.fresh_input_tokens == 7
    assert receipt.output_tokens == 3


def test_deepseek_hit_miss_without_prompt_tokens():
    # Cache fields alone must still enter the chat branch and reconstruct the
    # prompt total from hit + miss.
    receipt = normalize_usage({
        "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36,
    }, "deepseek")
    assert receipt.cached_input_tokens == 64
    assert receipt.fresh_input_tokens == 36
    assert receipt.input_tokens == 100


@pytest.mark.parametrize("provider", ["openai", "xai", "mistral", "groq", "openrouter"])
def test_openai_compatible_chat_cached_tokens(provider):
    receipt = normalize_usage({
        "prompt_tokens": 1000, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 640},
    }, provider)
    assert receipt.cached_input_tokens == 640
    assert receipt.fresh_input_tokens == 360
    assert receipt.output_tokens == 20


def test_openai_responses_cached_and_write_tokens():
    receipt = normalize_usage({
        "input_tokens": 1200, "output_tokens": 40,
        "input_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 100},
    }, "openai")
    assert receipt.cached_input_tokens == 1000
    assert receipt.cache_write_tokens == 100
    assert receipt.fresh_input_tokens == 100


def test_mistral_cache_miss_omits_cached_tokens():
    # Mistral omits/zeroes cached_tokens on a miss; the receipt stays all-fresh.
    receipt = normalize_usage({"prompt_tokens": 500, "completion_tokens": 9},
                              "mistral")
    assert receipt.cached_input_tokens == 0
    assert receipt.fresh_input_tokens == 500


# --------------------------------------------------------------------------- #
# Streaming receipts.
# --------------------------------------------------------------------------- #

def _feed_lines(parser, events):
    for event in events:
        parser.feed(b"data: " + json.dumps(event).encode() + b"\n\n")
    parser.feed(b"data: [DONE]\n")
    return parser.finish()


def test_sse_groq_stream_usage_under_x_groq():
    parser = SSEUsageParser("groq")
    receipt = _feed_lines(parser, [
        {"id": "cmpl-1", "choices": [{"delta": {"content": "hi"}}]},
        {"id": "cmpl-1", "choices": [],
         "x_groq": {"usage": {"prompt_tokens": 900, "completion_tokens": 12,
                              "prompt_tokens_details": {"cached_tokens": 512}}}},
    ])
    assert receipt.cached_input_tokens == 512
    assert receipt.fresh_input_tokens == 388
    assert receipt.output_tokens == 12


def test_sse_openai_stream_final_usage_chunk():
    # stream_options {"include_usage": true} shape: usage rides the last chunk.
    parser = SSEUsageParser("openai")
    receipt = _feed_lines(parser, [
        {"id": "cmpl-2", "choices": [{"delta": {"content": "hi"}}]},
        {"id": "cmpl-2", "choices": [],
         "usage": {"prompt_tokens": 2000, "completion_tokens": 31,
                   "prompt_tokens_details": {"cached_tokens": 1536}}},
    ])
    assert receipt.cached_input_tokens == 1536
    assert receipt.fresh_input_tokens == 464
    assert receipt.output_tokens == 31


def test_sse_deepseek_stream_hit_miss():
    parser = SSEUsageParser("deepseek")
    receipt = _feed_lines(parser, [
        {"id": "ds-1", "choices": [{"delta": {"content": "hi"}}]},
        {"id": "ds-1", "choices": [],
         "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 64,
                   "prompt_cache_miss_tokens": 36, "completion_tokens": 5}},
    ])
    assert receipt.cached_input_tokens == 64
    assert receipt.fresh_input_tokens == 36


# --------------------------------------------------------------------------- #
# MODEL_PRICES coverage: the models each measurable provider ships today.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("provider,model,cached,input_", [
    ("anthropic", "claude-fable-5", 1.0, 10.0),
    ("anthropic", "claude-opus-5", .5, 5.0),
    ("anthropic", "claude-sonnet-5", .3, 3.0),
    ("anthropic", "claude-haiku-4-5", .1, 1.0),
    ("openai", "gpt-5.6", .5, 5.0),
    ("openai", "gpt-5.6-sol", .5, 5.0),
    ("openai", "gpt-5.6-terra", .25, 2.5),
    ("openai", "gpt-5.6-luna", .1, 1.0),
    ("openai", "gpt-5.5", .5, 5.0),
    ("deepseek", "deepseek-v4-flash", .0028, .14),
    ("deepseek", "deepseek-chat", .0028, .14),
    ("xai", "grok-4.5", .5, 2.0),
    ("xai", "grok-4.1-fast", .05, .2),
    ("mistral", "mistral-large-latest", .05, .5),
])
def test_current_model_rows_priced(provider, model, cached, input_):
    price = model_price(provider, model)
    assert price is not None
    assert price["cached"] == pytest.approx(cached)
    assert price["input"] == pytest.approx(input_)


def test_dated_snapshots_resolve_to_alias_rows():
    assert model_price("openai", "gpt-5.6-terra-2026-07-09") == \
        model_price("openai", "gpt-5.6-terra")
    # Longest-prefix: the tier row wins over the bare family alias.
    assert model_price("openai", "gpt-5.6-luna-2026-07-09")["input"] == pytest.approx(1.0)
    assert model_price("anthropic", "claude-haiku-4-5-20251001") == \
        model_price("anthropic", "claude-haiku-4-5")


def test_gpt_56_explicit_write_premium_and_55_write_free():
    sol = model_price("openai", "gpt-5.6")
    assert sol["write"] == pytest.approx(sol["input"] * 1.25)
    # Pre-5.6 caching is automatic and write-free: write == input books a
    # zero premium in calculate_costs.
    legacy = model_price("openai", "gpt-5.5")
    assert legacy["write"] == pytest.approx(legacy["input"])


@pytest.mark.parametrize("provider,model", [
    ("xai", "grok-4.5"), ("xai", "grok-4.1-fast"),
    ("mistral", "mistral-large-latest"), ("deepseek", "deepseek-v4-flash"),
])
def test_automatic_cache_rows_have_no_write_premium(provider, model):
    price = model_price(provider, model)
    assert price["write"] == pytest.approx(price["input"])


def test_perplexity_stays_unpriced_no_cache_exists():
    # Sonar has no cached-input discount and bills non-token search fees, so
    # its rows are deliberately absent: measurement-only, never warmed.
    assert model_price("perplexity", "sonar") is None
    assert model_price("perplexity", "sonar-pro") is None


# --------------------------------------------------------------------------- #
# Reseller guard: aggregator traffic must never bill at direct-provider rates.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("provider,model", [
    ("openrouter", "gpt-4o"),
    ("openrouter", "anthropic/claude-opus-5"),
    ("together", "deepseek-chat"),
    ("fireworks", "deepseek-v4-flash"),
    ("groq", "openai/gpt-oss-120b"),
    ("perplexity", "sonar"),
])
def test_reseller_provider_is_never_remapped(provider, model):
    assert canonical_provider(provider, model) == provider
    # Unpriced is the honest outcome: resellers bill their own rates.
    assert model_price(provider, model) is None
    assert calculate_costs(provider, model, 100,
                           normalize_usage({"prompt_tokens": 100}))["pricing_status"] == "unpriced"


def test_direct_provider_remaps_still_hold():
    # SDK wrappers pass provider="openai" for any OpenAI-compatible base_url;
    # the model prefix remains the honest signal there.
    assert canonical_provider("openai", "deepseek-chat") == "deepseek"
    assert canonical_provider("azure_openai", "gpt-4o") == "openai"
    assert canonical_provider("", "grok-4.5") == "xai"
    assert canonical_provider("", "mistral-large-latest") == "mistral"
    assert canonical_provider("", "sonar-pro") == "perplexity"
    assert canonical_provider("", "claude-opus-5") == "anthropic"


# --------------------------------------------------------------------------- #
# Discount math lands for the newly priced rows.
# --------------------------------------------------------------------------- #

def test_gpt_56_cached_receipt_prices_and_saves():
    receipt = normalize_usage({
        "prompt_tokens": 100_000, "completion_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 80_000},
    }, "openai")
    costs = calculate_costs("openai", "gpt-5.6", 100_000, receipt,
                            baseline_output_tokens=0, cache_attributable=True)
    assert costs["pricing_status"] == "priced"
    # baseline 100k * $5/M = 0.5; actual 20k * $5/M + 80k * $0.5/M = 0.14.
    assert costs["baseline_cost_usd"] == pytest.approx(0.5)
    assert costs["actual_cost_usd"] == pytest.approx(0.14)
    assert costs["measured_savings_usd"] == pytest.approx(0.36)


def test_xai_cached_receipt_books_quarter_rate():
    receipt = normalize_usage({
        "prompt_tokens": 1_000_000, "completion_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 1_000_000},
    }, "xai")
    costs = calculate_costs("xai", "grok-4.5", 1_000_000, receipt,
                            baseline_output_tokens=0, cache_attributable=True)
    assert costs["pricing_status"] == "priced"
    assert costs["actual_cost_usd"] == pytest.approx(0.5)   # cached $0.50/M
    assert costs["baseline_cost_usd"] == pytest.approx(2.0)  # input $2.00/M


def test_non_attributable_cache_read_earns_no_savings():
    # Automatic provider caching Brevitas did not cause: measured, not billed.
    receipt = normalize_usage({
        "prompt_tokens": 100_000, "completion_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 100_000},
    }, "mistral")
    costs = calculate_costs("mistral", "mistral-large-latest", 100_000, receipt,
                            baseline_output_tokens=0, cache_attributable=False)
    assert costs["pricing_status"] == "priced"
    assert costs["measured_savings_usd"] == pytest.approx(0.0)
