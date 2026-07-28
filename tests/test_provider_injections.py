"""Metadata-only provider injections: xAI replica affinity + streamed usage.

Both were user-approved on the condition of losslessness: request metadata may
change (one header / one flag), response content never does. Verified live
2026-07-28 that xAI's cache only hits with x-grok-conv-id replica affinity.
"""
from __future__ import annotations

import json

import httpx

from tests.test_proxy_message_structure import _mock_proxy

_OK = {
    "id": "chatcmpl-1", "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 8, "completion_tokens": 1,
              "prompt_tokens_details": {"cached_tokens": 0}},
}


def _chat(client, model, *, headers=None, stream=False):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    if stream:
        body["stream"] = True
    return client.post("/v1/chat/completions", json=body,
                       headers={"Authorization": "Bearer test", **(headers or {})})


def test_xai_requests_get_stable_tenant_affinity_header(monkeypatch):
    captured = []

    def handler(request):
        captured.append(dict(request.headers))
        return httpx.Response(200, json=_OK)

    _, client = _mock_proxy(monkeypatch, handler)
    monkeypatch.setenv("BREVITAS_PROVIDER", "xai")
    assert _chat(client, "grok-4.5").status_code == 200
    assert _chat(client, "grok-4.5").status_code == 200
    first, second = captured[0]["x-grok-conv-id"], captured[1]["x-grok-conv-id"]
    # Stable per tenant (same credential -> same replica -> cache can hit),
    # opaque (a digest, not the credential), and deterministic across calls.
    assert first == second
    assert len(first) == 32 and "test" not in first


def test_caller_supplied_affinity_header_wins(monkeypatch):
    captured = []

    def handler(request):
        captured.append(dict(request.headers))
        return httpx.Response(200, json=_OK)

    _, client = _mock_proxy(monkeypatch, handler)
    monkeypatch.setenv("BREVITAS_PROVIDER", "xai")
    resp = _chat(client, "grok-4.5", headers={"x-grok-conv-id": "caller-owned"})
    assert resp.status_code == 200
    assert captured[0]["x-grok-conv-id"] == "caller-owned"


def test_non_xai_providers_get_no_affinity_header(monkeypatch):
    captured = []

    def handler(request):
        captured.append(dict(request.headers))
        return httpx.Response(200, json=_OK)

    _, client = _mock_proxy(monkeypatch, handler)
    for model in ("gpt-5.6-luna", "deepseek-chat"):
        assert _chat(client, model).status_code == 200
    assert all("x-grok-conv-id" not in h for h in captured)


def test_affinity_kill_switch(monkeypatch):
    captured = []

    def handler(request):
        captured.append(dict(request.headers))
        return httpx.Response(200, json=_OK)

    _, client = _mock_proxy(monkeypatch, handler)
    monkeypatch.setenv("BREVITAS_PROVIDER", "xai")
    monkeypatch.setenv("BREVITAS_XAI_CONV_AFFINITY", "0")
    assert _chat(client, "grok-4.5").status_code == 200
    assert "x-grok-conv-id" not in captured[0]


def _sse_response():
    chunk = {"id": "c", "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": {"content": "ok"},
                          "finish_reason": "stop"}]}
    payload = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    return httpx.Response(200, content=payload.encode(),
                          headers={"content-type": "text/event-stream"})


def test_streamed_openai_and_xai_request_usage_chunk(monkeypatch):
    forwarded = []

    def handler(request):
        forwarded.append(json.loads(request.content))
        return _sse_response()

    _, client = _mock_proxy(monkeypatch, handler)
    assert _chat(client, "gpt-5.6-luna", stream=True).status_code == 200
    monkeypatch.setenv("BREVITAS_PROVIDER", "xai")
    assert _chat(client, "grok-4.5", stream=True).status_code == 200
    assert forwarded[0]["stream_options"] == {"include_usage": True}
    assert forwarded[1]["stream_options"] == {"include_usage": True}


def test_streamed_deepseek_body_is_never_touched(monkeypatch):
    forwarded = []

    def handler(request):
        forwarded.append(json.loads(request.content))
        return _sse_response()

    _, client = _mock_proxy(monkeypatch, handler)
    assert _chat(client, "deepseek-chat", stream=True).status_code == 200
    assert "stream_options" not in forwarded[0]


def test_caller_stream_options_and_non_stream_are_untouched(monkeypatch):
    forwarded = []

    def handler(request):
        forwarded.append(json.loads(request.content))
        if request.content and b'"stream": true' in request.content.replace(b'"stream":true', b'"stream": true'):
            return _sse_response()
        return httpx.Response(200, json=_OK)

    _, client = _mock_proxy(monkeypatch, handler)
    body = {"model": "gpt-5.6-luna", "stream": True,
            "stream_options": {"include_usage": False},
            "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/chat/completions", json=body,
                       headers={"Authorization": "Bearer test"}).status_code == 200
    assert forwarded[0]["stream_options"] == {"include_usage": False}
    assert _chat(client, "gpt-5.6-luna").status_code == 200
    assert "stream_options" not in forwarded[1]


def test_stream_usage_kill_switch(monkeypatch):
    forwarded = []

    def handler(request):
        forwarded.append(json.loads(request.content))
        return _sse_response()

    _, client = _mock_proxy(monkeypatch, handler)
    monkeypatch.setenv("BREVITAS_STREAM_USAGE_INJECT", "0")
    assert _chat(client, "gpt-5.6-luna", stream=True).status_code == 200
    assert "stream_options" not in forwarded[0]
