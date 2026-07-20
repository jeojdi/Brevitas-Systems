# Enterprise observability

This repository provides content-free JSON logs, request/job correlation, bounded
OpenTelemetry traces and metrics, a collector template, a Grafana dashboard, and
Prometheus-compatible alert rules. It does not provision or contact a monitoring
provider.

## Data policy

Telemetry may contain route templates, HTTP methods, status classes, finite provider/
operation/outcome labels, durations, opaque request IDs, opaque job IDs, and trace IDs.
It must never contain request or response bodies, prompts, messages, content, names,
email addresses, credentials, authorization/cookie headers, raw URLs/query strings, or
customer-controlled exception messages.

Span attributes have a separate fixed allowlist: method, registered route template,
finite operation/provider/dependency/outcome, and bounded status code. Arbitrary span
attributes are dropped. OpenTelemetry automatic exception recording and automatic error
status are disabled; span context managers never receive the application exception,
traceback, locals, or message. Failures are represented only by finite metrics and the
exception class name in content-free JSON logs.

`JsonLogFormatter` serializes an allowlist and deliberately discards the free-form log
message and arguments. `StructuredLogger` is the preferred producer. The allowlist and
defensive redaction in `brevitas.observability` are local controls; the credential
security layer is then applied before serialization/export and must not weaken this
policy. `brevitas.security.redact()` receives only the already-allowlisted flat log/span/
metric structure. `redact_exception()` runs at the exception boundary, after which only
its validated exception type is retained; its message, causes, attributes, and locals are
discarded.
PostHog server events hash distinct IDs, accept only bounded scalar properties from a
fixed allowlist, disable person profiles/geolocation, and drop exception events before
send.

Operational API logs have a 30-day retention target. Security and immutable
administrative audit records are separate records with a 400-day target; do not copy an
audit record's payload into general telemetry.

## Runtime configuration

OpenTelemetry is disabled by default. Each Railway service sets a finite
`OTEL_SERVICE_NAME`: `api`, `worker`, `billing-worker`, or `compressor`.

Required when enabled:

- `BREVITAS_OTEL_ENABLED=true`
- `OTEL_EXPORTER_OTLP_ENDPOINT=https://...` for an OTLP/HTTP collector or provider
- `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`
- `OTEL_EXPORTER_OTLP_HEADERS` as a managed secret when provider authentication is needed
- `BREVITAS_ENV=staging|production`

Each process supplies an opaque `service.instance.id` resource attribute, using a valid
`OTEL_SERVICE_INSTANCE_ID`/`RAILWAY_REPLICA_ID` or a generated hexadecimal ID. This keeps
per-replica last-value gauges distinct without using a hostname or customer value.

Batching is bounded by `BREVITAS_OTEL_MAX_QUEUE_SIZE` (default 2,048) and
`BREVITAS_OTEL_MAX_EXPORT_BATCH_SIZE` (default 256). Span export has a five-second
timeout and metrics export every 60 seconds by default. All values are clamped.
Disabled mode has no background threads. Missing SDKs, exporter construction failures,
instrument failures, propagation failures, and flush failures do not fail startup or a
customer request. Owning lifespans call `graceful_observability_shutdown()` after
application work drains; shutdown flushes with a bounded deadline and is idempotent.
Provider/exporter constructors receive the same export timeout, shutdown receives a
deadline wherever the installed SDK supports one, and a partially constructed exporter/
processor/provider graph is closed in reverse order before falling back to no-op mode.

Provider credentials and durable-job payloads use the managed-KMS envelope contract in
`docs/CREDENTIAL_SECURITY.md`. Production sets `BREVITAS_KMS_REQUIRED=true`, an explicit
provider/key resource/immutable key version/algorithm, and a trusted adapter factory
(`module:factory` with an exact module allowlist, or a pre-registered `registry:name`).
Unwrapped DEK caching is TTL/LRU bounded. `BREVITAS_LOCAL_KMS_KEY` is development/test
only and forbidden in production. The obsolete process-held `BREVITAS_SECRET_KEY` and
`BREVITAS_JOB_ENCRYPTION_KEY` are not production configuration.

The collector template is `observability/collector/otel-collector.yml`. It accepts OTLP
only, applies a 256 MiB memory limiter and bounded batch/queue processors, and forwards
traces and metrics to one provider. Configure its upstream endpoint and authorization
as managed Railway secrets. Do not expose its receiver or health port publicly.

## Integration contracts

These hooks intentionally live outside files owned by other implementation workers.
They are required before production observability is complete.

### Public API and health/compressor (production topology owner)

Immediately after constructing the FastAPI app:

```python
from api.observability import install_fastapi_observability

install_fastapi_observability(app)
```

In the existing lifespan's final drain, after provider/Redis clients stop:

```python
from api.observability import graceful_observability_shutdown

graceful_observability_shutdown()
```

The middleware validates, rather than truncates, `X-Brevitas-Request-ID`, `X-Request-ID`,
or `X-Client-Request-ID`; invalid/unbounded values are replaced with a random 128-bit ID.
Accepted caller IDs are opaque hexadecimal/UUID forms only (plus the internal `job:`
form), so a syntactically valid human name cannot become telemetry.
It binds `request.state.brevitas_request_id` and returns both
`X-Brevitas-Request-ID` and `X-Request-ID`. CORS configuration must expose those two
response headers. It never reads a body. Route metrics use the framework route template;
only registered templates made from the fixed service-route vocabulary are accepted, and
unmatched/raw name, email, numeric, UUID, or other identifier paths collapse to
`unmatched`. The ASGI middleware remains active through the final response body byte, so
request context, trace lifetime, and `brevitas.api.request.duration` all represent full-
response latency for normal and streaming responses—not merely time to first byte.

At server-owned customer error translation boundaries, classify the two customer
contractual exclusions explicitly:

```python
from api.observability import mark_documented_upstream_outage, mark_request_fault_domain

mark_request_fault_domain(request, "customer_credentials")
mark_request_fault_domain(request, "customer_configuration")

# This excludes only while operations-controlled outage variables identify both
# this provider and a bounded incident reference.
mark_documented_upstream_outage(request, provider)
```

Ordinary provider failures, timeouts, rate limits, and circuit rejections are Brevitas-
owned and SLA-eligible; a failure is not evidence of a documented upstream outage. The
only upstream exclusion is `documented_upstream_outage`, which the generic marker cannot
set. The dedicated marker requires the provider in
`BREVITAS_DOCUMENTED_UPSTREAM_OUTAGES` and a bounded
`BREVITAS_DOCUMENTED_UPSTREAM_OUTAGE_REFERENCE`. Operations must set/clear both through
incident change control after verifying the provider incident. Unknown classifications
fail closed to `brevitas`. Brevitas-owned authentication, configuration, database, Redis,
compressor, provider integration, and internal defects remain `brevitas`.

The worker and compressor set content-free handlers before serving:

```python
from brevitas.observability import configure_json_logging

configure_json_logging(service="worker", logger_names=("brevitas.worker",))
# compressor process uses service="compressor" and its logger name
```

Wrap each durable execution with `api.observability.observe_job(row["id"], operation)`.
The private compressor records every request and health dependency result with:

```python
runtime.metrics.record_dependency(
    dependency="compressor", outcome="success", duration_seconds=elapsed
)
runtime.metrics.record_service_operation(service="compressor", outcome="success")
```

The compressor image already copies `brevitas/observability.py`; it must also copy the
shared `brevitas/security/redaction.py` module (or package the full shared modules) before
production. The standalone observability module has a strict content-free fallback to
avoid startup failure, but production should use the shared W6 redactor.

### Provider HTTP reliability owner

Immediately before every outbound attempt, preserving existing authorization headers:

```python
from brevitas.observability import get_runtime, provider_correlation_headers

headers = provider_correlation_headers(headers)
started = time.perf_counter()
# make exactly one provider attempt
get_runtime().metrics.record_provider(
    provider=provider,
    operation=operation,
    outcome="success",  # error|timeout|retry|circuit_open as applicable
    duration_seconds=time.perf_counter() - started,
    attempt=attempt,
)
```

The helper injects the validated request ID plus W3C trace context without inspecting or
logging other headers. Record every physical attempt, including circuit rejections.
Provider and operation labels collapse unknown values and attempts clamp to five retries.
Alternatively, synchronous call sites may use `api.observability.observe_provider_call`.
Provider instrumentation must not directly exclude timeouts, errors, or circuit opens
from the SLA. The API translation boundary may call `mark_documented_upstream_outage`;
its operations-controlled incident gate decides eligibility.

### Database scaling owner

Measure each production Postgres RPC/query/batch at its existing boundary:

```python
get_runtime().metrics.record_dependency(
    dependency="postgres", outcome="success", duration_seconds=elapsed
)
```

Use only `success`, `timeout`, `error`, or `unavailable`; never label by SQL text, table,
tenant, cursor, row ID, request ID, or batch contents. A truncated page/batch may emit the
fixed structured event `database_batch_truncated` with numeric duration only. Record
Redis calls identically with `dependency="redis"`.

### Billing recovery owner

Pass the adapter when constructing the authoritative Railway recovery processor:

```python
from api.observability import BillingTelemetryAdapter

processor = build_billing_recovery_processor_from_env(
    telemetry=BillingTelemetryAdapter(),
)
```

The adapter maps the existing `BillingTelemetry` protocol to fixed instruments for
reported/dead/review entries, leases lost, batch duration, pending lag, stale sends, and
review/dead gauges. `billing.catalog_contract_valid` sets the fixed catalog-contract gauge
to 1 or 0, and `billing_catalog_contract_invalid` also forces it to 0. It ignores unknown
dynamic metric names and never emits ledger IDs. The invalid rule uses `min(...)`, so one
invalid replica pages even when another reports valid. A separate `absent(...)` rule pages
when no authoritative billing loop reports the gauge for five minutes. The matching rules
also page on dead/stale entries and queue lag and ticket on review.

### Cache/resource-bound owner

Record finite cache outcomes only:

```python
get_runtime().metrics.record_cache(cache="semantic", outcome="hit")
```

Allowed cache labels are `auth_context`, `provider_state`, `semantic`, and `session`;
outcomes are `disabled`, `error`, `evicted`, `hit`, `miss`, and `write`. Do not label by
cache key, tenant, model string, TTL, prompt hash, or content.

### Company administration owner

The immutable database audit log is authoritative. General telemetry may emit a fixed
event such as `admin_audit_committed` in the current request context, but no actor ID,
target ID, email, service-account name, role-change payload, or audit metadata. Admin
route request count/latency is already covered by the API middleware route template.

### Credential security owner

The observability boundary applies the reusable credential redactor after its strict
content-free allowlist and before JSON serialization or OpenTelemetry recording. Preserve
the stricter rule: free-form exception text, stack locals, raw SDK objects, full URLs,
headers, encryption context, wrapped/plaintext keys, and ciphertext are not exported even
after pattern redaction. KMS failures expose only the fixed `KMSConfigurationError`,
`KMSUnavailable`, or envelope error type; never the provider SDK message. Never configure
`OTEL_EXPORTER_OTLP_HEADERS` through a public environment variable.

## Metric catalog

OpenTelemetry names use dots; Prometheus translation replaces dots with underscores,
adds `_total` to counters, and adds `_seconds` to second-based instruments.

| Area | Instruments | Finite labels |
| --- | --- | --- |
| API | `brevitas.api.requests`, `brevitas.api.request.duration` | method, route template, outcome, surface, finite fault domain, SLA eligibility |
| Internal availability | `brevitas.service.operations` | service, surface, outcome |
| Providers | `brevitas.provider.requests`, `.request.duration`, `.retries`, `.circuit` | provider, operation, outcome/state |
| Jobs/queue | `brevitas.jobs`, `.job.duration`, `.queue.depth`, `.queue.lag` | operation/status or queue |
| Cache | `brevitas.cache.operations` | cache, outcome |
| Billing | `brevitas.billing.entries`, `.recovery`, `.batch.duration`, `.queue.lag`, `.review`, `.dead`, `.stale`, `.catalog.contract` | status/outcome only; catalog gauge has no label |
| Dependencies | `brevitas.dependency.operations`, `.dependency.duration` | postgres/redis/compressor/provider/stripe, outcome |

Request IDs, job IDs, tenants, customers, service accounts, API keys, raw paths, model
IDs, exception messages, and hostnames are prohibited metric labels. The opaque standard
`service.instance.id` resource attribute is the sole replica identity and is generated or
strictly validated; it is required to evaluate per-replica last-value health gauges.

## Dashboards and alerts

`observability/grafana/enterprise-overview.json` covers 30-day external API availability,
p95 latency, provider outcomes, durable jobs, queue lag, billing risk, and dependency
failures. Import it only after verifying the monitoring provider's OpenTelemetry-to-
Prometheus name translation.

`observability/prometheus/alerts.yml` is JSON-compatible YAML so it can be parsed without
template evaluation. It includes:

- 99.9% monthly external API SLA: 1-hour/5-minute fast burn, 6-hour/30-minute slow burn,
  and an explicit rolling 30-day breach alert. Both numerator and denominator select only
  `sla_eligible="true"`; only operations-documented upstream outages and customer
  credential/configuration failures are excluded by the finite classification above.
- 99.95% internal availability: the same fast and slow multi-window method with a 0.05%
  monthly error budget.
- A separate operational API failure-ratio alert includes both Brevitas-owned errors and
  SLA-excluded upstream unavailability so contractual exclusions never hide degradation.
- API p95 full-response latency, provider errors/circuit opens, dead jobs and five-minute
  queue lag, billing lag/review/dead/stale/catalog states (including any-replica invalid
  and authoritative-loop absent), and Postgres/Redis/compressor degradation.

Validate expressions against the selected provider in staging. Do not route notifications
from this repository run; notification destinations and escalation policies are an
environment-side staging task.

## Incident checks

### Availability burn

Confirm whether the alert is external API or internal service availability, inspect the
short and long windows, correlate by trace/request ID, and check Railway replica health.
Never paste request payloads into the incident channel. P0 acknowledgement target is 30
minutes, with customer updates every hour.

### API errors and latency

Group only by route template and finite status/fault outcome. Check Postgres, Redis,
compressor, and provider panels before rollback. A customer credential/configuration or
operations-documented upstream outage may be SLA-excluded but remains on operational
panels and must still degrade safely. Ordinary provider errors/timeouts/circuit opens are
SLA-eligible. Never change eligibility based on a request header/body value or an isolated
provider request failure.

### Provider degradation

Check finite provider outcome, retry, and circuit metrics. Do not log provider response
bodies or transport error strings. Confirm retries apply only to operations the reliability
layer considers safe.

### Jobs and queue

For queue lag, confirm worker readiness and authoritative Postgres lease recovery before
using a job ID for an internal database lookup. A dead job pages; inspect its protected
database error classification, not its encrypted payload.

### Billing recovery

Stop duplicate meter writers, verify the exclusive-writer setting, then use the recovery
worker's reconciliation flow. Review/dead/stale alerts must not be cleared by direct row
edits. A catalog alert from `min(...)` identifies at least one invalid replica; an
`absent(...)` alert means the authoritative loop is not reporting at all. Manual recovery
remains authenticated and auditable.

### Dependencies

Postgres is authoritative and pages on degradation. Redis is coordination infrastructure;
fail closed where admission/leases require it and recover from Postgres. Compressor
degradation must preserve the API's documented safe fallback behavior.
