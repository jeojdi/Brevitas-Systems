"""End-to-end proxy regressions for Claude/Codex structured request bodies."""
from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient


def _mock_proxy(monkeypatch, handler):
    import brevitas.proxy as proxy

    real = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda *args, **kwargs: real(
        transport=httpx.MockTransport(handler)))
    proxy._cache_init_done = True
    proxy._cache_singleton = None
    proxy.set_usage_reporter(None)
    monkeypatch.delenv("BREVITAS_PASSTHROUGH", raising=False)
    return proxy, TestClient(proxy.proxy_app)


def test_anthropic_mid_system_body_keeps_order_through_optimized_proxy(monkeypatch):
    forwarded = []

    def handler(request):
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_ok", "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 8, "output_tokens": 1},
        })

    _, client = _mock_proxy(monkeypatch, handler)
    body = {
        "model": "claude-opus-4-8", "max_tokens": 20,
        "messages": [
            {"role": "user", "content": "draft"},
            {"role": "system", "content": "review now"},
            {"role": "assistant", "content": "reviewed"},
            {"role": "user", "content": "continue"},
        ],
    }
    response = client.post("/v1/messages", json=body, headers={
        "X-Api-Key": "test", "X-Brevitas-Pipeline": "fleet",
        "X-Brevitas-Agent": "reviewer",
    })
    assert response.status_code == 200
    assert forwarded[0]["messages"] == body["messages"]


def test_upstream_400_is_isolated_and_next_request_succeeds(monkeypatch):
    calls = []
    error = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": (
            "messages.10: role 'system' must follow a 'user' message or an "
            "'assistant' message ending in a server tool result"
        )},
    }

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(400, json=error)
        return httpx.Response(200, json={
            "id": "msg_recovered", "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        })

    _, client = _mock_proxy(monkeypatch, handler)
    headers = {"X-Api-Key": "test"}
    invalid = {"model": "claude-opus-4-8", "max_tokens": 20, "messages": [
        {"role": "assistant", "content": "answer"},
        {"role": "system", "content": "invalid here"},
        {"role": "user", "content": "next"},
    ]}
    rejected = client.post("/v1/messages", json=invalid, headers=headers)
    assert rejected.status_code == 400
    assert rejected.json() == error

    valid = {"model": "claude-opus-4-8", "max_tokens": 20,
             "messages": [{"role": "user", "content": "fresh request"}]}
    recovered = client.post("/v1/messages", json=valid, headers=headers)
    assert recovered.status_code == 200
    assert recovered.json()["id"] == "msg_recovered"
    assert len(calls) == 2


def test_responses_typed_items_are_forwarded_byte_for_byte(monkeypatch):
    forwarded = []

    def handler(request):
        forwarded.append(request.content)
        return httpx.Response(200, json={
            "id": "resp_ok", "output": [],
            "usage": {"input_tokens": 3, "output_tokens": 1},
        })

    _, client = _mock_proxy(monkeypatch, handler)
    raw = (b'{ "model": "gpt-5.6-sol", "input": ['
           b'{"type":"function_call","call_id":"c1","name":"read","arguments":"{}"},'
           b'{"type":"function_call_output","call_id":"c1","output":"ok"}'
           b'] }')
    response = client.post("/v1/responses", content=raw, headers={
        "Authorization": "Bearer test", "Content-Type": "application/json",
        "X-Brevitas-Pipeline": "codex",
    })
    assert response.status_code == 200
    assert forwarded == [raw]


def test_optimizer_exception_rolls_back_partial_chat_mutation(monkeypatch):
    forwarded = []

    def handler(request):
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chat_ok", "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        })

    proxy, client = _mock_proxy(monkeypatch, handler)

    def broken(body, *_args, **_kwargs):
        body["messages"].reverse()
        body["injected"] = "partial mutation"
        raise RuntimeError("future optimizer bug")

    monkeypatch.setattr(proxy, "optimize_request", broken)
    body = {"model": "gpt-5.6-sol", "messages": [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "question"},
    ]}
    response = client.post("/v1/chat/completions", json=body,
                           headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert forwarded == [body]


def test_optimizer_exception_rolls_back_responses_message_items(monkeypatch):
    forwarded = []

    def handler(request):
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_ok", "output": [],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        })

    proxy, client = _mock_proxy(monkeypatch, handler)

    def broken(body, *_args, **_kwargs):
        body["messages"].pop(0)
        raise ValueError("future Responses bug")

    monkeypatch.setattr(proxy, "optimize_request", broken)
    body = {"model": "gpt-5.6-sol", "input": [
        {"role": "developer", "content": "policy"},
        {"role": "user", "content": "question"},
    ]}
    response = client.post("/v1/responses", json=body,
                           headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert forwarded == [body]
