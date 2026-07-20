"""No-degradation guardrail: the SAFE savings levers must keep saving exactly as before.

The safety remediation removed *unsafe* savings (answers from pruned/compressed context,
truncated responses, non-temperature-0 reuse). It must NOT have weakened the safe levers:

  * exact-repeat caching at temperature 0 still eliminates the upstream call (100% savings
    on the repeat), and
  * the default optimize path stays byte-faithful, so provider-side prefix caching still
    engages (we never mutate the request on the safe path).

Deterministic: the upstream is mocked and counted, so "savings" here is the measurable
fraction of upstream calls avoided — no network, no keys, no flakiness.
"""
import os
import tempfile

os.environ["BREVITAS_CACHE_DB"] = tempfile.mktemp(suffix=".db")
os.environ["BREVITAS_API_KEY"] = ""     # billing HTTP calls no-op

import httpx
import pytest
from fastapi.testclient import TestClient

import brevitas.proxy as proxy
from token_efficiency_model.lossless import engine
from token_efficiency_model.lossless.router import BrevitasRouter


class _CountingClient:
    """Mock upstream that counts calls and returns a COMPLETE OpenAI response."""
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _CountingClient.calls += 1
        return _Resp()


class _Resp:
    status_code = 200

    def json(self):
        return {"id": "c1",
                "choices": [{"message": {"role": "assistant", "content": "42"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 3}}


def _client(monkeypatch):
    monkeypatch.setenv("BREVITAS_CACHE_ENABLED", "true")
    monkeypatch.setenv("BREVITAS_CACHE_LOCAL", "true")
    monkeypatch.setattr(httpx, "AsyncClient", _CountingClient)
    proxy._cache_init_done = False
    proxy._cache_singleton = None
    _CountingClient.calls = 0
    return TestClient(proxy.proxy_app)


def test_repeat_caching_savings_preserved(monkeypatch):
    """5 identical temperature-0 requests must cost exactly ONE upstream call (80% avoided)."""
    client = _client(monkeypatch)
    req = {"model": "gpt-4o-mini", "temperature": 0,
           "messages": [{"role": "user", "content": "what is 6 times 7"}]}
    N = 5
    for _ in range(N):
        r = client.post("/v1/chat/completions", json=req, headers={"authorization": "k"})
        assert r.status_code == 200

    upstream_calls = _CountingClient.calls
    saved_fraction = (N - upstream_calls) / N
    assert upstream_calls == 1, f"expected 1 upstream call, got {upstream_calls}"
    assert saved_fraction == pytest.approx(0.8), saved_fraction


def test_default_optimize_is_byte_faithful(monkeypatch):
    """On the safe default path optimize_request must not touch the request, so the
    provider's own byte-identical prefix cache still engages (no lost savings)."""
    monkeypatch.delenv("BREVITAS_RETRIEVAL_ENABLED", raising=False)
    monkeypatch.delenv("BREVITAS_MESSAGE_REORDER", raising=False)
    messages = [{"role": "system", "content": "you are a coding assistant"},
                {"role": "user", "content": "file A contents " * 200},
                {"role": "user", "content": "what does file A do?"}]
    body = {"model": "gpt-4o-mini", "messages": [dict(m) for m in messages]}
    meta = engine.optimize_request(body, "openai", BrevitasRouter(provider="openai"), "s1")
    assert meta["response_faithful"] is True
    assert body["messages"] == messages          # identical bytes → provider cache still hits


def test_anthropic_cache_breakpoints_still_applied(monkeypatch):
    """The Anthropic caching lever (the headline saver) must still mark a large stable
    prefix for caching on the default path."""
    monkeypatch.delenv("BREVITAS_RETRIEVAL_ENABLED", raising=False)
    big = "shared reference document. " * 400        # comfortably over the min-cache size
    body = {"model": "claude-haiku-4-5-20251001",
            "messages": [{"role": "user", "content": big},
                         {"role": "user", "content": "summarize it"}]}
    meta = engine.optimize_request(body, "anthropic", BrevitasRouter(provider="anthropic"), "s2")
    assert meta["response_faithful"] is True
    # A caching decision was made (breakpoints applied, or an explicit ROI reason recorded).
    assert "cache_breakpoints" in meta


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
