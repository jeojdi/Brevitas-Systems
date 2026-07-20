"""Durable Brevitas job worker.

Run separately from the API replicas:

    python -m api.worker

The database lease is the recovery mechanism. Killing this process leaves the
job reclaimable after BREVITAS_JOB_LEASE_SECONDS.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import os
import signal
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .distributed_limits import LimitIdentity, LimiterUnavailable
from .server import (
    AuthContext,
    _compress_pipeline,
    _distributed_limiter,
    _job_service,
    _resolve_configured_model_backend,
    _run_configured_model,
    _safe_record_usage,
    _store,
    _wait_for_provider_calls,
    _initialize_credential_cipher,
    _authoritative_service_key_context,
    _configure_managed_kms_from_deployment,
    _production_runtime,
)
from .billing_recovery import (
    billing_recovery_is_configured,
    build_billing_recovery_processor_from_env,
    run_billing_recovery_loop,
)
from .jobs import PermanentJobError
from .observability import (
    BillingTelemetryAdapter,
    graceful_observability_shutdown,
    observe_job,
)
from brevitas.observability import StructuredLogger, configure_json_logging
from brevitas.provider_reliability import close_provider_sync_clients
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


def _billing_loop_ready() -> bool:
    if not _BILLING_REQUIRED:
        return True
    return _billing_health_status()[0]


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


@health_app.get("/ready")
@health_app.get("/health")
async def readiness():
    database_ready, redis_ready = await _dependencies_ready()
    if _BILLING_REQUIRED:
        billing_ready, billing_health = _billing_health_status()
    else:
        billing_ready, billing_health = True, {
            "running": _BILLING_LOOP_RUNNING,
            "initial_validation_succeeded": False,
            "catalog_valid": False,
            "last_success_fresh": False,
            "last_success_age_seconds": None,
            "consecutive_errors": 0,
            "error_threshold_exceeded": False,
        }
    ready = _WORKER_ACCEPTING and database_ready and redis_ready and billing_ready
    payload = {
        "status": "ok" if ready else "unavailable",
        "accepting_jobs": _WORKER_ACCEPTING,
        "dependencies": {
            "postgres": {"status": "ready" if database_ready else "unavailable",
                         "authoritative": True},
            "redis": {"status": "ready" if redis_ready else "unavailable",
                      "authoritative": False, "role": "coordination"},
            "billing_recovery": {
                "status": ("disabled" if _BILLING_ROLE == "nonbilling" else
                           "ready" if billing_ready else "unavailable"),
                "authoritative": _BILLING_REQUIRED,
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
                    customer_id=row["customer_id"], key_type=str(key_context.get("key_type") or ""),
                ),
                key_hash=row["key_hash"], baseline_tokens=result["baseline_tokens"],
                optimized_tokens=result["optimized_tokens"], savings_pct=result["savings_pct"],
                quality_proxy=None, strategy="job:compress", receipt_source="worker",
                request_id=f"job:{row['id']}", provider="brevitas", model="compression",
            )
            return output
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


async def run() -> None:
    global _WORKER_ACCEPTING, _BILLING_ROLE, _BILLING_REQUIRED
    global _BILLING_CONFIGURED, _BILLING_LOOP_RUNNING
    worker_id = os.getenv("BREVITAS_WORKER_ID") or f"worker_{uuid.uuid4().hex[:16]}"
    concurrency = max(1, min(100, int(os.getenv("BREVITAS_WORKER_CONCURRENCY", "10"))))
    poll_seconds = max(0.05, float(os.getenv("BREVITAS_JOB_POLL_SECONDS", "0.25")))
    stop = asyncio.Event()
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
    billing_failed = False
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
        billing_task = asyncio.create_task(
            run_billing_recovery_loop(billing_processor, stop, **billing_kwargs),
            name="billing-recovery",
        )
        _BILLING_LOOP_RUNNING = True

        def billing_finished(_task: asyncio.Task) -> None:
            global _BILLING_LOOP_RUNNING
            nonlocal billing_failed
            _BILLING_LOOP_RUNNING = False
            with _BILLING_HEALTH_LOCK:
                stopped_health = dict(_BILLING_HEALTH)
            stopped_health["running"] = False
            _report_billing_health(stopped_health)
            if not stop.is_set():
                billing_failed = True
                try:
                    failure = _task.exception()
                except asyncio.CancelledError:
                    failure = None
                logger.error("billing_loop_stopped", outcome="failed")
                if failure is not None:
                    logger.error("billing_loop_failed", outcome="failed",
                                 error_type=type(failure).__name__)
                stop.set()

        billing_task.add_done_callback(billing_finished)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    health_config = uvicorn.Config(
        health_app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        access_log=False,
        log_level=os.getenv("BREVITAS_LOG_LEVEL", "info").lower(),
    )
    health_server = uvicorn.Server(health_config)
    # This process owns signal handling so it can drain leased work before stopping Uvicorn.
    health_server.install_signal_handlers = lambda: None
    health_task = asyncio.create_task(health_server.serve(), name="worker-health-server")
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
            _WORKER_ACCEPTING = (
                database_ready and redis_ready and _billing_loop_ready())
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
                await asyncio.wait_for(stop.wait(), timeout=300)
            except TimeoutError:
                pass

    database_ready, redis_ready = await _dependencies_ready()
    _WORKER_ACCEPTING = database_ready and redis_ready and _billing_loop_ready()
    tasks = [
        asyncio.create_task(dependency_monitor(), name="worker-dependency-monitor"),
        asyncio.create_task(maintenance(), name="worker-maintenance"),
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
        if billing_task is not None:
            # The billing loop shields bounded thread work and releases its leases.
            # It must finish naturally; cancellation could duplicate an in-flight send.
            await billing_task
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
    if billing_failed:
        raise RuntimeError("authoritative billing recovery loop stopped unexpectedly")


if __name__ == "__main__":
    asyncio.run(run())
