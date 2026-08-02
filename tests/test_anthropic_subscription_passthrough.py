"""Claude Pro/Max subscription (OAuth) traffic must pass through untouched.

A subscriber's Claude Code authenticates with an OAuth access token, not an API
key. That traffic has no per-token cost to optimize, and Anthropic rejects an
OAuth-authenticated request whose body was modified in flight — which is the
"API error" a subscriber hits the instant their tool is pointed at the proxy.
So the proxy must forward the body byte-for-byte while still emitting a
(zero-savings) receipt for visibility.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import brevitas.proxy as proxy


# --------------------------------------------------------------------------- #
# _is_anthropic_subscription — the auth-mode detector
# --------------------------------------------------------------------------- #

class _Req:
    def __init__(self, headers):
        self.headers = headers


@pytest.mark.parametrize("headers, expected", [
    ({"x-api-key": "sk-ant-api03-abc"}, False),                       # API key
    ({"authorization": "Bearer sk-ant-oat01-xyz"}, True),            # OAuth token
    ({"authorization": "Bearer sk-ant-api03-abc"}, False),          # API key over bearer
    ({"anthropic-beta": "oauth-2025-04-20"}, True),                 # OAuth beta flag
    # An OAuth beta flag never wins if a real API key is present (API billing).
    ({"x-api-key": "sk-ant-api03-abc", "anthropic-beta": "oauth-2025-04-20"}, False),
    ({}, False),                                                    # nothing
])
def test_subscription_detection(headers, expected):
    assert proxy._is_anthropic_subscription(_Req(headers)) is expected


# --------------------------------------------------------------------------- #
# End-to-end: the forwarded body and the receipt
# --------------------------------------------------------------------------- #

def _mock_proxy(monkeypatch, handler, reporter=None):
    real = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda *a, **k: real(
        transport=httpx.MockTransport(handler)))
    proxy._cache_init_done = True
    proxy._cache_singleton = None
    proxy.set_usage_reporter(reporter)
    monkeypatch.delenv("BREVITAS_PASSTHROUGH", raising=False)
    return TestClient(proxy.proxy_app)


_BODY = {
    "model": "claude-opus-4-8", "max_tokens": 20,
    "messages": [
        {"role": "user", "content": "draft"},
        {"role": "assistant", "content": "reviewed"},
        {"role": "user", "content": "continue"},
    ],
}


def _ok_handler(sink):
    def handler(request):
        sink.append({"body": json.loads(request.content),
                     "auth": request.headers.get("authorization", ""),
                     "beta": request.headers.get("anthropic-beta", "")})
        return httpx.Response(200, json={
            "id": "msg_ok", "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 8, "output_tokens": 1},
        })
    return handler


def test_oauth_request_is_forwarded_verbatim_and_credential_preserved(monkeypatch):
    forwarded, receipts = [], []
    client = _mock_proxy(monkeypatch, _ok_handler(forwarded),
                         reporter=lambda key, payload: receipts.append(payload))

    resp = client.post("/v1/messages", json=_BODY, headers={
        "Authorization": "Bearer sk-ant-oat01-secret",
        "anthropic-beta": "oauth-2025-04-20",
    })

    assert resp.status_code == 200
    # Body reached Anthropic untouched, and the OAuth credential + beta flag rode along.
    assert forwarded[0]["body"]["messages"] == _BODY["messages"]
    assert forwarded[0]["auth"] == "Bearer sk-ant-oat01-secret"
    assert "oauth" in forwarded[0]["beta"]
    # Still metered, but as a zero-savings passthrough (baseline == compressed).
    assert receipts, "subscription traffic must still emit a receipt"
    receipt = receipts[0]
    assert receipt["strategy"] == "subscription_passthrough"
    assert receipt["compressed_tokens"] == receipt["baseline_tokens"]


def test_api_key_request_is_not_marked_subscription(monkeypatch):
    """Control: an ordinary API-key call takes the normal optimized arm."""
    forwarded, receipts = [], []
    client = _mock_proxy(monkeypatch, _ok_handler(forwarded),
                         reporter=lambda key, payload: receipts.append(payload))

    resp = client.post("/v1/messages", json=_BODY, headers={"X-Api-Key": "sk-ant-api03-key"})

    assert resp.status_code == 200
    assert receipts and receipts[0]["strategy"] != "subscription_passthrough"
