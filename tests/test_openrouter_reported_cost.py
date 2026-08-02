"""OpenRouter (reseller) authoritative-cost pricing.

Resellers publish no per-token rate Brevitas trusts (MODEL_PRICES is silent for
them), so their traffic used to price as unpriced/$0 for everything. These tests
pin the alternative: the reseller's OWN reported dollars (OpenRouter usage.cost)
become a priced row — real spend on a live call, and the whole reported cost as
measured savings on a zero-spend cache replay — while the reseller GUARD stays
intact when no cost was reported.
"""
from cryptography.fernet import Fernet

from api.auth import hash_key
from api.store import UsageStore
from brevitas.receipts import (
    SSEUsageParser,
    calculate_costs,
    normalize_usage,
    provider_reported_cost,
    reseller_authoritative_costs,
)
from brevitas.semantic_cache import SemanticCache


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_provider_reported_cost_is_resellers_only():
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0123}
    # Resellers: the reported dollars are read.
    assert provider_reported_cost(usage, "openrouter") == 0.0123
    # First-party providers: the field is ignored — MODEL_PRICES prices them.
    assert provider_reported_cost(usage, "openai") is None
    assert provider_reported_cost(usage, "anthropic") is None


def test_provider_reported_cost_refuses_bad_values():
    assert provider_reported_cost({"prompt_tokens": 10}, "openrouter") is None      # absent
    assert provider_reported_cost({"cost": -1.0}, "openrouter") is None             # negative
    assert provider_reported_cost({"cost": True}, "openrouter") is None             # bool
    assert provider_reported_cost({"cost": "0.1"}, "openrouter") is None            # string


def test_reseller_costs_live_records_spend_without_inventing_savings():
    costs = reseller_authoritative_costs(0.02, zero_spend=False)
    assert costs["pricing_status"] == "priced"
    assert costs["actual_cost_usd"] == 0.02
    assert costs["baseline_cost_usd"] == 0.02
    assert costs["measured_savings_usd"] == 0.0     # a live call books no savings


def test_reseller_costs_replay_turns_the_whole_cost_into_savings():
    costs = reseller_authoritative_costs(0.02, zero_spend=True)
    assert costs["pricing_status"] == "priced"
    assert costs["actual_cost_usd"] == 0.0          # the replay touched no upstream
    assert costs["baseline_cost_usd"] == 0.02
    assert costs["measured_savings_usd"] == 0.02


def test_stream_parser_captures_reseller_cost_from_final_chunk():
    parser = SSEUsageParser("openrouter")
    parser.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n')
    parser.feed(b'data: {"usage":{"prompt_tokens":100,"completion_tokens":5,"cost":0.004}}\n')
    parser.feed(b"data: [DONE]\n")
    receipt = parser.finish()
    assert receipt.total_tokens == 105
    assert parser.reported_cost_usd == 0.004


# --------------------------------------------------------------------------- #
# Cache round-trip: the original cost must survive a store/lookup so a replay
# can be priced against it.
# --------------------------------------------------------------------------- #

def _cache_body(namespace="org_1:customer_1"):
    return {"model": "model", "temperature": 0,
            "messages": [{"role": "user", "content": "hello"}],
            "_brevitas_cache_namespace": namespace}


def test_cache_round_trips_reported_cost(tmp_path):
    cache = SemanticCache(str(tmp_path / "c.db"), encryption_key=Fernet.generate_key())
    cache.store(_cache_body(), "openai", "model",
                {"answer": "42"}, prompt_tokens=100, completion_tokens=20,
                origin_request_id="proxy:paid-1", reported_cost_usd=0.02)
    hit = cache.lookup(_cache_body(), "openai", "model")
    assert hit is not None
    assert hit.origin_request_id == "proxy:paid-1"
    assert hit.reported_cost_usd == 0.02


def test_cache_entry_without_cost_replays_at_zero(tmp_path):
    cache = SemanticCache(str(tmp_path / "c.db"), encryption_key=Fernet.generate_key())
    cache.store(_cache_body(), "openai", "model",
                {"answer": "42"}, prompt_tokens=100, completion_tokens=20,
                origin_request_id="proxy:paid-1")
    hit = cache.lookup(_cache_body(), "openai", "model")
    assert hit is not None and hit.reported_cost_usd == 0.0


# --------------------------------------------------------------------------- #
# End-to-end billing through _record_usage_report
# --------------------------------------------------------------------------- #

def _store(tmp_path, monkeypatch, name):
    import api.server as server
    store = UsageStore(str(tmp_path / f"{name}.db"))
    store.create_key(hash_key(name), name)
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()
    return server


def test_live_openrouter_call_is_priced_with_real_spend_no_fee(tmp_path, monkeypatch):
    server = _store(tmp_path, monkeypatch, "bvt_or_live")
    result = server._record_usage_report(
        hash_key("bvt_or_live"),
        server.UsageReportRequest(
            provider="openrouter", model="anthropic/claude-opus-5",
            baseline_tokens=100, compressed_tokens=100,
            fresh_input_tokens=100, output_tokens=20,
            strategy="passthrough", request_id="live-1",
            provider_reported_cost_usd=0.02,
        ),
        authoritative=True,
    )
    # "cost should not be 0": the row records what OpenRouter actually charged.
    assert result["pricing_status"] == "priced"
    assert result["actual_cost_usd"] == 0.02
    assert result["measured_savings_usd"] == 0.0
    assert result["verified_savings_usd"] == 0
    assert result["brevitas_fee_usd"] == 0


def test_openrouter_exact_cache_replay_bills_the_reported_cost(tmp_path, monkeypatch):
    server = _store(tmp_path, monkeypatch, "bvt_or_replay")
    result = server._record_usage_report(
        hash_key("bvt_or_replay"),
        server.UsageReportRequest(
            provider="openrouter", model="anthropic/claude-opus-5",
            baseline_tokens=100, baseline_output_tokens=20, compressed_tokens=0,
            fresh_input_tokens=0, output_tokens=0,
            strategy="exact_cache", cache_attributable=True,
            savings_anchor_request_id="proxy:paid-1", request_id="replay-1",
            provider_reported_cost_usd=0.02,
        ),
        authoritative=True,
    )
    assert result["pricing_status"] == "priced"
    assert result["actual_cost_usd"] == 0.0
    assert result["baseline_cost_usd"] == 0.02
    assert result["measured_savings_usd"] == 0.02
    assert result["verified_savings_usd"] == 0.02
    assert result["brevitas_fee_usd"] == 0.005          # 25% of the reported cost
    # The zero-spend replay is anchored to the paid ancestor it named.
    assert result["savings_anchor_request_id"] == "proxy:paid-1"


def test_openrouter_without_reported_cost_stays_unpriced(tmp_path, monkeypatch):
    """The reseller guard is intact: no reported cost → no invented price."""
    server = _store(tmp_path, monkeypatch, "bvt_or_guard")
    result = server._record_usage_report(
        hash_key("bvt_or_guard"),
        server.UsageReportRequest(
            provider="openrouter", model="anthropic/claude-opus-5",
            baseline_tokens=100, compressed_tokens=80,
            fresh_input_tokens=80, output_tokens=0,
            strategy="passthrough", request_id="noprice-1",
        ),
        authoritative=True,
    )
    assert result["pricing_status"] == "unpriced"
    assert result["verified_savings_usd"] == 0


def test_reported_cost_never_overrides_a_first_party_price(tmp_path, monkeypatch):
    """A stray reported cost on an OpenAI row must not touch its MODEL_PRICES rate."""
    server = _store(tmp_path, monkeypatch, "bvt_first_party")
    result = server._record_usage_report(
        hash_key("bvt_first_party"),
        server.UsageReportRequest(
            provider="openai", model="gpt-4o-mini",
            baseline_tokens=100, compressed_tokens=80,
            fresh_input_tokens=80, output_tokens=0,
            strategy="passthrough", request_id="fp-1",
            provider_reported_cost_usd=999.0,
        ),
        authoritative=True,
    )
    assert result["pricing_status"] == "priced"
    # Priced from the gpt-4o-mini rate (80 * 0.15 / 1M), not from the 999 claim.
    assert result["actual_cost_usd"] == round(80 * 0.15 / 1_000_000, 10)
