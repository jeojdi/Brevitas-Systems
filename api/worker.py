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
import random
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from datetime import datetime, timezone
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
from .store import (
    _WARM_TTL_MAX_GAP_SECONDS,
    warm_holdout_fraction,
    warm_model_class,
    warm_regime_classify,
    warm_ttl_tier,
)
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
        # Control arm, default 0 (off). Parsed by the store rather than
        # _warm_bound because api/server.py's warm_status has to report the
        # same number from the same env var, and two parsers of one knob is
        # how the advertised share and the applied share drift apart.
        "holdout_fraction": warm_holdout_fraction(),
        # The dollar index and its pacing dual (202608100002), default OFF.
        # Unlike the reward join and the regime scan -- which only write
        # analytics columns and are therefore default-on -- this flag changes
        # which candidates are claimed and in what order, so it is opt-in. At
        # off the RPC computes no index, touches no lambda row and behaves
        # exactly as 202608100001's did.
        "index_enabled": os.getenv("BREVITAS_WARM_INDEX", "false").lower()
                         in ("1", "true", "yes"),
        # Pacing gain: how hard lambda reacts to being off the linear intraday
        # spend target. Only read when the index is on.
        "lambda_eta": _warm_bound("BREVITAS_WARM_LAMBDA_ETA", 0.2, 0.01, 2.0),
        # Ceiling on the dual. lambda is in index units, and an index of 1000
        # means "a thousand times break-even", so this is effectively "deny
        # everything" without being unbounded.
        "lambda_max": _warm_bound(
            "BREVITAS_WARM_LAMBDA_MAX", 1000.0, 0.0, 1_000_000.0),
        # The decayed hierarchical hazard, P(alive) and the keep-alive chain
        # (202608100003), default OFF. Like the index and unlike the reward
        # join and the regime scan, this changes what gets claimed: p_return
        # stops being the lifetime histogram ratio and the stop-loss predicate
        # stops filtering candidates. At off no state row is read and the RPC
        # is 202608100002's. State WRITES are unconditional and happen in
        # warm_prefix_observe regardless, so the model has history the day
        # this is turned on.
        # INERT WITHOUT BREVITAS_WARM_INDEX. hazard_v2 is an EXTENSION of the
        # index policy, not an independent one: P(alive) and the keep-alive
        # chain only ever reach a decision through the index block, so the
        # claim requires BOTH flags before it retires the churn stop-loss.
        # Set alone it would otherwise drop the stop-loss with nothing in its
        # place, and a dead prefix would be re-claimed forever.
        "hazard_v2": os.getenv("BREVITAS_WARM_HAZARD_V2", "false").lower()
                     in ("1", "true", "yes"),
        # THE BETA SPEND CAP (202608100005), default 0. 0 means the cap does
        # not RUN, not that the cap is zero: a zero cap denies every candidate,
        # which is the right answer for an operator who armed the cap with no
        # control data and the wrong answer for one who never armed it. Above
        # zero, trailing 28-day warm spend may not exceed beta times the
        # control-verified savings in warm_control_savings_daily.
        "beta": _warm_bound("BREVITAS_WARM_SPEND_BETA", 0.0, 0.0, 100.0),
        # SHARED-PARENT DEDUP (202608100007), default OFF. Two arms of one
        # organization whose 202608100006 chain paths descend from a common
        # node share the provider's cache entry for the span up to that node,
        # so one keep-alive warms both and the second is a duplicate purchase.
        # On, the claim groups such arms, claims the cheapest one, and defers
        # the rest to the horizon that ping bought. Like the index and the
        # hazard model, and unlike the analytics jobs, this changes what gets
        # claimed, so it is opt-in.
        #
        # INERT WITHOUT A CHAIN. Grouping keys on warm_prefixes.chain_path,
        # which only 202608100006's observation writes and only when the salt
        # is configured and the tokenizer is exact. A deployment with no chain
        # rows groups nothing, which is the same no-op as leaving this off.
        "parent_dedup": os.getenv("BREVITAS_WARM_PARENT_DEDUP", "false").lower()
                        in ("1", "true", "yes"),
    }


# Transport failures that provably happen before any request byte can be
# accepted by the provider — the same set brevitas/provider_reliability.py:590-594
# retries unconditionally, and for the same reason. Everything else (read/write
# timeouts, resets, protocol errors) is ambiguous after a POST: the provider may
# already have run and billed the ping. UnsupportedProtocol never opens a
# connection at all. The terminal exception classifies the whole call
# because KNOWN_IDEMPOTENT_OPERATIONS is empty, so the pool only ever retries
# these pre-send failures — an ambiguous failure is never followed by another
# attempt whose type could mask it.
_WARM_PRE_SEND_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
    httpx.ProxyError, httpx.UnsupportedProtocol,
)


def _warm_touch_gap_seconds(row: Mapping[str, Any]) -> float | None:
    """Seconds since the claimed prefix was last provably touched.

    warm_due_claim reports the PRE-claim touch, so this is the interval the ping
    actually tested. None when the claim carries no usable timestamp or the gap
    falls outside the physics table's own bound, in which case no observation is
    recorded rather than a fabricated one.
    """
    raw = str(row.get("last_touch_at") or "")
    if not raw:
        return None
    try:
        touched = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if touched.tzinfo is None:
        touched = touched.replace(tzinfo=timezone.utc)
    gap = (datetime.now(timezone.utc) - touched).total_seconds()
    return gap if 0 <= gap <= _WARM_TTL_MAX_GAP_SECONDS else None


def _warm_ttl_outcome(receipt: Any) -> str | None:
    """Read the free TTL sensor off a ping receipt.

    Cached-input tokens mean the provider served the prefix from a live entry;
    cache-write tokens mean it had to create the entry, so the previous one was
    gone. A receipt with neither (unparseable body, or a provider that reports
    no cache legs) is not an observation and must not be invented.
    """
    if int(getattr(receipt, "cached_input_tokens", 0) or 0) > 0:
        return "warm"
    if int(getattr(receipt, "cache_write_tokens", 0) or 0) > 0:
        return "expired"
    return None


def _warm_error_is_pre_send(exc: BaseException) -> bool:
    """True when no request bytes can have reached the provider, so the ping
    cost nothing and its reservation is released rather than booked."""
    return isinstance(exc, _WARM_PRE_SEND_ERRORS)


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
        except httpx.HTTPError as exc:
            # A transport failure is only free when the request never left. Once
            # bytes are on the wire the provider may have accepted, cached and
            # billed the ping, and settling 'release' would book $0 — spend the
            # org really paid, missing from the ledger that computes the fee
            # ceiling. 'spent_unknown' books the full reservation instead.
            if not _warm_error_is_pre_send(exc):
                outcome = "spent_unknown"
            logger.warning(
                "warm_ping_transport_error", provider=provider,
                error_type=type(exc).__name__, outcome=outcome,
            )
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
        # The free TTL sensor. This receipt was already parsed to price the
        # ping; its cache legs say whether the entry survived the gap since the
        # prefix was last provably touched, which is the one measurement the
        # warming schedule currently has to guess. Best-effort and isolated:
        # a failure here must never change the settle that follows.
        try:
            ttl_outcome = _warm_ttl_outcome(receipt)
            gap_seconds = _warm_touch_gap_seconds(row)
            if ttl_outcome and gap_seconds is not None:
                await asyncio.to_thread(
                    _store.warm_ttl_observe, provider, warm_model_class(model),
                    warm_ttl_tier(int(row.get("provider_ttl_seconds")
                                      or spec["ttl_seconds"])),
                    gap_seconds, ttl_outcome, "ping")
        except Exception as exc:
            logger.warning("warm_ttl_observation_failed",
                           error_type=type(exc).__name__)
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
        # Stamp this ping's own receipt with the prefix it warmed, so the reward
        # join can find its cost. Same reason it is not a column on the insert
        # above: an unknown column there drops the whole row. Strictly after the
        # write, in its own guard -- the settle in `finally` has already been
        # committed to by `outcome`, and an analytics stamp must not be able to
        # divert this function into its error path.
        try:
            await asyncio.to_thread(
                _store.warm_usage_stamp_prefix, organization_id, recorded_by,
                f"warm:{prefix_hash[:16]}:{cycle_ts}", prefix_hash)
        except Exception as exc:
            logger.warning("warm_usage_stamp_failed",
                           error_type=type(exc).__name__)
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
                # Only 'warmed' carries a priced receipt. 'spent_unknown' has
                # none by definition, and the store books the reservation for
                # it rather than trusting anything sent here.
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
        # Close the loop on the logged decision. Strictly after the settle and
        # in its own guard: the decision log is analytics, and nothing about it
        # may delay, precede or fail the money path. A claim whose decision row
        # was never written (or has since aged out) updates nothing.
        claim_token = str(row.get("claim_token") or "")
        if claim_token:
            try:
                await asyncio.to_thread(
                    _store.warm_decision_settle_outcome, claim_token, outcome)
            except Exception as exc:
                logger.warning("warm_decision_outcome_failed",
                               error_type=type(exc).__name__)


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


def _warm_reward_join_enabled() -> bool:
    """ON by default, unlike warming itself.

    The join is read-only over usage_log and writes two analytics columns of
    warm_decision_log. It cannot ping, cannot spend, cannot bill and cannot
    settle, so the "new behavior ships OFF" rule that guards the warming loop
    does not apply -- but it is still a single env var away from silent, because
    a job nobody can stop is its own hazard.
    """
    return os.getenv("BREVITAS_WARM_REWARD_JOIN", "true").lower() not in (
        "0", "false", "no")


async def warm_reward_join(stop: asyncio.Event) -> None:
    """Hourly: credit each closed warm ping with what it actually earned.

    Gated on warming being on at all -- with no warming there are no decisions
    to score, and running the query anyway is pure load. Every cycle is
    independent: the RPC only ever fills a null realized_net_usd, so a missed
    hour is picked up by the next one for as long as the row stays inside the
    lookback window.
    """
    if not _warming_enabled() or not _warm_reward_join_enabled():
        return
    interval = _warm_bound("BREVITAS_WARM_REWARD_JOIN_INTERVAL_SECONDS",
                           3600, 60, 86_400)
    lookback = int(_warm_bound("BREVITAS_WARM_REWARD_JOIN_LOOKBACK_HOURS",
                               48, 1, 720))
    limit = int(_warm_bound("BREVITAS_WARM_REWARD_JOIN_LIMIT", 5000, 1, 50_000))
    while not stop.is_set():
        if _WORKER_ACCEPTING:
            try:
                result = await asyncio.to_thread(
                    _store.warm_reward_join, lookback, limit)
                logger.info(
                    "warm_reward_join_cycle",
                    scanned=int(result.get("scanned") or 0),
                    joined=int(result.get("joined") or 0),
                    attributed=int(result.get("attributed") or 0),
                    organic=int(result.get("organic") or 0),
                    unpriced=int(result.get("unpriced") or 0),
                )
            except Exception as exc:
                logger.error("warm_reward_join_error",
                             error_type=type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


def _warm_regime_enabled() -> bool:
    """ON by default, on the reward join's precedent and for its reason.

    The regime scan reads authoritative usage_log rows and writes two analytics
    columns of warm_customer_state. It cannot ping, spend, bill or settle, and
    NOTHING READS THE LABEL IT WRITES in Phase 1 -- the deterministic fast-path
    scheduler for periodic customers is plan section 4.3 and lands under its own
    flag. So the "new behavior ships OFF" rule that guards the warming loop does
    not apply; the label accumulates so that scheduler starts with data. Still a
    single env var away from silent, because a job nobody can stop is its own
    hazard.
    """
    return os.getenv("BREVITAS_WARM_REGIME", "true").lower() not in (
        "0", "false", "no")


async def warm_regime_refresh(stop: asyncio.Event) -> None:
    """Daily: label each learned customer periodic-daily, -weekly or aperiodic.

    Gated on warming being on at all -- with no warming there are no state rows
    to classify, and running the scan anyway is pure load. Every cycle is
    independent: only rows whose label is null or older than seven days are
    returned, so a missed day is picked up by the next one.
    """
    if not _warming_enabled() or not _warm_regime_enabled():
        return
    interval = _warm_bound("BREVITAS_WARM_REGIME_INTERVAL_SECONDS",
                           86_400, 3_600, 604_800)
    limit = int(_warm_bound("BREVITAS_WARM_REGIME_LIMIT", 500, 1, 10_000))
    while not stop.is_set():
        if _WORKER_ACCEPTING:
            try:
                series = await asyncio.to_thread(
                    _store.warm_customer_arrival_buckets, 28, limit)
                labelled = 0
                for entry in series or []:
                    regime, score = warm_regime_classify(
                        list(entry.get("counts") or []))
                    await asyncio.to_thread(
                        _store.warm_customer_state_set_regime,
                        str(entry.get("organization_id") or ""),
                        str(entry.get("customer_id") or ""),
                        str(entry.get("provider") or ""), regime, float(score))
                    labelled += 1
                logger.info("warm_regime_cycle", scanned=len(series or []),
                            labelled=labelled)
            except Exception as exc:
                logger.error("warm_regime_error", error_type=type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


def _warm_envelope_allocator_enabled() -> bool:
    """OFF by default, unlike the reward join and the regime scan.

    Those two only write analytics columns nothing gates on. This one WRITES A
    HARD SPEND GATE: once warm_customer_budget rows exist for an (org, provider,
    month), the claim path denies any candidate whose reservation would carry
    its customer past that envelope. An organization that never asked for
    per-customer fairness must not silently acquire per-customer ceilings
    because a worker was deployed, so the allocator is opt-in and the gate stays
    vacuous -- no row, no constraint -- until it runs or an operator PUTs a row.
    """
    return os.getenv("BREVITAS_WARM_ENVELOPE_ALLOCATOR", "false").lower() in (
        "1", "true", "yes")


async def warm_envelope_allocator(stop: asyncio.Event) -> None:
    """Daily: split each organization's monthly warming budget across its
    customers in proportion to trailing 28-day positive index mass.

    Gated on warming being on at all -- with no warming there is nothing to
    apportion. Every cycle is independent and idempotent in the direction that
    matters: the upsert is raise-only and skips org_override rows, so a missed
    day costs nothing and a double run changes nothing.
    """
    if not _warming_enabled() or not _warm_envelope_allocator_enabled():
        return
    interval = _warm_bound("BREVITAS_WARM_ENVELOPE_INTERVAL_SECONDS",
                           86_400, 3_600, 604_800)
    limit = int(_warm_bound("BREVITAS_WARM_ENVELOPE_ORG_LIMIT", 200, 1, 10_000))
    while not stop.is_set():
        if _WORKER_ACCEPTING:
            try:
                result = await asyncio.to_thread(
                    _store.warm_customer_budget_allocate, limit)
                logger.info("warm_envelope_allocator_cycle",
                            pairs=int(result.get("pairs") or 0),
                            written=int(result.get("written") or 0),
                            period_start=str(result.get("period_start") or ""))
            except Exception as exc:
                logger.error("warm_envelope_allocator_error",
                             error_type=type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


def _warm_learned_flags_on() -> bool:
    """True when anything LEARNED is actually running.

    The guardrail freezes a pair back to flat priors. With neither the index nor
    the hazard model on there is nothing learned to freeze -- flat priors are
    already what the claim path uses -- so the guardrail loop exits and its
    default-on flag has zero default behaviour change.
    """
    return (os.getenv("BREVITAS_WARM_INDEX", "false").lower() in ("1", "true", "yes")
            or os.getenv("BREVITAS_WARM_HAZARD_V2", "false").lower()
            in ("1", "true", "yes"))


async def warm_control_savings(stop: asyncio.Event) -> None:
    """Refresh the treated-versus-control comparison the beta cap reads.

    Default ON, on the reward-join precedent (api/worker.py:880-890): this
    writes an analytics table and nothing gates on it until an operator sets
    BREVITAS_WARM_SPEND_BETA above zero. Writing it by default is what makes the
    cap armable at all -- a cap switched on with an empty producer table denies
    everything, and the fix for that must not be "wait 28 days".
    """
    if not _warming_enabled() or os.getenv(
            "BREVITAS_WARM_CONTROL_SAVINGS", "true").lower() not in (
                "1", "true", "yes"):
        return
    interval = _warm_bound("BREVITAS_WARM_CONTROL_SAVINGS_INTERVAL_SECONDS",
                           21_600, 600, 86_400)
    while not stop.is_set():
        if _WORKER_ACCEPTING:
            try:
                result = await asyncio.to_thread(
                    _store.warm_control_savings_refresh, 28)
                logger.info("warm_control_savings_cycle",
                            days_scanned=int(result.get("days_scanned") or 0),
                            rows_written=int(result.get("rows_written") or 0),
                            mixed_units=int(result.get("mixed_units") or 0))
            except Exception as exc:
                logger.error("warm_control_savings_error",
                             error_type=type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


async def warm_attribution(stop: asyncio.Event) -> None:
    """Recompute the airport-game attribution statement for the open days.

    Default ON, on the reward-join and control-savings precedent
    (api/worker.py:880-890, 1101-1112): this writes an analytics table, nothing
    gates on it and nothing bills from it. It is also inert by construction
    until 202608100006's chain tree has data -- an arm with no chain_leaf_digest
    contributes no ancestry, so on a deployment where BREVITAS_WARM_CHAIN_SALT
    is unset the job scans and writes nothing.

    MEASURED-ONLY. The numbers this loop produces are never a billed quantity;
    promoting any of them requires an explicit decision this loop does not make.
    """
    if not _warming_enabled() or os.getenv(
            "BREVITAS_WARM_ATTRIBUTION", "true").lower() not in (
                "1", "true", "yes"):
        return
    interval = _warm_bound("BREVITAS_WARM_ATTRIBUTION_INTERVAL_SECONDS",
                           86_400, 600, 604_800)
    while not stop.is_set():
        if _WORKER_ACCEPTING:
            try:
                result = await asyncio.to_thread(
                    _store.warm_attribution_run, 2, 7)
                logger.info("warm_attribution_cycle",
                            days_scanned=int(result.get("days_scanned") or 0),
                            rows_written=int(result.get("rows_written") or 0),
                            cost_misses=int(result.get("cost_misses") or 0))
            except Exception as exc:
                # A conservation violation raises here rather than writing a
                # statement that does not add up. Failing the job is the point.
                logger.error("warm_attribution_error",
                             error_type=type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


async def warm_guardrail(stop: asyncio.Event) -> None:
    """Freeze any (organization, provider) whose learned policy is losing money.

    Default ON, but see _warm_learned_flags_on: with nothing learned running the
    loop returns before reading anything, so the default costs nothing and
    changes nothing.

    THE NET IS MEASURED, NOT SELF-REPORTED. warm_guardrail_scan differences the
    randomized control arm's lift against what warming actually cost; it does
    not read the policy's own realized_net_usd attribution, which comes back
    only as the policy_net_7d_usd diagnostic logged beside it.

    AND IT NEEDS EVIDENCE. A pair with no control rows differences zero lift
    against real spend and looks catastrophic when it is simply unmeasured, so
    a freeze requires at least three control-measured days
    (BREVITAS_WARM_GUARDRAIL_MIN_CONTROL_DAYS, default 3). Below that the pair
    is skipped, which is the conservative direction for a switch that only a
    human can undo.

    THE BASELINE IS A PLACEHOLDER, and deliberately a visible one. The plan's
    gate is `net_7d < (1 - alpha) * baseline_v1`, where baseline_v1 is the v1
    policy's own control-measured trailing net. That number does not exist until
    the holdout arm has run long enough to produce it, so the comparison
    degenerates to a ZERO FLOOR: freeze when the measured net is worse than
    losing BREVITAS_WARM_GUARDRAIL_MIN_USD. That is strictly more conservative
    than the plan's gate whenever baseline_v1 >= 0 and strictly less
    conservative when it is negative -- i.e. it under-freezes only when v1 was
    ALSO losing money, which is a decision for a human, not this loop.

    Unfreezing is manual: call warm_org_mode_set. A guardrail that unfreezes
    itself oscillates.
    """
    if not _warming_enabled() or not _warm_learned_flags_on():
        return
    if os.getenv("BREVITAS_WARM_GUARDRAIL", "true").lower() not in (
            "1", "true", "yes"):
        return
    interval = _warm_bound("BREVITAS_WARM_GUARDRAIL_INTERVAL_SECONDS",
                           3_600, 60, 86_400)
    min_usd = _warm_bound("BREVITAS_WARM_GUARDRAIL_MIN_USD", 0.10, 0.0, 1_000_000.0)
    min_control_days = int(_warm_bound(
        "BREVITAS_WARM_GUARDRAIL_MIN_CONTROL_DAYS", 3, 1, 90))
    baseline_v1 = 0.0
    while not stop.is_set():
        if _WORKER_ACCEPTING:
            try:
                for pair in await asyncio.to_thread(_store.warm_guardrail_scan, 7):
                    organization_id = str(pair.get("organization_id") or "")
                    provider = str(pair.get("provider") or "")
                    net7 = float(pair.get("net_7d_usd") or 0.0)
                    control_days = int(pair.get("control_days") or 0)
                    if str(pair.get("mode") or "learned") == "frozen":
                        logger.info("warm_guardrail_already_frozen",
                                    organization_id=organization_id,
                                    provider=provider, net_7d_usd=net7)
                        continue
                    if net7 >= baseline_v1 - min_usd:
                        continue
                    # No experiment, no freeze. An unmeasured pair reads as a
                    # total loss (zero measured lift against real spend), and
                    # freezing on that would punish a pair for not having run
                    # the holdout long enough yet.
                    if control_days < min_control_days:
                        logger.info("warm_guardrail_unmeasured",
                                    organization_id=organization_id,
                                    provider=provider, net_7d_usd=net7,
                                    control_days=control_days)
                        continue
                    await asyncio.to_thread(
                        _store.warm_org_mode_set, organization_id, provider,
                        "frozen",
                        f"guardrail net7={net7:.4f} control_days={control_days}")
                    logger.error("warm_guardrail_frozen",
                                 organization_id=organization_id,
                                 provider=provider, net_7d_usd=net7,
                                 control_days=control_days,
                                 policy_net_7d_usd=float(
                                     pair.get("policy_net_7d_usd") or 0.0))
            except Exception as exc:
                logger.error("warm_guardrail_error", error_type=type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


# The canary's vocabulary. Sixty-four ordinary English words, fixed forever, so
# a probe prefix is a pure function of its seed and CANNOT contain customer data
# by construction rather than by policy. Nothing here is read from a request, a
# database or an environment variable.
_CANARY_WORDS: tuple[str, ...] = (
    "harbor", "lantern", "meadow", "cobalt", "thistle", "quarry", "ember",
    "willow", "cinder", "marble", "trellis", "pebble", "aurora", "compass",
    "gallery", "hollow", "juniper", "kettle", "lattice", "mosaic", "nectar",
    "obsidian", "plateau", "quiver", "ribbon", "saffron", "tundra", "umbrella",
    "velvet", "wander", "yonder", "zephyr", "anchor", "beacon", "canyon",
    "driftwood", "estuary", "fathom", "granite", "heather", "island", "jasper",
    "kindling", "lagoon", "mariner", "northerly", "orchard", "prairie",
    "quartz", "rivulet", "seagrass", "timber", "upland", "vessel", "waterway",
    "yarrow", "almanac", "bramble", "cascade", "dunes", "eddy", "foghorn",
    "glacier", "highland",
)
# Minimum cacheable prefix per provider. Anthropic's haiku tier will not create
# a cache entry below 4096 tokens at all, so a shorter probe measures nothing;
# 4600 leaves headroom over the words x 1.35 estimate. DeepSeek's automatic
# prefix cache works from 512.
_CANARY_MIN_TOKENS: dict[str, int] = {"anthropic": 4600, "deepseek": 512}
# The gap ladder, as multiples of the believed TTL. Straddling 1.0 is the point:
# a censored observation below the belief and one above it bound the true TTL
# from both sides, which one-sided probing never does.
_CANARY_LADDER: tuple[float, ...] = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
# A LITERAL PAIR, asserted at the top of the loop. OpenAI stays
# measurement-only, so no BREVITAS_PROBE_OPENAI_KEY in the environment can make
# the canary reach it.
_CANARY_PROVIDERS: tuple[str, str] = ("anthropic", "deepseek")
# Believed TTL and the tier warm_ttl_observations records it under.
_CANARY_TTL: dict[str, tuple[int, str]] = {
    "anthropic": (300, "5m"),
    "deepseek": (14_400, "auto"),
}


def _canary_prefix(provider: str, seed: str) -> tuple[str, int]:
    """Build a probe prefix and its estimated token count from a seed.

    Deterministic: the same seed always yields the same text, which is what lets
    the probe leg rebuild the write leg's exact bytes without storing them.
    """
    rng = random.Random(seed)
    target_tokens = _CANARY_MIN_TOKENS.get(provider, 512)
    # ~1.35 tokens per word is the conservative direction: it UNDER-counts, so
    # the prefix ends up longer than the minimum rather than shorter, and a
    # prefix below the provider minimum caches nothing and measures nothing.
    words_needed = int(target_tokens / 1.35) + 1
    lines: list[str] = []
    used = 0
    index = 0
    while used < words_needed:
        span = rng.sample(_CANARY_WORDS, 12)
        lines.append(f"Brevitas TTL canary block {index}: " + " ".join(span) + ".")
        used += len(span) + 5
        index += 1
    text = "\n".join(lines)
    return text, int(len(text.split()) * 1.35)


def _canary_body(provider: str, model: str, prefix: str) -> dict:
    if provider == "anthropic":
        return {
            "model": model,
            "max_tokens": 1,
            "system": [{"type": "text", "text": prefix,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user",
                          "content": [{"type": "text", "text": "."}]}],
            "metadata": {"user_id": "brevitas-ttl-canary"},
        }
    return {
        "model": model,
        "max_tokens": 1,
        "stream": False,
        "messages": [{"role": "system", "content": prefix},
                     {"role": "user", "content": "."}],
    }


def _canary_headers(provider: str, key: str) -> dict:
    if provider == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01",
                "content-type": "application/json"}
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _canary_key(provider: str) -> str:
    return os.getenv(f"BREVITAS_PROBE_{provider.upper()}_KEY", "").strip()


def _canary_model(provider: str) -> str:
    default = "claude-haiku-4-5" if provider == "anthropic" else "deepseek-chat"
    return os.getenv(
        f"BREVITAS_TTL_CANARY_MODEL_{provider.upper()}", default).strip() or default


def _canary_estimate_usd(provider: str, model: str, prefix_tokens: int) -> float | None:
    """Pessimistic cost of one probe call: the whole prefix at input price with a
    1.5x write-premium margin. None when the model is unpriced, which disables
    that provider's canary rather than spending blind."""
    price = model_price(provider, model)
    if not price:
        return None
    return round(float(price.get("input") or 0.0) * prefix_tokens / 1_000_000.0
                 * 1.5, 10)


def _canary_actual_usd(provider: str, model: str, receipt: Any,
                       estimate: float) -> float:
    """What the call actually cost, or the reservation when the receipt cannot
    say. Refining downward on an unreadable body would release the whole
    reservation and the daily cap would stop binding on exactly the calls it
    cannot account for -- the rule _warm_one already applies to tenant pings."""
    costs = calculate_costs(provider, model, receipt.input_tokens, receipt)
    if receipt.total_tokens and str(costs.get("pricing_status")) == "priced":
        return float(costs.get("actual_cost_usd") or 0.0)
    return float(estimate)


async def _canary_send(provider: str, model: str, prefix: str) -> tuple[int, dict]:
    spec = WARM_PROVIDER_SPECS[provider]
    return await asyncio.to_thread(
        _send_warm_ping, provider,
        {"endpoint_url": spec["endpoint_url"], "operation": spec["operation"]},
        _canary_body(provider, model, prefix),
        _canary_headers(provider, _canary_key(provider)))


async def warm_ttl_canary(stop: asyncio.Event) -> None:
    """Measure provider cache TTL on Brevitas's own dime.

    OFF by default. The job writes a synthetic prefix with a Brevitas-owned
    probe key, waits a gap drawn from a ladder around the believed TTL, replays
    the identical bytes and reads the provider's cache legs back: a cache-read
    leg means the entry survived the gap ('warm'), a cache-write leg means it
    did not ('expired'). Both are censored observations of the same unknown, and
    warm_ttl_observations already knows how to hold them.

    WHAT IT MAY TOUCH: warm_ttl_observations (source 'canary') and the two
    canary tables. Nothing else -- no usage_log row, no warm_budget_ledger, no
    warm_customer_budget, no tenant credential. It spends Brevitas's money
    against Brevitas's account, fenced by a daily dollar cap that is reserved
    before the request leaves.

    OPENAI IS STRUCTURALLY UNREACHABLE: the provider tuple is a literal pair, so
    no environment variable can widen it.
    """
    if not _warming_enabled() or os.getenv(
            "BREVITAS_TTL_CANARY", "false").lower() not in ("1", "true", "yes"):
        return
    assert _CANARY_PROVIDERS == ("anthropic", "deepseek")
    interval = _warm_bound("BREVITAS_TTL_CANARY_INTERVAL_SECONDS", 300, 30, 3_600)
    daily_cap = _warm_bound("BREVITAS_TTL_CANARY_DAILY_USD", 0.25, 0.0, 100.0)
    while not stop.is_set():
        if _WORKER_ACCEPTING:
            for provider in _CANARY_PROVIDERS:
                try:
                    await _canary_cycle(provider, daily_cap)
                except Exception as exc:
                    logger.error("warm_ttl_canary_error", provider=provider,
                                 error_type=type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


async def _canary_cycle(provider: str, daily_cap: float) -> None:
    """One provider's turn: fire every due probe, then start one experiment if
    none is in flight."""
    if not _canary_key(provider):
        # No probe key is not an error: the canary is Brevitas-funded and a
        # deployment that has not been given a key simply does not run it.
        return
    model = _canary_model(provider)
    day = datetime.now(timezone.utc).date().isoformat()
    ttl_seconds, ttl_tier = _CANARY_TTL[provider]

    for probe in await asyncio.to_thread(_store.warm_canary_probe_due, provider, 50):
        prefix, _tokens = _canary_prefix(provider, str(probe.get("prefix_seed") or ""))
        estimate = _canary_estimate_usd(
            provider, model, int(probe.get("prefix_tokens") or 0))
        if estimate is None:
            logger.warning("warm_ttl_canary_unpriced_model", provider=provider,
                           model=model)
            return
        reservation = await asyncio.to_thread(
            _store.warm_canary_reserve, provider, day, estimate, daily_cap)
        if not reservation.get("allowed"):
            # Left pending on purpose: the experiment is still valid, it just
            # has to wait for tomorrow's cap. A probe fired late measures a
            # longer gap than it targeted, which is why the gap recorded below
            # is the MEASURED one, never the target.
            return
        try:
            status, data = await _canary_send(provider, model, prefix)
        except Exception as exc:
            # The estimate stays booked. A transport failure after a POST is
            # ambiguous -- the provider may have run and billed it -- and the
            # conservative reading of an ambiguous spend is that it happened.
            await asyncio.to_thread(
                _store.warm_canary_probe_mark, int(probe.get("id") or 0), "failed")
            logger.warning("warm_ttl_canary_send_failed", provider=provider,
                           error_type=type(exc).__name__)
            return
        if not 200 <= int(status) < 300:
            await asyncio.to_thread(
                _store.warm_canary_probe_mark, int(probe.get("id") or 0), "failed")
            logger.warning("warm_ttl_canary_http_error", provider=provider,
                           status=int(status))
            return
        receipt = normalize_usage(data.get("usage"), provider)
        outcome = _warm_ttl_outcome(receipt)
        gap = _canary_gap_seconds(probe)
        if outcome and gap is not None:
            await asyncio.to_thread(
                _store.warm_ttl_observe, provider, warm_model_class(model),
                ttl_tier, gap, outcome, "canary")
        await asyncio.to_thread(
            _store.warm_canary_probe_mark, int(probe.get("id") or 0), "done")
        # Refine the estimate DOWNWARD only on a parseable, priced receipt --
        # the same rule _warm_one applies for the same reason. Booking 0 for an
        # unreadable 2xx body would release the whole reservation, so the daily
        # cap would never bind on exactly the calls it cannot account for.
        await asyncio.to_thread(
            _store.warm_canary_settle, provider, day, estimate,
            _canary_actual_usd(provider, model, receipt, estimate))

    stats = await asyncio.to_thread(_store.warm_canary_probe_stats, provider)
    if int(stats.get("pending") or 0) > 0:
        return
    # A new experiment. The ladder index advances on MEASUREMENT, not on
    # attempts, so a run of failures cannot walk the ladder past the gaps that
    # actually bound the TTL.
    done = int(stats.get("done") or 0)
    gap_target = _CANARY_LADDER[done % len(_CANARY_LADDER)] * ttl_seconds
    seed = f"{provider}:{day}:{done}"
    prefix, prefix_tokens = _canary_prefix(provider, seed)
    estimate = _canary_estimate_usd(provider, model, prefix_tokens)
    if estimate is None:
        logger.warning("warm_ttl_canary_unpriced_model", provider=provider,
                       model=model)
        return
    reservation = await asyncio.to_thread(
        _store.warm_canary_reserve, provider, day, estimate, daily_cap)
    if not reservation.get("allowed"):
        return
    written_at = datetime.now(timezone.utc)
    try:
        status, data = await _canary_send(provider, model, prefix)
    except Exception as exc:
        logger.warning("warm_ttl_canary_write_failed", provider=provider,
                       error_type=type(exc).__name__)
        return
    if not 200 <= int(status) < 300:
        logger.warning("warm_ttl_canary_write_http_error", provider=provider,
                       status=int(status))
        return
    receipt = normalize_usage(data.get("usage"), provider)
    await asyncio.to_thread(
        _store.warm_canary_settle, provider, day, estimate,
        _canary_actual_usd(provider, model, receipt, estimate))
    await asyncio.to_thread(
        _store.warm_canary_probe_insert, provider, model, seed, prefix_tokens,
        written_at.isoformat(), float(gap_target))
    logger.info("warm_ttl_canary_started", provider=provider,
                gap_target_s=float(gap_target), prefix_tokens=prefix_tokens)


def _canary_gap_seconds(probe: Mapping[str, Any]) -> float | None:
    """The gap the probe ACTUALLY measured, not the one it targeted. A probe
    fired late by a cap denial or a slow cycle tested a longer gap, and the
    physics table must record what happened."""
    raw = str(probe.get("written_at") or "")
    if not raw:
        return None
    try:
        written = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    gap = (datetime.now(timezone.utc) - written).total_seconds()
    return gap if 0 <= gap <= _WARM_TTL_MAX_GAP_SECONDS else None


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
    # Redis (dispatcher.wait_for_notification) is the low-latency wake-up for
    # new jobs; this interval only paces the Postgres fallback poll that covers
    # missed notifications and expired-lease reclaims. At 0.25s the idle fleet
    # was ~50 claim_ai_job RPCs/sec (~4.3M/day) against the shared database.
    poll_seconds = max(0.05, float(os.getenv("BREVITAS_JOB_POLL_SECONDS", "5")))
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
        asyncio.create_task(warm_reward_join(stop), name="worker-warm-reward-join"),
        asyncio.create_task(warm_regime_refresh(stop), name="worker-warm-regime"),
        asyncio.create_task(warm_envelope_allocator(stop),
                            name="worker-warm-envelope-allocator"),
        asyncio.create_task(warm_control_savings(stop),
                            name="worker-warm-control-savings"),
        asyncio.create_task(warm_attribution(stop),
                            name="worker-warm-attribution"),
        asyncio.create_task(warm_guardrail(stop), name="worker-warm-guardrail"),
        asyncio.create_task(warm_ttl_canary(stop), name="worker-warm-ttl-canary"),
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
