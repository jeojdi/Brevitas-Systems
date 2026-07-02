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
import os
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ._compress import count_messages_tokens, report_usage
from .session import BrevitasSession
from token_efficiency_model.lossless.engine import optimize_request, record_usage
from token_efficiency_model.lossless.router import BrevitasRouter

# one router per (provider, key) — learns each session's repeat + real cache behavior
_routers: dict[str, BrevitasRouter] = {}


def _router_for(key: str, provider: str) -> BrevitasRouter:
    if key not in _routers:
        _routers[key] = BrevitasRouter(provider=provider)
    return _routers[key]

_ANTHROPIC_API = "https://api.anthropic.com"
_OPENAI_API    = "https://api.openai.com"
_DEEPSEEK_API  = "https://api.deepseek.com"
_GROQ_API      = "https://api.groq.com/openai"

# Allowlist of valid upstream URLs for SSRF protection
_ALLOWED_UPSTREAMS = {_OPENAI_API, _DEEPSEEK_API, _GROQ_API}

proxy_app = FastAPI(title="Brevitas Proxy", docs_url=None, redoc_url=None)


def get_openai_compatible_upstream(model: str, override_header: str | None = None) -> str:
    """
    Route OpenAI-compatible requests to the correct provider upstream.
    Returns base URL for the upstream API based on model name prefix or header override.

    Model routing:
    - deepseek-* → https://api.deepseek.com
    - grok-* or groq-* → https://api.groq.com/openai
    - openai models or unrecognized → https://api.openai.com

    Can be overridden with x-brevitas-upstream header (SSRF-protected: allowlist only).
    Non-allowlisted overrides are ignored; falls back to model-prefix routing.
    """
    # SSRF protection: only allow known upstream URLs
    if override_header and override_header in _ALLOWED_UPSTREAMS:
        return override_header

    model_lower = (model or "").lower()
    if model_lower.startswith("deepseek"):
        return _DEEPSEEK_API
    elif model_lower.startswith("grok") or model_lower.startswith("groq"):
        return _GROQ_API
    else:
        return _OPENAI_API

# One session per (proxy instance, provider-key) pair is fine for single-user
# local use; for multi-user, pass session_id in a custom header.
_sessions: dict[str, BrevitasSession] = {}


def _session_for(key: str) -> BrevitasSession:
    if key not in _sessions:
        _sessions[key] = BrevitasSession()
    return _sessions[key]


def parse_brevitas_headers(headers: dict) -> dict[str, str]:
    """Extract brevitas tracking labels from request headers (x-brevitas-pipeline/agent/run-id).
    Returns dict with 'pipeline', 'agent', 'run_id' keys (empty strings if not present)."""
    def _get(name: str) -> str:
        try:
            return headers.get(name, "") or ""
        except AttributeError:
            return ""
    return {
        "pipeline": _get("x-brevitas-pipeline"),
        "agent": _get("x-brevitas-agent"),
        "run_id": _get("x-brevitas-run-id"),
    }


def _auto_fleet_labels(explicit: dict, auth_key: str, body: dict) -> tuple[str, str]:
    """Auto-engage shared-prefix promotion (b9) for multi-agent fleets that DON'T send
    x-brevitas labels. OFF BY DEFAULT (BREVITAS_AUTO_SHARED_PREFIX=1 to enable).

    Why off by default: b9 REORDERS messages to hoist shared context to the front. If the
    provider is ALREADY caching the request's natural byte-identical prefix (common when a
    fleet's agents have growing-but-stable prefixes turn-over-turn), reordering CHANGES the
    prefix and DESTROYS that cache — measured on a 20-round AutoGen debate: it dropped
    DeepSeek's auto-cache from 63% to 2% and made the run 45% MORE expensive. b9 only helps
    the narrow case (a big shared block sitting behind differing prefixes with LOW natural
    repetition); applying it blindly is net-negative. So it's explicit opt-in only — the
    safe default preserves the provider's own prefix cache (cache_only passthrough)."""
    import hashlib
    if explicit.get("pipeline"):
        return explicit["pipeline"], explicit.get("agent", "")
    if os.environ.get("BREVITAS_AUTO_SHARED_PREFIX", "") not in ("1", "true", "yes"):
        return "", ""   # default: no auto b9 — never risk breaking the provider's own cache
    pipeline = (f"auto:{hashlib.sha256(auth_key.encode()).hexdigest()[:12]}"
                if auth_key else "")
    agent = explicit.get("agent")
    if not agent:
        sys_txt = ""
        sysv = body.get("system")
        if isinstance(sysv, str):
            sys_txt = sysv
        elif isinstance(sysv, list):
            sys_txt = " ".join(b.get("text", "") for b in sysv if isinstance(b, dict))
        else:
            for m in body.get("messages", []):
                if isinstance(m, dict) and m.get("role") == "system":
                    c = m.get("content", "")
                    sys_txt = c if isinstance(c, str) else " ".join(
                        b.get("text", "") for b in c if isinstance(b, dict))
                    break
        agent = f"auto:{hashlib.sha256(sys_txt.encode()).hexdigest()[:12]}" if sys_txt else "auto:default"
    return pipeline, agent


def _passthrough_mode() -> bool:
    """A/B measurement mode: BREVITAS_PASSTHROUGH=1 forwards requests completely
    untouched (no optimization) while still metering usage — the honest baseline arm
    for with/without-Brevitas comparisons."""
    return os.environ.get("BREVITAS_PASSTHROUGH", "") in ("1", "true", "yes")


def _meter(provider: str, model: str, usage: dict, labels: dict, optimized: bool) -> None:
    """Append one JSONL usage record per call when BREVITAS_METER_FILE is set.
    Works identically in passthrough and optimized modes so A/B runs are compared
    on the same instrument."""
    path = os.environ.get("BREVITAS_METER_FILE", "")
    if not path:
        return
    try:
        import time as _time
        rec = {"ts": _time.time(), "provider": provider, "model": model,
               "optimized": optimized, "usage": usage, **labels}
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass  # metering must never break the proxy


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
    router = _router_for(f"ant:{api_key}", "anthropic")
    labels = parse_brevitas_headers(request.headers)

    # Lossless auto-route: the router picks cache_only (cache_control breakpoints) vs retrieve
    # per request, based on context repetition + observed cache behavior. Never rewrites the
    # volatile message lossily; fails safe to full context.
    optimized = not _passthrough_mode()
    if optimized:
        fleet_pipe, fleet_agent = _auto_fleet_labels(labels, api_key, body)
        optimize_request(body, "anthropic", router, session.session_id,
                         pipeline=fleet_pipe, agent=fleet_agent)

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
            session.advance()
            return StreamingResponse(stream_gen(), media_type="text/event-stream")
        else:
            resp = await client.post(
                f"{_ANTHROPIC_API}/v1/messages", headers=headers, json=body
            )
            data = resp.json()
            try:
                text = data["content"][0]["text"]
                session.record_response(text)
            except (KeyError, IndexError):
                pass
            # Honest savings from REAL usage + feed cache-hit rate back to the router.
            usage = data.get("usage", {})
            if usage:
                _meter("anthropic", model, usage, labels, optimized)
                if optimized:
                    s = record_usage(usage, "anthropic", router, session.session_id)
                    report_usage("anthropic", model, int(s.uncached_cost), int(s.actual_cost), session,
                                 pipeline=labels["pipeline"], agent=labels["agent"], run_id=labels["run_id"])
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
    provider = "deepseek" if "deepseek" in (model or "").lower() else "openai"
    session = _session_for(f"oai:{auth}")
    router = _router_for(f"oai:{auth}", provider)
    labels = parse_brevitas_headers(request.headers)

    # Lossless auto-route. For OpenAI/DeepSeek the cache_only path forwards the prefix
    # byte-identical (auto-cached server-side); retrieve reduces context when the router
    # estimates it's cheaper. Volatile message never lossily rewritten; fail-safe to full.
    optimized = not _passthrough_mode()
    if optimized:
        fleet_pipe, fleet_agent = _auto_fleet_labels(labels, auth, body)
        optimize_request(body, provider, router, session.session_id,
                         pipeline=fleet_pipe, agent=fleet_agent)

    headers = _passthrough_headers(request, "openai")
    is_stream = body.get("stream", False)

    # Route to correct upstream API based on model name or header override
    override_upstream = request.headers.get("x-brevitas-upstream")
    upstream_url = get_openai_compatible_upstream(model, override_upstream)

    async with httpx.AsyncClient(timeout=120) as client:
        if is_stream:
            async def stream_gen():
                async with client.stream(
                    "POST", f"{upstream_url}/v1/chat/completions",
                    headers=headers, json=body
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            session.advance()
            return StreamingResponse(stream_gen(), media_type="text/event-stream")
        else:
            resp = await client.post(
                f"{upstream_url}/v1/chat/completions", headers=headers, json=body
            )
            data = resp.json()
            try:
                text = data["choices"][0]["message"]["content"]
                session.record_response(text)
            except (KeyError, IndexError):
                pass
            # Honest savings from REAL usage + feed cache-hit rate back to the router.
            usage = data.get("usage", {})
            if usage:
                _meter(provider, model, usage, labels, optimized)
                if optimized:
                    s = record_usage(usage, provider, router, session.session_id)
                    report_usage(provider, model, int(s.uncached_cost), int(s.actual_cost), session,
                                 pipeline=labels["pipeline"], agent=labels["agent"], run_id=labels["run_id"])
            session.advance()
            return JSONResponse(content=data, status_code=resp.status_code)


@proxy_app.get("/health")
async def proxy_health():
    return {"status": "ok", "service": "brevitas-proxy"}
