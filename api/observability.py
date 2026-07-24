"""FastAPI and durable-worker integration for content-free observability."""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from brevitas.observability import (
    REQUEST_ID_COMPAT_HEADER,
    REQUEST_ID_HEADER,
    StructuredLogger,
    configure_json_logging,
    correlation_context,
    documented_upstream_outage_active,
    fault_domain,
    get_runtime,
    job_context,
    normalize_request_id,
    provider_correlation_headers,
    route_label,
    shutdown_observability,
)


log = StructuredLogger("brevitas.api")


def _incoming_request_id(request: Request) -> str:
    for header in (REQUEST_ID_HEADER, REQUEST_ID_COMPAT_HEADER, "X-Client-Request-ID"):
        candidate = request.headers.get(header)
        if candidate:
            return normalize_request_id(candidate)
    return normalize_request_id("")


def _resolved_route(scope: Scope) -> str:
    route = scope.get("route")
    return route_label(getattr(route, "path", ""), registered=True)


def mark_request_fault_domain(request: Request, domain: str) -> None:
    """Mark customer-owned exclusions; ordinary upstream faults remain Brevitas-owned."""
    classified = fault_domain(domain)
    if classified == "documented_upstream_outage":
        classified = "brevitas"
    request.state.brevitas_fault_domain = classified


def mark_documented_upstream_outage(request: Request, provider: str) -> bool:
    """Apply the upstream exclusion only while an ops-referenced outage gate is active."""
    active = documented_upstream_outage_active(provider)
    request.state.brevitas_fault_domain = (
        "documented_upstream_outage" if active else "brevitas"
    )
    return active


class RequestObservabilityMiddleware:
    """ASGI middleware retaining correlation/traces through the final response byte."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        request_id = _incoming_request_id(request)
        method = str(scope.get("method") or "")
        started = time.perf_counter()
        status_code = 500
        response_started = False
        runtime = get_runtime(default_service="api")
        scope.setdefault("state", {})["brevitas_request_id"] = request_id

        async def correlated_send(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                headers[REQUEST_ID_COMPAT_HEADER] = request_id
            await send(message)

        with correlation_context(request_id=request_id):
            with runtime.span("http.server.request", {"http.request.method": method}):
                try:
                    await self.app(scope, receive, correlated_send)
                except Exception as exc:
                    status_code = 500
                    log.error(
                        "api_request_failed",
                        method=method,
                        route=_resolved_route(scope),
                        status_code=500,
                        error_type=type(exc).__name__,
                    )
                    if response_started:
                        # Headers already contain correlation; the server terminates the
                        # partial stream and handles transport cleanup.
                        raise
                    response = JSONResponse(
                        status_code=500,
                        content={"detail": "Internal server error"},
                    )
                    await response(scope, receive, correlated_send)
                finally:
                    duration = time.perf_counter() - started
                    route = _resolved_route(scope)
                    domain = fault_domain(
                        scope.get("state", {}).get("brevitas_fault_domain", "brevitas")
                    )
                    runtime.metrics.record_api_request(
                        duration_seconds=duration,
                        method=method,
                        route=route,
                        status_code=status_code,
                        fault=domain,
                    )
                    runtime.metrics.record_service_operation(
                        service="api",
                        outcome="server_error" if status_code >= 500 else "success",
                    )
                    log.info(
                        "api_request_completed",
                        method=method,
                        route=route,
                        status_code=status_code,
                        duration_ms=duration * 1000,
                        outcome=("server_error" if status_code >= 500 else
                                 "client_error" if status_code >= 400 else "success"),
                    )


def install_fastapi_observability(app: FastAPI, *, configure_logs: bool = True) -> None:
    """Install once during app construction; call shutdown from the owning lifespan."""
    if getattr(app.state, "brevitas_observability_installed", False):
        return
    if configure_logs:
        configure_json_logging(service="api", logger_names=("brevitas.api",))
    app.add_middleware(RequestObservabilityMiddleware)
    app.state.brevitas_observability_installed = True


def outbound_provider_headers(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    """Provider clients call this immediately before a request to inject correlation."""
    return provider_correlation_headers(headers)


@contextmanager
def observe_provider_call(
    provider: str, operation: str, *, attempt: int = 1,
) -> Iterator[None]:
    """Measure a provider attempt while preserving the original application exception."""
    runtime = get_runtime(default_service="api")
    started = time.perf_counter()
    outcome = "success"
    with runtime.span("provider.request"):
        try:
            yield
        except TimeoutError:
            outcome = "timeout"
            raise
        except Exception as exc:
            outcome = "circuit_open" if type(exc).__name__ == "ProviderCircuitOpen" else "error"
            raise
        finally:
            runtime.metrics.record_provider(
                provider=provider,
                operation=operation,
                outcome=outcome,
                duration_seconds=time.perf_counter() - started,
                attempt=attempt,
            )


@contextmanager
def observe_job(job_id: str, operation: str) -> Iterator[None]:
    """Bind a durable job ID and emit one terminal, content-free measurement."""
    runtime = get_runtime(default_service="worker")
    started = time.perf_counter()
    status = "succeeded"
    with job_context(job_id):
        with runtime.span("job.process"):
            try:
                yield
            except Exception:
                status = "failed"
                raise
            finally:
                runtime.metrics.record_job(
                    operation=operation,
                    status=status,
                    duration_seconds=time.perf_counter() - started,
                )
                log.info(
                    "job_completed",
                    operation=operation,
                    outcome="success" if status == "succeeded" else "failed",
                    duration_ms=(time.perf_counter() - started) * 1000,
                )


class BillingTelemetryAdapter:
    """Implements ``api.billing_recovery.BillingTelemetry`` using fixed instruments."""

    def metric(
        self, name: str, value: float, attributes: Mapping[str, str] | None = None,
    ) -> None:
        get_runtime(default_service="billing-worker").metrics.record_billing_metric(
            name, value, attributes
        )

    def alert(self, name: str, severity: str, fields: Mapping[str, int]) -> None:
        # Alert payload values are represented by fixed gauges; no arbitrary fields are logged.
        metrics = get_runtime(default_service="billing-worker").metrics
        if name == "billing_processing_lag":
            metrics._emit(
                metrics.billing_queue_lag, "set",
                max(0, int(fields.get("oldest_pending_seconds", 0))),
            )
        elif name == "billing_entries_require_review":
            metrics._emit(
                metrics.billing_review, "set", max(0, int(fields.get("review_count", 0)))
            )
        elif name == "billing_entries_dead":
            metrics._emit(
                metrics.billing_dead, "set", max(0, int(fields.get("dead_count", 0)))
            )
        elif name == "billing_stale_leases":
            metrics._emit(
                metrics.billing_stale, "set",
                max(0, int(fields.get("stale_sending_count", 0))),
            )
        elif name == "billing_catalog_contract_invalid":
            metrics._emit(metrics.billing_catalog_contract, "set", 0)
        log.warning("billing_alert", alert=name, severity=severity, billing_status="degraded")


def graceful_observability_shutdown() -> None:
    """Owning API/worker lifespans call this after work and clients finish draining."""
    shutdown_observability()


__all__ = [
    "BillingTelemetryAdapter", "graceful_observability_shutdown",
    "install_fastapi_observability", "mark_documented_upstream_outage",
    "mark_request_fault_domain", "observe_job", "observe_provider_call",
    "outbound_provider_headers", "RequestObservabilityMiddleware",
]
