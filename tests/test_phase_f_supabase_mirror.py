"""Canonical cloud receipt/store behavior (the old mirror path no longer exists)."""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.import_usage import import_sqlite
from api.store import SupabaseUsageStore, UsageStore, make_store
from brevitas.receipts import (SSEUsageParser, calculate_costs, count_request_tokens,
                               normalize_usage)


def test_provider_receipt_categories():
    cases = [
        ("anthropic", {"input_tokens": 10, "cache_read_input_tokens": 20,
                       "cache_creation_input_tokens": 5, "output_tokens": 7}, (10, 20, 5, 7)),
        ("openai", {"prompt_tokens": 30, "prompt_tokens_details": {"cached_tokens": 12},
                    "completion_tokens": 8}, (18, 12, 0, 8)),
        ("openai", {"input_tokens": 40, "input_tokens_details": {"cached_tokens": 15},
                    "output_tokens": 9}, (25, 15, 0, 9)),
        ("openai", {"prompt_tokens": 2_000, "prompt_tokens_details": {
                    "cached_tokens": 0, "cache_write_tokens": 2_000},
                    "completion_tokens": 9}, (0, 0, 2_000, 9)),
        ("deepseek", {"prompt_cache_hit_tokens": 45, "prompt_cache_miss_tokens": 5,
                      "completion_tokens": 6}, (5, 45, 0, 6)),
        ("google_gemini", {"promptTokenCount": 50, "cachedContentTokenCount": 10,
                           "candidatesTokenCount": 11}, (40, 10, 0, 11)),
        ("bedrock", {"inputTokens": 60, "outputTokens": 12}, (60, 0, 0, 12)),
        ("cohere", {"billed_units": {"input_tokens": 70, "output_tokens": 13}}, (70, 0, 0, 13)),
        ("ollama", {"prompt_eval_count": 80, "eval_count": 14}, (80, 0, 0, 14)),
        ("replicate", {"metrics": {"input_token_count": 90,
                                     "output_token_count": 15}}, (90, 0, 0, 15)),
    ]
    for provider, usage, expected in cases:
        receipt = normalize_usage(usage, provider)
        assert (receipt.fresh_input_tokens, receipt.cached_input_tokens,
                receipt.cache_write_tokens, receipt.output_tokens) == expected

    tiered = normalize_usage({
        "input_tokens": 0,
        "cache_creation_input_tokens": 100,
        "cache_creation": {"ephemeral_5m_input_tokens": 40,
                           "ephemeral_1h_input_tokens": 60},
    }, "anthropic")
    assert tiered.cache_write_5m_tokens == 40
    assert tiered.cache_write_1h_tokens == 60

    typed_gemini = SimpleNamespace(usage_metadata=SimpleNamespace(
        prompt_token_count=50, cached_content_token_count=10,
        candidates_token_count=11, thoughts_token_count=7,
        tool_use_prompt_token_count=3, total_token_count=68,
    ))
    gemini = normalize_usage(typed_gemini, "google_gemini")
    assert (gemini.fresh_input_tokens, gemini.cached_input_tokens,
            gemini.output_tokens) == (40, 10, 18)


def test_one_hour_cache_writes_use_two_x_price():
    from brevitas.receipts import TokenReceipt

    receipt = TokenReceipt(cache_write_tokens=100, cache_write_1h_tokens=100)
    costs = calculate_costs("anthropic", "claude-sonnet-4-6", 100, receipt)
    assert costs["baseline_cost_usd"] == 0.0003
    assert costs["actual_cost_usd"] == 0.0006
    assert costs["measured_savings_usd"] == -0.0003


def test_stream_parser_handles_split_final_events():
    parser = SSEUsageParser("openai")
    body = (b'data: {"type":"response.completed","response":{"id":"resp_1","usage":'
            b'{"input_tokens":100,"input_tokens_details":{"cached_tokens":40},'
            b'"output_tokens":20}}}\n\ndata: [DONE]\n\n')
    for chunk in (body[:17], body[17:71], body[71:]):
        parser.feed(chunk)
    receipt = parser.finish()
    assert parser.response_id == "resp_1"
    assert receipt.as_dict() == {"fresh_input_tokens": 60, "cached_input_tokens": 40,
                                 "cache_write_tokens": 0, "output_tokens": 20,
                                 "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0,
                                 "input_tokens": 100, "total_tokens": 120}

    anthropic = SSEUsageParser("anthropic")
    anthropic.feed(b'data: {"type":"message_start","message":{"id":"msg_1","usage":'
                   b'{"input_tokens":10,"cache_read_input_tokens":20,'
                   b'"cache_creation_input_tokens":5}}}\n\n')
    anthropic.feed(b'data: {"type":"message_delta","usage":{"output_tokens":7}}\n\n')
    assert anthropic.finish().as_dict()["total_tokens"] == 42


def test_unknown_model_is_unpriced():
    costs = calculate_costs("new-provider", "future-model", 100, normalize_usage(
        {"prompt_tokens": 80, "completion_tokens": 10}, "new-provider"))
    assert costs["pricing_status"] == "unpriced"
    assert costs["actual_cost_usd"] is None
    assert costs["measured_savings_usd"] is None


def test_dated_snapshot_uses_most_specific_alias_price():
    receipt = normalize_usage({
        "prompt_tokens": 1_000_000,
        "prompt_tokens_details": {"cached_tokens": 500_000},
    }, "openai")
    costs = calculate_costs(
        "openai", "gpt-4.1-mini-2025-04-14", 1_000_000, receipt,
        cache_attributable=False,
    )
    assert costs["pricing_status"] == "priced"
    assert costs["actual_cost_usd"] == .25
    assert costs["baseline_cost_usd"] == .25
    assert costs["prices"]["input"] == .4


def test_byte_preserving_text_block_split_has_zero_local_delta():
    text = "stable system prompt with tokenizer boundaries " * 20
    whole = count_request_tokens({"system": text, "messages": []})
    split = count_request_tokens({
        "system": [
            {"type": "text", "text": text[:137]},
            {"type": "text", "text": text[137:],
             "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [],
    })
    assert whole == split


def test_cloud_configuration_selects_supabase_and_sqlite_is_explicit_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-test-value")
    monkeypatch.delenv("BREVITAS_STORE", raising=False)
    assert isinstance(make_store(), SupabaseUsageStore)
    monkeypatch.setenv("BREVITAS_STORE", "sqlite")
    monkeypatch.setenv("BREVITAS_SQLITE_PATH", str(tmp_path / "fallback.db"))
    assert isinstance(make_store(), UsageStore)


def test_duplicate_and_breakdown_reconcile(tmp_path):
    store = UsageStore(str(tmp_path / "usage.db"))
    store.create_key("key", "test")
    common = dict(key_hash="key", baseline_tokens=100, optimized_tokens=75,
                  provider="openai", model="gpt-4o-mini", project="app",
                  environment="prod", source="api", request_id="request-1",
                  fresh_input_tokens=75, output_tokens=10,
                  measured_savings_usd=.01, verified_savings_usd=.008,
                  pricing_status="priced")
    assert store.record_usage(**common)
    assert not store.record_usage(**common)
    totals = store.get_stats("key")
    rows = store.get_breakdown("key")
    assert totals["total_calls"] == sum(row["calls"] for row in rows) == 1
    assert totals["total_tokens_saved"] == sum(row["tokens_saved"] for row in rows) == 25
    assert totals["total_measured_savings_usd"] == sum(row["measured_savings_usd"] for row in rows)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("select usage_raw from usage_log").fetchone()[0] == ""


def test_customer_totals_span_owned_keys_and_ownerless_keys_are_isolated(tmp_path):
    store = UsageStore(str(tmp_path / "accounts.db"))
    store.create_key("key-a", "a", owner_id="customer-1")
    store.create_key("key-b", "b", owner_id="customer-1")
    store.create_key("key-other", "other", owner_id="customer-2")
    store.create_key("legacy-a", "legacy-a")
    store.create_key("legacy-b", "legacy-b")
    store.record_usage("key-a", 100, 80, owner_id="customer-1", project="app",
                       source="api", provider="openai", model="gpt-4o-mini",
                       measured_savings_usd=.1, verified_savings_usd=.08)
    store.record_usage("key-b", 50, 40, owner_id="customer-1", project="app",
                       source="worker", provider="anthropic", model="claude-sonnet-4-6",
                       measured_savings_usd=.2, verified_savings_usd=.1)
    store.record_usage("key-other", 200, 100, owner_id="customer-2")

    totals = store.get_stats("key-a")
    rows = store.get_breakdown("key-a")
    assert totals["total_calls"] == sum(row["calls"] for row in rows) == 2
    assert totals["total_tokens_saved"] == sum(row["tokens_saved"] for row in rows) == 30
    assert totals["total_measured_savings_usd"] == round(sum(
        row["measured_savings_usd"] for row in rows), 8) == .3
    assert not store.delete_key("legacy-a", "legacy-b")
    assert store.key_exists("legacy-b")


def test_sqlite_import_twice_is_idempotent(tmp_path):
    source = UsageStore(str(tmp_path / "old.db"))
    source.record_usage("legacy-key", 200, 150, provider="deepseek", model="deepseek-chat",
                        ts="2024-01-02T03:04:05+00:00", pipeline="legacy")
    target = UsageStore(str(tmp_path / "new.db"))
    first = import_sqlite(source.db_path, target)
    second = import_sqlite(source.db_path, target)
    assert first == {"read": 1, "inserted": 1, "duplicates": 0}
    assert second == {"read": 1, "inserted": 0, "duplicates": 1}
    row = target._rows("legacy-key")[0]
    assert row["ts"] == "2024-01-02T03:04:05+00:00"
    assert row["project"] == "legacy"
