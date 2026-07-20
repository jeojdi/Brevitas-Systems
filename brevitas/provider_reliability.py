"""Bounded reliability controls for outbound model-provider HTTP calls.

This module deliberately logs nothing and never puts URLs, headers, bodies, credentials,
or raw transport messages in its own public exceptions. Callers must apply the same rule
when translating the returned HTTP response or a transport exception.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import random
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Mapping

import httpx

from .observability import get_runtime, provider_correlation_headers


ProviderOperation = tuple[str, str]

# A caller-provided Idempotency-Key is not proof that a provider deduplicates model work.
# No current chat/messages/responses endpoint in the supported provider set has a verified
# deduplication contract, so ambiguous POST failures are not retried by default. A future
# capability may be added here only after its provider+operation guarantee is documented.
KNOWN_IDEMPOTENT_OPERATIONS: frozenset[ProviderOperation] = frozenset()
_METRIC_PROVIDERS = frozenset({
    "anthropic", "azure_openai", "bedrock", "cohere", "deepseek", "fireworks",
    "google_gemini", "groq", "mistral", "ollama", "openai", "openrouter",
    "perplexity", "together", "xai",
})
_METRIC_OPERATIONS = frozenset({
    "chat", "embeddings", "generate", "messages", "responses", "unknown",
})


def _metric_provider(provider: object) -> str:
    value = str(provider or "other").lower()
    return value if value in _METRIC_PROVIDERS else "other"


def _metric_operation(operation: object) -> str:
    value = str(operation or "unknown").lower()
    if value in {"chat.completions", "completions"}:
        return "chat"
    return value if value in _METRIC_OPERATIONS else "unknown"


def _response_outcome(status_code: int) -> str:
    if status_code >= 500:
        return "server_error"
    if status_code >= 400:
        return "client_error"
    return "success"


def _transport_outcome(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    return "error"


def _record_provider_attempt(
    provider: object,
    operation: object,
    outcome: str,
    duration_seconds: float,
    attempt: int,
) -> None:
    """Best-effort, finite-label telemetry with no request material."""
    try:
        get_runtime(default_service="api").metrics.record_provider(
            provider=_metric_provider(provider),
            operation=_metric_operation(operation),
            outcome=outcome,
            duration_seconds=max(0.0, float(duration_seconds)),
            attempt=max(1, min(int(attempt), 6)),
        )
    except Exception:
        pass


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class ProviderReliabilityConfig:
    """Safe, bounded defaults for provider requests and connection pools."""

    connect_timeout_s: float = 5.0
    read_timeout_s: float = 120.0
    write_timeout_s: float = 30.0
    pool_timeout_s: float = 5.0
    max_connections: int = 100
    max_keepalive_connections: int = 20
    keepalive_expiry_s: float = 30.0
    max_retries: int = 2
    retry_base_s: float = 0.25
    retry_max_s: float = 8.0
    circuit_failure_threshold: int = 5
    circuit_open_s: float = 30.0
    circuit_state_ttl_s: float = 900.0
    max_provider_states: int = 32

    @classmethod
    def from_env(cls) -> "ProviderReliabilityConfig":
        return cls(
            connect_timeout_s=_env_float(
                "BREVITAS_PROVIDER_CONNECT_TIMEOUT_S", 5.0, 0.1, 60.0),
            read_timeout_s=_env_float(
                "BREVITAS_PROVIDER_READ_TIMEOUT_S", 120.0, 1.0, 900.0),
            write_timeout_s=_env_float(
                "BREVITAS_PROVIDER_WRITE_TIMEOUT_S", 30.0, 1.0, 300.0),
            pool_timeout_s=_env_float(
                "BREVITAS_PROVIDER_POOL_TIMEOUT_S", 5.0, 0.1, 60.0),
            max_connections=_env_int(
                "BREVITAS_PROVIDER_MAX_CONNECTIONS", 100, 1, 1000),
            max_keepalive_connections=_env_int(
                "BREVITAS_PROVIDER_MAX_KEEPALIVE", 20, 0, 1000),
            keepalive_expiry_s=_env_float(
                "BREVITAS_PROVIDER_KEEPALIVE_EXPIRY_S", 30.0, 1.0, 300.0),
            max_retries=_env_int("BREVITAS_PROVIDER_MAX_RETRIES", 2, 0, 5),
            retry_base_s=_env_float(
                "BREVITAS_PROVIDER_RETRY_BASE_S", 0.25, 0.01, 10.0),
            retry_max_s=_env_float(
                "BREVITAS_PROVIDER_RETRY_MAX_S", 8.0, 0.05, 60.0),
            circuit_failure_threshold=_env_int(
                "BREVITAS_PROVIDER_CIRCUIT_FAILURES", 5, 1, 100),
            circuit_open_s=_env_float(
                "BREVITAS_PROVIDER_CIRCUIT_OPEN_S", 30.0, 1.0, 600.0),
            circuit_state_ttl_s=_env_float(
                "BREVITAS_PROVIDER_CIRCUIT_TTL_S", 900.0, 30.0, 86400.0),
            max_provider_states=_env_int(
                "BREVITAS_PROVIDER_MAX_STATES", 32, 1, 128),
        )

    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout_s,
            read=self.read_timeout_s,
            write=self.write_timeout_s,
            pool=self.pool_timeout_s,
        )

    def limits(self) -> httpx.Limits:
        return httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=min(
                self.max_connections, self.max_keepalive_connections),
            keepalive_expiry=self.keepalive_expiry_s,
        )


class ProviderCircuitOpen(RuntimeError):
    """Raised without sensitive context when a provider circuit rejects a call."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__("provider temporarily unavailable")
        self.retry_after_s = max(0.0, retry_after_s)


@dataclass
class _CircuitState:
    failures: int = 0
    opened_until: float = 0.0
    generation: int = 0
    half_open_token: int | None = None
    in_flight: int = 0
    last_seen: float = 0.0


@dataclass
class ProviderCircuitPermit:
    """Opaque admission token resolved exactly once at a logical terminal outcome."""

    provider: str
    token: int
    generation: int
    half_open: bool
    _resolved: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def claim(self) -> bool:
        with self._lock:
            if self._resolved:
                return False
            self._resolved = True
            return True


class ProviderCircuitBreaker:
    """Thread-safe, TTL/LRU-bounded circuit state keyed only by provider name.

    ``before_request`` acquires one logical permit. Exactly one of ``record_success``,
    ``record_failure``, or ``abandon`` must resolve it. Active permits are never evicted.
    """

    def __init__(self, config: ProviderReliabilityConfig,
                 *, clock: Callable[[], float] = time.monotonic) -> None:
        self._config = config
        self._clock = clock
        self._states: OrderedDict[str, _CircuitState] = OrderedDict()
        self._lock = threading.Lock()
        self._next_token = 0

    @staticmethod
    def _protected(state: _CircuitState, now: float) -> bool:
        return state.in_flight > 0 or state.half_open_token is not None \
            or state.opened_until > now

    def _cleanup_locked(self, now: float) -> None:
        stale = [
            provider for provider, state in self._states.items()
            if not self._protected(state, now)
            and now - state.last_seen >= self._config.circuit_state_ttl_s
        ]
        for provider in stale:
            self._states.pop(provider, None)

    def _state_locked(self, provider: str, now: float) -> _CircuitState:
        self._cleanup_locked(now)
        state = self._states.get(provider)
        if state is not None:
            state.last_seen = now
            self._states.move_to_end(provider)
            return state

        if len(self._states) >= self._config.max_provider_states:
            evictable = next((
                name for name, candidate in self._states.items()
                if not self._protected(candidate, now)
            ), None)
            if evictable is None:
                # Bounded memory wins over silently dropping an active half-open probe.
                raise ProviderCircuitOpen(self._config.circuit_open_s)
            self._states.pop(evictable, None)

        state = _CircuitState(last_seen=now)
        self._states[provider] = state
        return state

    def before_request(self, provider: str) -> ProviderCircuitPermit:
        """Acquire a logical request permit, including its circuit generation."""
        now = self._clock()
        with self._lock:
            state = self._state_locked(provider, now)
            if state.opened_until > now:
                raise ProviderCircuitOpen(state.opened_until - now)
            half_open = False
            if state.opened_until:
                if state.half_open_token is not None:
                    raise ProviderCircuitOpen(self._config.circuit_open_s)
                half_open = True
            self._next_token += 1
            permit = ProviderCircuitPermit(
                provider=provider, token=self._next_token,
                generation=state.generation, half_open=half_open,
            )
            state.in_flight += 1
            if half_open:
                state.half_open_token = permit.token
            return permit

    def _take_permit_locked(
        self, permit: ProviderCircuitPermit,
    ) -> _CircuitState | None:
        if not permit.claim():
            return None
        state = self._states.get(permit.provider)
        if state is None:
            return None
        state.in_flight = max(0, state.in_flight - 1)
        return state

    def record_success(self, permit: ProviderCircuitPermit) -> None:
        now = self._clock()
        with self._lock:
            state = self._take_permit_locked(permit)
            if state is None:
                return
            if permit.half_open and state.half_open_token == permit.token:
                state.half_open_token = None
                state.failures = 0
                state.opened_until = 0.0
            elif permit.generation == state.generation and not state.opened_until:
                state.failures = 0
            state.last_seen = now
            self._states.move_to_end(permit.provider)

    def record_failure(self, permit: ProviderCircuitPermit) -> None:
        now = self._clock()
        with self._lock:
            state = self._take_permit_locked(permit)
            if state is None:
                return
            state.last_seen = now
            if permit.half_open and state.half_open_token == permit.token:
                state.half_open_token = None
                state.failures += 1
                state.generation += 1
                state.opened_until = now + self._config.circuit_open_s
            elif permit.generation == state.generation and not state.opened_until:
                state.failures += 1
                if state.failures >= self._config.circuit_failure_threshold:
                    state.generation += 1
                    state.opened_until = now + self._config.circuit_open_s
            self._states.move_to_end(permit.provider)

    def abandon(self, permit: ProviderCircuitPermit) -> None:
        """Release a cancelled/invalid permit without treating it as provider failure."""
        now = self._clock()
        with self._lock:
            state = self._take_permit_locked(permit)
            if state is None:
                return
            if state.half_open_token == permit.token:
                state.half_open_token = None
            state.last_seen = now
            self._states.move_to_end(permit.provider)

    def state_count(self) -> int:
        with self._lock:
            self._cleanup_locked(self._clock())
            return len(self._states)


def _header(headers: Mapping[str, str] | None, name: str) -> str:
    target = name.lower()
    return next((str(value) for key, value in (headers or {}).items()
                 if str(key).lower() == target), "")


def _deduplicated_request(
    provider: str,
    operation: str,
    method: str,
    headers: Mapping[str, str] | None,
    capabilities: frozenset[ProviderOperation],
) -> bool:
    if method.upper() in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}:
        return True
    return (
        (provider.lower(), operation.lower()) in capabilities
        and bool(_header(headers, "idempotency-key"))
    )


def _retry_after_seconds(value: str, now_wall: float) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, parsed.timestamp() - now_wall)
        except (TypeError, ValueError, OverflowError):
            return None


class _RetryPolicy:
    _TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        config: ProviderReliabilityConfig,
        capabilities: frozenset[ProviderOperation],
        random_value: Callable[[], float],
        wall_clock: Callable[[], float],
    ) -> None:
        self.config = config
        self.capabilities = capabilities
        self._random = random_value
        self._wall_clock = wall_clock

    def deduplicated(self, provider: str, operation: str, method: str,
                     headers: Mapping[str, str] | None) -> bool:
        return _deduplicated_request(
            provider, operation, method, headers, self.capabilities)

    def delay(self, attempt: int, response: Any | None = None) -> float | None:
        retry_after = _retry_after_seconds(
            _header(getattr(response, "headers", None), "retry-after"), self._wall_clock()
        ) if response is not None else None
        if retry_after is not None:
            return retry_after if retry_after <= self.config.retry_max_s else None
        ceiling = min(self.config.retry_max_s, self.config.retry_base_s * (2 ** attempt))
        return max(0.0, min(1.0, self._random())) * ceiling

    @staticmethod
    def retryable_exception(exc: httpx.TransportError, *, deduplicated: bool) -> bool:
        # These failures occur before request bytes can be accepted by the provider.
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return True
        # Read/write/protocol failures are ambiguous after a POST.
        return deduplicated

    def retryable_response(self, status: int, *, deduplicated: bool) -> bool:
        if status not in self._TRANSIENT_STATUSES:
            return False
        # 429 is a definite rejection. All other transient statuses may be ambiguous.
        return status == 429 or deduplicated

    @staticmethod
    def circuit_failure(status: int) -> bool:
        return status >= 500


class _ManagedAsyncStreamResponse:
    """Own a circuit permit until a 2xx stream reaches a terminal outcome."""

    def __init__(self, response: Any, permit: ProviderCircuitPermit,
                 circuits: ProviderCircuitBreaker, operation: str, attempt: int,
                 started: float, duration_clock: Callable[[], float],
                 on_finish: Callable[["_ManagedAsyncStreamResponse"], None]) -> None:
        self._response = response
        self._permit = permit
        self._circuits = circuits
        self._operation = operation
        self._attempt = attempt
        self._started = started
        self._duration_clock = duration_clock
        self._on_finish = on_finish
        self._done = False
        self._done_lock = threading.Lock()

    def _finish(self, outcome: str, metric_outcome: str) -> None:
        with self._done_lock:
            if self._done:
                return
            self._done = True
        try:
            _record_provider_attempt(
                self._permit.provider, self._operation, metric_outcome,
                self._duration_clock() - self._started, self._attempt,
            )
            if outcome == "success":
                self._circuits.record_success(self._permit)
            elif outcome == "failure":
                self._circuits.record_failure(self._permit)
            else:
                self._circuits.abandon(self._permit)
        finally:
            self._on_finish(self)

    async def aread(self) -> bytes:
        try:
            content = await self._response.aread()
        except httpx.TransportError as exc:
            self._finish("failure", _transport_outcome(exc))
            raise
        except BaseException as exc:
            self._finish("abandon", _transport_outcome(exc))
            raise
        self._finish("success", "success")
        return content

    async def aiter_bytes(self):
        try:
            async for chunk in self._response.aiter_bytes():
                yield chunk
        except httpx.TransportError as exc:
            self._finish("failure", _transport_outcome(exc))
            raise
        except BaseException as exc:
            self._finish("abandon", _transport_outcome(exc))
            raise
        self._finish("success", "success")

    async def aclose(self) -> None:
        try:
            await self._response.aclose()
        except httpx.TransportError as exc:
            self._finish("failure", _transport_outcome(exc))
            raise
        except BaseException as exc:
            self._finish("abandon", _transport_outcome(exc))
            raise
        self._finish("abandon", "cancelled")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


class ProviderHTTPClientPool:
    """One process-level async client with retry and circuit policies."""

    def __init__(
        self,
        config: ProviderReliabilityConfig | None = None,
        *,
        circuits: ProviderCircuitBreaker | None = None,
        idempotency_capabilities: frozenset[ProviderOperation] = KNOWN_IDEMPOTENT_OPERATIONS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        duration_clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.config = config or ProviderReliabilityConfig.from_env()
        self.circuits = circuits or ProviderCircuitBreaker(self.config, clock=monotonic)
        self._policy = _RetryPolicy(
            self.config, idempotency_capabilities, random_value, wall_clock)
        self._sleep = sleep
        self._duration_clock = duration_clock
        self._client: tuple[Any, Any] | None = None
        self._client_lock = threading.Lock()
        self._active_streams: set[_ManagedAsyncStreamResponse] = set()
        self._active_streams_lock = threading.Lock()

    async def _close_one(self, client: Any) -> None:
        close = getattr(client, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def _client_for(self) -> Any:
        factory = httpx.AsyncClient
        stale: Any | None = None
        with self._client_lock:
            entry = self._client
            if entry is not None and entry[1] is factory:
                return entry[0]
            if entry is not None:
                stale = entry[0]
            client = factory(
                timeout=self.config.timeout(),
                limits=self.config.limits(),
                follow_redirects=False,
                trust_env=False,
            )
            self._client = (client, factory)
        if stale is not None:
            await self._close_one(stale)
        return client

    def _stream_finished(self, response: _ManagedAsyncStreamResponse) -> None:
        with self._active_streams_lock:
            self._active_streams.discard(response)

    def _manage_stream(
        self, permit: ProviderCircuitPermit, response: Any, operation: str,
        attempt: int, started: float,
    ) -> _ManagedAsyncStreamResponse:
        managed = _ManagedAsyncStreamResponse(
            response, permit, self.circuits, operation, attempt, started,
            self._duration_clock, self._stream_finished)
        with self._active_streams_lock:
            self._active_streams.add(managed)
        return managed

    async def request(
        self,
        provider: str,
        operation: str,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        stream: bool = False,
        json: Any = None,
        content: bytes | None = None,
    ) -> Any:
        """Send one logical request, returning an open managed response for 2xx streams."""
        circuit_started = self._duration_clock()
        try:
            permit = self.circuits.before_request(provider)
        except ProviderCircuitOpen:
            _record_provider_attempt(
                provider, operation, "circuit_open",
                self._duration_clock() - circuit_started, 1,
            )
            raise
        finalized = False
        attempt_number = 1
        attempt_started: float | None = None
        attempt_recorded = True

        try:
            deduplicated = self._policy.deduplicated(
                provider, operation, method, headers)
            request_body = {"json": json} if json is not None else {}
            if content is not None:
                request_body = {"content": content}
            for attempt in range(self.config.max_retries + 1):
                attempt_number = attempt + 1
                attempt_started = self._duration_clock()
                attempt_recorded = False
                attempt_headers = provider_correlation_headers(headers)
                client = await self._client_for()
                try:
                    if stream:
                        outbound = client.build_request(
                            method, url, headers=attempt_headers, **request_body)
                        response = await client.send(outbound, stream=True)
                    elif method.upper() == "POST":
                        response = await client.post(
                            url, headers=attempt_headers, **request_body)
                    else:
                        response = await client.request(
                            method, url, headers=attempt_headers, **request_body)
                except httpx.TransportError as exc:
                    retryable = (
                        attempt < self.config.max_retries
                        and self._policy.retryable_exception(
                            exc, deduplicated=deduplicated)
                    )
                    _record_provider_attempt(
                        provider, operation,
                        "retry" if retryable else _transport_outcome(exc),
                        self._duration_clock() - attempt_started, attempt_number,
                    )
                    attempt_recorded = True
                    if retryable:
                        await self._sleep(self._policy.delay(attempt) or 0.0)
                        continue
                    raise

                status = int(response.status_code)
                if (
                    attempt < self.config.max_retries
                    and self._policy.retryable_response(
                        status, deduplicated=deduplicated)
                ):
                    delay = self._policy.delay(attempt, response)
                    if delay is not None:
                        _record_provider_attempt(
                            provider, operation, "retry",
                            self._duration_clock() - attempt_started, attempt_number,
                        )
                        attempt_recorded = True
                        await self._close_one(response)
                        await self._sleep(delay)
                        continue

                if stream and 200 <= status < 300:
                    managed = self._manage_stream(
                        permit, response, operation, attempt_number, attempt_started)
                    finalized = True  # the managed response now owns the permit
                    return managed

                _record_provider_attempt(
                    provider, operation, _response_outcome(status),
                    self._duration_clock() - attempt_started, attempt_number,
                )
                attempt_recorded = True

                if self._policy.circuit_failure(status):
                    self.circuits.record_failure(permit)
                else:
                    self.circuits.record_success(permit)
                finalized = True
                return response
        except httpx.TransportError as exc:
            if not attempt_recorded and attempt_started is not None:
                _record_provider_attempt(
                    provider, operation, _transport_outcome(exc),
                    self._duration_clock() - attempt_started, attempt_number,
                )
                attempt_recorded = True
            if not finalized:
                self.circuits.record_failure(permit)
                finalized = True
            raise
        except BaseException as exc:
            if not attempt_recorded and attempt_started is not None:
                _record_provider_attempt(
                    provider, operation, _transport_outcome(exc),
                    self._duration_clock() - attempt_started, attempt_number,
                )
                attempt_recorded = True
            if not finalized:
                self.circuits.abandon(permit)
                finalized = True
            raise

        if not finalized:
            self.circuits.abandon(permit)
        raise RuntimeError("provider request exhausted without a response")

    async def iter_bytes(self, provider: str, response: Any):
        """Yield a response exactly once; managed streams resolve their permit at EOF."""
        async for chunk in response.aiter_bytes():
            yield chunk

    async def aclose(self) -> None:
        """Abandon active streams and deterministically close the shared client."""
        with self._active_streams_lock:
            active = list(self._active_streams)
        for response in active:
            try:
                await response.aclose()
            except Exception:
                pass
        with self._client_lock:
            entry = self._client
            self._client = None
        if entry is not None:
            await self._close_one(entry[0])

    def client_count(self) -> int:
        with self._client_lock:
            return int(self._client is not None)


class ProviderSyncHTTPClientPool:
    """Thread-safe sync contract for playground and durable-worker one-off calls.

    Use ``request(provider, operation, method, url, ...)`` from worker threads and call
    ``close()`` once, after request threads drain, during process shutdown.
    """

    def __init__(
        self,
        config: ProviderReliabilityConfig | None = None,
        *,
        circuits: ProviderCircuitBreaker | None = None,
        idempotency_capabilities: frozenset[ProviderOperation] = KNOWN_IDEMPOTENT_OPERATIONS,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        duration_clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.config = config or ProviderReliabilityConfig.from_env()
        self.circuits = circuits or ProviderCircuitBreaker(self.config, clock=monotonic)
        self._policy = _RetryPolicy(
            self.config, idempotency_capabilities, random_value, wall_clock)
        self._sleep = sleep
        self._duration_clock = duration_clock
        self._client: tuple[Any, Any] | None = None
        self._client_lock = threading.Lock()

    def _client_for(self) -> Any:
        factory = httpx.Client
        stale: Any | None = None
        with self._client_lock:
            entry = self._client
            if entry is not None and entry[1] is factory:
                return entry[0]
            if entry is not None:
                stale = entry[0]
            client = factory(
                timeout=self.config.timeout(),
                limits=self.config.limits(),
                follow_redirects=False,
                trust_env=False,
            )
            self._client = (client, factory)
        if stale is not None:
            stale.close()
        return client

    def request(
        self,
        provider: str,
        operation: str,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
    ) -> Any:
        circuit_started = self._duration_clock()
        try:
            permit = self.circuits.before_request(provider)
        except ProviderCircuitOpen:
            _record_provider_attempt(
                provider, operation, "circuit_open",
                self._duration_clock() - circuit_started, 1,
            )
            raise
        finalized = False
        attempt_number = 1
        attempt_started: float | None = None
        attempt_recorded = True

        try:
            deduplicated = self._policy.deduplicated(
                provider, operation, method, headers)
            request_body = {"json": json} if json is not None else {}
            if content is not None:
                request_body = {"content": content}
            for attempt in range(self.config.max_retries + 1):
                attempt_number = attempt + 1
                attempt_started = self._duration_clock()
                attempt_recorded = False
                attempt_headers = provider_correlation_headers(headers)
                client = self._client_for()
                try:
                    response = client.request(
                        method, url, headers=attempt_headers, **request_body)
                except httpx.TransportError as exc:
                    retryable = (
                        attempt < self.config.max_retries
                        and self._policy.retryable_exception(
                            exc, deduplicated=deduplicated)
                    )
                    _record_provider_attempt(
                        provider, operation,
                        "retry" if retryable else _transport_outcome(exc),
                        self._duration_clock() - attempt_started, attempt_number,
                    )
                    attempt_recorded = True
                    if retryable:
                        self._sleep(self._policy.delay(attempt) or 0.0)
                        continue
                    raise

                status = int(response.status_code)
                if (
                    attempt < self.config.max_retries
                    and self._policy.retryable_response(
                        status, deduplicated=deduplicated)
                ):
                    delay = self._policy.delay(attempt, response)
                    if delay is not None:
                        _record_provider_attempt(
                            provider, operation, "retry",
                            self._duration_clock() - attempt_started, attempt_number,
                        )
                        attempt_recorded = True
                        response.close()
                        self._sleep(delay)
                        continue

                _record_provider_attempt(
                    provider, operation, _response_outcome(status),
                    self._duration_clock() - attempt_started, attempt_number,
                )
                attempt_recorded = True

                if self._policy.circuit_failure(status):
                    self.circuits.record_failure(permit)
                else:
                    self.circuits.record_success(permit)
                finalized = True
                return response
        except httpx.TransportError as exc:
            if not attempt_recorded and attempt_started is not None:
                _record_provider_attempt(
                    provider, operation, _transport_outcome(exc),
                    self._duration_clock() - attempt_started, attempt_number,
                )
                attempt_recorded = True
            if not finalized:
                self.circuits.record_failure(permit)
                finalized = True
            raise
        except BaseException as exc:
            if not attempt_recorded and attempt_started is not None:
                _record_provider_attempt(
                    provider, operation, _transport_outcome(exc),
                    self._duration_clock() - attempt_started, attempt_number,
                )
                attempt_recorded = True
            if not finalized:
                self.circuits.abandon(permit)
                finalized = True
            raise

        if not finalized:
            self.circuits.abandon(permit)
        raise RuntimeError("provider request exhausted without a response")

    def close(self) -> None:
        """Close the shared sync client after all request threads have drained."""
        with self._client_lock:
            entry = self._client
            self._client = None
        if entry is not None:
            entry[0].close()

    def client_count(self) -> int:
        with self._client_lock:
            return int(self._client is not None)


_provider_config = ProviderReliabilityConfig.from_env()
_provider_circuits = ProviderCircuitBreaker(_provider_config)
provider_http = ProviderHTTPClientPool(_provider_config, circuits=_provider_circuits)
provider_sync_http = ProviderSyncHTTPClientPool(
    _provider_config, circuits=_provider_circuits)


def close_provider_sync_clients() -> None:
    """Callable shutdown hook for sync API/worker processes."""
    provider_sync_http.close()
