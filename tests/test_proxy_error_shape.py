"""Gateway-origin errors on a proxy path must speak the provider's error dialect.

A client on the other end of a proxy route is an LLM SDK (Claude Code, the OpenAI
SDK, Cursor, …), not a browser. Those clients parse the provider error envelope to
surface a message and to decide retry/no-retry; FastAPI's bare {"detail": …} is an
unrecognized shape that shows up as a raw, unactionable "API error" and can kill a
running session. So every error the gateway itself generates on a proxy path must be
Anthropic-shaped (/v1/messages) or OpenAI-shaped (everything else), while the
control-plane API keeps its {"detail": …} body unchanged.
"""
from fastapi.testclient import TestClient

import api.server as server


# --------------------------------------------------------------------------
# Pure envelope mapping — deterministic, no env or store dependency.
# --------------------------------------------------------------------------

def test_anthropic_envelope_for_messages_path():
    body = server._proxy_error_content("/v1/messages", 401, "Missing X-Brevitas-Key header")
    assert body == {
        "type": "error",
        "error": {"type": "authentication_error", "message": "Missing X-Brevitas-Key header"},
    }


def test_anthropic_status_to_error_type():
    cases = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        429: "rate_limit_error",
        503: "overloaded_error",
        418: "api_error",  # unmapped status falls back to api_error
    }
    for status, etype in cases.items():
        body = server._proxy_error_content("/v1/messages", status, "x")
        assert body["error"]["type"] == etype, status


def test_openai_envelope_for_chat_completions_path():
    body = server._proxy_error_content("/v1/chat/completions", 401, "Missing X-Brevitas-Key header")
    assert body == {
        "error": {
            "message": "Missing X-Brevitas-Key header",
            "type": "invalid_request_error",
            "param": None,
            "code": "invalid_api_key",
        },
    }


def test_openai_envelope_for_prefixed_and_other_proxy_paths():
    # Azure/Bedrock and the other OpenAI-compatible routes all get the OpenAI shape.
    for path in ("/v1/embeddings", "/openai/v1/chat/completions", "/azure/x/y", "/bedrock/z"):
        body = server._proxy_error_content(path, 429, "Rate limit exceeded")
        assert "error" in body and body["error"]["type"] == "rate_limit_error", path
        assert "type" not in body, path  # not the Anthropic top-level {"type": "error"}


def test_is_proxy_path_covers_literals_and_prefixes():
    assert server._is_proxy_path("/v1/messages")
    assert server._is_proxy_path("/v1/chat/completions")
    assert server._is_proxy_path("/bedrock/model/foo/invoke")
    assert not server._is_proxy_path("/v1/organization/credits")


# --------------------------------------------------------------------------
# Integration — the running app reshapes proxy errors but not control-plane ones.
# --------------------------------------------------------------------------

def test_messages_missing_key_is_anthropic_shaped():
    client = TestClient(server.app)
    resp = client.post("/v1/messages", json={
        "model": "claude-3-5-haiku-20241022",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "ping"}],
    })
    assert resp.status_code >= 400
    body = resp.json()
    # Anthropic envelope: a top-level {"type": "error", "error": {...}}.
    assert body.get("type") == "error"
    assert set(body.get("error", {})) >= {"type", "message"}
    assert "detail" not in body


def test_chat_completions_missing_key_is_openai_shaped():
    client = TestClient(server.app)
    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "ping"}],
    })
    assert resp.status_code >= 400
    body = resp.json()
    # OpenAI envelope: a top-level {"error": {message, type, ...}}, no {"type": "error"}.
    assert "error" in body and body.get("type") != "error"
    assert set(body["error"]) >= {"message", "type"}
    assert "detail" not in body


def test_control_plane_path_keeps_detail_body():
    client = TestClient(server.app)
    resp = client.get("/v1/organization/credits")
    assert resp.status_code >= 400
    body = resp.json()
    # Unchanged FastAPI shape for the dashboard/control-plane API.
    assert "detail" in body
    assert body.get("type") != "error" and "error" not in body
