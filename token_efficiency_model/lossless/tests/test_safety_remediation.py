"""Regression tests for the safety-audit remediation (P0/P1/P2).

Covers the request-path invariants that the audit found broken:
  * optimize_request reports response_faithful correctly (P0.1)
  * a tripped quality-gate lever forces full-context fallback (P0.6)
  * message reordering is OFF by default (P0.5)
  * BM25 drops zero-score (lexically-irrelevant) docs (P1.9)
  * lossless prompt optimization is byte-identical (P0.4)
  * RLM injects `question` into its REPL so emitted code no longer NameErrors (P2.11)

Deterministic; no network, no model downloads (retrieval is stubbed).
"""
from __future__ import annotations

import types

import pytest

from token_efficiency_model.lossless import engine
from token_efficiency_model.lossless.router import BrevitasRouter
from token_efficiency_model.quality import gate


def _msgs():
    return [{"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "some earlier context about widgets"},
            {"role": "user", "content": "what is the price?"}]


@pytest.fixture(autouse=True)
def _reset_levers():
    gate.reset_lever("retrieval")
    gate.reset_lever("compression")
    gate.reset_lever("semantic_cache")
    yield
    gate.reset_lever("retrieval")
    gate.reset_lever("compression")
    gate.reset_lever("semantic_cache")


# ── P0.1 / P0.5: faithfulness ────────────────────────────────────────────────

def test_default_path_is_faithful_and_preserves_order(monkeypatch):
    monkeypatch.delenv("BREVITAS_RETRIEVAL_ENABLED", raising=False)
    monkeypatch.delenv("BREVITAS_MESSAGE_REORDER", raising=False)
    body = {"model": "gpt-4o-mini", "messages": _msgs()}
    before = [m["content"] for m in body["messages"]]
    meta = engine.optimize_request(body, "openai", BrevitasRouter(provider="openai"), "sess-1")
    assert meta["response_faithful"] is True
    assert [m["content"] for m in body["messages"]] == before  # no reorder, no prune


def test_retrieval_prune_marks_unfaithful(monkeypatch):
    """When retrieval actually drops context, the response is NOT faithful to the original."""
    monkeypatch.setenv("BREVITAS_RETRIEVAL_ENABLED", "1")

    # Force the router to pick retrieval, and stub the selector to prune to one chunk.
    def _decide(self, sid, stable, query):
        return types.SimpleNamespace(strategy="retrieve", reason="test")
    monkeypatch.setattr(BrevitasRouter, "decide", _decide)

    def _stub_select(task, prior_context, k=8, use_adaptive=True, **kw):
        keep = prior_context[:1]  # keep only the first context chunk
        return {"selected_context": keep, "baseline_tokens": 100,
                "optimized_tokens": 20, "savings_pct": 80.0,
                "fallback_applied": False, "reason": "test", "method": "stub"}
    monkeypatch.setattr(engine, "retrieval_select", _stub_select)

    body = {"model": "gpt-4o-mini", "messages": _msgs()}
    meta = engine.optimize_request(body, "openai", BrevitasRouter(provider="openai"), "sess-prune")
    assert meta["strategy"] == "retrieve"
    assert meta["response_faithful"] is False


# ── P0.6: tripped gate forces full-context fallback ──────────────────────────

def test_tripped_retrieval_lever_forces_full_context(monkeypatch):
    monkeypatch.setenv("BREVITAS_RETRIEVAL_ENABLED", "1")
    gate.trip_lever("retrieval")

    def _decide(self, sid, stable, query):
        return types.SimpleNamespace(strategy="retrieve", reason="test")
    monkeypatch.setattr(BrevitasRouter, "decide", _decide)

    # If the gate is honored this stub must never run (we force cache_only first).
    def _boom(*a, **k):
        raise AssertionError("retrieval_select must not run when the lever is tripped")
    monkeypatch.setattr(engine, "retrieval_select", _boom)

    body = {"model": "gpt-4o-mini", "messages": _msgs()}
    before = [m["content"] for m in body["messages"]]
    meta = engine.optimize_request(body, "openai", BrevitasRouter(provider="openai"), "sess-trip")
    assert meta["strategy"] == "cache_only"
    assert meta["reason"] == "retrieval_gate_tripped"
    assert meta["response_faithful"] is True
    assert [m["content"] for m in body["messages"]] == before


def test_lever_allowed_fails_closed_on_env(monkeypatch):
    monkeypatch.setenv("BREVITAS_TRIPPED_LEVERS", "semantic_cache, compression")
    assert gate.lever_allowed("semantic_cache") is False
    assert gate.lever_allowed("compression") is False
    assert gate.lever_allowed("retrieval") is True
    assert gate.lever_allowed("") is False  # empty/unknown fails closed


# ── P1.9: BM25 zero-score filter ─────────────────────────────────────────────

def test_bm25_drops_zero_score_docs():
    from token_efficiency_model.lossless.retrieval import BM25Retriever
    r = BM25Retriever()
    r.index(["the cat sat on the mat", "dogs run quickly in the park"])
    assert r.retrieve("quantum chromodynamics", k=5) == []      # no lexical overlap
    hits = r.retrieve("cat", k=5)
    assert len(hits) == 1 and hits[0][2] > 0.0                  # only the relevant doc


# ── P0.4: lossless prompt optimization is byte-identical ─────────────────────

@pytest.mark.parametrize("text", [
    "key:\n  nested: value\n  list:\n    - a\n    - b\n",       # YAML
    "def f(x):\n    if x:\n        return  x\n",                 # Python (indentation)
    "target:\n\tgcc  -o  main  main.c\n",                       # Makefile (tabs)
    "# Title\n\n- item   with   spaces\n\n\n\ntrailing   \n",   # Markdown
])
def test_lossless_prompt_is_byte_identical(text):
    from token_efficiency_model.lossless.prompt_optimizer import optimize_prompt
    out = optimize_prompt(text, rate=1.0)
    assert out.optimized == text
    assert out.lossy is False
    assert out.method == "lossless"


# ── P2.11: RLM injects `question` into the REPL ──────────────────────────────

def test_rlm_repl_exposes_question():
    from token_efficiency_model.lossless.rlm import RLM, REPLState
    r = RLM(llm=lambda p: "")
    state = REPLState(P="the long document", question="who won?")
    # Emitted code that references `question` (as the RLM's own instructions demand)
    # must run without a NameError and see the real question string.
    out = r._repl(state, "print(question)")
    assert "who won?" in out
    assert "<error" not in out
