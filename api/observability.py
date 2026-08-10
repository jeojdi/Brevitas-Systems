"""FastAPI and durable-worker integration for content-free observability."""
from __future__ import annotations

import threading
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
    sla_eligible_fault,
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


# ── Prometheus scrape mirror ─────────────────────────────────────────────────
# observability/prometheus/alerts.yml carries 25 rules and every one of them
# reads a `brevitas_*` series, but nothing in this repository ever published a
# scrape endpoint: the OTel path is the only exporter and it needs
# BREVITAS_OTEL_ENABLED plus a collector, which no deploy artifact sets. Every
# rule has therefore been evaluating absent series since the day it was written,
# and the burn-rate rules divide by clamp_min(), so absent reads as "healthy"
# rather than as "blind" (docs/SECURITY_AUDIT_2026-07-30.md, finding #1).
#
# This is a MIRROR, not a replacement. The OTel instruments stay exactly as they
# are; these counters are incremented from the same chokepoints and rendered by
# GET /metrics so a Prometheus that cannot reach an OTLP collector still has the
# series the alert rules most depend on. It is deliberately not the
# prometheus_client library: adding a dependency to publish six counter families
# is a worse trade than the exposition format below, and the process must keep
# starting when that library is absent.
#
# PROCESS-LOCAL AND MONOTONIC. Each replica counts what it served since its own
# boot, which is what a Prometheus counter means; rate()/increase() handle the
# restart reset. Nothing here is persisted and nothing is per-tenant: the label
# sets are copied from brevitas.observability.Metrics so the mirrored series and
# the OTel series can never disagree about a partition.
_PROM_HELP: dict[str, tuple[str, str]] = {
    "brevitas_api_requests_total": (
        "counter", "HTTP requests served by this API replica."),
    "brevitas_service_operations_total": (
        "counter", "Internal service operations attempted by this replica."),
    "brevitas_billing_savings_rows_total": (
        "counter", "Usage rows persisted, split by authority and billability."),
    "brevitas_billing_verified_savings_usd_total": (
        "counter", "Verified savings dollars persisted, split by authority."),
    "brevitas_warm_pings_total": (
        "counter", "Cache-warming keep-alive pings settled by this process."),
    "brevitas_warm_spend_usd_total": (
        "counter", "Dollars spent on cache-warming pings by this process."),
}
# A hard ceiling on distinct label combinations. Every label written here is
# already drawn from a finite vocabulary (route_label fails closed to
# "unmatched"), so this can only be reached by a defect -- and when it is, the
# mirror stops growing instead of becoming the memory leak.
_PROM_MAX_SERIES = 2048
_prom_lock = threading.Lock()
_prom_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}


def _prom_add(name: str, labels: Mapping[str, str], value: float = 1.0) -> None:
    """Increment one mirrored counter. Never raises: telemetry is not the work."""
    try:
        amount = float(value)
        if amount != amount or amount in (float("inf"), float("-inf")):
            return
        # A counter that can go down is a lie. Clamp rather than drop, so the
        # series still exists at zero -- an absent series and a zero one are
        # different answers to an alert rule, and only one of them is true.
        amount = max(0.0, amount)
        key = (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))
        with _prom_lock:
            if key not in _prom_counters and len(_prom_counters) >= _PROM_MAX_SERIES:
                return
            _prom_counters[key] = _prom_counters.get(key, 0.0) + amount
    except Exception:
        return


def _prom_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus_text() -> str:
    """Render the mirrored counters in Prometheus text exposition format 0.0.4.

    Families are emitted with their HELP/TYPE header even when they hold no
    series yet, so a rule reading `brevitas_billing_savings_rows_total` sees a
    family that exists and is zero rather than one that is absent -- the same
    distinction Metrics.record_savings_row's docstring exists to preserve.
    """
    with _prom_lock:
        snapshot = dict(_prom_counters)
    lines: list[str] = []
    for name, (kind, help_text) in _PROM_HELP.items():
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        series = sorted((labels, total) for (metric, labels), total
                        in snapshot.items() if metric == name)
        if not series:
            lines.append(f"{name} 0")
            continue
        for labels, total in series:
            if not labels:
                # An unlabelled sample renders bare. `name{} 1` is legal
                # exposition format but several scrapers and every human reader
                # treat the empty braces as a mistake.
                lines.append(f"{name} {total!r}")
                continue
            rendered = ",".join(f'{key}="{_prom_escape(val)}"' for key, val in labels)
            lines.append(f"{name}{{{rendered}}} {total!r}")
    return "\n".join(lines) + "\n"


def reset_prometheus_mirror() -> None:
    """Test-only: drop every mirrored series so a case starts from a known zero."""
    with _prom_lock:
        _prom_counters.clear()


def record_warm_ping(*, outcome: str, spent_usd: float = 0.0) -> None:
    """Mirror one settled warming ping. Called from the worker's settle path.

    Warming pings execute in api/worker.py, a different process from the API, so
    these two series are non-zero only on a process that runs the warming loop.
    That is a property of the deployment topology, not of this counter: a
    Prometheus scraping both processes sums them, and a scrape of the API alone
    correctly reports that the API served no pings.
    """
    safe = str(outcome or "unknown").lower()
    if safe not in {"warmed", "skipped", "failed", "spent_unknown", "expired"}:
        safe = "unknown"
    _prom_add("brevitas_warm_pings_total", {"outcome": safe})
    if spent_usd:
        _prom_add("brevitas_warm_spend_usd_total", {}, spent_usd)


def record_savings_row(
    *, authoritative: bool, billable: bool,
    verified_savings_usd: float | None = None,
) -> None:
    """Count each usage row the control plane actually persisted.

    Every other billing alert is a "too much bad" rule and reads green when the
    money path produces nothing at all; this is the "too little good" series, and
    the two labels separate "traffic stopped" from "traffic continued but nothing
    was billable". Routed through the Metrics facade, which swallows exporter
    faults — a receipt must never fail because telemetry is down. Deliberately
    carries no tenant or key label.

    ``verified_savings_usd`` is the magnitude half: rows alone stay green while
    the pipeline emits rows worth nothing (the 2026-07-29 hand-repricing shape).
    Passing ``None`` leaves the dollar series absent rather than publishing a
    fleet-wide $0; the facade coerces NaN/inf/negative/non-numeric values, so no
    validation is needed here.
    """
    get_runtime(default_service="api").metrics.record_savings_row(
        authoritative=authoritative, billable=billable,
        verified_savings_usd=verified_savings_usd,
    )
    # Same call, same labels, into the scrape mirror. Strictly after the facade
    # so a defect here can never precede the instrument the collector reads.
    _prom_add("brevitas_billing_savings_rows_total", {
        "authoritative": "true" if authoritative else "false",
        "billable": "true" if billable else "false",
    })
    if verified_savings_usd is not None:
        # Raw, not coerced here: _prom_add owns the float()/NaN/negative handling
        # inside its own guard, so a malformed amount cannot raise on the money
        # path the way a caller-side float() would.
        _prom_add("brevitas_billing_verified_savings_usd_total",
                  {"authoritative": "true" if authoritative else "false"},
                  verified_savings_usd)


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
                    # Scrape mirror. The outcome partition is recomputed with the
                    # SAME rule Metrics.record_api_request applies -- including
                    # the fault-domain split of 5xx into server_error vs
                    # unavailable, which the log line below deliberately does not
                    # make -- because the SLO burn rules select on
                    # outcome="server_error" and sla_eligible="true" and would
                    # otherwise count a provider outage against Brevitas's budget.
                    if status_code >= 500:
                        prom_outcome = ("server_error" if domain == "brevitas"
                                        else "unavailable")
                    elif status_code in (401, 403):
                        prom_outcome = "auth_denied"
                    elif status_code >= 400:
                        prom_outcome = "client_error"
                    else:
                        prom_outcome = "success"
                    _prom_add("brevitas_api_requests_total", {
                        "method": method.upper() or "OTHER",
                        # `route` is already a route_label() result (see
                        # _resolved_route), so it is a template or "unmatched".
                        "route": route,
                        "outcome": prom_outcome,
                        "surface": "external",
                        "fault_domain": domain,
                        "sla_eligible": ("true" if sla_eligible_fault(domain)
                                         else "false"),
                    })
                    _prom_add("brevitas_service_operations_total", {
                        "service": "api", "surface": "internal",
                        "outcome": ("server_error" if status_code >= 500
                                    else "success"),
                    })
                    log.info(
                        "api_request_completed",
                        method=method,
                        route=route,
                        status_code=status_code,
                        duration_ms=duration * 1000,
                        # Same partition Metrics.record_api_request uses, so a log
                        # line and its metric series never disagree about whether a
                        # 401/403 was a malformed request or a rejected credential.
                        outcome=("server_error" if status_code >= 500 else
                                 "auth_denied" if status_code in (401, 403) else
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
    "outbound_provider_headers", "record_savings_row", "record_warm_ping",
    "render_prometheus_text", "reset_prometheus_mirror",
    "RequestObservabilityMiddleware",
]
