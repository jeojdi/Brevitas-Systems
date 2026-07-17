# Run from repo root: uvicorn api.server:app --reload
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import json
import logging
import queue
import secrets
import threading
import time as _time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import requests as _requests
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Header, Depends, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from typing import List, Optional

from token_efficiency_model.lossless.api_adapter import retrieval_select
from token_efficiency_model.lossless.provider_cache import count_tokens
from token_efficiency_model.lossless.message_optimizer import optimize_message_text

logger = logging.getLogger("brevitas.api")
# Give the logger its own handler so compression telemetry is emitted even under uvicorn (whose
# logging config doesn't touch the root logger, so INFO lines would otherwise be dropped).
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(os.getenv("BREVITAS_LOG_LEVEL", "INFO").upper())
    logger.propagate = False


def estimate_tokens_many(chunks) -> int:
    return sum(count_tokens(c) for c in chunks)


def _lossy_enabled() -> bool:
    """Server-side kill-switch: BREVITAS_COMPRESS_LOSSY=0 forces strict-lossless passthrough
    for every request, regardless of the per-request `lossy` flag."""
    return os.getenv("BREVITAS_COMPRESS_LOSSY", "1").lower() not in ("0", "false", "no")


def _optimize_message_logged(text: str) -> dict:
    """Compress one message and emit a single structured log line for production analysis:
    prompt length, task type, compression ratio, semantic similarity, fallback reason, latency.
    Returns the optimize dict augmented with `latency_ms`."""
    t0 = _time.perf_counter()
    mo = optimize_message_text(text)
    latency_ms = round((_time.perf_counter() - t0) * 1000, 1)
    before, after = mo["tokens_before"], mo["tokens_after"]
    ratio = round(after / before, 4) if before else 1.0
    dens = mo.get("info_density") or {}
    logger.info(
        "compress reason=%s roles=%s rate=%s len_tok=%d out_tok=%d ratio=%.3f "
        "saved_pct=%.1f sim=%s info_ok=%s latency_ms=%.1f",
        mo["reason"], ",".join(mo.get("roles") or []), mo.get("rate"), before, after, ratio,
        round((1 - ratio) * 100, 1), mo.get("quality_sim"), dens.get("overall_ok"), latency_ms,
    )
    mo["latency_ms"] = latency_ms
    return mo


from .auth import generate_api_key, hash_key
from brevitas.receipts import TokenReceipt, calculate_costs, normalize_usage, MODEL_PRICES
from .store import make_store, PROVIDER_COSTS_PER_1M
from brevitas.semantic_cache import SemanticCache
from brevitas import _embed

# ── Encryption ───────────────────────────────────────────────────────────────

def _load_fernet() -> Fernet:
    secret = os.getenv("BREVITAS_SECRET_KEY")
    if secret:
        key = secret.encode() if isinstance(secret, str) else secret
        return Fernet(key)
    if os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
        raise RuntimeError("BREVITAS_SECRET_KEY is required when Supabase is authoritative")
    key_path = Path(__file__).parent / ".secret_key"
    if key_path.exists():
        return Fernet(key_path.read_bytes().strip())
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    return Fernet(key)

_fernet = _load_fernet()


def _encrypt(value: str) -> str:
    if not value:
        return ""
    return _fernet.encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        return value  # legacy plaintext fallback


# ── Provider backends ────────────────────────────────────────────────────────

_OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

_PROVIDER_BASE_URLS = {
    "openai":   "https://api.openai.com/v1",
    "grok":     "https://api.x.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "groq":     "https://api.groq.com/openai/v1",   # OpenAI-compatible; free tier powers the Playground default
}

_PROVIDER_MODELS = {
    "ollama":    ["llama3.2", "llama3.1", "mistral", "gemma3", "phi4", "qwen2.5"],
    "anthropic": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "openai":    ["gpt-4o", "gpt-4o-mini", "o3-mini"],
    "grok":      ["grok-3", "grok-3-mini"],
    "deepseek":  ["deepseek-chat", "deepseek-reasoner"],
    "groq":      ["gemma2-9b-it", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    "azure_openai": [], "google_gemini": [], "xai": [],
    "mistral": [], "cohere": [], "litellm": [], "langchain": [], "bedrock": [],
    "together": [], "fireworks": [], "openrouter": [], "perplexity": [],
    "replicate": [], "huggingface": [],
}

# Playground zero-config default: a free hosted model reached with a single SERVER-side key.
# The key is never sent to the browser and is only used when a request carries no bring-your-own key.
_PLAYGROUND_KEY      = os.getenv("BREVITAS_PLAYGROUND_KEY", "")
_PLAYGROUND_PROVIDER = os.getenv("BREVITAS_PLAYGROUND_PROVIDER", "groq")
_PLAYGROUND_MODEL    = os.getenv("BREVITAS_PLAYGROUND_MODEL", "gemma2-9b-it")

# Playground response cache — repeated/reworded questions skip the model call entirely
# (≈100% savings on that turn). Lazy singleton; any failure disables it so a cache issue
# can never break the endpoint. Semantic layer auto-enables where the embed model is present.
_playground_cache = None
_playground_cache_init = False


def _get_playground_cache():
    global _playground_cache, _playground_cache_init
    if not _playground_cache_init:
        _playground_cache_init = True
        try:
            _playground_cache = SemanticCache(semantic_enabled=_embed.available())
        except Exception as exc:  # pragma: no cover — cache is best-effort
            logger.warning("Playground cache disabled: %s", type(exc).__name__)
            _playground_cache = None
    return _playground_cache


# Saved tokens are priced at a reference paid model (the free default model is $0), clearly
# labeled in the UI as an estimate — never a charge.
_PLAYGROUND_PRICE_MODEL = os.getenv("BREVITAS_PLAYGROUND_PRICE_MODEL", "gpt-4o")
_PLAYGROUND_PRICE = MODEL_PRICES.get(("openai", _PLAYGROUND_PRICE_MODEL), {"input": 2.5, "output": 10.0})


def _price_usd(input_tokens: int, output_tokens: int) -> float:
    """Reference-rate dollar value of saved tokens (input + output)."""
    return round(
        max(0, input_tokens) * _PLAYGROUND_PRICE["input"] / 1_000_000
        + max(0, output_tokens) * _PLAYGROUND_PRICE["output"] / 1_000_000,
        6,
    )


def _make_ollama_backend(model: str):
    def backend(prompt: str, _routed: str) -> str:
        try:
            resp = _requests.post(
                f"{_OLLAMA_HOST}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json().get("response", "")
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Ollama request failed") from exc
    return backend


def _make_anthropic_backend(api_key: str, model: str):
    def backend(prompt: str, _routed: str) -> str:
        try:
            resp = _requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Anthropic request failed") from exc
    return backend


def _make_openai_compat_backend(api_key: str, model: str, base_url: str):
    def backend(prompt: str, _routed: str) -> str:
        try:
            resp = _requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "messages": [{"role": "user", "content": prompt}]},
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Model provider request failed") from exc
    return backend


def _noop_backend(prompt: str, _routed: str) -> str:
    return ""


def _build_backend(config: dict | None):
    if config is None:
        return _noop_backend  # no model configured — skip the call, don't hit localhost
    provider = config["provider"]
    api_key  = _decrypt(config["provider_api_key"])
    model    = config["model"]
    if provider == "ollama":
        return _make_ollama_backend(model)
    if provider == "anthropic":
        return _make_anthropic_backend(api_key, model)
    if provider in _PROVIDER_BASE_URLS:
        return _make_openai_compat_backend(api_key, model, _PROVIDER_BASE_URLS[provider])
    return _noop_backend


def _compress_pipeline(task: str, messages: list[str], prior_context: list[str],
                       prune_budget: int, lossy: bool) -> dict:
    """Shared context-reduction core used by /v1/compress, /v1/compress/stream and
    /v1/playground/stream. Messages pass through unchanged except the volatile LAST message,
    which is lossily shrunk when `lossy` is on and the remote compressor is available.
    prior_context is retrieval-pruned to the chunks relevant to `task`. All savings use the
    real tokenizer; no quality number is ever fabricated (quality_sim is None unless measured)."""
    sel = retrieval_select(task, prior_context, k=prune_budget, use_adaptive=True)
    baseline_msg_tokens = estimate_tokens_many(messages)

    out_messages = list(messages)
    message_reason = "lossy_disabled"
    method = "lossless"
    quality_sim = None
    message_rate = None
    message_roles = None
    info_density = None
    message_latency_ms = 0.0
    if lossy and _lossy_enabled() and out_messages:
        mo = _optimize_message_logged(out_messages[-1])
        out_messages[-1] = mo["text"]
        message_reason = mo["reason"]
        method = mo["method"]
        quality_sim = mo.get("quality_sim")
        message_rate = mo.get("rate")
        message_roles = mo.get("roles")
        info_density = mo.get("info_density")
        message_latency_ms = mo.get("latency_ms", 0.0)

    optimized_msg_tokens = estimate_tokens_many(out_messages)
    baseline_tokens = baseline_msg_tokens + sel["baseline_tokens"]
    output_tokens = optimized_msg_tokens + sel["optimized_tokens"]
    actual_savings = round(max(0.0, (1 - output_tokens / max(1, baseline_tokens)) * 100), 2)
    return {
        "out_messages":       out_messages,
        "selected_context":   sel["selected_context"],
        "baseline_tokens":    baseline_tokens,
        "optimized_tokens":   output_tokens,
        "savings_pct":        actual_savings,
        "message_reason":     message_reason,
        "method":             method,
        "quality_sim":        quality_sim,
        "message_rate":       message_rate,
        "message_roles":      message_roles,
        "info_density":       info_density,
        "message_latency_ms": message_latency_ms,
        "fallback_applied":   sel["fallback_applied"],
        "reason":             sel["reason"],
    }


def _make_named_backend(provider: str, model: str, raw_key: str):
    """Build a one-shot model backend from a provider id + RAW key (no encryption, no store).
    Used for ephemeral Playground keys and the server-side free default."""
    if provider == "ollama":
        return _make_ollama_backend(model)
    if provider == "anthropic":
        return _make_anthropic_backend(raw_key, model)
    if provider in _PROVIDER_BASE_URLS:
        return _make_openai_compat_backend(raw_key, model, _PROVIDER_BASE_URLS[provider])
    return _noop_backend


def _build_chat_backend(byok_provider: str, byok_model: str, byok_key: str):
    """Resolve the model backend for a Playground chat turn. Priority:
      1. bring-your-own ephemeral key from the request (never stored, never logged),
      2. the server-side free default (BREVITAS_PLAYGROUND_KEY),
      3. no model — compression-only (empty response).
    Returns (provider, model, backend)."""
    if byok_key and byok_provider and byok_model:
        allowed = _PROVIDER_MODELS.get(byok_provider)
        if not allowed or byok_model not in allowed:
            raise HTTPException(status_code=502, detail="Unsupported provider or model for chat")
        return byok_provider, byok_model, _make_named_backend(byok_provider, byok_model, byok_key)
    if _PLAYGROUND_KEY:
        return (_PLAYGROUND_PROVIDER, _PLAYGROUND_MODEL,
                _make_named_backend(_PLAYGROUND_PROVIDER, _PLAYGROUND_MODEL, _PLAYGROUND_KEY))
    return "", "", _noop_backend


def _run_configured_model(kh: str, messages: list[str], context: list[str], task: str) -> dict:
    config = _store.get_provider_config(kh)
    if not config:
        return {"provider": "", "model": "", "model_response": ""}
    if config.get("model") not in (_PROVIDER_MODELS.get(config.get("provider")) or []):
        raise HTTPException(status_code=502, detail="Saved model provider configuration is invalid")
    prompt = "\n\n".join(filter(None, [f"Task: {task}" if task else "", *messages, *context]))
    return {
        "provider": config["provider"],
        "model": config["model"],
        "model_response": _build_backend(config)(prompt, config["model"]),
    }


# ── Rate limiting ─────────────────────────────────────────────────────────────

def _rate_key(request: Request) -> str:
    raw = request.headers.get("X-Brevitas-Key") or request.headers.get("X-API-Key")
    return hash_key(raw) if raw else (request.client.host if request.client else "unknown")

limiter = Limiter(key_func=_rate_key)


# ── App setup ─────────────────────────────────────────────────────────────────

_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    # loud-once on boot if lossy compression is on but no compressor is reachable
    _warn_if_compressor_missing()
    yield


app = FastAPI(title="Brevitas API", version="1.0.0", docs_url=None, redoc_url=None,
              lifespan=_lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=_ALLOWED_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path != "/v1/health":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.middleware("http")
async def _check_body_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    try:
        too_large = content_length and int(content_length) > 2_000_000
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
    if too_large:
        return JSONResponse(status_code=413, content={"detail": "Request body too large (max 2 MB)"})
    if not content_length and request.method in {"POST", "PUT", "PATCH"}:
        if len(await request.body()) > 2_000_000:
            return JSONResponse(status_code=413, content={"detail": "Request body too large (max 2 MB)"})
    return await call_next(request)


_store = make_store()
_valid_key_cache: dict[str, float] = {}
_valid_key_lock = threading.Lock()
_proxy_windows: dict[str, deque] = defaultdict(deque)
_proxy_active: dict[str, int] = defaultdict(int)
_proxy_limit_lock = threading.Lock()
_PROXY_PATHS = {"/v1/messages", "/v1/chat/completions", "/openai/v1/chat/completions",
                "/openai/chat/completions",
                "/v1/responses", "/openai/v1/responses", "/v1/embeddings",
                "/openai/responses", "/openai/embeddings", "/openai/completions",
                "/openai/v1/embeddings", "/v1/completions", "/openai/v1/completions"}


def _key_exists(kh: str) -> bool:
    now = _time.monotonic()
    with _valid_key_lock:
        if _valid_key_cache.get(kh, 0) > now:
            return True
    valid = _store.key_exists(kh)
    if valid:
        with _valid_key_lock:
            _valid_key_cache[kh] = now + 30  # bounded revocation delay, avoids one DB read per AI call
    return valid


@app.middleware("http")
async def _protect_model_proxy(request: Request, call_next):
    if request.url.path not in _PROXY_PATHS:
        return await call_next(request)
    raw_key = request.headers.get("x-brevitas-key", "")
    if not raw_key and os.getenv("BREVITAS_PROXY_AUTH", "true").lower() not in ("0", "false", "no"):
        return JSONResponse(status_code=401, content={"detail": "Missing X-Brevitas-Key header"})
    kh = hash_key(raw_key) if raw_key else f"ip:{request.client.host if request.client else 'unknown'}"
    if raw_key:
        try:
            if not _key_exists(kh):
                return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
        except Exception:
            return JSONResponse(status_code=503, content={"detail": "Authentication store unavailable"})
    now = _time.monotonic()
    rpm = int(os.getenv("BREVITAS_PROXY_RPM", "300"))
    concurrency = int(os.getenv("BREVITAS_PROXY_CONCURRENCY", "20"))
    # ponytail: process-local counters are enough for one Railway replica; use Redis before scaling out.
    with _proxy_limit_lock:
        window = _proxy_windows[kh]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= rpm or _proxy_active[kh] >= concurrency:
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"},
                                headers={"Retry-After": "1"})
        window.append(now)
        _proxy_active[kh] += 1
    try:
        response = await call_next(request)
    except Exception:
        with _proxy_limit_lock:
            _proxy_active[kh] = max(0, _proxy_active[kh] - 1)
        raise
    original = response.body_iterator

    async def release_after_response():
        try:
            async for chunk in original:
                yield chunk
        finally:
            with _proxy_limit_lock:
                _proxy_active[kh] = max(0, _proxy_active[kh] - 1)

    response.body_iterator = release_after_response()
    return response


def _safe_record_usage(**values) -> bool:
    """Telemetry is best-effort; it must never damage a model/compression response."""
    try:
        if "owner_id" not in values and values.get("key_hash"):
            values["owner_id"] = _store.key_owner(values["key_hash"])
        return bool(_store.record_usage(**values))
    except Exception as exc:
        logger.error("usage write failed: %s", type(exc).__name__)
        return False


def _authenticated(x_api_key: Optional[str] = Header(None),
                   x_brevitas_key: Optional[str] = Header(None)) -> str:
    key = x_brevitas_key or x_api_key
    if not key:
        raise HTTPException(status_code=401, detail="Missing X-Brevitas-Key header")
    kh = hash_key(key)
    try:
        valid = _key_exists(kh)
    except Exception as exc:
        logger.error("API key store unavailable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Authentication store unavailable") from exc
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return kh


def _dashboard_identity(request: Request) -> dict:
    """Validate and return the current Supabase user."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return {}
    url = (os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or "").rstrip("/")
    # The store already requires this project-scoped credential; prefer it so a stale
    # optional anon key cannot make valid dashboard sessions look unauthenticated.
    api_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY") \
        or os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY")
    if not url or not api_key:
        return {}
    try:
        response = _requests.get(f"{url}/auth/v1/user", headers={
            "apikey": api_key, "Authorization": auth,
        }, timeout=5)
        if not response.ok:
            logger.warning("Supabase dashboard auth rejected status=%s", response.status_code)
            return {}
        return response.json()
    except Exception as exc:
        logger.warning("Supabase dashboard auth unavailable: %s", type(exc).__name__)
        return {}


def _dashboard_user(request: Request) -> str:
    return str(_dashboard_identity(request).get("id") or "")


def _admin_authenticated(request: Request) -> str:
    identity = _dashboard_identity(request)
    metadata = identity.get("app_metadata") or {}
    if metadata.get("brevitas_admin") is True or metadata.get("role") == "brevitas_admin":
        return str(identity.get("id") or "admin")
    raise HTTPException(status_code=403, detail="Admin access required")


_POSTHOG_CACHE: dict[str, dict] = {}
_POSTHOG_CACHE_TTL = 300


def _posthog_query(hogql: str) -> list:
    project_id = os.getenv("POSTHOG_PROJECT_ID", "")
    personal_key = os.getenv("POSTHOG_PERSONAL_API_KEY", "")
    api_host = os.getenv("POSTHOG_API_HOST", "https://us.posthog.com").rstrip("/")
    if not project_id or not personal_key:
        raise HTTPException(status_code=503, detail="PostHog reporting is not configured")
    try:
        response = _requests.post(
            f"{api_host}/api/projects/{project_id}/query/",
            headers={"Authorization": f"Bearer {personal_key}", "Content-Type": "application/json"},
            json={"query": {"kind": "HogQLQuery", "query": hogql}},
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("results") or []
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("PostHog admin summary unavailable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Traffic analytics temporarily unavailable") from exc


def _posthog_admin_summary(days: int) -> dict:
    cache_key = str(days)
    cached = _POSTHOG_CACHE.get(cache_key)
    now = _time.time()
    if cached and now - cached["time"] < _POSTHOG_CACHE_TTL:
        return cached["value"]

    interval = f"{days} DAY"
    overview = _posthog_query(f"""
        SELECT
          countIf(event = '$pageview') AS pageviews,
          uniqIf(distinct_id, event = '$pageview') AS visitors,
          uniqIf(toString(properties.$session_id), event = '$pageview') AS sessions,
          countIf(event = 'signup_started') AS signup_started,
          countIf(event = 'signup_submitted') AS signup_submitted
        FROM events
        WHERE timestamp >= now() - INTERVAL {interval}
    """)
    session_rows = _posthog_query(f"""
        SELECT round(avg(duration), 1), round(100 * avg(if(pageviews <= 1, 1, 0)), 1)
        FROM (
          SELECT dateDiff('second', min(timestamp), max(timestamp)) AS duration,
                 countIf(event = '$pageview') AS pageviews
          FROM events
          WHERE timestamp >= now() - INTERVAL {interval}
            AND notEmpty(toString(properties.$session_id))
          GROUP BY toString(properties.$session_id)
        )
    """)
    trend_rows = _posthog_query(f"""
        SELECT toDate(timestamp) AS day,
               uniqIf(distinct_id, event = '$pageview') AS visitors,
               uniqIf(toString(properties.$session_id), event = '$pageview') AS sessions,
               countIf(event = '$pageview') AS pageviews
        FROM events
        WHERE timestamp >= now() - INTERVAL {interval}
        GROUP BY day ORDER BY day
    """)
    totals = overview[0] if overview else [0, 0, 0, 0, 0]
    session_metrics = session_rows[0] if session_rows else [0, 0]
    project_id = os.getenv("POSTHOG_PROJECT_ID", "")
    ui_host = os.getenv("NEXT_PUBLIC_POSTHOG_UI_HOST", "https://us.posthog.com").rstrip("/")
    result = {
        "range_days": days,
        "pageviews": int(totals[0] or 0),
        "visitors": int(totals[1] or 0),
        "sessions": int(totals[2] or 0),
        "signup_started": int(totals[3] or 0),
        "signup_submitted": int(totals[4] or 0),
        "avg_session_duration_seconds": float(session_metrics[0] or 0),
        "bounce_rate": float(session_metrics[1] or 0),
        "trend": [{"date": str(row[0]), "visitors": int(row[1] or 0),
                   "sessions": int(row[2] or 0), "pageviews": int(row[3] or 0)}
                  for row in trend_rows],
        "posthog_url": f"{ui_host}/project/{project_id}",
    }
    _POSTHOG_CACHE[cache_key] = {"time": now, "value": result}
    return result


# ── bvx browser authorization ────────────────────────────────────────────────

class DeviceCodeRequest(BaseModel):
    device_code: str = Field(min_length=40, max_length=128,
                             pattern=r"^[A-Za-z0-9_-]+$")


def _device_expired(row: dict) -> bool:
    try:
        expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
        return expires <= datetime.now(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return True


@app.post("/v1/device-auth/start")
@limiter.limit("10/minute")
def start_device_auth(request: Request):
    device_code = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    try:
        _store.create_device_request(hash_key(device_code), expires.isoformat())
    except Exception as exc:
        logger.error("device auth start failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Device authorization unavailable") from exc
    dashboard = os.getenv("BREVITAS_DASHBOARD_URL", "https://brevitassystems.com/dashboard").rstrip("/")
    return JSONResponse({
        "device_code": device_code,
        "verification_uri_complete": f"{dashboard}#bvx={device_code}",
        "expires_in": 600,
        "interval": 2,
    }, headers={"Cache-Control": "no-store"})


@app.post("/v1/device-auth/approve")
@limiter.limit("20/minute")
def approve_device_auth(request: Request, body: DeviceCodeRequest):
    owner_id = _dashboard_user(request)
    if not owner_id:
        raise HTTPException(status_code=401, detail="Sign in to approve this device")
    device_hash = hash_key(body.device_code)
    row = _store.get_device_request(device_hash)
    if not row or _device_expired(row):
        raise HTTPException(status_code=410, detail="Device authorization expired")
    if row.get("approved_at"):
        if row.get("owner_id") != owner_id:
            raise HTTPException(status_code=409, detail="Device already connected")
        return {"status": "approved"}

    key = generate_api_key()
    kh = hash_key(key)
    if not _store.approve_device_request(device_hash, owner_id, kh, _encrypt(key)):
        raise HTTPException(status_code=409, detail="Device authorization already handled")
    logger.info("bvx device approved owner=%s", owner_id)
    return {"status": "approved"}


@app.post("/v1/device-auth/token")
@limiter.limit("120/minute")
def consume_device_auth(request: Request, body: DeviceCodeRequest):
    device_hash = hash_key(body.device_code)
    row = _store.get_device_request(device_hash)
    if not row or _device_expired(row):
        raise HTTPException(status_code=410, detail="Device authorization expired or consumed")
    if not row.get("approved_at"):
        return JSONResponse({"status": "pending"}, status_code=202,
                            headers={"Cache-Control": "no-store"})
    consumed = _store.consume_device_request(device_hash)
    if not consumed:
        raise HTTPException(status_code=410, detail="Device authorization already consumed")
    key = _decrypt(consumed["encrypted_key"])
    with _valid_key_lock:
        _valid_key_cache[hash_key(key)] = _time.monotonic() + 30
    return JSONResponse({"api_key": key},
                        headers={"Cache-Control": "no-store"})


# ── Key management ────────────────────────────────────────────────────────────

class CreateKeyRequest(BaseModel):
    name: str = Field(default="default", max_length=100)


@app.post("/v1/keys")
@limiter.limit("10/minute")
def create_key(request: Request, body: CreateKeyRequest):
    owner_id = _dashboard_user(request)
    parent = request.headers.get("x-brevitas-key") or request.headers.get("x-api-key") or ""
    parent_authenticated = False
    if parent:
        parent_hash = hash_key(parent)
        if not _key_exists(parent_hash):
            raise HTTPException(status_code=401, detail="Invalid API key")
        parent_authenticated = True
        owner_id = _store.key_owner(parent_hash)
    if not owner_id and not parent_authenticated and os.getenv("BREVITAS_ALLOW_KEY_CREATION", "").lower() not in ("1", "true", "yes"):
        raise HTTPException(status_code=401, detail="Sign in before creating an API key")
    key = generate_api_key()
    kh = hash_key(key)
    _store.create_key(kh, body.name, owner_id=owner_id)
    with _valid_key_lock:
        _valid_key_cache[kh] = _time.monotonic() + 30
    return {"api_key": key, "name": body.name}


@app.get("/v1/keys")
@limiter.limit("60/minute")
def list_keys(request: Request, _: str = Depends(_authenticated)):
    return {"keys": _store.list_keys(_)}


@app.delete("/v1/keys/{key_id}")
@limiter.limit("30/minute")
def revoke_key(request: Request, key_id: str, kh: str = Depends(_authenticated)):
    if len(key_id) != 64:
        raise HTTPException(status_code=400, detail="Invalid key id")
    if not _store.delete_key(kh, key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    with _valid_key_lock:
        _valid_key_cache.pop(key_id, None)
    return {"revoked": True}


# ── Provider config ───────────────────────────────────────────────────────────

class ProviderConfigRequest(BaseModel):
    provider: str
    provider_api_key: str = ""
    model: str = Field(min_length=1, max_length=100)


@app.get("/v1/provider")
@limiter.limit("120/minute")
def get_provider(request: Request, kh: str = Depends(_authenticated)):
    config = _store.get_provider_config(kh)
    if config is None:
        return {"configured": False, "provider": "ollama", "model": "llama3.2",
                "has_api_key": False}
    raw_key = _decrypt(config["provider_api_key"])
    masked = ("*" * 8 + raw_key[-4:]) if len(raw_key) > 4 else ""
    return {
        "configured": True,
        "provider": config["provider"],
        "model": config["model"],
        "has_api_key": bool(raw_key),
        "masked_key": masked,
    }


@app.put("/v1/provider")
@limiter.limit("30/minute")
def set_provider(request: Request, body: ProviderConfigRequest, kh: str = Depends(_authenticated)):
    if body.provider not in _PROVIDER_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{body.provider}'")
    allowed_models = _PROVIDER_MODELS[body.provider]
    if not allowed_models:
        raise HTTPException(status_code=400, detail=f"Provider '{body.provider}' is not available")
    if body.model not in allowed_models:
        raise HTTPException(status_code=400, detail="Model is not supported by this provider")
    existing = _store.get_provider_config(kh)
    if body.provider != "ollama" and not body.provider_api_key:
        # Allow if a key is already saved for this provider — keep it
        has_existing_key = existing and existing.get("provider_api_key") and existing.get("provider") == body.provider
        if not has_existing_key:
            raise HTTPException(status_code=400, detail="provider_api_key is required for this provider")
        encrypted_key = existing["provider_api_key"]
    else:
        encrypted_key = _encrypt(body.provider_api_key)
    _store.set_provider_config(kh, body.provider, encrypted_key, body.model)
    return {"ok": True, "provider": body.provider, "model": body.model}


@app.get("/v1/providers")
def list_providers():
    return {"providers": _PROVIDER_MODELS}


@app.get("/v1/ollama/models")
def ollama_models():
    try:
        resp = _requests.get(f"{_OLLAMA_HOST}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        return {"models": models, "available": True}
    except Exception:
        return {"models": _PROVIDER_MODELS["ollama"], "available": False}


# ── Compression ───────────────────────────────────────────────────────────────

_MAX_STR = 50_000


class CompressRequest(BaseModel):
    messages:          List[str] = Field(max_length=100)
    prior_context:     List[str] = Field(default=[], max_length=200)
    task:              str       = Field(default="", max_length=2000)
    complexity:        float     = Field(default=0.5, ge=0.0, le=1.0)
    urgency:           float     = Field(default=0.5, ge=0.0, le=1.0)
    compression_level: int       = Field(default=2, ge=1, le=3)
    prune_budget:      int       = Field(default=8, ge=1, le=50)
    lossy:             bool       = Field(default=True)   # compress the volatile last message (LLMLingua-2)
    delta_mode:        str       = Field(default="off", pattern="^(off|on)$")
    wire_mode:         str       = Field(default="json", pattern="^(json|msgpack)$")
    pipeline:          str       = Field(default="", max_length=100)
    agent:             str       = Field(default="", max_length=100)
    run_id:            str       = Field(default="", max_length=128)
    meter:             bool      = Field(default=True)

    @field_validator("messages", "prior_context", mode="before")
    @classmethod
    def _check_str_lengths(cls, v):
        for s in v if isinstance(v, list) else []:
            if isinstance(s, str) and len(s) > _MAX_STR:
                raise ValueError(f"Individual strings must be under {_MAX_STR:,} characters")
        return v


@app.post("/v1/compress")
@limiter.limit("60/minute")
def compress(request: Request, body: CompressRequest, kh: str = Depends(_authenticated)):
    """Context reduction (Lever 4 retrieval) with an accuracy-first fail-safe.

    Retrieval can omit evidence and is therefore experimental, not lossless. Messages pass
    through unchanged when ``lossy=false``;
    prior_context is reduced to the chunks relevant to `task`. If retrieval is unavailable
    or low-confidence, the FULL context is returned. Savings use the real tokenizer; no
    quality proxy is recorded.
    """
    task = body.task or (body.messages[0][:200] if body.messages else "")
    # Baseline is measured against the ORIGINAL messages + full prior context; the volatile
    # LAST message may be lossily shrunk while earlier messages stay byte-identical so the
    # provider cache still hits the stable prefix.
    pipe = _compress_pipeline(task, body.messages, body.prior_context, body.prune_budget, body.lossy)
    out_messages = pipe["out_messages"]
    model_result = _run_configured_model(
        kh, out_messages, pipe["selected_context"], task,
    )

    if body.meter:
        _safe_record_usage(
            key_hash=kh,
            baseline_tokens=pipe["baseline_tokens"],
            optimized_tokens=pipe["optimized_tokens"],
            savings_pct=pipe["savings_pct"],
            quality_proxy=None,
            strategy=f"lossy:{pipe['message_reason']}|ctx:{pipe['reason']}"[:64],
        )

    return {
        "compressed_messages": out_messages,             # last message may be compressed (lossy)
        "pruned_context":      pipe["selected_context"],
        "baseline_tokens":     pipe["baseline_tokens"],
        "optimized_tokens":    pipe["optimized_tokens"],
        "savings_pct":         pipe["savings_pct"],
        "fallback_applied":    pipe["fallback_applied"],
        "reason":              pipe["reason"],            # prior-context retrieval reason
        "message_reason":      pipe["message_reason"],    # last-message optimization reason
        "method":              pipe["method"],
        "quality_sim":         pipe["quality_sim"],       # embedding cosine sim (None if unmeasured)
        "message_rate":        pipe["message_rate"],      # chosen keep-ratio (adaptive), None if n/a
        "message_roles":       pipe["message_roles"],     # prompt segment roles seen (task/context/…)
        "info_density":        pipe["info_density"],      # per-class retention + overall_ok
        "message_latency_ms":  pipe["message_latency_ms"],
        **model_result,
        "routed_model_hint":   model_result["model"],
    }


class RetrievalCompressRequest(BaseModel):
    task:              str       = Field(default="", max_length=2000)
    prior_context:     List[str] = Field(default=[], max_length=500)
    k:                 int       = Field(default=8, ge=1, le=50)
    min_top_score:     float     = Field(default=0.2, ge=0.0, le=1.0)


class OptimizePromptRequest(BaseModel):
    prompt: str             = Field(max_length=200_000)
    rate:   Optional[float] = Field(default=None, ge=0.1, le=1.0)  # None=auto by task
    task:   Optional[str]   = Field(default=None, max_length=40)   # hint: creative/code/...
    smart:  bool            = Field(default=True)  # task-aware router (vs fixed rate)


@app.post("/v1/optimize-prompt")
@limiter.limit("120/minute")
def optimize_prompt_endpoint(request: Request, body: OptimizePromptRequest,
                             kh: str = Depends(_authenticated)):
    """Shrink a SINGLE prompt's tokens.

    smart=True (default): a task-aware router classifies the prompt (creative/code/reasoning/
    extraction/...) and picks a safe LLMLingua-2 compression rate — aggressive on creative/
    boilerplate, light on precise tasks — while protecting code blocks + key tokens.
    smart=False or explicit `rate`: use that fixed rate (1.0=lossless). Lossy when rate<1.0
    (LLMLingua-2, arXiv:2403.12968); fail-safe to lossless without the [promptopt] extra.
    Tokens measured with tiktoken."""
    if body.smart and body.rate is None:
        from token_efficiency_model.lossless.task_router import TaskCompressionRouter
        res = TaskCompressionRouter().route(body.prompt, task_hint=body.task)
        r = res.optimization
        extra = {"task": res.task, "rate": res.rate, "protected_code_blocks": res.protected_code_blocks,
                 "reason": res.reason, "quality_sim": res.quality_sim}
    else:
        from token_efficiency_model.lossless.prompt_optimizer import optimize_prompt as _opt
        r = _opt(body.prompt, rate=body.rate if body.rate is not None else 1.0)
        extra = {"task": None, "rate": body.rate}

    _safe_record_usage(
        key_hash=kh,
        baseline_tokens=r.tokens_before,
        optimized_tokens=r.tokens_after,
        savings_pct=r.saved_pct,
        quality_proxy=None,
    )
    return {
        "optimized_prompt": r.optimized,
        "tokens_before": r.tokens_before,
        "tokens_after": r.tokens_after,
        "saved_pct": r.saved_pct,
        "method": r.method,
        "lossy": r.lossy,
        "note": r.note,
        **extra,
    }


@app.post("/v1/compress/retrieval")
@limiter.limit("60/minute")
def compress_retrieval(request: Request, body: RetrievalCompressRequest,
                       kh: str = Depends(_authenticated)):
    """Experimental context reduction using hybrid dense+sparse multi-hop retrieval.

    The path fails safe to full context on empty, broad, low-confidence, or negligible-savings
    queries. It can still omit evidence, so token savings are unverified until the customer's
    paired workload clears a quality gate. Savings use the real tokenizer; no score is invented.
    """
    from token_efficiency_model.lossless.api_adapter import retrieval_select

    out = retrieval_select(body.task, body.prior_context, k=body.k,
                           min_top_score=body.min_top_score, use_adaptive=True)
    _safe_record_usage(
        key_hash=kh,
        baseline_tokens=out["baseline_tokens"],
        optimized_tokens=out["optimized_tokens"],
        savings_pct=out["savings_pct"],
        quality_proxy=None,
    )
    return out


class _ClientGone(Exception):
    """Raised inside the worker thread to unwind the pipeline when the client disconnects."""


@app.post("/v1/compress/stream")
@limiter.limit("60/minute")
async def compress_stream(request: Request, body: CompressRequest, kh: str = Depends(_authenticated)):
    event_queue: queue.Queue = queue.Queue()
    SENTINEL = object()
    cancel_event = threading.Event()

    def _run():
        try:
            task = body.task or (body.messages[0][:200] if body.messages else "")
            event_queue.put({"stage": "retrieving", "task": task[:120]})
            config = _store.get_provider_config(kh)
            if config:
                event_queue.put({"stage": "routed", "provider": config["provider"],
                                 "model": config["model"], "route_fit": 1.0})
            if cancel_event.is_set():
                return

            pipe = _compress_pipeline(task, body.messages, body.prior_context,
                                      body.prune_budget, body.lossy)
            if cancel_event.is_set():
                return
            out_messages = pipe["out_messages"]

            # Carry the same fields the dashboard's compression card reads, so the token
            # bar + savings + messages/context all populate live (not just on `done`).
            # quality_proxy stays None on this lossless path — never fake a quality number.
            event_queue.put({"stage": "compressed", "selected": len(pipe["selected_context"]),
                             "baseline_tokens": pipe["baseline_tokens"], "optimized_tokens": pipe["optimized_tokens"],
                             "savings_pct": pipe["savings_pct"], "quality_proxy": None,
                             "compressed_messages": out_messages,
                             "pruned_context": pipe["selected_context"],
                             "message_reason": pipe["message_reason"], "method": pipe["method"],
                             "quality_sim": pipe["quality_sim"],
                             "fallback": pipe["fallback_applied"]})

            model_result = _run_configured_model(
                kh, out_messages, pipe["selected_context"], task,
            )
            if model_result["model"]:
                event_queue.put({"stage": "model_response", **model_result,
                                 "text": model_result["model_response"]})

            if body.meter:
                _safe_record_usage(
                    key_hash=kh,
                    baseline_tokens=pipe["baseline_tokens"],
                    optimized_tokens=pipe["optimized_tokens"],
                    savings_pct=pipe["savings_pct"],
                    quality_proxy=None,
                    strategy=f"lossy:{pipe['message_reason']}|ctx:{pipe['reason']}"[:64],
                )

            event_queue.put({"stage": "done", "result": {
                "compressed_messages": out_messages,
                "pruned_context":      pipe["selected_context"],
                "baseline_tokens":     pipe["baseline_tokens"],
                "optimized_tokens":    pipe["optimized_tokens"],
                "savings_pct":         pipe["savings_pct"],
                "fallback_applied":    pipe["fallback_applied"],
                "reason":              pipe["reason"],
                "message_reason":      pipe["message_reason"],
                "method":              pipe["method"],
                "quality_sim":         pipe["quality_sim"],
                **model_result,
                "routed_model_hint":   model_result["model"],
            }})
        except _ClientGone:
            pass
        except Exception as exc:
            event_queue.put({"stage": "error", "message": str(exc)})
        finally:
            event_queue.put(SENTINEL)

    threading.Thread(target=_run, daemon=True).start()

    async def event_stream():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await loop.run_in_executor(None, lambda: event_queue.get(timeout=0.5))
                except queue.Empty:
                    continue
                if item is SENTINEL:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            # Signal the worker to stop on any exit (normal end, client abort,
            # or generator close) so it doesn't keep running / record usage.
            cancel_event.set()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Interactive Playground chat ───────────────────────────────────────────────

class PlaygroundChatRequest(BaseModel):
    messages:          List[str] = Field(max_length=100)
    prior_context:     List[str] = Field(default=[], max_length=400)
    task:              str       = Field(default="", max_length=2000)
    compression_level: int       = Field(default=2, ge=1, le=3)
    prune_budget:      int       = Field(default=5, ge=1, le=50)
    # Bring-your-own key: request-scoped only. NEVER stored, NEVER logged.
    byok_provider:     str       = Field(default="", max_length=32)
    byok_model:        str       = Field(default="", max_length=128)
    byok_key:          str       = Field(default="", max_length=400)

    @field_validator("messages", "prior_context", mode="before")
    @classmethod
    def _check_str_lengths(cls, v):
        for s in v if isinstance(v, list) else []:
            if isinstance(s, str) and len(s) > _MAX_STR:
                raise ValueError(f"Individual strings must be under {_MAX_STR:,} characters")
        return v


@app.post("/v1/playground/stream")
@limiter.limit("60/minute")
async def playground_stream(request: Request, body: PlaygroundChatRequest,
                            kh: str = Depends(_authenticated)):
    """Interactive chat for the dashboard Playground. Runs the same compression pipeline as
    /v1/compress/stream, then answers with either a bring-your-own ephemeral model or the
    server-side free default. Streams the same SSE stages so the frontend reader is shared."""
    # Resolve the backend up-front so an invalid BYOK provider/model returns a clean 502
    # instead of surfacing mid-stream. Raises HTTPException on bad input.
    provider, model, backend = _build_chat_backend(body.byok_provider, body.byok_model, body.byok_key)

    event_queue: queue.Queue = queue.Queue()
    SENTINEL = object()
    cancel_event = threading.Event()

    def _run():
        try:
            task = body.task or (body.messages[0][:200] if body.messages else "")
            event_queue.put({"stage": "retrieving", "task": task[:120]})
            if provider and model:
                event_queue.put({"stage": "routed", "provider": provider,
                                 "model": model, "route_fit": 1.0})
            if cancel_event.is_set():
                return

            pipe = _compress_pipeline(task, body.messages, body.prior_context,
                                      body.prune_budget, lossy=True)
            if cancel_event.is_set():
                return
            out_messages = pipe["out_messages"]

            event_queue.put({"stage": "compressed", "selected": len(pipe["selected_context"]),
                             "baseline_tokens": pipe["baseline_tokens"], "optimized_tokens": pipe["optimized_tokens"],
                             "savings_pct": pipe["savings_pct"], "quality_proxy": None,
                             "compressed_messages": out_messages,
                             "pruned_context": pipe["selected_context"],
                             "message_reason": pipe["message_reason"], "method": pipe["method"],
                             "quality_sim": pipe["quality_sim"],
                             "fallback": pipe["fallback_applied"]})

            # Answer with the resolved backend — but first check the semantic/exact cache:
            # a repeated (or reworded) question skips the model call entirely (≈100% savings).
            model_response = ""
            cache_hit = False
            cache_kind = ""
            cache_similarity = 1.0
            cache_saved_tokens = 0
            compression_saved = max(0, pipe["baseline_tokens"] - pipe["optimized_tokens"])
            if provider and model:
                prompt = "\n\n".join(filter(None, [
                    f"Task: {task}" if task else "", *out_messages, *pipe["selected_context"],
                ]))
                cache = _get_playground_cache()
                cbody = {"messages": [{"role": "user", "content": prompt}],
                         "temperature": 0, "_brevitas_cache_namespace": kh}
                hit = None
                if cache is not None:
                    try:
                        hit = cache.lookup(cbody, provider, model)
                    except Exception:
                        hit = None
                if cancel_event.is_set():
                    return

                if hit is not None:
                    model_response = (hit.response or {}).get("text", "")
                    cache_hit = True
                    cache_kind = hit.kind
                    cache_similarity = round(float(hit.similarity), 4)
                    cache_saved_tokens = (hit.prompt_tokens or count_tokens(prompt)) \
                        + (hit.completion_tokens or count_tokens(model_response))
                    event_queue.put({"stage": "cached", "kind": cache_kind,
                                     "similarity": cache_similarity, "tokens_saved": cache_saved_tokens})
                else:
                    model_response = backend(prompt, model)
                    if cache is not None:
                        try:
                            cache.store(cbody, provider, model, {"text": model_response},
                                        prompt_tokens=count_tokens(prompt),
                                        completion_tokens=count_tokens(model_response))
                        except Exception:
                            pass  # caching is best-effort — never fail the turn over it

                event_queue.put({"stage": "model_response", "provider": provider, "model": model,
                                 "text": model_response, "model_response": model_response,
                                 "cached": cache_hit})

            # Total tokens saved this turn = compression + (whole call, if the cache served it).
            tokens_saved_total = compression_saved + (cache_saved_tokens if cache_hit else 0)
            # Cost saved at reference rates: compression trims input tokens; a cache hit also
            # eliminates the full call (its prompt as input + completion as output).
            if cache_hit:
                cost_saved_usd = _price_usd(compression_saved + (hit.prompt_tokens or 0),
                                            hit.completion_tokens or count_tokens(model_response))
            else:
                cost_saved_usd = _price_usd(compression_saved, 0)

            # Record the turn's effective savings so the Overview graphs reflect wins. A cache
            # hit eliminates the whole call, so it books as ~100% savings for that turn.
            if cache_hit:
                _safe_record_usage(
                    key_hash=kh,
                    baseline_tokens=pipe["baseline_tokens"] + cache_saved_tokens,
                    optimized_tokens=0,
                    savings_pct=100.0,
                    quality_proxy=None,
                    strategy=f"chat:cache_{cache_kind}|ctx:{pipe['reason']}"[:64],
                )
            else:
                _safe_record_usage(
                    key_hash=kh,
                    baseline_tokens=pipe["baseline_tokens"],
                    optimized_tokens=pipe["optimized_tokens"],
                    savings_pct=pipe["savings_pct"],
                    quality_proxy=None,
                    strategy=f"chat:{pipe['message_reason']}|ctx:{pipe['reason']}"[:64],
                )

            event_queue.put({"stage": "done", "result": {
                "compressed_messages": out_messages,
                "pruned_context":      pipe["selected_context"],
                "baseline_tokens":     pipe["baseline_tokens"],
                "optimized_tokens":    pipe["optimized_tokens"],
                "savings_pct":         pipe["savings_pct"],
                "fallback_applied":    pipe["fallback_applied"],
                "reason":              pipe["reason"],
                "message_reason":      pipe["message_reason"],
                "method":              pipe["method"],
                "quality_sim":         pipe["quality_sim"],
                "cache_hit":           cache_hit,
                "cache_kind":          cache_kind,
                "cache_similarity":    cache_similarity,
                "tokens_saved_total":  tokens_saved_total,
                "cost_saved_usd":      cost_saved_usd,
                "price_basis":         _PLAYGROUND_PRICE_MODEL,
                "provider":            provider,
                "model":               model,
                "model_response":      model_response,
            }})
        except _ClientGone:
            pass
        except Exception as exc:
            event_queue.put({"stage": "error", "message": str(exc)})
        finally:
            event_queue.put(SENTINEL)

    threading.Thread(target=_run, daemon=True).start()

    async def event_stream():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await loop.run_in_executor(None, lambda: event_queue.get(timeout=0.5))
                except queue.Empty:
                    continue
                if item is SENTINEL:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            cancel_event.set()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── External usage reporting (SDK / proxy) ────────────────────────────────────

class UsageReportRequest(BaseModel):
    provider: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=128)
    operation: str = Field(default="chat", max_length=64)
    baseline_tokens: int = Field(ge=0)
    compressed_tokens: int = Field(ge=0)
    baseline_output_tokens: Optional[int] = Field(default=None, ge=0)
    fresh_input_tokens: Optional[int] = Field(default=None, ge=0)
    cached_input_tokens: Optional[int] = Field(default=None, ge=0)
    cache_write_tokens: Optional[int] = Field(default=None, ge=0)
    cache_write_5m_tokens: Optional[int] = Field(default=None, ge=0)
    cache_write_1h_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    cache_attributable: bool = False
    quality_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # Paired workload evaluation result for quality-affecting methods. A score is
    # observational only; verification is an explicit pass/fail decision rather
    # than an arbitrary global numeric threshold.
    quality_verified: Optional[bool] = None
    request_id: str = Field(default="", max_length=128)
    usage_raw: Optional[dict] = None  # parsed then discarded; never persisted
    strategy: str = Field(default="", max_length=64)
    session_id: str = Field(default="", max_length=128)
    project: str = Field(default="", max_length=128)
    environment: str = Field(default="", max_length=64)
    source: str = Field(default="", max_length=128)
    repo: str = Field(default="", max_length=128)
    client: str = Field(default="", max_length=128)
    pipeline: str = Field(default="", max_length=128)
    agent: str = Field(default="", max_length=128)
    call_site_id: str = Field(default="", max_length=128)
    framework: str = Field(default="", max_length=64)
    gateway: str = Field(default="", max_length=64)
    run_id: str = Field(default="", max_length=128)
    receipt_source: str = Field(default="sdk", pattern="^(sdk|proxy|import|manual)$")
    receipt_available: bool = True
    is_stream: bool = False

    @field_validator("provider", "model", "operation", "strategy", "session_id", "project",
                     "environment", "source", "repo", "client", "pipeline", "agent",
                     "call_site_id", "framework", "gateway", "run_id")
    @classmethod
    def _safe_metadata(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("metadata cannot contain control characters")
        return value

    @field_validator("project", "repo")
    @classmethod
    def _repo_name_only(cls, value: str) -> str:
        """Keep a display name, never a local path or Git remote."""
        name = value.strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        return name


_QUALITY_AFFECTING_STRATEGIES = (
    "retrieve", "retrieval", "llmlingua", "lossy", "semantic_cache", "compress",
)
_BYTE_PRESERVING_STRATEGIES = (
    "exact_cache", "native_cache", "cache_only", "passthrough", "byte_preserving",
    "lossless",
)
BREVITAS_FEE_RATE = 0.25


def _verification_mode(strategy: str) -> str:
    """Classify quality by what the optimizer did, never by an invented score."""
    value = (strategy or "").strip().lower()
    if any(marker in value for marker in _QUALITY_AFFECTING_STRATEGIES):
        return "quality_affecting"
    if any(marker in value for marker in _BYTE_PRESERVING_STRATEGIES):
        return "byte_preserving"
    return "unknown"


def _record_usage_report(kh: str, body: UsageReportRequest) -> dict:
    if body.request_id and _store.has_request(kh, body.request_id):
        return {"duplicate": True, "request_id": body.request_id,
                "tokens_saved": 0, "measured_savings_usd": 0.0,
                "verified_savings_usd": 0.0, "quality_status": "duplicate"}

    parsed = normalize_usage(body.usage_raw, body.provider)
    if any(value is not None for value in (body.fresh_input_tokens, body.cached_input_tokens,
                                            body.cache_write_tokens, body.cache_write_5m_tokens,
                                            body.cache_write_1h_tokens, body.output_tokens)):
        receipt = TokenReceipt(
            fresh_input_tokens=body.fresh_input_tokens or 0,
            cached_input_tokens=body.cached_input_tokens or 0,
            cache_write_tokens=body.cache_write_tokens or 0,
            output_tokens=body.output_tokens or 0,
            cache_write_5m_tokens=body.cache_write_5m_tokens or 0,
            cache_write_1h_tokens=body.cache_write_1h_tokens or 0,
        )
    elif parsed.total_tokens:
        receipt = parsed
    else:
        receipt = TokenReceipt(fresh_input_tokens=body.compressed_tokens)

    # The provider receipt is authoritative for the optimized request, including
    # system prompts, tool schemas, cache categories, and provider-tokenizer
    # overhead. The local tokenizer is used only for the before/after DELTA, where
    # its bias cancels. This keeps baseline and actual costs on the same basis.
    reported_delta = body.baseline_tokens - body.compressed_tokens
    if body.receipt_available:
        optimized_tokens = receipt.input_tokens
        baseline_tokens = max(0, optimized_tokens + reported_delta)
    else:
        baseline_tokens = body.baseline_tokens
        optimized_tokens = body.compressed_tokens
    tokens_saved = baseline_tokens - optimized_tokens
    savings_pct = round((tokens_saved / max(1, baseline_tokens)) * 100, 2)
    costs = (calculate_costs(body.provider, body.model, baseline_tokens, receipt,
                             body.baseline_output_tokens, body.cache_attributable)
             if body.receipt_available else {
                 "pricing_status": "unpriced", "baseline_cost_usd": None,
                 "actual_cost_usd": None, "measured_savings_usd": None,
                 "pricing_version": "", "prices": {},
             })
    measured = costs["measured_savings_usd"]

    mode = _verification_mode(body.strategy)
    stream = _seq_stream(kh)
    if mode == "byte_preserving":
        quality_status = "verified"
    elif body.quality_verified is None:
        quality_status = "unverified"
    else:
        stream.update(body.quality_verified)
        if stream.state.tripped:
            quality_status = "stream_tripped"
        else:
            quality_status = "verified" if body.quality_verified else "failed"
    verified = max(0.0, float(measured or 0)) if quality_status == "verified" else 0.0
    fee = round(verified * BREVITAS_FEE_RATE, 10)

    inserted = _store.record_usage(
        key_hash=kh,
        owner_id=_store.key_owner(kh),
        baseline_tokens=baseline_tokens,
        optimized_tokens=optimized_tokens,
        tokens_saved=tokens_saved,
        savings_pct=savings_pct,
        quality_proxy=body.quality_score,
        provider=body.provider,
        model=body.model,
        operation=body.operation,
        fresh_input_tokens=receipt.fresh_input_tokens,
        cached_input_tokens=receipt.cached_input_tokens,
        cache_write_tokens=receipt.cache_write_tokens,
        cache_write_5m_tokens=receipt.cache_write_5m_tokens,
        cache_write_1h_tokens=receipt.cache_write_1h_tokens,
        cache_attributable=body.cache_attributable,
        output_tokens=receipt.output_tokens,
        baseline_cost_usd=costs["baseline_cost_usd"],
        actual_cost_usd=costs["actual_cost_usd"],
        measured_savings_usd=measured,
        verified_savings_usd=verified,
        brevitas_fee_usd=fee,
        pricing_status=costs["pricing_status"],
        pricing_version=costs["pricing_version"],
        quality_status=quality_status,
        session_id=body.session_id,
        project=body.project,
        environment=body.environment,
        source=body.source,
        repo=body.repo,
        client=body.client,
        pipeline=body.pipeline,
        agent=body.agent,
        call_site_id=body.call_site_id,
        framework=body.framework,
        gateway=body.gateway,
        run_id=body.run_id,
        request_id=body.request_id,
        strategy=body.strategy,
        receipt_source=body.receipt_source,
        is_stream=body.is_stream,
    )
    if not inserted and body.request_id:
        return {"duplicate": True, "request_id": body.request_id,
                "tokens_saved": 0, "measured_savings_usd": 0.0,
                "verified_savings_usd": 0.0, "quality_status": "duplicate"}
    return {
        "tokens_saved": tokens_saved,
        "savings_pct": savings_pct,
        "baseline_tokens": baseline_tokens,
        "compressed_tokens": optimized_tokens,
        "baseline_cost_usd": costs["baseline_cost_usd"],
        "actual_cost_usd": costs["actual_cost_usd"],
        "measured_savings_usd": measured,
        "verified_savings_usd": round(verified, 8),
        "cost_saved_usd": round(verified, 8),
        "brevitas_fee_usd": round(fee, 8),
        "pricing_status": costs["pricing_status"],
        "quality_score": body.quality_score,
        "quality_status": quality_status,
        "stream": stream.to_dict(),
    }


@app.post("/v1/usage")
@limiter.limit("300/minute")
def report_usage(request: Request, body: UsageReportRequest, kh: str = Depends(_authenticated)):
    return _record_usage_report(kh, body)


# ── Sequential quality streams (brief b4) ─────────────────────────────────────
# One always-valid mSPRT stream per customer key. In-memory for now (process
# lifetime); serialized state is exposed via /v1/quality/stream for auditability.
_seq_streams: dict = {}


def _seq_stream(kh: str):
    from token_efficiency_model.quality.sequential import SequentialQualityGate
    if kh not in _seq_streams:
        _seq_streams[kh] = SequentialQualityGate(
            p0=float(os.environ.get("BREVITAS_QUALITY_P0", "0.9")),
            alpha=float(os.environ.get("BREVITAS_QUALITY_ALPHA", "0.05")))
    return _seq_streams[kh]


@app.get("/v1/quality/stream")
def quality_stream(request: Request, kh: str = Depends(_authenticated)):
    """Auditable state of this customer's sequential quality stream."""
    return _seq_stream(kh).to_dict()


@app.post("/v1/quality/stream/reset")
def quality_stream_reset(request: Request, kh: str = Depends(_authenticated)):
    """Reset a tripped stream (after investigation). Deliberately explicit."""
    _seq_streams.pop(kh, None)
    return {"reset": True}


@app.get("/v1/provider-costs")
def provider_costs():
    return {"pricing_as_of": "2026-07-10", "costs_per_1m_tokens": PROVIDER_COSTS_PER_1M}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/v1/stats")
@limiter.limit("120/minute")
def stats(request: Request, kh: str = Depends(_authenticated)):
    return _store.get_stats(kh)


@app.get("/v1/stats/breakdown")
@limiter.limit("120/minute")
def stats_breakdown(request: Request, kh: str = Depends(_authenticated)):
    rows = _store.get_breakdown(kh)
    return {"rows": rows, "totals": _store.get_stats(kh)}


@app.get("/v1/admin/stats")
@limiter.limit("60/minute")
def admin_stats(request: Request, _: str = Depends(_admin_authenticated)):
    logger.info("admin usage overview accessed actor=%s", _)
    return _store.get_admin_stats()


@app.get("/v1/admin/stats/breakdown")
@limiter.limit("60/minute")
def admin_stats_breakdown(
    request: Request,
    range: str = Query("30d", pattern=r"^(7d|30d|90d|all)$"),
    account: str = Query("", max_length=128),
    project: str = Query("", max_length=128),
    client: str = Query("", max_length=128),
    provider: str = Query("", max_length=64),
    model: str = Query("", max_length=128),
    sort: str = Query("actual_cost_usd", pattern=r"^(actual_cost_usd|baseline_cost_usd|verified_savings_usd|brevitas_fee_usd|calls|tokens_saved)$"),
    direction: str = Query("desc", pattern=r"^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: str = Depends(_admin_authenticated),
):
    logger.info("admin usage breakdown accessed actor=%s", _)
    start = ""
    if range != "all":
        days = int(range[:-1])
        start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    filters = {"start": start, "owner_id": account, "project": project,
               "client": client, "provider": provider, "model": model}
    report = (_store.get_admin_report(filters) if hasattr(_store, "get_admin_report") else
              {"rows": _store.get_admin_breakdown(), "totals": _store.get_admin_stats()})
    rows = report["rows"]
    reverse = direction == "desc"
    rows = sorted(rows, key=lambda row: (float(row.get(sort) or 0), row.get("account_id") or ""),
                  reverse=reverse)
    return {"rows": rows[offset:offset + limit], "totals": report["totals"],
            "pagination": {"total": len(rows), "limit": limit, "offset": offset},
            "range": range}


@app.get("/v1/admin/billing")
@limiter.limit("60/minute")
def admin_billing(
    request: Request,
    range: str = Query("30d", pattern=r"^(7d|30d|90d|all)$"),
    account: str = Query("", max_length=128),
    project: str = Query("", max_length=128),
    client: str = Query("", max_length=128),
    provider: str = Query("", max_length=64),
    model: str = Query("", max_length=128),
    _: str = Depends(_admin_authenticated),
):
    logger.info("admin billing summary accessed actor=%s", _)
    start = ""
    if range != "all":
        days = int(range[:-1])
        start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    filters = {"start": start, "owner_id": account, "project": project,
               "client": client, "provider": provider, "model": model}
    report = (_store.get_admin_report(filters) if hasattr(_store, "get_admin_report") else
              {"rows": _store.get_admin_breakdown(), "totals": _store.get_admin_stats()})
    accounts: dict[str, dict] = {}
    for row in report["rows"]:
        account_id = str(row.get("account_id") or "Unattributed")
        bucket = accounts.setdefault(account_id, {
            "account_id": account_id,
            "account_email": row.get("account_email") or "",
            "calls": 0,
            "actual_spend_usd": 0.0,
            "verified_savings_usd": 0.0,
            "amount_owed_usd": 0.0,
        })
        bucket["calls"] += int(row.get("calls") or 0)
        bucket["actual_spend_usd"] += float(row.get("actual_cost_usd") or 0)
        bucket["verified_savings_usd"] += float(row.get("verified_savings_usd") or 0)
        bucket["amount_owed_usd"] += float(row.get("brevitas_fee_usd") or 0)
    for bucket in accounts.values():
        for field in ("actual_spend_usd", "verified_savings_usd", "amount_owed_usd"):
            bucket[field] = round(bucket[field], 8)
    totals = report["totals"]
    return {
        "currency": "USD",
        "amount_owed_usd": round(float(totals.get("total_brevitas_fee_usd") or 0), 8),
        "basis": "metered_brevitas_fees",
        "payment_status_tracked": False,
        "accounts": sorted(accounts.values(),
                           key=lambda item: (-item["amount_owed_usd"], item["account_id"])),
        "range": range,
    }


@app.get("/v1/admin/analytics")
@limiter.limit("30/minute")
def admin_analytics(
    request: Request,
    range: str = Query("30d", pattern=r"^(7d|30d|90d)$"),
    _: str = Depends(_admin_authenticated),
):
    logger.info("admin traffic analytics accessed actor=%s", _)
    return _posthog_admin_summary(int(range[:-1]))


@app.get("/v1/stats/pipelines")
@limiter.limit("120/minute")
def stats_pipelines(request: Request, kh: str = Depends(_authenticated)):
    return _store.get_stats_by_pipeline(kh)


@app.get("/v1/stats/agents")
@limiter.limit("120/minute")
def stats_agents(request: Request, pipeline: str = "", kh: str = Depends(_authenticated)):
    return _store.get_stats_by_agent(kh, pipeline=pipeline)


@app.get("/v1/stats/runs")
@limiter.limit("120/minute")
def stats_runs(request: Request, pipeline: str = "", kh: str = Depends(_authenticated)):
    return _store.get_stats_by_run(kh, pipeline=pipeline)


_COMPRESSOR_STATUS: dict = {"ts": 0.0, "data": None}
_COMPRESSOR_TTL = 30.0  # seconds — probe the microservice at most once per window


def _compressor_status() -> dict:
    """Probe the remote LLMLingua-2 microservice so silent lossless-fallback is visible.

    Returns {configured, reachable, model_loaded}. Cached ~30s so /v1/health stays cheap.
    """
    now = _time.time()
    cached = _COMPRESSOR_STATUS["data"]
    if cached is not None and now - _COMPRESSOR_STATUS["ts"] < _COMPRESSOR_TTL:
        return cached

    url = os.getenv("BREVITAS_COMPRESS_URL", "").rstrip("/")
    data = {"configured": bool(url), "reachable": False, "model_loaded": False}
    if url:
        try:
            r = _requests.get(f"{url}/health", timeout=2)
            if r.ok:
                data["reachable"] = True
                data["model_loaded"] = bool(r.json().get("model_loaded"))
        except Exception:
            pass  # unreachable -> reachable stays False (fail-safe, never raises)
    _COMPRESSOR_STATUS.update(ts=now, data=data)
    return data


def _warn_if_compressor_missing():
    """Loud-once on boot if lossy compression is enabled but no compressor is reachable —
    otherwise the compress path silently degrades to lossless and nobody notices."""
    if not _lossy_enabled():
        logger.info("BREVITAS_COMPRESS_LOSSY disabled — /v1/compress is strict-lossless.")
        return
    st = _compressor_status()
    if not st["configured"]:
        logger.warning("Lossy compression ON but BREVITAS_COMPRESS_URL is unset — "
                       "/v1/compress will fall back to lossless (0%% savings on single prompts).")
    elif not st["reachable"] or not st["model_loaded"]:
        logger.warning("Lossy compression ON but the compress microservice is "
                       "unreachable/not-loaded (%s) — falling back to lossless.", st)


@app.get("/v1/health")
def health():
    compressor = _compressor_status()
    ready = not _lossy_enabled() or all(
        compressor[name] for name in ("configured", "reachable", "model_loaded")
    )
    payload = {"status": "ok" if ready else "degraded", "compressor": compressor}
    # Compression already fails safe to lossless, so a missing optional model must be
    # visible without failing Railway's health check and rolling back the whole API.
    return payload


def _hosted_proxy_receipt(raw_key: str, payload: dict) -> None:
    """In-process bridge: hosted proxy receipts use the caller's tenant key."""
    if not raw_key:
        return
    kh = hash_key(raw_key)
    if not _key_exists(kh):
        return
    _record_usage_report(kh, UsageReportRequest.model_validate(payload))


# Railway serves the management API and provider-compatible proxy from one process.
from brevitas.proxy import proxy_app, set_usage_reporter
set_usage_reporter(_hosted_proxy_receipt)
app.include_router(proxy_app.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
