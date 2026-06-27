"""
Local HTTP proxy that speaks both the Anthropic Messages API and the
OpenAI Chat Completions API.

Zero-code integration — set one env var and your existing code works:

    export ANTHROPIC_BASE_URL=http://localhost:4242
    export OPENAI_BASE_URL=http://localhost:4242/openai

Start:
    brevitas start [--port 4242] [--api-key bvt_...] [--base-url http://localhost:8000]

The proxy:
  1. Receives the request in the provider's native format
  2. Compresses the messages via the Brevitas compression API
  3. Forwards the compressed request to the real provider (preserving the
     user's API key from the original request headers)
  4. Reports usage to Brevitas for billing
  5. Returns the provider's response unchanged
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ._compress import compress_messages, count_messages_tokens, report_usage
from .session import BrevitasSession

_ANTHROPIC_API = "https://api.anthropic.com"
_OPENAI_API    = "https://api.openai.com"

proxy_app = FastAPI(title="Brevitas Proxy", docs_url=None, redoc_url=None)

# One session per (proxy instance, provider-key) pair is fine for single-user
# local use; for multi-user, pass session_id in a custom header.
_sessions: dict[str, BrevitasSession] = {}


def _session_for(key: str) -> BrevitasSession:
    if key not in _sessions:
        _sessions[key] = BrevitasSession()
    return _sessions[key]


def _passthrough_headers(request: Request, provider: str) -> dict[str, str]:
    """Extract provider auth headers from the incoming request."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if provider == "anthropic":
        for h in ("x-api-key", "anthropic-version", "anthropic-beta"):
            val = request.headers.get(h)
            if val:
                headers[h] = val
        if "anthropic-version" not in headers:
            headers["anthropic-version"] = "2023-06-01"
    else:
        auth = request.headers.get("authorization")
        if auth:
            headers["Authorization"] = auth
    return headers


# ── Anthropic: POST /v1/messages ──────────────────────────────────────────────

@proxy_app.post("/v1/messages")
async def proxy_anthropic_messages(request: Request) -> Any:
    body = await request.json()
    messages: list[dict] = body.get("messages", [])
    model: str = body.get("model", "")
    api_key = request.headers.get("x-api-key", "")
    session = _session_for(f"ant:{api_key}")

    compressed, baseline, compressed_tok = compress_messages(
        messages, session, task=body.get("system", "")
    )
    body["messages"] = compressed

    headers = _passthrough_headers(request, "anthropic")
    is_stream = body.get("stream", False)

    async with httpx.AsyncClient(timeout=120) as client:
        if is_stream:
            async def stream_gen():
                async with client.stream(
                    "POST", f"{_ANTHROPIC_API}/v1/messages",
                    headers=headers, json=body
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            report_usage("anthropic", model, baseline, compressed_tok, session)
            session.advance()
            return StreamingResponse(stream_gen(), media_type="text/event-stream")
        else:
            resp = await client.post(
                f"{_ANTHROPIC_API}/v1/messages", headers=headers, json=body
            )
            data = resp.json()
            # Record response for cross-hop context
            try:
                text = data["content"][0]["text"]
                session.record_response(text)
            except (KeyError, IndexError):
                pass
            report_usage("anthropic", model, baseline, compressed_tok, session)
            session.advance()
            return JSONResponse(content=data, status_code=resp.status_code)


# ── OpenAI: POST /v1/chat/completions ────────────────────────────────────────

@proxy_app.post("/openai/v1/chat/completions")
@proxy_app.post("/v1/chat/completions")
async def proxy_openai_chat(request: Request) -> Any:
    body = await request.json()
    messages: list[dict] = body.get("messages", [])
    model: str = body.get("model", "")
    auth = request.headers.get("authorization", "")
    session = _session_for(f"oai:{auth}")

    system_msgs = [m["content"] for m in messages if m.get("role") == "system"]
    task = system_msgs[0] if system_msgs else ""
    compressed, baseline, compressed_tok = compress_messages(
        messages, session, task=task
    )
    body["messages"] = compressed

    headers = _passthrough_headers(request, "openai")
    is_stream = body.get("stream", False)

    async with httpx.AsyncClient(timeout=120) as client:
        if is_stream:
            async def stream_gen():
                async with client.stream(
                    "POST", f"{_OPENAI_API}/v1/chat/completions",
                    headers=headers, json=body
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            report_usage("openai", model, baseline, compressed_tok, session)
            session.advance()
            return StreamingResponse(stream_gen(), media_type="text/event-stream")
        else:
            resp = await client.post(
                f"{_OPENAI_API}/v1/chat/completions", headers=headers, json=body
            )
            data = resp.json()
            try:
                text = data["choices"][0]["message"]["content"]
                session.record_response(text)
            except (KeyError, IndexError):
                pass
            report_usage("openai", model, baseline, compressed_tok, session)
            session.advance()
            return JSONResponse(content=data, status_code=resp.status_code)


@proxy_app.get("/health")
async def proxy_health():
    return {"status": "ok", "service": "brevitas-proxy"}
