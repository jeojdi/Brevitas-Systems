"""b9 v3 counterfactual "do no harm" lock (the death-spiral fix).

v2's gate read the LIVE observed hit rate: after a reorder busted the cache, the
falling hit rate told the gate "the provider isn't caching well" — which kept the
reorder on. v3 snapshots the hit rate BEFORE the first reorder as a counterfactual
baseline; if the post-reorder hit rate (>= _B9_MIN_POST observations) drops below
that snapshot minus a margin, b9 locks OFF for the pipeline, stickily.

Deterministic; no network.
"""
from __future__ import annotations

import uuid

import pytest

from token_efficiency_model.lossless import engine
from token_efficiency_model.lossless.router import BrevitasRouter


@pytest.fixture(autouse=True)
def _no_real_retrieval(monkeypatch):
    """b9 runs BEFORE the strategy branch; stub retrieval so no encoder loads."""
    def _stub(task, prior_context, k=8, use_adaptive=True, **kw):
        base = len(" ".join(prior_context).split())
        return {"selected_context": list(prior_context), "baseline_tokens": base,
                "optimized_tokens": base, "savings_pct": 0.0,
                "fallback_applied": True, "reason": "test-stub"}
    monkeypatch.setattr(engine, "retrieval_select", _stub)

BIG = "shared reference material for every analyst agent. " * 300   # >> 500-token gate


def _msgs(system: str, task: str) -> list[dict]:
    return [{"role": "system", "content": system},
            {"role": "user", "content": BIG},
            {"role": "user", "content": task}]


def _mk(pipe_suffix: str = ""):
    """Fresh router + unique pipeline id (module/global states are keyed by these)."""
    return BrevitasRouter(provider="deepseek", seed=7), f"pipe-{uuid.uuid4().hex[:8]}{pipe_suffix}"


def _observe_partial_cache(router: BrevitasRouter, sid: str, hit_frac: float, n: int = 2):
    for _ in range(n):
        router.observe_usage(sid, 10_000, int(10_000 * hit_frac))


def _engage_reorder(router: BrevitasRouter, pipe: str) -> str:
    """Drive the standard path until a reorder actually happens; returns the session
    that reordered. Natural cache is observed LOW (0.2) so the gate opens."""
    for agent in ("a1", "a2"):
        sid = f"s-{pipe}-{agent}"
        _observe_partial_cache(router, sid, 0.2)
        body = {"model": "deepseek-chat", "messages": _msgs(f"You are {agent}.", "task?")}
        engine.optimize_request(body, "deepseek", router, sid, pipeline=pipe, agent=agent)
    st9 = engine._b9_pipes[pipe]
    assert st9["reordered"], "setup: the shared block should have been promoted"
    assert not st9["locked"]
    return f"s-{pipe}-a2"


def test_lock_when_reorder_makes_cache_worse():
    router, pipe = _mk()
    sid = _engage_reorder(router, pipe)
    pre = engine._b9_pipes[pipe]["pre_hit"]
    assert pre > 0.0
    # post-reorder reality: cache collapses to zero for 3 observed calls
    usage = {"prompt_tokens": 10_000, "completion_tokens": 50,
             "prompt_tokens_details": {"cached_tokens": 0}}
    for _ in range(engine._B9_MIN_POST):
        engine.record_usage(usage, "deepseek", router, sid, pipeline=pipe)
    assert engine._b9_pipes[pipe]["locked"], "worse-than-counterfactual ⇒ sticky lock"

    # once locked, the pipeline is NEVER reordered again
    body = {"model": "deepseek-chat", "messages": _msgs("You are a2.", "task2?")}
    engine.optimize_request(body, "deepseek", router, sid, pipeline=pipe, agent="a2")
    assert body["messages"][0]["role"] == "system", "locked pipeline keeps natural order"


def test_no_lock_when_reorder_improves_cache():
    router, pipe = _mk()
    sid = _engage_reorder(router, pipe)
    # post-reorder reality: cross-agent hits IMPROVE on the 0.2 baseline
    usage = {"prompt_tokens": 10_000, "completion_tokens": 50,
             "prompt_tokens_details": {"cached_tokens": 6_000}}
    for _ in range(engine._B9_MIN_POST + 2):
        engine.record_usage(usage, "deepseek", router, sid, pipeline=pipe)
    assert not engine._b9_pipes[pipe]["locked"], "improved cache ⇒ b9 stays on"


def test_no_reorder_without_two_observations():
    # cold pipeline: no usage observed yet ⇒ conservative (assume well-cached), no reorder
    router, pipe = _mk()
    for agent in ("a1", "a2"):
        body = {"model": "deepseek-chat", "messages": _msgs(f"You are {agent}.", "t?")}
        engine.optimize_request(body, "deepseek", router, f"s-{pipe}-{agent}",
                                pipeline=pipe, agent=agent)
        assert body["messages"][0]["role"] == "system", "cold session must not reorder"
