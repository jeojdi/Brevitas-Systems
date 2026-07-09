"""Tests for the task-aware compression router (classification + policy + fail-safe)."""

from token_efficiency_model.lossless import semantic_gate
from token_efficiency_model.lossless.task_router import (
    TaskCompressionRouter,
    classify_task,
    _protect_tokens,
    _DEFAULT_RATES,
)


# --- task classification (the user's examples) ----------------------------- #
def test_marketing_reel_is_creative():
    assert classify_task("Make me a marketing reel for our new oak table") == "creative"
    assert classify_task("Write an Instagram caption + tagline for the launch") == "creative"


def test_frontend_code_is_code():
    assert classify_task("Make me a frontend React component for this dashboard") == "code"
    assert classify_task("Build me a web app with an HTML/CSS landing page") == "code"


def test_precise_tasks_classified():
    assert classify_task("Calculate the derivative step by step") == "reasoning"
    assert classify_task("Extract the exact invoice total from this text") == "extraction"
    assert classify_task("Summarize this article in key points") == "summarize"


def test_hint_overrides_classifier():
    assert classify_task("anything at all", hint="code") == "code"
    assert classify_task("ignored", hint="not-a-real-task") == "general"  # invalid hint -> classify


# --- the policy: simple tasks compress harder, precise ones lighter -------- #
def test_creative_rate_more_aggressive_than_reasoning():
    assert _DEFAULT_RATES["creative"] < _DEFAULT_RATES["reasoning"]
    assert _DEFAULT_RATES["extraction"] >= 0.85   # near-lossless for exact tasks


# --- token protection ("retain as much context as possible") --------------- #
def test_protect_tokens_forces_numbers_not_identifiers():
    # precise tasks force load-bearing NUMBERS; prose identifiers are NOT forced because large
    # force lists make real LLMLingua-2 assert and crash the whole compression into a no-op.
    prompt = "Build a function calculateTotal that returns 42.50 for user_id 1001"
    prot = _protect_tokens(prompt, "code")
    assert "42.50" in prot and "1001" in prot
    assert "calculateTotal" not in prot and "user_id" not in prot


# --- router behavior + fail-safe ------------------------------------------- #
def test_router_classifies_and_failsafes_to_lossless_without_llmlingua():
    r = TaskCompressionRouter()
    res = r.route("Make me a marketing reel for our new product. " * 10)
    assert res.task == "creative"
    assert res.rate == _DEFAULT_RATES["creative"]
    # without the [promptopt] extra installed, must fall back to lossless (never crash, never lossy)
    if res.optimization.method == "lossless":
        assert res.optimization.lossy is False
        assert "LLMLingua" in res.optimization.note
    else:
        assert res.optimization.lossy is True
    assert res.optimization.tokens_after <= res.optimization.tokens_before


def test_router_counts_code_blocks_for_code_tasks():
    prompt = ("Make me a frontend component for this:\n"
              "```jsx\nfunction Btn(){ return <button>Go</button> }\n```\n"
              "Style it nicely and keep it accessible.")
    res = TaskCompressionRouter().route(prompt)
    assert res.task == "code"
    assert res.protected_code_blocks == 1
    # the code fence must survive verbatim regardless of compression
    assert "function Btn(){ return <button>Go</button> }" in res.optimization.optimized


def test_custom_rates():
    r = TaskCompressionRouter(rates={**_DEFAULT_RATES, "creative": 0.3})
    res = r.route("Write a punchy ad reel script. " * 8)
    assert res.rate == 0.3


# --- semantic gate on the router path (same floor as the message optimizer) ------ #
_PROSE = "Please summarize the following operational report in careful detail. " * 6


def test_router_semantic_gate_rejects_low_similarity(monkeypatch):
    """A compression that drifts below the semantic floor is rejected — the lossless prose ships
    instead — so the router path can't silently emit meaning-degraded output."""
    monkeypatch.setattr(semantic_gate, "min_similarity", lambda: 0.75)
    monkeypatch.setattr(semantic_gate, "semantic_similarity", lambda a, b: 0.10)  # far below floor
    r = TaskCompressionRouter(compress_fn=lambda text, rate, force: "ZZZ")  # meaning-destroying
    res = r.route(_PROSE, task_hint="summarize")
    assert res.reason == "gate_rejected"
    assert res.optimization.lossy is False      # nothing accepted -> not a lossy result
    assert "ZZZ" not in res.optimization.optimized


def test_router_semantic_gate_accepts_high_similarity(monkeypatch):
    """A compression that clears the floor is accepted and its similarity is reported."""
    monkeypatch.setattr(semantic_gate, "min_similarity", lambda: 0.75)
    monkeypatch.setattr(semantic_gate, "semantic_similarity", lambda a, b: 0.95)
    r = TaskCompressionRouter(compress_fn=lambda text, rate, force: "concise report summary")
    res = r.route(_PROSE, task_hint="summarize")
    assert res.reason == "compressed"
    assert res.optimization.lossy is True
    assert res.quality_sim == 0.95


def test_router_gate_disabled_accepts_without_measuring(monkeypatch):
    """Floor of 0 disables the gate: compression is accepted with no similarity check (fail-open)."""
    monkeypatch.setattr(semantic_gate, "min_similarity", lambda: 0.0)
    def _boom(a, b):  # must never be called when the gate is off
        raise AssertionError("similarity measured despite disabled gate")
    monkeypatch.setattr(semantic_gate, "semantic_similarity", _boom)
    r = TaskCompressionRouter(compress_fn=lambda text, rate, force: "concise report summary")
    res = r.route(_PROSE, task_hint="summarize")
    assert res.reason == "compressed"
    assert res.quality_sim is None
