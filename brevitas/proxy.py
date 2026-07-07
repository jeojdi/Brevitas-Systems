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

import hashlib
import json
import os
from copy import deepcopy
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ._compress import compress_messages, count_messages_tokens, report_usage
from .session import BrevitasSession
# Token-aware cache placement: only marks a prefix that actually clears the model's
# minimum cacheable length (Haiku 4.5 = 4096, Opus 4.5 = 4096, default 1024), with a
# safety margin for tokenizer drift. The legacy optimizers/provider_cache/anthropic.py
# placed breakpoints without ever counting tokens, so sub-minimum markers were inert.
from token_efficiency_model.lossless.provider_cache import apply_anthropic_cache, savings_from_usage

_ANTHROPIC_API = "https://api.anthropic.com"
_OPENAI_API    = "https://api.openai.com"
_DEEPSEEK_API  = "https://api.deepseek.com"
_GROQ_API      = "https://api.groq.com/openai"
_XAI_API       = "https://api.x.ai"       # xAI Grok — a DIFFERENT company from Groq
_MISTRAL_API   = "https://api.mistral.ai"
# Google's OpenAI-COMPATIBLE endpoint (base already includes /v1beta/openai) — lets
# gemini-* route through the OpenAI path with no format translation.
_GEMINI_API    = "https://generativelanguage.googleapis.com/v1beta/openai"


# ── Semantic response cache ───────────────────────────────────────────────────
# On a hit we return the stored provider response verbatim and skip the upstream
# call entirely (100% savings). Lazy singleton; any failure disables it silently so
# the cache can NEVER break a customer's pipeline. Toggle: BREVITAS_CACHE_ENABLED=false.
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
        # count the tokens that were actually served (cache read + creation + fresh)
        prompt = int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0)) \
            + int(u.get("cache_creation_input_tokens", 0))
        return prompt, int(u.get("output_tokens", 0))
    return int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))


def _report_cache_hit(provider: str, model: str, hit, session: BrevitasSession) -> None:
    """A hit made no upstream call → the whole would-be spend is saved. Report it as
    baseline=(prompt+completion) vs compressed=0 so billing records 100% savings.

    The /v1/usage quality gate zeroes savings unless quality_score >= 0.8, so we must
    set it: a hit replays the model's OWN prior answer, so quality == match confidence
    (1.0 for an exact-hash hit, the cosine similarity for a semantic hit — both clear
    the gate by construction since the semantic threshold is 0.97)."""
    baseline = int(hit.prompt_tokens) + int(hit.completion_tokens)
    if baseline > 0:
        session.last_quality = float(getattr(hit, "similarity", 1.0))
        report_usage(provider, model, baseline, 0, session)


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


def _maybe_add_cache_key(body: dict, upstream_url: str) -> None:
    """Set a stable `prompt_cache_key` — a routing hint that groups same-prefix requests
    onto the same backend, raising the provider's native cache-hit rate. Only for
    providers that document the param on chat/completions (OpenAI, Mistral); sending an
    unknown param to others risks a 400, and their caching already works without it."""
    if upstream_url not in (_OPENAI_API, _MISTRAL_API):
        return
    if body.get("prompt_cache_key"):
        return
    msgs = body.get("messages", []) or []
    prefix = msgs[:-1] if len(msgs) > 1 else msgs   # the stable part (system + prior turns)
    key = hashlib.sha256(json.dumps(prefix, sort_keys=True, default=str).encode()).hexdigest()
    body["prompt_cache_key"] = key[:40]


def _cached_input_tokens(data: dict, provider: str, model: str) -> int:
    """Input tokens the provider served from its OWN prompt cache this call (billed at a
    discount) — the lossless cache saving reported to billing. Extraction differs per
    provider; savings_from_usage() already handles each shape."""
    try:
        return int(savings_from_usage((data or {}).get("usage", {}), provider, model).input_cached)
    except Exception:
        return 0

# Allowlist of valid upstream URLs for SSRF protection
_ALLOWED_UPSTREAMS = {_OPENAI_API, _DEEPSEEK_API, _GROQ_API, _XAI_API, _MISTRAL_API, _GEMINI_API}

# Model-name prefixes for Mistral's OpenAI-compatible endpoint
_MISTRAL_PREFIXES = ("mistral", "magistral", "ministral", "codestral", "devstral", "pixtral")


def _completions_url(upstream_base: str) -> str:
    """Full chat/completions URL for an upstream base. Google's OpenAI-compat base
    already ends in /v1beta/openai, so it takes just /chat/completions; everyone else
    takes /v1/chat/completions."""
    if upstream_base == _GEMINI_API:
        return f"{upstream_base}/chat/completions"
    return f"{upstream_base}/v1/chat/completions"


def parse_brevitas_headers(headers: dict) -> dict[str, str]:
    """
    Extract brevitas tracking labels from request headers.
    Returns dict with 'pipeline', 'agent', 'run_id' keys (empty strings if not present).
    """
    return {
        "pipeline": headers.get("x-brevitas-pipeline", ""),
        "agent": headers.get("x-brevitas-agent", ""),
        "run_id": headers.get("x-brevitas-run-id", ""),
    }

proxy_app = FastAPI(title="Brevitas Proxy", docs_url=None, redoc_url=None)


def get_openai_compatible_upstream(model: str, override_header: str | None = None) -> str:
    """
    Route OpenAI-compatible requests to the correct provider upstream.
    Returns base URL for the upstream API based on model name prefix or header override.

    Model routing:
    - deepseek-*                       → https://api.deepseek.com
    - grok-*                           → https://api.x.ai        (xAI)
    - groq-*                           → https://api.groq.com/openai (Groq host)
    - mistral-*/magistral-*/codestral… → https://api.mistral.ai
    - openai models or unrecognized    → https://api.openai.com

    Can be overridden with x-brevitas-upstream header (SSRF-protected: allowlist only).
    Non-allowlisted overrides are ignored; falls back to model-prefix routing.
    """
    # SSRF protection: only allow known upstream URLs
    if override_header and override_header in _ALLOWED_UPSTREAMS:
        return override_header

    model_lower = (model or "").lower()
    if model_lower.startswith("deepseek"):
        return _DEEPSEEK_API
    elif model_lower.startswith("grok"):
        return _XAI_API          # xAI Grok — was wrongly sent to Groq's API (bug fix)
    elif model_lower.startswith("groq"):
        return _GROQ_API
    elif model_lower.startswith(_MISTRAL_PREFIXES):
        return _MISTRAL_API
    elif model_lower.startswith(("gemini", "gemma")):
        return _GEMINI_API
    else:
        return _OPENAI_API

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

    # Semantic cache: key on the ORIGINAL request (deep-copied, since compression and
    # cache_control injection mutate `body` below and would change the key). On a hit,
    # skip compression AND the upstream call entirely.
    cache = _get_cache()
    cache_body = deepcopy(body) if cache is not None else None
    if cache is not None:
        hit = _cache_lookup(cache, cache_body, "anthropic", model)
        if hit is not None:
            _report_cache_hit("anthropic", model, hit, session)
            session.advance()
            return JSONResponse(content=hit.response, status_code=200)

    compressed, baseline, compressed_tok = compress_messages(
        messages, session, task=body.get("system", ""), lossless=True
    )
    body["messages"] = compressed

    # Apply Anthropic-specific cache_control breakpoints for ephemeral caching.
    # Mutates `body` in place and returns a CachePlan (which prefix/how many
    # breakpoints actually cleared the model minimum) — keep it for observability.
    cache_plan = apply_anthropic_cache(body)

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
            # ponytail: streamed calls report compression savings only; provider-cache
            # tokens live in the final SSE usage event — parse per-provider if itemizing
            # streamed cache savings ever matters.
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
            if cache is not None and resp.status_code == 200:
                _cache_store(cache, cache_body, "anthropic", model, data)
            cached_tok = _cached_input_tokens(data, "anthropic", model)
            report_usage("anthropic", model, baseline, compressed_tok, session,
                         cached_tokens=cached_tok)
            session.advance()
            return JSONResponse(content=data, status_code=resp.status_code)


# ── OpenAI: POST /v1/chat/completions ────────────────────────────────────────

# The OpenAI SDK appends `/chat/completions` to its base_url. So base_url
# `…/openai` → `/openai/chat/completions` and bare `…` → `/chat/completions`.
# Register all suffix variants so any documented OPENAI_BASE_URL value routes
# here instead of silently 404-ing (a call that bypasses Brevitas = lost savings).
@proxy_app.post("/openai/v1/chat/completions")
@proxy_app.post("/openai/chat/completions")
@proxy_app.post("/v1/chat/completions")
@proxy_app.post("/chat/completions")
async def proxy_openai_chat(request: Request) -> Any:
    body = await request.json()
    messages: list[dict] = body.get("messages", [])
    model: str = body.get("model", "")
    auth = request.headers.get("authorization", "")
    session = _session_for(f"oai:{auth}")

    # Semantic cache keyed on the ORIGINAL request. model_id already isolates the cache
    # per model, so the generic "openai" provider label here is fine for keying.
    cache = _get_cache()
    cache_body = deepcopy(body) if cache is not None else None
    if cache is not None:
        hit = _cache_lookup(cache, cache_body, "openai", model)
        if hit is not None:
            _report_cache_hit("openai", model, hit, session)
            session.advance()
            return JSONResponse(content=hit.response, status_code=200)

    system_msgs = [m["content"] for m in messages if m.get("role") == "system"]
    task = system_msgs[0] if system_msgs else ""
    compressed, baseline, compressed_tok = compress_messages(
        messages, session, task=task, lossless=True
    )
    body["messages"] = compressed

    headers = _passthrough_headers(request, "openai")
    is_stream = body.get("stream", False)

    # Route to correct upstream API based on model name or header override
    override_upstream = request.headers.get("x-brevitas-upstream")
    upstream_url = get_openai_compatible_upstream(model, override_upstream)
    _maybe_add_cache_key(body, upstream_url)

    async with httpx.AsyncClient(timeout=120) as client:
        if is_stream:
            async def stream_gen():
                async with client.stream(
                    "POST", _completions_url(upstream_url),
                    headers=headers, json=body
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            # ponytail: see Anthropic branch — streamed calls itemize compression only.
            report_usage("openai", model, baseline, compressed_tok, session)
            session.advance()
            return StreamingResponse(stream_gen(), media_type="text/event-stream")
        else:
            resp = await client.post(
                _completions_url(upstream_url), headers=headers, json=body
            )
            data = resp.json()
            try:
                text = data["choices"][0]["message"]["content"]
                session.record_response(text)
            except (KeyError, IndexError):
                pass
            if cache is not None and resp.status_code == 200:
                _cache_store(cache, cache_body, "openai", model, data)
            cached_tok = _cached_input_tokens(data, "openai", model)
            report_usage("openai", model, baseline, compressed_tok, session,
                         cached_tokens=cached_tok)
            session.advance()
            return JSONResponse(content=data, status_code=resp.status_code)


@proxy_app.get("/health")
async def proxy_health():
    return {"status": "ok", "service": "brevitas-proxy"}
