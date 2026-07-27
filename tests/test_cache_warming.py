"""Warm-prefix extraction + proxy observation hook (predictive cache warming).

Extraction is pure (no network, no keys). The proxy-hook tests mock the upstream
like tests/test_proxy_cache.py and inject hosted request.state via a thin ASGI
wrapper, since brevitas/ never imports server code.
"""
import asyncio
import json
import os
import tempfile
import threading
import time
import types

os.environ.setdefault("BREVITAS_CACHE_DB", tempfile.mktemp(suffix=".db"))
os.environ["BREVITAS_API_KEY"] = ""  # disable billing HTTP calls (report_usage no-ops)

import httpx
from fastapi.testclient import TestClient

import brevitas.proxy as proxy
from brevitas import warming
from brevitas.warming import WarmPrefix, extract_warm_prefix

# comfortably above the claude-fable 512-token cache minimum under cl100k
_BIG = "alpha beta gamma delta epsilon zeta " * 200


def _meta(owner: str = "brevitas", tokens: int = 4096) -> dict:
    return {"cache_control_owner": owner, "cached_prefix_tokens": tokens,
            "cache_breakpoints": 1}


def _marked_body() -> dict:
    return {
        "model": "claude-fable-5",
        "tools": [{"name": "search", "description": "find things"}],
        "system": [{"type": "text", "text": "stable rules"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "big document",
                                          "cache_control": {"type": "ephemeral"}}]},
            {"role": "assistant", "content": [{"type": "text", "text": "sure"}]},
            {"role": "user", "content": [{"type": "text", "text": "volatile question"}]},
        ],
    }


# ── extraction ────────────────────────────────────────────────────────────────

def test_extract_captures_through_last_marker():
    body = _marked_body()
    prefix = extract_warm_prefix(body, "anthropic", "claude-fable-5",
                                 {"anthropic-version": "2023-06-01"}, _meta())
    assert isinstance(prefix, WarmPrefix)
    assert prefix.payload["tools"] == body["tools"]
    assert prefix.payload["system"] == body["system"]
    assert [m["role"] for m in prefix.payload["messages_prefix"]] == ["user"]
    # the marker itself must survive into the replay payload
    assert "cache_control" in prefix.payload["messages_prefix"][0]["content"][0]
    # nothing past the marked block may enter the replay payload
    serialized = json.dumps(prefix.payload)
    assert "volatile question" not in serialized and "sure" not in serialized


def test_extract_marker_in_system_excludes_messages():
    body = {
        "model": "claude-fable-5",
        "system": [{"type": "text", "text": "stable",
                    "cache_control": {"type": "ephemeral"}},
                   {"type": "text", "text": "volatile tail"}],
        "messages": [{"role": "user", "content": "question"}],
    }
    prefix = extract_warm_prefix(body, "anthropic", "claude-fable-5", {}, _meta())
    assert prefix.payload["messages_prefix"] == []
    assert len(prefix.payload["system"]) == 1
    assert "volatile tail" not in json.dumps(prefix.payload)


def test_extract_marker_boundary_inside_message_blocks():
    body = {
        "model": "claude-fable-5",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "big document",
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "volatile question"},
        ]}],
    }
    prefix = extract_warm_prefix(body, "anthropic", "claude-fable-5", {}, _meta())
    blocks = prefix.payload["messages_prefix"][0]["content"]
    assert len(blocks) == 1 and blocks[0]["text"] == "big document"


def test_extract_respects_per_model_minimums():
    meta = _meta(tokens=2048)
    assert extract_warm_prefix(_marked_body(), "anthropic", "claude-fable-5",
                               {}, meta) is not None          # min 512
    assert extract_warm_prefix(_marked_body(), "anthropic", "claude-haiku-4-5",
                               {}, meta) is None              # min 4096


def test_extract_below_minimum_returns_none():
    assert extract_warm_prefix(_marked_body(), "anthropic", "claude-fable-5",
                               {}, _meta(tokens=100)) is None


def test_extract_recomputes_tokens_for_caller_owned_markers():
    # caller-owned plans skip apply_anthropic_cache, so meta has no token count
    meta = {"cache_control_owner": "caller", "cache_breakpoints": 1}
    body = {"model": "claude-fable-5",
            "system": [{"type": "text", "text": _BIG,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "q"}]}
    prefix = extract_warm_prefix(body, "anthropic", "claude-fable-5", {}, meta)
    assert prefix is not None and prefix.prefix_tokens >= 512
    small = {"model": "claude-fable-5",
             "system": [{"type": "text", "text": "tiny",
                         "cache_control": {"type": "ephemeral"}}],
             "messages": [{"role": "user", "content": "q"}]}
    assert extract_warm_prefix(small, "anthropic", "claude-fable-5", {}, meta) is None


def test_extract_hash_varies_with_anthropic_beta():
    versioned = {"anthropic-version": "2023-06-01"}
    a = extract_warm_prefix(_marked_body(), "anthropic", "claude-fable-5",
                            versioned, _meta())
    b = extract_warm_prefix(_marked_body(), "anthropic", "claude-fable-5",
                            {**versioned, "anthropic-beta": "extended-cache-ttl-2025-04-11"},
                            _meta())
    c = extract_warm_prefix(_marked_body(), "anthropic", "claude-fable-5",
                            versioned, _meta())
    assert a.prefix_hash != b.prefix_hash, "1h-TTL beta addresses a different cache entry"
    assert a.prefix_hash == c.prefix_hash, "identical prefix + headers must be stable"


def test_extract_records_ttl_tier():
    body = _marked_body()
    body["messages"][0]["content"][0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    prefix = extract_warm_prefix(body, "anthropic", "claude-fable-5", {}, _meta())
    assert prefix.payload["ttl"] == "1h" and prefix.provider_ttl_seconds == 3600
    default = extract_warm_prefix(_marked_body(), "anthropic", "claude-fable-5", {}, _meta())
    assert default.payload["ttl"] == "" and default.provider_ttl_seconds == 300


def test_extract_requires_markers_for_anthropic():
    unmarked = {"model": "claude-fable-5",
                "messages": [{"role": "user", "content": _BIG}]}
    assert extract_warm_prefix(unmarked, "anthropic", "claude-fable-5", {}, _meta()) is None


# ── extraction: automatic-prefix-cache providers ─────────────────────────────

def test_extract_deepseek_stable_prefix_boundary():
    body = {"model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": _BIG},
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "volatile question"}]}
    prefix = extract_warm_prefix(body, "deepseek", "deepseek-v4-flash", {}, {},
                                 upstream="https://api.deepseek.com/v1/chat/completions")
    assert isinstance(prefix, WarmPrefix)
    # everything before the final (volatile) turn, nothing after it
    assert [m["role"] for m in prefix.payload["messages_prefix"]] == \
        ["system", "user", "assistant"]
    assert "volatile question" not in json.dumps(prefix.payload)
    # the worker must ping the endpoint this request actually used
    assert prefix.payload["upstream"] == "https://api.deepseek.com/v1/chat/completions"
    assert prefix.provider_ttl_seconds == 14400


def test_extract_deepseek_min_tokens_and_single_turn():
    tiny = {"model": "deepseek-v4-flash",
            "messages": [{"role": "system", "content": "short rules"},
                         {"role": "user", "content": "q"}]}
    assert extract_warm_prefix(tiny, "deepseek", "deepseek-v4-flash", {}, {}) is None
    single = {"model": "deepseek-v4-flash",
              "messages": [{"role": "user", "content": _BIG}]}
    assert extract_warm_prefix(single, "deepseek", "deepseek-v4-flash", {}, {}) is None


def test_extract_openai_prompt_cache_key_round_trip():
    upstream = "https://api.openai.com/v1/chat/completions"

    def body(key=None):
        b = {"model": "gpt-5.6",
             "messages": [{"role": "system", "content": _BIG + _BIG},
                          {"role": "user", "content": "volatile question"}]}
        if key:
            b["prompt_cache_key"] = key
        return b

    keyed = extract_warm_prefix(body("brevitas:t1:abc"), "openai", "gpt-5.6",
                                {}, {}, upstream=upstream)
    assert isinstance(keyed, WarmPrefix)
    # the replay must address the same cache entry: key rides in the payload
    assert keyed.payload["prompt_cache_key"] == "brevitas:t1:abc"
    assert keyed.payload["upstream"] == upstream
    assert keyed.provider_ttl_seconds == 1800   # explicit-mode 30m minimum lifetime
    unkeyed = extract_warm_prefix(body(), "openai", "gpt-5.6", {}, {}, upstream=upstream)
    assert "prompt_cache_key" not in unkeyed.payload
    other = extract_warm_prefix(body("brevitas:t1:zzz"), "openai", "gpt-5.6",
                                {}, {}, upstream=upstream)
    assert keyed.prefix_hash != unkeyed.prefix_hash != other.prefix_hash, \
        "a different prompt_cache_key addresses a different cache entry"


def test_extract_openai_below_minimum_returns_none():
    small = {"model": "gpt-5.6",
             "messages": [{"role": "system", "content": "stable rules"},
                          {"role": "user", "content": "q"}]}
    assert extract_warm_prefix(small, "openai", "gpt-5.6", {}, {}) is None


def test_extract_measurement_only_providers_return_none():
    # no cache at all (perplexity), a break-even 0.5x cached read (groq, fireworks
    # serverless), or an undocumented TTL/eviction policy (mistral/xai/together/
    # openrouter): the warming math cannot work, so these get measurement only.
    body = {"model": "m",
            "messages": [{"role": "system", "content": _BIG},
                         {"role": "user", "content": "q"}]}
    for provider in ("groq", "fireworks", "perplexity", "mistral", "xai",
                     "together", "openrouter"):
        assert extract_warm_prefix(body, provider, "m", {}, {}) is None


# ── proxy observation hook ────────────────────────────────────────────────────

class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAnthropicClient:
    """Stand-in for httpx.AsyncClient returning a canned Anthropic response whose
    usage reports a provider cache read."""

    def __init__(self, *a, **k):
        pass

    async def post(self, url, headers=None, json=None, content=None):
        return _FakeResp({
            "id": "msg_1",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 1,
                      "cache_read_input_tokens": 900,
                      "cache_creation_input_tokens": 0},
        })


_HOSTED_STATE = {
    "brevitas_warm_enabled": True,
    "brevitas_organization_id": "org-1",
    "brevitas_customer_id": "cust-1",
}


def _hosted_app(**overrides):
    """proxy_app behind an ASGI shim that injects the request.state the hosted
    API's middleware would set. Overriding a key to None removes it."""
    state = {k: v for k, v in {**_HOSTED_STATE, **overrides}.items() if v is not None}

    async def app(scope, receive, send):
        if scope["type"] == "http":
            scope["state"] = {**(scope.get("state") or {}), **state}
        await proxy.proxy_app(scope, receive, send)
    return app


def _warm_request() -> dict:
    # caller-owned marker: deterministic breakpoints without depending on ROI state
    return {"model": "claude-fable-5",
            "system": [{"type": "text", "text": _BIG,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "hello"}]}


def _wait_for(cond, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.02)


def _post_and_collect(monkeypatch, app, body, api_key: str) -> list:
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAnthropicClient)
    calls: list = []
    warming.set_warm_observer(
        lambda org, cust, prefix, cache_read: calls.append((org, cust, prefix, cache_read)))
    try:
        with TestClient(app) as client:
            r = client.post("/v1/messages", json=body, headers={"x-api-key": api_key})
            assert r.status_code == 200
            _wait_for(lambda: calls, timeout=2.0)
    finally:
        warming.set_warm_observer(None)
    return calls


def test_observer_fires_when_all_gates_pass(monkeypatch):
    calls = _post_and_collect(monkeypatch, _hosted_app(), _warm_request(), "warm-key-pos")
    assert len(calls) == 1
    org, cust, prefix, cache_read = calls[0]
    assert (org, cust) == ("org-1", "cust-1")
    assert isinstance(prefix, WarmPrefix)
    assert prefix.provider == "anthropic" and prefix.model == "claude-fable-5"
    assert prefix.prefix_tokens >= 512
    assert cache_read is True, "fake usage reported cache_read_input_tokens > 0"


def test_observer_skipped_without_warm_enabled(monkeypatch):
    app = _hosted_app(brevitas_warm_enabled=None)
    assert _post_and_collect(monkeypatch, app, _warm_request(), "warm-key-off") == []


def test_observer_skipped_without_customer_identity(monkeypatch):
    app = _hosted_app(brevitas_customer_id=None)
    assert _post_and_collect(monkeypatch, app, _warm_request(), "warm-key-anon") == []


def test_observer_skipped_without_breakpoints(monkeypatch):
    # below every per-model minimum and unmarked → cache_breakpoints stays 0
    tiny = {"model": "claude-fable-5",
            "messages": [{"role": "user", "content": "hi"}]}
    assert _post_and_collect(monkeypatch, _hosted_app(), tiny, "warm-key-tiny") == []


def test_observer_skipped_when_response_not_faithful(monkeypatch):
    real_optimize = proxy.optimize_request

    def _prune(body, provider, router, session_id, **kw):
        meta = real_optimize(body, provider, router, session_id, **kw)
        meta["response_faithful"] = False     # simulate retrieval having dropped context
        return meta
    monkeypatch.setattr(proxy, "optimize_request", _prune)
    assert _post_and_collect(monkeypatch, _hosted_app(), _warm_request(),
                             "warm-key-unfaithful") == []


# ── OpenAI-compat chat handler hook ───────────────────────────────────────────

class _FakeOpenAIClient:
    """Stand-in for httpx.AsyncClient returning a canned OpenAI-compatible
    (DeepSeek-shaped) response whose usage reports an automatic prefix-cache hit.
    Records forwarded bodies so tests can assert passthrough stays byte-identical."""
    bodies: list = []

    def __init__(self, *a, **k):
        pass

    async def post(self, url, headers=None, json=None, content=None):
        _FakeOpenAIClient.bodies.append(json)
        return _FakeResp({
            "id": "chatcmpl-1",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1400, "completion_tokens": 1,
                      "prompt_cache_hit_tokens": 1280,
                      "prompt_cache_miss_tokens": 120},
        })


def _chat_request() -> dict:
    return {"model": "deepseek-v4-flash",
            "messages": [{"role": "system", "content": _BIG},
                         {"role": "user", "content": "hello"}]}


def _post_chat_and_collect(monkeypatch, app, body, auth, extra_headers=None) -> list:
    monkeypatch.setattr(httpx, "AsyncClient", _FakeOpenAIClient)
    _FakeOpenAIClient.bodies = []
    calls: list = []
    warming.set_warm_observer(
        lambda org, cust, prefix, cache_read: calls.append((org, cust, prefix, cache_read)))
    try:
        with TestClient(app) as client:
            r = client.post("/v1/chat/completions", json=body,
                            headers={"authorization": f"Bearer {auth}",
                                     **(extra_headers or {})})
            assert r.status_code == 200
            _wait_for(lambda: calls, timeout=2.0)
    finally:
        warming.set_warm_observer(None)
    return calls


def test_chat_observer_fires_for_deepseek_and_never_mutates(monkeypatch):
    calls = _post_chat_and_collect(monkeypatch, _hosted_app(), _chat_request(),
                                   "warm-chat-pos")
    assert len(calls) == 1
    org, cust, prefix, cache_read = calls[0]
    assert (org, cust) == ("org-1", "cust-1")
    assert isinstance(prefix, WarmPrefix)
    assert prefix.provider == "deepseek" and prefix.model == "deepseek-v4-flash"
    assert prefix.payload["upstream"] == "https://api.deepseek.com/v1/chat/completions"
    assert [m["role"] for m in prefix.payload["messages_prefix"]] == ["system"]
    assert cache_read is True, "fake usage reported prompt_cache_hit_tokens > 0"
    # DeepSeek passthrough stays byte-identical: observation reads, never mutates
    assert _FakeOpenAIClient.bodies == [_chat_request()]


def test_chat_observer_skipped_without_warm_enabled(monkeypatch):
    app = _hosted_app(brevitas_warm_enabled=None)
    assert _post_chat_and_collect(monkeypatch, app, _chat_request(), "warm-chat-off") == []


def test_chat_observer_skipped_without_customer_identity(monkeypatch):
    app = _hosted_app(brevitas_customer_id=None)
    assert _post_chat_and_collect(monkeypatch, app, _chat_request(), "warm-chat-anon") == []


def test_chat_observer_skipped_for_unwarmable_provider(monkeypatch):
    # groq's 0.5x cached read makes a ping cost exactly what a return saves:
    # measurement only, the observer must never fire
    body = {"model": "openai/gpt-oss-120b",
            "messages": [{"role": "system", "content": _BIG},
                         {"role": "user", "content": "hello"}]}
    assert _post_chat_and_collect(monkeypatch, _hosted_app(), body, "warm-chat-groq",
                                  extra_headers={"x-brevitas-provider": "groq"}) == []


def test_chat_observer_skipped_when_response_not_faithful(monkeypatch):
    real_optimize = proxy.optimize_request

    def _prune(body, provider, router, session_id, **kw):
        meta = real_optimize(body, provider, router, session_id, **kw)
        meta["response_faithful"] = False     # simulate retrieval having dropped context
        return meta
    monkeypatch.setattr(proxy, "optimize_request", _prune)
    assert _post_chat_and_collect(monkeypatch, _hosted_app(), _chat_request(),
                                  "warm-chat-unfaithful") == []


# ── observation isolation (must never contend with the request path) ─────────

class _StateReq:
    """_observe_warm_prefix only reads request.state attributes."""

    def __init__(self):
        self.state = types.SimpleNamespace(
            brevitas_warm_enabled=True,
            brevitas_organization_id="org-1",
            brevitas_customer_id="cust-1")


def test_observe_sheds_beyond_inflight_cap(monkeypatch):
    monkeypatch.setattr(proxy, "_WARM_OBSERVE_MAX_INFLIGHT", 2)
    calls: list = []
    warming.set_warm_observer(lambda *args: calls.append(args))
    try:
        async def run():
            body = _marked_body()
            # spawned back-to-back without yielding: the cap must bound the task
            # set synchronously, before any observation gets to run
            for _ in range(10):
                proxy._observe_warm_prefix(_StateReq(), body, "claude-fable-5",
                                           _meta(), {}, cache_read=False,
                                           response_faithful=True)
            assert len(proxy._warm_tasks) == 2, "spawns beyond the cap must be shed"
            await asyncio.gather(*proxy._warm_tasks)
        asyncio.run(run())
    finally:
        warming.set_warm_observer(None)
    assert len(calls) == 2


def test_sync_observer_runs_on_dedicated_warm_executor():
    thread_names: list = []
    warming.set_warm_observer(
        lambda *args: thread_names.append(threading.current_thread().name))
    try:
        async def run():
            proxy._observe_warm_prefix(_StateReq(), _marked_body(), "claude-fable-5",
                                       _meta(), {}, cache_read=True,
                                       response_faithful=True)
            await asyncio.gather(*proxy._warm_tasks)
        asyncio.run(run())
    finally:
        warming.set_warm_observer(None)
    assert thread_names and thread_names[0].startswith("brevitas-warm-observe"), \
        "sync observers must not run on the loop's shared default executor"


# ── response-path hardening: upstream usage is untrusted JSON ─────────────────
# The cache_read arguments at the non-stream observe call sites are evaluated
# BEFORE _observe_warm_prefix's try/except, on every successful response — a
# malformed usage field must count as zero, never 500 an already-billed reply.

def test_anthropic_cached_tokens_never_raises_on_untrusted_shapes():
    f = proxy._anthropic_cached_tokens
    assert f({"cache_read_input_tokens": 900}) == 900
    assert f({"cache_read_input_tokens": "n/a"}) == 0
    assert f({"cache_read_input_tokens": {"nested": 1}}) == 0
    assert f({"cache_read_input_tokens": -7}) == 0
    assert f("not-a-dict") == 0
    assert f(None) == 0


def test_openai_cached_tokens_never_raises_on_untrusted_shapes():
    f = proxy._openai_cached_tokens
    assert f({"prompt_tokens_details": {"cached_tokens": 256}}) == 256
    assert f({"prompt_cache_hit_tokens": 1280}) == 1280
    assert f({"prompt_tokens_details": {"cached_tokens": "n/a"}}) == 0
    assert f({"prompt_tokens_details": {"cached_tokens": {"total": 64}}}) == 0
    # malformed details must still fall through to the DeepSeek field
    assert f({"prompt_tokens_details": {"cached_tokens": "n/a"},
              "prompt_cache_hit_tokens": 128}) == 128
    assert f({"prompt_tokens_details": "bogus", "prompt_cache_hit_tokens": 64}) == 64
    assert f({"prompt_cache_hit_tokens": "oops"}) == 0
    assert f({"prompt_tokens_details": {"cached_tokens": -5}}) == 0
    assert f("not-a-dict") == 0
    assert f(None) == 0


class _MalformedUsageAnthropicClient(_FakeAnthropicClient):
    async def post(self, url, headers=None, json=None, content=None):
        return _FakeResp({
            "id": "msg_bad",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 1,
                      "cache_read_input_tokens": "n/a",
                      "cache_creation_input_tokens": 0},
        })


class _MalformedUsageOpenAIClient(_FakeOpenAIClient):
    async def post(self, url, headers=None, json=None, content=None):
        _FakeOpenAIClient.bodies.append(json)
        return _FakeResp({
            "id": "chatcmpl-bad",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1400, "completion_tokens": 1,
                      "prompt_tokens_details": {"cached_tokens": {"total": 64}}},
        })


def test_nonstream_survives_malformed_anthropic_usage(monkeypatch):
    # no hosted state, no observer: the coercion runs unconditionally, so the
    # guard must hold even with warming fully disabled
    monkeypatch.setattr(httpx, "AsyncClient", _MalformedUsageAnthropicClient)
    with TestClient(proxy.proxy_app) as client:
        r = client.post("/v1/messages", json=_warm_request(),
                        headers={"x-api-key": "warm-key-bad-usage"})
    assert r.status_code == 200
    assert r.json()["content"][0]["text"] == "ok"


def test_nonstream_survives_malformed_openai_compat_usage(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _MalformedUsageOpenAIClient)
    _FakeOpenAIClient.bodies = []
    with TestClient(proxy.proxy_app) as client:
        r = client.post("/v1/chat/completions", json=_chat_request(),
                        headers={"authorization": "Bearer warm-chat-bad-usage"})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "ok"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
