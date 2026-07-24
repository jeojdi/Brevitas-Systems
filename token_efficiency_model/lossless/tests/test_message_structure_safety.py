"""Regression and stress tests for provider-owned message ordering contracts."""
from __future__ import annotations

from copy import deepcopy

import pytest

from token_efficiency_model.lossless import engine
from token_efficiency_model.lossless.router import BrevitasRouter, RouteDecision


def _retrieve(*_args, **_kwargs):
    return RouteDecision("retrieve", "forced", 100.0, 10.0, 0.5, 0.1, 0.5, False)


@pytest.fixture
def forced_router(monkeypatch):
    router = BrevitasRouter(provider="openai", epsilon=0.0)
    monkeypatch.setattr(router, "decide", _retrieve)
    monkeypatch.setenv("BREVITAS_RETRIEVAL_ENABLED", "1")
    monkeypatch.setenv("BREVITAS_MESSAGE_REORDER", "1")
    monkeypatch.setattr(engine, "retrieval_select", lambda *_a, **_k: {
        "selected_context": [], "baseline_tokens": 5000, "optimized_tokens": 0,
        "fallback_applied": False, "reason": "forced", "method": "test",
    })
    return router


@pytest.mark.parametrize("messages", [
    [
        {"role": "user", "content": "draft"},
        {"role": "system", "content": "review now"},
        {"role": "assistant", "content": "reviewed"},
        {"role": "user", "content": "continue"},
    ],
    [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                             "name": "read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                        "content": "result"}]},
        {"role": "user", "content": "continue"},
    ],
    [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                          "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        {"role": "user", "content": "continue"},
    ],
    [
        {"role": "system", "content": [], "output_config": {"effort": "high"}},
        {"role": "user", "content": "continue"},
    ],
    [
        {"role": "user", "content": [{"type": "image", "source": {"type": "base64",
                                                                        "data": "AA=="}}]},
        {"role": "user", "content": "describe it"},
    ],
])
def test_structured_sequences_are_never_reordered_or_pruned(messages, forced_router):
    body = {"model": "test-model", "messages": deepcopy(messages)}
    original = deepcopy(body["messages"])
    meta = engine.optimize_request(body, "openai", forced_router, "safety",
                                   pipeline="fleet", agent="agent-a")
    assert body["messages"] == original
    assert meta["strategy"] == "cache_only"
    assert meta["reason"] == "message_structure_preserved"
    assert meta["quality_status"] == "byte_preserving"


def test_long_mid_system_transcript_keeps_every_index(forced_router):
    messages = []
    for i in range(12):
        messages.extend((
            {"role": "user", "content": f"question-{i}"},
            {"role": "assistant", "content": f"answer-{i}"},
        ))
    messages.insert(10, {"role": "system", "content": "switch policy"})
    body = {"messages": deepcopy(messages)}
    engine.optimize_request(body, "openai", forced_router, "long",
                            pipeline="fleet-long", agent="agent-b")
    assert body["messages"] == messages


def test_plain_text_chat_can_still_use_opted_in_retrieval(forced_router, monkeypatch):
    context = "large context"
    monkeypatch.setattr(engine, "retrieval_select", lambda *_a, **_k: {
        "selected_context": [context], "baseline_tokens": 5000,
        "optimized_tokens": 20, "fallback_applied": False,
        "reason": "forced", "method": "test",
    })
    body = {"messages": [
        {"role": "user", "content": context},
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "question"},
    ]}
    meta = engine.optimize_request(body, "openai", forced_router, "plain")
    assert meta["strategy"] == "retrieve"


def test_shared_prefix_reorder_is_never_marked_byte_preserving(forced_router, monkeypatch):
    from token_efficiency_model.lossless import shared_prefix

    original = [
        {"role": "user", "content": "agent-specific instruction"},
        {"role": "user", "content": "shared reference"},
        {"role": "user", "content": "question"},
    ]
    reordered = [original[1], original[0], original[2]]
    monkeypatch.setattr(shared_prefix, "layout_ex", lambda *_a, **_k: (reordered, True))
    body = {"model": "gpt-4o-mini", "messages": deepcopy(original)}

    meta = engine.optimize_request(
        body, "openai", forced_router, "reorder-safety",
        pipeline="fleet", agent="agent-a",
    )

    assert body["messages"] == reordered
    assert meta["strategy"] == "shared_prefix_reorder"
    assert meta["quality_status"] == "experimental_unverified"
    assert meta["semantic_order_changed"] is True


@pytest.mark.parametrize("messages", ["not-a-list", ["not-a-message"], [None], {"role": "user"}])
def test_malformed_message_containers_fail_safe_without_router(messages, forced_router):
    body = {"messages": deepcopy(messages)}
    original = deepcopy(body)
    meta = engine.optimize_request(body, "openai", forced_router, "malformed")
    assert body == original
    assert meta["strategy"] == "passthrough"
    assert meta["quality_status"] == "byte_preserving"
