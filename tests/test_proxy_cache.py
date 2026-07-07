"""Proxy semantic-cache wiring: a repeated request must short-circuit the upstream.

Runs without the optional embedding dependency — exercises the exact-hash layer,
which is the layer the proxy wiring is responsible for. Upstream is mocked so no
network/keys are needed.
"""
import os
import tempfile

os.environ["BREVITAS_CACHE_DB"] = tempfile.mktemp(suffix=".db")
os.environ["BREVITAS_API_KEY"] = ""  # disable billing HTTP calls (report_usage no-ops)

import httpx
from fastapi.testclient import TestClient

import brevitas.proxy as proxy


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient that counts upstream POSTs and returns a canned
    OpenAI-shaped response."""
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.calls += 1
        return _FakeResp({
            "id": "chatcmpl-1",
            "choices": [{"message": {"role": "assistant", "content": "42"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 3},
        })


def test_repeated_request_hits_cache(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    # force a fresh cache singleton bound to the temp db
    proxy._cache_init_done = False
    proxy._cache_singleton = None
    _FakeAsyncClient.calls = 0

    client = TestClient(proxy.proxy_app)
    req = {
        "model": "deepseek-chat",
        "temperature": 0,
        "messages": [{"role": "user", "content": "what is 6 times 7"}],
    }

    r1 = client.post("/v1/chat/completions", json=req,
                     headers={"authorization": "Bearer sk-test"})
    assert r1.status_code == 200
    assert r1.json()["choices"][0]["message"]["content"] == "42"
    assert _FakeAsyncClient.calls == 1, "first call must reach upstream"

    # identical request → exact-hash hit → upstream NOT called again
    r2 = client.post("/v1/chat/completions", json=req,
                     headers={"authorization": "Bearer sk-test"})
    assert r2.status_code == 200
    assert r2.json()["choices"][0]["message"]["content"] == "42"
    assert _FakeAsyncClient.calls == 1, "repeated call must be served from cache"


def test_high_temperature_not_cached(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    proxy._cache_init_done = False
    proxy._cache_singleton = None
    _FakeAsyncClient.calls = 0

    client = TestClient(proxy.proxy_app)
    req = {
        "model": "deepseek-chat",
        "temperature": 0.9,  # intentional randomness — must never be cached
        "messages": [{"role": "user", "content": "tell me a joke"}],
    }
    client.post("/v1/chat/completions", json=req, headers={"authorization": "Bearer x"})
    client.post("/v1/chat/completions", json=req, headers={"authorization": "Bearer x"})
    assert _FakeAsyncClient.calls == 2, "high-temp calls must both reach upstream"


def test_provider_routing():
    from brevitas.proxy import (
        get_openai_compatible_upstream as route, _completions_url,
        _OPENAI_API, _MISTRAL_API, _XAI_API, _DEEPSEEK_API, _GROQ_API, _GEMINI_API,
    )
    assert route("grok-4") == _XAI_API                 # bug fix: xAI, not Groq
    assert route("grok-4.1-fast") == _XAI_API
    assert route("groq-llama") == _GROQ_API            # explicit groq- → Groq host
    assert route("deepseek-chat") == _DEEPSEEK_API
    assert route("mistral-large-latest") == _MISTRAL_API
    assert route("codestral-latest") == _MISTRAL_API
    assert route("gemini-2.5-flash") == _GEMINI_API
    assert route("gpt-4o") == _OPENAI_API
    # Google's OpenAI-compat path differs (no extra /v1); everyone else keeps /v1
    assert _completions_url(_GEMINI_API).endswith("/v1beta/openai/chat/completions")
    assert _completions_url(_OPENAI_API).endswith("/v1/chat/completions")


def test_cache_key_hint():
    from brevitas.proxy import _maybe_add_cache_key, _MISTRAL_API, _DEEPSEEK_API
    a = {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]}
    b = {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "Q2"}]}
    _maybe_add_cache_key(a, _MISTRAL_API)
    _maybe_add_cache_key(b, _MISTRAL_API)
    assert a.get("prompt_cache_key") and a["prompt_cache_key"] == b["prompt_cache_key"]
    d = {"messages": [{"role": "user", "content": "x"}]}
    _maybe_add_cache_key(d, _DEEPSEEK_API)
    assert "prompt_cache_key" not in d  # never inject unknown params into auto-cache providers


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
