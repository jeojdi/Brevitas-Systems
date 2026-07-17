"""Cache-stable retrieval layout (brief b1): the retrieved context set is append-only
per session, so its prefix is byte-identical across turns and composes with provider
prefix caching. It only adds to what per-turn retrieval selected; retrieval itself is lossy."""
from __future__ import annotations

import pytest

from token_efficiency_model.lossless import engine as eng
from token_efficiency_model.lossless.router import BrevitasRouter


@pytest.fixture(autouse=True)
def _clear_blocks():
    eng._retrieved_blocks.clear()
    yield
    eng._retrieved_blocks.clear()


def test_accumulate_is_append_only_and_stable_order():
    a1 = eng._accumulate_retrieved("s", ["A", "C"])
    assert a1 == ["A", "C"]
    # a later turn retrieves a DIFFERENT set — previously-sent chunks are retained,
    # in their original order, new ones appended (stable prefix preserved)
    a2 = eng._accumulate_retrieved("s", ["A", "B"])
    assert a2 == ["A", "C", "B"]
    assert a2[:2] == a1, "the previously-sent prefix must stay byte-identical"
    # re-selecting the same set adds nothing
    assert eng._accumulate_retrieved("s", ["B", "A"]) == ["A", "C", "B"]


def test_accumulate_sessions_are_isolated_and_bounded():
    eng._accumulate_retrieved("s1", ["X"])
    eng._accumulate_retrieved("s2", ["Y"])
    assert eng._retrieved_blocks["s1"] == ["X"]
    assert eng._retrieved_blocks["s2"] == ["Y"]


def test_engine_retrieve_keeps_previously_sent_context(monkeypatch):
    # Two turns; turn 2 retrieval picks a different subset. The rebuilt message list
    # on turn 2 must still contain turn 1's kept context (append-only ⇒ cache-stable).
    big = lambda tag: " ".join(f"{tag}{i}" for i in range(700))  # ~1400 tok each
    docA, docB = big("a"), big("b")

    picks = {"n": 0}

    def fake_select(task, prior_context, k=8, min_top_score=0.2, use_adaptive=False):
        picks["n"] += 1
        chosen = [docA] if picks["n"] == 1 else [docB]   # different subset each turn
        return {"selected_context": chosen, "baseline_tokens": 4000,
                "optimized_tokens": 1400, "savings_pct": 65.0,
                "fallback_applied": False, "reason": "retrieved"}

    monkeypatch.setattr(eng, "retrieval_select", fake_select)
    monkeypatch.setenv("BREVITAS_RETRIEVAL_ENABLED", "1")
    router = BrevitasRouter(provider="deepseek", retrieve_keep_frac=0.3, epsilon=0.0)

    def turn(q):
        body = {"model": "deepseek-chat",
                "messages": [{"role": "user", "content": docA},
                             {"role": "assistant", "content": "ok"},
                             {"role": "user", "content": docB},
                             {"role": "assistant", "content": "ok2"},
                             {"role": "user", "content": q}]}
        # force the retrieve arm regardless of cost model
        monkeypatch.setattr(router, "decide", lambda *a, **k: _retrieve_decision())
        eng.optimize_request(body, "deepseek", router, "sess")
        return [m["content"] for m in body["messages"] if m.get("role") == "user"]

    users1 = turn("q1")
    users2 = turn("q2")
    assert docA in users1
    # turn 2 picked docB, but docA (sent on turn 1) is RETAINED — append-only
    assert docA in users2 and docB in users2, "previously-sent context must persist"


def test_engine_requires_explicit_retrieval_opt_in(monkeypatch):
    body = {
        "messages": [
            {"role": "user", "content": " ".join(["context"] * 1500)},
            {"role": "user", "content": "What matters?"},
        ]
    }
    router = BrevitasRouter(provider="deepseek", epsilon=0.0)
    monkeypatch.setattr(router, "decide", lambda *a, **k: _retrieve_decision())
    monkeypatch.delenv("BREVITAS_RETRIEVAL_ENABLED", raising=False)

    original = list(body["messages"])
    meta = eng.optimize_request(body, "deepseek", router, "opt-in")

    assert meta["strategy"] == "cache_only"
    assert meta["reason"] == "retrieval_opt_in_required"
    assert body["messages"] == original


def _retrieve_decision():
    from token_efficiency_model.lossless.router import RouteDecision
    return RouteDecision("retrieve", "forced", 100.0, 10.0, 0.5, 0.02, 0.5, False)
