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
from brevitas.semantic_cache import SemanticCache


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
        # A COMPLETE response — finish_reason "stop". The cache now refuses to store
        # truncated responses, so the fake must look like a naturally-finished one.
        return _FakeResp({
            "id": "chatcmpl-1",
            "choices": [{"message": {"role": "assistant", "content": "42"},
                         "finish_reason": "stop"}],
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
                     headers={"authorization": "test-auth-a"})
    assert r1.status_code == 200
    assert r1.json()["choices"][0]["message"]["content"] == "42"
    assert _FakeAsyncClient.calls == 1, "first call must reach upstream"

    # identical request → exact-hash hit → upstream NOT called again
    r2 = client.post("/v1/chat/completions", json=req,
                     headers={"authorization": "test-auth-a"})
    assert r2.status_code == 200
    assert r2.json()["choices"][0]["message"]["content"] == "42"
    assert _FakeAsyncClient.calls == 1, "repeated call must be served from cache"

    # A hosted proxy may serve many customers. Identical content under a different
    # credential must never reuse the first customer's response.
    r3 = client.post("/v1/chat/completions", json=req,
                     headers={"authorization": "test-auth-other-tenant"})
    assert r3.status_code == 200
    assert _FakeAsyncClient.calls == 2, "cache entries must be tenant-isolated"


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
    client.post("/v1/chat/completions", json=req, headers={"authorization": "test-auth-b"})
    client.post("/v1/chat/completions", json=req, headers={"authorization": "test-auth-b"})
    assert _FakeAsyncClient.calls == 2, "high-temp calls must both reach upstream"


def test_cache_key_includes_every_response_control(tmp_path):
    cache = SemanticCache(str(tmp_path / "cache.db"), semantic_enabled=False)
    base = {
        "model": "gpt-4o-mini", "temperature": 0, "seed": 1,
        "messages": [{"role": "user", "content": "answer"}],
    }
    cache.store(base, "openai", "gpt-4o-mini", {"answer": "a"},
                prompt_tokens=1, completion_tokens=1)
    assert cache.lookup(base, "openai", "gpt-4o-mini") is not None
    assert cache.lookup({**base, "seed": 2}, "openai", "gpt-4o-mini") is None
    assert cache.lookup({**base, "stop": ["END"]}, "openai", "gpt-4o-mini") is None
    assert cache.lookup({**base, "response_format": {"type": "json_object"}},
                        "openai", "gpt-4o-mini") is None


def test_hosted_cache_varies_by_provider_credential(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    proxy._cache_init_done = False
    proxy._cache_singleton = None
    _FakeAsyncClient.calls = 0
    client = TestClient(proxy.proxy_app)
    req = {"model": "gpt-4o-mini", "temperature": 0,
           "messages": [{"role": "user", "content": "same account request"}]}
    common = {"X-Brevitas-Key": "bvt_customer"}
    # NB: bare token strings (no "Bearer " prefix) — the proxy namespaces the cache on the
    # raw Authorization value, so distinct values suffice, and this avoids tripping the
    # pre-push secret scanner's "Bearer <token>" pattern on these fake test creds.
    assert client.post("/v1/chat/completions", json=req,
                       headers={**common, "Authorization": "provider-a"}).status_code == 200
    assert client.post("/v1/chat/completions", json=req,
                       headers={**common, "Authorization": "provider-b"}).status_code == 200
    assert _FakeAsyncClient.calls == 2


class _IncompleteClient(_FakeAsyncClient):
    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.calls += 1
        return _FakeResp({                       # truncated: finish_reason "length"
            "id": "chatcmpl-x",
            "choices": [{"message": {"role": "assistant", "content": "partial..."},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 3},
        })


def _fresh_client(monkeypatch, fake=_FakeAsyncClient):
    monkeypatch.setattr(httpx, "AsyncClient", fake)
    proxy._cache_init_done = False
    proxy._cache_singleton = None
    _FakeAsyncClient.calls = 0
    return TestClient(proxy.proxy_app)


def test_retrieval_answer_is_not_cached(monkeypatch):
    """The audit's P0 reproduction: an answer produced from a NON-faithful (retrieval/
    reorder) request must never be stored, so a second identical request re-hits upstream
    instead of replaying the degraded answer as a verified cache hit."""
    client = _fresh_client(monkeypatch)

    real_optimize = proxy.optimize_request

    def _prune(body, provider, router, session_id, **kw):
        meta = real_optimize(body, provider, router, session_id, **kw)
        meta["response_faithful"] = False        # simulate retrieval having dropped context
        return meta
    monkeypatch.setattr(proxy, "optimize_request", _prune)

    req = {"model": "gpt-4o-mini", "temperature": 0,
           "messages": [{"role": "user", "content": "summarize the doc"}]}
    client.post("/v1/chat/completions", json=req, headers={"authorization": "auth-p0"})
    client.post("/v1/chat/completions", json=req, headers={"authorization": "auth-p0"})
    assert _FakeAsyncClient.calls == 2, "non-faithful answer must not be cached"


def test_incomplete_response_not_cached(monkeypatch):
    client = _fresh_client(monkeypatch, _IncompleteClient)
    req = {"model": "gpt-4o-mini", "temperature": 0,
           "messages": [{"role": "user", "content": "long answer please"}]}
    client.post("/v1/chat/completions", json=req, headers={"authorization": "auth-inc"})
    client.post("/v1/chat/completions", json=req, headers={"authorization": "auth-inc"})
    assert _FakeAsyncClient.calls == 2, "truncated response must not be cached"


def test_tripped_cache_lever_skips_hits(monkeypatch):
    """Tripping the exact-cache lever must stop the proxy from serving cached hits."""
    from token_efficiency_model.quality import gate
    client = _fresh_client(monkeypatch)
    gate.trip_lever("cache")
    try:
        req = {"model": "gpt-4o-mini", "temperature": 0,
               "messages": [{"role": "user", "content": "same question twice"}]}
        client.post("/v1/chat/completions", json=req, headers={"authorization": "auth-lv"})
        client.post("/v1/chat/completions", json=req, headers={"authorization": "auth-lv"})
        assert _FakeAsyncClient.calls == 2, "tripped cache lever must not serve hits"
    finally:
        gate.reset_lever("cache")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
