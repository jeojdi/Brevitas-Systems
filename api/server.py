# Run from repo root: uvicorn api.server:app --reload
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import json
import queue
import threading

import requests as _requests
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from typing import List, Optional

from token_efficiency_model.combined_tactics.pipeline import TokenEfficientPipeline
from token_efficiency_model.common.metrics import estimate_tokens_many
from .auth import generate_api_key, hash_key
from .store import UsageStore, cost_for_tokens, PROVIDER_COSTS_PER_1M
from .mirror import mirror_to_supabase

# ── Supabase Mirror ──────────────────────────────────────────────────────────

def _get_user_id_for_key(key_hash: str) -> Optional[str]:
    """
    Get user UUID for an API key.

    In a production deployment, the api_keys table would have a user_id column
    populated during key creation. This function would query that.

    For now, returns None (mirror is optional and non-blocking).
    """
    # TODO: Query api_keys table to get user_id
    # In production: SELECT user_id FROM api_keys WHERE key_hash = ?
    # For now, mirror is optional
    return None


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

app = FastAPI(title="Brevitas API", version="1.0.0", docs_url=None, redoc_url=None)
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
_pipelines: dict = {}


def _get_pipeline(key_hash: str) -> TokenEfficientPipeline:
    if key_hash not in _pipelines:
        config = _store.get_provider_config(key_hash)
        _pipelines[key_hash] = TokenEfficientPipeline(
            model_backend=_build_backend(config),
            savings_target=60.0,
            quality_floor=0.99,
        )
    return _pipelines[key_hash]


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
    pipeline = _get_pipeline(kh)

    result = pipeline.process_task(
        task_text=body.task or (body.messages[0][:120] if body.messages else ""),
        incoming_messages=body.messages,
        prior_context=body.prior_context,
        complexity=body.complexity,
        urgency=body.urgency,
        compression_level=body.compression_level,
        prune_budget=body.prune_budget,
        delta_mode=body.delta_mode,
        wire_mode=body.wire_mode,
    )

    compressed_msgs = result.debug.get("compressed_messages", [])
    pruned_ctx      = result.debug.get("pruned_context", [])

    # Report savings based on what actually gets passed to the next agent,
    # not the internal wire-protocol payload.
    baseline_tokens  = result.baseline_tokens
    output_tokens    = estimate_tokens_many(compressed_msgs) + estimate_tokens_many(pruned_ctx)
    actual_savings   = round(max(0.0, (1 - output_tokens / max(1, baseline_tokens)) * 100), 2)

    _store.record_usage(
        key_hash=kh,
        baseline_tokens=baseline_tokens,
        optimized_tokens=output_tokens,
        savings_pct=actual_savings,
        quality_proxy=result.quality_proxy,
        pipeline=body.pipeline,
        agent=body.agent,
        run_id=body.run_id,
    )

    # Mirror to Supabase (non-blocking)
    try:
        user_id = _get_user_id_for_key(kh)
        if user_id:
            mirror_to_supabase(
                user_id=user_id,
                key_hash=kh,
                provider=body.provider or "unknown",
                model=body.model or "unknown",
                baseline_tokens=baseline_tokens,
                optimized_tokens=output_tokens,
                session_id=body.session_id or "",
                pipeline=body.pipeline or "",
                agent=body.agent or "",
                run_id=body.run_id or "",
            )
    except Exception as e:
        # Log but don't fail the request
        print(f"Warning: Failed to mirror to Supabase: {e}")

    return {
        "compressed_messages": compressed_msgs,
        "pruned_context":      pruned_ctx,
        "baseline_tokens":     baseline_tokens,
        "optimized_tokens":    output_tokens,
        "savings_pct":         actual_savings,
        "quality_proxy":       round(result.quality_proxy, 4),
        "routed_model_hint":   result.routed_model,
        "model_response":      result.model_response,
        "state_id":            result.debug.get("state_id", ""),
    }


class _ClientGone(Exception):
    """Raised inside the worker thread to unwind the pipeline when the client disconnects."""


@app.post("/v1/compress/stream")
@limiter.limit("60/minute")
async def compress_stream(request: Request, body: CompressRequest, kh: str = Depends(_authenticated)):
    pipeline = _get_pipeline(kh)
    event_queue: queue.Queue = queue.Queue()
    SENTINEL = object()
    cancel_event = threading.Event()

    def _run():
        def callback(stage: str, data: dict):
            # Abort the pipeline as soon as the client goes away. Raising here
            # unwinds process_task — before the model call if we haven't reached
            # it yet — and prevents usage being recorded for an abandoned request.
            if cancel_event.is_set():
                raise _ClientGone()
            event_queue.put({"stage": stage, **data})

        try:
            result = pipeline.process_task(
                task_text=body.task or (body.messages[0][:120] if body.messages else ""),
                incoming_messages=body.messages,
                prior_context=body.prior_context,
                complexity=body.complexity,
                urgency=body.urgency,
                compression_level=body.compression_level,
                prune_budget=body.prune_budget,
                delta_mode=body.delta_mode,
                wire_mode=body.wire_mode,
                progress_callback=callback,
            )

            if cancel_event.is_set():
                return

            compressed_msgs = result.debug.get("compressed_messages", [])
            pruned_ctx      = result.debug.get("pruned_context", [])
            baseline_tokens  = result.baseline_tokens
            output_tokens    = estimate_tokens_many(compressed_msgs) + estimate_tokens_many(pruned_ctx)
            actual_savings   = round(max(0.0, (1 - output_tokens / max(1, baseline_tokens)) * 100), 2)

            _store.record_usage(
                key_hash=kh,
                baseline_tokens=baseline_tokens,
                optimized_tokens=output_tokens,
                savings_pct=actual_savings,
                quality_proxy=result.quality_proxy,
                pipeline=body.pipeline,
                agent=body.agent,
                run_id=body.run_id,
            )

            event_queue.put({"stage": "done", "result": {
                "compressed_messages": compressed_msgs,
                "pruned_context":      pruned_ctx,
                "baseline_tokens":     baseline_tokens,
                "optimized_tokens":    output_tokens,
                "savings_pct":         actual_savings,
                "quality_proxy":       round(result.quality_proxy, 4),
                "routed_model_hint":   result.routed_model,
                "model_response":      result.model_response,
                "state_id":            result.debug.get("state_id", ""),
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
    session_id:       str   = Field(default="", max_length=128)
    pipeline:         str   = Field(default="", max_length=100)
    agent:            str   = Field(default="", max_length=100)
    run_id:           str   = Field(default="", max_length=128)


@app.post("/v1/usage")
@limiter.limit("300/minute")
def report_usage(request: Request, body: UsageReportRequest, kh: str = Depends(_authenticated)):
    tokens_saved  = max(0, body.baseline_tokens - body.compressed_tokens)
    savings_pct   = round((tokens_saved / max(1, body.baseline_tokens)) * 100, 2)

    # Phase 3: Only bill savings if quality passes gate
    quality_floor = 0.8  # Default floor; configurable per customer
    quality_verified = body.quality_score is not None and body.quality_score >= quality_floor

    if quality_verified:
        # Quality gate passed: bill the full savings
        cost_saved = cost_for_tokens(body.provider, body.model, tokens_saved)
        fee = round(cost_saved * 0.10, 8)
        quality_status = "verified"
    else:
        # Quality not verified or below floor: don't bill savings
        cost_saved = 0.0
        fee = 0.0
        quality_status = "unverified" if body.quality_score is None else "failed"
        tokens_saved = 0  # Don't report token savings if quality fails

    # Use real quality score if provided, else fallback to 1.0 for legacy compatibility
    actual_quality = body.quality_score if body.quality_score is not None else 1.0

    _store.record_usage(
        key_hash=kh,
        baseline_tokens=body.baseline_tokens,
        optimized_tokens=body.compressed_tokens,
        savings_pct=savings_pct,
        quality_proxy=actual_quality,
        provider=body.provider,
        model=body.model,
        cost_saved_usd=cost_saved,
        brevitas_fee_usd=fee,
        session_id=body.session_id,
        pipeline=body.pipeline,
        agent=body.agent,
        run_id=body.run_id,
    )
    return {
        "tokens_saved": tokens_saved,
        "savings_pct": savings_pct if quality_verified else 0.0,
        "cost_saved_usd": round(cost_saved, 6),
        "brevitas_fee_usd": round(fee, 6),
        "quality_score": actual_quality,
        "quality_status": quality_status,
    }


@app.get("/v1/provider-costs")
def provider_costs():
    return {"costs_per_1m_tokens": PROVIDER_COSTS_PER_1M}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/v1/stats")
@limiter.limit("120/minute")
def stats(request: Request, kh: str = Depends(_authenticated)):
    return _store.get_stats(kh)


@app.get("/v1/stats/pipelines")
@limiter.limit("120/minute")
def stats_pipelines(
    request: Request,
    kh: str = Depends(_authenticated),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    """Get aggregated stats by pipeline."""
    return _store.get_stats_by_pipeline(kh, start=start or "", end=end or "")


@app.get("/v1/stats/agents")
@limiter.limit("120/minute")
def stats_agents(
    request: Request,
    kh: str = Depends(_authenticated),
    pipeline: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    """Get aggregated stats by agent, optionally filtered by pipeline."""
    return _store.get_stats_by_agent(
        kh, pipeline=pipeline or "", start=start or "", end=end or ""
    )


@app.get("/v1/stats/runs")
@limiter.limit("120/minute")
def stats_runs(
    request: Request,
    kh: str = Depends(_authenticated),
    pipeline: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    """Get aggregated stats by run, optionally filtered by pipeline."""
    return _store.get_stats_by_run(
        kh, pipeline=pipeline or "", start=start or "", end=end or ""
    )


@app.get("/v1/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
