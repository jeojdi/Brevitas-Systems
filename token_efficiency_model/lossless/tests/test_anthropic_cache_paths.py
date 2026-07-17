"""Anthropic cache_control placement across ALL engine paths.

Regression tests for the live-E2E finding (2026-07-01): Anthropic showed ZERO cached
tokens on a realistic agent session because (a) the retrieve path returned before
marker placement and (b) the turn-1 "big context + question in one message" pattern
had no stable prefix to mark. OpenAI/DeepSeek cache byte-identical prefixes
automatically; Anthropic caches nothing without explicit markers.
"""
from __future__ import annotations

import pytest

from token_efficiency_model.lossless import engine as eng
from token_efficiency_model.lossless.provider_cache import apply_anthropic_cache
from token_efficiency_model.lossless.router import BrevitasRouter


def _big_text(n_words: int = 650) -> str:
    # ~1300 tokens (each "wordN" ≈ 2 tokens): above the 1024 default minimum,
    # below haiku's 2048 documented minimum.
    return " ".join(f"word{i}" for i in range(n_words))


def _huge_text(n_words: int = 2600) -> str:
    return " ".join(f"tok{i}" for i in range(n_words))


# --------------------------------------------------------------------------- #
# apply_anthropic_cache: last-user-message stable blocks
# --------------------------------------------------------------------------- #
def test_last_message_context_block_marked():
    body = {"model": "claude-sonnet-4-6",
            "messages": [{"role": "user",
                          "content": [{"type": "text", "text": _big_text()},
                                      {"type": "text", "text": "What is X?"}]}]}
    plan = apply_anthropic_cache(body)
    blocks = body["messages"][0]["content"]
    assert plan.breakpoints >= 1
    assert "cache_control" in blocks[0], "stable context block must be marked"
    assert "cache_control" not in blocks[1], "volatile final block must NEVER be marked"


def test_final_block_never_marked_even_when_huge():
    body = {"model": "claude-sonnet-4-6",
            "messages": [{"role": "user",
                          "content": [{"type": "text", "text": "context intro"},
                                      {"type": "text", "text": _big_text()}]}]}
    apply_anthropic_cache(body)
    assert "cache_control" not in body["messages"][0]["content"][-1]


def test_haiku_requires_2048_minimum():
    # ~1300 tokens: above the 1024 default, below haiku's 2048 documented minimum
    mk = lambda model: {"model": model,
                        "messages": [{"role": "user",
                                      "content": [{"type": "text", "text": _big_text()},
                                                  {"type": "text", "text": "Q?"}]}]}
    haiku = mk("claude-haiku-4-5-20251001")
    sonnet = mk("claude-sonnet-4-6")
    assert apply_anthropic_cache(haiku).breakpoints == 0
    assert apply_anthropic_cache(sonnet).breakpoints >= 1
    # and haiku DOES mark once above 2048
    haiku2 = {"model": "claude-haiku-4-5-20251001",
              "messages": [{"role": "user",
                            "content": [{"type": "text", "text": _huge_text()},
                                        {"type": "text", "text": "Q?"}]}]}
    assert apply_anthropic_cache(haiku2).breakpoints >= 1


def _any_marker(body: dict) -> bool:
    sysv = body.get("system")
    if isinstance(sysv, list) and any(isinstance(b, dict) and "cache_control" in b for b in sysv):
        return True
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(b, dict) and "cache_control" in b for b in c):
            return True
    return False


# --------------------------------------------------------------------------- #
# engine.optimize_request: markers on EVERY path
# --------------------------------------------------------------------------- #
def test_engine_marks_anthropic_on_retrieve_path_after_reuse_is_observed(monkeypatch):
    ctx1, ctx2 = _huge_text(1400), _huge_text(1500)

    def fake_select(task, prior_context, k=8, min_top_score=0.2, use_adaptive=False):
        keep = list(prior_context[:1])
        return {"selected_context": keep, "baseline_tokens": 4000, "optimized_tokens": 1500,
                "savings_pct": 60.0, "fallback_applied": False, "reason": "retrieved"}

    monkeypatch.setattr(eng, "retrieval_select", fake_select)
    monkeypatch.setenv("BREVITAS_RETRIEVAL_ENABLED", "1")
    def request_body():
        return {"model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": ctx1},
                             {"role": "assistant", "content": "noted"},
                             {"role": "user", "content": ctx2},
                             {"role": "user", "content": "final question?"}]}
    router = BrevitasRouter(provider="anthropic", retrieve_keep_frac=0.6)
    cold = request_body()
    cold_meta = eng.optimize_request(cold, "anthropic", router, "s1")
    assert cold_meta.get("cache_breakpoints", 0) == 0
    assert cold_meta.get("cache_roi", "").startswith("reuse_unproven")
    body = request_body()
    meta = eng.optimize_request(body, "anthropic", router, "s1")
    assert meta["strategy"] == "retrieve"
    assert "cache_breakpoints" in meta
    assert _any_marker(body), "retrieve path must still place Anthropic cache markers"


def test_engine_marks_anthropic_document_block_after_reuse():
    # The reusable document rides inside the final user message. The engine must
    # recognize it as stable, wait for observed reuse, then cache it.
    def request_body():
        return {"model": "claude-sonnet-4-6",
                "messages": [{"role": "user",
                              "content": [{"type": "text", "text": _huge_text()},
                                          {"type": "text", "text": "What is X?"}]}]}
    router = BrevitasRouter(provider="anthropic")
    cold = request_body()
    cold_meta = eng.optimize_request(cold, "anthropic", router, "s2")
    assert cold_meta.get("cache_breakpoints", 0) == 0
    body = request_body()
    meta = eng.optimize_request(body, "anthropic", router, "s2")
    assert meta["strategy"] == "cache_only"
    assert meta.get("cache_breakpoints", 0) >= 1
    assert _any_marker(body)


def _count_markers(body: dict) -> int:
    n = 0
    for blocks in [body.get("tools"), body.get("system")] + \
                  [m.get("content") for m in body.get("messages", []) if isinstance(m, dict)]:
        if isinstance(blocks, list):
            n += sum(1 for b in blocks if isinstance(b, dict) and "cache_control" in b)
    return n


def test_repeated_application_never_exceeds_four_markers():
    # Real customer pattern: message dicts are REUSED across turns (append-only history),
    # so markers from previous calls persist. Anthropic 400s above 4 cache_control blocks.
    history = [{"role": "user", "content": [{"type": "text", "text": _huge_text()}]},
               {"role": "assistant", "content": "noted"}]
    for turn in range(2, 8):
        body = {"model": "claude-sonnet-4-6",
                "messages": [dict(m) for m in history] + [
                    {"role": "user", "content": f"question {turn}?"}]}
        # dict(m) copies the message dict but NOT the content list — like real reuse
        plan = apply_anthropic_cache(body)
        assert _count_markers(body) <= 4, f"turn {turn}: {_count_markers(body)} markers"
        assert plan.breakpoints >= 1
        history = body["messages"] + [{"role": "assistant", "content": f"answer {turn}"}]


def test_strip_then_place_keeps_marks_at_latest_stable_positions():
    msgs = [{"role": "user", "content": [{"type": "text", "text": _huge_text()}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            {"role": "user", "content": "q1"}]
    body = {"model": "claude-sonnet-4-6", "messages": msgs}
    apply_anthropic_cache(body)
    first_total = _count_markers(body)
    # grow the conversation; old dicts (with old markers) are reused
    body2 = {"model": "claude-sonnet-4-6",
             "messages": msgs + [{"role": "assistant", "content": [{"type": "text", "text": _big_text()}]},
                                 {"role": "user", "content": "q2"}]}
    apply_anthropic_cache(body2)
    assert first_total <= 4 and _count_markers(body2) <= 4


def test_engine_never_marks_non_anthropic():
    body = {"model": "gpt-4o-mini",
            "messages": [{"role": "user",
                          "content": [{"type": "text", "text": _huge_text()},
                                      {"type": "text", "text": "Q?"}]}]}
    router = BrevitasRouter(provider="openai")
    eng.optimize_request(body, "openai", router, "s3")
    assert not _any_marker(body), "OpenAI/DeepSeek bodies must never be mutated with markers"
