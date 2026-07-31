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
import re
import secrets
import sqlite3
import threading
import time as _time
import uuid
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests as _requests
import httpx
from fastapi import FastAPI, HTTPException, Header, Depends, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from typing import Any, Callable, FrozenSet, List, Optional

from token_efficiency_model.lossless.api_adapter import retrieval_select
from token_efficiency_model.lossless.provider_cache import count_tokens
from token_efficiency_model.lossless.message_optimizer import optimize_message_text

from brevitas.provider_reliability import (
    ProviderCircuitOpen,
    bind_circuit_scope,
    close_provider_sync_clients,
    provider_sync_http,
)
from brevitas.resource_bounds import BoundedTTLMap, ResourceBounds, ResourceLimitExceeded
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
    ROLE_PERMISSIONS,
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
    record_savings_row,
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
from brevitas.receipts import (TokenReceipt, calculate_costs, normalize_usage,
                               MODEL_PRICES, PRICING_VERSION, model_price)
from brevitas.identity import CUSTOMER_ID_HEADER, normalize_customer_id, tenant_key
from .store import make_store, PROVIDER_COSTS_PER_1M
from brevitas.semantic_cache import make_semantic_cache
from brevitas.warming import WarmPrefix, set_warm_observer

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
    """Bucket rate limits on the verified network peer only.

    The X-Brevitas-Key / X-API-Key headers are attacker-controlled and unverified at
    limiter time, so keying on them let any caller mint a fresh empty bucket per request
    by rotating a random header value — bypassing every @limiter.limit. Identity is never
    trusted before authentication here; per-key quotas are enforced separately downstream.
    Behind an edge proxy the peer is only meaningful when uvicorn is launched with
    --forwarded-allow-ips (see Dockerfile / FORWARDED_ALLOW_IPS), otherwise every request
    collapses onto the edge IP.
    """
    return request.client.host if request.client else "unknown"


def _job_poll_rate_key(request: Request) -> str:
    """Bucket the documented 202-poll route on the caller's credential, not the peer.

    GET /v1/jobs/{job_id} is a status poll the API itself tells clients to repeat, and
    _rate_key collapses every process behind one NAT egress onto one bucket — a customer
    polling a handful of healthy jobs at 1 Hz from one office IP would eat 429s. Keying on
    the credential digest is safe HERE, despite _rate_key's warning, because the
    _authenticated dependency has already resolved (and 401'd unknown keys) before slowapi
    consults the bucket on this route — rotating garbage header values never reaches the
    limiter, and rotating VALID keys only spreads one tenant's own budget across that
    tenant's own credentials. Cross-tenant fairness is the per-organization active-job
    ceiling, not this brake. Digest, never the raw header: limiter storage (memory or
    Redis) must not hold key material.
    """
    raw_key = (request.headers.get("x-brevitas-key", "")
               or request.headers.get("x-api-key", ""))
    if raw_key:
        return f"jobpoll:{hash_key(raw_key)[:32]}"
    return _rate_key(request)


def _rate_limit_storage_uri() -> str:
    """Shared storage for the control-plane limits, or "" for per-process memory.

    With memory storage every documented limit is really N_replicas x the number
    (railway.json declares numReplicas: 2) and resets on every deploy — exactly when an
    abuse burst is cheapest. Redis makes the counters one fleet-wide bucket, matching what
    api/distributed_limits.py already does for the proxy path.

    Deliberately reuses REDIS_URL, so no new production secret is needed; when it is unset
    (dev, tests, CI) the limiter keeps today's in-memory behaviour.
    """
    url = os.getenv("BREVITAS_RATE_LIMIT_STORAGE_URI", "").strip()
    if url:
        return url
    redis_url = os.getenv("REDIS_URL", "").strip()
    return redis_url if redis_url.startswith(("redis://", "rediss://")) else ""


def _build_limiter() -> Limiter:
    # slowapi 0.1.10 evaluates limits through the SYNCHRONOUS limits strategies, so a
    # shared backend costs one blocking Redis round trip (~1-3 ms intra-region) per
    # rate-limited request. That is acceptable on the control plane — the proxy hot path
    # carries no @limiter.limit at all and is admitted by the async DistributedLimiter —
    # but it is the reason this is not wired to an async storage: there is no async-capable
    # hook in this slowapi version. in_memory_fallback_enabled keeps a Redis outage
    # degrading to today's per-process counters instead of 500ing every route.
    storage_uri = _rate_limit_storage_uri()
    if not storage_uri:
        return Limiter(key_func=_rate_key)
    try:
        return Limiter(key_func=_rate_key, storage_uri=storage_uri,
                       in_memory_fallback_enabled=True)
    except Exception as exc:
        logger.error("shared rate-limit storage unavailable, falling back to per-process "
                     "memory error_type=%s", type(exc).__name__)
        return Limiter(key_func=_rate_key)


limiter = _build_limiter()


def _rate_limit_storage_is_shared() -> bool:
    """True when the limiter counters are fleet-wide rather than per replica."""
    storage = getattr(limiter, "_storage", None)
    return bool(storage is not None
                and type(storage).__name__ not in {"MemoryStorage", "NoneType"})


# ── App setup ─────────────────────────────────────────────────────────────────

def _configured_allowed_origins() -> list[str]:
    """Return the normalized CORS allowlist used by middleware and startup checks."""

    return [
        origin.strip()
        for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]


_ALLOWED_ORIGINS = _configured_allowed_origins()


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
    origins = _configured_allowed_origins()
    if not origins or "*" in origins:
        raise RuntimeError(
            "Production requires an explicit ALLOWED_ORIGINS allowlist")
    forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "").strip()
    if not forwarded_allow_ips:
        raise RuntimeError(
            "Production requires FORWARDED_ALLOW_IPS so the rate-limit peer address is "
            "read from the trusted edge proxy instead of collapsing to one global bucket")
    if "*" in {hop.strip() for hop in forwarded_allow_ips.split(",")}:
        # "*" makes uvicorn trust the entire client-supplied X-Forwarded-For chain and
        # resolve the peer to its left-most (client-controlled) entry, so any caller can
        # send a random X-Forwarded-For per request and mint a fresh rate-limit bucket —
        # re-opening the exact bypass _rate_key was hardened to close. Name the specific
        # trusted proxy hop(s) instead (the Railway edge IP/CIDR).
        raise RuntimeError(
            "Production FORWARDED_ALLOW_IPS must name the specific trusted proxy hop(s), "
            "not '*': trusting '*' lets any client spoof X-Forwarded-For to bypass rate "
            "limiting. Set it to the Railway edge IP/CIDR (see GO_LIVE_RUNBOOK Phase 2).")
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url and not redis_url.startswith("rediss://"):
        raise RuntimeError("Production REDIS_URL must use TLS (rediss://)")
    if not _rate_limit_storage_is_shared():
        # Deliberately loud rather than fatal: tests/test_production_topology.py pins
        # startup as valid without REDIS_URL, so promoting this to a RuntimeError belongs
        # in the same change that updates that contract. Per-process storage means every
        # documented limit is really replica-count x the number and resets on each deploy.
        logger.error(
            "control-plane rate limits are using per-process memory storage: set REDIS_URL "
            "(or BREVITAS_RATE_LIMIT_STORAGE_URI) so the counters are fleet-wide")


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
    """Iterative on purpose: this runs pre-authentication in the outermost middleware.

    A recursive walk costs ~2 Python frames per nesting level against json.loads's one
    C-level check, so a few hundred nested arrays (under 1 KB) raised RecursionError here
    — a RuntimeError nobody caught. brevitas/proxy.py:_json_object already uses this
    worklist shape; the two enforce the same request_max_items bound.
    """
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            if len(item) > maximum:
                return True
            pending.extend(item)
        elif isinstance(item, dict):
            if len(item) > maximum:
                return True
            pending.extend(item.values())
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
            except RecursionError:
                # json.loads itself exhausts the C recursion budget past ~1000 nesting
                # levels. The body is then un-inspectable here, not oversized: leave the
                # 400 to the handler's own parse instead of raising a pre-auth 500.
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
    # Only the process-only liveness probe is cacheable. /v1/health carries live dependency
    # verdicts and is served from the public marketing origin, where an intermediary could
    # otherwise hand a stale "ok" (or a stale 503) to whoever asks next.
    if request.url.path != "/v1/health/live":
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
    # Live company role behind a dashboard-session key, resolved by the
    # per-request membership revalidation below. Empty for every other key type:
    # device/service/legacy keys carry no human role.
    company_role: str = ""

    def permits(self, scope: str) -> bool:
        return scope in self.scopes or "*" in self.scopes

    def holds_company_permission(self, permission: str) -> bool:
        """True only for a human session whose CURRENT role grants `permission`.

        Mirrors public.company_role_permissions so this gate cannot drift from
        /api/billing/status, which already refuses billing data to roles without
        billing:manage.
        """
        if self.key_type != "dashboard_session":
            return False
        return permission in ROLE_PERMISSIONS.get(self.company_role, frozenset())


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


def _device_credential_max_age_s() -> int:
    """Optional lifetime ceiling for `bvx login` device credentials (0 = none).

    Device keys are inserted with expires_at NULL, so nothing bounds them. A hard
    expiry cannot be the default yet: it needs a CLI re-login path and the
    onboarding evidence gates (202607200016 / 202607280004) still require the
    credential to be LIVE, so expiring one silently regresses cli_connected. This
    knob lets an operator bound them now and stays inert until they do.
    """
    try:
        return max(0, int(os.getenv("BREVITAS_DEVICE_KEY_MAX_AGE_SECONDS", "0")))
    except (TypeError, ValueError):
        return 0


def _device_credential_expired(row: dict) -> bool:
    if str(row.get("key_type") or "") != "device":
        return False
    max_age = _device_credential_max_age_s()
    if max_age <= 0:
        return False
    created = str(row.get("created") or "")
    if not created:
        return False
    try:
        minted = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return False
    if minted.tzinfo is None:
        minted = minted.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - minted).total_seconds() > max_age


def _require_current_dashboard_membership(context: AuthContext) -> str:
    """Revalidate a dashboard-session key's exact human membership every request.

    Returns the live company role (empty for other key types). Device keys are
    deliberately NOT revalidated here: their contexts are cached for at most 30s
    (_auth_context_cache), so event-driven revocation converges within that window
    without putting a Supabase round trip on every /v1/compress call.
    """
    if context.key_type != "dashboard_session":
        return ""
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
    return membership_role


def _customer_provision_cap() -> int:
    try:
        return max(1, int(os.getenv("BREVITAS_MAX_CUSTOMERS_PER_ORG", "10000")))
    except (TypeError, ValueError):
        return 10_000


def _enforce_customer_provision_quota(organization_id: str) -> None:
    """Bound the one write path a client header can drive on its own.

    X-Brevitas-Customer-ID mints a permanent `customers` row on every lookup miss — there
    is no TTL and no purge for them — so a key holding customer:route plus
    customer:auto_provision can convert its RPM allowance into millions of junk rows in the
    shared Postgres, each also costing two extra PostgREST round trips on the auth path.
    The count is racy by nature; bounded overshoot is acceptable for a DoS control. The
    durable guard belongs inside rpc/import_enterprise_customers so POST
    /v1/customers/import is covered by the same rule.
    """
    probe = getattr(_store, "customer_count_at_least", None)
    if not callable(probe):
        return
    try:
        exhausted = bool(probe(organization_id, _customer_provision_cap()))
    except Exception as exc:
        # Availability over the quota: a store blip must not reject live traffic.
        logger.warning("customer quota probe unavailable error_type=%s",
                       type(exc).__name__)
        return
    if exhausted:
        logger.error("customer auto-provisioning refused: organization at quota")
        raise HTTPException(
            status_code=429, detail="Customer limit reached for this organization",
            headers={"Retry-After": "60"},
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
            _enforce_customer_provision_quota(organization_id)
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
    if _device_credential_expired(row):
        raise HTTPException(status_code=401, detail="Device credential expired")
    company_role = _require_current_dashboard_membership(context)
    if company_role:
        context = replace(context, company_role=company_role)
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


# Admission control only. A full BPE encode of a 2 MiB body (the request_max_bytes
# default, env-raisable to 16 MiB) costs 100-200+ ms of pure CPU, and it ran on the single
# event loop BEFORE _distributed_limiter.acquire could reject anything — so one tenant's
# maximum-size prompts stalled every other tenant on the replica, and a request destined
# for a 429 still burned the whole encode. asyncio.to_thread would only relocate that CPU
# into the same default executor the auth lookups use.
_TOKEN_ESTIMATE_SAMPLE_BYTES = 64 * 1024


def _estimated_token_cost(raw_body: bytes) -> int:
    """Bounded token estimate for the rate limiter — never for billing.

    Tokenizes a head sample and scales by total length, which keeps the tokens-per-byte
    ratio of the actual payload: a high-entropy body still gets charged at its real
    density (~1.5 bytes/token), where a flat len//4 would under-charge it ~2.7x and walk
    it through the TPM guard. Billed tokens come from provider receipts (brevitas/receipts),
    never from here — token_cost feeds _distributed_limiter.acquire and nothing else.
    """
    if not raw_body:
        return 1
    head = raw_body[:_TOKEN_ESTIMATE_SAMPLE_BYTES]
    sample = count_tokens(head.decode("utf-8", errors="ignore"))
    if len(raw_body) > len(head):
        sample = sample * len(raw_body) // len(head)
    return max(1, sample)


def _key_validity(kh: str) -> str:
    """Return valid / unknown / store_unavailable — fail-closed, but attributable.

    A bare boolean collapses a store outage into "this key is not valid", which on the
    billable receipt path (_hosted_proxy_receipt) is the difference between a tenant
    that legitimately has no key and a Supabase blip silently discarding every
    authoritative usage row. Callers that only need a yes/no still get fail-closed
    behaviour from _key_exists; nothing here ever fails open.
    """
    with _valid_key_lock:
        if _valid_key_cache.get(kh, False):
            return "valid"
    try:
        row = _store.key_context(kh)
        valid = bool(row)
        if valid and str(row.get("key_type") or "") == "organization_service":
            valid = _authoritative_service_key_context(kh) is not None
    except Exception:
        return "store_unavailable"
    if valid:
        with _valid_key_lock:
            _valid_key_cache.put(kh, True)
    return "valid" if valid else "unknown"


def _key_exists(kh: str) -> bool:
    return _key_validity(kh) == "valid"


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


# Warming is org-level opt-in read on every proxy request; the TTL cache keeps
# the store lookup off the hot path (one RPC per org per minute on Supabase).
_warm_enabled_cache = BoundedTTLMap[str, bool](
    ttl_s=min(60, _RESOURCE_BOUNDS.registry_ttl_s),
    max_entries=_RESOURCE_BOUNDS.registry_max_entries,
    max_value_bytes=16,
    sizer=lambda _value: 1,
    copier=lambda value: value,
)


def _warm_enabled_cached(organization_id: str, customer_id: str = "") -> bool:
    """Warming is best-effort: a store failure means not-warm, never a 503."""
    if not organization_id:
        return False
    cached = _warm_enabled_cache.get(organization_id)
    if cached is not None:
        return cached
    try:
        enabled = bool(_store.warm_enabled(organization_id, customer_id))
    except Exception as exc:
        logger.warning("warm policy lookup unavailable error_type=%s",
                       type(exc).__name__)
        return False
    _warm_enabled_cache.put(organization_id, enabled)
    return enabled


# Cache policy is a per-tenant boolean read on EVERY authenticated request (including
# POST /v1/usage at 300/minute) and on every proxied completion, and it was both uncached
# — 1-2 PostgREST GETs per request — and fail-closed, so a Supabase blip on a feature flag
# turned into a 503 for inference traffic and lost usage receipts. Keyed on
# (organization, customer) because SupabaseUsageStore.cache_enabled consults the customer
# row first and only falls back to the organization: an org-only key would leak one
# customer's override onto its siblings. The TTL is deliberately short because
# PUT /v1/cache-policy {enabled:false} answers {"purged": true} — the other replica keeps
# serving cache for at most this window (railway.json numReplicas: 2).
_CACHE_ENABLED_TTL_S = 30
_cache_enabled_cache = BoundedTTLMap[str, bool](
    ttl_s=min(_CACHE_ENABLED_TTL_S, _RESOURCE_BOUNDS.registry_ttl_s),
    max_entries=_RESOURCE_BOUNDS.registry_max_entries,
    max_value_bytes=16,
    sizer=lambda _value: 1,
    copier=lambda value: value,
)


def _cache_policy_cache_key(organization_id: str, customer_id: str = "") -> str:
    return f"{organization_id}\x00{customer_id}"


def _cache_enabled_cached(organization_id: str, customer_id: str = "") -> bool:
    """Caching is best-effort: a store failure means not-cached, never a 503.

    Same contract as _warm_enabled_cached. Failing open to False can only disable an
    optimization; it can never serve a cached answer the tenant did not consent to.
    """
    if not organization_id:
        return False
    cache_key = _cache_policy_cache_key(organization_id, customer_id)
    cached = _cache_enabled_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        enabled = bool(_store.cache_enabled(organization_id, customer_id))
    except Exception as exc:
        logger.warning("cache policy lookup unavailable error_type=%s",
                       type(exc).__name__)
        return False
    _cache_enabled_cache.put(cache_key, enabled)
    return enabled


def _invalidate_cache_policy(organization_id: str) -> None:
    """Drop every cached decision for one organization, before the caller is answered.

    An organization-level change flips the fallback for all of its customers, so the whole
    org prefix goes — anything less keeps caching for up to the TTL after the API has
    already reported the namespaces purged.
    """
    prefix = f"{organization_id}\x00"
    for cache_key, _value in _cache_enabled_cache.items():
        if str(cache_key).startswith(prefix):
            _cache_enabled_cache.discard(cache_key)


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
                _cache_enabled_cached,
                auth_context.organization_id, auth_context.customer_id)
            request.state.brevitas_warm_enabled = await asyncio.to_thread(
                _warm_enabled_cached, auth_context.organization_id,
                auth_context.customer_id)
            _proxy_auth_context.set(auth_context)
            # Per-tenant circuit fairness (finding 44): attribute this request's
            # provider failures to the authenticated tenant's own circuit so one
            # tenant's engineered timeouts cannot 503 every other tenant. Bound
            # here — the one place the tenant is verified — exactly like the auth
            # context above, and set without reset for the same streaming-body
            # lifetime reason.
            bind_circuit_scope(auth_context.organization_id)
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
        token_cost = _estimated_token_cost(raw_body)
        # json.loads of a 2 MiB body is ~1 ms, so this stays on the loop deliberately: a
        # thread hop would only add queue latency to the executor the auth lookups share.
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
        # No `authoritative` default. Every caller of this helper is an advisory
        # transform endpoint that never observed a provider call, so it passes
        # authoritative=False explicitly. `authoritative` is the load-bearing
        # billing predicate (202607280007) and only _hosted_proxy_receipt, which
        # did observe the provider response, may set it — a default of True here
        # silently labelled every /v1/compress and Playground row as billable
        # evidence. _usage_row treats an omitted value as False, the safe
        # direction, so a future caller cannot re-acquire the label by accident.
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
    # Never a 503: an unreachable store on a caching feature flag must not reject the
    # caller's inference traffic or their usage receipts (see _cache_enabled_cached).
    request.state.brevitas_cache_enabled = _cache_enabled_cached(
        context.organization_id, context.customer_id)
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


COMPLIANCE_TENANT_HEADER = "X-Brevitas-Compliance-Tenant"


def _compliance_tenant_authority_only() -> bool:
    """Refuse the operator's own workspace as a compliance tenant when set.

    Default off so a running compliance workflow does not stop the moment this
    ships; turn it on once compliance_tenant_authority is populated.
    """
    return os.getenv("BREVITAS_COMPLIANCE_TENANT_AUTHORITY_ONLY", "").lower() in (
        "1", "true", "yes", "on")


def _authorized_compliance_tenant(actor_id: str, organization_id: str) -> str:
    """Return the named tenant only if a platform grant authorizes this operator."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", organization_id):
        raise HTTPException(status_code=400, detail="Invalid compliance tenant")
    checker = getattr(_store, "compliance_tenant_authority", None)
    if not callable(checker):
        raise HTTPException(
            status_code=503, detail="Compliance tenant authority unavailable",
            headers={"Retry-After": "1"},
        )
    try:
        authorized = bool(checker(actor_id, organization_id))
    except ValueError:
        return ""
    except Exception as exc:
        logger.error("compliance tenant authority unavailable error_type=%s",
                     type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Compliance tenant authority unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    return organization_id if authorized else ""


def _compliance_admin_principal(request: Request) -> ComplianceAdminPrincipal:
    """Derive compliance authority only from verified identity and live DB state.

    A DSR belongs to the SUBJECT's tenant, which is rarely one the operator is a
    member of, so the request names it explicitly and an immutable platform grant
    (compliance_tenant_authority, re-checked inside the submit RPCs) decides. The
    operator's own active workspace is a mutable UI preference and must not choose
    the tenant a two-person-approved erasure lands in; it survives only as a
    transitional fallback for when no tenant is named.
    """
    identity = _dashboard_identity(request)
    actor_id = str(identity.get("id") or "")
    metadata = identity.get("app_metadata")
    if (not actor_id or not isinstance(metadata, dict)
            or metadata.get("role") != "brevitas_admin"):
        return ComplianceAdminPrincipal(actor_id, "", "")
    named_tenant = str(request.headers.get(COMPLIANCE_TENANT_HEADER, "") or "").strip()
    if named_tenant:
        return ComplianceAdminPrincipal(
            actor_id, _authorized_compliance_tenant(actor_id, named_tenant),
            "brevitas_admin")
    if _compliance_tenant_authority_only():
        logger.error("compliance request named no tenant and the workspace "
                     "fallback is disabled")
        return ComplianceAdminPrincipal(actor_id, "", "brevitas_admin")
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


def _audit_platform_read(request: Request, actor_id: str, action: str, *,
                         target_type: str = "platform",
                         target_id: str = "all_tenants") -> None:
    """Attribute a Brevitas-staff cross-tenant read in audit_events before serving it.

    The DB audit log is the authoritative record (docs/OBSERVABILITY.md) and general
    telemetry may not carry actor or tenant identity, so there is no log-only
    substitute: the %s-style logger.info lines these routes used to carry emitted
    nothing at all, because JsonLogFormatter never reads record.args.

    organization_id stays NULL: these reads span every tenant.
    """
    recorder = getattr(_store, "append_audit_event", None)
    if not callable(recorder):
        logger.error("platform admin read is unattributable action=%s", action)
        return
    state = getattr(request, "state", None)
    try:
        recorder(
            action=action, target_type=target_type, target_id=target_id,
            actor_id=actor_id or "admin", actor_role="brevitas_admin",
            request_id=str(getattr(state, "brevitas_request_id", "") or ""),
        )
    except Exception as exc:
        logger.error("platform admin audit write failed action=%s error_type=%s",
                     action, type(exc).__name__)
        # Fail closed on the hosted store: an unattributable cross-tenant read is
        # exactly what an enterprise audit review asks us to prove cannot happen.
        if hasattr(_store, "_request"):
            raise HTTPException(
                status_code=503, detail="Audit trail unavailable",
                headers={"Retry-After": "1"},
            ) from exc


def _audit_tenant_mutation(request: Request, organization_id: str, actor_id: str,
                           actor_role: str, action: str, *, target_type: str,
                           target_id: str) -> None:
    """Attribute a credential/policy write that has ALREADY committed.

    Provider credentials, warming spend consent and cache policy are the three
    tenant mutations that never went through an audited RPC: provider_config has no
    updated_by/updated_at at all, warm_credentials overwrites consent_actor_id on
    every change, and warm_credentials_purge deletes the row outright. Without this
    row, "who swapped our provider key / wiped our cache" has no answer.

    Best effort on purpose: the write cannot be rolled back, so a failed append is
    logged loudly instead of being reported to the caller as a failed mutation. The
    durable fix is an append_company_audit call inside those security-definer
    functions, in the same transaction.
    """
    recorder = getattr(_store, "append_audit_event", None)
    if not callable(recorder) or not organization_id:
        logger.error("tenant mutation is unattributable action=%s", action)
        return
    try:
        recorder(
            action=action, target_type=target_type, target_id=target_id,
            actor_id=actor_id or "system", actor_role=actor_role or "legacy",
            request_id=str(getattr(request.state, "brevitas_request_id", "") or ""),
            organization_id=organization_id,
        )
    except Exception as exc:
        logger.error("tenant mutation audit write failed action=%s error_type=%s",
                     action, type(exc).__name__)


def _key_actor_audit_identity(context: AuthContext) -> tuple[str, str]:
    """Audit (actor_id, actor_role) for an API-key caller — never the key hash.

    validate_audit_event_insert rejects a 64-hex actor_id and any non-null
    actor_key_hash, so the resolved human/service identity is the only usable one.
    """
    if context.key_type == "dashboard_session":
        return context.actor_user_id or "system", context.company_role or "none"
    if context.key_type == "organization_service":
        return context.service_account_id or "system", "service_account"
    # A device/legacy credential's owner_id is the BILLING owner, not the holder,
    # so naming it here would be a false attribution.
    return "system", "legacy"


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
    # Developer machines share the production project token, so without this every
    # `npm run dev` session lands in the customer-facing numbers. At the volumes this
    # dashboard reports, a single local QA pass dominates the signup ratio — two of the
    # nine signup attempts on 2026-07-29 were http://localhost:3000.
    real_traffic = (
        "NOT startsWith(toString(properties.$host), 'localhost') "
        "AND NOT startsWith(toString(properties.$host), '127.0.0.1')"
    )
    overview = _posthog_query(f"""
        SELECT
          countIf(event = '$pageview') AS pageviews,
          uniqIf(distinct_id, event = '$pageview') AS visitors,
          uniqIf(toString(properties.$session_id), event = '$pageview') AS sessions,
          uniqIf(distinct_id, event = 'signup_started') AS signup_started,
          uniqIf(distinct_id, event = 'signup_submitted') AS signup_submitted,
          countIf(event = 'signup_started') AS signup_attempts,
          countIf(event = 'signup_failed') AS signup_failures
        FROM events
        WHERE timestamp >= now() - INTERVAL {interval}
          AND {real_traffic}
    """)
    session_rows = _posthog_query(f"""
        SELECT round(avg(duration), 1), round(100 * avg(if(pageviews <= 1, 1, 0)), 1)
        FROM (
          SELECT dateDiff('second', min(timestamp), max(timestamp)) AS duration,
                 countIf(event = '$pageview') AS pageviews
          FROM events
          WHERE timestamp >= now() - INTERVAL {interval}
            AND notEmpty(toString(properties.$session_id))
            AND {real_traffic}
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
          AND {real_traffic}
        GROUP BY day ORDER BY day
    """)
    totals = overview[0] if overview else [0, 0, 0, 0, 0, 0, 0]
    session_metrics = session_rows[0] if session_rows else [0, 0]
    project_id = os.getenv("POSTHOG_PROJECT_ID", "")
    ui_host = os.getenv("NEXT_PUBLIC_POSTHOG_UI_HOST", "https://us.posthog.com").rstrip("/")
    result = {
        "range_days": days,
        "pageviews": int(totals[0] or 0),
        "visitors": int(totals[1] or 0),
        "sessions": int(totals[2] or 0),
        # People, not button presses. `signup_attempts` keeps the raw press count so the
        # retry rate stays visible instead of silently inflating the conversion ratio.
        "signup_started": int(totals[3] or 0),
        "signup_submitted": int(totals[4] or 0),
        "signup_attempts": int(totals[5] or 0),
        "signup_failures": int(totals[6] or 0),
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


def _dashboard_session_scopes(actor_role: str) -> list[str]:
    """Scopes for a browser session key.

    quality:manage clears a deliberately-held quality trip, so it is minted only for the
    roles allowed to mutate company state — every other scope here is granted to a plain
    member as well. Long-lived organization_service keys deliberately never get it: the
    credential whose own reports trip the stream must not be able to clear the trip.
    In production the scope array is chosen inside
    company_admin_create_dashboard_session_key, which does not mint quality:manage, so
    this list only governs the dev/SQLite path. POST /v1/quality/stream/reset therefore
    authorizes on the live company role and treats the scope as an alternative, so nothing
    depends on this list reaching production.
    """
    scopes = ["proxy:invoke", "usage:read_own", "provider:read", "provider:manage"]
    if _canonical_company_role(actor_role) in ("company_owner", "company_admin"):
        scopes.append("quality:manage")
    return scopes


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
                scopes=_dashboard_session_scopes(actor_role),
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
        scopes = _dashboard_session_scopes(actor_role)
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
    # `bvx login` device credentials have their own audited revocation RPC (the
    # dashboard-session one rejects every other type by design), so read the type
    # first and let the store dispatch. Without this the Revoke button the
    # dashboard already renders for a device key can only ever answer 403.
    key_type = ""
    reader = getattr(_store, "organization_key_type", None)
    if callable(reader):
        try:
            key_type = str(reader(organization["id"], key_id) or "")
        except Exception as exc:
            raise _key_admin_unavailable(exc) from exc
    try:
        revoked = _store.revoke_organization_key(
            organization["id"], key_id, actor_user_id=owner_id,
            request_id=str(getattr(request.state, "brevitas_request_id", "")),
            actor_role=actor_role, key_type=key_type)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Key revocation denied") from exc
    except RuntimeError as exc:
        # Both are permanent refusals from the revocation dispatcher, not outages:
        # organization_service keys stay under the service-account lifecycle RPCs,
        # so retrying this route can never succeed.
        if str(exc).endswith(("forbidden_or_not_found",
                              "service_account_lifecycle_required")):
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


# Page size for GET /v1/customers. The default is generous on purpose: no dashboard or SDK
# caller pages yet, so a small default would silently truncate an existing tenant's list.
_CUSTOMER_PAGE_DEFAULT = 200
_CUSTOMER_PAGE_MAX = 500


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
def list_customers(
    request: Request,
    limit: int = Query(_CUSTOMER_PAGE_DEFAULT, ge=1, le=_CUSTOMER_PAGE_MAX),
    offset: int = Query(0, ge=0),
):
    """Paged: every sibling listing endpoint is, and this one materialized the whole
    `customers` table for an organization into one Python list in a shared replica."""
    _, organization = _member_organization(request)
    page = _store.list_customers(organization["id"], limit=limit + 1, offset=offset)
    has_more = len(page) > limit
    return {"customers": page[:limit], "limit": limit, "offset": offset,
            "has_more": has_more}


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


def _tenant_cache_namespace(request: Request) -> str:
    """The ONE semantic-cache namespace shape every writer must use.

    brevitas/proxy.py writes this form, and it is the only form the two cleanup paths know:
    the purge in set_cache_policy below, and compliance_delete_tenant's
    digest(org||':unattributed') / digest(org||':'||customer) branches. A key-derived
    namespace (sha256 of the tenant key) matched neither, so Playground-cached prompts and
    responses survived both {"purged": true} and a tenant-deletion DSR — and became
    unreconstructable once the key rotated. Returns "" when there is no resolved
    organization, in which case the caller must not cache at all.
    """
    organization_id = str(getattr(request.state, "brevitas_organization_id", "") or "")
    if not organization_id:
        return ""
    customer_id = str(getattr(request.state, "brevitas_customer_id", "") or "")
    return f"{organization_id}:{customer_id or 'unattributed'}"


@app.put("/v1/cache-policy")
@limiter.limit("30/minute")
def set_cache_policy(request: Request, body: CachePolicyRequest):
    user_id, organization = _member_organization(request, write=True)
    customer_id = ""
    if body.customer_external_id:
        customer = _store.find_customer(organization["id"], body.customer_external_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        customer_id = str(customer["id"])
    _store.set_cache_enabled(organization["id"], body.enabled, customer_id)
    # Synchronously, before the purge and before answering: otherwise this replica keeps
    # admitting cache reads for up to _CACHE_ENABLED_TTL_S after reporting purged: true.
    _invalidate_cache_policy(organization["id"])
    if not body.enabled:
        try:
            cache = make_semantic_cache()
            # Same shape as _tenant_cache_namespace — every writer must be purgeable here.
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
    actor_role = _canonical_company_role(organization.get("role"))
    target_id = f"{organization['id']}:{customer_id or 'organization'}"
    _audit_tenant_mutation(
        request, organization["id"], user_id, actor_role, "cache_policy.changed",
        target_type="cache_policy", target_id=target_id)
    if not body.enabled:
        _audit_tenant_mutation(
            request, organization["id"], user_id, actor_role, "cache.purged",
            target_type="cache_policy", target_id=target_id)
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


# These three were the only mutating job routes with no limit of any kind, while ~45
# neighbouring routes carry one. On the mutating routes _rate_key buckets on the verified
# peer IP, so this is a runaway-client / accident brake and NOT the anti-abuse control: an
# attacker rotating source addresses walks straight through it. The GET poll is different:
# this is a documented 202-poll API, so status polls bucket per credential
# (_job_poll_rate_key) with a budget sized for many concurrent healthy jobs — an office
# NAT must not 429 one customer because another is polling. Cross-tenant fairness has to
# come from a per-organization queued-job quota in JobService.submit (enforced AFTER the
# idempotency lookup, so a legitimate retry still returns the existing row) plus per-org
# fairness in claim_ai_job. Until that lands, a store that reports capacity is at least
# answered with a 429 here instead of being swallowed into a 503.
@app.post("/v1/jobs", status_code=202)
@limiter.limit("300/minute")
async def create_job(request: Request, body: JobRequest,
                     kh: str = Depends(_authenticated)):
    tenant = _job_tenant(request, kh, "jobs:create")
    try:
        row, created = await _job_service.submit(
            tenant, body, request.headers.get("idempotency-key", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ResourceLimitExceeded as exc:
        logger.warning("job submission refused: queue at capacity")
        raise HTTPException(
            status_code=429, detail="Too many queued jobs",
            headers={"Retry-After": "5"},
        ) from exc
    except _CREDENTIAL_DEPENDENCY_ERRORS as exc:
        raise _credential_dependency_unavailable(exc) from exc
    except Exception as exc:
        logger.error("job submission failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Job queue unavailable") from exc
    return JSONResponse({**row, "created": created}, status_code=202,
                        headers={"Cache-Control": "no-store"})


@app.get("/v1/jobs/{job_id}")
@limiter.limit("600/minute", key_func=_job_poll_rate_key)
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
@limiter.limit("120/minute")
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
    """Access review for the tenant: current members, devices, installations, keys.

    The key half needs actor/request/role context — the hosted store has no
    unauthenticated key reader — so thread it through the same way GET /v1/keys
    does, and surface a store failure as 503 rather than a bare 500.
    """
    actor_user_id, organization = _member_organization(request)
    actor_role = _canonical_company_role(organization.get("role"))
    try:
        if hasattr(_store, "_request"):
            active_organization, actor_role = _active_company_membership(actor_user_id)
            if (active_organization != str(organization.get("id") or "")
                    or not actor_role):
                raise HTTPException(status_code=403, detail="Organization access denied")
        return _store.organization_inventory(
            organization["id"], actor_user_id=actor_user_id,
            request_id=str(getattr(request.state, "brevitas_request_id", "")),
            actor_role=actor_role,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Organization access denied") from exc
    except Exception as exc:
        raise _key_admin_unavailable(exc) from exc


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
    context = _request_auth_context(request, kh)
    actor_id, actor_role = _key_actor_audit_identity(context)
    _audit_tenant_mutation(
        request, context.organization_id, actor_id, actor_role,
        "provider_credential.updated", target_type="provider_config",
        target_id=f"{context.organization_id}:{body.provider}")
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


# ── Predictive cache warming ──────────────────────────────────────────────────

_WARM_PROVIDERS = ("anthropic", "openai", "deepseek")
# Warming is active only where the cache math works AND a worker keep-alive
# path exists. Anthropic: 0.10x reads that refresh the TTL for free. DeepSeek:
# 0.02x reads on an automatic cache that persists hours while in use. Enabling
# any other provider would consent to spend that can never verifiably warm a
# cache; the per-provider reason below is surfaced in the 400.
_WARM_ACTIVE_PROVIDERS = ("anthropic", "deepseek")
_WARM_INACTIVE_REASONS = {
    # Economics work on gpt-5.6+ (0.10x cached reads, explicit breakpoints),
    # but OpenAI does not document TTL refresh on read, so keep-alive pings
    # are unverifiable spend — and no OpenAI ping pipeline exists yet.
    "openai": "gpt-5.6+ reads at 0.10x, but OpenAI does not document TTL "
              "refresh on read, so keep-alive pings cannot verifiably keep a "
              "cache warm",
}


class WarmingConfigRequest(BaseModel):
    provider: str
    provider_api_key: str = ""
    enabled: bool
    # Warming spends the organization's provider budget on keep-alive pings, so
    # enabling demands an explicit spend acknowledgement in the SAME request.
    accept_spend_terms: bool = False
    daily_budget_usd: float = Field(default=0.0, ge=0, le=99_999_999)
    max_warm_customers: int = Field(default=100, ge=1, le=1_000_000)
    max_pings_per_customer_day: int = Field(default=24, ge=1, le=10_000)


@app.get("/v1/warming")
@limiter.limit("60/minute")
def get_warming(request: Request):
    _, organization = _member_organization(request)
    try:
        status = _store.warm_status(organization["id"])
    except Exception as exc:
        raise _key_admin_unavailable(exc) from exc
    # Warming status carries the organization's daily provider spend budget and the
    # spend booked against it. A plain member keeps the operational view (which
    # providers are warm, ping/hit counts) but not the money, on the same
    # billing:manage rule /api/billing/status already enforces. Owners and admins
    # manage warming spend, so they always see it.
    role = _canonical_company_role(organization.get("role"))
    if (role in ("company_owner", "company_admin")
            or "billing:manage" in ROLE_PERMISSIONS.get(role, frozenset())):
        return status
    redacted = _without_spend_fields(status)
    if isinstance(redacted, dict):
        redacted["spend_redacted"] = True
    return redacted


@app.put("/v1/warming")
@limiter.limit("30/minute")
def set_warming(request: Request, body: WarmingConfigRequest):
    user_id, organization = _member_organization(request, write=True)
    if body.provider not in _WARM_PROVIDERS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown warming provider '{body.provider}'")
    if body.enabled and body.provider not in _WARM_ACTIVE_PROVIDERS:
        reason = _WARM_INACTIVE_REASONS.get(
            body.provider, "no keep-alive ping pipeline exists")
        raise HTTPException(
            status_code=400,
            detail=f"Warming for '{body.provider}' is not active ({reason}); "
                   f"only {', '.join(_WARM_ACTIVE_PROVIDERS)} can be enabled")
    if body.enabled and body.accept_spend_terms is not True:
        raise HTTPException(
            status_code=400,
            detail="Enabling warming requires accept_spend_terms=true in the same request")
    if body.provider_api_key:
        try:
            encrypted_key = _encrypt(body.provider_api_key, context={
                "purpose": "warm_provider_credential",
                "organization_id": organization["id"],
            })
        except _CREDENTIAL_DEPENDENCY_ERRORS as exc:
            raise _credential_dependency_unavailable(exc) from exc
    else:
        encrypted_key = ""  # keep-existing-key semantics live in the store
    try:
        saved = _store.warm_credentials_upsert(
            organization["id"], body.provider, encrypted_key, body.enabled,
            user_id, body.daily_budget_usd, body.max_warm_customers,
            body.max_pings_per_customer_day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _key_admin_unavailable(exc) from exc
    _warm_enabled_cache.discard(organization["id"])
    _audit_tenant_mutation(
        request, organization["id"], user_id,
        _canonical_company_role(organization.get("role")),
        "warming.consent_granted" if body.enabled else "warming.disabled",
        target_type="warm_credential",
        target_id=f"{organization['id']}:{body.provider}")
    masked = ("*" * 8 + body.provider_api_key[-4:]
              if len(body.provider_api_key) > 4 else "")
    return {"ok": True, "provider": saved["provider"],
            "enabled": bool(saved["enabled"]),
            "credential_state": saved["credential_state"], "masked_key": masked}


@app.delete("/v1/warming/{provider}")
@limiter.limit("30/minute")
def delete_warming(request: Request, provider: str):
    user_id, organization = _member_organization(request, write=True)
    if provider not in _WARM_PROVIDERS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown warming provider '{provider}'")
    try:
        purged = _store.warm_credentials_purge(organization["id"], provider)
    except Exception as exc:
        raise _key_admin_unavailable(exc) from exc
    _warm_enabled_cache.discard(organization["id"])
    if purged.get("credentials_deleted"):
        # warm_credentials_purge deletes the consent row, so this audit event is
        # the only remaining evidence that spend consent ever existed.
        _audit_tenant_mutation(
            request, organization["id"], user_id,
            _canonical_company_role(organization.get("role")),
            "warming.credential_deleted", target_type="warm_credential",
            target_id=f"{organization['id']}:{provider}")
    if not purged.get("credentials_deleted"):
        raise HTTPException(status_code=404,
                            detail="Warming is not configured for this provider")
    return {"purged": True, "provider": provider,
            "prefixes_deleted": int(purged.get("prefixes_deleted") or 0)}


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
            # Advisory transform: no provider call was observed, so this row
            # is telemetry, never billing evidence.
            authoritative=False,
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
        # Advisory transform: no provider call was observed, so this row
        # is telemetry, never billing evidence.
        authoritative=False,
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
        # Advisory transform: no provider call was observed, so this row
        # is telemetry, never billing evidence.
        authoritative=False,
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


class _ThreadEventChannel:
    """Worker-thread -> event-loop hand-off for the SSE endpoints.

    The previous drain — `await loop.run_in_executor(None, lambda: q.get(timeout=0.5))` in
    a loop — parked one DEFAULT-executor thread per open stream at ~100% duty cycle for the
    whole life of that stream (up to the 120 s provider read timeout). That is the same pool
    every asyncio.to_thread uses, including the proxy auth lookups and the readiness
    probe's _store.healthy, so enough concurrent streams degraded authentication for all
    tenants and could flap /v1/health/ready. call_soon_threadsafe + asyncio.Queue costs no
    pool thread at all, and awaiting with a timeout keeps request.is_disconnected() polled.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()

    def put(self, item: object) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            # The loop is gone (client aborted and the response finished): the worker is
            # already being cancelled through cancel_event, so dropping is correct.
            pass

    async def get(self, timeout: float) -> object:
        """Raises asyncio.TimeoutError, like queue.Empty did, so the caller can re-poll."""
        return await asyncio.wait_for(self._queue.get(), timeout)


@app.post("/v1/compress/stream")
@limiter.limit("60/minute")
async def compress_stream(request: Request, body: CompressRequest, kh: str = Depends(_authenticated)):
    _require_scope(request, kh, "proxy:invoke")
    # Resolve the store record and unwrap the provider credential before the
    # StreamingResponse commits a 200. This also avoids a second store/KMS read
    # in the worker thread.
    config, backend = await asyncio.to_thread(
        _resolve_configured_model_backend, kh, request)
    event_queue = _ThreadEventChannel(asyncio.get_running_loop())
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
                    # Advisory transform: no provider call was observed, so this row
                    # is telemetry, never billing evidence.
                    authoritative=False,
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
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await event_queue.get(0.5)
                except asyncio.TimeoutError:
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
    cache_namespace = _tenant_cache_namespace(request)

    event_queue = _ThreadEventChannel(asyncio.get_running_loop())
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
                cache = _get_playground_cache(request) if cache_namespace else None
                cbody = {"messages": [{"role": "user", "content": prompt}],
                         "temperature": 0,
                         "_brevitas_cache_namespace": cache_namespace}
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
                # Advisory transform: no provider call was observed, so this row
                # is telemetry, never billing evidence.
                authoritative=False,
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
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await event_queue.get(0.5)
                except asyncio.TimeoutError:
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

# Caller-supplied tracking labels: cosmetic attribution only, never money. Kept
# together so the receipt bridge can drop them wholesale when one fails
# validation rather than lose the billable row with them.
_RECEIPT_LABEL_FIELDS = frozenset({
    "project", "repo", "environment", "source", "client", "pipeline", "agent",
    "call_site_id", "framework", "gateway", "run_id",
})


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
    # Metering id of the PAID request whose provider response this row replays.
    # Reported by the in-process proxy from the cache entry that served the hit.
    # ACCEPTED HERE, DECIDED IN _record_usage_report: a value on this field is a
    # claim, never a fact, and every /v1/usage caller can set it. See the gate
    # there for the conditions under which it is allowed to reach storage.
    #
    # Deliberately NOT covered by the control-character validator below. The
    # proxy derives this from the provider response id of the paid call, and
    # x-brevitas-upstream lets a caller choose the host that mints it — so a
    # validator here would be a way to make a hostile upstream id raise, get
    # swallowed by _hosted_proxy_receipt, and DROP a billable receipt. It is
    # sanitized in _record_usage_report instead, where a bad value costs the
    # anchor and nothing else.
    savings_anchor_request_id: str = Field(default="", max_length=128)
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
    "retrieve", "retrieval", "llmlingua", "lossy", "semantic_cache",
    "response_cache", "reorder", "compress",
)
_BYTE_PRESERVING_STRATEGIES = (
    # exact_cache replays a provider response for a byte-identical request, so
    # reuse changes nothing the caller can observe. Fuzzy semantic_cache stays
    # quality-affecting: cosine similarity does not prove answer equivalence.
    "native_cache", "cache_only", "passthrough", "byte_preserving", "lossless",
    "exact_cache",
)
BREVITAS_FEE_RATE = 0.25

# Strategies whose savings are zero-spend BY CONSTRUCTION because the replay
# never touches an upstream (brevitas/proxy.py:_report_cache_hit ->
# brevitas/receipts.py:calculate_costs with an all-zero receipt). These are the
# only rows an anchor is meaningful on, and the only rows it is accepted on.
_BREVITAS_REPLAY_STRATEGIES = ("exact_cache", "semantic_cache")
# A server-minted metering id and nothing else. The proxy mints
# `proxy:<uuid4 hex>` or `proxy:<provider response id>`; the provider half is
# chosen by whoever answers the upstream, and x-brevitas-upstream lets a caller
# choose that host, so the shape is pinned here rather than trusted.
_ANCHOR_ID_SHAPE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def _decide_savings_anchor(body: UsageReportRequest, *, authoritative: bool) -> str:
    """Decide the paid-ancestor anchor for this row. Never trust the claim.

    A zero-spend saving is billable only if it is ANCHORED: a Brevitas cache
    replay whose baseline links to a real, receipted, PAID request. That makes
    the anchor a money-bearing field, so it is DECIDED here from facts this
    process controls rather than accepted from the payload.

    Every condition is necessary:

    * `authoritative` — only _hosted_proxy_receipt, which observed the provider
      response in-process, sets it. POST /v1/usage always passes False, so a
      client-supplied anchor over that route is discarded outright. This is the
      same boundary that already governs `verified`: a caller can no more anchor
      a row than it can make one authoritative.
    * `cache_attributable` — a provider-NATIVE cache discount (DeepSeek
      prompt_cache_hit_tokens, an automatic prefix cache) is not something
      Brevitas caused, and must stay unbillable. Those rows report False.
    * a replay strategy — the anchor describes a replayed response. Nothing
      else has an ancestor to point at.
    * the `proxy:` namespace — the ancestor is a receipt the proxy itself
      minted. An id outside that namespace names a row that, by the /v1/usage
      intake rewrite, cannot be an authoritative proxy receipt.
    * not self-referential — a row is not its own ancestor. Without this a
      single forged replay could anchor to itself and claim provenance from a
      request that never had spend.

    The join to the ancestor row (same tenant, authoritative, actual_cost_usd>0)
    is the database's job; this function's contract is that a non-empty value is
    a server-minted id that this process saw on a real paid call.
    """
    claimed = str(body.savings_anchor_request_id or "")
    if not (authoritative and body.cache_attributable and claimed):
        return ""
    if not (body.strategy or "").strip().lower().startswith(_BREVITAS_REPLAY_STRATEGIES):
        return ""
    if not _ANCHOR_ID_SHAPE.fullmatch(claimed):
        return ""
    if not claimed.startswith(RECEIPT_ID_PREFIX):
        return ""
    if claimed == body.request_id:
        return ""
    return claimed


def _verification_mode(strategy: str, *, cache_attributable: bool = False) -> str:
    """Classify quality by what the optimizer did, never by an invented score."""
    value = (strategy or "").strip().lower()
    if any(marker in value for marker in _QUALITY_AFFECTING_STRATEGIES):
        return "quality_affecting"
    if any(marker in value for marker in _BYTE_PRESERVING_STRATEGIES):
        return "byte_preserving"
    # A provider-native cache read on Brevitas-owned breakpoints leaves the
    # response provably unchanged, whatever the strategy label says.
    if cache_attributable:
        return "byte_preserving"
    return "unknown"


# ── Provider-receipt anchoring ────────────────────────────────────────────────
# Plausibility quarantine parameters.
#
# UNVALIDATED POLICY. The ratio below is a placeholder: validating it requires
# Q1 (an empirical comparison of provider receipts against local baselines over
# real traffic), which cannot run without provider credentials and a populated
# usage_log. Until Q1 produces evidence, the default ACTION is "observe":
# an implausible receipt is labelled and no number changes. Do not switch the
# action to a mutating value on intuition — a wrong quarantine silently deletes
# real customer savings.
#
# Actions:
#   "observe"      — annotate only (default, conservative, never changes money).
#   "drop_savings" — treat the report as zero savings. Opt-in only, post-Q1.
_ANCHOR_ACTIONS = ("observe", "drop_savings")


def _anchor_env_ratio() -> float:
    try:
        value = float(os.getenv("BREVITAS_RECEIPT_ANCHOR_IMPLAUSIBLE_RATIO", "3.0"))
    except ValueError:
        return 3.0
    return value if value > 1.0 else 3.0


def _anchor_env_action() -> str:
    """Unknown or unset values fall back to the non-mutating action."""
    value = os.getenv("BREVITAS_RECEIPT_ANCHOR_IMPLAUSIBLE_ACTION", "").strip().lower()
    return value if value in _ANCHOR_ACTIONS else "observe"


RECEIPT_ANCHOR_IMPLAUSIBLE_RATIO = _anchor_env_ratio()
RECEIPT_ANCHOR_IMPLAUSIBLE_ACTION = _anchor_env_action()


@dataclass(frozen=True)
class AnchoredTokens:
    """Both cost legs expressed on one tokenizer basis."""
    baseline_tokens: int
    optimized_tokens: int
    tokens_saved: int
    reported_delta: int
    basis: str          # "provider_receipt" | "caller_local"
    plausibility: str   # "ok" | "implausible" | "not_checked"


def _anchor_token_legs(body: UsageReportRequest, receipt: TokenReceipt) -> AnchoredTokens:
    """Anchor the LEVEL of both cost legs to the provider receipt.

    The provider receipt is authoritative for the optimized request: it counts
    the system prompt, tool schemas, cache categories and provider-tokenizer
    overhead that a local counter cannot see. So the optimized leg becomes
    ``receipt.input_tokens`` and the baseline leg is lifted by the same amount,
    which puts baseline and actual cost on a single basis. Every receipt
    component that reaches pricing and storage (fresh / cached / cache_write /
    5m / 1h / output) is likewise receipt-sourced, never caller-sourced.

    INVARIANT — anchoring moves the LEVEL, never the DELTA.
        optimized_tokens == receipt.input_tokens
        baseline_tokens  == optimized_tokens + reported_delta
        tokens_saved     == reported_delta

    Both endpoints shift by the same constant, so their difference is invariant
    under anchoring. This is deliberate: the local tokenizer's bias cancels in a
    before/after difference but not in an absolute count, so the delta is the
    only quantity a caller-side counter can contribute honestly.

    CONSEQUENCE — a zero delta cannot be repaired here. The wire format carries
    only ``baseline_tokens`` and ``compressed_tokens`` from the same local
    counter; if a caller reports them equal, no arithmetic over the receipt can
    reconstruct a saving, because no independent pre-optimization signal was
    transmitted. Anchoring is not, and cannot be, a savings-recovery mechanism.
    Recovering savings from such a report would require a NEW wire field
    carrying an independent pre-optimization measurement.

    There are exactly two exceptions to ``tokens_saved == reported_delta``:

    1. The non-negative clamp on the baseline leg, which binds only for a
       negative delta larger in magnitude than the receipt input (an expansion
       bigger than the whole request). The clamp then reports a smaller loss
       rather than a negative baseline.
    2. The plausibility quarantine, when
       ``BREVITAS_RECEIPT_ANCHOR_IMPLAUSIBLE_ACTION`` is set to
       ``"drop_savings"`` (opt-in; the default ``"observe"`` changes nothing).
       On a receipt judged implausible this collapses the baseline leg onto the
       optimized leg and forces ``tokens_saved = 0``, discarding a real,
       positive reported delta. This is the mode in which the invariant is
       load-bearing: under it a stored ``tokens_saved`` of 0 does NOT mean the
       caller reported no savings. Reconcile such rows against the
       ``reported_token_delta`` and ``receipt_plausibility`` fields on the
       response, not against ``tokens_saved``.
    """
    reported_delta = body.baseline_tokens - body.compressed_tokens
    if not body.receipt_available:
        # No provider receipt: caller-local numbers on the caller's own basis.
        baseline_tokens = body.baseline_tokens
        optimized_tokens = body.compressed_tokens
        return AnchoredTokens(baseline_tokens, optimized_tokens,
                              baseline_tokens - optimized_tokens, reported_delta,
                              "caller_local", "not_checked")

    optimized_tokens = receipt.input_tokens
    baseline_tokens = max(0, optimized_tokens + reported_delta)
    tokens_saved = baseline_tokens - optimized_tokens

    # Plausibility check (see the UNVALIDATED note above). A receipt vastly
    # larger than the local baseline MAY mean the caller measured a different
    # request than the provider billed — but it is also the normal shape of a
    # request whose system prompt and tool schemas dominate the message text,
    # which the local counter never sees. That ambiguity is precisely why the
    # default action is observe-only: the label is evidence for Q1, not a
    # billing decision.
    plausibility = "not_checked"
    if body.baseline_tokens > 0 and optimized_tokens > 0:
        ratio = optimized_tokens / body.baseline_tokens
        plausibility = "implausible" if ratio > RECEIPT_ANCHOR_IMPLAUSIBLE_RATIO else "ok"
    if plausibility == "implausible" and RECEIPT_ANCHOR_IMPLAUSIBLE_ACTION == "drop_savings":
        baseline_tokens = optimized_tokens
        tokens_saved = 0
    return AnchoredTokens(baseline_tokens, optimized_tokens, tokens_saved,
                          reported_delta, "provider_receipt", plausibility)


def _record_usage_report(kh: str, body: UsageReportRequest, *,
                         auth_context: AuthContext | None = None,
                         authoritative: bool = False,
                         tenant_gate_key: str | None = None) -> dict:
    tenant_gate_key = tenant_gate_key or kh
    # Dedupe within the SAME authority. request_id is derived from provider-visible
    # material (the SDK mints `proxy:<provider response id>`, which the streaming paths
    # yield to the client in the first SSE chunk), so a caller can name an id it has seen.
    # Scoped to (key_hash, request_id) across the whole usage_log, one cheap
    # authoritative=False POST /v1/usage with that id would suppress the billable
    # authoritative receipt that the hosted bridge writes afterwards.
    #
    # This probe is authority-scoped and, since 202607280026, so is the storage
    # layer: the unique index is usage_log_request_authority_unique on
    # (key_hash, request_id, authoritative) where request_id <> ''. Index and
    # probe now agree on the same key, so a non-authoritative row no longer
    # collides with a later authoritative one and this probe IS load-bearing.
    # KEEP the RECEIPT_ID_PREFIX namespace reservation on the /v1/usage intake
    # path anyway — it rewrites any caller-supplied `proxy:` id into `client:`.
    # It is now genuine defence in depth rather than the only defence, and it is
    # also what protects deployments where 202607280026 is not yet applied.
    if body.request_id and _store.has_request(kh, body.request_id,
                                              authoritative=authoritative):
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

    # Both cost legs are anchored to the provider receipt: see _anchor_token_legs
    # for the invariant (anchoring moves the LEVEL of both legs, never the DELTA
    # between them) and for why a caller-reported zero delta cannot be repaired
    # here. Receipt components below (fresh/cached/write/5m/1h/output) are taken
    # from the same receipt, so pricing and storage share one basis.
    anchored = _anchor_token_legs(body, receipt)
    baseline_tokens = anchored.baseline_tokens
    optimized_tokens = anchored.optimized_tokens
    tokens_saved = anchored.tokens_saved
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

    mode = _verification_mode(body.strategy,
                              cache_attributable=body.cache_attributable)
    # The sequential quality stream is ANALYTICS, never money. Note that
    # `verified` below is non-zero only when mode == "byte_preserving", and that
    # branch fixes quality_status without consulting the stream at all — so no
    # billable amount can ever depend on anything inside this block.
    #
    # It nonetheless used to be able to DESTROY a billable amount. Everything
    # here can raise before _store.record_usage runs: _seq_stream goes through a
    # BoundedTTLMap whose get_or_create raises ResourceLimitExceeded (an
    # over-long tenant key, or a value the sizer rejects), SequentialQualityGate
    # is constructed from BREVITAS_QUALITY_P0/ALPHA and float() on a
    # mistyped env var raises ValueError, and the lever trip does a LAZY import
    # of token_efficiency_model.quality.gate. Any of those propagated out of
    # this function into _hosted_proxy_receipt's `except Exception`, was logged
    # once as "write failed", and the authoritative receipt was gone — same
    # swallow-everything class as the label-validation drop above it, and
    # invisible for the same reason. Record the money, degrade the cosmetic
    # field.
    #
    # The initial value is the CONSERVATIVE one for each mode, and it is only
    # ever revised upward inside the try, so a fault mid-way cannot manufacture
    # a "verified" status: byte_preserving is verified by definition of the
    # mode, everything else stays "unverified" until the stream says otherwise.
    stream = None
    quality_status = "verified" if mode == "byte_preserving" else "unverified"
    try:
        stream = _seq_stream(tenant_gate_key)
        if mode != "byte_preserving" and body.quality_verified is not None:
            stream.update(body.quality_verified)
            if stream.state.tripped:
                quality_status = "stream_tripped"
                # A tripped stream must stop THIS TENANT's request path from applying any
                # unproven lever — not just stop billing. Trips are keyed by the customer key,
                # so one tenant's failing reports never disable levers for other tenants.
                from token_efficiency_model.quality.gate import trip_lever
                for _lever in _QUALITY_TRIP_LEVERS:
                    trip_lever(_lever, key=tenant_gate_key)
            else:
                quality_status = "verified" if body.quality_verified else "failed"
    except Exception as exc:
        # Type-only, like every other receipt-path log: the payload is customer
        # content. Loud (warning) because a degraded stream means the quality
        # gate stopped accumulating evidence for this tenant.
        logger.warning("usage quality stream degraded error_type=%s", type(exc).__name__)
    # Caller-reported SDK values are analytics only. Only the in-process proxy,
    # which observed the provider response, may create verified/billable savings.
    # Live charging is intentionally narrower than analytics. Only authoritative
    # provider receipts from input-byte-preserving methods (including exact_cache
    # replays) can create billable savings. Reordering, retrieval, and other
    # quality-affecting methods remain non-billable until their gate state is
    # durable and auditable. The native cache discount reaches `measured` only
    # through the cache_attributable branch of calculate_costs, so caller-owned
    # markers and provider-automatic caching never enter the billable number.
    # Warming pings (strategy cache_warm) are spend Brevitas initiated — spend
    # is recorded, savings never.
    #
    # SIGNED ON PURPOSE — do not re-add a per-row floor here. An eligible
    # byte-preserving row can legitimately have NEGATIVE measured savings: a
    # cold cache WRITE is priced above plain input (claude-sonnet-5: 3.75 vs
    # 3.00 per Mtok) and TokenReceipt.input_tokens already counts the write, so
    # the write leg costs more than the baseline while the warm reads that
    # follow more than repay it. Flooring each row at zero would make a period
    # sum that can never go negative, so a week whose true net is negative would
    # still bill every positive row in it — exactly the failure
    # supabase/migrations/202607280007_period_settlement_ledger.sql exists to
    # eliminate, and it would silently overcharge every write-heavy period.
    # The ONE floor lives at the period level: 202607280007's generated
    # net_savings_usd = greatest(verified_savings_usd - warm_spend_usd, 0) and
    # 202607280008's greatest(net_verified_savings_usd, 0). Those read
    # sum(usage_log.verified_savings_usd) and require this column to be signed.
    verified = (float(measured or 0)
                if authoritative and mode == "byte_preserving"
                and quality_status == "verified"
                and strategy_name != "cache_warm" else 0.0)
    # The per-row fee stays non-negative. A negative fee is unrepresentable by
    # design (202607280007 item 4: Stripe rejects negative meter values), and
    # per-row fees are no longer a billing input at all — 202607280006 dropped
    # queue_brevitas_fee_after_usage, the only writer into billing_ledger.
    # Netting is a period-level operation over `verified`; it is deliberately
    # NOT expressed as a per-row fee credit.
    fee = round(max(0.0, verified) * BREVITAS_FEE_RATE, 10)
    # Decided, never accepted. See _decide_savings_anchor: this is what makes a
    # zero-spend cache replay provably organic rather than merely zero-cost.
    savings_anchor_request_id = _decide_savings_anchor(body, authoritative=authoritative)

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
        savings_anchor_request_id=savings_anchor_request_id,
        strategy=body.strategy,
        receipt_source=body.receipt_source,
        is_stream=body.is_stream,
    )
    if not inserted and body.request_id:
        return {"duplicate": True, "request_id": body.request_id,
                "tokens_saved": 0, "measured_savings_usd": 0.0,
                "verified_savings_usd": 0.0, "quality_status": "duplicate"}
    # Emitted after the dedupe return so the series counts rows that were really
    # persisted; a duplicate produces no row and must not read as billable work.
    record_savings_row(
        authoritative=bool(authoritative), billable=verified > 0,
        verified_savings_usd=verified,
    )
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
        # What the row was actually anchored to, echoed so a reporter can tell a
        # dropped claim from an accepted one instead of inferring it from a bill.
        "savings_anchor_request_id": savings_anchor_request_id,
        # Anchoring provenance. Analytics/audit only — no billing depends on it.
        "token_basis": anchored.basis,
        "reported_token_delta": anchored.reported_delta,
        "receipt_plausibility": anchored.plausibility,
        # Serialized AFTER the row is written, but still guarded: raising here
        # would 500 the /v1/usage caller (and log "write failed" in the hosted
        # bridge) for a row that was in fact persisted, which is worse than
        # saying the stream state is unavailable.
        "stream": _stream_snapshot(stream),
    }


def _stream_snapshot(stream: Any) -> dict:
    """Serialized quality-stream state, or an explicit 'no state' marker.

    Never None and never absent: a consumer must be able to tell "the stream
    said nothing" from "the stream said zero observations".
    """
    if stream is None:
        return {"available": False}
    try:
        state = stream.to_dict()
    except Exception as exc:
        logger.warning("usage quality stream snapshot failed error_type=%s",
                       type(exc).__name__)
        return {"available": False}
    return state if isinstance(state, dict) else {"available": False}


# Namespace a caller-reported id is rewritten into when it claims the server-minted
# RECEIPT_ID_PREFIX. Deterministic, not random: the local BVX proxy legitimately reports
# its own `proxy:<provider id>` receipts over this route, so replacing the prefix keeps
# its retries idempotent while leaving the billable namespace unoccupied.
_CLIENT_REQUEST_ID_PREFIX = "client:"


@app.post("/v1/usage")
@limiter.limit("300/minute")
def report_usage(request: Request, body: UsageReportRequest, kh: str = Depends(_authenticated)):
    # RECEIPT_ID_PREFIX is a RESERVED namespace, not merely a named one. usage_log
    # carries a unique index on (key_hash, request_id) where request_id <> ''
    # (20260710_cloud_usage.sql:125), so a caller that pre-inserts an analytics row under
    # an id it read out of its own in-flight SSE stream would occupy that slot and the
    # authoritative receipt written afterwards by _hosted_proxy_receipt would be silently
    # ignored as a duplicate — full optimization, zero billed. Rewrite rather than reject:
    # this route is the local proxy's own reporting transport.
    if body.request_id.startswith(RECEIPT_ID_PREFIX):
        body = body.model_copy(update={
            "request_id": (_CLIENT_REQUEST_ID_PREFIX
                           + body.request_id[len(RECEIPT_ID_PREFIX):])[:128]})
    return _record_usage_report(
        kh, body,
        auth_context=_require_scope(request, kh, "usage:write"),
        authoritative=False,
        tenant_gate_key=_request_tenant_key(request, kh),
    )


# ── Sequential quality streams (brief b4) ─────────────────────────────────────
# One always-valid mSPRT stream per customer key. In-memory for now (process
# lifetime); serialized state is exposed via /v1/quality/stream for auditability.
# The TTL is an IDLE window, refreshed on every access (see _seq_stream): with
# BoundedTTLMap's create-time stamp the martingale was destroyed exactly one hour after a
# tenant's FIRST report and rebuilt at n=0/log_m=0, so a tenant reporting fewer than the
# ~30-40 observations the trip threshold needs per hour could never trip at all — which
# contradicts the "anytime-valid over the whole monitoring horizon" claim the gate makes.
_seq_streams = BoundedTTLMap[str, object](
    ttl_s=max(_RESOURCE_BOUNDS.registry_ttl_s, 6 * 3600),
    max_entries=_RESOURCE_BOUNDS.registry_max_entries,
    max_value_bytes=1024,
    sizer=lambda _value: 256,
    copier=lambda value: value,
    snapshotter=lambda value: value,
)

# The four risky levers a tripped stream must stop the request path from applying.
_QUALITY_TRIP_LEVERS = ("retrieval", "compression", "semantic_cache", "reorder")
# Plus the byte-preserving levers, reported so a poller sees every decision.
_QUALITY_GATE_LEVERS = _QUALITY_TRIP_LEVERS + ("cache", "cache_injection")


def _seq_stream(kh: str):
    from token_efficiency_model.quality.sequential import SequentialQualityGate
    stream = _seq_streams.get_or_create(
        kh,
        lambda: SequentialQualityGate(
            p0=float(os.environ.get("BREVITAS_QUALITY_P0", "0.9")),
            alpha=float(os.environ.get("BREVITAS_QUALITY_ALPHA", "0.05")),
        ),
    )
    # get_or_create only reorders LRU on a hit; it never restamps expires_at. Re-put so an
    # actively-reporting tenant keeps its accumulated evidence. Identity copier/snapshotter
    # means this is the same object the caller just mutated, not a copy.
    with suppress(ResourceLimitExceeded):
        _seq_streams.put(kh, stream)
    return stream


def _quality_lever_state(tenant_gate_key: str) -> dict[str, dict[str, bool]]:
    from token_efficiency_model.quality.gate import lever_allowed
    return {name: {"allowed": bool(lever_allowed(name, tenant_gate_key))}
            for name in _QUALITY_GATE_LEVERS}


@app.get("/v1/quality/stream")
def quality_stream(request: Request, kh: str = Depends(_authenticated)):
    """Auditable state of this customer's sequential quality stream.

    The lever decisions ship alongside it because the two live in different structures:
    _tripped_levers never expires while the stream is evicted under memory pressure, so a
    fresh n=0/tripped=false stream can coexist with still-disabled levers. Reporting both
    is what lets an operator see WHY levers are off.
    """
    _require_scope(request, kh, "usage:read_own")
    tenant_gate_key = _request_tenant_key(request, kh)
    payload = _seq_stream(tenant_gate_key).to_dict()
    return {**payload, "levers": _quality_lever_state(tenant_gate_key)}


@app.get("/v1/quality/levers")
@limiter.limit("120/minute")
def quality_levers(request: Request, kh: str = Depends(_authenticated)):
    """This tenant's lever decisions, so the customer-installed proxy can stop applying an
    unproven lever instead of only the replica that recorded the trip.

    Replica-local by construction: token_efficiency_model/quality/gate.py keeps the trip set
    in a module global, so this answers for whichever replica serves the poll and a redeploy
    clears it. Durable, fleet-wide trip state needs a table keyed on
    (organization_id, customer_id, lever) that preserves the empty-key global kill switch.
    """
    _require_scope(request, kh, "usage:read_own")
    return {"levers": _quality_lever_state(_request_tenant_key(request, kh)),
            "replica_local": True}


@app.post("/v1/quality/stream/reset")
@limiter.limit("30/minute")
def quality_stream_reset(request: Request, kh: str = Depends(_authenticated)):
    """Reset a tripped stream (after investigation). Deliberately explicit.
    Also clears this tenant's lever trips so the request-path levers re-enable together
    with the billing stream (the two must not drift apart).

    Not authorized by usage:read_own alone: this re-enables
    retrieval/compression/semantic-cache over the tenant's prompt content and erases the
    accumulated mSPRT evidence, so it is a mutation, and usage:read_own is minted into
    every dashboard session key including a plain member's.

    Authorization is the LIVE company role, with an explicit quality:manage scope accepted
    as the alternative. The scope cannot carry this on its own in production: the hosted
    session key's scope array is chosen inside
    public.company_admin_create_dashboard_session_key, which mints
    proxy:invoke/usage:read_own/provider:read/provider:manage and nothing else, so a
    scope-only gate would 403 every caller — including the owner — and leave a tripped
    stream unclearable short of a replica redeploy, which stops the savings this bills on.
    A long-lived organization_service key still does not get the scope from
    _dashboard_session_scopes(): the credential whose own reports trip the stream must not
    be able to clear the trip unless an operator granted it deliberately.
    """
    context = _require_scope(request, kh, "usage:read_own")
    if (context.company_role not in ("company_owner", "company_admin")
            and not context.permits("quality:manage")):
        raise HTTPException(status_code=403,
                            detail="Organization admin access required")
    tenant_gate_key = _request_tenant_key(request, kh)
    _seq_streams.pop(tenant_gate_key, None)
    from token_efficiency_model.quality.gate import reset_all_levers
    reset_all_levers(key=tenant_gate_key)
    actor_id, actor_role = _key_actor_audit_identity(context)
    # target_id must satisfy the audit trigger's ^[A-Za-z0-9._:-]{1,200}$ and must not be a
    # 64-hex digest, so the tenant key itself is unusable here.
    _audit_tenant_mutation(
        request, context.organization_id, actor_id, actor_role,
        "quality.stream_reset", target_type="quality_stream",
        target_id=f"{context.organization_id}:{context.customer_id or 'organization'}")
    return {"reset": True}


@app.get("/v1/provider-costs")
def provider_costs():
    return {"pricing_as_of": PRICING_VERSION, "costs_per_1m_tokens": PROVIDER_COSTS_PER_1M}


# ── Stats ─────────────────────────────────────────────────────────────────────

# usage:read_own is minted into EVERY dashboard session key, including a plain
# 'member''s, but the backing RPCs are organization-wide. Money is the one part of
# that answer the product already refuses to a role without billing:manage
# (/api/billing/status -> 403), so it is stripped here on the same rule instead of
# being silently readable on a third surface. Every dollar figure in these payloads
# is a *_usd key, at any nesting depth.
_SPEND_FIELD_SUFFIX = "_usd"


def _without_spend_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_spend_fields(item) for key, item in value.items()
                if not str(key).endswith(_SPEND_FIELD_SUFFIX)}
    if isinstance(value, list):
        return [_without_spend_fields(item) for item in value]
    return value


def _spend_readable(context: AuthContext) -> bool:
    """Only a human session is role-gated.

    Device, organization-service and legacy keys carry no human role and
    legitimately aggregate their tenant's own rows, so they are unchanged.

    Owners and admins always read their own tenant's money — the same rule
    get_warming() applies. ROLE_PERMISSIONS grants billing:manage to
    company_owner and billing_admin only, so gating on the permission alone
    would blank the money view for a company_admin session; only a plain
    'member' is redacted here.
    """
    if context.key_type != "dashboard_session":
        return True
    return (context.company_role in ("company_owner", "company_admin")
            or context.holds_company_permission("billing:manage"))


def _spend_filtered(context: AuthContext, payload: Any) -> Any:
    if _spend_readable(context):
        return payload
    filtered = _without_spend_fields(payload)
    if isinstance(filtered, dict):
        # Named, not silent: a consumer must be able to tell "withheld" from
        # "zero" rather than rendering a confident $0.00.
        filtered["spend_redacted"] = True
    return filtered


def _spend_filtered_rows(context: AuthContext, rows: Any) -> dict:
    """CONTRACT A envelope for the per-dimension stats lists.

    /v1/stats/pipelines, /v1/stats/agents and /v1/stats/runs used to return a
    BARE LIST. _spend_filtered strips every `*_usd` key from a redacted caller's
    payload but can only hang the `spend_redacted` marker on a dict, so a list
    response reached the dashboard indistinguishable from "this pipeline saved
    nothing" — a confident $0.00 over withheld money, the exact failure the flag
    exists to prevent.

    The flag is emitted UNCONDITIONALLY (False when the caller may read spend)
    so a consumer never has to infer redaction from an absent key. The dashboard
    additionally reads a bare array as {rows, spend_redacted: false}, so API
    (Railway) and dashboard (Vercel) may ship in either order.
    """
    payload = _spend_filtered(context, {"rows": rows})
    if not isinstance(payload, dict):  # pragma: no cover - defensive
        payload = {"rows": payload}
    payload.setdefault("spend_redacted", False)
    return payload


@app.get("/v1/stats")
@limiter.limit("120/minute")
def stats(request: Request, kh: str = Depends(_authenticated)):
    context = _require_scope(request, kh, "usage:read_own")
    return _spend_filtered(context, _store.get_stats(kh))


@app.get("/v1/stats/breakdown")
@limiter.limit("120/minute")
def stats_breakdown(request: Request, kh: str = Depends(_authenticated)):
    context = _require_scope(request, kh, "usage:read_own")
    rows = _store.get_breakdown(kh)
    return _spend_filtered(context, {"rows": rows, "totals": _store.get_stats(kh)})


@app.get("/v1/stats/activity")
@limiter.limit("120/minute")
def stats_activity(request: Request, kh: str = Depends(_authenticated)):
    context = _require_scope(request, kh, "usage:read_own")
    return _spend_filtered(context, _store.get_activity(kh))


# Per-provider breakdown rows for /v1/stats/cache. Additive contract: a store
# that does not compute per_provider yet keeps the original response shape
# rather than advertising an empty breakdown it never measured.
_CACHE_STATS_PROVIDER_FIELDS = (
    "provider", "cached_input_tokens", "native_cache_discount_usd",
    "attributable_discount_usd", "warm_pings", "warm_spend_usd", "warm_hits")


@app.get("/v1/stats/cache")
@limiter.limit("120/minute")
def stats_cache(request: Request, kh: str = Depends(_authenticated)):
    context = _require_scope(request, kh, "usage:read_own")
    body = _store.cache_stats(kh)
    if not isinstance(body, dict):
        return body
    rows = body.get("per_provider")
    if isinstance(rows, list):
        body["per_provider"] = [
            {field: row.get(field) for field in _CACHE_STATS_PROVIDER_FIELDS}
            for row in rows if isinstance(row, dict)]
    else:
        body.pop("per_provider", None)
    return _spend_filtered(context, body)


# ── Optimization audit (traffic side) ─────────────────────────────────────────
# Honesty contract: every claim below is backed by stored usage rows, and any
# capability the rows cannot prove stays "unknown" with a pointer at the static
# scanner — a false "opportunity" a prospect can refute kills the deal.

# Minimum recent rows before the audit will assert anything negative about a
# provider's traffic; below this, absence of evidence stays "unknown".
_AUDIT_MIN_EVIDENCE_ROWS = 20
# cached/(cached+fresh) thresholds for the byte-stability approximation.
_AUDIT_HIT_RATE_IMPLEMENTED_PCT = 20.0
_AUDIT_HIT_RATE_OPPORTUNITY_PCT = 5.0
# Provider caches ignore prompts under a minimum size (~1024 tokens for both
# OpenAI automatic caching and Anthropic cache_control), so a workload whose
# average prompt sits below this floor can never show cached tokens no matter
# how byte-stable it is — it must not be accused of prompt churn.
_AUDIT_MIN_CACHEABLE_PROMPT_TOKENS = 1024
_AUDIT_STATIC_SCAN_DETAIL = ("not observable in proxied usage rows; run the "
                             "static code scan (brevitas audit <repo>)")

# Served when brevitas.audit_capabilities has not shipped (or fails to import):
# same contract shape, so the endpoint stands alone. The registry module is the
# source of truth once present.
_AUDIT_CAPABILITY_FALLBACK = (
    {"id": "anthropic_cache_control", "name": "Anthropic cache_control injection",
     "providers": ["anthropic"], "detect": "both", "weight": 9,
     "traffic_metric": "caller_cache_markers"},
    {"id": "openai_prompt_cache_key", "name": "OpenAI prompt_cache_key routing",
     "providers": ["openai"], "detect": "static", "weight": 6,
     "traffic_metric": None},
    {"id": "prompt_byte_stability", "name": "Prompt byte-stability",
     "providers": ["anthropic", "openai", "deepseek", "xai"], "detect": "both",
     "weight": 8, "traffic_metric": "cache_hit_ratio"},
    {"id": "xai_conv_id_affinity", "name": "xAI x-grok-conv-id cache affinity",
     "providers": ["xai"], "detect": "both", "weight": 7,
     "traffic_metric": "xai_cache_hits"},
    {"id": "stream_options_include_usage",
     "name": "Streamed usage receipts (stream_options.include_usage)",
     "providers": ["openai", "xai"], "detect": "both", "weight": 3,
     "traffic_metric": "stream_receipt_visibility"},
    {"id": "exact_repeat_response_caching", "name": "Exact-repeat response caching",
     "providers": "all", "detect": "both", "weight": 6,
     "traffic_metric": "exact_repeat_rate"},
    {"id": "predictive_cache_warming", "name": "Predictive cache warming",
     "providers": ["anthropic"], "detect": "both", "weight": 7,
     "traffic_metric": "warming_status"},
    {"id": "cache_ttl_tiering", "name": "Cache TTL tiering",
     "providers": ["anthropic"], "detect": "both", "weight": 4,
     "traffic_metric": "ttl_tier_writes"},
    {"id": "usage_cost_metering", "name": "Usage and cost metering",
     "providers": "all", "detect": "static", "weight": 3,
     "traffic_metric": None},
    {"id": "concurrent_prefix_batching", "name": "Concurrent same-prefix batching",
     "providers": "all", "detect": "static", "weight": 5,
     "traffic_metric": None},
)


_audit_registry_warned = False


def _audit_capability_registry() -> list[dict]:
    """brevitas.audit_capabilities.CAPABILITIES when importable, else the
    in-module fallback. Defensive: rows without an id are dropped and missing
    fields default at evaluation time, so a partial registry never 500s."""
    global _audit_registry_warned
    try:
        from brevitas.audit_capabilities import CAPABILITIES
        caps = [dict(cap) for cap in CAPABILITIES
                if isinstance(cap, dict) and cap.get("id")]
        if caps:
            return caps
    except Exception:
        pass
    if not _audit_registry_warned:
        _audit_registry_warned = True
        logger.warning("brevitas.audit_capabilities unavailable; "
                       "serving fallback registry")
    return [dict(cap) for cap in _AUDIT_CAPABILITY_FALLBACK]


def _audit_verdict_anthropic_cache_control(m: dict) -> tuple[str, list[str], str]:
    p = (m.get("per_provider") or {}).get("anthropic") or {}
    rows, caller = _i_safe(p.get("rows")), _i_safe(p.get("caller_owned_cache_rows"))
    brevitas_rows = _i_safe(p.get("brevitas_owned_cache_rows"))
    if caller:
        return ("already_implemented",
                [f"{caller} of {rows} recent anthropic requests carry "
                 "caller-owned prompt-cache reads"],
                "your code already places cache_control markers")
    if brevitas_rows:
        return ("already_implemented",
                [f"{brevitas_rows} of {rows} recent anthropic requests hit "
                 "Brevitas-managed cache breakpoints"],
                "active via Brevitas-managed cache_control injection")
    write_rows = _i_safe(p.get("cache_write_rows"))
    if write_rows:
        # Anthropic only emits cache-write tokens for requests carrying
        # cache_control markers, so writes prove the markers are in place even
        # before any read lands (cold cache, recent adoption, or per-deploy
        # prefix rotation). Calling this an opportunity would be refuted by
        # the write tokens in this same report.
        evidence = [f"{write_rows} of {rows} recent anthropic requests wrote "
                    f"{_i_safe(p.get('cache_write_tokens'))} prompt-cache "
                    "tokens; no reads landed in this window (approximation: "
                    "bounded recent window)"]
        if (_i_safe(p.get("brevitas_owned_write_rows"))
                and not _i_safe(p.get("caller_owned_write_rows"))):
            return ("already_implemented", evidence,
                    "active via Brevitas-managed cache_control injection; "
                    "cache written but not yet read back in this window")
        return ("already_implemented", evidence,
                "your code already places cache_control markers; cache writes "
                "observed without reads yet (cold or recently adopted cache)")
    if rows >= _AUDIT_MIN_EVIDENCE_ROWS and not _i_safe(p.get("cached_input_tokens")):
        return ("opportunity",
                [f"0 cached input tokens across {rows} recent anthropic "
                 "requests (approximation: bounded recent window)"],
                "no prompt-cache reads observed on anthropic traffic")
    return ("unknown", [], "not enough anthropic traffic to measure")


def _audit_verdict_byte_stability(m: dict) -> tuple[str, list[str], str]:
    per = m.get("per_provider") or {}
    cached = fresh = rows = small_rows = anthropic_write_rows = 0
    small_providers: list[str] = []
    for provider in ("anthropic", "openai", "deepseek", "xai"):
        entry = per.get(provider) or {}
        p_rows = _i_safe(entry.get("rows"))
        if not p_rows:
            continue
        p_cached = _i_safe(entry.get("cached_input_tokens"))
        p_fresh = _i_safe(entry.get("fresh_input_tokens"))
        if (not p_cached and
                (p_cached + p_fresh) / p_rows < _AUDIT_MIN_CACHEABLE_PROMPT_TOKENS):
            # Under the provider cache floor a hit rate of zero is structural,
            # not evidence of unstable bytes — keep these rows out of the
            # ratio. Observed cache reads trump the average-size heuristic:
            # a provider with hits demonstrably caches this workload.
            small_rows += p_rows
            small_providers.append(provider)
            continue
        cached += p_cached
        fresh += p_fresh
        rows += p_rows
        if provider == "anthropic":
            anthropic_write_rows = _i_safe(entry.get("cache_write_rows"))
    total = cached + fresh
    if not total or rows < _AUDIT_MIN_EVIDENCE_ROWS:
        if small_rows >= _AUDIT_MIN_EVIDENCE_ROWS:
            return ("not_applicable",
                    [f"average prompt on {', '.join(small_providers)} is under "
                     f"the ~{_AUDIT_MIN_CACHEABLE_PROMPT_TOKENS}-token provider "
                     f"cache floor across {small_rows} recent requests "
                     "(approximation: window average, not per-request sizes)"],
                    "prompts are below the minimum size provider caches will "
                    "store, so byte stability cannot change the bill")
        return ("unknown", [], "not enough cache-capable traffic to measure")
    hit = round(100 * cached / total, 2)
    evidence = [f"{hit}% of input tokens on cache-capable providers were "
                f"served from provider caches across {rows} recent requests"]
    if small_providers:
        evidence.append(f"excluded {small_rows} requests on "
                        f"{', '.join(small_providers)}: average prompt under "
                        f"the ~{_AUDIT_MIN_CACHEABLE_PROMPT_TOKENS}-token "
                        "provider cache floor")
    if hit >= _AUDIT_HIT_RATE_IMPLEMENTED_PCT:
        return ("already_implemented", evidence,
                "prompts are byte-stable enough for provider caches to hit")
    repeat_rows = _i_safe((m.get("repeat") or {}).get("repeat_session_rows"))
    if hit < _AUDIT_HIT_RATE_OPPORTUNITY_PCT and repeat_rows:
        if anthropic_write_rows:
            # Writes without reads fits both prompt-byte churn and a cold,
            # recently adopted cache; a bounded window cannot tell them apart,
            # so naming "unstable prompt bytes" here would be refutable.
            evidence.append(f"{anthropic_write_rows} recent anthropic requests "
                            "wrote cache entries that were not read back")
            return ("unknown", evidence,
                    "cache writes without reads can mean prompt-byte churn or "
                    "a cold, recently adopted cache; this window cannot "
                    "distinguish them")
        evidence.append(f"{repeat_rows} recent requests repeated within the "
                        "same session inside an hour — while a provider cache "
                        "entry could still have been live — yet rarely hit "
                        "cache (approximation: hit-ratio inference, not a "
                        "byte diff)")
        return ("opportunity", evidence,
                "repeat traffic with near-zero cache hits suggests unstable "
                "prompt bytes (timestamps, uuids, unsorted json)")
    if hit < _AUDIT_HIT_RATE_OPPORTUNITY_PCT:
        return ("unknown", evidence,
                "low cache hits but no repeat-session evidence; the workload "
                "may simply not repeat")
    return ("partial", evidence,
            "some cache reuse; hit ratio suggests headroom (approximation)")


def _audit_verdict_xai_affinity(m: dict) -> tuple[str, list[str], str]:
    p = (m.get("per_provider") or {}).get("xai") or {}
    cached, rows = _i_safe(p.get("cached_input_tokens")), _i_safe(p.get("rows"))
    if cached:
        return ("already_implemented",
                [f"{cached} cached input tokens across {rows} recent xai "
                 "requests — xAI's cache does not hit without conversation "
                 "affinity, so x-grok-conv-id is effectively in place"],
                "xAI cache reads observed, which require x-grok-conv-id")
    return ("unknown", [],
            "request headers are not stored; " + _AUDIT_STATIC_SCAN_DETAIL)


def _audit_verdict_stream_usage(m: dict) -> tuple[str, list[str], str]:
    s = m.get("streaming") or {}
    streamed = _i_safe(s.get("streamed_rows"))
    missing = _i_safe(s.get("streamed_missing_receipt_rows"))
    if not streamed:
        return ("unknown", [], "no streamed openai/xai requests observed")
    evidence = [f"{streamed - missing} of {streamed} recent streamed "
                "openai/xai requests returned a usage receipt"]
    if not missing:
        return ("already_implemented", evidence,
                "streamed responses already report usage")
    if missing == streamed:
        return ("opportunity", evidence,
                "no streamed request reported usage; "
                "stream_options.include_usage is not set")
    return ("partial", evidence,
            "some streamed call sites lack stream_options.include_usage")


def _audit_verdict_exact_repeat(m: dict) -> tuple[str, list[str], str]:
    r = m.get("repeat") or {}
    replays = _i_safe(r.get("exact_cache_rows"))
    avoided = _i_safe(r.get("calls_avoided"))
    if replays or avoided:
        return ("already_implemented",
                [f"{replays} exact-replay responses served, "
                 f"{avoided} provider calls avoided in the recent window"],
                "active via Brevitas exact-replay caching")
    return ("unknown", [],
            "upstream memoization never reaches the proxy, so absence of "
            "replays proves nothing; " + _AUDIT_STATIC_SCAN_DETAIL)


def _audit_verdict_warming(m: dict) -> tuple[str, list[str], str]:
    w = m.get("warming") or {}
    pings = _i_safe(w.get("warm_pings"))
    if w.get("configured") and pings:
        return ("already_implemented",
                [f"{pings} warming pings, {_i_safe(w.get('warm_hits'))} "
                 "confirmed warm hits"],
                "active via Brevitas predictive warming")
    if w.get("configured"):
        return ("partial", ["warming enrolled but no pings recorded yet"],
                "enrollment active, ping pipeline not yet exercised")
    if _i_safe(m.get("window_rows")) < _AUDIT_MIN_EVIDENCE_ROWS:
        # A handful of requests cannot establish the return-cadence patterns
        # warming monetizes; claiming an opportunity here would be guesswork.
        return ("unknown", [],
                "too few proxied requests to judge whether warming would pay")
    return ("opportunity",
            ["no warming enrollment for this organization (approximation: "
             "self-managed keep-alive outside the proxy is not visible)"],
            "provider caches expire between bursts; predictive warming keeps "
            "them hot")


def _audit_verdict_ttl_tiering(m: dict) -> tuple[str, list[str], str]:
    t = m.get("ttl") or {}
    one_hour = _i_safe(t.get("cache_write_1h_tokens"))
    write_rows = _i_safe(t.get("cache_write_rows"))
    if one_hour:
        owner = ("caller-owned" if _i_safe(t.get("caller_owned_1h_rows"))
                 else "Brevitas-managed")
        return ("already_implemented",
                [f"{one_hour} one-hour cache-write tokens observed "
                 f"({owner}; ownership inferred from cache_attributable)"],
                "1h TTL tier in use")
    if write_rows:
        return ("partial",
                [f"{write_rows} recent requests wrote cache entries, all at "
                 "the default 5m TTL"],
                "caching active but no 1h tier; whether the 2x write premium "
                "pays off depends on inter-arrival times (approximation)")
    return ("unknown", [], "no cache writes observed to judge TTL usage")


def _i_safe(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


_AUDIT_TRAFFIC_EVALUATORS: dict[str, Callable[[dict], tuple[str, list[str], str]]] = {
    "caller_cache_markers": _audit_verdict_anthropic_cache_control,
    "anthropic_cache_control": _audit_verdict_anthropic_cache_control,
    "cache_hit_ratio": _audit_verdict_byte_stability,
    "prompt_byte_stability": _audit_verdict_byte_stability,
    "xai_cache_hits": _audit_verdict_xai_affinity,
    "xai_conv_id_affinity": _audit_verdict_xai_affinity,
    "stream_receipt_visibility": _audit_verdict_stream_usage,
    "stream_options_include_usage": _audit_verdict_stream_usage,
    "exact_repeat_rate": _audit_verdict_exact_repeat,
    "exact_repeat_response_caching": _audit_verdict_exact_repeat,
    "warming_status": _audit_verdict_warming,
    "predictive_cache_warming": _audit_verdict_warming,
    "ttl_tier_writes": _audit_verdict_ttl_tiering,
    "cache_ttl_tiering": _audit_verdict_ttl_tiering,
}


def _audit_estimated_impact(verdict: str, weight: int) -> Optional[str]:
    if verdict not in ("opportunity", "partial"):
        return None
    return "high" if weight >= 7 else "medium" if weight >= 4 else "low"


def _build_audit_report(metrics: dict) -> dict:
    metrics = metrics if isinstance(metrics, dict) else {}
    providers = [str(p) for p in (metrics.get("providers") or [])]
    detected = set(providers)
    no_traffic = not _i_safe(metrics.get("window_rows"))
    capabilities, numerator, denominator = [], 0.0, 0
    measured = measured_impl = False
    for cap in _audit_capability_registry():
        cap_id = str(cap.get("id"))
        weight = max(1, _i_safe(cap.get("weight")) or 1)
        applicable_to = cap.get("providers", "all")
        applicable = (applicable_to == "all"
                      or bool(detected & set(applicable_to or [])))
        evaluator = (_AUDIT_TRAFFIC_EVALUATORS.get(str(cap.get("traffic_metric")))
                     or _AUDIT_TRAFFIC_EVALUATORS.get(cap_id))
        if no_traffic:
            # With zero rows nothing is measurable — including applicability,
            # so nothing may be called not_applicable either.
            verdict, evidence, detail = "unknown", [], (
                "no proxied traffic recorded for this key; nothing measured")
        elif not applicable:
            verdict, evidence, detail = "not_applicable", [], (
                "no traffic observed for the providers this applies to")
        elif evaluator is None:
            verdict, evidence, detail = "unknown", [], _AUDIT_STATIC_SCAN_DETAIL
        else:
            verdict, evidence, detail = evaluator(metrics)
        if verdict != "not_applicable":
            denominator += weight
            if verdict == "opportunity":
                numerator += weight
            elif verdict == "partial":
                numerator += weight * 0.5
            if verdict in ("already_implemented", "partial", "opportunity"):
                measured = True
            if verdict in ("already_implemented", "partial"):
                measured_impl = True
        capabilities.append({
            "id": cap_id, "name": str(cap.get("name") or cap_id),
            "verdict": verdict, "evidence": evidence,
            "estimated_impact": _audit_estimated_impact(verdict, weight),
            "detail": detail,
        })
    score = int(round(100 * numerator / denominator)) if denominator else 0
    if not measured:
        verdict = "not_measured"
    elif score < 25 and not measured_impl:
        # A low score earned purely from sparse/unknown data plus a stray
        # opportunity is absence of evidence, not evidence of optimization —
        # never congratulate an org nothing was actually measured for.
        verdict = "not_measured"
    elif score < 25:
        verdict = "well_optimized"
    elif score < 60:
        verdict = "moderate_opportunity"
    else:
        verdict = "high_opportunity"
    dollars = metrics.get("dollars") or {}
    caveats = ["the traffic audit only sees requests proxied through "
               "Brevitas; capabilities marked unknown need the static code "
               "scan (brevitas audit <repo>)"]
    if no_traffic:
        caveats.append("no proxied traffic recorded for this key; run the "
                       "static scanner or route traffic first")
    caveats.extend(str(a) for a in (metrics.get("approximations") or []))
    return {
        "schema": "brevitas.audit.v1",
        "source": "traffic",
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "providers_detected": providers,
        "score": score,
        "verdict": verdict,
        "capabilities": capabilities,
        "caveats": caveats,
        # Additive dollar context — real numbers from priced usage rows, the
        # one place the audit may print dollars (static reports never do).
        "traffic": {
            "window_rows": _i_safe(metrics.get("window_rows")),
            "cache_hit_rate_pct": (metrics.get("cache") or {}).get("hit_rate_pct"),
            "native_cache_discount_usd": float(
                dollars.get("native_cache_discount_usd") or 0.0),
            "attributable_discount_usd": float(
                dollars.get("attributable_discount_usd") or 0.0),
            "calls_avoided": _i_safe(
                (metrics.get("repeat") or {}).get("calls_avoided")),
        },
    }


@app.get("/v1/audit")
@limiter.limit("120/minute")
def audit_report(request: Request, kh: str = Depends(_authenticated)):
    context = _require_scope(request, kh, "usage:read_own")
    metrics = _store.audit_metrics(kh)
    return _spend_filtered(context, _build_audit_report(metrics))


@app.get("/v1/admin/stats")
@limiter.limit("60/minute")
def admin_stats(request: Request, _: str = Depends(_admin_authenticated)):
    _audit_platform_read(request, _, "platform.usage_overview.read")
    return _store.get_admin_stats()


@app.get("/v1/admin/keys")
@limiter.limit("60/minute")
def admin_keys(request: Request, _: str = Depends(_admin_authenticated)):
    _audit_platform_read(request, _, "platform.key_inventory.read")
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
    _audit_platform_read(request, _, "platform.usage_breakdown.read")
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


@app.get("/v1/admin/accounts/{owner_id}/usage")
@limiter.limit("60/minute")
def admin_account_usage(request: Request, owner_id: str, _: str = Depends(_admin_authenticated)):
    if not (0 < len(owner_id) <= 64 and all(c.isalnum() or c in "-_" for c in owner_id)):
        raise HTTPException(status_code=400, detail="Invalid account id")
    _audit_platform_read(request, _, "platform.account_usage.read",
                         target_type="account", target_id=owner_id)
    return _store.get_admin_account_detail(owner_id)


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
    _audit_platform_read(request, _, "platform.billing.read")
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
        # Same number under its honest name; see the response-level comment below.
        bucket["gross_positive_row_fees_usd"] = bucket["amount_owed_usd"]
    totals = report["totals"]
    gross_row_fees_usd = round(float(totals.get("total_brevitas_fee_usd") or 0), 8)
    return {
        "currency": "USD",
        # NOT a settlement figure. This is sum(usage_log.brevitas_fee_usd), and each
        # row's fee was floored at zero when it was written (_record_usage_report):
        # a byte-preserving row may legitimately carry NEGATIVE savings, so a period
        # whose true net is negative still bills every positive row in it. No
        # warm_budget_ledger deduction is applied either, which 202607280008 calls a
        # mandatory 100% pre-rate deduction. The honest figure is period-scoped
        # (202607280007/0008) and has no writer yet, hence settlement_pending.
        #
        # amount_owed_usd is retained ON PURPOSE while the dashboard still reads it:
        # the API (Railway) and the dashboard (Vercel) do not ship together, and
        # dashboard billingUsd() renders `Number(undefined || 0)` as a confident
        # "$0.00". Drop it only after the dashboard reads the field below.
        "amount_owed_usd": gross_row_fees_usd,
        "gross_positive_row_fees_usd": gross_row_fees_usd,
        "netted": False,
        "warm_spend_deducted": False,
        "settlement_pending": True,
        "basis": "gross_positive_row_fees_unnetted",
        "payment_status_tracked": False,
        "accounts": sorted(accounts.values(),
                           key=lambda item: (-item["amount_owed_usd"], item["account_id"])),
        "range": range,
        # Where the honest number lives, so an operator reading this payload is
        # never left guessing why amount_owed_usd is labelled unnetted.
        "settlement_endpoint": "/v1/admin/billing/settlement",
    }


# Reasons the settlement view can refuse that this route decides itself, rather
# than receiving from the store. Kept in the same vocabulary as the store's
# (lowercase snake_case, machine-readable, never a sentence to parse).
_SETTLEMENT_STORE_MISSING = "settlement_read_unsupported_by_store"
_SETTLEMENT_READ_FAILED = "settlement_read_failed"


@app.get("/v1/admin/billing/settlement")
@limiter.limit("30/minute")
def admin_billing_settlement(request: Request, _: str = Depends(_admin_authenticated)):
    """The netted, period-scoped figure — or an explicit refusal to state one.

    /v1/admin/billing reports sum(usage_log.brevitas_fee_usd): every row floored
    at zero, no warm-spend deduction, no period boundary. This route reports
    what 202607280007/0008/0012/0013 actually define — savings netted across the
    period, warm ping spend deducted before the rate, and the ledger's own
    settled/reported/committed buckets kept apart from the open week's estimate.

    THE CONTRACT IS "settleable", NOT "amount". Every failure mode below returns
    {"settleable": false, "reason": "..."} and states no money at all:

      * the migration chain is not applied yet (PGRST202 / missing relation) —
        the normal state on production today, since api/ deploys on push and
        migrations are hand-applied one file at a time with no ledger;
      * the store backend has no settlement ledger (SQLite);
      * any authoritative usage row carries no organization_id, so it is
        invisible to every per-organization evidence query and the platform
        total would silently understate by an unknown amount;
      * the read failed for any other reason.

    A $0 here would be indistinguishable from "nothing is owed" on an invoice
    screen, and that specific wrong number is what this workstream exists to
    prevent. /v1/admin/billing keeps amount_owed_usd untouched until the
    dashboard is confirmed reading this route.
    """
    _audit_platform_read(request, _, "platform.billing_settlement.read")
    base = {"currency": "USD", "basis": "period_settlement_netted",
            "netted": True, "warm_spend_deducted": True,
            "unattributed_authoritative_usage": None, "organizations": []}
    if not hasattr(_store, "get_billing_settlement"):
        return {**base, "settleable": False, "reason": _SETTLEMENT_STORE_MISSING}
    try:
        settlement = _store.get_billing_settlement()
    except Exception as exc:
        # Degrade, never 500: an operator staring at a stack trace and an
        # operator staring at "$0.00" both make the wrong call, but only one of
        # them can be told what is actually wrong. Type-only, like every other
        # money-path log.
        logger.error("admin settlement read failed error_type=%s", type(exc).__name__)
        return {**base, "settleable": False, "reason": _SETTLEMENT_READ_FAILED}
    if not isinstance(settlement, dict):  # pragma: no cover - defensive
        return {**base, "settleable": False, "reason": _SETTLEMENT_READ_FAILED}
    return {**base, **settlement,
            "settleable": settlement.get("settleable") is True}


@app.get("/v1/admin/analytics")
@limiter.limit("30/minute")
def admin_analytics(
    request: Request,
    range: str = Query("30d", pattern=r"^(7d|30d|90d)$"),
    _: str = Depends(_admin_authenticated),
):
    _audit_platform_read(request, _, "platform.analytics.read")
    return _posthog_admin_summary(int(range[:-1]))


@app.get("/v1/stats/pipelines")
@limiter.limit("120/minute")
def stats_pipelines(request: Request, kh: str = Depends(_authenticated)):
    context = _require_scope(request, kh, "usage:read_own")
    return _spend_filtered_rows(context, _store.get_stats_by_pipeline(kh))


@app.get("/v1/stats/agents")
@limiter.limit("120/minute")
def stats_agents(request: Request, pipeline: str = "", kh: str = Depends(_authenticated)):
    context = _require_scope(request, kh, "usage:read_own")
    return _spend_filtered_rows(context, _store.get_stats_by_agent(kh, pipeline=pipeline))


@app.get("/v1/stats/runs")
@limiter.limit("120/minute")
def stats_runs(request: Request, pipeline: str = "", kh: str = Depends(_authenticated)):
    context = _require_scope(request, kh, "usage:read_own")
    return _spend_filtered_rows(context, _store.get_stats_by_run(kh, pipeline=pipeline))


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


_DEPENDENCY_PROBE_TTL_S = 2.0
_DEPENDENCY_PROBE_FAILURE_TTL_S = 1.0
_dependency_probe_cache: dict[str, dict] = {}
_dependency_probe_running: set[str] = set()
_dependency_probe_lock = threading.Lock()


async def _cached_dependency_probe(name: str, probe: Callable[[], Any],
                                   timeout: float) -> bool:
    """Memoize one readiness probe for a couple of seconds, one probe at a time.

    /v1/health and /v1/health/ready are unauthenticated and reachable through the marketing
    origin's /v1/:path* rewrite, and each call ran an uncached PostgREST GET plus a Redis
    PING — an anonymous loop was therefore an amplifier onto the same Supabase budget and
    the same default executor the proxy auth path uses. Caching makes the endpoint O(1) in
    call rate while keeping the platform probe meaningful: railway.json's healthcheckTimeout
    is 120 s, so a 2 s window cannot mask an outage from the prober, and a FAILURE is held
    for less time than a success so recovery is never delayed.

    No asyncio primitives on purpose: this module-level state outlives any single event loop
    (every TestClient creates a new one), and an asyncio.Lock would bind to the first.
    """
    now = _time.monotonic()
    with _dependency_probe_lock:
        entry = _dependency_probe_cache.get(name)
        if entry is not None and now - entry["ts"] < entry["ttl"]:
            return bool(entry["ready"])
        if name in _dependency_probe_running:
            # A probe is already in flight; serve the previous verdict rather than stacking
            # a second dependency call behind it. No verdict yet means not-ready.
            return bool(entry["ready"]) if entry is not None else False
        _dependency_probe_running.add(name)
    ready = False
    completed = False
    try:
        result = probe()
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            ready = bool(await asyncio.wait_for(result, timeout=timeout))
        else:
            ready = bool(result)
        completed = True
    except (Exception, asyncio.TimeoutError):
        ready = False
        completed = True
    finally:
        with _dependency_probe_lock:
            # Publish before clearing the marker so the next caller reads the fresh verdict
            # instead of starting a second probe in the same instant. A cancelled probe
            # publishes nothing: the caller went away, its verdict is not evidence.
            if completed:
                _dependency_probe_cache[name] = {
                    "ts": _time.monotonic(), "ready": ready,
                    "ttl": (_DEPENDENCY_PROBE_TTL_S if ready
                            else _DEPENDENCY_PROBE_FAILURE_TTL_S),
                }
            _dependency_probe_running.discard(name)
    return ready


@app.get("/v1/health/ready")
async def readiness():
    """Railway's healthcheck target: deliberately NOT rate limited.

    _rate_key buckets on the peer address, which behind the Railway edge collapses every
    caller onto one bucket, so a 429 here would be answered to the platform prober itself
    and cause the very restart loop this endpoint exists to avoid. The dependency probes are
    TTL-cached instead, which bounds the cost regardless of call rate.
    """
    return await health()


@app.get("/v1/health")
@limiter.limit("120/minute")
async def public_health(request: Request):
    """Public alias, reachable anonymously through the marketing origin's rewrite."""
    return await health()


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
    # Keyed by the identity of the probed dependency: a swapped store/limiter (deployment
    # reconfiguration, or a test double) must never read the previous object's verdict.
    database_ready = await _cached_dependency_probe(
        f"postgres:{id(_store)}", lambda: asyncio.to_thread(_store.healthy),
        dependency_timeout)
    redis_ready = await _cached_dependency_probe(
        f"redis:{id(_distributed_limiter)}", _distributed_limiter.healthy,
        dependency_timeout)
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
    """In-process bridge: hosted proxy receipts use the caller's tenant key.

    This is the ONLY path that writes authoritative (billable) usage, and its
    caller (brevitas.proxy._emit_usage) swallows everything by contract so a
    reporting fault can never alter a provider response. Every non-recording exit
    is therefore logged here, type-only — including the two plain `return`s, which
    a logger inside _emit_usage's except clause would never see. A dropped receipt
    is lost revenue and understated customer savings, so it must not be silent.
    Never fail open: an unverified key is logged and dropped, never recorded.
    """
    if not raw_key:
        logger.warning("hosted proxy receipt dropped reason=missing_key")
        return
    kh = hash_key(raw_key)
    try:
        validity = _key_validity(kh)
    except Exception as exc:
        # _key_validity is a store read, so a transient database fault used to
        # leave this function by RAISING — straight into _emit_usage's
        # swallow-everything except clause, with no log anywhere. A dropped
        # billable receipt must never be silent, and the claim in the docstring
        # above ("every non-recording exit is logged here") has to stay true.
        # Still fails CLOSED: an unverifiable key is dropped, never recorded.
        logger.error("hosted proxy receipt dropped reason=key_validity_unavailable "
                     "error_type=%s", type(exc).__name__)
        return
    if validity != "valid":
        logger.warning("hosted proxy receipt dropped reason=%s", validity)
        return
    payload = dict(payload)
    tenant_gate_key = str(payload.pop("_brevitas_tenant_key", "") or kh)
    # The metering id is minted server-side per request by brevitas.proxy._request_id.
    # Re-derive it here as well: request_id is the billing dedupe key, so anything
    # outside the server-minted namespace (a caller header smuggled onto the payload,
    # an older SDK's collapsed id) must never become one.
    if not str(payload.get("request_id") or "").startswith(RECEIPT_ID_PREFIX):
        payload["request_id"] = f"{RECEIPT_ID_PREFIX}{uuid.uuid4().hex}"
    try:
        # Inside the try ON PURPOSE. _auth_context_for_key is another store read
        # and it used to sit outside, so a transient fault resolving the tenant
        # context dropped the receipt with no log at all. It is NOT degraded to
        # a None context: that path writes organization_id='' and an
        # unattributed row is exactly what makes a period un-settleable
        # (/v1/admin/billing/settlement). Losing the row loudly beats
        # mis-attributing it silently.
        context = _proxy_auth_context.get()
        if context is None or context.key_hash != kh:
            context = _auth_context_for_key(kh)
        try:
            report = UsageReportRequest.model_validate(payload)
        except ValidationError:
            # Money must never depend on a cosmetic label passing validation. The
            # tracking labels are caller-supplied, and every one of them is
            # length- and control-character-checked; a single bad label used to
            # raise here, get swallowed below, and silently drop a billable
            # receipt — metering suppression by way of an HTTP header. Retry once
            # with the labels cleared so the money is recorded either way.
            report = UsageReportRequest.model_validate(
                {key: value for key, value in payload.items() if key not in _RECEIPT_LABEL_FIELDS})
            logger.warning("hosted proxy receipt metadata coerced reason=label_validation")
        result = _record_usage_report(kh, report, auth_context=context, authoritative=True,
                                     tenant_gate_key=tenant_gate_key)
    except Exception as exc:
        logger.error("hosted proxy receipt write failed error_type=%s", type(exc).__name__)
        return
    if result.get("duplicate"):
        # Server-minted ids make this unreachable for an ordinary request, so it
        # means a genuine id collision — i.e. billable savings were just dropped.
        logger.error("hosted proxy receipt dropped reason=duplicate_request_id")


def _hosted_warm_observe(organization_id: str, customer_id: str,
                         prefix: WarmPrefix, cache_read: bool) -> None:
    """In-process bridge: encrypt an observed warm prefix and record its arrival.

    Best-effort by contract — every failure is logged type-only and swallowed,
    so observation can never affect the response path that fired it. The
    plaintext wraps the canonical payload with the recording service key hash:
    the worker refuses to attribute ping spend without a live organization_service
    key, so requests made with any other key type are not observable at all
    (recording them would only churn claim→prefix_invalid in the worker).
    """
    try:
        auth_context = _proxy_auth_context.get()
        if (auth_context is None
                or auth_context.key_type != "organization_service"
                or auth_context.organization_id != organization_id):
            # Reason-carrying, never silent: a None context here previously hid a
            # contextvars-propagation bug that no-opped every hosted observation.
            # Log the organization id only — the prefix payload is customer content.
            reason = ("missing_auth_context" if auth_context is None
                      else "key_type_not_observable"
                      if auth_context.key_type != "organization_service"
                      else "organization_mismatch")
            logger.warning(
                "warm prefix observation skipped reason=%s organization_id=%s",
                reason, organization_id)
            return
        payload_ciphertext = _encrypt(
            json.dumps({"recorded_by_key_hash": auth_context.key_hash,
                        "payload": prefix.payload},
                       sort_keys=True, separators=(",", ":"), default=str),
            # Must byte-match the worker's decrypt context (_warm_one,
            # api/worker.py) — the envelope cipher binds it, so any drift
            # makes every ping fail.
            context={
                "purpose": "warm_prefix_payload",
                "organization_id": organization_id,
                "customer_id": customer_id,
            })
        # Observer-priced reserve for one keep-alive, held against the daily
        # budget by warm_due_claim. The reserve must upper-bound whatever
        # settle can book, or the daily ceiling stops being structural —
        # warm_ping_settle adds real receipt cost with no clamp, and the
        # claim-time BREVITAS_WARM_RESERVE_USD_PER_MTOK floor is an
        # anthropic-calibrated tunable (0..1000), not a guarantee. Anthropic
        # worst case is a full cache write at the model+TTL premium.
        # Automatic-cache providers (deepseek) have no write premium, but
        # their worst case is a ping whose entry already evicted: the whole
        # prefix billed at the full input rate, 39-120x the cached rate — so
        # the cached (best-case) rate is never a valid reserve. None
        # (unpriced model) keeps any stored value.
        price = model_price(prefix.provider, prefix.model)
        ping_reserve_usd = None
        if price:
            if prefix.provider == "anthropic":
                ping_rate = (price.get("write_1h", price["input"] * 2.0)
                             if prefix.provider_ttl_seconds > 300
                             else price.get("write", price["input"] * 1.25))
            else:
                ping_rate = max(price["input"],
                                price.get("write", price["input"]))
            ping_reserve_usd = round(
                ping_rate * prefix.prefix_tokens / 1_000_000.0, 10)
        _store.warm_prefix_observe(
            organization_id, customer_id, prefix.provider, prefix.prefix_hash,
            payload_ciphertext, prefix.prefix_tokens, prefix.provider_ttl_seconds,
            int(os.getenv("BREVITAS_WARM_SAFETY_MARGIN_SECONDS", "60")),
            cache_read, ping_reserve_usd=ping_reserve_usd)
    except Exception as exc:
        logger.warning("warm prefix observation failed error_type=%s",
                       type(exc).__name__)


# Railway serves the management API and provider-compatible proxy from one process.
from brevitas.proxy import RECEIPT_ID_PREFIX, proxy_app, set_usage_reporter
set_usage_reporter(_hosted_proxy_receipt)
set_warm_observer(_hosted_warm_observe)
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
