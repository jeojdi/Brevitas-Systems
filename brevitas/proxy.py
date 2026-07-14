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
import time
import asyncio
import inspect
import uuid
from copy import deepcopy
from typing import Any, Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ._compress import count_messages_tokens, report_usage
from .labels import _git_root_name
from .receipts import SSEUsageParser, TokenReceipt, count_request_tokens, normalize_usage
from .session import BrevitasSession
from token_efficiency_model.lossless import state_store
from token_efficiency_model.lossless.batch_group import BatchGroupGate
from token_efficiency_model.lossless.engine import optimize_request, record_usage
from token_efficiency_model.lossless.router import BrevitasRouter

# Batch prefix grouping (pathfinder gate, CR1): concurrent same-prefix requests wait
# for the first one's prefill to write the provider cache, then read it instead of all
# re-writing the same bytes. Only ever holds a request whose identical-prefix sibling
# is ALREADY in flight (bounded by max_wait), so interactive traffic is never touched.
# On by default; BREVITAS_BATCH_GROUP=0 is the kill-switch.
_BG_ON = os.environ.get("BREVITAS_BATCH_GROUP", "1") not in ("0", "false", "no")
_bg = BatchGroupGate(max_wait=float(os.environ.get("BREVITAS_BATCH_GROUP_MAX_WAIT", "15")))
_BG_WARM = {"deepseek": 2880.0, "anthropic": 240.0, "openai": 240.0}  # ~0.8x cache TTL

# one router per (provider, key) — learns each session's repeat + real cache behavior
_routers: dict[str, BrevitasRouter] = {}


def _router_for(key: str, provider: str) -> BrevitasRouter:
    if key not in _routers:
        _routers[key] = BrevitasRouter(provider=provider)
    return _routers[key]


# ── cross-run state persistence (lossless — decision state only, content-free) ──
# BREVITAS_STATE_FILE=<path> makes learned routing state (LCP fingerprints, observed
# cache rates, b9 promotion/locks, tokenizer ratios) survive proxy restarts, so run
# N+1 of the same pipeline is recognized instead of relearned. Debounced writes off
# the request path; corrupt/missing files fail safe to a cold start.
_STATE_FILE = os.environ.get("BREVITAS_STATE_FILE", "")
_STATE_EVERY_S = 5.0
_state_last_save = 0.0

def _key_id(secret: str) -> str:
    """Registry/session identity for an auth secret: a short hash, NEVER the raw key —
    these identities are persisted to the cross-run state file."""
    import hashlib
    return hashlib.sha256((secret or "").encode()).hexdigest()[:16]


if _STATE_FILE:
    _restored = state_store.load(_STATE_FILE, _routers,
                                 lambda prov: BrevitasRouter(provider=prov))
    if _restored:
        print(f"[brevitas] cross-run state: restored {_restored} sessions from {_STATE_FILE}",
              flush=True)


def _state_save() -> None:
    global _state_last_save
    if not _STATE_FILE:
        return
    now = time.time()
    if now - _state_last_save >= _STATE_EVERY_S:
        _state_last_save = now
        state_store.save(_STATE_FILE, _routers)

_ANTHROPIC_API = "https://api.anthropic.com"
_UPSTREAMS = {
    "openai": "https://api.openai.com",
    "deepseek": "https://api.deepseek.com",
    "groq": "https://api.groq.com/openai",
    "xai": "https://api.x.ai",
    "mistral": "https://api.mistral.ai",
    "together": "https://api.together.xyz",
    "fireworks": "https://api.fireworks.ai/inference",
    "openrouter": "https://openrouter.ai/api",
    "perplexity": "https://api.perplexity.ai",
}
_CHAT_ENDPOINTS = {provider: f"{base}/v1/chat/completions" for provider, base in _UPSTREAMS.items()}
_CHAT_ENDPOINTS["perplexity"] = "https://api.perplexity.ai/chat/completions"
_ALLOWED_UPSTREAMS = set(_UPSTREAMS.values())

_usage_reporter: Callable | None = None


def set_usage_reporter(callback: Callable | None) -> None:
    """Install the hosted API's in-process receipt writer; local proxy uses HTTP."""
    global _usage_reporter
    _usage_reporter = callback


# ── Semantic response cache ───────────────────────────────────────────────────
# On a hit we return the stored provider response verbatim and skip the upstream call
# (and all optimization) entirely — 100% savings on that call. This is distinct from
# the router/engine's provider-native prompt caching (which discounts a call that still
# happens); the semantic cache eliminates the call. Lazy singleton; any failure disables
# it silently so the cache can NEVER break a customer's pipeline.
# Toggle: BREVITAS_CACHE_ENABLED=false.
_cache_singleton: Any = None
_cache_init_done = False


def _get_cache():
    global _cache_singleton, _cache_init_done
    if _cache_init_done:
        return _cache_singleton
    _cache_init_done = True
    if os.getenv("BREVITAS_CACHE_ENABLED", "true").lower() == "false":
        _cache_singleton = None
        return None
    try:
        from .semantic_cache import make_semantic_cache
        _cache_singleton = make_semantic_cache()
    except Exception:
        _cache_singleton = None
    return _cache_singleton


def _usage_tokens(data: dict, provider: str) -> tuple[int, int]:
    """(prompt_tokens, completion_tokens) from a provider response usage object."""
    u = (data or {}).get("usage", {}) or {}
    if provider == "anthropic":
        prompt = int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0)) \
            + int(u.get("cache_creation_input_tokens", 0))
        return prompt, int(u.get("output_tokens", 0))
    return int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))


def _cache_lookup(cache, body: dict, provider: str, model: str):
    try:
        return cache.lookup(body, provider, model)
    except Exception:
        return None


def _cache_store(cache, body: dict, provider: str, model: str, data: dict) -> None:
    try:
        p, c = _usage_tokens(data, provider)
        cache.store(body, provider, model, data, prompt_tokens=p, completion_tokens=c)
    except Exception:
        pass


async def _report_cache_hit(request: Request, provider: str, model: str, hit,
                            session: BrevitasSession, labels: dict) -> None:
    """A cache hit avoided both the recorded prompt and completion costs."""
    baseline = int(hit.prompt_tokens) + int(hit.completion_tokens)
    if baseline > 0:
        session.last_quality = float(getattr(hit, "similarity", 1.0))
        await _emit_usage(request, {"provider": provider, "model": model,
            "operation": "chat", "baseline_tokens": int(hit.prompt_tokens),
            "baseline_output_tokens": int(hit.completion_tokens), "compressed_tokens": 0,
            "fresh_input_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0,
            "output_tokens": 0, "quality_score": session.last_quality,
            "request_id": _request_id(request), "strategy": "semantic_cache",
            "session_id": session.session_id, "receipt_source": "proxy", **labels})


proxy_app = FastAPI(title="Brevitas Proxy", docs_url=None, redoc_url=None)


def _provider_for(model: str, explicit: str = "") -> str:
    if explicit in _UPSTREAMS:
        return explicit
    value = (model or "").lower()
    if value.startswith("deepseek"):
        return "deepseek"
    if value.startswith(("grok", "xai")):
        return "xai"
    if value.startswith("mistral") or value.startswith("codestral"):
        return "mistral"
    return "openai"


def _request_id(request: Request, provider_id: str = "") -> str:
    return (request.headers.get("x-brevitas-request-id")
            or request.headers.get("x-client-request-id")
            or provider_id or uuid.uuid4().hex)


async def _emit_usage(request: Request, payload: dict) -> None:
    """Best effort only: reporting errors never alter the provider response."""
    try:
        if _usage_reporter is not None:
            key = request.headers.get("x-brevitas-key", "")
            if inspect.iscoroutinefunction(_usage_reporter):
                await _usage_reporter(key, payload)
            else:
                await asyncio.to_thread(_usage_reporter, key, payload)
            return
        session = _session_for(payload.get("session_id") or "usage")
        session.last_quality = payload.get("quality_score")
        await asyncio.to_thread(
            report_usage, payload.get("provider", ""), payload.get("model", ""),
            payload.get("baseline_tokens", 0), payload.get("compressed_tokens", 0), session,
            payload.get("pipeline", ""), payload.get("agent", ""), payload.get("run_id", ""),
            payload.get("usage_raw"), payload.get("strategy", ""), payload,
        )
    except Exception:
        pass


def _router_usage(receipt: TokenReceipt, provider: str) -> dict:
    if provider == "anthropic":
        return {"input_tokens": receipt.fresh_input_tokens,
                "cache_read_input_tokens": receipt.cached_input_tokens,
                "cache_creation_input_tokens": receipt.cache_write_tokens,
                "output_tokens": receipt.output_tokens}
    return {"prompt_tokens": receipt.input_tokens,
            "prompt_tokens_details": {"cached_tokens": receipt.cached_input_tokens},
            "completion_tokens": receipt.output_tokens}


async def _record_receipt(request: Request, provider: str, model: str, operation: str,
                          baseline: int, receipt: TokenReceipt, session: BrevitasSession,
                          router: BrevitasRouter, labels: dict, optimized: bool,
                          response_id: str = "", strategy: str = "native_cache",
                          fleet_pipe: str = "") -> None:
    has_receipt = receipt.total_tokens > 0
    usage = _router_usage(receipt, provider) if has_receipt else {}
    if has_receipt:
        _meter(provider, model, usage, labels, optimized)
    if optimized and has_receipt:
        record_usage(usage, provider, router, session.session_id,
                     pipeline=fleet_pipe, model=model)
        _state_save()
    receipt_fields = receipt.as_dict() if has_receipt else {}
    await _emit_usage(request, {
        "provider": provider, "model": model, "operation": operation,
        "baseline_tokens": baseline,
        "compressed_tokens": receipt.input_tokens if has_receipt else baseline,
        **receipt_fields, "quality_score": session.last_quality,
        "receipt_available": has_receipt,
        "request_id": _request_id(request, response_id),
        "strategy": (strategy if has_receipt else f"{strategy}:missing_receipt")[:64],
        "session_id": session.session_id, "receipt_source": "proxy", **labels,
    })


def _response_headers(response: httpx.Response) -> dict[str, str]:
    keep = {"content-type", "request-id", "x-request-id", "openai-request-id",
            "retry-after", "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
            "x-ratelimit-reset-requests"}
    headers = getattr(response, "headers", {}) or {}
    return {key: value for key, value in headers.items() if key.lower() in keep}


def _response_content(response: httpx.Response, parsed: dict) -> bytes:
    content = getattr(response, "content", None)
    return content if isinstance(content, bytes) else json.dumps(parsed, separators=(",", ":")).encode()


def get_openai_compatible_upstream(model: str, override_header: str | None = None,
                                   provider: str = "") -> str:
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

    return _UPSTREAMS[_provider_for(model, provider)]

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
    project = (_get("x-brevitas-project") or _get("x-brevitas-repo")
               or os.getenv("BREVITAS_PROJECT") or os.getenv("BREVITAS_REPO")
               or _git_root_name())
    source = (_get("x-brevitas-source") or _get("x-brevitas-client")
              or os.getenv("BREVITAS_SOURCE") or os.getenv("BREVITAS_CLIENT")
              or "proxy")
    return {
        "project": project, "repo": project,
        "environment": (_get("x-brevitas-environment")
                        or os.getenv("BREVITAS_ENVIRONMENT", "")),
        "source": source, "client": source,
        "pipeline": _get("x-brevitas-pipeline"), "agent": _get("x-brevitas-agent"),
        "call_site_id": _get("x-brevitas-call-site"),
        "framework": _get("x-brevitas-framework"), "gateway": _get("x-brevitas-gateway"),
        "run_id": _get("x-brevitas-run-id"),
    }


def _agent_label(explicit: dict, body: dict) -> str:
    """Stable per-agent label: the explicit x-brevitas-agent header, else a hash of the
    system prompt (an agent's identity in every framework we've measured). Used to key
    router SESSIONS per agent — a fleet shares one API key, and mixing agents in one
    session corrupts the LCP repeat-detection and observed-cache stats that the router
    and the b9 gate consume (agent B's request never prefix-matches agent A's).
    Single-agent flows produce one constant label, so their behavior is unchanged."""
    import hashlib
    if explicit.get("agent"):
        return explicit["agent"]
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
    return f"auto:{hashlib.sha256(sys_txt.encode()).hexdigest()[:12]}" if sys_txt else "auto:default"


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
    return pipeline, _agent_label(explicit, body)


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
        forward = {"authorization", "openai-organization", "openai-project",
                   "openai-beta", "idempotency-key", "http-referer", "x-title",
                   "x-client-request-id"}
        for name, value in request.headers.items():
            lower = name.lower()
            if lower in forward or lower.startswith("x-stainless-"):
                headers[name] = value
    return headers


# ── Anthropic: POST /v1/messages ──────────────────────────────────────────────

@proxy_app.post("/v1/messages")
async def proxy_anthropic_messages(request: Request) -> Any:
    body = await request.json()
    model: str = body.get("model", "")
    api_key = request.headers.get("x-api-key", "")
    labels = parse_brevitas_headers(request.headers)
    baseline = count_request_tokens(body, "messages")
    identity = request.headers.get("x-brevitas-key") or api_key
    sess_key = f"ant:{_key_id(identity)}:{_agent_label(labels, body)}"
    session = _session_for(sess_key)
    router = _router_for(sess_key, "anthropic")

    cache = _get_cache()
    cache_body = deepcopy(body) if cache is not None else None
    if cache_body is not None:
        cache_body["_brevitas_cache_namespace"] = _key_id(identity)
    if cache is not None:
        hit = _cache_lookup(cache, cache_body, "anthropic", model)
        if hit is not None:
            await _report_cache_hit(request, "anthropic", model, hit, session, labels)
            session.advance()
            return JSONResponse(content=hit.response, status_code=200)

    optimized = not _passthrough_mode()
    fleet_pipe, fleet_agent = _auto_fleet_labels(labels, api_key, body)
    strategy = "passthrough"
    if optimized:
        meta = optimize_request(body, "anthropic", router, session.session_id,
                                pipeline=fleet_pipe, agent=fleet_agent)
        strategy = meta.get("strategy", "native_cache")

    bg_sig, bg_role = None, "free"
    if optimized and _BG_ON:
        bg_sig = _bg.signature(body)
        if bg_sig:
            bg_role, _bg_waited = await _bg.acquire(bg_sig)

    headers = _passthrough_headers(request, "anthropic")
    is_stream = body.get("stream", False)
    endpoint = f"{_ANTHROPIC_API}/v1/messages"
    if is_stream:
        client = httpx.AsyncClient(timeout=120)
        upstream = await client.send(
            client.build_request("POST", endpoint, headers=headers, json=body), stream=True
        )
        if upstream.status_code >= 400:
            content = await upstream.aread()
            response_headers = _response_headers(upstream)
            await upstream.aclose()
            await client.aclose()
            if bg_role == "pathfinder":
                _bg.release(bg_sig, _BG_WARM["anthropic"])
            return Response(content=content, status_code=upstream.status_code,
                            headers=response_headers)
        parser = SSEUsageParser("anthropic")

        async def stream_gen():
            released = False
            completed = False
            try:
                async for chunk in upstream.aiter_bytes():
                    parser.feed(chunk)
                    if not released and bg_role == "pathfinder":
                        _bg.release(bg_sig, _BG_WARM["anthropic"])
                        released = True
                    yield chunk
                completed = True
            finally:
                await upstream.aclose()
                await client.aclose()
                if bg_role == "pathfinder" and not released:
                    _bg.release(bg_sig, _BG_WARM["anthropic"])
                if completed:
                    await _record_receipt(
                        request, "anthropic", model, "messages", baseline, parser.finish(),
                        session, router, labels, optimized, parser.response_id, strategy, fleet_pipe,
                    )
                    session.advance()

        return StreamingResponse(stream_gen(), status_code=upstream.status_code,
                                 headers=_response_headers(upstream), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=120) as client:
        try:
            upstream = await client.post(endpoint, headers=headers, json=body)
        finally:
            if bg_role == "pathfinder":
                _bg.release(bg_sig, _BG_WARM["anthropic"])
    try:
        data = upstream.json()
    except Exception:
        data = {}
    try:
        session.record_response(data["content"][0]["text"])
    except (KeyError, IndexError, TypeError):
        pass
    if cache is not None and upstream.status_code == 200 and data:
        _cache_store(cache, cache_body, "anthropic", model, data)
    await _record_receipt(
        request, "anthropic", model, "messages", baseline,
        normalize_usage(data.get("usage"), "anthropic"), session, router, labels,
        optimized, str(data.get("id") or ""), strategy, fleet_pipe,
    )
    session.advance()
    return Response(content=_response_content(upstream, data), status_code=upstream.status_code,
                    headers=_response_headers(upstream))


# ── OpenAI: POST /v1/chat/completions ────────────────────────────────────────

@proxy_app.post("/openai/v1/chat/completions")
@proxy_app.post("/openai/chat/completions")
@proxy_app.post("/v1/chat/completions")
async def proxy_openai_chat(request: Request) -> Any:
    body = await request.json()
    model: str = body.get("model", "")
    auth = request.headers.get("authorization", "")
    provider = _provider_for(model, request.headers.get("x-brevitas-provider", ""))
    labels = parse_brevitas_headers(request.headers)
    labels["gateway"] = labels.get("gateway") or (provider if provider == "openrouter" else "")
    baseline = count_request_tokens(body, "chat.completions")
    identity = request.headers.get("x-brevitas-key") or auth
    sess_key = f"oai:{_key_id(identity)}:{_agent_label(labels, body)}"
    session = _session_for(sess_key)
    router = _router_for(sess_key, provider)

    # Semantic cache: key on the ORIGINAL request; model_id already isolates per model.
    cache = _get_cache()
    cache_body = deepcopy(body) if cache is not None else None
    if cache_body is not None:
        cache_body["_brevitas_cache_namespace"] = _key_id(identity)
    if cache is not None:
        hit = _cache_lookup(cache, cache_body, provider, model)
        if hit is not None:
            await _report_cache_hit(request, provider, model, hit, session, labels)
            session.advance()
            return JSONResponse(content=hit.response, status_code=200)

    # Lossless auto-route. For OpenAI/DeepSeek the cache_only path forwards the prefix
    # byte-identical (auto-cached server-side); retrieve reduces context when the router
    # estimates it's cheaper. Volatile message never lossily rewritten; fail-safe to full.
    optimized = not _passthrough_mode()
    fleet_pipe, fleet_agent = _auto_fleet_labels(labels, auth, body)
    strategy = "passthrough"
    if optimized:
        meta = optimize_request(body, provider, router, session.session_id,
                                pipeline=fleet_pipe, agent=fleet_agent)
        strategy = meta.get("strategy", "native_cache")

    # pathfinder gate — signature computed AFTER optimization (the bytes actually sent)
    bg_sig, bg_role = None, "free"
    if optimized and _BG_ON:
        bg_sig = _bg.signature(body)
        if bg_sig:
            bg_role, _bg_waited = await _bg.acquire(bg_sig)
    bg_warm = _BG_WARM.get(provider, 240.0)

    headers = _passthrough_headers(request, "openai")
    is_stream = body.get("stream", False)
    if is_stream and provider in ("openai", "deepseek"):
        body.setdefault("stream_options", {}).setdefault("include_usage", True)

    override_upstream = request.headers.get("x-brevitas-upstream")
    upstream_base = get_openai_compatible_upstream(model, override_upstream, provider)
    endpoint = _CHAT_ENDPOINTS.get(provider, f"{upstream_base.rstrip('/')}/v1/chat/completions")
    if override_upstream:
        endpoint = f"{upstream_base.rstrip('/')}/v1/chat/completions"
    if is_stream:
        client = httpx.AsyncClient(timeout=120)
        upstream = await client.send(
            client.build_request("POST", endpoint, headers=headers, json=body), stream=True
        )
        if upstream.status_code >= 400:
            content = await upstream.aread()
            response_headers = _response_headers(upstream)
            await upstream.aclose()
            await client.aclose()
            if bg_role == "pathfinder":
                _bg.release(bg_sig, bg_warm)
            return Response(content=content, status_code=upstream.status_code,
                            headers=response_headers)
        parser = SSEUsageParser(provider)

        async def stream_gen():
            released = False
            completed = False
            try:
                async for chunk in upstream.aiter_bytes():
                    parser.feed(chunk)
                    if not released and bg_role == "pathfinder":
                        _bg.release(bg_sig, bg_warm)
                        released = True
                    yield chunk
                completed = True
            finally:
                await upstream.aclose()
                await client.aclose()
                if bg_role == "pathfinder" and not released:
                    _bg.release(bg_sig, bg_warm)
                if completed:
                    await _record_receipt(
                        request, provider, model, "chat.completions", baseline, parser.finish(),
                        session, router, labels, optimized, parser.response_id, strategy, fleet_pipe,
                    )
                    session.advance()

        return StreamingResponse(stream_gen(), status_code=upstream.status_code,
                                 headers=_response_headers(upstream), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=120) as client:
        try:
            upstream = await client.post(endpoint, headers=headers, json=body)
        finally:
            if bg_role == "pathfinder":
                _bg.release(bg_sig, bg_warm)
    try:
        data = upstream.json()
    except Exception:
        data = {}
    try:
        session.record_response(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        pass
    if cache is not None and upstream.status_code == 200 and data:
        _cache_store(cache, cache_body, provider, model, data)
    await _record_receipt(
        request, provider, model, "chat.completions", baseline,
        normalize_usage(data.get("usage"), provider), session, router, labels,
        optimized, str(data.get("id") or ""), strategy, fleet_pipe,
    )
    session.advance()
    return Response(content=_response_content(upstream, data), status_code=upstream.status_code,
                    headers=_response_headers(upstream))


# ── OpenAI Responses API (Codex and compatible clients) ─────────────────────

@proxy_app.post("/openai/v1/responses")
@proxy_app.post("/openai/responses")
@proxy_app.post("/v1/responses")
async def proxy_openai_responses(request: Request) -> Any:
    raw_body = await request.body()
    body = json.loads(raw_body)
    model = str(body.get("model") or "")
    provider = _provider_for(model, request.headers.get("x-brevitas-provider", ""))
    labels = parse_brevitas_headers(request.headers)
    baseline = count_request_tokens(body, "responses")
    auth = request.headers.get("authorization", "")
    identity = request.headers.get("x-brevitas-key") or auth
    sess_key = f"responses:{_key_id(identity)}:{_agent_label(labels, body)}"
    session = _session_for(sess_key)
    router = _router_for(sess_key, provider)
    optimized = False
    body_changed = False
    strategy = "passthrough"

    response_input = body.get("input")
    if not _passthrough_mode() and isinstance(response_input, list) and all(
        isinstance(item, dict) and "role" in item for item in response_input
    ):
        temporary = {"model": model, "messages": deepcopy(response_input)}
        if body.get("instructions"):
            temporary["system"] = body["instructions"]
        meta = optimize_request(temporary, provider, router, session.session_id,
                                pipeline=labels.get("pipeline", ""),
                                agent=labels.get("agent", ""))
        body["input"] = temporary["messages"]
        body_changed = body["input"] != response_input
        optimized = body_changed
        strategy = meta.get("strategy", "native_cache")

    base = get_openai_compatible_upstream(
        model, request.headers.get("x-brevitas-upstream"), provider
    )
    endpoint = f"{base.rstrip('/')}/v1/responses"
    headers = _passthrough_headers(request, "openai")
    is_stream = bool(body.get("stream"))
    if is_stream:
        client = httpx.AsyncClient(timeout=120)
        request_body = {"json": body} if body_changed else {"content": raw_body}
        upstream = await client.send(
            client.build_request("POST", endpoint, headers=headers, **request_body), stream=True
        )
        if upstream.status_code >= 400:
            content = await upstream.aread()
            response_headers = _response_headers(upstream)
            await upstream.aclose()
            await client.aclose()
            return Response(content=content, status_code=upstream.status_code,
                            headers=response_headers)
        parser = SSEUsageParser(provider)

        async def stream_gen():
            completed = False
            try:
                async for chunk in upstream.aiter_bytes():
                    parser.feed(chunk)
                    yield chunk
                completed = True
            finally:
                await upstream.aclose()
                await client.aclose()
                if completed:
                    await _record_receipt(
                        request, provider, model, "responses", baseline, parser.finish(),
                        session, router, labels, optimized, parser.response_id, strategy,
                        labels.get("pipeline", ""),
                    )
                    session.advance()

        return StreamingResponse(stream_gen(), status_code=upstream.status_code,
                                 headers=_response_headers(upstream), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=120) as client:
        request_body = {"json": body} if body_changed else {"content": raw_body}
        upstream = await client.post(endpoint, headers=headers, **request_body)
    try:
        data = upstream.json()
    except Exception:
        data = {}
    await _record_receipt(
        request, provider, model, "responses", baseline,
        normalize_usage(data.get("usage"), provider), session, router, labels,
        optimized, str(data.get("id") or ""), strategy, labels.get("pipeline", ""),
    )
    session.advance()
    return Response(content=_response_content(upstream, data), status_code=upstream.status_code,
                    headers=_response_headers(upstream))


async def _proxy_openai_plain(request: Request, operation: str) -> Any:
    """Meter OpenAI-compatible endpoints that do not need message optimization."""
    body = await request.json()
    model = str(body.get("model") or "")
    provider = _provider_for(model, request.headers.get("x-brevitas-provider", ""))
    labels = parse_brevitas_headers(request.headers)
    baseline = count_request_tokens(body, operation)
    identity = request.headers.get("x-brevitas-key") or request.headers.get("authorization", "")
    sess_key = f"{operation}:{_key_id(identity)}"
    session, router = _session_for(sess_key), _router_for(sess_key, provider)
    base = get_openai_compatible_upstream(
        model, request.headers.get("x-brevitas-upstream"), provider
    )
    endpoint = f"{base.rstrip('/')}/v1/{operation}"
    headers = _passthrough_headers(request, "openai")
    if body.get("stream"):
        client = httpx.AsyncClient(timeout=120)
        upstream = await client.send(
            client.build_request("POST", endpoint, headers=headers, json=body), stream=True
        )
        if upstream.status_code >= 400:
            content = await upstream.aread()
            response_headers = _response_headers(upstream)
            await upstream.aclose(); await client.aclose()
            return Response(content=content, status_code=upstream.status_code,
                            headers=response_headers)
        parser = SSEUsageParser(provider)

        async def stream_gen():
            completed = False
            try:
                async for chunk in upstream.aiter_bytes():
                    parser.feed(chunk)
                    yield chunk
                completed = True
            finally:
                await upstream.aclose(); await client.aclose()
                if completed:
                    await _record_receipt(request, provider, model, operation, baseline,
                        parser.finish(), session, router, labels, False, parser.response_id,
                        "passthrough", labels.get("pipeline", ""))
                    session.advance()
        return StreamingResponse(stream_gen(), status_code=upstream.status_code,
                                 headers=_response_headers(upstream), media_type="text/event-stream")
    async with httpx.AsyncClient(timeout=120) as client:
        upstream = await client.post(endpoint, headers=headers, json=body)
    try:
        data = upstream.json()
    except Exception:
        data = {}
    await _record_receipt(
        request, provider, model, operation, baseline,
        normalize_usage(data.get("usage"), provider), session, router, labels,
        False, str(data.get("id") or ""), "passthrough", labels.get("pipeline", ""),
    )
    session.advance()
    return Response(content=_response_content(upstream, data), status_code=upstream.status_code,
                    headers=_response_headers(upstream))


@proxy_app.post("/openai/v1/embeddings")
@proxy_app.post("/openai/embeddings")
@proxy_app.post("/v1/embeddings")
async def proxy_openai_embeddings(request: Request) -> Response:
    return await _proxy_openai_plain(request, "embeddings")


@proxy_app.post("/openai/v1/completions")
@proxy_app.post("/openai/completions")
@proxy_app.post("/v1/completions")
async def proxy_openai_completions(request: Request) -> Response:
    return await _proxy_openai_plain(request, "completions")


@proxy_app.get("/health")
async def proxy_health():
    return {"status": "ok", "service": "brevitas-proxy"}
