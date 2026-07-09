# Run from repo root: uvicorn api.server:app --reload
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import json
import logging
import queue
import threading
import time as _time
from contextlib import asynccontextmanager

import requests as _requests
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from typing import List, Optional

from token_efficiency_model.lossless.api_adapter import retrieval_select
from token_efficiency_model.lossless.provider_cache import count_tokens, savings_from_usage
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
from .store import UsageStore, cost_for_tokens, PROVIDER_COSTS_PER_1M

# ── Encryption ───────────────────────────────────────────────────────────────

def _load_fernet() -> Fernet:
    secret = os.getenv("BREVITAS_SECRET_KEY")
    if secret:
        key = secret.encode() if isinstance(secret, str) else secret
        return Fernet(key)
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
}

_PROVIDER_MODELS = {
    "ollama":    ["llama3.2", "llama3.1", "mistral", "gemma3", "phi4", "qwen2.5"],
    "anthropic": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "openai":    ["gpt-4o", "gpt-4o-mini", "o3-mini"],
    "grok":      ["grok-3", "grok-3-mini"],
    "deepseek":  ["deepseek-chat", "deepseek-reasoner"],
}


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
            return f"[ollama error: {exc}]"
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
            return f"[anthropic error: {exc}]"
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
            return f"[{base_url} error: {exc}]"
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


# ── Rate limiting ─────────────────────────────────────────────────────────────

def _rate_key(request: Request) -> str:
    return request.headers.get("X-API-Key") or request.client.host

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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _check_body_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 2_000_000:
        return JSONResponse(status_code=413, content={"detail": "Request body too large (max 2 MB)"})
    return await call_next(request)


_store = UsageStore()


def _authenticated(x_api_key: Optional[str] = Header(None)) -> str:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    kh = hash_key(x_api_key)
    if not _store.key_exists(kh):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return kh


# ── Key management ────────────────────────────────────────────────────────────

class CreateKeyRequest(BaseModel):
    name: str = Field(default="default", max_length=100)


@app.post("/v1/keys")
@limiter.limit("10/minute")
def create_key(request: Request, body: CreateKeyRequest):
    key = generate_api_key()
    kh = hash_key(key)
    _store.create_key(kh, body.name)
    return {"api_key": key, "name": body.name}


@app.get("/v1/keys")
@limiter.limit("60/minute")
def list_keys(request: Request, _: str = Depends(_authenticated)):
    return {"keys": _store.list_keys()}


# ── Provider config ───────────────────────────────────────────────────────────

class ProviderConfigRequest(BaseModel):
    provider: str
    provider_api_key: str = ""
    model: str = Field(max_length=100)


@app.get("/v1/provider")
@limiter.limit("120/minute")
def get_provider(request: Request, kh: str = Depends(_authenticated)):
    config = _store.get_provider_config(kh)
    if config is None:
        return {"provider": "ollama", "model": "llama3.2", "has_api_key": False}
    raw_key = _decrypt(config["provider_api_key"])
    masked = ("*" * 8 + raw_key[-4:]) if len(raw_key) > 4 else ""
    return {
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
    _pipelines.pop(kh, None)
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
    prune_budget:      int       = Field(default=5, ge=1, le=50)
    lossy:             bool       = Field(default=True)   # compress the volatile last message (LLMLingua-2)
    delta_mode:        str       = Field(default="off", pattern="^(off|on)$")
    wire_mode:         str       = Field(default="json", pattern="^(json|msgpack)$")
    pipeline:          str       = Field(default="", max_length=100)
    agent:             str       = Field(default="", max_length=100)
    run_id:            str       = Field(default="", max_length=128)

    @field_validator("messages", "prior_context", mode="before")
    @classmethod
    def _check_str_lengths(cls, v):
        for s in v:
            if len(s) > _MAX_STR:
                raise ValueError(f"Individual strings must be under {_MAX_STR:,} characters")
        return v


@app.post("/v1/compress")
@limiter.limit("60/minute")
def compress(request: Request, body: CompressRequest, kh: str = Depends(_authenticated)):
    """Lossless context reduction (Lever 4 retrieval) with accuracy-first fail-safe.

    Messages pass through unchanged (the volatile content is never lossily rewritten);
    prior_context is reduced to the chunks relevant to `task`. If retrieval is unavailable
    or low-confidence, the FULL context is returned. Savings use the real tokenizer; no
    quality proxy is recorded.
    """
    task = body.task or (body.messages[0][:200] if body.messages else "")
    sel = retrieval_select(task, body.prior_context, k=body.prune_budget, use_adaptive=True)

    # Baseline is measured against the ORIGINAL messages + full prior context.
    baseline_msg_tokens = estimate_tokens_many(body.messages)

    # Lossy lever: shrink the volatile LAST message via the remote compressor. Earlier
    # messages stay byte-identical so the provider cache still hits the stable prefix.
    out_messages = list(body.messages)
    message_reason = "lossy_disabled"
    method = "lossless"
    quality_sim = None
    message_rate = None
    message_roles = None
    info_density = None
    message_latency_ms = 0.0
    if body.lossy and _lossy_enabled() and out_messages:
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

    _store.record_usage(
        key_hash=kh,
        baseline_tokens=baseline_tokens,
        optimized_tokens=output_tokens,
        savings_pct=actual_savings,
        quality_proxy=None,
        strategy=f"lossy:{message_reason}|ctx:{sel['reason']}"[:64],
    )

    return {
        "compressed_messages": out_messages,             # last message may be compressed (lossy)
        "pruned_context":      sel["selected_context"],
        "baseline_tokens":     baseline_tokens,
        "optimized_tokens":    output_tokens,
        "savings_pct":         actual_savings,
        "fallback_applied":    sel["fallback_applied"],
        "reason":              sel["reason"],            # prior-context retrieval reason
        "message_reason":      message_reason,           # last-message optimization reason
        "method":              method,
        "quality_sim":         quality_sim,              # embedding cosine sim (None if unmeasured)
        "message_rate":        message_rate,             # chosen keep-ratio (adaptive), None if n/a
        "message_roles":       message_roles,            # prompt segment roles seen (task/context/…)
        "info_density":        info_density,             # per-class retention + overall_ok
        "message_latency_ms":  message_latency_ms,
    }


class RetrievalCompressRequest(BaseModel):
    task:              str       = Field(default="", max_length=2000)
    prior_context:     List[str] = Field(default=[], max_length=500)
    k:                 int       = Field(default=5, ge=1, le=50)
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

    _store.record_usage(
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
    """Lossless-lever path: reduce prior_context to the chunks relevant to `task` using
    dense retrieval (Lever 4 — DPR/ColBERTv2 family), with an accuracy-first fail-safe to
    full context. Savings are measured with the real tokenizer; no quality proxy."""
    from token_efficiency_model.lossless.api_adapter import retrieval_select

    out = retrieval_select(body.task, body.prior_context, k=body.k,
                           min_top_score=body.min_top_score, use_adaptive=True)
    _store.record_usage(
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
            if cancel_event.is_set():
                return

            sel = retrieval_select(task, body.prior_context, k=body.prune_budget, use_adaptive=True)
            if cancel_event.is_set():
                return

            baseline_msg_tokens = estimate_tokens_many(body.messages)

            # Lossy lever: shrink only the volatile last message (see /v1/compress).
            out_messages = list(body.messages)
            message_reason = "lossy_disabled"
            method = "lossless"
            quality_sim = None
            if body.lossy and _lossy_enabled() and out_messages:
                mo = _optimize_message_logged(out_messages[-1])
                out_messages[-1] = mo["text"]
                message_reason = mo["reason"]
                method = mo["method"]
                quality_sim = mo.get("quality_sim")
            if cancel_event.is_set():
                return

            optimized_msg_tokens = estimate_tokens_many(out_messages)
            baseline_tokens = baseline_msg_tokens + sel["baseline_tokens"]
            output_tokens = optimized_msg_tokens + sel["optimized_tokens"]
            actual_savings = round(max(0.0, (1 - output_tokens / max(1, baseline_tokens)) * 100), 2)

            # Carry the same fields the dashboard's compression card reads, so the token
            # bar + savings + messages/context all populate live (not just on `done`).
            # quality_proxy stays None on this lossless path — never fake a quality number.
            event_queue.put({"stage": "compressed", "selected": len(sel["selected_context"]),
                             "baseline_tokens": baseline_tokens, "optimized_tokens": output_tokens,
                             "savings_pct": actual_savings, "quality_proxy": None,
                             "compressed_messages": out_messages,
                             "pruned_context": sel["selected_context"],
                             "message_reason": message_reason, "method": method,
                             "quality_sim": quality_sim,
                             "fallback": sel["fallback_applied"]})

            _store.record_usage(
                key_hash=kh,
                baseline_tokens=baseline_tokens,
                optimized_tokens=output_tokens,
                savings_pct=actual_savings,
                quality_proxy=None,
                strategy=f"lossy:{message_reason}|ctx:{sel['reason']}"[:64],
            )

            event_queue.put({"stage": "done", "result": {
                "compressed_messages": out_messages,
                "pruned_context":      sel["selected_context"],
                "baseline_tokens":     baseline_tokens,
                "optimized_tokens":    output_tokens,
                "savings_pct":         actual_savings,
                "fallback_applied":    sel["fallback_applied"],
                "reason":              sel["reason"],
                "message_reason":      message_reason,
                "method":              method,
                "quality_sim":         quality_sim,
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


# ── External usage reporting (SDK / proxy) ────────────────────────────────────

class UsageReportRequest(BaseModel):
    provider:         str   = Field(default="", max_length=50)
    model:            str   = Field(default="", max_length=100)
    baseline_tokens:  int   = Field(ge=0)
    compressed_tokens: int  = Field(ge=0)
    quality_score:    Optional[float] = Field(default=None, ge=0.0, le=1.0)  # Real quality from gate
    request_id:       str   = Field(default="", max_length=64)   # idempotency key
    usage_raw:        Optional[dict] = None                       # provider receipt (verbatim usage object)
    strategy:         str   = Field(default="", max_length=32)    # lever that produced the savings
    session_id:       str   = Field(default="", max_length=128)
    pipeline:         str   = Field(default="", max_length=100)
    agent:            str   = Field(default="", max_length=100)
    run_id:           str   = Field(default="", max_length=128)


@app.post("/v1/usage")
@limiter.limit("300/minute")
def report_usage(request: Request, body: UsageReportRequest, kh: str = Depends(_authenticated)):
    # Idempotency (brief b4): a retried post with the same request_id must not
    # double-bill. First-writer wins; duplicates are acknowledged but not recorded.
    if body.request_id and _store.has_request(kh, body.request_id):
        return {"duplicate": True, "request_id": body.request_id,
                "tokens_saved": 0, "cost_saved_usd": 0.0, "brevitas_fee_usd": 0.0,
                "quality_status": "duplicate"}

    tokens_saved  = max(0, body.baseline_tokens - body.compressed_tokens)
    savings_pct   = round((tokens_saved / max(1, body.baseline_tokens)) * 100, 2)

    # Lever 2 visibility: pull the provider's real prompt-cache read count out of the
    # verbatim usage receipt so it lands in the cached_tokens column (else it's 0 and
    # native caching looks like it never fired). Best-effort — never break reporting.
    cached_tokens = 0
    if body.usage_raw:
        try:
            cached_tokens = int(savings_from_usage(body.usage_raw, body.provider, body.model).cached_tokens)
        except Exception:
            cached_tokens = 0

    # Only bill savings if quality passes the gate (unverified ⇒ $0, always)
    quality_floor = 0.8  # Default floor; configurable per customer
    quality_verified = body.quality_score is not None and body.quality_score >= quality_floor

    # Sequential stream gate (brief b4, always-valid mSPRT): every scored call
    # updates the per-customer stream; once the stream trips, billing stops for
    # ALL subsequent calls until the stream is reset — even individually-"verified"
    # ones (the stream evidence says the lever is degrading).
    stream = _seq_stream(kh)
    if body.quality_score is not None:
        stream.update(body.quality_score >= quality_floor)
    stream_tripped = stream.state.tripped

    if quality_verified and not stream_tripped:
        cost_saved = cost_for_tokens(body.provider, body.model, tokens_saved)
        fee = round(cost_saved * 0.10, 8)
        quality_status = "verified"
    else:
        cost_saved = 0.0
        fee = 0.0
        if stream_tripped:
            quality_status = "stream_tripped"
        else:
            quality_status = "unverified" if body.quality_score is None else "failed"

    # Record EVERY call verbatim (auditable log — no more wins-only records):
    # quality_proxy stays None when nothing was verified (never fake 1.0),
    # token/savings figures are stored as reported with billing decided above.
    _store.record_usage(
        key_hash=kh,
        baseline_tokens=body.baseline_tokens,
        optimized_tokens=body.compressed_tokens,
        savings_pct=savings_pct,
        quality_proxy=body.quality_score,
        provider=body.provider,
        model=body.model,
        cost_saved_usd=cost_saved,
        brevitas_fee_usd=fee,
        session_id=body.session_id,
        pipeline=body.pipeline,
        agent=body.agent,
        run_id=body.run_id,
        request_id=body.request_id,
        usage_raw=json.dumps(body.usage_raw) if body.usage_raw else "",
        quality_status=quality_status,
        strategy=body.strategy,
        cached_tokens=cached_tokens,
    )
    return {
        "tokens_saved": tokens_saved if quality_status == "verified" else 0,
        "savings_pct": savings_pct if quality_status == "verified" else 0.0,
        "cost_saved_usd": round(cost_saved, 6),
        "brevitas_fee_usd": round(fee, 6),
        "quality_score": body.quality_score,
        "quality_status": quality_status,
        "stream": stream.to_dict(),
    }


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
    return {"costs_per_1m_tokens": PROVIDER_COSTS_PER_1M}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/v1/stats")
@limiter.limit("120/minute")
def stats(request: Request, kh: str = Depends(_authenticated)):
    return _store.get_stats(kh)


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
    return {"status": "ok", "compressor": _compressor_status()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
