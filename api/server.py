# Run from repo root: uvicorn api.server:app --reload
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import concurrent.futures
import importlib
import json
import logging
import math
import queue
import re
import secrets
import sqlite3
import threading
import time as _time
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests as _requests
import httpx
from fastapi import FastAPI, HTTPException, Header, Depends, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from typing import Callable, FrozenSet, List, Optional

from token_efficiency_model.lossless.api_adapter import retrieval_select
from token_efficiency_model.lossless.provider_cache import count_tokens
from token_efficiency_model.lossless.message_optimizer import optimize_message_text

from brevitas.provider_reliability import (
    ProviderCircuitOpen,
    close_provider_sync_clients,
    provider_sync_http,
)
from brevitas.resource_bounds import BoundedTTLMap, ResourceBounds
from brevitas.security import (
    EnvelopeCipher,
    EnvelopeError,
    KMSConfigurationError,
    KMSReadinessMonitor,
    KMSUnavailable,
    ManagedKMS,
)
from brevitas.observability import documented_upstream_outage_active

from .company_admin import (
    COMPANY_ROLES,
    CompanyPrincipal,
    company_admin_for_store,
    configure_company_admin,
    router as company_admin_router,
    service_account_key_context,
)
from .compliance_admin import (
    ComplianceAdminPrincipal,
    SupabaseComplianceAdminService,
    configure_compliance_admin,
    router as compliance_admin_router,
)
from .distributed_limits import DistributedLimiter, LimitIdentity, LimiterUnavailable
from .jobs import (
    InMemoryJobStore, JobCrypto, JobRequest, JobService, JobTenant,
    RedisJobDispatcher, SQLiteJobStore, SupabaseJobStore,
)
from .observability import (
    graceful_observability_shutdown,
    install_fastapi_observability,
    mark_documented_upstream_outage,
)
from .security import credential_cipher_from_environment
from .runtime import hosted_runtime

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
from .build_info import build_identity, validate_production_build_identity
from brevitas.receipts import TokenReceipt, calculate_costs, normalize_usage, MODEL_PRICES
from brevitas.identity import CUSTOMER_ID_HEADER, normalize_customer_id, tenant_key
from .store import make_store, PROVIDER_COSTS_PER_1M
from brevitas.semantic_cache import make_semantic_cache

# ── Encryption ───────────────────────────────────────────────────────────────

_RESOURCE_BOUNDS = ResourceBounds.from_env()
_managed_kms_adapter: ManagedKMS | None = None
_legacy_credential_keys: tuple[str | bytes, ...] = ()
_credential_cipher: EnvelopeCipher | None = None
_kms_readiness_monitor = KMSReadinessMonitor(clock=_time.monotonic)
_managed_kms_factories: dict[str, object] = {}
_KMS_FACTORY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_KMS_MODULE_NAME = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){1,15}$")


def register_managed_kms_factory(name: str, factory) -> None:
    """Register a trusted deployment-owned, zero-argument KMS adapter factory."""
    if not _KMS_FACTORY_NAME.fullmatch(str(name or "")) or not callable(factory):
        raise KMSConfigurationError("managed KMS factory registration is invalid")
    if name not in _managed_kms_factories and len(_managed_kms_factories) >= 32:
        raise KMSConfigurationError("managed KMS factory registry is at capacity")
    _managed_kms_factories[name] = factory


def _configure_managed_kms_from_deployment() -> None:
    """Load one explicitly trusted adapter before readiness can become true.

    A deployment may register a factory in-process or name an exact allowlisted
    module and callable. Arbitrary dotted imports are rejected.
    """
    if _managed_kms_adapter is not None:
        return
    specification = os.getenv("BREVITAS_KMS_ADAPTER_FACTORY", "").strip()
    if not specification:
        return
    if len(specification) > 256:
        raise KMSConfigurationError("managed KMS adapter factory is invalid")
    factory = None
    if specification.startswith("registry:"):
        name = specification.partition(":")[2]
        if not _KMS_FACTORY_NAME.fullmatch(name):
            raise KMSConfigurationError("managed KMS registry factory is invalid")
        factory = _managed_kms_factories.get(name)
    else:
        module_name, separator, attribute = specification.partition(":")
        trusted_raw = os.getenv("BREVITAS_KMS_ADAPTER_TRUSTED_MODULES", "")
        if len(trusted_raw) > 2048:
            raise KMSConfigurationError("managed KMS module allowlist is invalid")
        trusted = {value.strip() for value in trusted_raw.split(",") if value.strip()}
        if len(trusted) > 32:
            raise KMSConfigurationError("managed KMS module allowlist is invalid")
        if (separator != ":" or not _KMS_MODULE_NAME.fullmatch(module_name)
                or not _KMS_FACTORY_NAME.fullmatch(attribute)
                or module_name not in trusted):
            raise KMSConfigurationError("managed KMS module factory is not trusted")
        try:
            factory = getattr(importlib.import_module(module_name), attribute)
        except Exception:
            raise KMSConfigurationError(
                "managed KMS module factory is unavailable") from None
    if not callable(factory):
        raise KMSConfigurationError("managed KMS adapter factory is unavailable")
    try:
        adapter = factory()
    except Exception:
        raise KMSConfigurationError("managed KMS adapter factory failed") from None
    if not isinstance(adapter, ManagedKMS) or not bool(adapter.is_managed):
        raise KMSConfigurationError("managed KMS adapter factory returned an invalid adapter")
    configure_managed_kms(adapter)


def configure_managed_kms(
    adapter: ManagedKMS,
    *,
    legacy_keys: tuple[str | bytes, ...] = (),
) -> None:
    """Deployment injection point for a real managed KMS adapter.

    Legacy keys are explicit decrypt-only migration inputs. This function does
    not accept or create a plaintext encryption fallback.
    """
    global _managed_kms_adapter, _legacy_credential_keys, _credential_cipher
    if _credential_cipher is not None:
        _credential_cipher.cache.clear()
    _managed_kms_adapter = adapter
    _legacy_credential_keys = tuple(legacy_keys)
    _credential_cipher = None
    _kms_readiness_monitor.reset()
    service = globals().get("_job_service")
    if service is not None:
        service.crypto = None


def _initialize_credential_cipher(*, required: bool = False) -> EnvelopeCipher | None:
    global _credential_cipher
    if _credential_cipher is not None:
        return _credential_cipher
    configured = _managed_kms_adapter is not None or any(os.getenv(name) for name in (
        "BREVITAS_KMS_PROVIDER", "BREVITAS_KMS_KEY_ID", "BREVITAS_KMS_KEY_VERSION",
        "BREVITAS_LOCAL_KMS_KEY", "BREVITAS_KMS_REQUIRED",
    ))
    if not configured and not required:
        return None
    _credential_cipher = credential_cipher_from_environment(
        adapter=_managed_kms_adapter,
        legacy_keys=_legacy_credential_keys,
    )
    service = globals().get("_job_service")
    if service is not None and getattr(service, "crypto", None) is None:
        service.configure_crypto(JobCrypto(_credential_cipher, bounds=_RESOURCE_BOUNDS))
    return _credential_cipher


def _require_credential_cipher() -> EnvelopeCipher:
    cipher = _initialize_credential_cipher(required=True)
    if cipher is None:  # pragma: no cover - required construction either returns or raises
        raise KMSConfigurationError("credential encryption is unavailable")
    return cipher


_KMS_CONFIGURATION_NAMES = (
    "BREVITAS_KMS_PROVIDER",
    "BREVITAS_KMS_KEY_ID",
    "BREVITAS_KMS_KEY_VERSION",
    "BREVITAS_LOCAL_KMS_KEY",
    "BREVITAS_KMS_REQUIRED",
    "BREVITAS_KMS_ADAPTER_FACTORY",
)


def _kms_is_configured() -> bool:
    return bool(
        _production_runtime()
        or _credential_cipher is not None
        or _managed_kms_adapter is not None
        or any(os.getenv(name) for name in _KMS_CONFIGURATION_NAMES)
    )


def _kms_readiness_bound(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        raise KMSConfigurationError("KMS readiness bound is invalid") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise KMSConfigurationError("KMS readiness bound is outside safe limits")
    return value


async def _kms_readiness_status() -> dict[str, bool]:
    """Return content-free, fail-closed active KMS readiness evidence."""
    configured = _kms_is_configured()
    unavailable = {
        "configured": configured,
        "active_probe": False,
        "fresh": False,
    }
    if not configured:
        return unavailable
    try:
        cipher = _initialize_credential_cipher(required=True)
        if cipher is None:
            return unavailable
        timeout = _kms_readiness_bound(
            "BREVITAS_KMS_READINESS_TIMEOUT_SECONDS", 1.0, 0.05, 10.0
        )
        max_age = _kms_readiness_bound(
            "BREVITAS_KMS_READINESS_MAX_AGE_SECONDS", 30.0, 1.0, 300.0
        )
        result = await _kms_readiness_monitor.check(
            cipher,
            timeout_seconds=timeout,
            max_age_seconds=max_age,
        )
    except (Exception, asyncio.TimeoutError):
        return unavailable
    return {
        "configured": True,
        "active_probe": result.ready,
        "fresh": result.fresh,
    }


def _kms_dependency_ready(status: dict[str, bool]) -> bool:
    return not status["configured"] or bool(
        status["active_probe"] and status["fresh"]
    )


_CREDENTIAL_DEPENDENCY_ERRORS = (
    EnvelopeError, KMSConfigurationError, KMSUnavailable,
)


def _credential_dependency_unavailable(exc: Exception) -> HTTPException:
    logger.error("credential_dependency_unavailable error_type=%s", type(exc).__name__)
    return HTTPException(
        status_code=503,
        detail="Credential security dependency unavailable",
        headers={"Retry-After": "1"},
    )


def _encrypt(value: str, *, context: dict[str, str]) -> str:
    if not value:
        return ""
    return _require_credential_cipher().encrypt_text(value, context=context)


def _decrypt(value: str, *, context: dict[str, str]) -> str:
    if not value:
        return ""
    return _require_credential_cipher().decrypt_text(value, context=context)


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

# Playground response cache — repeated questions skip the model call entirely (≈100%
# savings on that turn). Lazy singleton; any failure disables it so a cache issue can never
# break the endpoint. The EXACT-hash layer (byte-identical repeats) is safe and on. The
# fuzzy SEMANTIC layer is NOT auto-enabled just because an embed model is present: cosine
# similarity alone does not prove answer equivalence, so a reworded match could replay a
# wrong answer. It requires the explicit BREVITAS_SEMANTIC_CACHE opt-in (fail-closed).
_playground_cache = None
_playground_cache_init = False


def _get_playground_cache(request: Request | None = None):
    global _playground_cache, _playground_cache_init
    if request is None or not bool(getattr(request.state, "brevitas_cache_enabled", False)):
        return None
    if os.getenv("BREVITAS_CACHE_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return None
    if not _playground_cache_init:
        _playground_cache_init = True
        try:
            _playground_cache = make_semantic_cache()
        except Exception as exc:  # pragma: no cover — cache is best-effort
            logger.warning("Playground cache disabled: %s", type(exc).__name__)
            _playground_cache = None
    return _playground_cache


# Saved tokens are priced at a reference paid model (the free default model is $0), clearly
# labeled in the UI as an estimate — never a charge.
_PLAYGROUND_PRICE_MODEL = os.getenv("BREVITAS_PLAYGROUND_PRICE_MODEL", "gpt-4o")
_PLAYGROUND_PRICE = MODEL_PRICES.get(("openai", _PLAYGROUND_PRICE_MODEL), {"input": 2.5, "output": 10.0})
_provider_call_condition = threading.Condition()
_provider_calls_active = 0
_provider_backend_context: ContextVar[tuple[str, Request | None]] = ContextVar(
    "brevitas_provider_backend_context", default=("", None))


@contextmanager
def _provider_call():
    """Track synchronous provider work so shutdown never closes an in-use pool."""
    global _provider_calls_active
    with _provider_call_condition:
        _provider_calls_active += 1
    try:
        yield
    finally:
        with _provider_call_condition:
            _provider_calls_active = max(0, _provider_calls_active - 1)
            _provider_call_condition.notify_all()


def _wait_for_provider_calls(timeout: float) -> bool:
    deadline = _time.monotonic() + max(0.0, timeout)
    with _provider_call_condition:
        while _provider_calls_active:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return False
            _provider_call_condition.wait(remaining)
        return True


def _provider_unavailable(exc: Exception, label: str) -> HTTPException:
    if isinstance(exc, ProviderCircuitOpen):
        retry_after = max(1, int(exc.retry_after_s + 0.999))
        return ProviderRequestNotAccepted(
            status_code=503,
            detail=f"{label} temporarily unavailable",
            headers={"Retry-After": str(retry_after)},
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = int(getattr(exc.response, "status_code", 0) or 0)
        if status == 429:
            # A rate-limit response is a definite rejection; no model work was
            # accepted, so a later durable attempt is safe.
            return ProviderRequestNotAccepted(
                status_code=502, detail=f"{label} request failed",
            )
        if status >= 500 or status in {408, 409, 425}:
            return ProviderOutcomeAmbiguous(label)
        return ProviderRequestRejected(label)
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        # These fail before request bytes can be accepted by the provider.
        return ProviderRequestNotAccepted(
            status_code=502, detail=f"{label} request failed",
        )
    if isinstance(exc, httpx.TransportError):
        return ProviderOutcomeAmbiguous(label)
    return HTTPException(status_code=502, detail=f"{label} request failed")


class ProviderOutcomeAmbiguous(HTTPException):
    """A provider may have accepted billable work but returned no usable result."""

    job_retryable = False

    def __init__(self, label: str) -> None:
        super().__init__(status_code=502, detail=f"{label} request failed")


class ProviderRequestNotAccepted(HTTPException):
    """A retryable outcome proven to precede provider request acceptance."""

    job_retryable = True
    provider_outbound_not_accepted = True


class ProviderRequestRejected(HTTPException):
    """A non-transient provider rejection that a queue retry cannot repair."""

    job_retryable = False
    provider_outbound_not_accepted = True

    def __init__(self, label: str) -> None:
        super().__init__(status_code=502, detail=f"{label} request failed")


def _provider_output_token_limit(request: Request | None) -> int:
    """Apply a caller-requested ceiling without permitting an unbounded model call."""
    raw = (request.headers.get("x-brevitas-max-output-tokens", "")
           if request is not None else "")
    if raw and not re.fullmatch(r"[1-9][0-9]{0,3}", raw):
        raise HTTPException(status_code=400, detail="Invalid model output limit")
    limit = int(raw) if raw else 1024
    if limit > 1024:
        raise HTTPException(status_code=400, detail="Invalid model output limit")
    return limit


def _price_usd(input_tokens: int, output_tokens: int) -> float:
    """Reference-rate dollar value of saved tokens (input + output)."""
    return round(
        max(0, input_tokens) * _PLAYGROUND_PRICE["input"] / 1_000_000
        + max(0, output_tokens) * _PLAYGROUND_PRICE["output"] / 1_000_000,
        6,
    )


def _mark_documented_outage(request: Request | None, provider: str) -> None:
    if request is not None and documented_upstream_outage_active(provider):
        mark_documented_upstream_outage(request, provider)


def _make_ollama_backend(model: str, request: Request | None = None):
    def backend(prompt: str, _routed: str) -> str:
        try:
            with _provider_call():
                resp = provider_sync_http.request(
                    "ollama", "generate", "POST", f"{_OLLAMA_HOST}/api/generate",
                    json={
                        "model": model, "prompt": prompt, "stream": False,
                        "options": {"num_predict": _provider_output_token_limit(request)},
                    },
                )
                try:
                    resp.raise_for_status()
                    data = resp.json()
                    backend.last_complete = str(data.get("done_reason") or "stop") == "stop"
                    return data.get("response", "")
                finally:
                    resp.close()
        except (ProviderCircuitOpen, httpx.HTTPError) as exc:
            _mark_documented_outage(request, "ollama")
            raise _provider_unavailable(exc, "Ollama") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderOutcomeAmbiguous("Ollama") from exc
    backend.last_complete = True
    return backend


def _make_anthropic_backend(api_key: str, model: str, request: Request | None = None):
    def backend(prompt: str, _routed: str) -> str:
        try:
            with _provider_call():
                resp = provider_sync_http.request(
                    "anthropic", "messages", "POST",
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": _provider_output_token_limit(request),
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                try:
                    resp.raise_for_status()
                    data = resp.json()
                    backend.last_complete = str(data.get("stop_reason") or "") in (
                        "end_turn", "stop_sequence")
                    return data["content"][0]["text"]
                finally:
                    resp.close()
        except (ProviderCircuitOpen, httpx.HTTPError) as exc:
            _mark_documented_outage(request, "anthropic")
            raise _provider_unavailable(exc, "Anthropic") from exc
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ProviderOutcomeAmbiguous("Anthropic") from exc
    backend.last_complete = True
    return backend


def _make_openai_compat_backend(provider: str, api_key: str, model: str, base_url: str,
                                request: Request | None = None):
    def backend(prompt: str, _routed: str) -> str:
        try:
            with _provider_call():
                token_field = ("max_completion_tokens"
                               if provider == "openai" and model.startswith("o")
                               else "max_tokens")
                resp = provider_sync_http.request(
                    provider, "chat.completions", "POST", f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        token_field: _provider_output_token_limit(request),
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                try:
                    resp.raise_for_status()
                    choice = resp.json()["choices"][0]
                    backend.last_complete = str(choice.get("finish_reason") or "") == "stop"
                    return choice["message"]["content"]
                finally:
                    resp.close()
        except (ProviderCircuitOpen, httpx.HTTPError) as exc:
            _mark_documented_outage(request, provider)
            raise _provider_unavailable(exc, "Model provider") from exc
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ProviderOutcomeAmbiguous("Model provider") from exc
    backend.last_complete = True
    return backend


def _noop_backend(prompt: str, _routed: str) -> str:
    return ""


def _build_backend(config: dict | None):
    if config is None:
        return _noop_backend  # no model configured — skip the call, don't hit localhost
    key_hash, request = _provider_backend_context.get()
    if not key_hash:
        raise RuntimeError("provider credential context is unavailable")
    provider = config["provider"]
    try:
        api_key = _decrypt(config["provider_api_key"], context={
            "purpose": "provider_credential", "key_hash": key_hash,
        })
    except _CREDENTIAL_DEPENDENCY_ERRORS as exc:
        raise _credential_dependency_unavailable(exc) from exc
    model    = config["model"]
    if provider == "ollama":
        return _make_ollama_backend(model, request)
    if provider == "anthropic":
        return _make_anthropic_backend(api_key, model, request)
    if provider in _PROVIDER_BASE_URLS:
        return _make_openai_compat_backend(
            provider, api_key, model, _PROVIDER_BASE_URLS[provider], request)
    return _noop_backend


def _compress_pipeline(task: str, messages: list[str], prior_context: list[str],
                       prune_budget: int, lossy: bool, retrieval: bool = False,
                       key_hash: str = "") -> dict:
    """Shared context-reduction core used by /v1/compress, /v1/compress/stream and
    /v1/playground/stream. Messages pass through unchanged except the volatile LAST message,
    which is lossily shrunk when `lossy` is on and the remote compressor is available.
    prior_context is retrieval-pruned to the chunks relevant to `task` ONLY when `retrieval`
    is explicitly enabled; otherwise it passes through whole. All savings use the real
    tokenizer; no quality number is ever fabricated (quality_sim is None unless measured).

    Reports `faithful`: True only when the returned request is byte-identical to the input
    (no lossy rewrite, no pruning), so callers know whether the answer is safe to cache."""
    from token_efficiency_model.quality.gate import lever_allowed
    # Fail-closed gate, per tenant: a lever runs only if the operator opted in AND this
    # tenant's lever has not tripped. Absence of approval / a tripped stream => full context.
    if retrieval and not lever_allowed("retrieval", key_hash):
        retrieval = False
    if lossy and not lever_allowed("compression", key_hash):
        lossy = False
    if retrieval:
        sel = retrieval_select(task, prior_context, k=prune_budget, use_adaptive=True)
    else:
        # Retrieval off (default): send the full prior context, byte-identical.
        ctx_tokens = estimate_tokens_many(prior_context)
        sel = {"selected_context": list(prior_context), "baseline_tokens": ctx_tokens,
               "optimized_tokens": ctx_tokens, "fallback_applied": True,
               "reason": "retrieval_disabled"}
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
    # Byte-faithful iff nothing was rewritten or dropped: the last message is unchanged
    # AND the context we return equals the full input context.
    faithful = (out_messages == list(messages)
                and sel["selected_context"] == list(prior_context))
    return {
        "out_messages":       out_messages,
        "selected_context":   sel["selected_context"],
        "faithful":           faithful,
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


def _make_named_backend(provider: str, model: str, raw_key: str,
                        request: Request | None = None):
    """Build a one-shot model backend from a provider id + RAW key (no encryption, no store).
    Used for ephemeral Playground keys and the server-side free default."""
    if provider == "ollama":
        return _make_ollama_backend(model, request)
    if provider == "anthropic":
        return _make_anthropic_backend(raw_key, model, request)
    if provider in _PROVIDER_BASE_URLS:
        return _make_openai_compat_backend(
            provider, raw_key, model, _PROVIDER_BASE_URLS[provider], request)
    return _noop_backend


def _build_chat_backend(byok_provider: str, byok_model: str, byok_key: str,
                        request: Request | None = None):
    """Resolve the model backend for a Playground chat turn. Priority:
      1. bring-your-own ephemeral key from the request (never stored, never logged),
      2. the server-side free default (BREVITAS_PLAYGROUND_KEY),
      3. no model — compression-only (empty response).
    Returns (provider, model, backend)."""
    if byok_key and byok_provider and byok_model:
        allowed = _PROVIDER_MODELS.get(byok_provider)
        if not allowed or byok_model not in allowed:
            raise HTTPException(status_code=502, detail="Unsupported provider or model for chat")
        return (byok_provider, byok_model,
                _make_named_backend(byok_provider, byok_model, byok_key, request))
    if _PLAYGROUND_KEY:
        return (_PLAYGROUND_PROVIDER, _PLAYGROUND_MODEL,
                _make_named_backend(
                    _PLAYGROUND_PROVIDER, _PLAYGROUND_MODEL, _PLAYGROUND_KEY, request))
    return "", "", _noop_backend


def _provider_config_unavailable(exc: Exception) -> HTTPException:
    logger.error("provider configuration unavailable error_type=%s",
                 type(exc).__name__)
    return HTTPException(
        status_code=503, detail="Provider configuration unavailable",
        headers={"Retry-After": "1"},
    )


def _provider_config_for_key(kh: str) -> dict | None:
    try:
        config = _store.get_provider_config(kh)
    except Exception as exc:
        raise _provider_config_unavailable(exc) from exc
    if config is None:
        return None
    if not isinstance(config, dict):
        raise _provider_config_unavailable(
            RuntimeError("invalid provider configuration response"))
    provider = config.get("provider")
    model = config.get("model")
    encrypted_key = config.get("provider_api_key")
    if (not isinstance(provider, str)
            or not isinstance(model, str)
            or not isinstance(encrypted_key, str)
            or model not in (_PROVIDER_MODELS.get(provider) or [])):
        raise _provider_config_unavailable(
            RuntimeError("invalid provider configuration response"))
    return config


def _resolve_configured_model_backend(
    kh: str, request: Request | None = None,
) -> tuple[dict | None, Callable[[str, str], str]]:
    """Read and decrypt a saved provider configuration before starting work.

    Streaming callers use this as a preflight so a KMS failure is returned as a
    normal retryable HTTP response instead of being hidden inside a 200/SSE body.
    The returned backend owns the already-decrypted credential for this request;
    callers must not persist or log it.
    """
    config = _provider_config_for_key(kh)
    if not config:
        return None, _noop_backend
    token = _provider_backend_context.set((kh, request))
    try:
        backend = _build_backend(config)
    finally:
        _provider_backend_context.reset(token)
    return config, backend


def _run_configured_model(
    kh: str,
    messages: list[str],
    context: list[str],
    task: str,
    request: Request | None = None,
    *,
    resolved_config: dict | None = None,
    resolved_backend: Callable[[str, str], str] | None = None,
) -> dict:
    if resolved_backend is None:
        config, backend = _resolve_configured_model_backend(kh, request)
    else:
        config, backend = resolved_config, resolved_backend
    if not config:
        return {"provider": "", "model": "", "model_response": ""}
    prompt = "\n\n".join(filter(None, [f"Task: {task}" if task else "", *messages, *context]))
    response = backend(prompt, config["model"])
    return {
        "provider": config["provider"],
        "model": config["model"],
        "model_response": response,
    }


# ── Rate limiting ─────────────────────────────────────────────────────────────

def _rate_key(request: Request) -> str:
    raw = request.headers.get("X-Brevitas-Key") or request.headers.get("X-API-Key")
    return hash_key(raw) if raw else (request.client.host if request.client else "unknown")

limiter = Limiter(key_func=_rate_key)


# ── App setup ─────────────────────────────────────────────────────────────────

_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]


def _proxy_auth_enabled() -> bool:
    return os.getenv("BREVITAS_PROXY_AUTH", "true").lower() not in {"0", "false", "no"}


def _validate_runtime_config() -> None:
    if not _production_runtime():
        return
    if not _proxy_auth_enabled():
        raise RuntimeError("Production requires BREVITAS_PROXY_AUTH=true")
    if len(os.getenv("COMPANY_ADMIN_CURSOR_SECRET", "")) < 32:
        raise RuntimeError(
            "Production COMPANY_ADMIN_CURSOR_SECRET must be at least 32 characters")
    compressor_url = os.getenv("BREVITAS_COMPRESS_URL", "").strip().rstrip("/")
    if compressor_url and not _private_compressor_url(compressor_url):
        raise RuntimeError(
            "Production BREVITAS_COMPRESS_URL must use Railway private networking")
    if compressor_url and not os.getenv("BREVITAS_COMPRESS_TOKEN", "").strip():
        raise RuntimeError(
            "Production compressor configuration requires BREVITAS_COMPRESS_TOKEN")


@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    app.state.accepting_traffic = False
    validate_production_build_identity(_production_runtime())
    _validate_runtime_config()
    _configure_managed_kms_from_deployment()
    _initialize_credential_cipher(required=_production_runtime())
    _configure_company_admin_runtime()
    _configure_compliance_admin_runtime()
    compressor = await _compressor_status()
    _warn_if_compressor_missing(compressor)
    app.state.accepting_traffic = True
    try:
        yield
    finally:
        # Uvicorn enters lifespan shutdown after it has stopped accepting and drained HTTP
        # requests. This state protects teardown; it is not a pre-stop load-balancer signal.
        app.state.accepting_traffic = False
        provider_drain = max(
            0.0, float(os.getenv("BREVITAS_PROVIDER_CLOSE_DRAIN_SECONDS", "10")))
        provider_drained = await asyncio.to_thread(
            _wait_for_provider_calls, provider_drain)
        if provider_drained:
            await asyncio.to_thread(close_provider_sync_clients)
        else:
            # Do not close a shared client underneath a still-running request thread.
            logger.warning("provider client close skipped because request threads are active")
        clients = {
            id(client): client for client in (
                getattr(_distributed_limiter, "redis", None),
                getattr(_job_service.dispatcher, "redis", None),
            ) if client is not None
        }
        for client in clients.values():
            closer = getattr(client, "aclose", None)
            if closer is not None:
                with suppress(Exception):
                    await closer()
        cipher = _credential_cipher
        if cipher is not None:
            cipher.cache.clear()
        graceful_observability_shutdown()


app = FastAPI(title="Brevitas API", version="1.0.0", docs_url=None, redoc_url=None,
              lifespan=_lifespan)
app.state.limiter = limiter
app.state.accepting_traffic = False
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=_ALLOWED_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Brevitas-Request-ID", "X-Request-ID"],
)


def _request_collection_exceeds(value: object, maximum: int) -> bool:
    if isinstance(value, list):
        return len(value) > maximum or any(
            _request_collection_exceeds(item, maximum) for item in value)
    if isinstance(value, dict):
        return len(value) > maximum or any(
            _request_collection_exceeds(item, maximum) for item in value.values())
    return False


class _AggregateRequestBoundsMiddleware:
    """Cap the actual ASGI body stream before any outer handler materializes it."""

    def __init__(self, application, *, max_bytes: int, max_items: int):
        self.application = application
        self.max_bytes = max_bytes
        self.max_items = max_items

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http"
                or scope.get("method") not in {"POST", "PUT", "PATCH"}):
            await self.application(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        raw_length = headers.get("content-length", "")
        if raw_length:
            try:
                parsed_length = int(raw_length)
                if parsed_length < 0:
                    raise ValueError
                if parsed_length > self.max_bytes:
                    response = JSONResponse(
                        {"detail": "Request body too large"}, status_code=413)
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse(
                    {"detail": "Invalid Content-Length"}, status_code=400)
                await response(scope, receive, send)
                return

        messages = []
        body = bytearray()
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") != "http.request":
                break
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                response = JSONResponse(
                    {"detail": "Request body too large"}, status_code=413)
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if body and content_type == "application/json":
            try:
                value = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            if value is not None and _request_collection_exceeds(value, self.max_items):
                response = JSONResponse(
                    {"detail": "Request contains too many items"}, status_code=413)
                await response(scope, receive, send)
                return

        position = 0

        async def replay():
            nonlocal position
            if position >= len(messages):
                return await receive()
            message = messages[position]
            position += 1
            return message

        await self.application(scope, replay, send)


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
        too_large = content_length and int(content_length) > _RESOURCE_BOUNDS.request_max_bytes
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
    if too_large:
        return JSONResponse(status_code=413, content={"detail": "Request body too large"})
    if not content_length and request.method in {"POST", "PUT", "PATCH"}:
        if len(await request.body()) > _RESOURCE_BOUNDS.request_max_bytes:
            return JSONResponse(status_code=413, content={"detail": "Request body too large"})
    return await call_next(request)


_store = make_store()
_distributed_limiter = DistributedLimiter()
_job_store = (SupabaseJobStore(_store) if hasattr(_store, "_request")
              else SQLiteJobStore(_store) if hasattr(_store, "_conn")
              else InMemoryJobStore(bounds=_RESOURCE_BOUNDS))
_job_service = JobService(
    _job_store, dispatcher=RedisJobDispatcher(bounds=_RESOURCE_BOUNDS),
    lease_seconds=int(os.getenv("BREVITAS_JOB_LEASE_SECONDS", "180")),
    bounds=_RESOURCE_BOUNDS,
)
_valid_key_cache = BoundedTTLMap[str, bool](
    ttl_s=min(30, _RESOURCE_BOUNDS.registry_ttl_s),
    max_entries=_RESOURCE_BOUNDS.registry_max_entries,
    max_value_bytes=16,
    sizer=lambda _value: 1,
    copier=lambda value: value,
)
_valid_key_lock = threading.Lock()
_auth_context_cache = BoundedTTLMap[tuple[str, str], "AuthContext"](
    ttl_s=min(30, _RESOURCE_BOUNDS.registry_ttl_s),
    max_entries=_RESOURCE_BOUNDS.registry_max_entries,
    max_value_bytes=_RESOURCE_BOUNDS.registry_max_value_bytes,
    copier=lambda value: value,
    snapshotter=lambda value: value,
)
_auth_context_lock = threading.Lock()
_proxy_windows = BoundedTTLMap[str, list[float]](
    ttl_s=_RESOURCE_BOUNDS.registry_ttl_s,
    max_entries=_RESOURCE_BOUNDS.registry_max_entries,
    max_value_bytes=_RESOURCE_BOUNDS.registry_max_value_bytes,
)
_proxy_active = BoundedTTLMap[str, int](
    ttl_s=_RESOURCE_BOUNDS.registry_ttl_s,
    max_entries=_RESOURCE_BOUNDS.registry_max_entries,
    max_value_bytes=32,
    sizer=lambda _value: 8,
    copier=lambda value: value,
)
_proxy_limit_lock = threading.Lock()
_PROXY_PATHS = {"/v1/messages", "/v1/chat/completions", "/openai/v1/chat/completions",
                "/openai/chat/completions",
                "/v1/responses", "/openai/v1/responses", "/v1/embeddings",
                "/openai/responses", "/openai/embeddings", "/openai/completions",
                "/openai/v1/embeddings", "/v1/completions", "/openai/v1/completions"}

_CUSTOMER_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


@dataclass(frozen=True)
class AuthContext:
    key_hash: str
    organization_id: str = ""
    billing_owner_id: str = ""
    customer_id: str = ""
    customer_external_id: str = ""
    service_account_id: str = ""
    actor_user_id: str = ""
    key_type: str = "legacy"
    scopes: FrozenSet[str] = frozenset()
    environment: str = ""

    def permits(self, scope: str) -> bool:
        return scope in self.scopes or "*" in self.scopes


_proxy_auth_context: ContextVar[AuthContext | None] = ContextVar(
    "brevitas_proxy_auth_context", default=None)


def _authoritative_service_key_context(kh: str) -> dict | None:
    try:
        return service_account_key_context(_store, kh)
    except sqlite3.OperationalError:
        # Local databases created before the company-admin module was composed
        # receive its additive development schema before authorization retries.
        if hasattr(_store, "_request") or not getattr(_store, "db_path", ""):
            raise
        company_admin_for_store(_store)
        return service_account_key_context(_store, kh)


def _require_current_dashboard_membership(context: AuthContext) -> None:
    """Revalidate a dashboard-session key's exact human membership every request."""
    if context.key_type != "dashboard_session":
        return
    if not context.actor_user_id or not context.organization_id:
        raise HTTPException(status_code=403, detail="Active company membership required")
    resolver = getattr(_store, "resolve_device_approval_organization", None)
    if not callable(resolver):
        raise HTTPException(
            status_code=503, detail="Membership verification unavailable",
            headers={"Retry-After": "1"},
        )
    try:
        membership = resolver(context.actor_user_id, context.organization_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=403, detail="Active company membership required") from exc
    except Exception as exc:
        logger.error("dashboard membership verification unavailable error_type=%s",
                     type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Membership verification unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    membership_id = str(
        membership.get("id") if isinstance(membership, dict) else "")
    membership_role = _canonical_company_role(
        membership.get("role") if isinstance(membership, dict) else "")
    if (membership_id != context.organization_id
            or membership_role not in COMPANY_ROLES):
        logger.error("dashboard membership resolver returned unsafe result")
        raise HTTPException(
            status_code=503, detail="Membership verification unavailable",
            headers={"Retry-After": "1"},
        )


def _auth_context_for_key(kh: str, customer_external_id: str = "") -> AuthContext:
    external_id = customer_external_id.strip()
    cache_key = (kh, external_id)
    with _auth_context_lock:
        cached = _auth_context_cache.get(cache_key)
        # Service-key billing ownership and dashboard-session revocation/expiry
        # can change independently of this process. Re-resolve both types so a
        # rotated browser tab cannot spend the generic auth-cache TTL as valid.
        if cached is not None and cached.key_type not in (
                "organization_service", "dashboard_session"):
            _require_current_dashboard_membership(cached)
            return cached
    row = _store.key_context(kh)
    if not row:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if str(row.get("key_type") or "") == "organization_service":
        authoritative = _authoritative_service_key_context(kh)
        if not authoritative:
            raise HTTPException(status_code=401, detail="Invalid API key")
        if (str(authoritative.get("organization_id") or "")
                != str(row.get("organization_id") or "")
                or str(authoritative.get("service_account_id") or "")
                != str(row.get("service_account_id") or "")):
            raise HTTPException(status_code=401, detail="Invalid API key")
        row = {**row, **authoritative}
    scopes = frozenset(row.get("scopes") or [])
    organization_id = str(row.get("organization_id") or "")
    customer_id = ""
    if external_id:
        if not _CUSTOMER_EXTERNAL_ID.fullmatch(external_id):
            raise HTTPException(status_code=400, detail="Invalid X-Brevitas-Customer-ID")
        if not organization_id or "customer:route" not in scopes:
            raise HTTPException(status_code=403, detail="Key cannot route customer traffic")
        customer = _store.find_customer(organization_id, external_id)
        if customer is None:
            if "customer:auto_provision" not in scopes:
                raise HTTPException(status_code=404, detail="Customer is not registered")
            customer = _store.upsert_customer(organization_id, external_id)
        if customer.get("status") != "active":
            raise HTTPException(status_code=403, detail="Customer is not active")
        customer_id = str(customer["id"])
    context = AuthContext(
        key_hash=kh, organization_id=organization_id,
        billing_owner_id=str(row.get("owner_id") or ""), customer_id=customer_id,
        customer_external_id=external_id,
        service_account_id=str(row.get("service_account_id") or ""),
        actor_user_id=(str(row.get("owner_id") or "")
                       if str(row.get("key_type") or "") == "dashboard_session"
                       else ""),
        key_type=str(row.get("key_type") or "legacy"), scopes=scopes,
        environment=str(row.get("environment") or ""),
    )
    _require_current_dashboard_membership(context)
    with _auth_context_lock:
        try:
            configured_cap = max(
                1, int(os.getenv(
                    "BREVITAS_AUTH_CONTEXT_CACHE_MAX",
                    str(_RESOURCE_BOUNDS.registry_max_entries),
                )),
            )
        except (TypeError, ValueError):
            configured_cap = _RESOURCE_BOUNDS.registry_max_entries
        _auth_context_cache.max_entries = min(
            _RESOURCE_BOUNDS.registry_max_entries, configured_cap)
        if context.key_type not in ("organization_service", "dashboard_session"):
            _auth_context_cache.put(cache_key, context)
    return context


def _require_scope(request: Request, kh: str, scope: str) -> AuthContext:
    context = _request_auth_context(request, kh)
    if not context.permits(scope):
        raise HTTPException(status_code=403, detail=f"Key lacks {scope} scope")
    return context


def _provider_bucket(path: str, raw_body: bytes) -> str:
    if path == "/v1/messages":
        return "anthropic"
    try:
        model = str((json.loads(raw_body) or {}).get("model") or "").lower()
    except (TypeError, ValueError, json.JSONDecodeError):
        return "all"
    if model.startswith("deepseek"):
        return "deepseek"
    if model.startswith(("grok", "xai")):
        return "xai"
    if model.startswith(("mistral", "codestral")):
        return "mistral"
    return "openai" if model else "all"


def _key_exists(kh: str) -> bool:
    with _valid_key_lock:
        if _valid_key_cache.get(kh, False):
            return True
    try:
        row = _store.key_context(kh)
        valid = bool(row)
        if valid and str(row.get("key_type") or "") == "organization_service":
            valid = _authoritative_service_key_context(kh) is not None
    except Exception:
        valid = False
    if valid:
        with _valid_key_lock:
            _valid_key_cache.put(kh, True)
    return valid


def _admission_renewal_interval(lease) -> float:
    """Renew well before expiry, including for the minimum one-second lease."""
    return max(0.1, float(lease._limiter.policy.lease_seconds) / 3)


async def _lease_guarded_body_iterator(original, lease, release_admission,
                                       cancellation_event: threading.Event):
    """Stop a response body immediately when its distributed lease is lost.

    Renewal failure is a loss of ownership, not an observability-only event. Each
    pending body read races the renewal guard so no later chunk is exposed after
    Redis reports a missing/expired member or cannot prove ownership.
    """
    iterator = original.__aiter__()
    lease_lost = asyncio.Event()
    next_chunk = None
    wait_for_loss = None

    async def renew_while_open():
        interval = _admission_renewal_interval(lease)
        while True:
            await asyncio.sleep(interval)
            try:
                owned = await lease.renew()
            except Exception:
                logger.error("distributed concurrency renewal failed; stream canceled")
                cancellation_event.set()
                lease_lost.set()
                return
            if not owned:
                logger.error("distributed concurrency lease lost; stream canceled")
                cancellation_event.set()
                lease_lost.set()
                return

    renewal = None
    try:
        # `call_next` may spend most of the original lease waiting for provider
        # headers. Re-prove ownership before exposing even the first body chunk.
        try:
            initially_owned = await lease.renew()
        except Exception:
            logger.error("distributed concurrency renewal failed; stream canceled")
            cancellation_event.set()
            lease_lost.set()
            return
        if not initially_owned:
            logger.error("distributed concurrency lease lost; stream canceled")
            cancellation_event.set()
            lease_lost.set()
            return
        renewal = asyncio.create_task(renew_while_open())
        while not lease_lost.is_set():
            next_chunk = asyncio.create_task(anext(iterator))
            wait_for_loss = asyncio.create_task(lease_lost.wait())
            await asyncio.wait(
                (next_chunk, wait_for_loss), return_when=asyncio.FIRST_COMPLETED,
            )
            if lease_lost.is_set():
                if not next_chunk.done():
                    next_chunk.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    await next_chunk
                next_chunk = None
                break

            wait_for_loss.cancel()
            with suppress(asyncio.CancelledError):
                await wait_for_loss
            wait_for_loss = None
            try:
                chunk = next_chunk.result()
            except StopAsyncIteration:
                next_chunk = None
                break
            next_chunk = None
            # Renewal can complete between the wait and result retrieval.
            if lease_lost.is_set():
                break
            yield chunk
    finally:
        cancellation_event.set()
        lease_lost.set()
        for task in (next_chunk, wait_for_loss, renewal):
            if task is not None and not task.done():
                task.cancel()
        for task in (next_chunk, wait_for_loss, renewal):
            if task is not None:
                with suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    await task
        close = getattr(iterator, "aclose", None)
        if close is not None:
            with suppress(asyncio.CancelledError, Exception):
                await close()
        await release_admission()


@app.middleware("http")
async def _protect_model_proxy(request: Request, call_next):
    if request.url.path not in _PROXY_PATHS:
        return await call_next(request)
    raw_key = request.headers.get("x-brevitas-key", "")
    if _production_runtime() and not _proxy_auth_enabled():
        return JSONResponse(status_code=503, content={"detail": "Proxy authentication unavailable"})
    if not raw_key and _proxy_auth_enabled():
        return JSONResponse(status_code=401, content={"detail": "Missing X-Brevitas-Key header"})
    if not raw_key and _production_runtime():
        return JSONResponse(status_code=503, content={"detail": "Proxy authentication unavailable"})
    kh = hash_key(raw_key) if raw_key else f"ip:{request.client.host if request.client else 'unknown'}"
    auth_context = None
    try:
        customer_external_id = normalize_customer_id(
            request.headers.get(CUSTOMER_ID_HEADER, ""))
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    request.state.brevitas_key_hash = kh
    request.state.brevitas_tenant_key = (
        tenant_key(raw_key, customer_external_id) if raw_key else kh)
    if raw_key:
        try:
            auth_context = await asyncio.to_thread(
                _auth_context_for_key, kh,
                customer_external_id)
            if not auth_context.permits("proxy:invoke"):
                return JSONResponse(status_code=403, content={"detail": "Key lacks proxy:invoke scope"})
            if auth_context.key_type == "organization_service" and not auth_context.customer_id:
                return JSONResponse(status_code=400, content={
                    "detail": "Organization service proxy calls require X-Brevitas-Customer-ID"
                })
            request.state.auth_context = auth_context
            request.state.brevitas_organization_id = auth_context.organization_id
            request.state.brevitas_customer_id = auth_context.customer_id
            request.state.brevitas_cache_enabled = await asyncio.to_thread(
                _store.cache_enabled, auth_context.organization_id, auth_context.customer_id)
            _proxy_auth_context.set(auth_context)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code, content={"detail": exc.detail},
                headers=exc.headers,
            )
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"detail": "Authentication store unavailable"},
                headers={"Retry-After": "1"},
            )
    lease = None
    local_admitted = False
    if auth_context:
        raw_body = await request.body()
        token_cost = max(1, count_tokens(raw_body.decode("utf-8", errors="ignore")))
        provider = _provider_bucket(request.url.path, raw_body)
        try:
            lease = await _distributed_limiter.acquire(
                LimitIdentity(
                    auth_context.organization_id or "legacy",
                    auth_context.customer_id or "unattributed",
                    kh,
                    provider,
                ),
                tokens=token_cost,
                request_id="",
            )
        except LimiterUnavailable:
            return JSONResponse(status_code=503,
                                content={"detail": "Admission control unavailable"},
                                headers={"Retry-After": "1"})
        if not lease.allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded", "limit": lease.reason},
                headers={
                    "Retry-After": str(lease.retry_after),
                    "X-RateLimit-Remaining": str(lease.remaining_requests),
                    "X-RateLimit-Reset": str(lease.reset_seconds),
                },
            )

    # Local fallback is development-only. Even a misconfigured/fake limiter must never put
    # production traffic into process-local admission state.
    if _production_runtime() and (lease is None or lease._limiter is None):
        return JSONResponse(status_code=503,
                            content={"detail": "Admission control unavailable"},
                            headers={"Retry-After": "1"})
    if lease is None or lease._limiter is None:
        now = _time.monotonic()
        rpm = int(os.getenv("BREVITAS_PROXY_RPM", "300"))
        concurrency = int(os.getenv("BREVITAS_PROXY_CONCURRENCY", "20"))
        with _proxy_limit_lock:
            window = _proxy_windows.get(kh, []) or []
            while window and now - window[0] >= 60:
                window.pop(0)
            active = int(_proxy_active.get(kh, 0) or 0)
            rpm_blocked = len(window) >= rpm
            concurrency_blocked = active >= concurrency
            if rpm_blocked or concurrency_blocked:
                retry_after = (max(1, int(61 - (now - window[0])))
                               if rpm_blocked and window else 1)
                return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"},
                                    headers={"Retry-After": str(retry_after)})
            window.append(now)
            _proxy_windows.put(kh, window)
            _proxy_active.put(kh, active + 1)
            local_admitted = True

    async def release_admission():
        if lease is not None and lease._limiter is not None:
            try:
                await lease.release()
            except LimiterUnavailable:
                logger.error("distributed concurrency release failed")
        if local_admitted:
            with _proxy_limit_lock:
                active = max(0, int(_proxy_active.get(kh, 0) or 0) - 1)
                if active:
                    _proxy_active.put(kh, active)
                else:
                    _proxy_active.pop(kh, None)

    admission_cancellation = threading.Event()
    request.state.brevitas_admission_cancellation = admission_cancellation
    try:
        response = await call_next(request)
    except Exception:
        await release_admission()
        raise
    if lease is not None:
        response.headers.setdefault("X-RateLimit-Remaining", str(lease.remaining_requests))
        response.headers.setdefault("X-RateLimit-Reset", str(lease.reset_seconds))
    original = response.body_iterator

    if lease is not None and lease._limiter is not None:
        response.body_iterator = _lease_guarded_body_iterator(
            original, lease, release_admission, admission_cancellation,
        )
    else:
        async def release_after_response():
            try:
                async for chunk in original:
                    yield chunk
            finally:
                admission_cancellation.set()
                await release_admission()

        response.body_iterator = release_after_response()
    return response


def _safe_record_usage(*, auth_context: AuthContext | None = None, **values) -> bool:
    """Telemetry is best-effort; it must never damage a model/compression response."""
    try:
        if auth_context is not None:
            values["organization_id"] = auth_context.organization_id
            values["customer_id"] = auth_context.customer_id
            if auth_context.billing_owner_id:
                values["owner_id"] = auth_context.billing_owner_id
        if "owner_id" not in values and values.get("key_hash"):
            values["owner_id"] = _store.key_owner(values["key_hash"])
        values.setdefault("authoritative", True)
        return bool(_store.record_usage(**values))
    except Exception as exc:
        logger.error("usage write failed: %s", type(exc).__name__)
        return False


def _authenticated(request: Request, x_api_key: Optional[str] = Header(None),
                   x_brevitas_key: Optional[str] = Header(None),
                   x_brevitas_customer_id: Optional[str] = Header(None)) -> str:
    key = x_brevitas_key or x_api_key
    if not key:
        raise HTTPException(status_code=401, detail="Missing X-Brevitas-Key header")
    kh = hash_key(key)
    try:
        customer_external_id = normalize_customer_id(x_brevitas_customer_id or "")
        context = _auth_context_for_key(kh, customer_external_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("API key store unavailable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Authentication store unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    request.state.auth_context = context
    request.state.brevitas_key_hash = kh
    request.state.brevitas_tenant_key = tenant_key(key, customer_external_id)
    request.state.brevitas_organization_id = context.organization_id
    request.state.brevitas_customer_id = context.customer_id
    try:
        request.state.brevitas_cache_enabled = _store.cache_enabled(
            context.organization_id, context.customer_id)
    except Exception as exc:
        logger.error("cache policy lookup unavailable error_type=%s",
                     type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Authentication store unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    return kh


def _request_auth_context(request: Request, kh: str) -> AuthContext:
    context = getattr(request.state, "auth_context", None)
    return context if isinstance(context, AuthContext) else _auth_context_for_key(kh)


def _request_tenant_key(request: Request, fallback_key: str) -> str:
    """Return middleware/auth tenant state with a safe local-test fallback.

    FastAPI dependency overrides used by embedders and tests can bypass `_authenticated`.
    Those calls are still isolated by the override's key instead of crashing because
    request state was not populated.
    """
    value = getattr(request.state, "brevitas_tenant_key", "")
    if value:
        return str(value)
    customer_id = normalize_customer_id(request.headers.get(CUSTOMER_ID_HEADER, ""))
    return tenant_key(fallback_key, customer_id) if customer_id else fallback_key


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
            if int(getattr(response, "status_code", 500)) >= 500:
                raise HTTPException(
                    status_code=503, detail="Authentication dependency unavailable",
                    headers={"Retry-After": "1"},
                )
            logger.warning("Supabase dashboard auth rejected status=%s", response.status_code)
            return {}
        identity = response.json()
        if not isinstance(identity, dict):
            raise ValueError("invalid dashboard identity response")
        return identity
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("dashboard authentication unavailable error_type=%s",
                     type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Authentication dependency unavailable",
            headers={"Retry-After": "1"},
        ) from exc


def _dashboard_user(request: Request) -> str:
    return str(_dashboard_identity(request).get("id") or "")


_COMPANY_ROLE_ALIASES = {
    "owner": "company_owner", "admin": "company_admin", "billing": "billing_admin",
}
_company_admin_service = None


def _canonical_company_role(role: object) -> str:
    value = str(role or "")
    return _COMPANY_ROLE_ALIASES.get(value, value)


def _active_company_membership(user_id: str) -> tuple[str, str]:
    try:
        organization = _store.member_organization(user_id)
    except Exception as exc:
        logger.error("company membership lookup unavailable error_type=%s",
                     type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Membership verification unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    if not organization:
        return "", ""
    organization_id = str(organization.get("id") or "")
    role = ""
    if hasattr(_store, "_request"):
        try:
            value = _store._request("POST", "rpc/lock_company_actor_role", data={
                "p_organization_id": organization_id,
                "p_actor_user_id": user_id,
            })
        except Exception as exc:
            logger.error("company membership validation unavailable error_type=%s",
                         type(exc).__name__)
            raise HTTPException(
                status_code=503, detail="Membership verification unavailable",
                headers={"Retry-After": "1"},
            ) from exc
        if isinstance(value, list):
            value = value[0] if value else ""
            if isinstance(value, dict):
                value = next(iter(value.values()), "")
        role = _canonical_company_role(value)
    else:
        db_path = getattr(_store, "db_path", "")
        if db_path:
            with sqlite3.connect(str(db_path)) as db:
                row = db.execute(
                    "SELECT role,status FROM organization_members "
                    "WHERE organization_id=? AND user_id=? LIMIT 1",
                    (organization_id, user_id),
                ).fetchone()
            if row and str(row[1] or "active") == "active":
                role = _canonical_company_role(row[0])
    return (organization_id, role) if role else ("", "")


def _company_admin_principal(request: Request) -> CompanyPrincipal:
    identity = _dashboard_identity(request)
    actor_id = str(identity.get("id") or "")
    if not actor_id:
        return CompanyPrincipal("", "", "")
    organization_id, role = _active_company_membership(actor_id)
    invitee_lookup = ""
    if request.url.path.endswith("/v1/company/invitations/accept"):
        email = str(identity.get("email") or "")
        email_confirmed_at = str(identity.get("email_confirmed_at") or "")
        if email and email_confirmed_at and _company_admin_service is not None:
            invitee_lookup = _company_admin_service.invitee_lookup(email)
    return CompanyPrincipal(actor_id, organization_id, role, invitee_lookup)


def _configure_company_admin_runtime() -> None:
    global _company_admin_service
    try:
        _company_admin_service = company_admin_for_store(_store)
    except RuntimeError:
        _company_admin_service = None
        configure_company_admin(None, _company_admin_principal)  # type: ignore[arg-type]
        if _production_runtime():
            raise
        return
    configure_company_admin(
        _company_admin_service,
        _company_admin_principal,
        lambda request: str(getattr(request.state, "brevitas_request_id", "")),
    )


_compliance_admin_service = None


def _compliance_admin_principal(request: Request) -> ComplianceAdminPrincipal:
    """Derive compliance authority only from verified identity and live DB state."""
    identity = _dashboard_identity(request)
    actor_id = str(identity.get("id") or "")
    metadata = identity.get("app_metadata")
    if (not actor_id or not isinstance(metadata, dict)
            or metadata.get("role") != "brevitas_admin"):
        return ComplianceAdminPrincipal(actor_id, "", "")
    organization_id, membership_role = _active_company_membership(actor_id)
    if not organization_id or membership_role not in COMPANY_ROLES:
        return ComplianceAdminPrincipal(actor_id, "", "brevitas_admin")
    return ComplianceAdminPrincipal(actor_id, organization_id, "brevitas_admin")


def _configure_compliance_admin_runtime() -> None:
    global _compliance_admin_service
    try:
        _compliance_admin_service = SupabaseComplianceAdminService(_store)
    except Exception as exc:
        _compliance_admin_service = None
        configure_compliance_admin(None, None)
        if _production_runtime():
            raise RuntimeError(
                "Production compliance administration requires Supabase") from exc
        return
    configure_compliance_admin(
        _compliance_admin_service,
        _compliance_admin_principal,
        lambda request: str(getattr(request.state, "brevitas_request_id", "")),
    )


def _member_organization(request: Request, *, write: bool = False,
                         create: bool = False) -> tuple[str, dict]:
    user_id = _dashboard_user(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in to manage your organization")
    try:
        organization = _store.member_organization(user_id)
        if organization is None and create:
            _store.ensure_organization(user_id)
            organization = _store.member_organization(user_id)
    except Exception as exc:
        logger.error("company membership lookup unavailable error_type=%s",
                     type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Membership verification unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    if organization is None:
        raise HTTPException(status_code=403, detail="Active company membership required")
    role = _canonical_company_role(organization.get("role"))
    if role not in COMPANY_ROLES:
        raise HTTPException(status_code=403, detail="Active company membership required")
    organization = {**organization, "role": role}
    if write and role not in (
        "company_owner", "company_admin",
    ):
        raise HTTPException(status_code=403, detail="Organization admin access required")
    return user_id, organization


def _admin_authenticated(request: Request) -> str:
    identity = _dashboard_identity(request)
    metadata = identity.get("app_metadata") or {}
    if metadata.get("brevitas_admin") is True or metadata.get("role") == "brevitas_admin":
        return str(identity.get("id") or "admin")
    raise HTTPException(status_code=403, detail="Admin access required")


_POSTHOG_CACHE_TTL = 300
_POSTHOG_CACHE = BoundedTTLMap[str, dict](
    ttl_s=min(_POSTHOG_CACHE_TTL, _RESOURCE_BOUNDS.registry_ttl_s),
    max_entries=_RESOURCE_BOUNDS.registry_max_entries,
    max_value_bytes=_RESOURCE_BOUNDS.registry_max_value_bytes,
)


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
        if getattr(response, "status_code", 200) in (401, 403):
            logger.warning(
                "PostHog reporting credentials rejected status=%s",
                response.status_code,
            )
            raise HTTPException(
                status_code=503,
                detail="PostHog reporting credentials were rejected; update POSTHOG_PERSONAL_API_KEY",
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
    if cached is not None:
        return cached

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
    _POSTHOG_CACHE.put(cache_key, result)
    return result


# ── bvx browser authorization ────────────────────────────────────────────────

class DeviceCodeRequest(BaseModel):
    device_code: str = Field(min_length=40, max_length=128,
                             pattern=r"^[A-Za-z0-9_-]+$")


class DeviceApprovalRequest(DeviceCodeRequest):
    # This is only a tenant selector. Authorization always comes from the
    # authenticated user plus a fresh database membership/role check.
    company_id: str = Field(
        default="", max_length=36,
        pattern=(r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
                 r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})?$"),
    )


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
def approve_device_auth(
    request: Request,
    body: DeviceApprovalRequest,
    company_header: str = Header(
        default="", alias="X-Brevitas-Company-ID", max_length=36,
        pattern=(r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
                 r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})?$"),
    ),
):
    owner_id = _dashboard_user(request)
    if not owner_id:
        raise HTTPException(status_code=401, detail="Sign in to approve this device")
    device_hash = hash_key(body.device_code)
    try:
        row = _store.get_device_request(device_hash)
    except Exception as exc:
        logger.error("device approval lookup unavailable error_type=%s",
                     type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Device authorization unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    if not row or _device_expired(row):
        raise HTTPException(status_code=410, detail="Device authorization expired")
    if row.get("approved_at"):
        if row.get("owner_id") != owner_id:
            raise HTTPException(status_code=409, detail="Device already connected")

    body_company = body.company_id.lower()
    header_company = company_header.lower()
    if body_company and header_company and body_company != header_company:
        raise HTTPException(status_code=400, detail="Conflicting company selectors")
    selected_company = body_company or header_company

    # Preserve first-use onboarding without turning it into tenant selection:
    # ensure_organization creates only when there is no membership. With one or
    # more memberships, the resolver below remains the authority.
    if not selected_company:
        try:
            _store.ensure_organization(owner_id)
        except Exception as exc:
            logger.error("device company initialization unavailable error_type=%s",
                         type(exc).__name__)
            raise HTTPException(
                status_code=503, detail="Device authorization unavailable",
                headers={"Retry-After": "1"},
            ) from exc
    resolve_company = getattr(
        _store, "resolve_device_approval_organization", None)
    if not callable(resolve_company):
        logger.error("device company resolver unavailable")
        raise HTTPException(
            status_code=503, detail="Device authorization unavailable",
            headers={"Retry-After": "1"},
        )
    try:
        organization = resolve_company(owner_id, selected_company)
    except ValueError as exc:
        if str(exc) == "company_selection_required" and not selected_company:
            raise HTTPException(
                status_code=409, detail="Select a company for this device") from exc
        raise HTTPException(status_code=403, detail="Company access denied") from exc
    except Exception as exc:
        logger.error("device company resolution unavailable error_type=%s",
                     type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Device authorization unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    organization_id = str(
        organization.get("id") if isinstance(organization, dict) else "")
    organization_role = _canonical_company_role(
        organization.get("role") if isinstance(organization, dict) else "")
    if (not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
            r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", organization_id)
            or organization_role not in COMPANY_ROLES
            or (selected_company and organization_id.lower() != selected_company)):
        logger.error("device company resolver returned unsafe membership")
        raise HTTPException(
            status_code=503, detail="Device authorization unavailable",
            headers={"Retry-After": "1"},
        )
    if row.get("approved_at"):
        if str(row.get("organization_id") or "") != organization_id:
            raise HTTPException(status_code=409, detail="Device already connected")
        return {"status": "approved"}

    # BVX devices belong to the approving human's company organization, never
    # to an end customer routed by that company's backend.
    key = generate_api_key()
    kh = hash_key(key)
    try:
        encrypted_key = _encrypt(key, context={
            "purpose": "device_key", "device_hash": device_hash,
            "organization_id": organization_id,
        })
    except _CREDENTIAL_DEPENDENCY_ERRORS as exc:
        raise _credential_dependency_unavailable(exc) from exc
    try:
        approved = _store.approve_device_request(
            device_hash, owner_id, kh, encrypted_key,
            organization_id=organization_id)
    except ValueError as exc:
        if str(exc) == "company_selection_required":
            raise HTTPException(
                status_code=409, detail="Select a company for this device") from exc
        raise HTTPException(status_code=403, detail="Company access denied") from exc
    except Exception as exc:
        logger.error("device approval unavailable error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Device authorization unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    if not approved:
        raise HTTPException(status_code=409, detail="Device authorization already handled")
    logger.info("bvx device approved")
    return {"status": "approved"}


@app.post("/v1/device-auth/token")
@limiter.limit("120/minute")
def consume_device_auth(request: Request, body: DeviceCodeRequest):
    device_hash = hash_key(body.device_code)
    try:
        row = _store.get_device_request(device_hash)
    except Exception as exc:
        logger.error("device authorization lookup unavailable error_type=%s",
                     type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Device authorization unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    if not row or _device_expired(row):
        raise HTTPException(status_code=410, detail="Device authorization expired or consumed")
    if not row.get("approved_at"):
        return JSONResponse({"status": "pending"}, status_code=202,
                            headers={"Cache-Control": "no-store"})
    encrypted_key = str(row.get("encrypted_key") or "")
    organization_id = str(row.get("organization_id") or "")
    if not encrypted_key:
        raise HTTPException(
            status_code=503, detail="Device authorization unavailable",
            headers={"Retry-After": "1"},
        )
    try:
        # Decrypt before the one-time atomic consume. A transient KMS outage
        # must leave the approved record recoverable for the next poll.
        key = _decrypt(encrypted_key, context={
            "purpose": "device_key", "device_hash": device_hash,
            "organization_id": organization_id,
        })
    except _CREDENTIAL_DEPENDENCY_ERRORS as exc:
        raise _credential_dependency_unavailable(exc) from exc
    expected_key_hash = str(row.get("key_hash") or "")
    decrypted_key_hash = hash_key(key)
    consume_idempotently = getattr(
        _store, "consume_device_request_idempotent", None)
    if not callable(consume_idempotently):
        logger.error("device authorization idempotent consume unavailable")
        raise HTTPException(
            status_code=503, detail="Device authorization unavailable",
            headers={"Retry-After": "1"},
        )
    request_id = str(getattr(request.state, "brevitas_request_id", ""))
    if (not expected_key_hash
            or not secrets.compare_digest(decrypted_key_hash, expected_key_hash)):
        # Passing the decrypted digest through the atomic consume contract lets
        # the store quarantine the inconsistent exchange (and revoke any
        # retained activation) without ever returning the suspect credential.
        try:
            consume_idempotently(device_hash, decrypted_key_hash, request_id)
        except Exception as exc:
            logger.error("device authorization digest quarantine error_type=%s",
                         type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Device authorization unavailable",
            headers={"Retry-After": "1"},
        )
    try:
        consumed = consume_idempotently(
            device_hash, expected_key_hash, request_id)
    except Exception as exc:
        logger.error("device authorization consume unavailable error_type=%s",
                     type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Device authorization unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    if not consumed:
        raise HTTPException(status_code=410, detail="Device authorization already consumed")
    if not isinstance(consumed, dict):
        logger.error("device authorization consume receipt invalid")
        raise HTTPException(
            status_code=503, detail="Device authorization unavailable",
            headers={"Retry-After": "1"},
        )
    consumed_key_hash = str(consumed.get("key_hash") or "")
    consumed_encrypted_key = str(consumed.get("encrypted_key") or "")
    consumed_organization_id = str(consumed.get("organization_id") or "")
    receipt_valid = (
        consumed.get("status") == "consumed"
        and isinstance(consumed.get("already_consumed"), bool)
        and bool(consumed_key_hash)
        and secrets.compare_digest(consumed_key_hash, expected_key_hash)
        and bool(consumed_encrypted_key)
        and secrets.compare_digest(consumed_encrypted_key, encrypted_key)
        and bool(consumed_organization_id)
        and secrets.compare_digest(consumed_organization_id, organization_id)
    )
    if not receipt_valid:
        logger.error("device authorization consume receipt digest mismatch")
        raise HTTPException(
            status_code=503, detail="Device authorization unavailable",
            headers={"Retry-After": "1"},
        )
    with _valid_key_lock:
        _valid_key_cache.put(hash_key(key), True)
    return JSONResponse({"api_key": key},
                        headers={"Cache-Control": "no-store"})


# ── Key management ────────────────────────────────────────────────────────────

class OrganizationBootstrapRequest(BaseModel):
    account_type: str = Field(pattern=r"^(individual|company)$")
    name: str = Field(default="", max_length=100)


def _bootstrap_workspace_name(body: OrganizationBootstrapRequest) -> str:
    if any(ord(character) < 32 for character in body.name):
        raise HTTPException(status_code=422, detail="Invalid workspace name")
    name = re.sub(r" {2,}", " ", body.name.strip())
    if body.account_type == "company" and not name:
        raise HTTPException(status_code=422, detail="Company name is required")
    return name or "Personal workspace"


@app.post("/v1/organization/bootstrap")
@limiter.limit("10/minute")
def bootstrap_organization(request: Request, body: OrganizationBootstrapRequest):
    """Create the signed-in human's first workspace.

    Existing memberships always win. This endpoint cannot create an additional
    company for a user or select a company supplied by the browser.
    """

    user_id = _dashboard_user(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in to create a workspace")
    workspace_name = _bootstrap_workspace_name(body)
    try:
        organization = _store.member_organization(user_id)
        created = organization is None
        if created:
            _store.ensure_organization(user_id, workspace_name, body.account_type)
            organization = _store.member_organization(user_id)
    except Exception as exc:
        logger.error("workspace bootstrap unavailable error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Workspace setup unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    if not isinstance(organization, dict):
        raise HTTPException(
            status_code=503,
            detail="Workspace setup unavailable",
            headers={"Retry-After": "1"},
        )
    role = _canonical_company_role(organization.get("role"))
    organization_id = str(organization.get("id") or "")
    organization_name = str(organization.get("name") or "").strip()
    account_type = str(organization.get("account_type") or "")
    if (not organization_id or role not in COMPANY_ROLES or not organization_name
            or account_type not in {"individual", "company"}):
        logger.error("workspace bootstrap returned unsafe membership")
        raise HTTPException(
            status_code=503,
            detail="Workspace setup unavailable",
            headers={"Retry-After": "1"},
        )
    return JSONResponse({
        "company_id": organization_id,
        "company_name": organization_name,
        "role": role,
        "account_type": account_type,
        "created": created,
    }, headers={"Cache-Control": "private, no-store"})


def _organization_onboarding_status(request: Request, *, complete: bool = False) -> dict:
    user_id, organization = _member_organization(request)
    organization_id = str(organization.get("id") or "")
    if complete and organization.get("role") != "company_owner":
        raise HTTPException(
            status_code=403, detail="Company owner access required to finish onboarding")
    try:
        if complete:
            status = _store.complete_onboarding(
                user_id,
                organization_id,
                str(getattr(request.state, "brevitas_request_id", "")),
            )
        else:
            status = _store.onboarding_status(user_id, organization_id)
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail="Active company membership required") from exc
    except Exception as exc:
        logger.error(
            "organization onboarding lookup unavailable error_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Onboarding verification unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    if (not isinstance(status, dict)
            or status.get("company_id") != organization_id
            or status.get("status") not in ("pending", "complete")
            or not isinstance(status.get("cli_connected"), bool)
            or not isinstance(status.get("proxied_request_observed"), bool)):
        logger.error("organization onboarding store returned unsafe status")
        raise HTTPException(
            status_code=503,
            detail="Onboarding verification unavailable",
            headers={"Retry-After": "1"},
        )
    return status


@app.get("/v1/organization/onboarding")
@limiter.limit("60/minute")
def organization_onboarding_status(request: Request):
    return JSONResponse(
        _organization_onboarding_status(request),
        headers={"Cache-Control": "private, no-store"},
    )


@app.post("/v1/organization/onboarding/complete")
@limiter.limit("30/minute")
def complete_organization_onboarding(request: Request):
    status = _organization_onboarding_status(request, complete=True)
    if status["status"] != "complete":
        detail = (
            "Run bvx install with the released CLI before checking verification."
            if not status["cli_connected"]
            else "No successful request from a BVX-configured tool has reached the proxy yet."
        )
        raise HTTPException(status_code=409, detail=detail)
    return JSONResponse(status, headers={"Cache-Control": "private, no-store"})


class CreateKeyRequest(BaseModel):
    name: str = Field(default="Company backend", max_length=100)
    environment: str = Field(default="production", min_length=1, max_length=32,
                             pattern=r"^[A-Za-z0-9._-]+$")
    purpose: str = Field(default="service", pattern=r"^(service|dashboard_session)$")


def _key_admin_unavailable(exc: Exception) -> HTTPException:
    logger.error("key administration unavailable error_type=%s", type(exc).__name__)
    return HTTPException(
        status_code=503, detail="Key administration unavailable",
        headers={"Retry-After": "1"},
    )


@app.post("/v1/keys")
@limiter.limit("10/minute")
def create_key(request: Request, body: CreateKeyRequest):
    dashboard_session = body.purpose == "dashboard_session"
    try:
        owner_id, organization = _member_organization(
            request, write=not dashboard_session, create=True)
    except HTTPException:
        raise
    except Exception as exc:
        raise _key_admin_unavailable(exc) from exc
    request_id = str(getattr(request.state, "brevitas_request_id", ""))
    actor_role = _canonical_company_role(organization.get("role"))
    if hasattr(_store, "_request"):
        if not dashboard_session:
            raise HTTPException(
                status_code=409,
                detail=("Long-lived keys are managed through the company "
                        "service-account endpoints"),
            )
        try:
            active_organization, actor_role = _active_company_membership(owner_id)
        except Exception as exc:
            raise _key_admin_unavailable(exc) from exc
        if active_organization != str(organization.get("id") or "") or not actor_role:
            raise HTTPException(status_code=403, detail="Organization access denied")
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
        try:
            created = _store.create_key(
                "", body.name,
                owner_id=organization.get("billing_owner_id") or owner_id,
                organization_id=organization["id"], key_type="dashboard_session",
                scopes=["proxy:invoke", "usage:read_own", "provider:read",
                        "provider:manage"],
                environment="dashboard", created_by=owner_id,
                expires_at=expires_at, request_id=request_id,
                actor_role=actor_role,
            )
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Key creation denied") from exc
        except RuntimeError as exc:
            failure = str(exc)
            if failure.endswith("forbidden_or_invalid"):
                raise HTTPException(status_code=403, detail="Key creation denied") from exc
            if failure.endswith(("company_session_cap", "duplicate_key")):
                raise HTTPException(status_code=409, detail="Key creation conflict") from exc
            logger.error("atomic dashboard key creation unavailable error_type=%s",
                         type(exc).__name__)
            raise HTTPException(
                status_code=503, detail="Key administration unavailable",
                headers={"Retry-After": "1"},
            ) from exc
        except Exception as exc:
            raise _key_admin_unavailable(exc) from exc
        raw_key = str(created.get("api_key") or "")
        if not raw_key:
            raise HTTPException(status_code=503, detail="Key administration unavailable")
        with _valid_key_lock:
            _valid_key_cache.put(hash_key(raw_key), True)
        return {
            **created,
            "name": body.name,
            "service_account_id": None,
            "purpose": "dashboard_session",
        }
    service_account = (None if dashboard_session else _store.ensure_service_account(
        organization["id"], body.environment, created_by=owner_id))
    key = generate_api_key()
    kh = hash_key(key)
    if dashboard_session:
        scopes = ["proxy:invoke", "usage:read_own", "provider:read", "provider:manage"]
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
    else:
        scopes = ["proxy:invoke", "usage:write", "usage:read_own",
                  "customer:route", "customer:auto_provision",
                  "repositories:register", "installations:register",
                  "provider:read", "provider:manage",
                  "jobs:create", "jobs:read", "jobs:cancel"]
        expires_at = ""
    try:
        _store.create_key(
            kh, body.name,
            owner_id=(owner_id if dashboard_session
                      else organization.get("billing_owner_id") or owner_id),
            organization_id=organization["id"],
            service_account_id=service_account["id"] if service_account else "",
            key_type="dashboard_session" if dashboard_session else "organization_service",
            scopes=scopes, environment=body.environment, key_prefix=key[:12],
            created_by=owner_id, expires_at=expires_at,
            request_id=request_id, actor_role=actor_role,
        )
    except RuntimeError as exc:
        if dashboard_session and str(exc) in (
                "duplicate key", "dashboard session company cap reached"):
            raise HTTPException(
                status_code=409, detail="Dashboard session limit reached") from exc
        raise _key_admin_unavailable(exc) from exc
    with _valid_key_lock:
        _valid_key_cache.put(kh, True)
    return {"api_key": key, "name": body.name, "organization_id": organization["id"],
            "service_account_id": service_account["id"] if service_account else None,
            "environment": body.environment, "purpose": body.purpose,
            "expires_at": expires_at or None,
            "scopes": scopes, "secret_available_once": True}


@app.get("/v1/keys")
@limiter.limit("60/minute")
def list_keys(
    request: Request,
    cursor: str = Query("", max_length=512),
    limit: int = Query(50, ge=1, le=100),
):
    try:
        owner_id, organization = _member_organization(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise _key_admin_unavailable(exc) from exc
    request_id = str(getattr(request.state, "brevitas_request_id", ""))
    actor_role = _canonical_company_role(organization.get("role"))
    try:
        if hasattr(_store, "_request"):
            active_organization, actor_role = _active_company_membership(owner_id)
            if (active_organization != str(organization.get("id") or "")
                    or not actor_role):
                raise HTTPException(status_code=403, detail="Organization access denied")
            page = _store.list_organization_keys_page(
                organization["id"], owner_id, cursor=cursor, limit=limit,
                request_id=request_id, actor_role=actor_role,
            )
        else:
            if cursor:
                raise HTTPException(
                    status_code=400,
                    detail="Pagination cursors require the hosted database",
                )
            rows = _store.list_organization_keys(organization["id"])
            page = {
                "keys": rows[:limit], "next_cursor": "",
                "has_more": len(rows) > limit, "limit": limit,
            }
    except HTTPException:
        raise
    except ValueError as exc:
        if "cursor" in str(exc).lower():
            raise HTTPException(status_code=400, detail="Invalid pagination cursor") from exc
        raise _key_admin_unavailable(exc) from exc
    except Exception as exc:
        raise _key_admin_unavailable(exc) from exc
    if (not isinstance(page, dict) or not isinstance(page.get("keys"), list)
            or not isinstance(page.get("next_cursor"), str)
            or len(page["next_cursor"]) > 512
            or not isinstance(page.get("has_more"), bool)
            or page.get("limit") != limit):
        raise _key_admin_unavailable(RuntimeError("invalid key page response"))
    return {
        "keys": page["keys"], "next_cursor": page["next_cursor"],
        "has_more": page["has_more"], "limit": page["limit"],
    }


@app.delete("/v1/keys/{key_id}")
@limiter.limit("30/minute")
def revoke_key(request: Request, key_id: str):
    try:
        owner_id, organization = _member_organization(
            request, write=not hasattr(_store, "_request"))
    except HTTPException:
        raise
    except Exception as exc:
        raise _key_admin_unavailable(exc) from exc
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", key_id):
        raise HTTPException(status_code=400, detail="Invalid key id")
    actor_role = _canonical_company_role(organization.get("role"))
    if hasattr(_store, "_request"):
        try:
            active_organization, actor_role = _active_company_membership(owner_id)
        except Exception as exc:
            raise _key_admin_unavailable(exc) from exc
        if active_organization != str(organization.get("id") or "") or not actor_role:
            raise HTTPException(status_code=403, detail="Organization access denied")
    try:
        revoked = _store.revoke_organization_key(
            organization["id"], key_id, actor_user_id=owner_id,
            request_id=str(getattr(request.state, "brevitas_request_id", "")),
            actor_role=actor_role)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Key revocation denied") from exc
    except RuntimeError as exc:
        if str(exc).endswith("forbidden_or_not_found"):
            raise HTTPException(status_code=403, detail="Key revocation denied") from exc
        logger.error("atomic key revocation unavailable error_type=%s",
                     type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Key administration unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    except Exception as exc:
        raise _key_admin_unavailable(exc) from exc
    if not revoked:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"revoked": True}


class CustomerImportItem(BaseModel):
    external_id: str = Field(min_length=1, max_length=200,
                             pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    display_name: str = Field(default="", max_length=200)


class CustomerImportRequest(BaseModel):
    customers: list[CustomerImportItem] = Field(min_length=1, max_length=1000)


def _customer_import_organization(request: Request) -> dict:
    """Allow human admins or an organization key that can already auto-provision.

    Bulk import does not grant the workload key more authority than it has on
    first customer traffic; it only makes the exact-ID provisioning efficient.
    """
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        _, organization = _member_organization(request, write=True, create=True)
        return organization
    raw_key = request.headers.get("x-brevitas-key", "")
    if not raw_key:
        raise HTTPException(status_code=401, detail="Sign in or provide X-Brevitas-Key")
    context = _auth_context_for_key(hash_key(raw_key))
    can_import = (
        context.key_type == "organization_service"
        and context.permits("customer:auto_provision")
    ) or context.permits("customers:import")
    if not context.organization_id or not can_import:
        raise HTTPException(status_code=403, detail="Key cannot import customers")
    return {"id": context.organization_id}


@app.post("/v1/customers/import")
@limiter.limit("120/minute")
def import_customers(request: Request, body: CustomerImportRequest):
    organization = _customer_import_organization(request)
    imported = _store.upsert_customers(organization["id"], [
        {"external_id": item.external_id, "display_name": item.display_name}
        for item in body.customers
    ])
    return {"organization_id": organization["id"], "customers": imported,
            "count": len(imported)}


@app.get("/v1/customers")
@limiter.limit("60/minute")
def list_customers(request: Request):
    _, organization = _member_organization(request)
    return {"customers": _store.list_customers(organization["id"])}


class CachePolicyRequest(BaseModel):
    enabled: bool
    customer_external_id: str = Field(default="", max_length=200,
                                      pattern=r"^[A-Za-z0-9._:-]*$")


@app.get("/v1/cache-policy")
@limiter.limit("60/minute")
def get_cache_policy(
    request: Request,
    customer_external_id: str = Query(
        "", max_length=200, pattern=r"^[A-Za-z0-9._:-]*$"),
):
    _, organization = _member_organization(request)
    customer_id = ""
    if customer_external_id:
        try:
            customer = _store.find_customer(
                organization["id"], customer_external_id)
        except Exception as exc:
            raise _key_admin_unavailable(exc) from exc
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        customer_id = str(customer["id"])
    try:
        enabled = _store.cache_enabled(organization["id"], customer_id)
    except Exception as exc:
        raise _key_admin_unavailable(exc) from exc
    return {"enabled": bool(enabled), "customer_external_id": customer_external_id}


@app.put("/v1/cache-policy")
@limiter.limit("30/minute")
def set_cache_policy(request: Request, body: CachePolicyRequest):
    _, organization = _member_organization(request, write=True)
    customer_id = ""
    if body.customer_external_id:
        customer = _store.find_customer(organization["id"], body.customer_external_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        customer_id = str(customer["id"])
    _store.set_cache_enabled(organization["id"], body.enabled, customer_id)
    if not body.enabled:
        try:
            cache = make_semantic_cache()
            namespaces = [f"{organization['id']}:{customer_id or 'unattributed'}"]
            if not customer_id:
                namespaces.extend(
                    f"{organization['id']}:{customer['id']}"
                    for customer in _store.list_customers(organization["id"])
                )
            for namespace in namespaces:
                cache.purge_namespace(namespace, strict=True)
        except Exception as exc:
            logger.error("cache purge failed error_type=%s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Cache purge unavailable") from exc
    return {"enabled": body.enabled, "customer_external_id": body.customer_external_id,
            "purged": not body.enabled}


def _job_tenant(request: Request, kh: str, scope: str) -> JobTenant:
    context = _request_auth_context(request, kh)
    if not context.permits(scope):
        raise HTTPException(status_code=403, detail=f"Key lacks {scope} scope")
    if not context.organization_id or not context.customer_id:
        raise HTTPException(
            status_code=400,
            detail="Jobs require X-Brevitas-Customer-ID",
        )
    return JobTenant(context.organization_id, context.customer_id, kh)


@app.post("/v1/jobs", status_code=202)
async def create_job(request: Request, body: JobRequest,
                     kh: str = Depends(_authenticated)):
    tenant = _job_tenant(request, kh, "jobs:create")
    try:
        row, created = await _job_service.submit(
            tenant, body, request.headers.get("idempotency-key", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except _CREDENTIAL_DEPENDENCY_ERRORS as exc:
        raise _credential_dependency_unavailable(exc) from exc
    except Exception as exc:
        logger.error("job submission failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Job queue unavailable") from exc
    return JSONResponse({**row, "created": created}, status_code=202,
                        headers={"Cache-Control": "no-store"})


@app.get("/v1/jobs/{job_id}")
async def get_job(request: Request, job_id: str, kh: str = Depends(_authenticated)):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", job_id):
        raise HTTPException(status_code=400, detail="Invalid job id")
    try:
        row = await _job_service.get(_job_tenant(request, kh, "jobs:read"), job_id)
    except _CREDENTIAL_DEPENDENCY_ERRORS as exc:
        raise _credential_dependency_unavailable(exc) from exc
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(row, headers={"Cache-Control": "no-store"})


@app.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str, kh: str = Depends(_authenticated)):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", job_id):
        raise HTTPException(status_code=400, detail="Invalid job id")
    row = await _job_service.cancel(_job_tenant(request, kh, "jobs:cancel"), job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(row, headers={"Cache-Control": "no-store"})


class RegisterRepositoryRequest(BaseModel):
    repo: str = Field(min_length=1, max_length=512)
    source: str = Field(default="bvx", max_length=32, pattern=r"^[A-Za-z0-9._-]+$")

    @field_validator("repo")
    @classmethod
    def safe_repo_name(cls, value: str) -> str:
        name = value.strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if not name or len(name) > 128 or any(ord(char) < 32 for char in name):
            raise ValueError("repo must contain a safe repository name")
        return name


@app.post("/v1/repositories")
@limiter.limit("30/minute")
def register_repository(request: Request, body: RegisterRepositoryRequest,
                        kh: str = Depends(_authenticated)):
    _require_scope(request, kh, "repositories:register")
    _store.register_repository(kh, body.repo, body.source)
    return {"registered": True, "repo": body.repo}


class InstallationDevice(BaseModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    platform: str = Field(default="", max_length=64)
    arch: str = Field(default="", max_length=64)


class InstallationRepository(BaseModel):
    id: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9._:-]*$")
    label: str = Field(default="", max_length=128)


class InstallationClient(BaseModel):
    name: str = Field(default="bvx", max_length=64)
    version: str = Field(default="", max_length=64)


class InstallationRequest(BaseModel):
    installation_id: str = Field(min_length=36, max_length=36,
                                 pattern=r"^[0-9a-fA-F-]{36}$")
    device: InstallationDevice
    repository: InstallationRepository
    environment: str = Field(default="", max_length=32,
                             pattern=r"^[A-Za-z0-9._-]*$")
    client: InstallationClient


class InstallationHeartbeatRequest(BaseModel):
    device: InstallationDevice
    environment: str = Field(default="", max_length=32,
                             pattern=r"^[A-Za-z0-9._-]*$")
    client: InstallationClient


class LegacyInstallationRequest(BaseModel):
    installation_id: str = Field(min_length=36, max_length=36,
                                 pattern=r"^[0-9a-fA-F-]{36}$")
    repository: str = Field(default="", max_length=128)
    environment: str = Field(default="", max_length=32,
                             pattern=r"^[A-Za-z0-9._-]*$")
    bvx_version: str = Field(default="", max_length=64)
    device_fingerprint: str = Field(default="", max_length=128,
                                    pattern=r"^[A-Za-z0-9._:-]*$")


@app.post("/v1/installations")
@limiter.limit("30/minute")
def create_installation(request: Request, body: InstallationRequest,
                        kh: str = Depends(_authenticated)):
    return _register_installation(
        request, kh, body.installation_id, body.device,
        body.environment, body.client, body.repository)


@app.post("/v1/installations/{installation_id}/heartbeat")
@limiter.limit("120/minute")
def heartbeat_installation(request: Request, installation_id: str,
                           body: InstallationHeartbeatRequest,
                           kh: str = Depends(_authenticated)):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", installation_id):
        raise HTTPException(status_code=400, detail="Invalid installation id")
    return _register_installation(
        request, kh, installation_id, body.device,
        body.environment, body.client, None)


@app.post("/v1/installations/register")
@limiter.limit("30/minute")
def register_installation_legacy(request: Request, body: LegacyInstallationRequest,
                          kh: str = Depends(_authenticated)):
    device = InstallationDevice(id=body.device_fingerprint or body.installation_id)
    client = InstallationClient(version=body.bvx_version)
    repository = InstallationRepository(label=body.repository)
    return _register_installation(
        request, kh, body.installation_id, device, body.environment, client, repository)


def _register_installation(request: Request, kh: str, installation_id: str,
                           device: InstallationDevice, environment: str,
                           client: InstallationClient,
                           repository: InstallationRepository | None):
    context = _request_auth_context(request, kh)
    if not context.organization_id or not context.permits("installations:register"):
        raise HTTPException(status_code=403, detail="Key cannot register installations")
    try:
        installation = _store.register_installation(
            context.organization_id, context.service_account_id, installation_id,
            repository.label if repository else None, environment or context.environment,
            client.version, device.id, repository_id=repository.id if repository else "",
            device_platform=device.platform, device_arch=device.arch, client_name=client.name,
            registration_key_hash=kh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"installation_id": installation["id"], "status": "active",
            "heartbeat_interval_seconds": 300}


@app.get("/v1/installations")
@limiter.limit("60/minute")
def installation_inventory(request: Request):
    _, organization = _member_organization(request)
    return {"installations": _store.list_installations(organization["id"])}


@app.get("/v1/organization/inventory")
@limiter.limit("60/minute")
def organization_inventory(request: Request):
    _, organization = _member_organization(request)
    return _store.organization_inventory(organization["id"])


@app.delete("/v1/installations/{installation_id}")
@limiter.limit("30/minute")
def revoke_installation(request: Request, installation_id: str):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", installation_id):
        raise HTTPException(status_code=400, detail="Invalid installation id")
    _, organization = _member_organization(request, write=True)
    if not _store.revoke_installation(organization["id"], installation_id):
        raise HTTPException(status_code=404, detail="Installation not found")
    return {"revoked": True}


# ── Provider config ───────────────────────────────────────────────────────────

class ProviderConfigRequest(BaseModel):
    provider: str
    provider_api_key: str = ""
    model: str = Field(min_length=1, max_length=100)


@app.get("/v1/provider")
@limiter.limit("120/minute")
def get_provider(request: Request, kh: str = Depends(_authenticated)):
    _require_scope(request, kh, "provider:read")
    config = _provider_config_for_key(kh)
    if config is None:
        return {"configured": False, "provider": "ollama", "model": "llama3.2",
                "has_api_key": False}
    try:
        raw_key = _decrypt(config["provider_api_key"], context={
            "purpose": "provider_credential", "key_hash": kh,
        })
    except _CREDENTIAL_DEPENDENCY_ERRORS as exc:
        raise _credential_dependency_unavailable(exc) from exc
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
    _require_scope(request, kh, "provider:manage")
    if body.provider not in _PROVIDER_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{body.provider}'")
    allowed_models = _PROVIDER_MODELS[body.provider]
    if not allowed_models:
        raise HTTPException(status_code=400, detail=f"Provider '{body.provider}' is not available")
    if body.model not in allowed_models:
        raise HTTPException(status_code=400, detail="Model is not supported by this provider")
    existing = _provider_config_for_key(kh)
    if body.provider != "ollama" and not body.provider_api_key:
        # Allow if a key is already saved for this provider — keep it
        has_existing_key = existing and existing.get("provider_api_key") and existing.get("provider") == body.provider
        if not has_existing_key:
            raise HTTPException(status_code=400, detail="provider_api_key is required for this provider")
        encrypted_key = existing["provider_api_key"]
    else:
        try:
            encrypted_key = _encrypt(body.provider_api_key, context={
                "purpose": "provider_credential", "key_hash": kh,
            })
        except _CREDENTIAL_DEPENDENCY_ERRORS as exc:
            raise _credential_dependency_unavailable(exc) from exc
    try:
        _store.set_provider_config(kh, body.provider, encrypted_key, body.model)
    except Exception as exc:
        raise _provider_config_unavailable(exc) from exc
    return {"ok": True, "provider": body.provider, "model": body.model}


@app.get("/v1/providers")
def list_providers(request: Request, kh: str = Depends(_authenticated)):
    _require_scope(request, kh, "provider:read")
    return {"providers": _PROVIDER_MODELS}


@app.get("/v1/ollama/models")
def ollama_models(request: Request, kh: str = Depends(_authenticated)):
    _require_scope(request, kh, "provider:read")
    try:
        with _provider_call():
            resp = provider_sync_http.request(
                "ollama", "models.list", "GET", f"{_OLLAMA_HOST}/api/tags")
            try:
                resp.raise_for_status()
                models = [m["name"] for m in resp.json().get("models", [])]
                return {"models": models, "available": True}
            finally:
                resp.close()
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
    lossy:             bool       = Field(default=False)  # off by default: lossy last-message rewrite is opt-in
    retrieval:         bool       = Field(default=False)  # off by default: context pruning can drop evidence
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
    _require_scope(request, kh, "proxy:invoke")
    task = body.task or (body.messages[0][:200] if body.messages else "")
    # Baseline is measured against the ORIGINAL messages + full prior context; the volatile
    # LAST message may be lossily shrunk while earlier messages stay byte-identical so the
    # provider cache still hits the stable prefix.
    pipe = _compress_pipeline(task, body.messages, body.prior_context, body.prune_budget,
                              body.lossy, retrieval=body.retrieval,
                              key_hash=_request_tenant_key(request, kh))
    out_messages = pipe["out_messages"]
    model_result = _run_configured_model(
        kh, out_messages, pipe["selected_context"], task, request,
    )

    if body.meter:
        _safe_record_usage(
            auth_context=_request_auth_context(request, kh),
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
    from token_efficiency_model.quality.gate import lever_allowed
    _require_scope(request, kh, "proxy:invoke")
    # Lossy prompt compression is a risky lever: fail-closed unless this tenant has opted in
    # (and not tripped). When not allowed, force the lossless (byte-identical) path.
    compression_ok = lever_allowed("compression", _request_tenant_key(request, kh))
    if body.smart and body.rate is None and compression_ok:
        from token_efficiency_model.lossless.task_router import TaskCompressionRouter
        res = TaskCompressionRouter().route(body.prompt, task_hint=body.task)
        r = res.optimization
        extra = {"task": res.task, "rate": res.rate, "protected_code_blocks": res.protected_code_blocks,
                 "reason": res.reason, "quality_sim": res.quality_sim}
    else:
        from token_efficiency_model.lossless.prompt_optimizer import optimize_prompt as _opt
        rate = body.rate if body.rate is not None else 1.0
        if rate < 1.0 and not compression_ok:
            rate = 1.0   # gate not open → refuse to compress; return the prompt losslessly
        r = _opt(body.prompt, rate=rate)
        extra = {"task": None, "rate": rate}

    _safe_record_usage(
        auth_context=_request_auth_context(request, kh),
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
    _require_scope(request, kh, "proxy:invoke")
    from token_efficiency_model.lossless.api_adapter import retrieval_select
    from token_efficiency_model.quality.gate import lever_allowed

    # Fail-closed, per tenant: retrieval can omit evidence, so only prune when this tenant
    # has opted in AND the retrieval lever has not tripped. Otherwise return full context.
    if not lever_allowed("retrieval", _request_tenant_key(request, kh)):
        ctx_tokens = estimate_tokens_many(body.prior_context)
        return {"selected_context": list(body.prior_context),
                "baseline_tokens": ctx_tokens, "optimized_tokens": ctx_tokens,
                "savings_pct": 0.0, "fallback_applied": True,
                "reason": "retrieval_gate_closed"}

    out = retrieval_select(body.task, body.prior_context, k=body.k,
                           min_top_score=body.min_top_score, use_adaptive=True)
    _safe_record_usage(
        auth_context=_request_auth_context(request, kh),
        key_hash=kh,
        baseline_tokens=out["baseline_tokens"],
        optimized_tokens=out["optimized_tokens"],
        savings_pct=out["savings_pct"],
        quality_proxy=None,
    )
    return out


class _ClientGone(Exception):
    """Raised inside the worker thread to unwind the pipeline when the client disconnects."""


_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _safe_error_type(exc: BaseException | None) -> str:
    if exc is None:
        return "none"
    name = type(exc).__name__
    return name if _SAFE_ERROR_TYPE.fullmatch(name) else "Exception"


def _stream_error_event(route: str, exc: Exception, request: Request,
                        provider: str = "") -> dict:
    """Return a stable SSE error without copying exception/provider content.

    Provider response bodies, transport URLs, credentials, and exception text are
    deliberately excluded from both the client payload and server log. The log
    retains bounded operational dimensions that are sufficient to correlate and
    classify the failure without turning an upstream message into a secret sink.
    """
    status_code = int(exc.status_code) if isinstance(exc, HTTPException) else 0
    if status_code == 503 or isinstance(exc, ProviderCircuitOpen):
        code = "provider_stream_unavailable"
        message = "Model provider temporarily unavailable"
    elif status_code == 502 or isinstance(exc, httpx.HTTPError):
        code = "provider_stream_failed"
        message = "Model provider stream failed"
    elif route == "playground":
        code = "playground_stream_failed"
        message = "Playground stream failed"
    else:
        code = "compression_stream_failed"
        message = "Compression stream failed"

    request_id = str(getattr(request.state, "brevitas_request_id", "") or "")
    if not _SAFE_REQUEST_ID.fullmatch(request_id):
        request_id = "unavailable"
    safe_provider = provider if provider in _PROVIDER_MODELS else "none"
    cause = exc.__cause__ if isinstance(exc.__cause__, BaseException) else None
    logger.error(
        "stream_failure route=%s code=%s provider=%s http_status=%d "
        "error_type=%s cause_type=%s request_id=%s",
        route, code, safe_provider, status_code, _safe_error_type(exc),
        _safe_error_type(cause), request_id,
    )
    return {"stage": "error", "code": code, "message": message}


@app.post("/v1/compress/stream")
@limiter.limit("60/minute")
async def compress_stream(request: Request, body: CompressRequest, kh: str = Depends(_authenticated)):
    _require_scope(request, kh, "proxy:invoke")
    # Resolve the store record and unwrap the provider credential before the
    # StreamingResponse commits a 200. This also avoids a second store/KMS read
    # in the worker thread.
    config, backend = await asyncio.to_thread(
        _resolve_configured_model_backend, kh, request)
    event_queue: queue.Queue = queue.Queue()
    SENTINEL = object()
    cancel_event = threading.Event()

    def _run():
        try:
            task = body.task or (body.messages[0][:200] if body.messages else "")
            event_queue.put({"stage": "retrieving", "task": task[:120]})
            if config:
                event_queue.put({"stage": "routed", "provider": config["provider"],
                                 "model": config["model"], "route_fit": 1.0})
            if cancel_event.is_set():
                return

            pipe = _compress_pipeline(task, body.messages, body.prior_context,
                                      body.prune_budget, body.lossy, retrieval=body.retrieval,
                                      key_hash=_request_tenant_key(request, kh))
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
                kh, out_messages, pipe["selected_context"], task, request,
                resolved_config=config, resolved_backend=backend,
            )
            if model_result["model"]:
                event_queue.put({"stage": "model_response", **model_result,
                                 "text": model_result["model_response"]})

            if body.meter:
                _safe_record_usage(
                    auth_context=_request_auth_context(request, kh),
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
            if cancel_event.is_set():
                return
            event_queue.put(_stream_error_event(
                "compress", exc, request,
                str((config or {}).get("provider") or ""),
            ))
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
    lossy:             bool       = Field(default=False)  # opt-in lossy last-message rewrite
    retrieval:         bool       = Field(default=False)  # opt-in context pruning
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
    _require_scope(request, kh, "proxy:invoke")
    # Resolve the backend up-front so an invalid BYOK provider/model returns a clean 502
    # instead of surfacing mid-stream. Raises HTTPException on bad input.
    provider, model, backend = _build_chat_backend(
        body.byok_provider, body.byok_model, body.byok_key, request)
    tenant_gate_key = _request_tenant_key(request, kh)

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
                                      body.prune_budget, lossy=body.lossy,
                                      retrieval=body.retrieval,
                                      key_hash=tenant_gate_key)
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
            # an eligible exact repeat can skip the model call. Fuzzy reuse is separately
            # opt-in and fail-closed; neither kind is reported as provider token deletion.
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
                cache = _get_playground_cache(request)
                cbody = {"messages": [{"role": "user", "content": prompt}],
                         "temperature": 0,
                         "_brevitas_cache_namespace": tenant_gate_key}
                hit = None
                from token_efficiency_model.quality.gate import lever_allowed
                # Gate on the safe exact-cache lever for this tenant; the fuzzy semantic
                # sub-layer is separately fail-closed inside the cache.
                if cache is not None and lever_allowed("cache", tenant_gate_key):
                    try:
                        hit = cache.lookup(
                            cbody, provider, model,
                            gate_key=tenant_gate_key,
                        )
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
                                     "similarity": cache_similarity,
                                     "calls_avoided": 1,
                                     "replayed_call_tokens": cache_saved_tokens})
                else:
                    model_response = backend(prompt, model)
                    # Cache only when BOTH hold: (1) the prompt we answered was byte-faithful
                    # to the original (no lossy compression / retrieval pruning), and (2) the
                    # provider finished naturally — a response truncated at the token cap
                    # (Anthropic stop_reason=max_tokens, OpenAI finish_reason=length) is a
                    # partial answer and must never be replayed as a complete one.
                    complete = getattr(backend, "last_complete", True)
                    if cache is not None and pipe.get("faithful", True) and complete:
                        try:
                            cache.store(cbody, provider, model, {"text": model_response},
                                        prompt_tokens=count_tokens(prompt),
                                        completion_tokens=count_tokens(model_response))
                        except Exception:
                            pass  # caching is best-effort — never fail the turn over it

                event_queue.put({"stage": "model_response", "provider": provider, "model": model,
                                 "text": model_response, "model_response": model_response,
                                 "cached": cache_hit})

            # Mechanisms remain separate: compression can avoid provider input tokens;
            # exact response replay avoids a model call. A replay is never token deletion.
            provider_input_tokens_avoided = compression_saved
            calls_avoided = int(cache_hit)
            # Estimated reference-price delta: compression trims input tokens; an exact
            # replay also avoids the reference call. This is not paired-control evidence.
            if cache_hit:
                estimated_cost_delta_usd = _price_usd(
                    compression_saved + (hit.prompt_tokens or 0),
                    hit.completion_tokens or count_tokens(model_response),
                )
            else:
                estimated_cost_delta_usd = _price_usd(compression_saved, 0)

            _safe_record_usage(
                auth_context=_request_auth_context(request, kh),
                key_hash=kh,
                baseline_tokens=pipe["baseline_tokens"],
                optimized_tokens=pipe["optimized_tokens"],
                savings_pct=pipe["savings_pct"],
                provider_input_tokens_avoided=provider_input_tokens_avoided,
                calls_avoided=calls_avoided,
                quality_proxy=None,
                strategy=(f"chat:cache_{cache_kind}|ctx:{pipe['reason']}" if cache_hit
                          else f"chat:{pipe['message_reason']}|ctx:{pipe['reason']}")[:64],
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
                "provider_input_tokens_avoided": provider_input_tokens_avoided,
                "calls_avoided":       calls_avoided,
                "estimated_cost_delta_usd": estimated_cost_delta_usd,
                # Deprecated compatibility aliases. `tokens_saved_total` now means only
                # provider input avoided; it never includes replayed response tokens.
                "tokens_saved_total":  provider_input_tokens_avoided,
                "cost_saved_usd":      estimated_cost_delta_usd,
                "price_basis":         _PLAYGROUND_PRICE_MODEL,
                "provider":            provider,
                "model":               model,
                "model_response":      model_response,
            }})
        except _ClientGone:
            pass
        except Exception as exc:
            if cancel_event.is_set():
                return
            event_queue.put(_stream_error_event(
                "playground", exc, request, provider,
            ))
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
    # Internal-only bridge metadata populated from the authenticated proxy request.
    customer_external_id: str = Field(default="", max_length=200, exclude=True)
    # Mechanism-separated evidence. Incremental savings is reported only when a
    # paired control arm supplied an authoritative provider cost.
    control_cost_usd: Optional[float] = Field(default=None, ge=0)
    transport_bytes_avoided: int = Field(default=0, ge=0)

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
    "retrieve", "retrieval", "llmlingua", "lossy", "semantic_cache", "exact_cache",
    "response_cache", "reorder", "compress",
)
_BYTE_PRESERVING_STRATEGIES = (
    "native_cache", "cache_only", "passthrough", "byte_preserving", "lossless",
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


def _record_usage_report(kh: str, body: UsageReportRequest, *,
                         auth_context: AuthContext | None = None,
                         authoritative: bool = False,
                         tenant_gate_key: str | None = None) -> dict:
    tenant_gate_key = tenant_gate_key or kh
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
    provider_input_tokens_avoided = max(0, baseline_tokens - receipt.input_tokens)
    strategy_name = (body.strategy or "").strip().lower()
    calls_avoided = int(strategy_name.startswith(("exact_cache", "semantic_cache")))
    native_cache_discount_usd = None
    prices = costs.get("prices") or {}
    if costs["pricing_status"] == "priced" and prices:
        cached_discount = receipt.cached_input_tokens * (
            prices["input"] - prices["cached"])
        write_5m = receipt.cache_write_5m_tokens
        write_1h = receipt.cache_write_1h_tokens
        tiered = write_5m + write_1h
        if tiered > receipt.cache_write_tokens:
            write_5m = write_1h = tiered = 0
        unspecified = receipt.cache_write_tokens - tiered
        write_premium = (
            (unspecified + write_5m) * (prices["write"] - prices["input"])
            + write_1h * (prices.get("write_1h", prices["input"] * 2.0)
                          - prices["input"])
        )
        native_cache_discount_usd = round(
            (cached_discount - write_premium) / 1_000_000, 10)
    incremental_savings_usd = None
    if body.control_cost_usd is not None and costs["actual_cost_usd"] is not None:
        incremental_savings_usd = round(
            body.control_cost_usd - costs["actual_cost_usd"], 10)

    mode = _verification_mode(body.strategy)
    stream = _seq_stream(tenant_gate_key)
    if mode == "byte_preserving":
        quality_status = "verified"
    elif body.quality_verified is None:
        quality_status = "unverified"
    else:
        stream.update(body.quality_verified)
        if stream.state.tripped:
            quality_status = "stream_tripped"
            # A tripped stream must stop THIS TENANT's request path from applying any
            # unproven lever — not just stop billing. Trips are keyed by the customer key,
            # so one tenant's failing reports never disable levers for other tenants.
            from token_efficiency_model.quality.gate import trip_lever
            for _lever in ("retrieval", "compression", "semantic_cache", "reorder"):
                trip_lever(_lever, key=tenant_gate_key)
        else:
            quality_status = "verified" if body.quality_verified else "failed"
    # Caller-reported SDK values are analytics only. Only the in-process proxy,
    # which observed the provider response, may create verified/billable savings.
    # Live charging is intentionally narrower than analytics. Only authoritative
    # provider receipts from input-byte-preserving methods can create billable
    # savings. Response reuse, reordering, retrieval, and other quality-affecting
    # methods remain non-billable until their gate state is durable and auditable.
    verified = (max(0.0, float(measured or 0))
                if authoritative and mode == "byte_preserving"
                and quality_status == "verified" else 0.0)
    fee = round(verified * BREVITAS_FEE_RATE, 10)

    inserted = _store.record_usage(
        key_hash=kh,
        owner_id=(auth_context.billing_owner_id if auth_context else _store.key_owner(kh)),
        organization_id=auth_context.organization_id if auth_context else "",
        customer_id=auth_context.customer_id if auth_context else "",
        authoritative=authoritative,
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
        provider_input_tokens_avoided=provider_input_tokens_avoided,
        native_cache_discount_usd=native_cache_discount_usd,
        calls_avoided=calls_avoided,
        transport_bytes_avoided=body.transport_bytes_avoided,
        brevitas_incremental_savings_usd=incremental_savings_usd,
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
        "provider_input_tokens_avoided": provider_input_tokens_avoided,
        "native_cache_discount_usd": native_cache_discount_usd,
        "calls_avoided": calls_avoided,
        "transport_bytes_avoided": body.transport_bytes_avoided,
        "brevitas_incremental_savings_usd": incremental_savings_usd,
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
    return _record_usage_report(
        kh, body,
        auth_context=_require_scope(request, kh, "usage:write"),
        authoritative=False,
        tenant_gate_key=_request_tenant_key(request, kh),
    )


# ── Sequential quality streams (brief b4) ─────────────────────────────────────
# One always-valid mSPRT stream per customer key. In-memory for now (process
# lifetime); serialized state is exposed via /v1/quality/stream for auditability.
_seq_streams = BoundedTTLMap[str, object](
    ttl_s=_RESOURCE_BOUNDS.registry_ttl_s,
    max_entries=_RESOURCE_BOUNDS.registry_max_entries,
    max_value_bytes=1024,
    sizer=lambda _value: 256,
    copier=lambda value: value,
    snapshotter=lambda value: value,
)


def _seq_stream(kh: str):
    from token_efficiency_model.quality.sequential import SequentialQualityGate
    return _seq_streams.get_or_create(
        kh,
        lambda: SequentialQualityGate(
            p0=float(os.environ.get("BREVITAS_QUALITY_P0", "0.9")),
            alpha=float(os.environ.get("BREVITAS_QUALITY_ALPHA", "0.05")),
        ),
    )


@app.get("/v1/quality/stream")
def quality_stream(request: Request, kh: str = Depends(_authenticated)):
    """Auditable state of this customer's sequential quality stream."""
    _require_scope(request, kh, "usage:read_own")
    return _seq_stream(_request_tenant_key(request, kh)).to_dict()


@app.post("/v1/quality/stream/reset")
def quality_stream_reset(request: Request, kh: str = Depends(_authenticated)):
    """Reset a tripped stream (after investigation). Deliberately explicit.
    Also clears this tenant's lever trips so the request-path levers re-enable together
    with the billing stream (the two must not drift apart)."""
    _require_scope(request, kh, "usage:read_own")
    tenant_gate_key = _request_tenant_key(request, kh)
    _seq_streams.pop(tenant_gate_key, None)
    from token_efficiency_model.quality.gate import reset_all_levers
    reset_all_levers(key=tenant_gate_key)
    return {"reset": True}


@app.get("/v1/provider-costs")
def provider_costs():
    return {"pricing_as_of": "2026-07-10", "costs_per_1m_tokens": PROVIDER_COSTS_PER_1M}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/v1/stats")
@limiter.limit("120/minute")
def stats(request: Request, kh: str = Depends(_authenticated)):
    _require_scope(request, kh, "usage:read_own")
    return _store.get_stats(kh)


@app.get("/v1/stats/breakdown")
@limiter.limit("120/minute")
def stats_breakdown(request: Request, kh: str = Depends(_authenticated)):
    _require_scope(request, kh, "usage:read_own")
    rows = _store.get_breakdown(kh)
    return {"rows": rows, "totals": _store.get_stats(kh)}


@app.get("/v1/admin/stats")
@limiter.limit("60/minute")
def admin_stats(request: Request, _: str = Depends(_admin_authenticated)):
    logger.info("admin usage overview accessed actor=%s", _)
    return _store.get_admin_stats()


@app.get("/v1/admin/keys")
@limiter.limit("60/minute")
def admin_keys(request: Request, _: str = Depends(_admin_authenticated)):
    logger.info("admin key inventory accessed actor=%s", _)
    return _store.get_admin_key_inventory()


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
    cursor: str = Query("", max_length=512),
    _: str = Depends(_admin_authenticated),
):
    logger.info("admin usage breakdown accessed actor=%s", _)
    start = ""
    if range != "all":
        days = int(range[:-1])
        start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    filters = {"start": start, "owner_id": account, "project": project,
               "client": client, "provider": provider, "model": model}
    try:
        report = _store.get_admin_report_page(
            filters, sort=sort, direction=direction, cursor=cursor, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid pagination cursor") from exc
    return {**report, "range": range}


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
    _require_scope(request, kh, "usage:read_own")
    return _store.get_stats_by_pipeline(kh)


@app.get("/v1/stats/agents")
@limiter.limit("120/minute")
def stats_agents(request: Request, pipeline: str = "", kh: str = Depends(_authenticated)):
    _require_scope(request, kh, "usage:read_own")
    return _store.get_stats_by_agent(kh, pipeline=pipeline)


@app.get("/v1/stats/runs")
@limiter.limit("120/minute")
def stats_runs(request: Request, pipeline: str = "", kh: str = Depends(_authenticated)):
    _require_scope(request, kh, "usage:read_own")
    return _store.get_stats_by_run(kh, pipeline=pipeline)


_COMPRESSOR_STATUS: dict = {"ts": 0.0, "data": None}
_COMPRESSOR_TTL = 30.0  # seconds — probe the microservice at most once per window
_COMPRESSOR_STATUS_LOCK = threading.Lock()
_COMPRESSOR_INFLIGHT: concurrent.futures.Future | None = None
_COMPRESSOR_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="brevitas-compressor-probe")


def _production_runtime() -> bool:
    """Compatibility name for the hosted fail-closed runtime boundary."""
    return hosted_runtime()


def _private_compressor_url(url: str) -> bool:
    """Only permit the Railway private DNS endpoint in production.

    Loopback remains valid for local development and container tests. The URL itself is never
    returned by health checks or logs because Railway service names can reveal topology.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return not _production_runtime()
    return host.endswith(".railway.internal")


def _compressor_status_base(url: str) -> dict:
    return {
        "configured": bool(url),
        "internal_auth_configured": bool(os.getenv("BREVITAS_COMPRESS_TOKEN", "").strip()),
        "private_endpoint": _private_compressor_url(url) if url else False,
        "reachable": False,
        "model_loaded": False,
    }


def _compressor_probe(url: str, timeout: float, base: dict) -> dict:
    data = dict(base)
    if not url:
        return data
    try:
        response = _requests.get(f"{url}/ready", timeout=(timeout, timeout))
        if response.ok:
            data["reachable"] = True
            data["model_loaded"] = bool(response.json().get("model_loaded"))
    except Exception:
        pass
    return data


async def _compressor_status() -> dict:
    """Return a bounded, cached, single-flight private-compressor probe.

    The blocking HTTP client runs in a worker thread. Concurrent readiness requests share one
    probe and health payloads expose only non-secret booleans.
    """
    global _COMPRESSOR_INFLIGHT
    now = _time.monotonic()
    url = os.getenv("BREVITAS_COMPRESS_URL", "").rstrip("/")
    base = _compressor_status_base(url)
    try:
        timeout = float(os.getenv("BREVITAS_COMPRESS_PROBE_TIMEOUT_SECONDS", "1"))
    except (TypeError, ValueError):
        timeout = 1.0
    timeout = min(5.0, max(0.1, timeout))
    try:
        wait_timeout = float(os.getenv(
            "BREVITAS_COMPRESS_PROBE_WAIT_SECONDS", str(timeout * 2 + 0.25)))
    except (TypeError, ValueError):
        wait_timeout = timeout * 2 + 0.25
    wait_timeout = min(10.0, max(0.01, wait_timeout))

    started = False
    with _COMPRESSOR_STATUS_LOCK:
        cached = _COMPRESSOR_STATUS["data"]
        if cached is not None and now - _COMPRESSOR_STATUS["ts"] < _COMPRESSOR_TTL:
            return dict(cached)
        future = _COMPRESSOR_INFLIGHT
        if future is None:
            future = _COMPRESSOR_EXECUTOR.submit(_compressor_probe, url, timeout, base)
            _COMPRESSOR_INFLIGHT = future
            started = True

    if started:
        def publish(completed: concurrent.futures.Future) -> None:
            global _COMPRESSOR_INFLIGHT
            try:
                data = completed.result()
            except Exception:
                data = base
            with _COMPRESSOR_STATUS_LOCK:
                if _COMPRESSOR_INFLIGHT is completed:
                    _COMPRESSOR_STATUS.update(
                        ts=_time.monotonic(), data=dict(data))
                    _COMPRESSOR_INFLIGHT = None

        future.add_done_callback(publish)

    try:
        wrapped = asyncio.wrap_future(future)
        return dict(await asyncio.wait_for(
            asyncio.shield(wrapped), timeout=wait_timeout))
    except asyncio.CancelledError:
        raise
    except (Exception, asyncio.TimeoutError):
        # The dedicated thread remains the single owner. Its callback publishes the eventual
        # result and clears the marker only after the underlying probe actually terminates.
        return base


def _warn_if_compressor_missing(status: dict):
    """Loud-once on boot if lossy compression is enabled but no compressor is reachable —
    otherwise the compress path silently degrades to lossless and nobody notices."""
    if not _lossy_enabled():
        logger.info("BREVITAS_COMPRESS_LOSSY disabled — /v1/compress is strict-lossless.")
        return
    st = status
    if not st["configured"]:
        logger.warning("Lossy compression ON but BREVITAS_COMPRESS_URL is unset — "
                       "/v1/compress will fall back to lossless (0%% savings on single prompts).")
    elif not st["reachable"] or not st["model_loaded"]:
        logger.warning("Lossy compression ON but the compress microservice is "
                       "unreachable/not-loaded (%s) — falling back to lossless.", st)


@app.get("/v1/health")
@app.get("/v1/health/ready")
async def health():
    compressor = await _compressor_status()
    compressor_healthy = all(
        compressor[name] for name in (
            "configured", "internal_auth_configured", "private_endpoint", "reachable",
            "model_loaded",
        )
    )
    compressor_required = os.getenv(
        "BREVITAS_COMPRESS_REQUIRED", "false").lower() in {"1", "true", "yes"}
    compressor_active = _lossy_enabled() or compressor_required
    compressor_ready = not compressor_active or compressor_healthy
    dependency_timeout = max(0.1, float(os.getenv("BREVITAS_HEALTH_TIMEOUT_SECONDS", "3")))
    try:
        database_ready = await asyncio.wait_for(
            asyncio.to_thread(_store.healthy), timeout=dependency_timeout,
        )
    except (Exception, asyncio.TimeoutError):
        database_ready = False
    try:
        redis_ready = await asyncio.wait_for(
            _distributed_limiter.healthy(), timeout=dependency_timeout,
        )
    except (Exception, asyncio.TimeoutError):
        redis_ready = False
    kms = await _kms_readiness_status()
    kms_ready = _kms_dependency_ready(kms)
    accepting_traffic = bool(getattr(app.state, "accepting_traffic", False))
    core_ready = accepting_traffic and database_ready and redis_ready and kms_ready
    compressor_blocks_readiness = compressor_required and not compressor_healthy
    payload = {
        "status": ("unavailable" if not core_ready or compressor_blocks_readiness else
                   "degraded" if not compressor_ready else "ok"),
        "accepting_traffic": accepting_traffic,
        "database_ready": database_ready,
        "redis_ready": redis_ready,
        "kms_ready": kms_ready,
        "compressor": compressor,
        "dependencies": {
            "postgres": {
                "status": "ready" if database_ready else "unavailable",
                "authoritative": True,
            },
            "redis": {
                "status": "ready" if redis_ready else "unavailable",
                "authoritative": False,
                "role": "coordination",
            },
            "kms": {
                "status": (
                    "disabled" if not kms["configured"] else
                    "ready" if kms_ready else "unavailable"
                ),
                **kms,
            },
            "compressor": {
                "status": "ready" if compressor_healthy else "unavailable",
                "required": compressor_required,
            },
        },
    }
    return payload if core_ready and not compressor_blocks_readiness else JSONResponse(
        payload, status_code=503)


@app.get("/v1/health/live")
async def liveness():
    """Process-only probe: dependency outages must not trigger a restart storm."""
    return {"status": "ok"}


@app.get("/v1/version")
async def version():
    """Public, non-secret identity for matching a deployment to its tested source."""
    return {"service": "api", "build": build_identity(required=_production_runtime())}


def _hosted_proxy_receipt(raw_key: str, payload: dict) -> None:
    """In-process bridge: hosted proxy receipts use the caller's tenant key."""
    if not raw_key:
        return
    kh = hash_key(raw_key)
    if not _key_exists(kh):
        return
    payload = dict(payload)
    tenant_gate_key = str(payload.pop("_brevitas_tenant_key", "") or kh)
    context = _proxy_auth_context.get()
    if context is None or context.key_hash != kh:
        context = _auth_context_for_key(kh)
    _record_usage_report(kh, UsageReportRequest.model_validate(payload),
                         auth_context=context, authoritative=True,
                         tenant_gate_key=tenant_gate_key)


# Railway serves the management API and provider-compatible proxy from one process.
from brevitas.proxy import proxy_app, set_usage_reporter
set_usage_reporter(_hosted_proxy_receipt)
app.include_router(company_admin_router)
app.include_router(compliance_admin_router)
app.include_router(proxy_app.router)
app.add_middleware(
    _AggregateRequestBoundsMiddleware,
    max_bytes=_RESOURCE_BOUNDS.request_max_bytes,
    max_items=_RESOURCE_BOUNDS.request_max_items,
)
install_fastapi_observability(app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
