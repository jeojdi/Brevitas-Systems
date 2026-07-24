"""Deterministic provider reliability tests; no live provider calls."""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import HTTPException, Request

from brevitas.provider_reliability import (
    ProviderCircuitBreaker,
    ProviderCircuitOpen,
    ProviderHTTPClientPool,
    ProviderReliabilityConfig,
    ProviderSyncHTTPClientPool,
)
from brevitas.resource_bounds import ResourceBounds


class _Response:
    def __init__(self, status_code: int = 200, *, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1

    def close(self) -> None:
        self.closed += 1


def _config(**overrides) -> ProviderReliabilityConfig:
    values = {
        "connect_timeout_s": 1.5,
        "read_timeout_s": 20.0,
        "write_timeout_s": 4.0,
        "pool_timeout_s": 0.75,
        "max_connections": 7,
        "max_keepalive_connections": 3,
        "keepalive_expiry_s": 12.0,
        "max_retries": 2,
        "retry_base_s": 0.2,
        "retry_max_s": 3.0,
        "circuit_failure_threshold": 3,
        "circuit_open_s": 10.0,
        "circuit_state_ttl_s": 30.0,
        "max_provider_states": 4,
    }
    values.update(overrides)
    return ProviderReliabilityConfig(**values)


def _open_circuit(circuit: ProviderCircuitBreaker, provider: str) -> None:
    permit = circuit.before_request(provider)
    circuit.record_failure(permit)


def test_environment_configuration_is_parsed_and_clamped(monkeypatch):
    monkeypatch.setenv("BREVITAS_PROVIDER_CONNECT_TIMEOUT_S", "2.5")
    monkeypatch.setenv("BREVITAS_PROVIDER_READ_TIMEOUT_S", "99999")
    monkeypatch.setenv("BREVITAS_PROVIDER_MAX_CONNECTIONS", "0")
    monkeypatch.setenv("BREVITAS_PROVIDER_MAX_RETRIES", "999")
    config = ProviderReliabilityConfig.from_env()
    assert config.connect_timeout_s == 2.5
    assert config.read_timeout_s == 900.0
    assert config.max_connections == 1
    assert config.max_retries == 5


def test_one_async_process_pool_has_explicit_limits_and_shutdown(monkeypatch):
    created = []

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = 0
            created.append(self)

        async def post(self, url, **kwargs):
            return _Response()

        async def aclose(self):
            self.closed += 1

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    pool = ProviderHTTPClientPool(_config())

    async def exercise():
        await pool.request(
            "openai", "chat.completions", "POST", "https://provider.invalid/a")
        await pool.request(
            "anthropic", "messages", "POST", "https://provider.invalid/b")
        assert pool.client_count() == 1
        await pool.aclose()

    asyncio.run(exercise())
    assert len(created) == 1
    assert created[0].closed == 1
    timeout = created[0].kwargs["timeout"]
    limits = created[0].kwargs["limits"]
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (1.5, 20.0, 4.0, 0.75)
    assert (limits.max_connections, limits.max_keepalive_connections) == (7, 3)
    assert limits.keepalive_expiry == 12.0
    assert created[0].kwargs["follow_redirects"] is False
    assert created[0].kwargs["trust_env"] is False


def test_sync_pool_is_thread_safe_reused_and_deterministically_closed(monkeypatch):
    created = []

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = 0
            self.closed = 0
            self.lock = threading.Lock()
            created.append(self)

        def request(self, method, url, **kwargs):
            with self.lock:
                self.calls += 1
            return _Response()

        def close(self):
            self.closed += 1

    monkeypatch.setattr(httpx, "Client", Client)
    pool = ProviderSyncHTTPClientPool(_config())

    def call(_):
        return pool.request(
            "openai", "chat.completions", "POST", "https://provider.invalid").status_code

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(call, range(24))) == [200] * 24
    assert len(created) == 1
    assert created[0].calls == 24
    assert pool.client_count() == 1
    pool.close()
    pool.close()
    assert created[0].closed == 1
    assert pool.client_count() == 0


def test_sync_unsupported_provider_does_not_trust_caller_idempotency_key(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            self.calls = 0

        def request(self, method, url, **kwargs):
            self.calls += 1
            return _Response(503 if self.calls == 1 else 200)

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", Client)
    pool = ProviderSyncHTTPClientPool(_config())
    response = pool.request(
        "unverified-provider", "jobs.create", "POST", "https://provider.invalid",
        headers={"Idempotency-Key": "caller-asserted-only"},
    )
    assert response.status_code == 503
    assert pool._client[0].calls == 1


def test_connect_failures_retry_with_bounded_exponential_full_jitter(monkeypatch):
    sleeps = []

    class Client:
        def __init__(self, **kwargs):
            self.calls = 0

        async def post(self, url, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise httpx.ConnectError("sensitive transport detail")
            return _Response()

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    async def sleep(delay):
        sleeps.append(delay)

    pool = ProviderHTTPClientPool(_config(), sleep=sleep, random_value=lambda: 0.5)
    asyncio.run(pool.request(
        "openai", "chat.completions", "POST", "https://provider.invalid"))
    assert pool._client[0].calls == 3
    assert sleeps == pytest.approx([0.1, 0.2])


@pytest.mark.parametrize("provider,operation", [
    ("anthropic", "messages"),
    ("unverified-provider", "jobs.create"),
])
@pytest.mark.parametrize("failure", [httpx.WriteError, httpx.ReadTimeout])
def test_arbitrary_idempotency_key_never_retries_ambiguous_unsupported_call(
        monkeypatch, provider, operation, failure):
    class Client:
        def __init__(self, **kwargs):
            self.calls = 0

        async def post(self, url, **kwargs):
            self.calls += 1
            raise failure("request acceptance is ambiguous")

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    pool = ProviderHTTPClientPool(_config())
    with pytest.raises(failure):
        asyncio.run(pool.request(
            provider, operation, "POST", "https://provider.invalid",
            headers={"Idempotency-Key": "caller-asserted-only"}))
    assert pool._client[0].calls == 1


@pytest.mark.parametrize("provider,operation", [
    ("anthropic", "messages"),
    ("unverified-provider", "jobs.create"),
])
def test_arbitrary_idempotency_key_never_retries_5xx_unsupported_call(
        monkeypatch, provider, operation):
    class Client:
        def __init__(self, **kwargs):
            self.calls = 0

        async def post(self, url, **kwargs):
            self.calls += 1
            return _Response(503 if self.calls == 1 else 200)

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    pool = ProviderHTTPClientPool(_config())
    response = asyncio.run(pool.request(
        provider, operation, "POST", "https://provider.invalid",
        headers={"Idempotency-Key": "caller-asserted-only"}))
    assert response.status_code == 503
    assert pool._client[0].calls == 1


def test_internal_provider_operation_capability_allows_deduplicated_retry(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            self.calls = 0

        async def post(self, url, **kwargs):
            self.calls += 1
            return _Response(503 if self.calls == 1 else 200)

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    pool = ProviderHTTPClientPool(
        _config(),
        idempotency_capabilities=frozenset({("verified", "jobs.create")}),
        sleep=lambda _: asyncio.sleep(0),
    )
    response = asyncio.run(pool.request(
        "verified", "jobs.create", "POST", "https://provider.invalid",
        headers={"Idempotency-Key": "provider-supported-key"}))
    assert response.status_code == 200
    assert pool._client[0].calls == 2


def test_retry_after_is_respected_for_definite_429_rejection(monkeypatch):
    sleeps = []
    first = _Response(429, headers={"Retry-After": "2"})

    class Client:
        def __init__(self, **kwargs):
            self.responses = [first, _Response(200)]

        async def post(self, url, **kwargs):
            return self.responses.pop(0)

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    async def sleep(delay):
        sleeps.append(delay)

    pool = ProviderHTTPClientPool(_config(), sleep=sleep)
    response = asyncio.run(pool.request(
        "openai", "chat.completions", "POST", "https://provider.invalid"))
    assert response.status_code == 200
    assert sleeps == [2.0]
    assert first.closed == 1


def test_retry_after_over_bound_returns_without_retrying(monkeypatch):
    first = _Response(429, headers={"Retry-After": "120"})

    class Client:
        def __init__(self, **kwargs):
            self.calls = 0

        async def post(self, url, **kwargs):
            self.calls += 1
            return first

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    pool = ProviderHTTPClientPool(_config(retry_max_s=3.0))
    response = asyncio.run(pool.request(
        "openai", "chat.completions", "POST", "https://provider.invalid"))
    assert response is first
    assert pool._client[0].calls == 1
    assert first.closed == 0


def test_anthropic_passthrough_does_not_forward_caller_idempotency_key():
    import brevitas.proxy as proxy

    request = Request({
        "type": "http", "method": "POST", "scheme": "https", "path": "/v1/messages",
        "query_string": b"", "server": ("test", 443), "client": ("test", 1),
        "headers": [
            (b"x-api-key", b"provider-key"),
            (b"idempotency-key", b"caller-asserted-only"),
        ],
    })
    headers = proxy._passthrough_headers(request, "anthropic")
    assert headers["x-api-key"] == "provider-key"
    assert "idempotency-key" not in headers


def test_half_open_stream_remains_exclusive_until_clean_eof(monkeypatch):
    now = [0.0]
    config = _config(circuit_failure_threshold=1, circuit_open_s=5.0)
    circuit = ProviderCircuitBreaker(config, clock=lambda: now[0])
    _open_circuit(circuit, "openai")
    now[0] = 6.0

    class StreamingResponse(_Response):
        async def aiter_bytes(self):
            yield b"one"
            yield b"two"

    class Client:
        def __init__(self, **kwargs):
            self.sends = 0

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, *, stream):
            self.sends += 1
            return StreamingResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    pool = ProviderHTTPClientPool(config, circuits=circuit)

    async def consume():
        response = await pool.request(
            "openai", "chat.completions", "POST", "https://provider.invalid", stream=True)
        with pytest.raises(ProviderCircuitOpen):
            circuit.before_request("openai")
        chunks = [chunk async for chunk in pool.iter_bytes("openai", response)]
        permit = circuit.before_request("openai")
        circuit.abandon(permit)
        return chunks

    assert asyncio.run(consume()) == [b"one", b"two"]
    assert pool._client[0].sends == 1


def test_half_open_stream_transport_failure_reopens_without_replay(monkeypatch):
    now = [0.0]
    config = _config(circuit_failure_threshold=1, circuit_open_s=5.0)
    circuit = ProviderCircuitBreaker(config, clock=lambda: now[0])
    _open_circuit(circuit, "openai")
    now[0] = 6.0

    class StreamingResponse(_Response):
        async def aiter_bytes(self):
            yield b"first-and-only-copy"
            raise httpx.ReadError("stream interrupted")

    class Client:
        def __init__(self, **kwargs):
            self.sends = 0

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, *, stream):
            self.sends += 1
            return StreamingResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    pool = ProviderHTTPClientPool(config, circuits=circuit)

    async def consume():
        response = await pool.request(
            "openai", "chat.completions", "POST", "https://provider.invalid", stream=True)
        chunks = []
        with pytest.raises(httpx.ReadError):
            async for chunk in pool.iter_bytes("openai", response):
                chunks.append(chunk)
        with pytest.raises(ProviderCircuitOpen):
            circuit.before_request("openai")
        return chunks

    assert asyncio.run(consume()) == [b"first-and-only-copy"]
    assert pool._client[0].sends == 1


def test_half_open_stream_cancellation_abandons_probe(monkeypatch):
    now = [0.0]
    config = _config(circuit_failure_threshold=1, circuit_open_s=5.0)
    circuit = ProviderCircuitBreaker(config, clock=lambda: now[0])
    _open_circuit(circuit, "openai")
    now[0] = 6.0

    class StreamingResponse(_Response):
        async def aiter_bytes(self):
            raise asyncio.CancelledError()
            yield b"unreachable"

    class Client:
        def __init__(self, **kwargs):
            self.sends = 0

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, *, stream):
            self.sends += 1
            return StreamingResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    pool = ProviderHTTPClientPool(config, circuits=circuit)

    async def consume():
        response = await pool.request(
            "openai", "chat.completions", "POST", "https://provider.invalid", stream=True)
        async for _ in pool.iter_bytes("openai", response):
            pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(consume())
    permit = circuit.before_request("openai")
    circuit.abandon(permit)
    assert pool._client[0].sends == 1


def test_pool_shutdown_closes_and_abandons_unconsumed_half_open_stream(monkeypatch):
    now = [0.0]
    config = _config(circuit_failure_threshold=1, circuit_open_s=5.0)
    circuit = ProviderCircuitBreaker(config, clock=lambda: now[0])
    _open_circuit(circuit, "openai")
    now[0] = 6.0
    raw = _Response()

    class Client:
        def __init__(self, **kwargs):
            self.closed = 0

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, *, stream):
            return raw

        async def aclose(self):
            self.closed += 1

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    pool = ProviderHTTPClientPool(config, circuits=circuit)

    async def exercise():
        await pool.request(
            "openai", "chat.completions", "POST", "https://provider.invalid", stream=True)
        with pytest.raises(ProviderCircuitOpen):
            circuit.before_request("openai")
        client = pool._client[0]
        await pool.aclose()
        permit = circuit.before_request("openai")
        circuit.abandon(permit)
        return client

    client = asyncio.run(exercise())
    assert raw.closed == 1
    assert client.closed == 1


def test_non_httpx_exit_abandons_half_open_probe(monkeypatch):
    now = [0.0]
    config = _config(circuit_failure_threshold=1, circuit_open_s=5.0)
    circuit = ProviderCircuitBreaker(config, clock=lambda: now[0])
    _open_circuit(circuit, "openai")
    now[0] = 6.0

    class Client:
        def __init__(self, **kwargs):
            pass

        async def post(self, url, **kwargs):
            raise ValueError("unexpected client adapter failure")

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    pool = ProviderHTTPClientPool(config, circuits=circuit)
    with pytest.raises(ValueError):
        asyncio.run(pool.request(
            "openai", "chat.completions", "POST", "https://provider.invalid"))
    permit = circuit.before_request("openai")
    circuit.abandon(permit)


def test_pre_transport_policy_exception_also_abandons_half_open_probe():
    now = [0.0]
    config = _config(circuit_failure_threshold=1, circuit_open_s=5.0)
    circuit = ProviderCircuitBreaker(config, clock=lambda: now[0])
    _open_circuit(circuit, "openai")
    now[0] = 6.0
    pool = ProviderHTTPClientPool(config, circuits=circuit)

    with pytest.raises(AttributeError):
        asyncio.run(pool.request(
            "openai", None, "POST", "https://provider.invalid"))  # type: ignore[arg-type]
    permit = circuit.before_request("openai")
    circuit.abandon(permit)


def test_circuit_half_open_allows_exactly_one_concurrent_probe():
    now = [0.0]
    config = _config(circuit_failure_threshold=1, circuit_open_s=5.0)
    circuit = ProviderCircuitBreaker(config, clock=lambda: now[0])
    _open_circuit(circuit, "openai")
    now[0] = 6.0
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait()
        try:
            return circuit.before_request("openai")
        except ProviderCircuitOpen:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(lambda _: attempt(), range(8)))
    probes = [permit for permit in outcomes if permit is not None]
    assert len(probes) == 1
    assert outcomes.count(None) == 7
    circuit.record_success(probes[0])


def test_older_inflight_success_cannot_clear_newer_half_open_probe():
    now = [0.0]
    config = _config(circuit_failure_threshold=1, circuit_open_s=5.0)
    circuit = ProviderCircuitBreaker(config, clock=lambda: now[0])
    older = circuit.before_request("openai")
    failing = circuit.before_request("openai")
    circuit.record_failure(failing)
    now[0] = 6.0
    probe = circuit.before_request("openai")

    circuit.record_success(older)
    with pytest.raises(ProviderCircuitOpen):
        circuit.before_request("openai")
    circuit.record_success(probe)
    permit = circuit.before_request("openai")
    circuit.abandon(permit)


def test_capacity_never_evicts_active_half_open_probes():
    now = [0.0]
    config = _config(
        circuit_failure_threshold=1, circuit_open_s=5.0, max_provider_states=2)
    circuit = ProviderCircuitBreaker(config, clock=lambda: now[0])
    _open_circuit(circuit, "openai")
    _open_circuit(circuit, "anthropic")
    now[0] = 6.0
    openai_probe = circuit.before_request("openai")
    anthropic_probe = circuit.before_request("anthropic")

    with pytest.raises(ProviderCircuitOpen):
        circuit.before_request("deepseek")
    assert circuit.state_count() == 2
    with pytest.raises(ProviderCircuitOpen):
        circuit.before_request("openai")
    with pytest.raises(ProviderCircuitOpen):
        circuit.before_request("anthropic")

    circuit.abandon(openai_probe)
    deepseek_permit = circuit.before_request("deepseek")
    assert circuit.state_count() == 2
    with pytest.raises(ProviderCircuitOpen):
        circuit.before_request("anthropic")
    circuit.abandon(anthropic_probe)
    circuit.abandon(deepseek_permit)


def test_circuit_state_is_lru_and_ttl_bounded():
    now = [0.0]
    circuit = ProviderCircuitBreaker(
        _config(max_provider_states=2, circuit_state_ttl_s=10.0),
        clock=lambda: now[0],
    )
    for provider in ("openai", "anthropic", "deepseek"):
        permit = circuit.before_request(provider)
        circuit.record_success(permit)
    assert circuit.state_count() == 2
    now[0] = 11.0
    assert circuit.state_count() == 0


def test_proxy_transport_error_is_sanitized(monkeypatch):
    import brevitas.proxy as proxy

    class FailingPool:
        async def request(self, *args, **kwargs):
            raise httpx.ConnectError("Bearer real-secret and private prompt")

    monkeypatch.setattr(proxy, "provider_http", FailingPool())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(proxy._provider_request(
            "openai", "chat.completions", "POST", "https://provider.invalid",
            headers={"Authorization": "Bearer real-secret"},
            json_body={"input": "private prompt"},
        ))
    assert caught.value.status_code == 502
    assert caught.value.detail == "Model provider connection failed"
    assert "secret" not in caught.value.detail
    assert "prompt" not in caught.value.detail


def test_each_physical_attempt_gets_correlation_and_content_free_metrics(monkeypatch):
    import brevitas.provider_reliability as reliability

    correlations = []
    metric_events = []

    class Metrics:
        def record_provider(self, **fields):
            metric_events.append(fields)

    class Runtime:
        metrics = Metrics()

    def correlate(headers):
        correlations.append(dict(headers or {}))
        return {**dict(headers or {}), "X-Brevitas-Request-ID": "a" * 32}

    class Client:
        def __init__(self, **kwargs):
            self.calls = []

        async def post(self, url, **kwargs):
            self.calls.append(kwargs["headers"])
            if len(self.calls) == 1:
                raise httpx.ConnectError("private transport detail")
            return _Response()

        async def aclose(self):
            pass

    monkeypatch.setattr(reliability, "get_runtime", lambda **kwargs: Runtime())
    monkeypatch.setattr(reliability, "provider_correlation_headers", correlate)
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    ticks = iter([0.0, 0.1, 0.2, 0.3, 0.4])
    pool = ProviderHTTPClientPool(
        _config(), sleep=lambda _: asyncio.sleep(0), duration_clock=lambda: next(ticks))
    asyncio.run(pool.request(
        "openai", "chat.completions", "POST", "https://never-record.invalid/private",
        headers={"Authorization": "Bearer real-secret"},
        json={"messages": ["private prompt"]},
    ))

    assert len(correlations) == 2
    assert len(pool._client[0].calls) == 2
    assert all(headers["Authorization"] == "Bearer real-secret"
               for headers in pool._client[0].calls)
    assert all(headers["X-Brevitas-Request-ID"] == "a" * 32
               for headers in pool._client[0].calls)
    assert [(event["operation"], event["outcome"], event["attempt"])
            for event in metric_events] == [
        ("chat", "retry", 1), ("chat", "success", 2),
    ]
    rendered = repr(metric_events)
    assert "real-secret" not in rendered
    assert "private prompt" not in rendered
    assert "never-record" not in rendered


def test_sync_attempt_applies_correlation_and_finite_metric_labels(monkeypatch):
    import brevitas.provider_reliability as reliability

    metric_events = []
    outbound_headers = []

    class Metrics:
        def record_provider(self, **fields):
            metric_events.append(fields)

    class Runtime:
        metrics = Metrics()

    class Client:
        def __init__(self, **kwargs):
            pass

        def request(self, method, url, **kwargs):
            outbound_headers.append(kwargs["headers"])
            return _Response()

        def close(self):
            pass

    monkeypatch.setattr(reliability, "get_runtime", lambda **kwargs: Runtime())
    monkeypatch.setattr(
        reliability, "provider_correlation_headers",
        lambda headers: {**dict(headers or {}), "X-Request-ID": "b" * 32},
    )
    monkeypatch.setattr(httpx, "Client", Client)
    ticks = iter([0.0, 0.1, 0.2])
    pool = ProviderSyncHTTPClientPool(
        _config(), duration_clock=lambda: next(ticks))
    pool.request(
        "unbounded-provider-label", "unbounded-operation-label", "POST",
        "https://private-url.invalid", headers={"Authorization": "Bearer secret"},
        json={"prompt": "private"},
    )
    assert outbound_headers[0]["X-Request-ID"] == "b" * 32
    assert metric_events == [{
        "provider": "other", "operation": "unknown", "outcome": "success",
        "duration_seconds": pytest.approx(0.1), "attempt": 1,
    }]
    assert "secret" not in repr(metric_events)
    assert "private-url" not in repr(metric_events)


def test_stream_metric_waits_for_clean_eof_and_circuit_event_is_finite(monkeypatch):
    import brevitas.provider_reliability as reliability

    metric_events = []
    correlation_calls = []

    class Metrics:
        def record_provider(self, **fields):
            metric_events.append(fields)

    class Runtime:
        metrics = Metrics()

    class StreamingResponse(_Response):
        async def aiter_bytes(self):
            yield b"safe"

    class Client:
        def __init__(self, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, *, stream):
            return StreamingResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(reliability, "get_runtime", lambda **kwargs: Runtime())
    monkeypatch.setattr(
        reliability, "provider_correlation_headers",
        lambda headers: correlation_calls.append(dict(headers or {})) or dict(headers or {}),
    )
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    ticks = iter([0.0, 0.1, 0.5])
    pool = ProviderHTTPClientPool(_config(), duration_clock=lambda: next(ticks))

    async def consume():
        response = await pool.request(
            "anthropic", "messages", "POST", "https://provider.invalid", stream=True)
        assert metric_events == []
        return [chunk async for chunk in pool.iter_bytes("anthropic", response)]

    assert asyncio.run(consume()) == [b"safe"]
    assert len(correlation_calls) == 1
    assert [(event["provider"], event["operation"], event["outcome"])
            for event in metric_events] == [("anthropic", "messages", "success")]

    config = _config(circuit_failure_threshold=1)
    circuit = ProviderCircuitBreaker(config, clock=lambda: 0.0)
    _open_circuit(circuit, "unknown-provider-value")
    rejected = ProviderHTTPClientPool(
        config, circuits=circuit, duration_clock=lambda: 1.0)
    with pytest.raises(ProviderCircuitOpen):
        asyncio.run(rejected.request(
            "unknown-provider-value", "dynamic-operation", "POST",
            "https://provider.invalid"))
    assert metric_events[-1]["outcome"] == "circuit_open"
    assert metric_events[-1]["provider"] == "other"
    assert metric_events[-1]["operation"] == "unknown"
    assert set(metric_events[-1]) == {
        "provider", "operation", "outcome", "duration_seconds", "attempt",
    }


def test_proxy_rejects_declared_and_streamed_body_overflow_before_materialization(
        monkeypatch):
    import brevitas.proxy as proxy

    bounds = ResourceBounds(request_max_bytes=1024)
    monkeypatch.setattr(proxy, "_RESOURCE_BOUNDS", bounds)
    receive_calls = 0

    async def should_not_read():
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    declared = Request({
        "type": "http", "method": "POST", "scheme": "https", "path": "/v1/messages",
        "query_string": b"", "server": ("test", 443), "client": ("test", 1),
        "headers": [(b"content-length", b"2048")],
    }, receive=should_not_read)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(proxy._json_object(declared))
    assert caught.value.status_code == 413
    assert receive_calls == 0

    chunks = [
        {"type": "http.request", "body": b"a" * 700, "more_body": True},
        {"type": "http.request", "body": b"b" * 700, "more_body": False},
    ]

    async def receive_chunk():
        return chunks.pop(0)

    streamed = Request({
        "type": "http", "method": "POST", "scheme": "https", "path": "/v1/messages",
        "query_string": b"", "server": ("test", 443), "client": ("test", 1),
        "headers": [],
    }, receive=receive_chunk)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(proxy._json_object(streamed))
    assert caught.value.status_code == 413


def test_proxy_rejects_oversized_nested_arrays(monkeypatch):
    import brevitas.proxy as proxy

    bounds = ResourceBounds(request_max_items=2)
    monkeypatch.setattr(proxy, "_RESOURCE_BOUNDS", bounds)
    raw = b'{"messages":[{"content":[1,2,3]}]}'
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "scheme": "https", "path": "/v1/messages",
        "query_string": b"", "server": ("test", 443), "client": ("test", 1),
        "headers": [(b"content-length", str(len(raw)).encode())],
    }, receive=receive)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(proxy._json_object(request))
    assert caught.value.status_code == 413


def test_proxy_router_and_session_registries_are_owned_bounded_and_concurrent(monkeypatch):
    import brevitas.proxy as proxy

    now = [0.0]
    bounds = ResourceBounds(
        registry_ttl_s=1, registry_max_entries=2, registry_max_value_bytes=4096,
        session_content_ttl_s=1, session_max_items=8,
        session_max_bytes=2048, session_max_item_bytes=1024,
    )
    monkeypatch.setattr(proxy, "_RESOURCE_BOUNDS", bounds)
    monkeypatch.setattr(proxy, "_SESSION_CONTENT_BUDGET", 2048)
    monkeypatch.setattr(proxy, "_routers", proxy._make_router_registry(
        bounds, clock=lambda: now[0]))
    monkeypatch.setattr(proxy, "_sessions", proxy._make_session_registry(
        bounds, clock=lambda: now[0]))

    handle = proxy._router_for("router-a", "openai")
    leaked = proxy._routers.get("router-a")
    leaked.model = "external-alias"
    assert proxy._routers.get("router-a").model == ""

    def mutate_router(_):
        proxy._mutate_router(
            handle,
            lambda router: setattr(
                router, "retrieve_keep_frac", router.retrieve_keep_frac + 1),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(mutate_router, range(24)))
    assert proxy._routers.get("router-a").retrieve_keep_frac == pytest.approx(24.6)

    session = proxy._session_for("session-a")
    session.record_response("bounded private response")
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: session.advance(), range(40)))
    snapshot = proxy._sessions.get("session-a")
    assert snapshot.hop_count == 40
    assert snapshot.prior_context() == ["bounded private response"]
    snapshot.reset()
    assert proxy._sessions.get("session-a").prior_context() == ["bounded private response"]
    assert 0 < proxy._sessions.total_bytes <= proxy._sessions.max_total_bytes

    proxy._router_for("router-b", "openai")
    proxy._router_for("router-c", "openai")
    assert len(proxy._routers) == 2
    assert proxy._routers.get("router-a") is None
    now[0] = 2.0
    assert len(proxy._routers) == 0
    assert len(proxy._sessions) == 0


def test_proxy_registries_close_owned_pools_on_replacement_ttl_discard_and_shutdown(
        monkeypatch):
    import brevitas.proxy as proxy
    from brevitas.session import BrevitasSession
    from token_efficiency_model.lossless.router import BrevitasRouter

    class Pool:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    class ProviderPool:
        def __init__(self):
            self.close_calls = 0

        async def aclose(self):
            self.close_calls += 1

    now = [0.0]
    bounds = ResourceBounds(
        registry_ttl_s=1, registry_max_entries=4, registry_max_value_bytes=4096,
        session_content_ttl_s=1, session_max_items=8,
        session_max_bytes=2048, session_max_item_bytes=1024,
    )
    routers = proxy._make_router_registry(bounds, clock=lambda: now[0])
    sessions = proxy._make_session_registry(bounds, clock=lambda: now[0])
    assert routers._resource_key is proxy._registry_resource
    assert routers._on_remove is proxy._close_registry_value
    assert sessions._resource_key is proxy._registry_resource
    assert sessions._on_remove is proxy._close_registry_value

    replaced_pool, ttl_pool = Pool(), Pool()
    original = BrevitasRouter(provider="openai")
    original.client = replaced_pool
    routers.put("owned", original)
    assert routers.get("owned").client is replaced_pool
    routers.mutate("owned", lambda value: setattr(value, "model", "gpt-test"))
    assert routers.get("owned").client is replaced_pool
    assert replaced_pool.close_calls == 0

    replacement = BrevitasRouter(provider="openai")
    replacement.client = ttl_pool
    routers.put("owned", replacement)
    assert replaced_pool.close_calls == 1
    now[0] = 2.0
    assert routers.cleanup() == 1
    assert ttl_pool.close_calls == 1

    discarded_pool = Pool()
    session = BrevitasSession(session_id="discarded")
    session.client = discarded_pool
    sessions.put("discarded", session)
    sessions.mutate("discarded", lambda value: value.advance())
    assert sessions.get("discarded").client is discarded_pool
    assert discarded_pool.close_calls == 0
    assert sessions.discard("discarded") is True
    assert discarded_pool.close_calls == 1

    shutdown_router_pool, shutdown_session_pool = Pool(), Pool()
    shutdown_router = BrevitasRouter(provider="anthropic")
    shutdown_router.client = shutdown_router_pool
    shutdown_session = BrevitasSession(session_id="shutdown")
    shutdown_session.client = shutdown_session_pool
    routers.put("shutdown-router", shutdown_router)
    sessions.put("shutdown-session", shutdown_session)
    provider_pool = ProviderPool()
    monkeypatch.setattr(proxy, "_STATE_FILE", "")
    monkeypatch.setattr(proxy, "_routers", routers)
    monkeypatch.setattr(proxy, "_sessions", sessions)
    monkeypatch.setattr(proxy, "provider_http", provider_pool)

    asyncio.run(proxy.close_provider_clients())
    assert provider_pool.close_calls == 1
    assert shutdown_router_pool.close_calls == 1
    assert shutdown_session_pool.close_calls == 1
    assert len(routers) == 0
    assert len(sessions) == 0

    class Pool:
        closed = False

        async def aclose(self):
            self.closed = True

    pool = Pool()
    monkeypatch.setattr(proxy, "provider_http", pool)
    proxy._router_for("shutdown-router", "openai")
    proxy._session_for("shutdown-session")
    asyncio.run(proxy.close_provider_clients())
    assert pool.closed is True
    assert len(proxy._routers) == 0
    assert len(proxy._sessions) == 0


@pytest.mark.parametrize("wrapper_module,class_name,resource_name", [
    ("brevitas.wrappers.openai", "BrevitasOpenAIClient", "chat"),
    ("brevitas.wrappers.anthropic", "BrevitasAnthropicClient", "messages"),
])
def test_sdk_wrappers_close_underlying_pool_in_context_manager(
        wrapper_module, class_name, resource_name):
    module = __import__(wrapper_module, fromlist=[class_name])
    wrapper_class = getattr(module, class_name)

    class Resource:
        def create(self, **kwargs):
            return None

    class SDK:
        def __init__(self):
            self.closed = 0
            if resource_name == "chat":
                chat = type("Chat", (), {})()
                chat.completions = Resource()
                self.chat = chat
            else:
                self.messages = Resource()

        def close(self):
            self.closed += 1

    sdk = SDK()
    with wrapper_class(sdk) as wrapped:
        assert wrapped is not sdk
    assert sdk.closed == 1
