"""Durable Brevitas job worker.

Run separately from the API replicas:

    python -m api.worker

The database lease is the recovery mechanism. Killing this process leaves the
job reclaimable after BREVITAS_JOB_LEASE_SECONDS.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .distributed_limits import LimitIdentity, LimiterUnavailable
from .server import (
    AuthContext,
    _compress_pipeline,
    _decrypt,
    _distributed_limiter,
    _job_service,
    _provider_call,
    _resolve_configured_model_backend,
    _run_configured_model,
    _safe_record_usage,
    _store,
    _wait_for_provider_calls,
    _initialize_credential_cipher,
    _kms_dependency_ready,
    _kms_readiness_status,
    _authoritative_service_key_context,
    _configure_managed_kms_from_deployment,
    _production_runtime,
)
from .billing_recovery import (
    billing_recovery_is_configured,
    billing_worker_owner,
    build_billing_recovery_processor_from_env,
    run_billing_recovery_loop,
)
from .billing_settlement_sweep import (
    SWEEP_ENABLED_ENV,
    billing_settlement_sweep_is_configured,
    billing_settlement_sweep_is_enabled,
    build_settlement_sweep_from_env,
    run_billing_settlement_sweep_loop,
)
from .build_info import build_identity, validate_production_build_identity
from .jobs import PermanentJobError
from .observability import (
    BillingTelemetryAdapter,
    graceful_observability_shutdown,
    observe_job,
)
from brevitas.observability import StructuredLogger, configure_json_logging
from brevitas.provider_reliability import (
    ProviderCircuitOpen,
    close_provider_sync_clients,
    provider_sync_http,
)
from brevitas.receipts import calculate_costs, model_price, normalize_usage
from token_efficiency_model.lossless.provider_cache import count_tokens

configure_json_logging(
    service="worker",
    logger_names=("brevitas.worker", "brevitas.billing_recovery"),
)
logger = StructuredLogger("brevitas.worker")
_WORKER_ACCEPTING = False
_BILLING_ROLE = "optional"
_BILLING_REQUIRED = False
_BILLING_CONFIGURED = False
_BILLING_LOOP_RUNNING = False
_BILLING_HEALTH_LOCK = threading.Lock()
_BILLING_HEALTH: dict[str, Any] = {
    "running": False,
    "initial_validation_succeeded": False,
    "catalog_valid": False,
    "last_success_monotonic": 0.0,
    "consecutive_errors": 0,
    "last_error_monotonic": 0.0,
}

health_app = FastAPI(title="Brevitas Worker Health", docs_url=None, redoc_url=None)


def _billing_worker_role() -> str:
    configured = os.getenv("BREVITAS_WORKER_BILLING_ROLE", "").strip().lower()
    role = configured or ("authoritative" if _production_runtime() else "optional")
    if role not in {"authoritative", "nonbilling", "optional"}:
        raise RuntimeError("BREVITAS_WORKER_BILLING_ROLE is invalid")
    if _production_runtime() and role == "optional":
        raise RuntimeError("production billing role must be authoritative or nonbilling")
    return role


def _billing_readiness_bound(name: str, default: float, minimum: float,
                             maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return min(maximum, max(minimum, value))


def _report_billing_health(snapshot: Mapping[str, Any] | object) -> None:
    """Accept W4's content-free loop snapshot without trusting arbitrary fields."""
    def field(name: str, default: Any) -> Any:
        if isinstance(snapshot, Mapping):
            return snapshot.get(name, default)
        return getattr(snapshot, name, default)

    now = time.monotonic()
    try:
        last_success = float(field("last_success_monotonic", 0.0) or 0.0)
    except (TypeError, ValueError):
        last_success = 0.0
    try:
        last_error = float(field("last_error_monotonic", 0.0) or 0.0)
    except (TypeError, ValueError):
        last_error = 0.0
    try:
        errors = max(0, int(field("consecutive_errors", 0) or 0))
    except (TypeError, ValueError):
        errors = 0
    # Monotonic timestamps are process-local. Reject non-finite/future values
    # instead of allowing a malformed callback to hold readiness open forever.
    if not math.isfinite(last_success) or last_success > now + 1:
        last_success = 0.0
    if not math.isfinite(last_error) or last_error > now + 1:
        last_error = 0.0
    sanitized = {
        "running": field("running", False) is True,
        "initial_validation_succeeded": (
            field("initial_validation_succeeded", False) is True),
        "catalog_valid": field("catalog_valid", False) is True,
        "last_success_monotonic": max(0.0, last_success),
        "consecutive_errors": min(errors, 1_000_000),
        "last_error_monotonic": max(0.0, last_error),
    }
    with _BILLING_HEALTH_LOCK:
        _BILLING_HEALTH.update(sanitized)


def _billing_health_status() -> tuple[bool, dict[str, Any]]:
    with _BILLING_HEALTH_LOCK:
        snapshot = dict(_BILLING_HEALTH)
    now = time.monotonic()
    stale_after = _billing_readiness_bound(
        "BREVITAS_BILLING_READINESS_STALE_SECONDS", 120.0, 5.0, 3600.0)
    error_threshold = int(_billing_readiness_bound(
        "BREVITAS_BILLING_READINESS_ERROR_THRESHOLD", 3, 1, 100))
    last_success = float(snapshot["last_success_monotonic"])
    success_age = max(0.0, now - last_success) if last_success > 0 else None
    success_fresh = success_age is not None and success_age <= stale_after
    errors_exceeded = int(snapshot["consecutive_errors"]) >= error_threshold
    ready = (
        _BILLING_CONFIGURED
        and _BILLING_LOOP_RUNNING
        and snapshot["running"] is True
        and snapshot["initial_validation_succeeded"] is True
        and snapshot["catalog_valid"] is True
        and success_fresh
        and not errors_exceeded
    )
    public = {
        "running": snapshot["running"] is True,
        "initial_validation_succeeded": (
            snapshot["initial_validation_succeeded"] is True),
        "catalog_valid": snapshot["catalog_valid"] is True,
        "last_success_fresh": success_fresh,
        "last_success_age_seconds": (
            round(success_age, 3) if success_age is not None else None),
        "consecutive_errors": int(snapshot["consecutive_errors"]),
        "error_threshold_exceeded": errors_exceeded,
    }
    return ready, public


def _billing_recovery_block() -> tuple[str, dict[str, Any]]:
    """Honest billing-loop status for /ready, whatever this worker's role is.

    The health payload is always the real BillingLoopHealth snapshot. A
    non-required role used to hardcode ready with a fabricated all-false health
    block, so a dead loop on an `optional` worker reported "ready" forever
    (WR-1). Billing recovery stays non-authoritative for durable job acceptance
    either way; only this diagnostic block changes.
    """
    healthy, health = _billing_health_status()
    if _BILLING_ROLE == "nonbilling" or not _BILLING_CONFIGURED:
        # No loop is supposed to exist here, so there is nothing to call
        # degraded: report it as deliberately off rather than as a dead loop.
        return "disabled", health
    return ("ready" if healthy else "unavailable"), health


def _billing_loop_ready() -> bool:
    """Whether billing recovery is healthy or deliberately disabled.

    Never gates durable job acceptance; it answers the same question /ready
    surfaces, from the same snapshot, for callers that want one boolean.
    """
    return _billing_recovery_block()[0] != "unavailable"


async def _dependencies_ready() -> tuple[bool, bool]:
    timeout = max(0.1, float(os.getenv("BREVITAS_HEALTH_TIMEOUT_SECONDS", "3")))
    try:
        database_ready = await asyncio.wait_for(
            asyncio.to_thread(_store.healthy), timeout=timeout,
        )
    except (Exception, asyncio.TimeoutError):
        database_ready = False
    try:
        redis_ready = await asyncio.wait_for(
            _distributed_limiter.healthy(), timeout=timeout,
        )
    except (Exception, asyncio.TimeoutError):
        redis_ready = False
    return bool(database_ready), bool(redis_ready)


@health_app.get("/live")
async def liveness():
    return {"status": "ok"}


@health_app.get("/version")
async def version():
    return {"service": "worker", "build": build_identity(required=_production_runtime())}


@health_app.get("/ready")
@health_app.get("/health")
async def readiness():
    database_ready, redis_ready = await _dependencies_ready()
    kms = await _kms_readiness_status()
    kms_ready = _kms_dependency_ready(kms)
    billing_status, billing_health = _billing_recovery_block()
    ready = (
        _WORKER_ACCEPTING
        and database_ready
        and redis_ready
        and kms_ready
    )
    payload = {
        "status": "ok" if ready else "unavailable",
        "accepting_jobs": _WORKER_ACCEPTING,
        "dependencies": {
            "postgres": {"status": "ready" if database_ready else "unavailable",
                         "authoritative": True},
            "redis": {"status": "ready" if redis_ready else "unavailable",
                      "authoritative": False, "role": "coordination"},
            "kms": {
                "status": (
                    "disabled" if not kms["configured"] else
                    "ready" if kms_ready else "unavailable"
                ),
                **kms,
            },
            "billing_recovery": {
                # Derived from the real loop snapshot for every role: a dead
                # loop must never advertise "ready" (WR-1).
                "status": billing_status,
                # Non-authoritative for job acceptance: billing degradation is
                # surfaced here but never gates durable job consumption.
                "authoritative": False,
                "billing_required": _BILLING_REQUIRED,
                "configured": _BILLING_CONFIGURED,
                "running": _BILLING_LOOP_RUNNING,
                "role": _BILLING_ROLE,
                "health": billing_health,
            },
        },
    }
    return payload if ready else JSONResponse(payload, status_code=503)


async def _process_job(payload: dict, row: dict) -> dict:
    key_context = await asyncio.to_thread(
        _authoritative_service_key_context, row["key_hash"])
    if not key_context:
        raise PermanentJobError("service_key_revoked")
    config = None
    backend = None
    provider = "compressor" if payload.get("operation") == "compress" else "all"
    if payload.get("operation") != "compress":
        config, backend = await asyncio.to_thread(
            _resolve_configured_model_backend, row["key_hash"])
        if not config:
            raise PermanentJobError("provider_not_configured")
        provider = str(config.get("provider") or "all")
    token_cost = max(1, sum(count_tokens(value) for value in (
        [payload.get("task", ""), *payload.get("messages", []), *payload.get("context", [])]
    )))
    lease = await _distributed_limiter.acquire(
        LimitIdentity(row["organization_id"], row["customer_id"], row["key_hash"], provider),
        tokens=token_cost,
        request_id=f"job_{row['id'].replace('-', '')}",
    )
    if not lease.allowed:
        raise RuntimeError("provider_capacity")
    try:
        if payload.get("operation") == "compress":
            result = await asyncio.to_thread(
                _compress_pipeline,
                payload.get("task", ""), payload.get("messages", []),
                payload.get("context", []), 8, False,
            )
            output = {
                "compressed_messages": result["out_messages"],
                "selected_context": result["selected_context"],
                "baseline_tokens": result["baseline_tokens"],
                "optimized_tokens": result["optimized_tokens"],
                "_job_metadata": {"provider": "brevitas", "model": "compression"},
            }
            await asyncio.to_thread(
                _safe_record_usage,
                auth_context=AuthContext(
                    key_hash=row["key_hash"], organization_id=row["organization_id"],
                    billing_owner_id=str(key_context.get("owner_id") or ""),
                    customer_id=row["customer_id"], key_type=str(key_context.get("key_type") or ""),
                ),
                key_hash=row["key_hash"], baseline_tokens=result["baseline_tokens"],
                optimized_tokens=result["optimized_tokens"], savings_pct=result["savings_pct"],
                quality_proxy=None, strategy="job:compress", receipt_source="worker",
                request_id=f"job:{row['id']}", provider="brevitas", model="compression",
            )
            return output
        # This ownership-fenced marker is the final durable boundary before a
        # potentially billable provider POST. A reclaimed marked job is never
        # sent automatically because supported providers offer no verified
        # idempotency or result reconciliation contract.
        await _job_service.mark_provider_outbound_started(row)
        output = await asyncio.to_thread(
            _run_configured_model,
            row["key_hash"], payload.get("messages", []),
            payload.get("context", []), payload.get("task", ""),
            resolved_config=config, resolved_backend=backend,
        )
        await asyncio.to_thread(
            _safe_record_usage,
            auth_context=AuthContext(
                key_hash=row["key_hash"], organization_id=row["organization_id"],
                billing_owner_id=str(key_context.get("owner_id") or ""),
                customer_id=row["customer_id"], key_type=str(key_context.get("key_type") or ""),
            ),
            key_hash=row["key_hash"], baseline_tokens=token_cost,
            optimized_tokens=token_cost, savings_pct=0, quality_proxy=None,
            strategy="job:chat", receipt_source="worker", request_id=f"job:{row['id']}",
            provider=str(output.get("provider") or provider), model=str(output.get("model") or ""),
        )
        output["_job_metadata"] = {
            "provider": str(output.get("provider") or provider),
            "model": str(output.get("model") or ""),
        }
        return output
    finally:
        try:
            await lease.release()
        except LimiterUnavailable:
            pass


async def process(payload: dict, row: dict) -> dict:
    with observe_job(str(row.get("id") or ""), str(payload.get("operation") or "unknown")):
        return await _process_job(payload, row)


def _warming_enabled() -> bool:
    return os.getenv("BREVITAS_WARMING", "false").lower() in ("1", "true", "yes")


def _warm_bound(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return min(maximum, max(minimum, value))


def _warm_anthropic_body(payload: dict) -> dict:
    """Anthropic keep-alive: replay the stored prefix byte-identical (markers and
    per-marker ttl ride inside the captured blocks) plus one minimal user turn."""
    # No sampling params: current Anthropic models reject any temperature with
    # a 400, which _warm_one maps to a permanent prefix stop.
    body: dict[str, Any] = {
        "model": str(payload.get("model") or ""),
        "max_tokens": 1,
        "metadata": {"user_id": "brevitas-cache-warm"},
        "messages": [*(payload.get("messages_prefix") or []),
                     {"role": "user", "content": [{"type": "text", "text": "."}]}],
    }
    if payload.get("tools"):
        body["tools"] = payload["tools"]
    if payload.get("system") is not None:
        body["system"] = payload["system"]
    return body


def _warm_anthropic_headers(credential: str, vary: dict) -> dict:
    headers = {
        "x-api-key": credential,
        "anthropic-version": str(vary.get("anthropic-version") or "2023-06-01"),
        "content-type": "application/json",
    }
    # The 1h TTL tier only exists under its beta flag; replaying without it
    # would address a different cache entry.
    if vary.get("anthropic-beta"):
        headers["anthropic-beta"] = str(vary["anthropic-beta"])
    return headers


def _warm_deepseek_body(payload: dict) -> dict:
    """DeepSeek keep-alive: OpenAI-compatible replay of the stored prefix plus
    one minimal user turn. The cache is automatic (no markers), so byte-identical
    prefix order is the entire addressing scheme."""
    # No sampling params: a rejected param would 400 and permanently stop the
    # prefix, and a keep-alive needs nothing beyond one generated token.
    body: dict[str, Any] = {
        "model": str(payload.get("model") or ""),
        "max_tokens": 1,
        "stream": False,
        "messages": [*(payload.get("messages_prefix") or []),
                     {"role": "user", "content": "."}],
    }
    if payload.get("tools"):
        body["tools"] = payload["tools"]
    return body


def _warm_deepseek_headers(credential: str, vary: dict) -> dict:
    return {
        "Authorization": f"Bearer {credential}",
        "Content-Type": "application/json",
    }


# Keep-alive dispatch: a provider is warmable only when a spec here defines a
# dedicated endpoint (credentials are never replayed against another provider's
# URL) and its cache math can work. Fractions/TTLs from the 2026-07 capability
# review. Deliberately absent, per the measurement-only rule:
#   - openai: gpt-5.6+ reads at 0.10x, but OpenAI does not document TTL refresh
#     on read, so keep-alive pings are unverifiable spend (mirrors
#     _WARM_INACTIVE_REASONS in api/server.py).
#   - groq/fireworks: 0.50x reads — a ping costs exactly what a return saves,
#     so warming can never net positive.
#   - perplexity: no cached-input discount exists; there is nothing to warm.
WARM_PROVIDER_SPECS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "endpoint_url": "https://api.anthropic.com/v1/messages",
        "operation": "messages",
        "build_body": _warm_anthropic_body,
        "build_headers": _warm_anthropic_headers,
        # 0.10x reads refresh the 5m TTL for free; the scalar env ROI knobs
        # are calibrated to this provider (see _warm_break_even_by_provider).
        "read_cost_fraction": 0.10,
        "ttl_seconds": 300,
    },
    "deepseek": {
        # OpenAI-compatible chat/completions at the same base URL the proxy
        # routes deepseek traffic to (brevitas/proxy _UPSTREAMS).
        "endpoint_url": "https://api.deepseek.com/v1/chat/completions",
        "operation": "chat.completions",
        "build_body": _warm_deepseek_body,
        "build_headers": _warm_deepseek_headers,
        # Automatic prefix cache: hits bill at 0.02x of input with no write
        # premium, and the entry persists hours while in use.
        "read_cost_fraction": 0.02,
        "ttl_seconds": 14_400,
    },
}


def _warm_break_even_by_provider(roi_break_even_p: float) -> dict[str, float]:
    """Per-provider break-even return probability for warm_due_claim. The
    scalar env knob stays the global fallback and keeps governing anthropic
    unchanged (its default was calibrated to that provider's
    1.25x-write/0.9x-read economics). Every other spec'd provider derives its
    break-even from its own read-cost fraction: a warm ping costs f, a warm
    return saves 1-f, so warming nets positive only when P(return) exceeds
    f / (1 - f). Reserve and TTL deliberately have no per-provider channel —
    ping_reserve_usd is already observer-priced per row at the stored model,
    and the flat reserve_usd_per_mtok floor only ever over-reserves (a safe
    upper bound), matching the warm_due_claim contract in migration
    202607280003."""
    return {
        provider: (
            roi_break_even_p if provider == "anthropic"
            else min(1.0, round(
                float(spec["read_cost_fraction"])
                / max(1.0 - float(spec["read_cost_fraction"]), 1e-9), 6)))
        for provider, spec in WARM_PROVIDER_SPECS.items()
    }


def _warm_claim_kwargs() -> dict[str, Any]:
    reserve_usd_per_mtok = _warm_bound(
        "BREVITAS_WARM_RESERVE_USD_PER_MTOK", 3.75, 0.0, 1000.0)
    # Break-even return probability at one ping per TTL window: a 1.25x
    # write premium buys a ~0.9x read discount on the same prefix tokens.
    # Both scalars are the anthropic-calibrated global fallback;
    # roi_break_even_by_provider carries the provider-correct break-evens
    # for everything else.
    roi_break_even_p = _warm_bound(
        "BREVITAS_WARM_ROI_BREAK_EVEN_P", 0.11, 0.0, 1.0)
    return {
        "reserve_usd_per_mtok": reserve_usd_per_mtok,
        "roi_min_arrivals": int(_warm_bound(
            "BREVITAS_WARM_ROI_MIN_ARRIVALS", 5, 1, 1000)),
        "roi_min_p": _warm_bound("BREVITAS_WARM_ROI_MIN_P", 0.35, 0.0, 1.0),
        "roi_break_even_p": roi_break_even_p,
        "roi_break_even_by_provider": _warm_break_even_by_provider(roi_break_even_p),
        "stop_loss": int(_warm_bound("BREVITAS_WARM_STOP_LOSS", 3, 1, 100)),
        "max_gap_seconds": int(_warm_bound(
            "BREVITAS_WARM_MAX_GAP", 3600, 1, 604_800)),
        "safety_margin_seconds": int(_warm_bound(
            "BREVITAS_WARM_SAFETY_MARGIN_SECONDS", 60, 0, 3600)),
        # The lease must outlive a full sequential batch of pings, not one
        # tick, or an unsynchronized replica re-claims the batch's tail.
        "claim_lease_seconds": int(_warm_bound(
            "BREVITAS_WARM_CLAIM_LEASE_SECONDS",
            max(900, int(_warm_bound("BREVITAS_WARM_CLAIM_LIMIT", 50, 1, 500)) * 30),
            60, 7200)),
    }


def _send_warm_ping(provider: str, spec: dict, body: dict,
                    headers: dict) -> tuple[int, dict]:
    with _provider_call():
        resp = provider_sync_http.request(
            provider, spec["operation"], "POST", spec["endpoint_url"],
            headers=headers, json=body,
        )
        try:
            status = int(resp.status_code)
            data: dict = {}
            if 200 <= status < 300:
                try:
                    parsed = resp.json()
                    data = parsed if isinstance(parsed, dict) else {}
                except (TypeError, ValueError):
                    data = {}
        finally:
            resp.close()
    return status, data


async def _warm_one(row: dict, cycle_ts: int, safety_margin_seconds: int) -> None:
    organization_id = str(row.get("organization_id") or "")
    customer_id = str(row.get("customer_id") or "")
    provider = str(row.get("provider") or "")
    prefix_hash = str(row.get("prefix_hash") or "")
    spec = WARM_PROVIDER_SPECS.get(provider)
    outcome = "release"
    spent_usd = 0.0
    lease = None
    try:
        credential = _decrypt(str(row.get("credential_ciphertext") or ""), context={
            "purpose": "warm_provider_credential",
            "organization_id": organization_id,
        })
        # AAD must byte-match the encrypt context in api/server.py
        # _hosted_warm_observe, the sole writer of this ciphertext.
        envelope = json.loads(_decrypt(str(row.get("payload_ciphertext") or ""), context={
            "purpose": "warm_prefix_payload",
            "organization_id": organization_id,
            "customer_id": customer_id,
        }))
        recorded_by = str(envelope.get("recorded_by_key_hash") or "")
        payload = (envelope.get("payload")
                   if isinstance(envelope.get("payload"), dict) else envelope)
        key_context = (await asyncio.to_thread(
            _authoritative_service_key_context, recorded_by) if recorded_by else None)
        if not key_context:
            # Spend can never be attributed or billed without the recording
            # service key; a revoked key permanently stops this prefix.
            outcome = "prefix_invalid"
            return
        if spec is None:
            # Only providers with a keep-alive spec are warmable, each against
            # its own dedicated endpoint; replaying a credential against
            # another provider's URL must never happen, and providers whose
            # cache math cannot work stay measurement-only (see the
            # WARM_PROVIDER_SPECS exclusion list).
            outcome = "prefix_invalid"
            return
        model = str(payload.get("model") or "")
        if model_price(provider, model) is None:
            # An unpriced model would spend real provider dollars while
            # settling spent_usd=0, so daily_budget_usd never binds. Stop the
            # prefix; re-observation reactivates it once MODEL_PRICES knows
            # the model.
            outcome = "prefix_invalid"
            return
        upstream = str(payload.get("upstream") or "")
        if upstream and upstream != spec["endpoint_url"]:
            # Automatic-cache payloads record the exact endpoint the observed
            # request used (x-brevitas-upstream can route an allowlisted
            # alternate host while the provider name stays the same). Live
            # traffic never reads this spec's cache then, so a ping here is
            # pure spend — and the credential must never be replayed against
            # a URL it was not observed with. Anthropic payloads carry no
            # upstream field; the spec endpoint is their only endpoint.
            outcome = "prefix_invalid"
            return
        lease = await _distributed_limiter.acquire(
            LimitIdentity(organization_id, customer_id, recorded_by, provider),
            tokens=max(1, int(row.get("prefix_tokens") or 0)),
            request_id=f"warm_{prefix_hash[:16]}",
        )
        if not lease.allowed:
            # Live traffic outranks warming; releasing the reservation is the
            # whole backoff (the claim already pushed next_due_at).
            return
        body = spec["build_body"](payload)
        headers = spec["build_headers"](credential, payload.get("vary") or {})
        try:
            status, data = await asyncio.to_thread(
                _send_warm_ping, provider, spec, body, headers)
        except ProviderCircuitOpen:
            # Fires before any request is issued, so nothing was spent.
            return
        except httpx.HTTPError:
            # Known residual gap: a read timeout or reset after the provider
            # already accepted the request settles 'release', which books no
            # spend. Booking it needs a settle outcome that releases the
            # reservation while recording spend ('spent_unknown'), which has to
            # be added to warm_ping_settle and its RPC whitelist together — a
            # forward migration plus api/store.py, neither of which can change
            # here. Until then this is capped by max_pings_per_customer_day.
            return
        if status in (401, 403):
            outcome = "auth_failed"
            return
        if status in (400, 404):
            outcome = "prefix_invalid"
            return
        if not 200 <= status < 300:
            return
        # The provider has already charged for this ping, so it must settle as
        # 'warmed' from here on: that is the only outcome warm_ping_settle books
        # spend for, and the one that advances next_due_at. Start from the
        # reservation — warm_due_claim observer-prices it as an upper bound on
        # this ping and the daily ceiling already admitted it — and refine it
        # downward only once a parseable, priced receipt proves the real cost.
        # Booking 0 for an unreadable 2xx body would release the whole
        # reservation, so daily_budget_usd would never bind and
        # billing_period_settlement_evidence.warm_spend_usd would compute the
        # fee ceiling as if this spend never happened.
        outcome = "warmed"
        spent_usd = float(row.get("reserved_usd") or 0.0)
        receipt = normalize_usage(data.get("usage"), provider)
        costs = calculate_costs(provider, model, receipt.input_tokens, receipt)
        if receipt.total_tokens and str(costs.get("pricing_status")) == "priced":
            spent_usd = float(costs.get("actual_cost_usd") or 0.0)
        else:
            logger.warning(
                "warm_ping_usage_unreadable", provider=provider,
                pricing_status=str(costs.get("pricing_status") or "unpriced"),
            )
        await asyncio.to_thread(
            _safe_record_usage,
            auth_context=AuthContext(
                key_hash=recorded_by, organization_id=organization_id,
                billing_owner_id=str(key_context.get("owner_id") or ""),
                customer_id=customer_id, key_type=str(key_context.get("key_type") or ""),
            ),
            key_hash=recorded_by, baseline_tokens=receipt.input_tokens,
            optimized_tokens=receipt.input_tokens, savings_pct=0, quality_proxy=None,
            strategy="cache_warm", receipt_source="worker",
            request_id=f"warm:{prefix_hash[:16]}:{cycle_ts}",
            provider=provider, model=model,
            fresh_input_tokens=receipt.fresh_input_tokens,
            cached_input_tokens=receipt.cached_input_tokens,
            cache_write_tokens=receipt.cache_write_tokens,
            cache_write_5m_tokens=receipt.cache_write_5m_tokens,
            cache_write_1h_tokens=receipt.cache_write_1h_tokens,
            output_tokens=receipt.output_tokens,
            # A ping is pure spend, never savings: baseline equals actual.
            baseline_cost_usd=costs.get("actual_cost_usd"),
            actual_cost_usd=costs.get("actual_cost_usd"),
            measured_savings_usd=0.0,
            pricing_status=costs.get("pricing_status") or "unpriced",
            pricing_version=costs.get("pricing_version") or "",
        )
    except Exception as exc:
        # outcome/spent_usd are already committed above for any ping the provider
        # answered, so a failure after that point still books the spend. This also
        # covers asyncio.CancelledError on shutdown, which `except Exception`
        # cannot see: it propagates, the finally settles, and the ping is booked.
        logger.error("warm_ping_failed", error_type=type(exc).__name__)
    finally:
        if lease is not None:
            try:
                await lease.release()
            except LimiterUnavailable:
                pass
        try:
            await asyncio.to_thread(
                _store.warm_ping_settle,
                organization_id, customer_id, provider, prefix_hash,
                str(row.get("budget_day") or ""),
                float(row.get("reserved_usd") or 0.0),
                spent_usd if outcome == "warmed" else 0.0, outcome,
                int(row.get("provider_ttl_seconds")
                    or (spec["ttl_seconds"] if spec else 300)),
                safety_margin_seconds,
                # Fences the prefix mutation to this claim: a lapsed lease
                # cannot double-apply counters after a re-claim.
                claim_token=str(row.get("claim_token") or "") or None,
            )
        except Exception as exc:
            logger.error("warm_settle_failed", error_type=type(exc).__name__)


async def warming(stop: asyncio.Event) -> None:
    if not _warming_enabled():
        return
    interval = max(1.0, _warm_bound("BREVITAS_WARM_INTERVAL_SECONDS", 60, 1, 3600))
    claim_limit = int(_warm_bound("BREVITAS_WARM_CLAIM_LIMIT", 50, 1, 500))
    while not stop.is_set():
        if _WORKER_ACCEPTING:
            try:
                claim_kwargs = _warm_claim_kwargs()
                claimed = await asyncio.to_thread(
                    _store.warm_due_claim, claim_limit, **claim_kwargs)
                # lease_unavailable means another replica holds this cycle's
                # advisory lock; sleeping until the next tick is the protocol.
                if str(claimed.get("status") or "") == "ok":
                    cycle_ts = int(time.time())
                    for row in claimed.get("rows") or []:
                        await _warm_one(
                            row, cycle_ts, claim_kwargs["safety_margin_seconds"])
            except Exception as exc:
                logger.error("warming_cycle_error", error_type=type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


async def settlement_sweep(stop: asyncio.Event) -> None:
    """Draft-only period-settlement sweep. OFF unless explicitly armed.

    Three independent things must all be true before a single RPC is sent:
    BREVITAS_BILLING_SETTLEMENT_SWEEP_ENABLED is exactly "true", Supabase
    service-role credentials are present, and this worker is not declared
    `nonbilling`. Anything less returns immediately, so deploying the sweep
    cannot draft for anyone by surprise.

    Deliberately NOT under _run_billing_supervisor: that supervisor escalates to
    a full process restart because a dead send loop is silent revenue loss. This
    loop only writes drafts — money still cannot move without a human running
    promote_billing_period_settlement in psql — so a sweep outage is a ticket,
    not a reason to bounce a worker that is serving jobs.
    """
    if _BILLING_ROLE == "nonbilling":
        return
    if not billing_settlement_sweep_is_enabled():
        return
    if not billing_settlement_sweep_is_configured():
        logger.error(
            "billing_settlement_sweep_unconfigured", outcome="disabled",
            flag=SWEEP_ENABLED_ENV,
        )
        return
    try:
        sweep = build_settlement_sweep_from_env(owner=billing_worker_owner())
    except Exception as exc:
        logger.error("billing_settlement_sweep_build_failed",
                     error_type=type(exc).__name__)
        return
    await run_billing_settlement_sweep_loop(sweep, stop)


def _billing_restart_delay(restart_backoff: float, restarts: int) -> float:
    """Bounded restart backoff: linear in restart count, hard-capped at 60s."""
    return min(max(0.0, restart_backoff) * max(1, restarts), 60.0)


async def _run_billing_supervisor(
    stop: asyncio.Event,
    loop_factory: Callable[[], Coroutine[Any, Any, None]],
    *,
    max_restarts: int,
    restart_backoff: float,
) -> None:
    """Supervise the billing recovery loop: restart, back off, then escalate.

    The loop is decoupled from durable job consumption (a billing outage must not
    stop jobs), but its process-level exit still needs supervision: otherwise a
    single unexpected escape (a bug in the loop body, MemoryError, etc.)
    permanently halts Stripe reporting while /ready stays green — silent revenue
    loss. On an unexpected exit we log it, back off, and recreate the loop. If
    self-heal is exhausted on an authoritative worker (billing is required), we
    escalate to a full process restart by setting `stop` — the orchestrator
    brings the worker back, restoring the pre-decoupling safety net — rather than
    run on indefinitely with billing dead.

    The inner task is never cancelled from here: the loop defers cancellation
    until its current bounded `to_thread` call returns and releases its own
    never-started claims, so cancelling it could duplicate an in-flight send
    (docs/STRIPE_BILLING.md, W1 worker integration contract items 3 and 5).
    """
    global _BILLING_LOOP_RUNNING
    restarts = 0
    while not stop.is_set():
        inner = asyncio.create_task(loop_factory(), name="billing-recovery")
        _BILLING_LOOP_RUNNING = True
        failure: BaseException | None = None
        try:
            await inner
        except asyncio.CancelledError:
            _BILLING_LOOP_RUNNING = False
            inner.cancel()
            await asyncio.gather(inner, return_exceptions=True)
            raise
        except Exception as exc:
            # WR-1: without this branch an exception escaping the loop body kills
            # the supervisor right here, so every line below — the advertised
            # restart, backoff and escalation — was dead code under *every*
            # execution. `await inner` re-raises, so the exception is held in
            # `failure` instead of being re-read off the completed task (which,
            # on the only other way out of the loop, is provably always None).
            failure = exc
        _BILLING_LOOP_RUNNING = False
        with _BILLING_HEALTH_LOCK:
            stopped_health = dict(_BILLING_HEALTH)
        stopped_health["running"] = False
        _report_billing_health(stopped_health)
        if failure is None and stop.is_set():
            break  # normal shutdown: the loop observed `stop` and returned
        restarts += 1
        logger.error(
            "billing_loop_stopped", outcome="degraded", restart=restarts,
            error_type=type(failure).__name__ if failure is not None else "none")
        if stop.is_set():
            # A drain (or a previous escalation) is already under way: never
            # start another loop that the bounded shutdown would have to await.
            break
        if restarts > max_restarts:
            logger.error("billing_loop_unrecoverable", outcome="halted",
                         restarts=restarts, billing_required=_BILLING_REQUIRED)
            if _BILLING_REQUIRED:
                # Authoritative billing cannot silently halt. Escalate to a
                # process restart; the loop is not cancelled, so no in-flight
                # send is duplicated.
                stop.set()
            break
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=_billing_restart_delay(restart_backoff, restarts))
        except TimeoutError:
            pass


async def _drain_billing_supervisor(billing_task: "asyncio.Task[None] | None") -> None:
    """Await the billing supervisor without letting its failure skip cleanup.

    The billing loop shields bounded thread work and releases its own leases, so
    it must finish naturally; cancellation could duplicate an in-flight send. A
    bare `await` here would re-raise a stored supervisor exception out of run()'s
    finally block and skip every shutdown step after it (health server, provider
    clients, Redis clients, credential cipher cache, observability flush).
    """
    if billing_task is None:
        return
    try:
        await billing_task
    except Exception as exc:
        logger.error("billing_supervisor_failed", outcome="degraded",
                     error_type=type(exc).__name__)


async def run() -> None:
    global _WORKER_ACCEPTING, _BILLING_ROLE, _BILLING_REQUIRED
    global _BILLING_CONFIGURED, _BILLING_LOOP_RUNNING
    worker_id = os.getenv("BREVITAS_WORKER_ID") or f"worker_{uuid.uuid4().hex[:16]}"
    concurrency = max(1, min(100, int(os.getenv("BREVITAS_WORKER_CONCURRENCY", "10"))))
    poll_seconds = max(0.05, float(os.getenv("BREVITAS_JOB_POLL_SECONDS", "0.25")))
    stop = asyncio.Event()
    validate_production_build_identity(_production_runtime())
    _configure_managed_kms_from_deployment()
    cipher = _initialize_credential_cipher(required=True)
    _BILLING_ROLE = _billing_worker_role()
    _BILLING_REQUIRED = _BILLING_ROLE == "authoritative"
    _BILLING_CONFIGURED = billing_recovery_is_configured()
    _BILLING_LOOP_RUNNING = False
    _report_billing_health({
        "running": False,
        "initial_validation_succeeded": False,
        "catalog_valid": False,
        "last_success_monotonic": 0.0,
        "consecutive_errors": 0,
        "last_error_monotonic": 0.0,
    })
    if _BILLING_REQUIRED and not _BILLING_CONFIGURED:
        raise RuntimeError("authoritative billing recovery configuration is incomplete")
    billing_task = None
    if _BILLING_ROLE != "nonbilling" and _BILLING_CONFIGURED:
        billing_processor = build_billing_recovery_processor_from_env(
            telemetry=BillingTelemetryAdapter(),
        )
        billing_kwargs: dict[str, Any] = {"owner": worker_id}
        try:
            reporter_supported = (
                "health_reporter" in inspect.signature(
                    run_billing_recovery_loop).parameters)
        except (TypeError, ValueError):
            reporter_supported = False
        if reporter_supported:
            billing_kwargs["health_reporter"] = _report_billing_health

        # Supervise the billing loop instead of firing it once and forgetting it
        # (see _run_billing_supervisor for the restart/escalation contract).
        max_restarts = int(_billing_readiness_bound(
            "BREVITAS_BILLING_LOOP_MAX_RESTARTS", 5, 0, 100))
        restart_backoff = _billing_readiness_bound(
            "BREVITAS_BILLING_LOOP_RESTART_BACKOFF_SECONDS", 2.0, 0.0, 60.0)
        billing_task = asyncio.create_task(
            _run_billing_supervisor(
                stop,
                lambda: run_billing_recovery_loop(
                    billing_processor, stop, **billing_kwargs),
                max_restarts=max_restarts,
                restart_backoff=restart_backoff,
            ),
            name="billing-recovery-supervisor")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    # Both address families are required: Railway's healthcheck prober reaches
    # /ready over IPv4, while the private mesh (*.railway.internal) is IPv6-only.
    # asyncio sets IPV6_V6ONLY=1 on every AF_INET6 socket it opens, so letting
    # uvicorn bind host="::" yields an IPv6-ONLY listener that refuses the IPv4
    # healthcheck, and host="0.0.0.0" passes the healthcheck but leaves the mesh
    # with no socket. Bind one socket here with IPV6_V6ONLY=0 and hand it to
    # uvicorn, which then ignores config host/port and serves both families.
    health_socket = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    health_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    health_socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    health_socket.bind(("::", int(os.getenv("PORT", "8001"))))
    health_config = uvicorn.Config(
        health_app,
        access_log=False,
        log_level=os.getenv("BREVITAS_LOG_LEVEL", "info").lower(),
    )
    health_server = uvicorn.Server(health_config)
    # This process owns signal handling so it can drain leased work before stopping Uvicorn.
    health_server.install_signal_handlers = lambda: None
    # Uvicorn calls listen() on the handed-off socket during startup and closes it
    # on shutdown, so the should_exit drain below still releases the port.
    health_task = asyncio.create_task(
        health_server.serve(sockets=[health_socket]), name="worker-health-server")
    health_task.add_done_callback(lambda _task: stop.set())

    async def consume(slot: int) -> None:
        slot_id = f"{worker_id}_{slot}"
        stream_id = "$"
        while not stop.is_set():
            if not _WORKER_ACCEPTING:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
                continue
            try:
                processed = await _job_service.process_one(slot_id, process)
            except Exception as exc:
                logger.error("job_consumer_error", error_type=type(exc).__name__,
                             worker_slot=slot)
                processed = False
            if not processed:
                try:
                    stream_id = await asyncio.wait_for(
                        _job_service.dispatcher.wait_for_notification(
                            stream_id, max(50, int(poll_seconds * 1000))
                        ),
                        timeout=poll_seconds + 0.5,
                    )
                except TimeoutError:
                    pass

    async def dependency_monitor() -> None:
        global _WORKER_ACCEPTING
        interval = max(1.0, float(os.getenv("BREVITAS_WORKER_HEALTH_INTERVAL", "5")))
        while not stop.is_set():
            database_ready, redis_ready = await _dependencies_ready()
            kms_ready = _kms_dependency_ready(await _kms_readiness_status())
            # Job acceptance depends only on the deps jobs actually need.
            # Billing recovery health is reported separately and never gates.
            _WORKER_ACCEPTING = database_ready and redis_ready and kms_ready
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def maintenance() -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(_job_service.store.purge)
            except Exception as exc:
                logger.error("job_retention_purge_failed", error_type=type(exc).__name__)
            try:
                await asyncio.to_thread(_store.purge_provider_configs)
            except Exception as exc:
                logger.error(
                    "provider_credential_cleanup_failed",
                    error_type=type(exc).__name__,
                )
            try:
                # 365, not 7: 202607280017 floors the warm_budget_ledger horizon at
                # 365 days internally because the ledger is settlement evidence, so
                # a 7-day default was only misleading about the ledger. The same
                # value drives the warm_prefixes TTL cleanup, where it is the
                # operative bound — so state the retention intent explicitly rather
                # than relying on the database floor to correct it.
                await asyncio.to_thread(
                    _store.purge_warm_state,
                    int(_warm_bound("BREVITAS_WARM_RETENTION_DAYS", 365, 1, 365)),
                )
            except Exception as exc:
                logger.error("warm_state_purge_failed", error_type=type(exc).__name__)
            # 202607280025's limiter janitor. Every limiter scope sweeps itself
            # inline, but only while its own endpoint is being hit — waitlist.global,
            # waitlist.identity and the billing_* scopes linger when their endpoint
            # goes quiet. Deletes expired rows ONLY, so it can never widen a budget.
            # Absent on the local store and a no-op until the migration is applied.
            purge_limits = getattr(_store, "purge_shared_endpoint_rate_limits", None)
            if callable(purge_limits):
                try:
                    await asyncio.to_thread(purge_limits, 5000)
                except Exception as exc:
                    logger.error("shared_rate_limit_purge_failed",
                                 error_type=type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=300)
            except TimeoutError:
                pass

    database_ready, redis_ready = await _dependencies_ready()
    kms_ready = _kms_dependency_ready(await _kms_readiness_status())
    _WORKER_ACCEPTING = database_ready and redis_ready and kms_ready
    tasks = [
        asyncio.create_task(dependency_monitor(), name="worker-dependency-monitor"),
        asyncio.create_task(maintenance(), name="worker-maintenance"),
        asyncio.create_task(warming(stop), name="worker-warming"),
        asyncio.create_task(settlement_sweep(stop), name="worker-settlement-sweep"),
        *(asyncio.create_task(consume(slot), name=f"worker-consumer-{slot}")
          for slot in range(concurrency)),
    ]
    await stop.wait()
    _WORKER_ACCEPTING = False
    drain_seconds = max(1.0, float(os.getenv("BREVITAS_WORKER_DRAIN_SECONDS", "120")))
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=drain_seconds)
    except TimeoutError:
        # active jobs will recover by lease expiry after this bounded drain.
        logger.warning("worker_drain_deadline", outcome="lease_lost")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # `stop` is already set here. The billing loop shields bounded thread work
        # and releases its own leases, so it is awaited (never cancelled) — and a
        # supervisor failure must not skip any cleanup below it.
        await _drain_billing_supervisor(billing_task)
        health_server.should_exit = True
        await asyncio.gather(health_task, return_exceptions=True)
        provider_drain = max(
            0.0, float(os.getenv("BREVITAS_PROVIDER_CLOSE_DRAIN_SECONDS", "10")))
        provider_drained = await asyncio.to_thread(
            _wait_for_provider_calls, provider_drain)
        if provider_drained:
            await asyncio.to_thread(close_provider_sync_clients)
        else:
            # A cancelled asyncio.to_thread call can still be completing in its OS thread.
            # Let process exit close sockets instead of invalidating its shared client mid-call.
            logger.warning("provider_client_close_skipped", outcome="unavailable")
        clients = {
            id(client): client for client in (
                getattr(_distributed_limiter, "redis", None),
                getattr(_job_service.dispatcher, "redis", None),
            ) if client is not None
        }
        for client in clients.values():
            closer = getattr(client, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    pass
        if cipher is not None:
            cipher.cache.clear()
        graceful_observability_shutdown()


if __name__ == "__main__":
    asyncio.run(run())
