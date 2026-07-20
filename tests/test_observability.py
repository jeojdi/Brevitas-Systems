"""Local, content-free observability contract tests; no exporter/network calls."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import api.observability as api_observability
from api.observability import (
    BillingTelemetryAdapter,
    install_fastapi_observability,
    mark_documented_upstream_outage,
    mark_request_fault_domain,
)
import brevitas.observability as observability
from brevitas.security import KMSUnavailable
from brevitas.observability import (
    JsonLogFormatter,
    Metrics,
    ObservabilityRuntime,
    ObservabilitySettings,
    correlation_context,
    current_job_id,
    current_request_id,
    documented_upstream_outage_active,
    job_context,
    normalize_request_id,
    provider_correlation_headers,
    redact_text,
    route_label,
    sanitize_span_attributes,
    valid_correlation_id,
)


ROOT = Path(__file__).parent.parent


def test_request_id_is_bounded_validated_and_propagated_to_provider_and_response():
    app = FastAPI()
    install_fastapi_observability(app, configure_logs=False)

    @app.get("/v1/items/{item_id}")
    async def item(item_id: str):
        headers = provider_correlation_headers({"Accept": "application/json"})
        return {
            "request_id": current_request_id(),
            "provider_request_id": headers["X-Brevitas-Request-ID"],
            "compat_request_id": headers["X-Request-ID"],
        }

    with TestClient(app) as client:
        response = client.get("/v1/items/123", headers={
            "X-Brevitas-Request-ID": "0123456789abcdef0123456789abcdef",
        })
        assert response.status_code == 200
        assert response.headers["X-Brevitas-Request-ID"] == "0123456789abcdef0123456789abcdef"
        assert response.headers["X-Request-ID"] == "0123456789abcdef0123456789abcdef"
        assert response.json() == {
            "request_id": "0123456789abcdef0123456789abcdef",
            "provider_request_id": "0123456789abcdef0123456789abcdef",
            "compat_request_id": "0123456789abcdef0123456789abcdef",
        }

        malicious = "victim@example.com.Authorization-Bearer-secret"
        replaced = client.get("/v1/items/456", headers={"X-Request-ID": malicious})
        generated = replaced.headers["X-Brevitas-Request-ID"]
        assert generated != malicious
        assert valid_correlation_id(generated)
        assert len(generated) == 32

    assert current_request_id() == ""


def test_unhandled_500_always_returns_both_correlation_headers():
    app = FastAPI()
    install_fastapi_observability(app, configure_logs=False)

    @app.get("/v1/health")
    async def explode():
        raise RuntimeError("secret prompt person@example.com sk_live_123456789")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/health", headers={
            "X-Request-ID": "11111111111111111111111111111111",
        })
    assert response.status_code == 500
    assert response.headers["X-Brevitas-Request-ID"] == "11111111111111111111111111111111"
    assert response.headers["X-Request-ID"] == "11111111111111111111111111111111"
    assert response.json() == {"detail": "Internal server error"}


def test_contextvars_isolate_concurrent_requests_and_correlate_jobs():
    async def request(value: str) -> tuple[str, str]:
        with correlation_context(request_id=value):
            await asyncio.sleep(0)
            return current_request_id(), current_job_id()

    first, second = asyncio.run(_gather(
        request("aaaaaaaaaaaaaaaa0000000000000001"),
        request("bbbbbbbbbbbbbbbb0000000000000002"),
    ))
    assert first == ("aaaaaaaaaaaaaaaa0000000000000001", "")
    assert second == ("bbbbbbbbbbbbbbbb0000000000000002", "")

    with job_context("123e4567-e89b-12d3-a456-426614174000"):
        assert current_job_id() == "123e4567-e89b-12d3-a456-426614174000"
        assert current_request_id() == "job:123e4567-e89b-12d3-a456-426614174000"


async def _gather(*awaitables):
    return await asyncio.gather(*awaitables)


def test_structured_json_logging_never_serializes_freeform_or_sensitive_fields():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter("api", "test"))
    logger = logging.Logger("brevitas.test")
    logger.addHandler(handler)
    raw_secret = "sk_live_1234567890"
    raw_email = "person@example.com"
    raw_prompt = "Please summarize the acquisition plan for Alice"

    with correlation_context(request_id="cccccccccccccccc0000000000000003"):
        logger.error(
            "free-form %s %s %s",
            raw_secret,
            raw_email,
            raw_prompt,
            extra={
                "telemetry_event": "provider_failed",
                "telemetry_fields": {
                    "authorization": f"Bearer {raw_secret}",
                    "body": raw_prompt,
                    "email": raw_email,
                    "prompt": raw_prompt,
                    "provider": raw_secret,
                    "operation": "chat",
                    "error_type": "ProviderTimeout",
                },
            },
        )

    encoded = stream.getvalue()
    payload = json.loads(encoded)
    assert payload["event"] == "provider_failed"
    assert payload["request_id"] == "cccccccccccccccc0000000000000003"
    assert payload["provider"] == "other"
    assert payload["operation"] == "chat"
    assert payload["error_type"] == "ProviderTimeout"
    for forbidden in (raw_secret, raw_email, raw_prompt, "authorization", "prompt", "body"):
        assert forbidden not in encoded
    assert "[REDACTED]" in redact_text(f"Bearer {raw_secret} {raw_email}")


def test_shared_redactor_handles_attacker_keys_values_and_kms_exception(monkeypatch):
    redact_calls = []
    exception_calls = []
    actual_redact = observability.security_redact
    actual_redact_exception = observability.security_redact_exception

    def tracked_redact(value, **kwargs):
        redact_calls.append(value)
        return actual_redact(value, **kwargs)

    def tracked_exception(error):
        exception_calls.append(error)
        return actual_redact_exception(error)

    monkeypatch.setattr(observability, "security_redact", tracked_redact)
    monkeypatch.setattr(observability, "security_redact_exception", tracked_exception)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter("api", "test"))
    logger = logging.Logger("brevitas.kms")
    logger.addHandler(handler)
    error = KMSUnavailable(
        "SDK https://user:password@example.com/kms?token=secret "
        "ciphertext=bvt-envelope-secret prompt for Alice"
    )
    error.request_context = {"tenant": "Alice", "api_key": "sk_private_value"}
    error.ciphertext = b"wrapped-and-plaintext-key-material"
    logger.error(
        "raw SDK object must be discarded",
        exc_info=(KMSUnavailable, error, None),
        extra={
            "telemetry_event": "kms_unavailable",
            "telemetry_fields": {
                "authorization=Bearer attacker-key": "secret value",
                "url": "https://user:password@example.com/private?token=secret",
                "provider": "Bearer provider-secret",
                "operation": "unknown",
            },
        },
    )
    encoded = stream.getvalue()
    payload = json.loads(encoded)
    assert payload["event"] == "kms_unavailable"
    assert payload["error_type"] == "KMSUnavailable"
    assert payload["provider"] == "other"
    assert exception_calls == [error]
    assert redact_calls
    for forbidden in (
        "password@example", "bvt-envelope-secret", "Alice", "sk_private_value",
        "wrapped-and-plaintext", "attacker-key", "secret value", "message", "cause",
        "attributes", "ciphertext", "request_context", "url",
    ):
        assert forbidden not in encoded


def test_metric_export_boundary_drops_attacker_keys_and_redacts_values():
    calls = []
    instrument = _Instrument("attack.metric", calls)
    Metrics._emit(instrument, "add", 1, {
        "provider": "Bearer attacker-secret",
        "outcome": "success",
        "url": "https://user:password@example.com/private?token=secret",
        "ciphertext": "bvt-envelope-private",
    })
    assert calls == [(
        "attack.metric", "add", 1,
        {"provider": "[REDACTED]", "outcome": "success"},
    )]


class _Instrument:
    def __init__(self, name: str, calls: list):
        self.name = name
        self.calls = calls

    def add(self, value, attributes=None):
        self.calls.append((self.name, "add", value, attributes or {}))

    def record(self, value, attributes=None):
        self.calls.append((self.name, "record", value, attributes or {}))

    def set(self, value, attributes=None):
        self.calls.append((self.name, "set", value, attributes or {}))


class _Meter:
    def __init__(self):
        self.calls = []

    def create_counter(self, name, **_kwargs):
        return _Instrument(name, self.calls)

    def create_histogram(self, name, **_kwargs):
        return _Instrument(name, self.calls)

    def create_gauge(self, name, **_kwargs):
        return _Instrument(name, self.calls)


def test_sla_eligibility_excludes_only_finite_server_classified_faults():
    meter = _Meter()
    metrics = Metrics(meter)
    metrics.record_api_request(
        duration_seconds=.1,
        method="POST",
        route="/v1/items/{item_id}",
        status_code=503,
        fault="upstream_provider",
    )
    metrics.record_api_request(
        duration_seconds=.1,
        method="POST",
        route="/v1/items/{item_id}",
        status_code=503,
        fault="documented_upstream_outage",
    )
    metrics.record_api_request(
        duration_seconds=.1,
        method="POST",
        route="/v1/items/{item_id}",
        status_code=400,
        fault="customer_configuration",
    )
    metrics.record_api_request(
        duration_seconds=.1,
        method="POST",
        route="/v1/items/{item_id}",
        status_code=503,
        fault="person@example.com",
    )
    request_labels = [
        attrs for name, method, _value, attrs in meter.calls
        if name == "brevitas.api.requests" and method == "add"
    ]
    # A provider error/timeout/circuit rejection is not itself a contractual exclusion.
    assert request_labels[0]["fault_domain"] == "brevitas"
    assert request_labels[0]["sla_eligible"] == "true"
    assert request_labels[0]["outcome"] == "server_error"
    assert request_labels[1]["fault_domain"] == "documented_upstream_outage"
    assert request_labels[1]["sla_eligible"] == "false"
    assert request_labels[1]["outcome"] == "unavailable"
    assert request_labels[2]["fault_domain"] == "customer_configuration"
    assert request_labels[2]["sla_eligible"] == "false"
    assert request_labels[2]["outcome"] == "client_error"
    assert request_labels[3]["fault_domain"] == "brevitas"
    assert request_labels[3]["sla_eligible"] == "true"
    assert request_labels[3]["outcome"] == "server_error"
    assert "person@example.com" not in json.dumps(request_labels)


def test_documented_upstream_outage_requires_ops_provider_and_reference(monkeypatch):
    monkeypatch.setenv("BREVITAS_DOCUMENTED_UPSTREAM_OUTAGES", "openai")
    monkeypatch.delenv("BREVITAS_DOCUMENTED_UPSTREAM_OUTAGE_REFERENCE", raising=False)
    assert documented_upstream_outage_active("openai") is False
    monkeypatch.setenv(
        "BREVITAS_DOCUMENTED_UPSTREAM_OUTAGE_REFERENCE", "INCIDENT-20260718"
    )
    assert documented_upstream_outage_active("openai") is True
    assert documented_upstream_outage_active("anthropic") is False
    assert documented_upstream_outage_active("person@example.com") is False

    request = Request({
        "type": "http", "method": "GET", "path": "/v1/health", "headers": [],
        "query_string": b"", "scheme": "https", "server": ("test", 443),
        "client": ("test", 123), "state": {},
    })
    mark_request_fault_domain(request, "documented_upstream_outage")
    assert request.state.brevitas_fault_domain == "brevitas"
    assert mark_documented_upstream_outage(request, "openai") is True
    assert request.state.brevitas_fault_domain == "documented_upstream_outage"


def test_route_labels_require_registered_templates_and_reject_raw_identity_segments():
    assert route_label("/v1/jobs/{job_id}", registered=True) == "/v1/jobs/{job_id}"
    assert route_label("/v1/health/ready", registered=True) == "/v1/health/ready"
    assert route_label("/v1/jobs/{job_id}") == "unmatched"
    for unsafe in (
        "/v1/jobs/alice",
        "/v1/jobs/123456789",
        "/v1/jobs/123e4567-e89b-12d3-a456-426614174000",
        "/v1/jobs/person@example.com",
        "/v1/unknown-static-segment",
    ):
        assert route_label(unsafe, registered=True) == "unmatched"


def test_metrics_cover_services_and_collapse_unbounded_labels():
    meter = _Meter()
    metrics = Metrics(meter)
    metrics.record_api_request(
        duration_seconds=0.2,
        method="TRACE",
        route="/customers/123456789012345678901234",
        status_code=503,
    )
    metrics.record_provider(
        provider="person@example.com",
        operation="a-new-dynamic-operation",
        outcome="made-up-outcome",
        duration_seconds=1.2,
        attempt=99,
    )
    metrics.record_job(operation="compress", status="dead", duration_seconds=3)
    metrics.record_queue(depth=12, lag_seconds=9)
    metrics.record_cache(cache="tenant-specific-cache", outcome="hit")
    metrics.record_dependency(dependency="postgres", outcome="timeout", duration_seconds=.5)
    metrics.record_billing_metric("billing.review_count", 2)
    metrics.record_billing_metric("billing.stale_sending_count", 1)
    metrics.record_billing_metric("billing.catalog_contract_invalid", 1)

    labels = [attributes for _name, _method, _value, attributes in meter.calls]
    rendered = json.dumps(labels)
    assert "person@example.com" not in rendered
    assert "123456789012345678901234" not in rendered
    assert '"provider": "other"' in rendered
    assert '"operation": "unknown"' in rendered
    assert '"route": "unmatched"' in rendered
    assert '"dependency": "postgres"' in rendered
    assert {name for name, _method, _value, _attrs in meter.calls} >= {
        "brevitas.api.requests",
        "brevitas.provider.requests",
        "brevitas.jobs",
        "brevitas.queue.lag",
        "brevitas.cache.operations",
        "brevitas.dependency.operations",
        "brevitas.billing.review",
        "brevitas.billing.stale",
        "brevitas.billing.catalog.contract",
    }


def test_disabled_runtime_and_exporter_defects_are_request_safe(monkeypatch):
    monkeypatch.setenv("BREVITAS_OTEL_ENABLED", "false")
    monkeypatch.setenv("BREVITAS_OTEL_MAX_QUEUE_SIZE", "999999")
    monkeypatch.setenv("BREVITAS_OTEL_MAX_EXPORT_BATCH_SIZE", "999999")
    settings = ObservabilitySettings.from_env()
    assert settings.enabled is False
    assert settings.queue_size == 65_536
    assert settings.batch_size == 8192
    assert valid_correlation_id(settings.instance_id)
    runtime = ObservabilityRuntime(settings)
    with runtime.span("safe.operation") as span:
        assert span is None
    runtime.metrics.record_provider(
        provider="openai", operation="chat", outcome="success", duration_seconds=.1
    )
    runtime.shutdown()
    runtime.shutdown()

    class BrokenTracer:
        def start_as_current_span(self, *_args, **_kwargs):
            raise RuntimeError("exporter unavailable")

    enabled = ObservabilitySettings(enabled=True)
    broken = ObservabilityRuntime(enabled, tracer=BrokenTracer())
    with broken.span("safe.operation") as span:
        assert span is None


def test_span_attributes_are_allowlisted_and_exceptions_never_reach_tracer():
    calls = []

    class SpanManager:
        def __enter__(self):
            calls.append(("enter",))
            return object()

        def __exit__(self, *exception):
            calls.append(("exit", exception))

    class Tracer:
        def start_as_current_span(self, name, **kwargs):
            calls.append(("start", name, kwargs))
            return SpanManager()

    runtime = ObservabilityRuntime(
        ObservabilitySettings(enabled=True), tracer=Tracer()
    )
    secret = "sk_live_123456789 person@example.com private prompt"
    with pytest.raises(RuntimeError, match="private prompt"):
        with runtime.span("provider.request", {
            "http.request.method": "POST",
            "gen_ai.provider.name": "openai",
            "gen_ai.operation.name": "chat",
            "prompt": secret,
            "email": "person@example.com",
            "arbitrary": secret,
        }):
            raise RuntimeError(secret)

    start = next(call for call in calls if call[0] == "start")
    assert start[2]["attributes"] == {
        "http.request.method": "POST",
        "gen_ai.provider.name": "openai",
        "gen_ai.operation.name": "chat",
    }
    assert start[2]["record_exception"] is False
    assert start[2]["set_status_on_exception"] is False
    assert next(call for call in calls if call[0] == "exit")[1] == (None, None, None)
    assert secret not in json.dumps(calls, default=str)
    assert sanitize_span_attributes({"http.route": "/v1/jobs/alice", "prompt": secret}) == {}


def test_partial_otel_construction_is_cleaned_up_with_supported_deadlines():
    shutdowns = []
    constructor_timeouts = []
    resource_attributes = []

    class Component:
        def __init__(self, name):
            self.name = name

        def shutdown(self, timeout_millis=None):
            shutdowns.append((self.name, timeout_millis))

    class Resource:
        @staticmethod
        def create(attributes):
            resource_attributes.append(attributes)
            return object()

    class TracerProvider(Component):
        def __init__(self, **_kwargs):
            super().__init__("tracer_provider")

        def add_span_processor(self, _processor):
            return None

    def span_exporter(**kwargs):
        constructor_timeouts.append(kwargs["timeout"])
        return Component("span_exporter")

    def span_processor(_exporter, **_kwargs):
        return Component("span_processor")

    def metric_exporter(**kwargs):
        constructor_timeouts.append(kwargs["timeout"])
        raise RuntimeError("late metric exporter construction failure")

    settings = ObservabilitySettings(
        enabled=True,
        export_timeout_ms=4321,
        instance_id="abcdef0123456789",
    )
    runtime = observability._build_runtime(settings, components={
        "BatchSpanProcessor": span_processor,
        "MeterProvider": lambda **_kwargs: Component("meter_provider"),
        "OTLPMetricExporter": metric_exporter,
        "OTLPSpanExporter": span_exporter,
        "PeriodicExportingMetricReader": lambda *_args, **_kwargs: Component("reader"),
        "Resource": Resource,
        "TracerProvider": TracerProvider,
    })
    assert runtime.enabled is False
    assert {name for name, _timeout in shutdowns} == {
        "tracer_provider", "span_exporter", "span_processor",
    }
    assert all(timeout == 4321 for _name, timeout in shutdowns)
    assert constructor_timeouts == [4.321, 4.321]
    assert resource_attributes[0]["service.instance.id"] == "abcdef0123456789"


def test_streaming_context_and_full_response_latency_end_after_last_body(monkeypatch):
    events = []

    class RuntimeMetrics:
        def record_api_request(self, **values):
            events.append(("metric", current_request_id(), values))

        def record_service_operation(self, **values):
            events.append(("service", current_request_id(), values))

    class Runtime:
        metrics = RuntimeMetrics()

        @contextmanager
        def span(self, _name, _attributes=None):
            events.append(("span_enter", current_request_id(), {}))
            try:
                yield None
            finally:
                events.append(("span_exit", current_request_id(), {}))

    monkeypatch.setattr(api_observability, "get_runtime", lambda **_kwargs: Runtime())
    app = FastAPI()
    install_fastapi_observability(app, configure_logs=False)

    async def chunks():
        events.append(("chunk_1", current_request_id(), {}))
        yield b"first"
        await asyncio.sleep(.02)
        events.append(("chunk_2", current_request_id(), {}))
        yield b"second"

    @app.get("/v1/stream")
    async def stream():
        return StreamingResponse(chunks())

    with TestClient(app) as client:
        response = client.get("/v1/stream", headers={
            "X-Request-ID": "22222222222222222222222222222222",
        })
    assert response.content == b"firstsecond"
    assert response.headers["X-Brevitas-Request-ID"] == "22222222222222222222222222222222"
    kinds = [event[0] for event in events]
    assert kinds.index("chunk_2") < kinds.index("metric") < kinds.index("span_exit")
    assert all(event[1] == "22222222222222222222222222222222" for event in events)
    metric = next(event[2] for event in events if event[0] == "metric")
    assert metric["duration_seconds"] >= .02


def test_billing_catalog_contract_metric_and_alert_share_fixed_gauge(monkeypatch):
    meter = _Meter()
    metrics = Metrics(meter)

    class Runtime:
        pass

    runtime = Runtime()
    runtime.metrics = metrics
    monkeypatch.setattr(api_observability, "get_runtime", lambda **_kwargs: runtime)
    telemetry = BillingTelemetryAdapter()
    telemetry.metric("billing.catalog_contract_valid", 1)
    telemetry.alert(
        "billing_catalog_contract_invalid", "page", {"catalog_contract_valid": 0}
    )
    catalog = [
        (method, value, attrs) for name, method, value, attrs in meter.calls
        if name == "brevitas.billing.catalog.contract"
    ]
    assert catalog == [("set", 1, {}), ("set", 0, {})]


def test_dashboard_alert_and_collector_definitions_parse_and_cover_required_alerts():
    collector = json.loads((ROOT / "observability/collector/otel-collector.yml").read_text())
    alerts = json.loads((ROOT / "observability/prometheus/alerts.yml").read_text())
    dashboard = json.loads((ROOT / "observability/grafana/enterprise-overview.json").read_text())

    assert set(collector["service"]["pipelines"]) == {"traces", "metrics"}
    assert collector["processors"]["memory_limiter"]["limit_mib"] == 256
    assert collector["exporters"]["otlphttp/monitoring"]["sending_queue"]["queue_size"] == 2048
    rules = [rule for group in alerts["groups"] for rule in group["rules"]]
    names = {rule["alert"] for rule in rules}
    assert names >= {
        "ExternalApiAvailabilityFastBurn",
        "ExternalApiAvailabilitySlowBurn",
        "ExternalApiMonthlySLABreach",
        "InternalAvailabilityFastBurn",
        "InternalAvailabilitySlowBurn",
        "ExternalApiHighLatency",
        "ExternalApiOperationalFailureRate",
        "DurableJobDead",
        "DurableJobQueueLag",
        "BillingEntriesRequireReview",
        "BillingDeadOrStale",
        "BillingCatalogContractInvalid",
        "BillingCatalogContractMissing",
        "PostgresDegraded",
        "RedisDegraded",
        "CompressorDegraded",
    }
    rendered_rules = json.dumps(rules)
    assert "0.001" in rendered_rules  # 99.9% external monthly budget
    assert "0.0005" in rendered_rules  # 99.95% internal budget
    contractual = [rule for rule in rules if rule["alert"] in {
        "ExternalApiAvailabilityFastBurn",
        "ExternalApiAvailabilitySlowBurn",
        "ExternalApiMonthlySLABreach",
    }]
    assert all('sla_eligible=\\"true\\"' in json.dumps(rule) for rule in contractual)
    operational = next(rule for rule in rules if rule["alert"] == "ExternalApiOperationalFailureRate")
    assert "sla_eligible" not in operational["expr"]
    assert "unavailable" in operational["expr"]
    catalog_invalid = next(rule for rule in rules if rule["alert"] == "BillingCatalogContractInvalid")
    catalog_missing = next(rule for rule in rules if rule["alert"] == "BillingCatalogContractMissing")
    assert catalog_invalid["expr"] == "min(brevitas_billing_catalog_contract) < 1"
    assert min([1, 0]) < 1  # one invalid replica must page even while another is valid
    assert catalog_missing["expr"] == "absent(brevitas_billing_catalog_contract) == 1"
    assert len({panel["id"] for panel in dashboard["panels"]}) == len(dashboard["panels"])
    assert dashboard["uid"] == "brevitas-enterprise-overview"


def test_server_posthog_is_pseudonymous_allowlisted_and_bounded():
    helper = (ROOT / "src/lib/posthog-server.ts").read_text()
    assert "pseudonymousDistinctId" in helper
    assert 'createHash("sha256")' in helper
    assert "SAFE_PROPERTIES" in helper
    assert "maxQueueSize: 100" in helper
    assert 'event.event === "$exception"' in helper
    assert "sanitizeProperties(event.properties)" in helper
    assert re.search(r"catch \{[\s\S]+never break a customer-facing request", helper)


def test_normalize_request_id_does_not_truncate_invalid_attacker_input():
    attacker = "a" * 129
    normalized = normalize_request_id(attacker)
    assert normalized != attacker[:128]
    assert len(normalized) == 32
    assert valid_correlation_id(normalized)


def test_environment_template_uses_managed_kms_and_authoritative_billing_contracts():
    template = (ROOT / ".env.example").read_text()
    values = {}
    for raw in template.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value

    assert "BREVITAS_SECRET_KEY" not in values
    assert "BREVITAS_JOB_ENCRYPTION_KEY" not in values
    assert "fernet" not in template.lower()
    for name in (
        "BREVITAS_KMS_REQUIRED",
        "BREVITAS_KMS_PROVIDER",
        "BREVITAS_KMS_KEY_ID",
        "BREVITAS_KMS_KEY_VERSION",
        "BREVITAS_KMS_ALGORITHM",
        "BREVITAS_KMS_ADAPTER_FACTORY",
        "BREVITAS_KMS_ADAPTER_TRUSTED_MODULES",
        "BREVITAS_DATA_KEY_CACHE_TTL_SECONDS",
        "BREVITAS_DATA_KEY_CACHE_MAX_ENTRIES",
        "BREVITAS_LOCAL_KMS_KEY",
    ):
        assert name in values
    assert values["BREVITAS_KMS_REQUIRED"] == "true"
    version = values["BREVITAS_KMS_KEY_VERSION"].lower()
    assert version not in {"latest", "current", "active", "default"}
    assert not version.startswith("alias/")
    assert "module:factory" in template and "registry:factory_name" in template
    assert "forbidden in production" in template

    assert values["BREVITAS_WORKER_BILLING_ROLE"] == "authoritative"
    assert values["BREVITAS_BILLING_POLL_SECONDS"] == "5"
    assert values["BREVITAS_BILLING_LEASE_SECONDS"] == "120"
    assert values["BREVITAS_BILLING_BATCH_SIZE"] == "1"
    assert values["BREVITAS_BILLING_LAG_ALERT_SECONDS"] == "300"
    assert values["BREVITAS_BILLING_REVIEW_ALERT_COUNT"] == "1"
    assert values["BREVITAS_BILLING_DEAD_ALERT_COUNT"] == "1"
    assert values["BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER"] == "false"
    for name in (
        "BREVITAS_PROVIDER_CONNECT_TIMEOUT_S",
        "BREVITAS_PROVIDER_READ_TIMEOUT_S",
        "BREVITAS_PROVIDER_MAX_RETRIES",
        "BREVITAS_PROVIDER_CIRCUIT_FAILURES",
        "BREVITAS_OTEL_ENABLED",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "BREVITAS_OTEL_MAX_QUEUE_SIZE",
    ):
        assert name in values


def test_w1_installed_api_worker_and_compressor_lifecycle_hooks():
    server = (ROOT / "api/server.py").read_text()
    worker = (ROOT / "api/worker.py").read_text()
    compressor = (ROOT / "services/compress/app.py").read_text()
    assert server.count("install_fastapi_observability(app)") == 1
    assert "graceful_observability_shutdown()" in server
    assert server.index("app.include_router(proxy_app.router)") < server.index(
        "install_fastapi_observability(app)"
    )
    assert "with observe_job(" in worker
    assert "telemetry=BillingTelemetryAdapter()" in worker
    assert "graceful_observability_shutdown()" in worker
    assert "shutdown_observability()" in compressor
    assert 'get_runtime(default_service="compressor")' in compressor
