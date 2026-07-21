# Provider reliability controls

The FastAPI provider proxy uses one shared `httpx.AsyncClient` per process lifecycle for
OpenAI, Anthropic, and supported OpenAI-compatible providers. Connection reuse is handled
by the shared bounded pool; FastAPI shutdown closes it deterministically. Synchronous
playground and durable-worker calls use one thread-safe shared `httpx.Client`. SDK clients
wrapped by `brevitas.wrap(...)` expose `close()` and context-manager cleanup for their
underlying SDK pools. The async and synchronous process singletons share the same
thread-safe per-provider circuit state.

## Request policy

- Connect, read, write, and pool waits have separate finite timeouts.
- Retries are bounded and use exponential full jitter.
- A valid `Retry-After` up to the configured retry-delay maximum is honored exactly. A
  longer value is returned to the caller instead of occupying a worker or retrying early.
- Connection and pool failures that occur before any request can reach a provider may be
  retried. A 429 rejection may also be retried. Ambiguous read, write, protocol, and 5xx
  failures on POST are retried only for an internally registered provider+operation
  capability and a provider-supported idempotency key. Caller headers alone never enable
  those retries. The current model-operation capability registry is intentionally empty.
- Caller `Idempotency-Key` is not forwarded to Anthropic and does not prove deduplication.
- A streaming request may retry only before its response is exposed. Once stream bytes are
  available, the proxy never replays the request or duplicates the stream.
- Each provider has an independent closed/open/half-open circuit. State is concurrency
  safe, TTL-expiring, and LRU-bounded. Only one half-open probe is admitted, it remains
  exclusive until a streaming response reaches clean EOF, and active probes are not evicted.
- Public transport errors are generic. Credentials, request content, upstream URLs, and
  raw exception messages are not logged or returned.

## Correlation and telemetry

Immediately before every physical async or sync attempt, the reliability pool copies the
outbound headers and applies `provider_correlation_headers`. Authorization headers remain
intact but never enter telemetry. Each attempt records only the finite provider, operation,
outcome, non-negative duration, and bounded attempt number. Retry attempts emit `retry`;
circuit admission failures emit `circuit_open`; streaming success is recorded only at clean
EOF. URLs, headers, keys, models, tenant IDs, prompts, and responses are never attributes.

Provider failures remain Brevitas-owned/SLA-eligible by default. This layer never marks a
documented upstream outage; only the operations-controlled outage gate at the API boundary
may do that. Callers should not wrap this pool in a second provider metric or correlation
helper, which would double-count the same call.

## Proxy input and process-state bounds

The proxy rejects a declared oversized request before reading its body, and bounds chunked
ASGI reads before appending each chunk. JSON messages and every nested array are limited by
`BREVITAS_REQUEST_MAX_ITEMS`; violations return `413` without forwarding upstream.

Router and session registries use the shared copy-owning `BoundedTTLMap` with finite TTL,
key count, per-value bytes, and aggregate bytes. Immutable router handles serialize every
learned-state mutation. Session handles use a content-aware copier and sizer, and session
updates commit only through `mutate`; returned snapshots are never live aliases. Router
state contains only content-free hashes/counters. Shutdown saves a content-free router
snapshot, closes provider clients, and clears both registries.

## Configuration

All values are optional and clamped to safe bounds.

| Variable | Default | Purpose |
| --- | ---: | --- |
| `BREVITAS_PROVIDER_CONNECT_TIMEOUT_S` | `5` | TCP/TLS connection timeout |
| `BREVITAS_PROVIDER_READ_TIMEOUT_S` | `120` | Response read timeout, including stream reads |
| `BREVITAS_PROVIDER_WRITE_TIMEOUT_S` | `30` | Request upload timeout |
| `BREVITAS_PROVIDER_POOL_TIMEOUT_S` | `5` | Wait for a pooled connection |
| `BREVITAS_PROVIDER_MAX_CONNECTIONS` | `100` | Total process-level connections |
| `BREVITAS_PROVIDER_MAX_KEEPALIVE` | `20` | Idle reusable connections |
| `BREVITAS_PROVIDER_KEEPALIVE_EXPIRY_S` | `30` | Idle connection lifetime |
| `BREVITAS_PROVIDER_MAX_RETRIES` | `2` | Retry attempts after the initial attempt |
| `BREVITAS_PROVIDER_RETRY_BASE_S` | `0.25` | Initial full-jitter ceiling |
| `BREVITAS_PROVIDER_RETRY_MAX_S` | `8` | Maximum jitter or accepted `Retry-After` delay |
| `BREVITAS_PROVIDER_CIRCUIT_FAILURES` | `5` | Logical failures before opening |
| `BREVITAS_PROVIDER_CIRCUIT_OPEN_S` | `30` | Open interval before a half-open probe |
| `BREVITAS_PROVIDER_CIRCUIT_TTL_S` | `900` | Inactive provider-state lifetime |
| `BREVITAS_PROVIDER_MAX_STATES` | `32` | Maximum provider circuit entries |

## Synchronous integration contract

`api/server.py` playground/configured-model backends and the durable worker should replace
direct `requests.post` calls with the process singleton below. `operation` must be a stable,
content-free identifier such as `messages` or `chat.completions`; it is used only for the
internal retry-capability lookup and must not be chosen from a request header.

```python
from brevitas.provider_reliability import (
    ProviderCircuitOpen,
    close_provider_sync_clients,
    provider_sync_http,
)

response = provider_sync_http.request(
    provider="anthropic",
    operation="messages",
    method="POST",
    url="https://api.anthropic.com/v1/messages",
    headers=sanitized_provider_headers,
    json=provider_payload,
)
```

Correlation and per-attempt metrics are already applied inside `request`; do not add an
outer `observe_provider_call` or `outbound_provider_headers` wrapper.

The caller continues to parse the provider-specific response and must translate
`ProviderCircuitOpen` and `httpx.TransportError` into generic errors without logging raw
headers, bodies, URLs, or exception messages. The callable is thread-safe. After request
threads drain, both the API lifespan and the durable worker's shutdown `finally` block must
call `close_provider_sync_clients()` exactly once (repeat calls are safe). The async proxy
continues to call `await close_provider_clients()` through its router shutdown hook.

A client retrying a proxy `502`, `503`, or `504` remains responsible for idempotency; a
non-deduplicated request may have been accepted upstream even when its response was lost.
The hosted proxy also closes an active downstream/upstream stream when Redis cannot renew
its hierarchical concurrency lease or reports that its exact lease member is no longer
owned. This prevents further response delivery and receipt work, but cancellation cannot
retract provider work or charges already accepted before lease loss. Provider idempotency
and restricted provider-side budgets remain necessary for that external side effect.

## Durable worker ambiguity fence

Durable chat jobs persist an ownership- and lease-fenced outbound marker immediately before the
provider POST. If the worker crashes or loses its lease after that marker, a later claim moves the
job to `dead` with the content-free code `provider_outcome_ambiguous`; it never automatically
replays the provider call. Proven pre-acceptance outcomes—circuit admission rejection, connection
or pool failure, and a definite 429 rejection—may clear the marker and use the bounded job retry.
Read/write ambiguity, provider 5xx responses, and malformed success responses retain the marker.
Compression jobs do not use this marker.

This is an at-most-once replay fence, not an exactly-once provider guarantee. A remote call may
have completed even when Brevitas cannot commit its result. Automatic recovery would require a
verified provider idempotency or result-reconciliation contract; the current supported Chat
Completions contracts do not provide one that this worker can rely on. Migration
`202607200015_provider_outbound_ambiguity.sql` must be applied before deploying the matching
worker, and marker fields are never returned in the public job status.

## Public SDK client lifecycle

`BrevitasDropIn` and its public alias `BrevitasClient` own the provider SDK pool they create.
They support `close()`/`aclose()`, `with`, and `async with`. Closure is idempotent and
thread-safe; async cleanup completes before caller cancellation is re-raised, without
leaving a background task. Switching a drop-in between providers waits for active calls,
closes the prior SDK client exactly once, and only then installs the replacement. Provider
close errors are suppressed without logging exception text or credentials, so context
cleanup never masks the application exception. A closed drop-in is terminal and rejects
new routes.
