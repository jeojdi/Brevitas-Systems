"""
Local HTTP proxy that speaks both the Anthropic Messages API and the
OpenAI Chat Completions API.

Zero-code integration — set one env var and your existing code works:

    export ANTHROPIC_BASE_URL=http://localhost:4242
    export OPENAI_BASE_URL=http://localhost:4242/openai

Start:
    brevitas start [--port 4242] [--api-key bvt_...]

--base-url overrides the Brevitas control plane and defaults to
https://api.brevitassystems.com; pass it only when you are running your own API
(self-hosted or local development), e.g. --base-url http://localhost:8000.

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
import logging
import os
import time
import asyncio
import contextvars
import inspect
import math
import threading
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ._compress import count_messages_tokens, report_usage
from .identity import CUSTOMER_ID_HEADER, normalize_customer_id, short_tenant_key, tenant_key
from .labels import _git_root_name
from .provider_reliability import ProviderCircuitOpen, provider_http
from .receipts import SSEUsageParser, TokenReceipt, _int as _usage_int, count_request_tokens, normalize_usage
from .resource_bounds import (
    BoundedTTLMap,
    ResourceBounds,
    ResourceLimitExceeded,
    safe_close_resource,
    serialized_size_bytes,
    utf8_size,
)
from .session import BrevitasSession
from .warming import extract_warm_prefix, get_warm_observer, provider_warmable
from token_efficiency_model.lossless import state_store
from token_efficiency_model.lossless.batch_group import BatchGroupGate
from token_efficiency_model.lossless.engine import optimize_request, record_usage
from token_efficiency_model.lossless.router import BrevitasRouter
from token_efficiency_model.quality.gate import lever_allowed

# getLogger only: this module ships inside the customer-installed SDK, so it must
# never configure handlers or levels for the host application.
logger = logging.getLogger("brevitas.proxy")

# Batch prefix grouping (pathfinder gate, CR1): concurrent same-prefix requests wait
# for the first one's prefill to write the provider cache, then read it instead of all
# re-writing the same bytes. Only ever holds a request whose identical-prefix sibling
# is ALREADY in flight (bounded by max_wait), so interactive traffic is never touched.
# On by default; BREVITAS_BATCH_GROUP=0 is the kill-switch.
_BG_ON = os.environ.get("BREVITAS_BATCH_GROUP", "1") not in ("0", "false", "no")

# Providers whose streamed responses omit usage unless the request opts in.
# DeepSeek is deliberately absent: it reports usage on the final chunk by
# default and its request bodies stay byte-identical end to end.
_STREAM_USAGE_PROVIDERS = {"openai", "xai"}


def _stream_usage_inject_enabled() -> bool:
    return os.environ.get("BREVITAS_STREAM_USAGE_INJECT", "1") not in ("0", "false", "no")


def _xai_affinity_enabled() -> bool:
    return os.environ.get("BREVITAS_XAI_CONV_AFFINITY", "1") not in ("0", "false", "no")


def _inject_stream_usage(body: dict, provider: str) -> None:
    """Ask the provider to append its standard usage chunk to the stream.

    Without it, streamed OpenAI/xAI responses carry no usage object, so cache
    discounts on streams are invisible to metering. Only fires when the caller
    did not set stream_options themselves; deltas/content are unaffected and
    the provider bytes are forwarded verbatim.
    """
    if (body.get("stream") and provider in _STREAM_USAGE_PROVIDERS
            and "stream_options" not in body and _stream_usage_inject_enabled()):
        body["stream_options"] = {"include_usage": True}


def _apply_xai_affinity(headers: dict[str, str], request: Request,
                        provider: str, state_key: str,
                        upstream_base: str = "") -> bool:
    """Pin xAI replica affinity so their prompt cache can actually hit.

    Verified live 2026-07-28: xAI's cached_tokens never exceeds the ~128-token
    template floor without x-grok-conv-id — prompt_cache_key alone routes to
    arbitrary replicas and misses. The id is an opaque tenant-scoped digest
    (no PII); a caller-supplied header always wins.

    Header injection is gated on the declared provider (x-brevitas-provider is
    the routing contract, see _provider_for). ATTRIBUTION is gated on the
    resolved destination as well: the return value drives cache_attributable,
    i.e. "this discount exists because Brevitas caused it", which is only true
    when the bytes actually went to api.x.ai. x-brevitas-upstream can send an
    xai-labelled request to a different allowlisted host, and billing 25% of a
    discount Brevitas demonstrably did not cause would not survive an audit.
    """
    if provider != "xai" or not _xai_affinity_enabled() or not state_key:
        return False
    if request.headers.get("x-grok-conv-id"):
        return False
    digest = hashlib.sha256(f"brevitas-xai-affinity:{state_key}".encode()).hexdigest()
    headers["x-grok-conv-id"] = digest[:32]
    return upstream_base == _UPSTREAMS["xai"]
_bg = BatchGroupGate(max_wait=float(os.environ.get("BREVITAS_BATCH_GROUP_MAX_WAIT", "15")))
_BG_WARM = {"deepseek": 2880.0, "anthropic": 240.0, "openai": 240.0}  # ~0.8x cache TTL

_RESOURCE_BOUNDS = ResourceBounds.from_env()
_ROUTER_SESSION_FIELDS = (
    "msg_hashes", "msg_tokens", "last_ts", "obs_hit", "obs_count", "keep_frac",
    "last_est", "tok_ratio", "last_strategy", "gap_ewma", "repeat_observations",
    "cache_read_tokens", "cache_write_tokens", "cache_net_units",
    "cache_negative_writes", "cache_blocked_until",
)


def _copy_router(router: BrevitasRouter) -> BrevitasRouter:
    """Return an isolated router; never expose the registry-owned mutable value."""
    client = getattr(router, "client", None)
    memo = {id(client): client} if client is not None else None
    return deepcopy(router, memo)


def _registry_resource(value: object) -> object:
    """Return the pool owned by a registry value, or the value itself."""
    client = getattr(value, "client", None)
    return client if client is not None else value


def _close_registry_value(value: object) -> None:
    safe_close_resource(_registry_resource(value))


def _router_size(router: BrevitasRouter) -> int:
    sessions = {
        str(session_id): {
            name: getattr(state, name, None) for name in _ROUTER_SESSION_FIELDS
        }
        for session_id, state in getattr(router._sessions, "_sessions", {}).items()
    }
    return serialized_size_bytes({
        "provider": router.provider,
        "model": router.model,
        "retrieve_keep_frac": router.retrieve_keep_frac,
        "sessions": sessions,
    })


def _copy_session(session: BrevitasSession) -> BrevitasSession:
    """Copy content and counters while retaining only the original clock handle."""
    copied = BrevitasSession(
        session_id=session.session_id,
        prior_ttl_s=session.prior_ttl_s,
        max_prior_items=session.max_prior_items,
        max_prior_bytes=session.max_prior_bytes,
        max_prior_item_bytes=session.max_prior_item_bytes,
        clock=session._clock,
    )
    with session._lock:
        copied._prior_content = deque(session._prior_content)
        copied._prior_bytes = session._prior_bytes
        copied.hop_count = session.hop_count
        copied.last_quality = session.last_quality
    if hasattr(session, "client"):
        copied.client = session.client
    return copied


def _session_size(session: BrevitasSession) -> int:
    return 512 + utf8_size(session.session_id) + session.retained_bytes


def _make_router_registry(
    bounds: ResourceBounds, *, clock: Callable[[], float] = time.monotonic,
) -> BoundedTTLMap[str, BrevitasRouter]:
    return BoundedTTLMap(
        ttl_s=bounds.registry_ttl_s,
        max_entries=bounds.registry_max_entries,
        max_value_bytes=bounds.registry_max_value_bytes,
        clock=clock,
        sizer=_router_size,
        copier=_copy_router,
        snapshotter=_copy_router,
        on_remove=_close_registry_value,
        resource_key=_registry_resource,
    )


def _make_session_registry(
    bounds: ResourceBounds, *, clock: Callable[[], float] = time.monotonic,
) -> BoundedTTLMap[str, BrevitasSession]:
    return BoundedTTLMap(
        ttl_s=min(bounds.registry_ttl_s, bounds.session_content_ttl_s),
        max_entries=bounds.registry_max_entries,
        max_value_bytes=bounds.registry_max_value_bytes,
        clock=clock,
        sizer=_session_size,
        copier=_copy_session,
        snapshotter=_copy_session,
        on_remove=_close_registry_value,
        resource_key=_registry_resource,
    )


_routers = _make_router_registry(_RESOURCE_BOUNDS)
_sessions = _make_session_registry(_RESOURCE_BOUNDS)
_router_registry_lock = threading.RLock()
_session_registry_lock = threading.RLock()
_SESSION_CONTENT_BUDGET = max(
    1, min(_RESOURCE_BOUNDS.session_max_bytes,
           _RESOURCE_BOUNDS.registry_max_value_bytes - 512)
)


@dataclass(frozen=True)
class _RouterHandle:
    key: str
    provider: str


def _router_for(key: str, provider: str) -> _RouterHandle:
    with _router_registry_lock:
        _routers.get_or_create(key, lambda: BrevitasRouter(provider=provider))
    return _RouterHandle(key, provider)


def _mutate_router(handle: _RouterHandle, mutator: Callable[[BrevitasRouter], Any]) -> Any:
    result: list[Any] = []

    def apply(router: BrevitasRouter) -> BrevitasRouter:
        result.append(mutator(router))
        return router

    with _router_registry_lock:
        _routers.get_or_create(
            handle.key, lambda: BrevitasRouter(provider=handle.provider))
        _routers.mutate(handle.key, apply, copier=_copy_router)
    return result[0] if result else None


@dataclass
class _SessionHandle:
    key: str
    session_id: str

    @property
    def last_quality(self) -> float | None:
        snapshot = _sessions.get(self.key)
        return snapshot.last_quality if snapshot is not None else None

    @last_quality.setter
    def last_quality(self, value: float | None) -> None:
        _mutate_session(self, lambda session: setattr(session, "last_quality", value))

    def record_response(self, text: str) -> None:
        try:
            _mutate_session(self, lambda session: session.record_response(text))
        except ResourceLimitExceeded:
            pass

    def advance(self) -> None:
        _mutate_session(self, lambda session: session.advance())

    def prior_context(self) -> list[str]:
        snapshot = _sessions.get(self.key)
        return snapshot.prior_context() if snapshot is not None else []


def _new_session(session_id: str = "") -> BrevitasSession:
    return BrevitasSession(
        session_id=session_id,
        max_prior_items=_RESOURCE_BOUNDS.session_max_items,
        max_prior_bytes=_SESSION_CONTENT_BUDGET,
        max_prior_item_bytes=min(
            _RESOURCE_BOUNDS.session_max_item_bytes, _SESSION_CONTENT_BUDGET),
    )


def _stable_session_id(key: str) -> str:
    """Derive a receipt-stable session id from the bounded session key.

    `BrevitasSession` falls back to `"sess_" + secrets.token_urlsafe(12)` when it
    is given no id. `_session_for` used to pass `_new_session` to
    `get_or_create` as a zero-arg factory, so the session key never reached the
    session and every bucket got a fresh random id. That id is what lands in
    `usage_log.session_id`, so receipts for one tenant+provider+model+operation
    +agent bucket were unjoinable across a proxy restart or an LRU/TTL eviction
    of that bucket -- the same logical session emitted several unrelated ids.

    Hashing rather than using `key` directly is deliberate on two counts: the
    key embeds the tenant credential digest (`_tenant_context`), which must not
    be republished in a receipt column; and `UsageReportRequest.session_id`
    caps at 128 characters, which an unbounded model name in the key could
    otherwise breach and turn a receipt into a 422.
    """
    digest = hashlib.sha256(f"brevitas-session:{key}".encode()).hexdigest()
    return "sess_" + digest[:24]


def _session_for(key: str, session_id: str = "") -> _SessionHandle:
    """Look up (or open) the session bucket for `key`.

    `session_id` pins the identity a caller already computed for this session,
    for the paths that re-enter with a receipt in hand. Left empty, the identity
    is derived from the key.
    """
    identity = session_id or _stable_session_id(key)
    with _session_registry_lock:
        snapshot = _sessions.get_or_create(key, lambda: _new_session(identity))
    return _SessionHandle(key, snapshot.session_id)


def _mutate_session(handle: _SessionHandle,
                    mutator: Callable[[BrevitasSession], Any]) -> Any:
    result: list[Any] = []

    def apply(session: BrevitasSession) -> BrevitasSession:
        result.append(mutator(session))
        return session

    with _session_registry_lock:
        updated = _sessions.mutate(handle.key, apply, copier=_copy_session)
        if updated is None:
            replacement = _new_session(handle.session_id)
            _sessions.put(handle.key, replacement)
            _sessions.mutate(handle.key, apply, copier=_copy_session)
    return result[0] if result else None


def _optimize_fail_open(body: dict, provider: str, router: _RouterHandle,
                        session_id: str, **kwargs: Any) -> dict:
    """Optimize transactionally; any failure restores the exact caller body.

    The proxy is shared infrastructure for multiple API dialects. An optimizer bug must
    therefore degrade to passthrough for one request, never corrupt a partially-mutated
    payload or take every configured client offline.
    """
    original = deepcopy(body)
    try:
        return _mutate_router(
            router,
            lambda value: optimize_request(
                body, provider, value, session_id, **kwargs),
        )
    except Exception as exc:
        body.clear()
        body.update(original)
        return {
            "strategy": "passthrough",
            "reason": "optimizer_fail_open",
            "quality_status": "byte_preserving",
            "optimizer_error": type(exc).__name__,
        }


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
    _restore_target: dict[str, BrevitasRouter] = {}
    _restored = state_store.load(
        _STATE_FILE, _restore_target, lambda prov: BrevitasRouter(provider=prov))
    for _restored_key, _restored_router in _restore_target.items():
        try:
            _routers.put(_restored_key, _restored_router)
        except ResourceLimitExceeded:
            pass
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
        with _router_registry_lock:
            state_store.save(_STATE_FILE, dict(_routers.items()))

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
# Destination host -> provider. Keyed on netloc, not the base URL: groq's base
# carries a path (/openai) and perplexity's chat endpoint is a non-/v1 override,
# so only the host is a stable identity for "where did the bytes actually go".
_UPSTREAM_HOSTS = {urlsplit(base).netloc.lower(): provider
                   for provider, base in _UPSTREAMS.items()}


def _provider_for_host(base: str) -> str:
    """The provider label implied by a resolved destination, '' if unrecognized.

    x-brevitas-provider (routing/behaviour) and x-brevitas-upstream (destination)
    are independent caller headers, so the BILLED label must be derived from the
    destination instead of the declaration. Otherwise a tenant routes to
    api.openai.com while declaring a reseller label, which has no MODEL_PRICES
    rows, so every receipt prices as 'unpriced' and never bills.
    """
    return _UPSTREAM_HOSTS.get(urlsplit(base or "").netloc.lower(), "")

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
# Hosted caching is fail-closed and tenant opt-in. Standalone local proxy users may
# explicitly opt in with BREVITAS_CACHE_LOCAL=true.
_cache_singleton: Any = None
_cache_init_done = False


def _get_cache():
    global _cache_singleton, _cache_init_done
    if _cache_init_done:
        return _cache_singleton
    _cache_init_done = True
    if os.getenv("BREVITAS_CACHE_ENABLED", "false").lower() not in ("1", "true", "yes"):
        _cache_singleton = None
        return None
    try:
        from .semantic_cache import make_semantic_cache
        _cache_singleton = make_semantic_cache()
    except Exception as exc:
        # Still never raises — but say why, or an absent optional dependency
        # (e.g. cryptography) reads as "caching is off" with no way to tell that
        # the operator asked for it and did not get it.
        logger.warning(
            "semantic cache disabled despite BREVITAS_CACHE_ENABLED: %s: %s",
            type(exc).__name__, exc,
        )
        _cache_singleton = None
    return _cache_singleton


def _cache_for_request(request: Request):
    """Return a cache only after the authenticated tenant explicitly opted in."""
    tenant_opt_in = bool(getattr(request.state, "brevitas_cache_enabled", False))
    local_opt_in = os.getenv("BREVITAS_CACHE_LOCAL", "false").lower() in ("1", "true", "yes")
    return _get_cache() if tenant_opt_in or local_opt_in else None


def _admission_canceled(request: Request) -> bool:
    """Cooperatively stop upstream/receipt work after a distributed lease loss."""
    event = getattr(request.state, "brevitas_admission_cancellation", None)
    return bool(event is not None and event.is_set())


def _usage_tokens(data: dict, provider: str) -> tuple[int, int]:
    """(prompt_tokens, completion_tokens) from a provider response usage object."""
    u = (data or {}).get("usage", {}) or {}
    if provider == "anthropic":
        prompt = int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0)) \
            + int(u.get("cache_creation_input_tokens", 0))
        return prompt, int(u.get("output_tokens", 0))
    return int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))


def _cache_lookup(cache, body: dict, provider: str, model: str, gate_key: str = ""):
    try:
        return cache.lookup(body, provider, model, gate_key=gate_key)
    except Exception:
        return None


def _response_complete(data: dict, provider: str) -> bool:
    """Only a naturally-complete response is cacheable — never a truncated one
    (finish_reason/stop_reason of max_tokens/length), which is a partial answer.
    EVERY choice must be complete: a multi-choice response with any truncated choice
    is not cacheable."""
    try:
        if provider == "anthropic":
            return str((data or {}).get("stop_reason") or "") in ("end_turn", "stop_sequence")
        choices = (data or {}).get("choices") or []
        return bool(choices) and all(
            str(c.get("finish_reason") or "") == "stop" for c in choices)
    except Exception:
        return False


def _cache_store(cache, body: dict, provider: str, model: str, data: dict,
                 origin_request_id: str = "") -> None:
    """Store a paid response together with the metering id that paid for it.

    `origin_request_id` is this request's server-minted receipt id (_request_id).
    It is what lets the replay of this entry name a real, receipted, PAID
    ancestor rather than merely assert that one existed. Every legitimate
    Brevitas cache hit has such an ancestor; a forged or buggy zero-spend row
    does not, and stays unanchored — hence unbillable.

    `store` gained this keyword with the anchor; a cache object that predates it
    raises TypeError, which is retried once without the keyword so an older or
    third-party implementation degrades to unanchored instead of losing the
    write. Everything here stays swallowed either way — a cache fault must never
    reach the caller.
    """
    try:
        if not _response_complete(data, provider):
            return                 # never cache a truncated / incomplete response
        p, c = _usage_tokens(data, provider)
    except Exception:
        return
    try:
        cache.store(body, provider, model, data, prompt_tokens=p, completion_tokens=c,
                    origin_request_id=origin_request_id)
        return
    except TypeError:
        pass                       # older/foreign cache object: store unanchored
    except Exception:
        return
    try:
        cache.store(body, provider, model, data, prompt_tokens=p, completion_tokens=c)
    except Exception:
        pass


def _cache_body(body: dict, request: Request, *credentials: str) -> dict:
    """Original request plus safe cache-vary metadata; never persist raw credentials."""
    cached = deepcopy(body)
    organization_id = str(getattr(request.state, "brevitas_organization_id", "") or "")
    customer_id = str(getattr(request.state, "brevitas_customer_id", "") or "") or _customer_id(request)
    if organization_id:
        cached["_brevitas_cache_namespace"] = f"{organization_id}:{customer_id or 'unattributed'}"
    else:
        namespace_parts = [value for value in credentials if value]
        if customer_id:
            namespace_parts.extend(("brevitas-customer", customer_id))
        cached["_brevitas_cache_namespace"] = _key_id("\0".join(namespace_parts))
    cached["_brevitas_cache_vary"] = {
        name: request.headers.get(name, "") for name in (
            "anthropic-version", "anthropic-beta", "openai-organization",
            "openai-project", "openai-beta", "idempotency-key", "x-brevitas-upstream",
        ) if request.headers.get(name)
    }
    cached["_brevitas_cache_vary"]["provider_credential"] = _key_id("\0".join(
        value for value in credentials if value
    ))
    return cached


def _customer_id(request: Request) -> str:
    try:
        return normalize_customer_id(request.headers.get(CUSTOMER_ID_HEADER, ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _tenant_context(request: Request, fallback_credential: str = "") -> tuple[str, str]:
    """Return (full quality-gate key, bounded process-state key)."""
    credential = request.headers.get("x-brevitas-key", "") or fallback_credential
    gate_key = tenant_key(credential, _customer_id(request))
    return gate_key, short_tenant_key(gate_key)


async def _json_object(request: Request) -> tuple[bytes, dict]:
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > _RESOURCE_BOUNDS.request_max_bytes:
                raise HTTPException(status_code=413, detail="Request body is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc

    raw_buffer = bytearray()
    async for chunk in request.stream():
        if len(chunk) > _RESOURCE_BOUNDS.request_max_bytes - len(raw_buffer):
            raise HTTPException(status_code=413, detail="Request body is too large")
        raw_buffer.extend(chunk)
    raw = bytes(raw_buffer)
    try:
        body = json.loads(raw)
    except (RecursionError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    pending: list[Any] = [body]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            if len(value) > _RESOURCE_BOUNDS.request_max_items:
                raise HTTPException(status_code=413, detail="Request contains too many items")
            pending.extend(value)
        elif isinstance(value, dict):
            pending.extend(value.values())
    return raw, body


def _upstream_ok(response: httpx.Response) -> bool:
    return 200 <= int(response.status_code) < 300


async def _report_cache_hit(request: Request, provider: str, model: str, hit,
                            session: _SessionHandle, labels: dict,
                            billing_provider: str = "") -> None:
    """A cache hit avoided both the recorded prompt and completion costs.

    `billing_provider` is the label derived from the resolved destination host
    (_provider_for_host). Cache hits never touch an upstream, so it must be
    computed from the header pair up front: these are the rows that carry
    cache_attributable=True and ~100% savings, so a forged provider label here
    is the single highest-value way to make billable savings price as unpriced.
    """
    baseline = int(hit.prompt_tokens) + int(hit.completion_tokens)
    if baseline > 0:
        session.last_quality = float(getattr(hit, "similarity", 1.0))
        kind = str(getattr(hit, "kind", "semantic") or "semantic").lower()
        strategy = "exact_cache" if kind == "exact" else "semantic_cache"
        # The forward link. This replay's savings are zero-spend BY CONSTRUCTION
        # (no upstream was touched), so the only thing that separates it from a
        # forged or buggy zero-spend row is a provable paid ancestor. The cache
        # entry remembers the metering id of the real, receipted call that filled
        # it; carry that id onto the receipt so the anchor is first-class data
        # rather than an inference. `getattr` because a cache implementation that
        # predates the column returns hits without one — those replay unanchored,
        # which bills zero.
        anchor = str(getattr(hit, "origin_request_id", "") or "")[:128]
        await _emit_usage(request, {"provider": billing_provider or provider, "model": model,
            "operation": "chat", "baseline_tokens": int(hit.prompt_tokens),
            "baseline_output_tokens": int(hit.completion_tokens), "compressed_tokens": 0,
            "fresh_input_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0,
            "output_tokens": 0, "quality_score": session.last_quality,
            "cache_attributable": True,
            # Labels first: server-minted fields must always win. See _record_receipt.
            **labels,
            "request_id": _request_id(request), "strategy": strategy,
            # AFTER the labels for the same reason request_id is: a tracking
            # header named savings_anchor_request_id must never reach the API as
            # an anchor. (_record_usage_report re-decides it anyway.)
            "savings_anchor_request_id": anchor,
            "session_id": session.session_id, "receipt_source": "proxy"})


proxy_app = FastAPI(title="Brevitas Proxy", docs_url=None, redoc_url=None)


async def close_provider_clients() -> None:
    """Close pools, persist content-free learning, and release bounded registries."""
    global _warm_executor
    try:
        if _STATE_FILE:
            with _router_registry_lock:
                state_store.save(_STATE_FILE, dict(_routers.items()))
        await provider_http.aclose()
    finally:
        with _warm_executor_lock:
            if _warm_executor is not None:
                _warm_executor.shutdown(wait=False, cancel_futures=True)
                _warm_executor = None
        with _router_registry_lock:
            _routers.clear()
        with _session_registry_lock:
            _sessions.clear()


# Keep this on the router so FastAPI propagates it when api.server includes the proxy
# routes instead of mounting proxy_app as a child ASGI application.
proxy_app.router.on_shutdown.append(close_provider_clients)


async def _provider_request(provider: str, operation: str, method: str, endpoint: str, *,
                            headers: dict[str, str], stream: bool = False,
                            json_body: Any = None,
                            content: bytes | None = None) -> Any:
    """Send without exposing credentials, content, URLs, or raw transport errors."""
    try:
        return await provider_http.request(
            provider, operation, method, endpoint, headers=headers, stream=stream,
            json=json_body, content=content,
        )
    except ProviderCircuitOpen as exc:
        raise HTTPException(
            status_code=503,
            detail="Model provider is temporarily unavailable",
            headers={"Retry-After": str(max(1, math.ceil(exc.retry_after_s)))},
        ) from None
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
            httpx.PoolTimeout):
        raise HTTPException(status_code=504, detail="Model provider timed out") from None
    except httpx.TransportError:
        raise HTTPException(status_code=502, detail="Model provider connection failed") from None


def _provider_for(model: str, explicit: str = "") -> str:
    # An explicit provider (x-brevitas-provider header or BREVITAS_PROVIDER env) ALWAYS
    # wins — model-name guessing is only a fallback. DeepSeek drop-in users who point
    # OPENAI_BASE_URL at the proxy should set the provider so routing is never guessed.
    if explicit in _UPSTREAMS:
        return explicit
    value = (model or "").lower()
    if "deepseek" in value:                 # substring, not just prefix: catch ds-* aliases
        return "deepseek"
    if value.startswith(("grok", "xai")):
        return "xai"
    if value.startswith("mistral") or value.startswith("codestral"):
        return "mistral"
    return "openai"


def _explicit_provider(request: Request) -> str:
    """Caller-declared provider: the x-brevitas-provider header, else BREVITAS_PROVIDER env.
    Used so OpenAI-compatible drop-in traffic (DeepSeek, Groq, …) routes to the right
    upstream even when the model name is ambiguous."""
    return request.headers.get("x-brevitas-provider", "") or os.getenv("BREVITAS_PROVIDER", "")


# Namespace for every server-minted metering id. It is what keeps a proxy receipt
# from ever colliding with a caller-declared /v1/usage idempotency key on the same
# key hash, and it is what api.server._hosted_proxy_receipt checks before trusting
# an incoming request_id. Keep the two in sync.
RECEIPT_ID_PREFIX = "proxy:"


def _request_id(request: Request, provider_id: str = "") -> str:
    """Mint this request's metering id — never read it from a caller header.

    The receipt's request_id IS the billing dedupe key: the API drops any receipt
    whose (key_hash, request_id) already exists, and a unique index enforces the
    same thing. Taking it from x-brevitas-request-id / x-client-request-id let a
    tenant pin one constant value and suppress every receipt after the first for
    the life of that key — full optimization, one metered call. Gateways also set
    x-client-request-id to non-unique values by accident, which zeroes that
    tenant's own savings dashboard.

    The caller's correlation id is not lost: the observability middleware still
    reads those headers and echoes the id back on the response.

    Cached on request.state so a request that emits more than once (e.g. a
    receipt after a cache-hit report) yields exactly ONE metering id, which a
    bare uuid4 at emit time would not give.
    """
    minted = str(getattr(request.state, "brevitas_receipt_id", "") or "")
    if not minted:
        # Only borrow the provider's response id when it can actually carry
        # uniqueness. Truncating a long id at 128 would let two responses sharing
        # a prefix mint the same metering id, and an upstream that returns a
        # constant or near-empty id would make every call after the first
        # unmetered — both losses show up as `duplicate_request_id`, not as
        # missing revenue. uuid4 is the safe fallback in every doubtful case.
        borrowed = str(provider_id or "")
        if len(borrowed) < 8 or len(borrowed) > 100:
            borrowed = ""
        minted = f"{RECEIPT_ID_PREFIX}{borrowed or uuid.uuid4().hex}"
        request.state.brevitas_receipt_id = minted
    return minted


async def _emit_usage(request: Request, payload: dict) -> None:
    """Best effort only: reporting errors never alter the provider response."""
    try:
        if _usage_reporter is not None:
            key = request.headers.get("x-brevitas-key", "")
            payload = {**payload, "_brevitas_tenant_key": _tenant_context(request, key)[0]}
            if inspect.iscoroutinefunction(_usage_reporter):
                await _usage_reporter(key, payload)
            else:
                await asyncio.to_thread(_usage_reporter, key, payload)
            return
        # `report_usage` rebuilds the receipt from the session object and sets
        # "session_id": session.session_id, so the id this handle carries is the
        # one that reaches usage_log -- the payload's own value is discarded.
        # Pin the handle to the id the request already computed, otherwise this
        # transport silently substitutes a different session for the receipt.
        reported_session = payload.get("session_id") or ""
        session = _session_for(reported_session or "usage", reported_session)
        session.last_quality = payload.get("quality_score")
        await asyncio.to_thread(
            report_usage, payload.get("provider", ""), payload.get("model", ""),
            payload.get("baseline_tokens", 0), payload.get("compressed_tokens", 0), session,
            payload.get("pipeline", ""), payload.get("agent", ""), payload.get("run_id", ""),
            payload.get("usage_raw"), payload.get("strategy", ""), payload,
        )
    except Exception as exc:
        # Keep swallowing — a reporting fault must never alter the provider
        # response — but never silently: on the hosted path this is the only
        # write that records billable savings, so a dropped receipt is lost
        # revenue and understated customer savings. Type only: the payload
        # carries customer prompt-derived fields and must not be logged.
        logger.error("usage receipt emit failed error_type=%s", type(exc).__name__)


# ── Predictive warming observation ────────────────────────────────────────────
# The hosted API installs a sink via brevitas.warming.set_warm_observer; the proxy
# reports each markered, faithful, attributed request so a worker can keep the
# provider cache warm ahead of the customer's predicted return.
# Observation must never contend with the request path: sync observers run on a
# small dedicated executor — NOT the loop's default executor, which the hosted
# auth middleware's to_thread lookups share — and in-flight observations are
# capped. Beyond the cap new ones are shed (dropping a best-effort observation is
# always safe), so a slow store/KMS can neither delay live-traffic auth nor grow
# _warm_tasks without bound.
_WARM_OBSERVE_MAX_INFLIGHT = max(1, int(os.getenv("BREVITAS_WARM_OBSERVE_MAX_INFLIGHT", "32")))
_WARM_OBSERVE_THREADS = max(1, int(os.getenv("BREVITAS_WARM_OBSERVE_THREADS", "4")))
_warm_tasks: set[asyncio.Task] = set()
_warm_executor: ThreadPoolExecutor | None = None
_warm_executor_lock = threading.Lock()


def _get_warm_executor() -> ThreadPoolExecutor:
    global _warm_executor
    if _warm_executor is None:
        with _warm_executor_lock:
            if _warm_executor is None:
                _warm_executor = ThreadPoolExecutor(
                    max_workers=_WARM_OBSERVE_THREADS,
                    thread_name_prefix="brevitas-warm-observe")
    return _warm_executor


def _observe_warm_prefix(request: Request, body: dict, model: str, meta: dict,
                         headers: dict[str, str], cache_read: bool,
                         response_faithful: bool, provider: str = "anthropic",
                         upstream: str = "") -> None:
    """Fire-and-forget: gate cheaply on request state + meta, then extract and
    deliver off the response path. Never alters the provider response."""
    try:
        if get_warm_observer() is None:
            return
        if not getattr(request.state, "brevitas_warm_enabled", False):
            return
        organization_id = str(getattr(request.state, "brevitas_organization_id", "") or "")
        customer_id = str(getattr(request.state, "brevitas_customer_id", "") or "")
        if not organization_id or not customer_id:
            return
        if not response_faithful:
            return
        if provider == "anthropic":
            # Explicit-marker provider: no breakpoints means nothing was cached.
            if int(meta.get("cache_breakpoints", 0) or 0) <= 0:
                return
        elif not provider_warmable(provider):
            # No cache, or a discount pings can never beat: measurement only.
            # Automatic-cache providers have no breakpoint signal — extraction
            # itself (stable prefix + provider minimum) is the gate downstream.
            return
        if len(_warm_tasks) >= _WARM_OBSERVE_MAX_INFLIGHT:
            return                     # shed rather than queue behind a slow store
        task = asyncio.get_running_loop().create_task(_deliver_warm_prefix(
            organization_id, customer_id, body, model, headers, meta, cache_read,
            provider, upstream))
        _warm_tasks.add(task)
        task.add_done_callback(_warm_tasks.discard)
    except Exception:
        pass


async def _deliver_warm_prefix(organization_id: str, customer_id: str, body: dict,
                               model: str, headers: dict[str, str], meta: dict,
                               cache_read: bool, provider: str = "anthropic",
                               upstream: str = "") -> None:
    """Best effort only: observation errors never surface to the caller."""
    try:
        observer = get_warm_observer()
        if observer is None:
            return
        prefix = extract_warm_prefix(body, provider, model, headers, meta,
                                     upstream=upstream)
        if prefix is None:
            return
        if inspect.iscoroutinefunction(observer):
            await observer(organization_id, customer_id, prefix, cache_read)
        else:
            # run_in_executor does NOT copy the caller's contextvars, and the
            # hosted observer (_hosted_warm_observe) reads the request's auth
            # context from a ContextVar as its first act — without the copy every
            # hosted observation is a silent no-op. Deliberately NOT
            # asyncio.to_thread: the default executor is forbidden here.
            ctx = contextvars.copy_context()
            await asyncio.get_running_loop().run_in_executor(
                _get_warm_executor(), ctx.run, observer,
                organization_id, customer_id, prefix, cache_read)
    except Exception:
        pass


def _anthropic_cached_tokens(usage: Any) -> int:
    """Cached-read tokens from an Anthropic usage object. Upstream usage is
    untrusted JSON evaluated on the response path — never raise."""
    usage = usage if isinstance(usage, dict) else {}
    return _usage_int(usage.get("cache_read_input_tokens"))


def _openai_cached_tokens(usage: Any) -> int:
    """Cached-read tokens from an OpenAI-compatible usage object: the shared
    prompt_tokens_details.cached_tokens shape (OpenAI/xAI/OpenRouter), falling
    back to DeepSeek's prompt_cache_hit_tokens. Upstream usage is untrusted
    JSON evaluated on the response path — never raise; a malformed field
    counts as zero, same as receipts._int."""
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = _usage_int(details.get("cached_tokens"))
        if cached:
            return cached
    return _usage_int(usage.get("prompt_cache_hit_tokens"))


def _router_usage(receipt: TokenReceipt, provider: str) -> dict:
    if provider == "anthropic":
        return {"input_tokens": receipt.fresh_input_tokens,
                "cache_read_input_tokens": receipt.cached_input_tokens,
                "cache_creation_input_tokens": receipt.cache_write_tokens,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": receipt.cache_write_5m_tokens,
                    "ephemeral_1h_input_tokens": receipt.cache_write_1h_tokens,
                },
                "output_tokens": receipt.output_tokens}
    return {"prompt_tokens": receipt.input_tokens,
            "prompt_tokens_details": {
                "cached_tokens": receipt.cached_input_tokens,
                "cache_write_tokens": receipt.cache_write_tokens,
            },
            "completion_tokens": receipt.output_tokens}


async def _record_receipt(request: Request, provider: str, model: str, operation: str,
                          baseline: int, receipt: TokenReceipt, session: _SessionHandle,
                          router: _RouterHandle, labels: dict, optimized: bool,
                          response_id: str = "", strategy: str = "native_cache",
                          fleet_pipe: str = "", cache_attributable: bool = False,
                          optimized_tokens: int | None = None,
                          tenant_key: str = "", billing_provider: str = "") -> None:
    # `provider` stays the header-derived routing label — it drives the receipt
    # shape, the meter, and the router's economics. `billing_provider` is the
    # label implied by the resolved destination host and is used ONLY for the
    # emitted receipt, so pricing follows the bytes rather than the declaration.
    has_receipt = receipt.total_tokens > 0
    usage = _router_usage(receipt, provider) if has_receipt else {}
    if has_receipt:
        _meter(provider, model, usage, labels, optimized)
    if optimized and has_receipt:
        try:
            _mutate_router(
                router,
                lambda value: record_usage(
                    usage, provider, value, session.session_id,
                    pipeline=fleet_pipe, model=model, tenant_key=tenant_key),
            )
            _state_save()
        except Exception:
            pass
    receipt_fields = receipt.as_dict() if has_receipt else {}
    await _emit_usage(request, {
        "provider": billing_provider or provider, "model": model, "operation": operation,
        "baseline_tokens": baseline,
        # Both values use the same local counter and therefore carry only a
        # transformation DELTA. The API anchors the LEVEL of both legs to the
        # authoritative provider receipt (tools/system/tokenizer included) but
        # cannot change their difference, so this delta is the only savings
        # signal on the wire. `optimized_tokens is None` therefore reports
        # exactly zero savings, and every optimizing call site must pass a real
        # post-optimization count; only the passthrough call sites may omit it,
        # where zero is the correct answer.
        "compressed_tokens": baseline if optimized_tokens is None else optimized_tokens,
        **receipt_fields, "quality_score": session.last_quality,
        "cache_attributable": cache_attributable,
        "receipt_available": has_receipt,
        # Labels are splatted FIRST so server-minted fields always win. With the
        # splat last, adding any label key named request_id / receipt_source would
        # hand the caller the billing dedupe key back.
        **labels,
        "request_id": _request_id(request, response_id),
        "strategy": (strategy if has_receipt else f"{strategy}:missing_receipt")[:64],
        "session_id": session.session_id, "receipt_source": "proxy",
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
    # SSRF protection: only allow known upstream URLs.
    if override_header and override_header in _ALLOWED_UPSTREAMS:
        return override_header

    # A SUPPLIED-BUT-REJECTED override is refused, not ignored. Silently falling
    # back re-routes the request to a DIFFERENT company than the caller named --
    # and _passthrough_headers forwards `authorization` for every non-Anthropic
    # provider, so the caller's credential goes with it. A customer aiming at
    # their own Azure resource and mistyping the host would have had their Entra
    # bearer transmitted to api.openai.com, with a 200 back and nothing to see.
    #
    # Ignoring an override is only safe when an override cannot carry a secret
    # somewhere new. It can.
    if override_header:
        raise HTTPException(
            status_code=400,
            detail=("x-brevitas-upstream is not an allowed upstream. Refusing "
                    "rather than routing this request, and the credential it "
                    "carries, to a different provider."),
        )

    return _UPSTREAMS[_provider_for(model, provider)]

def _clean_label(value: str, limit: int) -> str:
    """Coerce a tracking label to something UsageReportRequest always accepts."""
    text = str(value or "")
    # Mirrors the API's _safe_metadata rule, which REJECTS control characters
    # rather than stripping them — a rejection here would drop the whole receipt.
    text = "".join(char for char in text if ord(char) >= 32 and ord(char) != 127)
    return text[:limit]


def parse_brevitas_headers(headers: dict) -> dict[str, str]:
    """Extract brevitas tracking labels from request headers (x-brevitas-pipeline/agent/run-id).
    Returns dict with 'pipeline', 'agent', 'run_id' keys (empty strings if not present).

    Every value is sanitized to what UsageReportRequest accepts. These labels are
    cosmetic, but they ride the receipt payload: the API validates the whole
    payload in one pass, so a label the schema rejects — over its max_length, or
    carrying a control character — raises before the row is written and the
    receipt is silently dropped. That made a caller header a metering kill switch
    (unmetered use of a paid product), and misfired by accident on any customer
    whose pipeline name ran long. Clamp here so a label can never decide whether
    a call is billed; the API coerces again on the far side.
    """
    def _get(name: str, limit: int = 128) -> str:
        try:
            value = headers.get(name, "") or ""
        except AttributeError:
            return ""
        return _clean_label(value, limit)
    project = (_get("x-brevitas-project") or _get("x-brevitas-repo")
               or _clean_label(os.getenv("BREVITAS_PROJECT", ""), 128)
               or _clean_label(os.getenv("BREVITAS_REPO", ""), 128)
               or _clean_label(_git_root_name(), 128))
    source = (_get("x-brevitas-source") or _get("x-brevitas-client")
              or _clean_label(os.getenv("BREVITAS_SOURCE", ""), 128)
              or _clean_label(os.getenv("BREVITAS_CLIENT", ""), 128)
              or "proxy")
    return {
        "project": project, "repo": project,
        "environment": (_get("x-brevitas-environment", 64)
                        or _clean_label(os.getenv("BREVITAS_ENVIRONMENT", ""), 64)),
        "source": source, "client": source,
        "pipeline": _get("x-brevitas-pipeline"), "agent": _get("x-brevitas-agent"),
        "call_site_id": _get("x-brevitas-call-site"),
        "framework": _get("x-brevitas-framework", 64),
        "gateway": _get("x-brevitas-gateway", 64),
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
        # Anthropic accepts both x-api-key and Authorization: Bearer (used by
        # Claude Code OAuth/WIF flows). Preserve whichever credential the caller sent.
        for h in ("x-api-key", "authorization", "anthropic-version", "anthropic-beta"):
            val = request.headers.get(h)
            if val:
                headers[h] = val
        if "anthropic-version" not in headers:
            headers["anthropic-version"] = "2023-06-01"
    else:
        forward = {"authorization", "openai-organization", "openai-project",
                   "openai-beta", "idempotency-key", "http-referer", "x-title",
                   "x-client-request-id",
                   # Azure OpenAI's native credential header. AzureOpenAI(
                   # api_key=...) sends `api-key`, not Authorization, so without
                   # this an Azure caller's request reaches the upstream with no
                   # credential at all and 401s -- and a failed upstream writes
                   # no receipt, so the traffic is invisible as well as broken.
                   # Entra/managed-identity callers send Authorization instead
                   # and were already covered.
                   "api-key",
                   # xAI replica-affinity hint; a caller-supplied value always
                   # beats Brevitas's injected one (_apply_xai_affinity).
                   "x-grok-conv-id"}
        for name, value in request.headers.items():
            lower = name.lower()
            if lower in forward or lower.startswith("x-stainless-"):
                headers[name] = value
    return headers


# ── Anthropic: POST /v1/messages ──────────────────────────────────────────────

@proxy_app.post("/v1/messages")
async def proxy_anthropic_messages(request: Request) -> Any:
    _, body = await _json_object(request)
    model: str = body.get("model", "")
    api_key = request.headers.get("x-api-key", "")
    provider_auth = api_key or request.headers.get("authorization", "")
    labels = parse_brevitas_headers(request.headers)
    baseline = count_request_tokens(body, "messages")
    brevitas_key = request.headers.get("x-brevitas-key", "")
    gate_key, state_key = _tenant_context(request, provider_auth)
    # Key state by tenant + provider + exact model + operation + agent so a router (whose
    # provider/economics are fixed at construction) never mixes providers or models.
    sess_key = f"ant:{state_key}:anthropic:{model}:messages:{_agent_label(labels, body)}"
    session = _session_for(sess_key)
    router = _router_for(sess_key, "anthropic")

    cache = _cache_for_request(request)
    cache_body = (_cache_body(body, request, brevitas_key, provider_auth)
                  if cache is not None else None)
    if cache is not None and lever_allowed("cache", gate_key):
        hit = _cache_lookup(cache, cache_body, "anthropic", model, gate_key)
        if hit is not None:
            await _report_cache_hit(request, "anthropic", model, hit, session, labels)
            session.advance()
            return JSONResponse(content=hit.response, status_code=200)

    optimized = not _passthrough_mode()
    fleet_pipe, fleet_agent = _auto_fleet_labels(labels, provider_auth, body)
    strategy = "passthrough"
    cache_attributable = False
    meta: dict[str, Any] = {}
    # Faithful means the request we forward is byte-faithful to the original, so its
    # response is valid to cache under the original key. Retrieval/reorder set it False.
    response_faithful = True
    if optimized:
        meta = _optimize_fail_open(body, "anthropic", router, session.session_id,
                                   pipeline=fleet_pipe, agent=fleet_agent, tenant_key=gate_key)
        strategy = meta.get("strategy", "native_cache")
        cache_attributable = meta.get("cache_control_owner") == "brevitas"
        response_faithful = bool(meta.get("response_faithful", True))
    optimized_tokens = count_request_tokens(body, "messages")

    bg_sig, bg_role = None, "free"
    if optimized and _BG_ON:
        bg_sig = _bg.signature(body, namespace=gate_key)
        if bg_sig:
            bg_role, _bg_waited = await _bg.acquire(bg_sig)

    headers = _passthrough_headers(request, "anthropic")
    is_stream = body.get("stream", False)
    endpoint = f"{_ANTHROPIC_API}/v1/messages"
    if is_stream:
        try:
            upstream = await _provider_request(
                "anthropic", "messages", "POST", endpoint, headers=headers,
                stream=True, json_body=body
            )
        except BaseException:
            if bg_role == "pathfinder":
                _bg.release(bg_sig, _BG_WARM["anthropic"])
            raise
        if not _upstream_ok(upstream):
            content = await upstream.aread()
            response_headers = _response_headers(upstream)
            await upstream.aclose()
            if bg_role == "pathfinder":
                _bg.release(bg_sig, _BG_WARM["anthropic"])
            return Response(content=content, status_code=upstream.status_code,
                            headers=response_headers)
        parser = SSEUsageParser("anthropic")

        async def stream_gen():
            released = False
            completed = False
            try:
                async for chunk in provider_http.iter_bytes("anthropic", upstream):
                    if _admission_canceled(request):
                        break
                    parser.feed(chunk)
                    if not released and bg_role == "pathfinder":
                        _bg.release(bg_sig, _BG_WARM["anthropic"])
                        released = True
                    if not _admission_canceled(request):
                        yield chunk
                completed = not _admission_canceled(request)
            finally:
                await upstream.aclose()
                if bg_role == "pathfinder" and not released:
                    _bg.release(bg_sig, _BG_WARM["anthropic"])
                if completed:
                    receipt = parser.finish()
                    _observe_warm_prefix(request, body, model, meta, headers,
                                         cache_read=receipt.cached_input_tokens > 0,
                                         response_faithful=response_faithful)
                    await _record_receipt(
                        request, "anthropic", model, "messages", baseline, receipt,
                        session, router, labels, optimized, parser.response_id, strategy, fleet_pipe,
                        cache_attributable=cache_attributable,
                        optimized_tokens=optimized_tokens,
                        tenant_key=gate_key,
                    )
                    session.advance()

        return StreamingResponse(stream_gen(), status_code=upstream.status_code,
                                 headers=_response_headers(upstream), media_type="text/event-stream")

    try:
        upstream = await _provider_request(
            "anthropic", "messages", "POST", endpoint, headers=headers, json_body=body
        )
    finally:
        if bg_role == "pathfinder":
            _bg.release(bg_sig, _BG_WARM["anthropic"])
    try:
        data = upstream.json()
    except Exception:
        data = {}
    if _upstream_ok(upstream):
        try:
            session.record_response(data["content"][0]["text"])
        except (KeyError, IndexError, TypeError):
            pass
        # Only cache when the forwarded request was byte-faithful to the original —
        # never store an answer produced from retrieval-pruned or reordered context.
        if cache is not None and data and response_faithful:
            _cache_store(cache, cache_body, "anthropic", model, data,
                         _request_id(request, str(data.get("id") or "")))
        _observe_warm_prefix(
            request, body, model, meta, headers,
            cache_read=_anthropic_cached_tokens(data.get("usage")) > 0,
            response_faithful=response_faithful)
        await _record_receipt(
            request, "anthropic", model, "messages", baseline,
            normalize_usage(data.get("usage"), "anthropic"), session, router, labels,
            optimized, str(data.get("id") or ""), strategy, fleet_pipe,
            cache_attributable=cache_attributable,
            optimized_tokens=optimized_tokens,
            tenant_key=gate_key,
        )
        session.advance()
    return Response(content=_response_content(upstream, data), status_code=upstream.status_code,
                    headers=_response_headers(upstream))


# ── OpenAI: POST /v1/chat/completions ────────────────────────────────────────

@proxy_app.post("/openai/v1/chat/completions")
@proxy_app.post("/openai/chat/completions")
@proxy_app.post("/v1/chat/completions")
async def proxy_openai_chat(request: Request) -> Any:
    _, body = await _json_object(request)
    model: str = body.get("model", "")
    auth = request.headers.get("authorization", "")
    provider = _provider_for(model, _explicit_provider(request))
    labels = parse_brevitas_headers(request.headers)
    labels["gateway"] = labels.get("gateway") or (provider if provider == "openrouter" else "")
    baseline = count_request_tokens(body, "chat.completions")
    brevitas_key = request.headers.get("x-brevitas-key", "")
    gate_key, state_key = _tenant_context(request, auth)
    # Key by tenant + provider + exact model + operation + agent (see anthropic handler):
    # this is what stops DeepSeek/OpenAI economics from mixing in one shared-key fleet.
    sess_key = f"oai:{state_key}:{provider}:{model}:chat.completions:{_agent_label(labels, body)}"
    session = _session_for(sess_key)
    router = _router_for(sess_key, provider)

    # Resolve the destination before anything that reports a receipt: a cache hit
    # returns without ever touching an upstream, and the billed label has to
    # follow the host on that path too (see _report_cache_hit).
    override_upstream = request.headers.get("x-brevitas-upstream")
    upstream_base = get_openai_compatible_upstream(model, override_upstream, provider)
    billing_provider = _provider_for_host(upstream_base) or provider

    # Semantic cache: key on the ORIGINAL request; model_id already isolates per model.
    cache = _cache_for_request(request)
    cache_body = _cache_body(body, request, brevitas_key, auth) if cache is not None else None
    if cache is not None and lever_allowed("cache", gate_key):
        hit = _cache_lookup(cache, cache_body, provider, model, gate_key)
        if hit is not None:
            await _report_cache_hit(request, provider, model, hit, session, labels,
                                    billing_provider)
            session.advance()
            return JSONResponse(content=hit.response, status_code=200)

    # Lossless auto-route. For OpenAI/DeepSeek the cache_only path forwards the prefix
    # byte-identical (auto-cached server-side); retrieve reduces context when the router
    # estimates it's cheaper. Volatile message never lossily rewritten; fail-safe to full.
    optimized = not _passthrough_mode()
    fleet_pipe, fleet_agent = _auto_fleet_labels(labels, auth, body)
    strategy = "passthrough"
    response_faithful = True
    cache_attributable = False
    meta: dict[str, Any] = {}
    if optimized:
        meta = _optimize_fail_open(body, provider, router, session.session_id,
                                   pipeline=fleet_pipe, agent=fleet_agent, tenant_key=gate_key)
        strategy = meta.get("strategy", "native_cache")
        response_faithful = bool(meta.get("response_faithful", True))
        cache_attributable = bool(meta.get("cache_attributable", False))
    optimized_tokens = count_request_tokens(body, "chat.completions")

    # pathfinder gate — signature computed AFTER optimization (the bytes actually sent)
    bg_sig, bg_role = None, "free"
    if optimized and _BG_ON:
        bg_sig = _bg.signature(body, namespace=gate_key)
        if bg_sig:
            bg_role, _bg_waited = await _bg.acquire(bg_sig)
    bg_warm = _BG_WARM.get(provider, 240.0)

    headers = _passthrough_headers(request, "openai")
    is_stream = body.get("stream", False)
    _inject_stream_usage(body, provider)
    if _apply_xai_affinity(headers, request, provider, state_key, upstream_base):
        # Without the injected affinity xAI's cache never hits, so any
        # discount on this request exists because Brevitas caused it.
        cache_attributable = True

    endpoint = _CHAT_ENDPOINTS.get(provider, f"{upstream_base.rstrip('/')}/v1/chat/completions")
    if override_upstream:
        endpoint = f"{upstream_base.rstrip('/')}/v1/chat/completions"
    if is_stream:
        try:
            upstream = await _provider_request(
                provider, "chat.completions", "POST", endpoint, headers=headers,
                stream=True, json_body=body
            )
        except BaseException:
            if bg_role == "pathfinder":
                _bg.release(bg_sig, bg_warm)
            raise
        if not _upstream_ok(upstream):
            content = await upstream.aread()
            response_headers = _response_headers(upstream)
            await upstream.aclose()
            if bg_role == "pathfinder":
                _bg.release(bg_sig, bg_warm)
            return Response(content=content, status_code=upstream.status_code,
                            headers=response_headers)
        parser = SSEUsageParser(provider)

        async def stream_gen():
            released = False
            completed = False
            try:
                async for chunk in provider_http.iter_bytes(provider, upstream):
                    if _admission_canceled(request):
                        break
                    parser.feed(chunk)
                    if not released and bg_role == "pathfinder":
                        _bg.release(bg_sig, bg_warm)
                        released = True
                    if not _admission_canceled(request):
                        yield chunk
                completed = not _admission_canceled(request)
            finally:
                await upstream.aclose()
                if bg_role == "pathfinder" and not released:
                    _bg.release(bg_sig, bg_warm)
                if completed:
                    receipt = parser.finish()
                    _observe_warm_prefix(request, body, model, meta, headers,
                                         cache_read=receipt.cached_input_tokens > 0,
                                         response_faithful=response_faithful,
                                         provider=provider, upstream=endpoint)
                    await _record_receipt(
                        request, provider, model, "chat.completions", baseline, receipt,
                        session, router, labels, optimized, parser.response_id, strategy, fleet_pipe,
                        cache_attributable=cache_attributable,
                        optimized_tokens=optimized_tokens,
                        tenant_key=gate_key, billing_provider=billing_provider,
                    )
                    session.advance()

        return StreamingResponse(stream_gen(), status_code=upstream.status_code,
                                 headers=_response_headers(upstream), media_type="text/event-stream")

    try:
        upstream = await _provider_request(
            provider, "chat.completions", "POST", endpoint,
            headers=headers, json_body=body
        )
    finally:
        if bg_role == "pathfinder":
            _bg.release(bg_sig, bg_warm)
    try:
        data = upstream.json()
    except Exception:
        data = {}
    if _upstream_ok(upstream):
        try:
            session.record_response(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            pass
        # Only cache when the forwarded request was byte-faithful to the original —
        # never store an answer produced from retrieval-pruned or reordered context.
        if cache is not None and data and response_faithful:
            # Mint the metering id HERE, with the same provider response id
            # _record_receipt passes below. _request_id caches on request.state,
            # so the id written onto the cache entry and the id on this request's
            # paid receipt are the same string by construction.
            #
            # It is a forward reference: the receipt is written a few lines down
            # and can still fail. That is safe in the only direction that
            # matters — a replay anchored to a receipt that never landed simply
            # finds no paid ancestor to join to, and is treated as unanchored,
            # i.e. unbillable. The anchor can promise provenance, never invent it.
            _cache_store(cache, cache_body, provider, model, data,
                         _request_id(request, str(data.get("id") or "")))
        _observe_warm_prefix(
            request, body, model, meta, headers,
            cache_read=_openai_cached_tokens(data.get("usage")) > 0,
            response_faithful=response_faithful, provider=provider, upstream=endpoint)
        await _record_receipt(
            request, provider, model, "chat.completions", baseline,
            normalize_usage(data.get("usage"), provider), session, router, labels,
            optimized, str(data.get("id") or ""), strategy, fleet_pipe,
            cache_attributable=cache_attributable,
            optimized_tokens=optimized_tokens,
            tenant_key=gate_key, billing_provider=billing_provider,
        )
        session.advance()
    return Response(content=_response_content(upstream, data), status_code=upstream.status_code,
                    headers=_response_headers(upstream))


# ── OpenAI Responses API (Codex and compatible clients) ─────────────────────

@proxy_app.post("/openai/v1/responses")
@proxy_app.post("/openai/responses")
@proxy_app.post("/v1/responses")
async def proxy_openai_responses(request: Request) -> Any:
    raw_body, body = await _json_object(request)
    model = str(body.get("model") or "")
    provider = _provider_for(model, _explicit_provider(request))
    labels = parse_brevitas_headers(request.headers)
    baseline = count_request_tokens(body, "responses")
    auth = request.headers.get("authorization", "")
    gate_key, state_key = _tenant_context(request, auth)
    sess_key = f"responses:{state_key}:{provider}:{model}:responses:{_agent_label(labels, body)}"
    session = _session_for(sess_key)
    router = _router_for(sess_key, provider)
    optimized = False
    body_changed = False
    strategy = "passthrough"
    cache_attributable = False

    response_input = body.get("input")
    if not _passthrough_mode() and isinstance(response_input, list) and all(
        isinstance(item, dict) and "role" in item for item in response_input
    ):
        temporary = {"model": model, "messages": deepcopy(response_input),
                     "_brevitas_operation": "responses"}
        if body.get("instructions"):
            temporary["system"] = body["instructions"]
        meta = _optimize_fail_open(temporary, provider, router, session.session_id,
                                   pipeline=labels.get("pipeline", ""),
                                   agent=labels.get("agent", ""), tenant_key=gate_key)
        body["input"] = temporary["messages"]
        for cache_field in ("prompt_cache_key", "prompt_cache_options"):
            if cache_field in temporary:
                body[cache_field] = temporary[cache_field]
        body_changed = body["input"] != response_input
        optimized = body_changed
        strategy = meta.get("strategy", "native_cache")
        cache_attributable = bool(meta.get("cache_attributable", False))
    optimized_tokens = count_request_tokens(body, "responses")

    base = get_openai_compatible_upstream(
        model, request.headers.get("x-brevitas-upstream"), provider
    )
    billing_provider = _provider_for_host(base) or provider
    endpoint = f"{base.rstrip('/')}/v1/responses"
    headers = _passthrough_headers(request, "openai")
    # Header-only injection here: the body may be forwarded byte-verbatim
    # below, and the Responses API reports usage on response.completed
    # natively, so no stream_options mutation is needed or wanted.
    if _apply_xai_affinity(headers, request, provider, state_key, base):
        cache_attributable = True
    is_stream = bool(body.get("stream"))
    request_content = None if body_changed else raw_body
    request_json = body if body_changed else None
    if is_stream:
        upstream = await _provider_request(
            provider, "responses", "POST", endpoint, headers=headers, stream=True,
            json_body=request_json, content=request_content,
        )
        if not _upstream_ok(upstream):
            content = await upstream.aread()
            response_headers = _response_headers(upstream)
            await upstream.aclose()
            return Response(content=content, status_code=upstream.status_code,
                            headers=response_headers)
        parser = SSEUsageParser(provider)

        async def stream_gen():
            completed = False
            try:
                async for chunk in provider_http.iter_bytes(provider, upstream):
                    if _admission_canceled(request):
                        break
                    parser.feed(chunk)
                    if not _admission_canceled(request):
                        yield chunk
                completed = not _admission_canceled(request)
            finally:
                await upstream.aclose()
                if completed:
                    await _record_receipt(
                        request, provider, model, "responses", baseline, parser.finish(),
                        session, router, labels, optimized, parser.response_id, strategy,
                        labels.get("pipeline", ""),
                        cache_attributable=cache_attributable,
                        optimized_tokens=optimized_tokens,
                        tenant_key=gate_key, billing_provider=billing_provider,
                    )
                    session.advance()

        return StreamingResponse(stream_gen(), status_code=upstream.status_code,
                                 headers=_response_headers(upstream), media_type="text/event-stream")

    upstream = await _provider_request(
        provider, "responses", "POST", endpoint, headers=headers,
        json_body=request_json, content=request_content,
    )
    try:
        data = upstream.json()
    except Exception:
        data = {}
    if _upstream_ok(upstream):
        await _record_receipt(
            request, provider, model, "responses", baseline,
            normalize_usage(data.get("usage"), provider), session, router, labels,
            optimized, str(data.get("id") or ""), strategy, labels.get("pipeline", ""),
            cache_attributable=cache_attributable,
            optimized_tokens=optimized_tokens,
            tenant_key=gate_key, billing_provider=billing_provider,
        )
        session.advance()
    return Response(content=_response_content(upstream, data), status_code=upstream.status_code,
                    headers=_response_headers(upstream))


async def _proxy_openai_plain(request: Request, operation: str) -> Any:
    """Meter OpenAI-compatible endpoints that do not need message optimization."""
    _, body = await _json_object(request)
    model = str(body.get("model") or "")
    provider = _provider_for(model, _explicit_provider(request))
    labels = parse_brevitas_headers(request.headers)
    baseline = count_request_tokens(body, operation)
    gate_key, state_key = _tenant_context(
        request, request.headers.get("authorization", ""))
    sess_key = f"{operation}:{state_key}:{provider}:{model}"
    session, router = _session_for(sess_key), _router_for(sess_key, provider)
    base = get_openai_compatible_upstream(
        model, request.headers.get("x-brevitas-upstream"), provider
    )
    billing_provider = _provider_for_host(base) or provider
    endpoint = f"{base.rstrip('/')}/v1/{operation}"
    headers = _passthrough_headers(request, "openai")
    if body.get("stream"):
        upstream = await _provider_request(
            provider, operation, "POST", endpoint,
            headers=headers, stream=True, json_body=body
        )
        if not _upstream_ok(upstream):
            content = await upstream.aread()
            response_headers = _response_headers(upstream)
            await upstream.aclose()
            return Response(content=content, status_code=upstream.status_code,
                            headers=response_headers)
        parser = SSEUsageParser(provider)

        async def stream_gen():
            completed = False
            try:
                async for chunk in provider_http.iter_bytes(provider, upstream):
                    if _admission_canceled(request):
                        break
                    parser.feed(chunk)
                    if not _admission_canceled(request):
                        yield chunk
                completed = not _admission_canceled(request)
            finally:
                await upstream.aclose()
                if completed:
                    await _record_receipt(request, provider, model, operation, baseline,
                        parser.finish(), session, router, labels, False, parser.response_id,
                        "passthrough", labels.get("pipeline", ""), tenant_key=gate_key,
                        billing_provider=billing_provider)
                    session.advance()
        return StreamingResponse(stream_gen(), status_code=upstream.status_code,
                                 headers=_response_headers(upstream), media_type="text/event-stream")
    upstream = await _provider_request(
        provider, operation, "POST", endpoint, headers=headers, json_body=body
    )
    try:
        data = upstream.json()
    except Exception:
        data = {}
    if _upstream_ok(upstream):
        await _record_receipt(
            request, provider, model, operation, baseline,
            normalize_usage(data.get("usage"), provider), session, router, labels,
            False, str(data.get("id") or ""), "passthrough", labels.get("pipeline", ""),
            tenant_key=gate_key, billing_provider=billing_provider,
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
