"""Content-free observability primitives shared by API and worker processes.

The module is deliberately safe to import without the OpenTelemetry SDK.  When
telemetry is disabled (the default), or the optional SDK is unavailable, every
span and metric operation is a bounded no-op.  Application work must never
depend on an exporter being healthy.
"""
from __future__ import annotations

import atexit
import inspect
import json
import logging
import os
import re
import secrets
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Mapping

try:
    from brevitas.security.redaction import redact as security_redact
    from brevitas.security.redaction import redact_exception as security_redact_exception
except ImportError:  # Private compressor image currently copies this module alone.
    def security_redact(
        value: object, *, safe_fields: object = None,
        max_depth: int = 2, max_items: int = 64,
    ) -> object:
        del max_depth
        if not isinstance(value, Mapping):
            return "[REDACTED]"
        allowed = {str(field) for field in (safe_fields or ())}
        return {
            str(key): item
            for index, (key, item) in enumerate(value.items())
            if index < max_items and str(key) in allowed
            and (item is None or isinstance(item, (bool, int, float, str)))
        }

    def security_redact_exception(error: BaseException) -> dict[str, object]:
        return {"type": type(error).__name__[:128]}


REQUEST_ID_HEADER = "X-Brevitas-Request-ID"
REQUEST_ID_COMPAT_HEADER = "X-Request-ID"
MAX_CORRELATION_ID_LENGTH = 128

_CORRELATION_ID = re.compile(
    r"^(?:job:)?(?:[0-9a-fA-F]{16,64}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_TYPE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_SAFE_ROUTE = re.compile(r"^/[A-Za-z0-9_{}:./-]{0,159}$")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_CREDENTIAL = re.compile(
    r"(?i)\b(?:sk|rk|pk|phx|phs|whsec|xox[baprs]|gh[opusr])_[A-Za-z0-9_-]{6,}"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|authorization|password|secret|token)\s*[:=]\s*[^\s,;]+"
)
_PROHIBITED_KEYS = re.compile(
    r"(?i)(?:^|[_-])(?:body|content|email|first_name|last_name|name|prompt|response|"
    r"message|messages|authorization|cookie|password|secret|token|api_key|apikey|"
    r"provider_api_key|headers?|query|url)(?:$|[_-])"
)

_request_id_var: ContextVar[str] = ContextVar("brevitas_request_id", default="")
_job_id_var: ContextVar[str] = ContextVar("brevitas_job_id", default="")

_LOG_FIELDS = frozenset({
    "alert", "attempt", "billing_status", "cache_result", "dependency",
    "duration_ms", "error_type", "job_id", "method", "operation", "outcome",
    "provider", "queue", "queue_lag_seconds", "request_id", "route", "severity",
    "status_code", "worker_slot",
})
_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_PROVIDERS = frozenset({
    "anthropic", "azure_openai", "bedrock", "brevitas", "cohere", "compressor",
    "deepseek", "fireworks", "google_gemini", "groq", "huggingface", "langchain",
    "litellm", "mistral", "ollama", "openai", "openrouter", "perplexity",
    "replicate", "together", "xai",
})
_OPERATIONS = frozenset({
    "chat", "compress", "embeddings", "generate", "health", "messages", "proxy",
    "recovery", "responses", "unknown",
})
_OUTCOMES = frozenset({
    "cancelled", "circuit_open", "client_error", "dead", "error", "failed", "hit",
    "lease_lost", "miss", "rejected", "retry", "server_error", "success", "timeout",
    "unavailable", "unknown",
})
_DEPENDENCIES = frozenset({"compressor", "postgres", "provider", "redis", "stripe"})
_SERVICES = frozenset({"api", "billing-worker", "compressor", "dashboard", "worker"})
_FAULT_DOMAINS = frozenset({
    "brevitas", "customer_configuration", "customer_credentials",
    "documented_upstream_outage",
})
_SLA_EXCLUDED_FAULT_DOMAINS = frozenset({
    "customer_configuration", "customer_credentials", "documented_upstream_outage",
})
_OUTAGE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$")
_STATIC_ROUTE_SEGMENTS = frozenset({
    "admin", "agents", "analytics", "approve", "billing", "breakdown", "cache-policy",
    "cancel", "chat", "completions", "compress", "customers", "device-auth", "embeddings",
    "health", "heartbeat", "import", "installations", "inventory", "items", "jobs", "keys",
    "live", "messages", "models", "ollama", "openai", "optimize", "optimize-prompt",
    "organization", "pipelines", "playground", "provider",
    "provider-costs", "providers", "quality", "ready", "register", "repositories", "reset",
    "responses", "retrieval", "runs", "start", "startup", "stats", "stream", "token",
    "usage", "v1",
})
_SPAN_ATTRIBUTES = frozenset({
    "brevitas.dependency", "brevitas.operation", "brevitas.outcome",
    "gen_ai.operation.name", "gen_ai.provider.name", "http.request.method", "http.route",
    "http.response.status_code",
})
_METRIC_ATTRIBUTES = frozenset({
    "cache", "dependency", "fault_domain", "method", "operation", "outcome",
    "provider", "queue", "route", "service", "sla_eligible", "state", "status",
    "surface",
})
_SERIALIZED_LOG_FIELDS = frozenset({
    *_LOG_FIELDS,
    "environment", "event", "logger", "service", "span_id", "timestamp", "trace_id",
})


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def new_request_id() -> str:
    """Return an opaque 128-bit request ID with no customer-derived material."""
    return secrets.token_hex(16)


def valid_correlation_id(value: object) -> bool:
    return isinstance(value, str) and bool(_CORRELATION_ID.fullmatch(value))


def normalize_request_id(value: object) -> str:
    """Accept a bounded safe caller ID or replace it; never truncate attacker input."""
    candidate = value.strip() if isinstance(value, str) else ""
    return candidate if valid_correlation_id(candidate) else new_request_id()


def normalize_job_id(value: object) -> str:
    candidate = value.strip() if isinstance(value, str) else ""
    return candidate if valid_correlation_id(candidate) else ""


def current_request_id() -> str:
    return _request_id_var.get()


def current_job_id() -> str:
    return _job_id_var.get()


@contextmanager
def correlation_context(*, request_id: object = "", job_id: object = "") -> Iterator[str]:
    """Bind request/job correlation to the current async or thread context."""
    request_value = normalize_request_id(request_id)
    job_value = normalize_job_id(job_id)
    request_token = _request_id_var.set(request_value)
    job_token = _job_id_var.set(job_value)
    try:
        yield request_value
    finally:
        _job_id_var.reset(job_token)
        _request_id_var.reset(request_token)


@contextmanager
def job_context(job_id: object, *, request_id: object = "") -> Iterator[str]:
    """Bind a durable job to a deterministic correlation ID when one is available."""
    safe_job_id = normalize_job_id(job_id)
    derived = request_id or (f"job:{safe_job_id}" if safe_job_id else "")
    with correlation_context(request_id=derived, job_id=safe_job_id) as bound:
        yield bound


def redact_text(value: object, *, maximum: int = 160) -> str:
    """Defensively remove common credential/PII shapes from an already-safe field."""
    text = str(value or "")[: max(0, maximum)]
    text = _EMAIL.sub("[REDACTED]", text)
    text = _BEARER.sub("[REDACTED]", text)
    text = _CREDENTIAL.sub("[REDACTED]", text)
    text = _SECRET_ASSIGNMENT.sub("[REDACTED]", text)
    return "".join(char for char in text if char >= " " and char not in "\x7f\r\n")


def route_label(value: object, *, registered: bool = False) -> str:
    """Return a registered route template; all raw/dynamic path values fail closed."""
    route = str(value or "")
    if "?" in route or not _SAFE_ROUTE.fullmatch(route):
        return "unmatched"
    if not registered:
        return "unmatched"
    for segment in route.split("/"):
        if not segment:
            continue
        if segment.startswith("{") and segment.endswith("}"):
            parameter = segment[1:-1].split(":", 1)[0]
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", parameter):
                return "unmatched"
            continue
        if segment not in _STATIC_ROUTE_SEGMENTS:
            return "unmatched"
    return route or "unmatched"


def fault_domain(value: object) -> str:
    """Collapse server-derived fault classification to a contractual finite set."""
    candidate = str(value or "").strip().lower()
    return candidate if candidate in _FAULT_DOMAINS else "brevitas"


def sla_eligible_fault(value: object) -> bool:
    return fault_domain(value) not in _SLA_EXCLUDED_FAULT_DOMAINS


def documented_upstream_outage_active(provider: object) -> bool:
    """Return true only for an operations-controlled, referenced provider incident."""
    safe_provider = _finite(provider, _PROVIDERS, "other")
    if safe_provider == "other":
        return False
    configured = {
        _finite(item, _PROVIDERS, "other")
        for item in os.getenv("BREVITAS_DOCUMENTED_UPSTREAM_OUTAGES", "")[:256].split(",")
        if item.strip()
    }
    reference = os.getenv("BREVITAS_DOCUMENTED_UPSTREAM_OUTAGE_REFERENCE", "")
    return safe_provider in configured and bool(_OUTAGE_REFERENCE.fullmatch(reference))


def _finite(value: object, allowed: frozenset[str], default: str = "unknown") -> str:
    candidate = str(value or "").strip().lower().replace("-", "_")
    return candidate if candidate in allowed else default


def sanitize_span_attributes(attributes: Mapping[str, object] | None) -> dict[str, object]:
    """Allow only finite server metadata; never pass arbitrary values into a tracer."""
    safe: dict[str, object] = {}
    for key, value in dict(attributes or {}).items():
        if key not in _SPAN_ATTRIBUTES:
            continue
        if key == "http.request.method":
            method = str(value or "").upper()
            safe[key] = method if method in _METHODS else "OTHER"
        elif key == "http.route":
            route = route_label(value, registered=True)
            if route != "unmatched":
                safe[key] = route
        elif key == "http.response.status_code":
            try:
                status = int(value)
            except (TypeError, ValueError):
                continue
            if 100 <= status <= 599:
                safe[key] = status
        elif key in {"brevitas.operation", "gen_ai.operation.name"}:
            safe[key] = _finite(value, _OPERATIONS)
        elif key == "gen_ai.provider.name":
            safe[key] = _finite(value, _PROVIDERS, "other")
        elif key == "brevitas.dependency":
            safe[key] = _finite(value, _DEPENDENCIES, "other")
        elif key == "brevitas.outcome":
            safe[key] = _finite(value, _OUTCOMES)
    try:
        cleaned = security_redact(
            safe, safe_fields=_SPAN_ATTRIBUTES, max_depth=2, max_items=16,
        )
    except Exception:
        return {}
    return dict(cleaned) if isinstance(cleaned, Mapping) else {}


def _metric_export_attributes(
    attributes: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if attributes is None:
        return None
    strict = {
        str(key): value
        for key, value in attributes.items()
        if str(key) in _METRIC_ATTRIBUTES
        and (value is None or isinstance(value, (bool, int, float, str)))
    }
    try:
        cleaned = security_redact(
            strict, safe_fields=_METRIC_ATTRIBUTES, max_depth=2, max_items=24,
        )
    except Exception:
        return {}
    return dict(cleaned) if isinstance(cleaned, Mapping) else {}


def _safe_field(key: str, value: object) -> object | None:
    if key not in _LOG_FIELDS or _PROHIBITED_KEYS.search(key):
        return None
    if key == "request_id":
        return str(value) if valid_correlation_id(value) else "[REDACTED]"
    if key == "job_id":
        return normalize_job_id(value) or "[REDACTED]"
    if key == "route":
        return route_label(value, registered=True)
    if key == "method":
        method = str(value or "").upper()
        return method if method in _METHODS else "OTHER"
    if key == "provider":
        return _finite(value, _PROVIDERS, "other")
    if key == "operation":
        return _finite(value, _OPERATIONS)
    if key == "outcome":
        return _finite(value, _OUTCOMES)
    if key == "dependency":
        return _finite(value, _DEPENDENCIES, "other")
    if key in {"duration_ms", "queue_lag_seconds"}:
        try:
            return round(max(0.0, min(float(value), 86_400_000.0)), 3)
        except (TypeError, ValueError):
            return None
    if key in {"attempt", "status_code", "worker_slot"}:
        try:
            return max(0, min(int(value), 100_000))
        except (TypeError, ValueError):
            return None
    if key == "error_type":
        candidate = str(value or "")
        return candidate if _TYPE_NAME.fullmatch(candidate) else "Error"
    if key == "alert":
        candidate = str(value or "")
        return candidate if _EVENT_NAME.fullmatch(candidate) else "unknown"
    if key == "severity":
        candidate = str(value or "").lower()
        return candidate if candidate in {"info", "page", "ticket", "warning"} else "warning"
    if key == "billing_status":
        candidate = str(value or "").lower()
        return candidate if candidate in {
            "capped", "dead", "degraded", "expired", "pending", "reported", "review"
        } else "unknown"
    if key == "cache_result":
        candidate = str(value or "").lower()
        return candidate if candidate in {
            "disabled", "error", "evicted", "hit", "miss", "write"
        } else "unknown"
    if key == "queue":
        return "billing" if str(value) == "billing" else "jobs"
    return redact_text(value, maximum=80)


def sanitize_log_fields(fields: Mapping[str, object] | None) -> dict[str, object]:
    """Apply an allowlist before redaction so payload-shaped values cannot escape."""
    safe: dict[str, object] = {}
    for key, value in dict(fields or {}).items():
        safe_value = _safe_field(str(key), value)
        if safe_value is not None:
            safe[str(key)] = safe_value
    return safe


def _trace_ids() -> tuple[str, str]:
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            return f"{context.trace_id:032x}", f"{context.span_id:016x}"
    except Exception:
        pass
    return "", ""


class JsonLogFormatter(logging.Formatter):
    """Stable JSON formatter that intentionally discards free-form log messages."""

    def __init__(self, service: str, environment: str) -> None:
        super().__init__()
        self.service = _finite(service, _SERVICES, "api")
        self.environment = redact_text(environment, maximum=32) or "development"

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "telemetry_event", "application_log")
        if not isinstance(event, str) or not _EVENT_NAME.fullmatch(event):
            event = "application_log"
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "severity": record.levelname.lower(),
            "service": self.service,
            "environment": self.environment,
            "logger": redact_text(record.name, maximum=80),
            "event": event,
        }
        request_id = current_request_id()
        job_id = current_job_id()
        if request_id:
            payload["request_id"] = request_id
        if job_id:
            payload["job_id"] = job_id
        trace_id, span_id = _trace_ids()
        if trace_id:
            payload["trace_id"] = trace_id
            payload["span_id"] = span_id
        payload.update(sanitize_log_fields(getattr(record, "telemetry_fields", None)))
        if record.exc_info and record.exc_info[0]:
            try:
                exception = security_redact_exception(record.exc_info[1])
                error_type = exception.get("type", "Error")
            except Exception:
                error_type = "Error"
            payload["error_type"] = _safe_field("error_type", error_type) or "Error"
        try:
            cleaned = security_redact(
                payload,
                safe_fields=_SERIALIZED_LOG_FIELDS,
                max_depth=2,
                max_items=64,
            )
        except Exception:
            cleaned = {
                "event": "telemetry_redaction_failed",
                "severity": "error",
                "service": self.service,
            }
        return json.dumps(cleaned, separators=(",", ":"), sort_keys=True)


class StructuredLogger:
    """Small logger facade that never accepts a free-form body or message field."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def emit(self, level: int, event: str, **fields: object) -> None:
        safe_event = event if _EVENT_NAME.fullmatch(event) else "application_log"
        self._logger.log(
            level,
            safe_event,
            extra={"telemetry_event": safe_event, "telemetry_fields": sanitize_log_fields(fields)},
        )

    def info(self, event: str, **fields: object) -> None:
        self.emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: object) -> None:
        self.emit(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: object) -> None:
        self.emit(logging.ERROR, event, **fields)


def configure_json_logging(
    *, service: str, environment: str | None = None, logger_names: tuple[str, ...] = ("",),
    replace_handlers: bool = True,
) -> None:
    """Install content-free stdout JSON logging on the selected logger names."""
    formatter = JsonLogFormatter(service, environment or os.getenv("BREVITAS_ENV", "development"))
    for name in logger_names:
        logger = logging.getLogger(name)
        if any(getattr(handler, "_brevitas_json", False) for handler in logger.handlers):
            continue
        if replace_handlers:
            # Explicit observability setup replaces plaintext handlers that could render
            # arguments containing customer data. It does not alter unselected loggers.
            logger.handlers.clear()
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler._brevitas_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        logger.setLevel(os.getenv("BREVITAS_LOG_LEVEL", "INFO").upper())
        if name:
            logger.propagate = False


class _NoopInstrument:
    def add(self, amount: int | float, attributes: Mapping[str, object] | None = None) -> None:
        return None

    def record(self, amount: int | float, attributes: Mapping[str, object] | None = None) -> None:
        return None

    def set(self, amount: int | float, attributes: Mapping[str, object] | None = None) -> None:
        return None


class _NoopMeter:
    def create_counter(self, *args: object, **kwargs: object) -> _NoopInstrument:
        return _NoopInstrument()

    def create_histogram(self, *args: object, **kwargs: object) -> _NoopInstrument:
        return _NoopInstrument()

    def create_gauge(self, *args: object, **kwargs: object) -> _NoopInstrument:
        return _NoopInstrument()


def _gauge(meter: object, name: str, **kwargs: object) -> object:
    creator = getattr(meter, "create_gauge", None)
    return creator(name, **kwargs) if creator else _NoopInstrument()


class Metrics:
    """Typed, low-cardinality metric facade for every production dependency."""

    def __init__(self, meter: object | None = None) -> None:
        meter = meter or _NoopMeter()
        self.api_requests = meter.create_counter("brevitas.api.requests", unit="1")
        self.api_duration = meter.create_histogram("brevitas.api.request.duration", unit="s")
        self.service_operations = meter.create_counter("brevitas.service.operations", unit="1")
        self.provider_requests = meter.create_counter("brevitas.provider.requests", unit="1")
        self.provider_duration = meter.create_histogram("brevitas.provider.request.duration", unit="s")
        self.provider_retries = meter.create_counter("brevitas.provider.retries", unit="1")
        self.provider_circuit = meter.create_counter("brevitas.provider.circuit", unit="1")
        self.jobs = meter.create_counter("brevitas.jobs", unit="1")
        self.job_duration = meter.create_histogram("brevitas.job.duration", unit="s")
        self.queue_lag = _gauge(meter, "brevitas.queue.lag", unit="s")
        self.queue_depth = _gauge(meter, "brevitas.queue.depth", unit="1")
        self.cache_operations = meter.create_counter("brevitas.cache.operations", unit="1")
        self.billing_entries = meter.create_counter("brevitas.billing.entries", unit="1")
        self.billing_recovery = meter.create_counter("brevitas.billing.recovery", unit="1")
        self.billing_batch_duration = meter.create_histogram("brevitas.billing.batch.duration", unit="s")
        self.billing_queue_lag = _gauge(meter, "brevitas.billing.queue.lag", unit="s")
        self.billing_review = _gauge(meter, "brevitas.billing.review", unit="1")
        self.billing_dead = _gauge(meter, "brevitas.billing.dead", unit="1")
        self.billing_stale = _gauge(meter, "brevitas.billing.stale", unit="1")
        self.billing_catalog_contract = _gauge(
            meter, "brevitas.billing.catalog.contract", unit="1"
        )
        self.dependency_operations = meter.create_counter(
            "brevitas.dependency.operations", unit="1"
        )
        self.dependency_duration = meter.create_histogram(
            "brevitas.dependency.duration", unit="s"
        )

    @staticmethod
    def _outcome(value: object) -> str:
        return _finite(value, _OUTCOMES)

    @staticmethod
    def _emit(
        instrument: object, method: str, value: int | float,
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        try:
            operation = getattr(instrument, method)
            safe_attributes = _metric_export_attributes(attributes)
            if attributes is None:
                operation(value)
            else:
                operation(value, safe_attributes or {})
        except Exception:
            # Synchronous instrument/exporter defects must not affect application work.
            pass

    def record_api_request(
        self, *, duration_seconds: float, method: object, route: object,
        status_code: int, surface: str = "external", fault: object = "brevitas",
    ) -> None:
        domain = fault_domain(fault)
        outcome = "success"
        if status_code >= 500:
            outcome = "server_error" if domain == "brevitas" else "unavailable"
        elif status_code >= 400:
            outcome = "client_error"
        attrs = {
            "method": str(method).upper() if str(method).upper() in _METHODS else "OTHER",
            "route": route_label(route, registered=True),
            "outcome": outcome,
            "surface": "internal" if surface == "internal" else "external",
            "fault_domain": domain,
            "sla_eligible": "true" if sla_eligible_fault(domain) else "false",
        }
        self._emit(self.api_requests, "add", 1, attrs)
        self._emit(self.api_duration, "record", max(0.0, float(duration_seconds)), attrs)

    def record_service_operation(self, *, service: object, outcome: object) -> None:
        self._emit(self.service_operations, "add", 1, {
            "service": _finite(service, _SERVICES, "api"),
            "surface": "internal",
            "outcome": self._outcome(outcome),
        })

    def record_provider(
        self, *, provider: object, operation: object, outcome: object,
        duration_seconds: float, attempt: int = 1,
    ) -> None:
        attrs = {
            "provider": _finite(provider, _PROVIDERS, "other"),
            "operation": _finite(operation, _OPERATIONS),
            "outcome": self._outcome(outcome),
        }
        self._emit(self.provider_requests, "add", 1, attrs)
        self._emit(self.provider_duration, "record", max(0.0, float(duration_seconds)), attrs)
        if attempt > 1:
            self._emit(self.provider_retries, "add", max(0, min(int(attempt) - 1, 5)), attrs)
        if attrs["outcome"] == "circuit_open":
            self._emit(self.provider_circuit, "add", 1, {
                "provider": attrs["provider"], "state": "open",
            })

    def record_job(self, *, operation: object, status: object, duration_seconds: float) -> None:
        state = str(status or "unknown").lower()
        if state not in {"cancelled", "dead", "failed", "leased", "queued", "succeeded"}:
            state = "unknown"
        attrs = {"operation": _finite(operation, _OPERATIONS), "status": state}
        self._emit(self.jobs, "add", 1, attrs)
        self._emit(self.job_duration, "record", max(0.0, float(duration_seconds)), attrs)

    def record_queue(self, *, queue: object = "jobs", depth: int, lag_seconds: float) -> None:
        queue_name = "billing" if str(queue) == "billing" else "jobs"
        attrs = {"queue": queue_name}
        self._emit(self.queue_depth, "set", max(0, min(int(depth), 10_000_000)), attrs)
        self._emit(self.queue_lag, "set", max(0.0, min(float(lag_seconds), 86_400.0)), attrs)

    def record_cache(self, *, cache: object, outcome: object) -> None:
        cache_name = str(cache or "semantic").lower()
        if cache_name not in {"auth_context", "provider_state", "semantic", "session"}:
            cache_name = "other"
        result = str(outcome or "unknown").lower()
        if result not in {"disabled", "error", "evicted", "hit", "miss", "write"}:
            result = "unknown"
        self._emit(self.cache_operations, "add", 1, {"cache": cache_name, "outcome": result})

    def record_dependency(
        self, *, dependency: object, outcome: object, duration_seconds: float,
    ) -> None:
        attrs = {
            "dependency": _finite(dependency, _DEPENDENCIES, "other"),
            "outcome": self._outcome(outcome),
        }
        self._emit(self.dependency_operations, "add", 1, attrs)
        self._emit(self.dependency_duration, "record", max(0.0, float(duration_seconds)), attrs)

    def record_billing_metric(
        self, name: str, value: float, attributes: Mapping[str, str] | None = None,
    ) -> None:
        """Adapt the billing worker's fixed metric protocol without dynamic instruments."""
        try:
            safe_value = max(0.0, min(float(value), 10_000_000_000.0))
        except (TypeError, ValueError):
            return
        attrs = dict(attributes or {})
        if name == "billing.entries":
            status = str(attrs.get("status") or "unknown")
            if status not in {"capped", "dead", "expired", "pending", "reported", "review"}:
                status = "unknown"
            self._emit(self.billing_entries, "add", safe_value, {"status": status})
        elif name == "billing.lease_lost":
            self._emit(self.billing_recovery, "add", safe_value, {"outcome": "lease_lost"})
        elif name == "billing.batch.claimed":
            self._emit(self.billing_recovery, "add", safe_value, {"outcome": "claimed"})
        elif name == "billing.batch.duration_ms":
            self._emit(self.billing_batch_duration, "record", safe_value / 1000.0)
        elif name == "billing.oldest_pending_seconds":
            self._emit(self.billing_queue_lag, "set", safe_value)
        elif name == "billing.review_count":
            self._emit(self.billing_review, "set", safe_value)
        elif name == "billing.dead_count":
            self._emit(self.billing_dead, "set", safe_value)
        elif name == "billing.stale_sending_count":
            self._emit(self.billing_stale, "set", safe_value)
        elif name == "billing.catalog_contract_valid":
            self._emit(self.billing_catalog_contract, "set", 1 if safe_value > 0 else 0)
        elif name == "billing.catalog_contract_invalid":
            self._emit(self.billing_catalog_contract, "set", 0)


@dataclass(frozen=True)
class ObservabilitySettings:
    enabled: bool = False
    service_name: str = "api"
    environment: str = "development"
    queue_size: int = 2048
    batch_size: int = 256
    schedule_delay_ms: int = 5000
    export_timeout_ms: int = 5000
    metric_interval_ms: int = 60_000
    instance_id: str = ""

    @classmethod
    def from_env(cls, *, default_service: str = "api") -> "ObservabilitySettings":
        queue_size = _bounded_int("BREVITAS_OTEL_MAX_QUEUE_SIZE", 2048, 64, 65_536)
        batch_size = min(
            queue_size,
            _bounded_int("BREVITAS_OTEL_MAX_EXPORT_BATCH_SIZE", 256, 1, 8192),
        )
        disabled = _enabled(os.getenv("OTEL_SDK_DISABLED"))
        configured_instance = (
            os.getenv("OTEL_SERVICE_INSTANCE_ID")
            or os.getenv("RAILWAY_REPLICA_ID")
            or ""
        )
        instance_id = (
            configured_instance
            if valid_correlation_id(configured_instance) and not configured_instance.startswith("job:")
            else secrets.token_hex(8)
        )
        return cls(
            enabled=_enabled(os.getenv("BREVITAS_OTEL_ENABLED")) and not disabled,
            service_name=_finite(os.getenv("OTEL_SERVICE_NAME", default_service), _SERVICES, default_service),
            environment=redact_text(os.getenv("BREVITAS_ENV", "development"), maximum=32),
            queue_size=queue_size,
            batch_size=batch_size,
            schedule_delay_ms=_bounded_int("BREVITAS_OTEL_SCHEDULE_DELAY_MS", 5000, 100, 60_000),
            export_timeout_ms=_bounded_int("BREVITAS_OTEL_EXPORT_TIMEOUT_MS", 5000, 100, 30_000),
            metric_interval_ms=_bounded_int(
                "BREVITAS_OTEL_METRIC_INTERVAL_MS", 60_000, 5000, 300_000
            ),
            instance_id=instance_id,
        )


class ObservabilityRuntime:
    """Owns bounded processors and provides idempotent flush/shutdown hooks."""

    def __init__(
        self, settings: ObservabilitySettings, *, tracer: object | None = None,
        meter: object | None = None, tracer_provider: object | None = None,
        meter_provider: object | None = None,
    ) -> None:
        self.settings = settings
        self.enabled = bool(settings.enabled and tracer is not None)
        self.tracer = tracer
        self.metrics = Metrics(meter)
        self._tracer_provider = tracer_provider
        self._meter_provider = meter_provider
        self._closed = False
        self._lock = threading.Lock()

    @contextmanager
    def span(self, name: str, attributes: Mapping[str, object] | None = None) -> Iterator[object | None]:
        if not self.enabled or self.tracer is None:
            yield None
            return
        safe_name = name if _EVENT_NAME.fullmatch(name) else "application.operation"
        try:
            manager = self.tracer.start_as_current_span(
                safe_name,
                attributes=sanitize_span_attributes(attributes),
                record_exception=False,
                set_status_on_exception=False,
            )
        except Exception:
            yield None
            return
        try:
            span = manager.__enter__()
        except Exception:
            yield None
            return
        try:
            yield span
        except BaseException:
            raise
        finally:
            try:
                # Never pass the application exception object/message/traceback into an
                # SDK context manager. Callers emit only finite outcome/error classes.
                manager.__exit__(None, None, None)
            except Exception:
                pass

    def force_flush(self, timeout_ms: int | None = None) -> bool:
        timeout = timeout_ms or self.settings.export_timeout_ms
        result = True
        for provider in (self._tracer_provider, self._meter_provider):
            flush = getattr(provider, "force_flush", None)
            if flush is not None:
                try:
                    flushed = flush(timeout_millis=timeout)
                    result = (flushed is not False) and result
                except Exception:
                    result = False
        return result

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.force_flush()
        for provider in (self._meter_provider, self._tracer_provider):
            _shutdown_component(provider, self.settings.export_timeout_ms)
        self.enabled = False


def _shutdown_component(component: object | None, timeout_ms: int) -> None:
    if component is None:
        return
    shutdown = getattr(component, "shutdown", None)
    if shutdown is None:
        return
    try:
        parameters = inspect.signature(shutdown).parameters
    except (TypeError, ValueError):
        parameters = {}
    try:
        if "timeout_millis" in parameters:
            shutdown(timeout_millis=timeout_ms)
        elif "timeout" in parameters:
            shutdown(timeout=max(0.1, timeout_ms / 1000.0))
        else:
            shutdown()
    except Exception:
        pass


def _otel_components() -> dict[str, object]:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    return {
        "BatchSpanProcessor": BatchSpanProcessor,
        "MeterProvider": MeterProvider,
        "OTLPMetricExporter": OTLPMetricExporter,
        "OTLPSpanExporter": OTLPSpanExporter,
        "PeriodicExportingMetricReader": PeriodicExportingMetricReader,
        "Resource": Resource,
        "TracerProvider": TracerProvider,
    }


def _build_runtime(
    settings: ObservabilitySettings, *, components: Mapping[str, object] | None = None,
) -> ObservabilityRuntime:
    if not settings.enabled:
        return ObservabilityRuntime(settings)
    constructed: list[object] = []
    try:
        factories = dict(components or _otel_components())
        resource = factories["Resource"].create({  # type: ignore[union-attr]
            "service.name": settings.service_name,
            "service.instance.id": settings.instance_id or secrets.token_hex(8),
            "deployment.environment.name": settings.environment,
        })
        tracer_provider = factories["TracerProvider"](resource=resource)  # type: ignore[operator]
        constructed.append(tracer_provider)
        span_exporter = factories["OTLPSpanExporter"](  # type: ignore[operator]
            timeout=settings.export_timeout_ms / 1000.0,
        )
        constructed.append(span_exporter)
        span_processor = factories["BatchSpanProcessor"](  # type: ignore[operator]
            span_exporter,
            max_queue_size=settings.queue_size,
            max_export_batch_size=settings.batch_size,
            schedule_delay_millis=settings.schedule_delay_ms,
            export_timeout_millis=settings.export_timeout_ms,
        )
        constructed.append(span_processor)
        tracer_provider.add_span_processor(span_processor)
        metric_exporter = factories["OTLPMetricExporter"](  # type: ignore[operator]
            timeout=settings.export_timeout_ms / 1000.0,
        )
        constructed.append(metric_exporter)
        metric_reader = factories["PeriodicExportingMetricReader"](  # type: ignore[operator]
            metric_exporter,
            export_interval_millis=settings.metric_interval_ms,
            export_timeout_millis=settings.export_timeout_ms,
        )
        constructed.append(metric_reader)
        meter_provider = factories["MeterProvider"](  # type: ignore[operator]
            resource=resource, metric_readers=[metric_reader]
        )
        constructed.append(meter_provider)
        return ObservabilityRuntime(
            settings,
            tracer=tracer_provider.get_tracer("brevitas"),
            meter=meter_provider.get_meter("brevitas"),
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
        )
    except Exception:
        # A later constructor can fail after an exporter thread exists. Close every
        # component already built, in reverse order, before returning a safe no-op.
        seen: set[int] = set()
        for component in reversed(constructed):
            if id(component) not in seen:
                seen.add(id(component))
                _shutdown_component(component, settings.export_timeout_ms)
        return ObservabilityRuntime(settings)


_runtime_lock = threading.Lock()
_runtime: ObservabilityRuntime | None = None


def get_runtime(*, default_service: str = "api") -> ObservabilityRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = _build_runtime(
                ObservabilitySettings.from_env(default_service=default_service)
            )
        return _runtime


def shutdown_observability() -> None:
    runtime = _runtime
    if runtime is not None:
        runtime.shutdown()


def provider_correlation_headers(
    headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy outbound headers and inject request/trace correlation without logging them."""
    outgoing = dict(headers or {})
    request_id = current_request_id()
    if request_id:
        outgoing[REQUEST_ID_HEADER] = request_id
        outgoing[REQUEST_ID_COMPAT_HEADER] = request_id
    try:
        from opentelemetry.propagate import inject

        inject(outgoing)
    except Exception:
        pass
    return outgoing


atexit.register(shutdown_observability)


__all__ = [
    "JsonLogFormatter", "MAX_CORRELATION_ID_LENGTH", "Metrics", "ObservabilityRuntime",
    "ObservabilitySettings", "REQUEST_ID_COMPAT_HEADER", "REQUEST_ID_HEADER",
    "StructuredLogger", "configure_json_logging", "correlation_context", "current_job_id",
    "current_request_id", "documented_upstream_outage_active", "fault_domain", "get_runtime",
    "job_context", "new_request_id",
    "normalize_job_id", "normalize_request_id", "provider_correlation_headers",
    "redact_text", "route_label", "sanitize_log_fields", "sanitize_span_attributes",
    "shutdown_observability", "sla_eligible_fault", "valid_correlation_id",
]
