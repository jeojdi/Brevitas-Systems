"""Offline lifecycle coverage for benchmark provider clients."""
from __future__ import annotations

import ast
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
OWNER_BENCHMARKS = {
    "incremental": ROOT / "benchmarks/oss_fleet/incremental_session_ab.py",
    "context": ROOT / "benchmarks/context_accuracy_benchmark.py",
    "ground_truth": ROOT / "benchmarks/ground_truth_benchmark.py",
    "ground_truth_v2": ROOT / "benchmarks/ground_truth_v2_benchmark.py",
    "deepseek": ROOT / "benchmarks/deepseek_benchmark.py",
    "cache": ROOT / "benchmarks/cache_benchmark.py",
}


class _ClosableClient:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize("name,path", OWNER_BENCHMARKS.items(), ids=OWNER_BENCHMARKS)
def test_assigned_benchmark_constructors_have_owned_finally_contract(name, path):
    """Every constructor is function-scoped and closes only when locally owned."""
    tree = ast.parse(path.read_text(), filename=str(path))
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

    constructors = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func.id if isinstance(node.func, ast.Name) else (
            node.func.attr if isinstance(node.func, ast.Attribute) else ""
        )
        if called in {"Anthropic", "OpenAI", "BrevitasClient", "BrevitasDropIn"}:
            constructors.append(node)

    assert constructors, f"{name} has no provider constructor under test"
    for constructor in constructors:
        owner = parents.get(constructor)
        while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents.get(owner)
        assert owner is not None, f"{name} constructs a provider client at module scope"
        assert any(arg.arg == "client" for arg in owner.args.args)

        finalizers = [
            node for node in ast.walk(owner)
            if isinstance(node, ast.Try) and node.finalbody
        ]
        assert finalizers, f"{name}.{owner.name} has no finally cleanup"
        assert any(
            any(
                isinstance(call, ast.Call)
                and (
                    isinstance(call.func, ast.Name)
                    and call.func.id in {"safe_close_resource", "_close_client"}
                )
                for statement in finalizer.finalbody
                for call in ast.walk(statement)
            )
            for finalizer in finalizers
        )
        assert any(
            isinstance(node, ast.Name) and node.id == "owned"
            for finalizer in finalizers
            for statement in finalizer.finalbody
            for node in ast.walk(statement)
        ), f"{name}.{owner.name} may close an injected client"


def test_incremental_session_does_not_close_injected_client(monkeypatch):
    from benchmarks.oss_fleet import incremental_session_ab

    client = _ClosableClient()
    monkeypatch.setattr(
        incremental_session_ab, "_run_provider_with_client",
        lambda *_args: "complete",
    )
    assert incremental_session_ab.run_provider(
        "openai", "baseline", "nonce", [], client=client
    ) == "complete"
    assert client.close_calls == 0


def test_cache_benchmark_closes_owned_failure_but_not_injected(monkeypatch):
    monkeypatch.setenv("Deepseek_api_key", "offline-test-key")
    monkeypatch.setitem(
        sys.modules, "datasets", SimpleNamespace(load_dataset=lambda *_args, **_kwargs: [])
    )
    monkeypatch.setitem(
        sys.modules, "openai", SimpleNamespace(OpenAI=lambda **_kwargs: _ClosableClient())
    )
    monkeypatch.setitem(sys.modules, "token_efficiency_model.common", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "token_efficiency_model.common.metrics",
        SimpleNamespace(estimate_tokens=lambda _value: 0),
    )
    from benchmarks import cache_benchmark

    injected = _ClosableClient()
    monkeypatch.setattr(
        cache_benchmark, "_run_cache_benchmark_with_client",
        lambda *_args: "complete",
    )
    assert cache_benchmark.run_cache_benchmark("arc", client=injected) == "complete"
    assert injected.close_calls == 0

    owned = _ClosableClient()
    monkeypatch.setattr(cache_benchmark, "OpenAI", lambda **_kwargs: owned)

    def fail(*_args):
        raise RuntimeError("offline failure")

    monkeypatch.setattr(cache_benchmark, "_run_cache_benchmark_with_client", fail)
    with pytest.raises(RuntimeError, match="offline failure"):
        cache_benchmark.run_cache_benchmark("arc")
    assert owned.close_calls == 1


@pytest.mark.parametrize("fail", [False, True])
def test_oss_ab_closes_client_on_success_and_retry_exhaustion(monkeypatch, fail):
    from benchmarks import oss_ab

    client = _ClosableClient()
    monkeypatch.setattr(oss_ab, "_mk_client", lambda *_args: client)
    monkeypatch.setattr(
        oss_ab, "MARKETING_AGENTS",
        [("Analyst", "Analyze safely.", "Return a result.")],
    )
    monkeypatch.setattr(oss_ab.time, "sleep", lambda _seconds: None)

    def call(*_args):
        if fail:
            raise RuntimeError("offline failure")
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=2,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
            usage=usage,
        )

    monkeypatch.setattr(oss_ab, "_call", call)
    result = oss_ab.run("deepseek", "marketing", optimized=True)
    assert ("error" in result) is fail
    assert client.close_calls == 1


@pytest.mark.parametrize("fail", [False, True])
def test_live_e2e_closes_dropin_on_success_and_retry_exhaustion(monkeypatch, fail):
    from benchmarks import live_e2e

    class FakeDropIn(_ClosableClient):
        instances = []

        def __init__(self, **_kwargs):
            super().__init__()
            self.instances.append(self)

        def chat(self, **_kwargs):
            if fail:
                raise RuntimeError("offline failure")
            response = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="expected"))]
            )
            savings = SimpleNamespace(
                input_fresh=10, input_cached=2, output_tokens=1, cached_tokens=2,
                input_savings_pct=10.0, savings_pct=5.0, retrieval_applied=False,
            )
            return response, savings

    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setattr(live_e2e, "BrevitasDropIn", FakeDropIn)
    monkeypatch.setattr(live_e2e.time, "sleep", lambda _seconds: None)

    result = live_e2e.run_provider(
        "openai", "system", "context", [("question", ["expected"])]
    )
    assert bool(result["errors"]) is fail
    assert len(FakeDropIn.instances) == 1
    assert FakeDropIn.instances[0].close_calls == 1


def test_accuracy_suite_closes_raw_client_after_partial_progress(monkeypatch):
    from benchmarks import accuracy_suite

    client = _ClosableClient()
    calls = 0

    def call(*_args):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("offline failure")
        return object()

    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setattr(accuracy_suite, "raw_client", lambda _provider: client)
    monkeypatch.setattr(accuracy_suite, "call", call)
    monkeypatch.setattr(
        accuracy_suite, "read", lambda _response, _provider: (
            "The answer is (A)", 0.001, 10, 2
        ),
    )
    monkeypatch.setattr(accuracy_suite.time, "sleep", lambda _seconds: None)

    result = accuracy_suite.run_arm(
        "openai", "mmlu", "system",
        [{"q": "first", "gold": "(A)"}, {"q": "second", "gold": "(B)"}],
        "mc", optimized=False,
    )
    assert result["n"] == 1
    assert client.close_calls == 1
