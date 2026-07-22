"""Atomic hierarchical admission control shared by every API replica.

Production deliberately fails closed when Redis is unavailable. Development can
run without Redis so unit tests and the local SQLite server remain usable.
Credentials are never used in Redis keys; callers provide database identifiers.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from brevitas.resource_bounds import clamp_int
from .runtime import hosted_runtime


def _redis_failure_outcome(exc: BaseException) -> str:
    """Classify without importing an optional Redis package or exposing exception text."""
    if isinstance(exc, TimeoutError) or any(
        "timeout" in cls.__name__.lower() for cls in type(exc).__mro__
    ):
        return "timeout"
    return "error"


def _record_redis_dependency(outcome: str, started: float) -> None:
    """Emit only fixed Redis dependency labels; telemetry must always fail open."""
    try:
        from brevitas.observability import get_runtime

        get_runtime(default_service="api").metrics.record_dependency(
            dependency="redis",
            outcome=outcome,
            duration_seconds=max(0.0, time.perf_counter() - started),
        )
    except Exception:
        pass


class LimiterUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class LimitIdentity:
    organization_id: str
    customer_id: str
    key_id: str
    provider: str = "all"


@dataclass(frozen=True)
class LimitPolicy:
    organization_rpm: int = 3000
    customer_rpm: int = 300
    key_rpm: int = 300
    provider_rpm: int = 10000
    organization_tpm: int = 2_000_000
    customer_tpm: int = 200_000
    key_tpm: int = 200_000
    organization_concurrency: int = 200
    customer_concurrency: int = 20
    key_concurrency: int = 20
    provider_concurrency: int = 500
    lease_seconds: int = 900

    def __post_init__(self) -> None:
        ceilings = {
            "organization_rpm": 10_000_000,
            "customer_rpm": 10_000_000,
            "key_rpm": 10_000_000,
            "provider_rpm": 10_000_000,
            "organization_tpm": 2_000_000_000,
            "customer_tpm": 2_000_000_000,
            "key_tpm": 2_000_000_000,
            "organization_concurrency": 100_000,
            "customer_concurrency": 100_000,
            "key_concurrency": 100_000,
            "provider_concurrency": 100_000,
            "lease_seconds": 3_600,
        }
        for name, ceiling in ceilings.items():
            object.__setattr__(
                self, name,
                clamp_int(getattr(self, name), minimum=1, maximum=ceiling,
                          name=f"limit policy {name}"),
            )

    @classmethod
    def from_env(cls) -> "LimitPolicy":
        def value(name: str, default: int) -> int:
            return int(os.getenv(name, str(default)))

        return cls(
            organization_rpm=value("BREVITAS_ORG_RPM", 3000),
            customer_rpm=value("BREVITAS_CUSTOMER_RPM", 300),
            key_rpm=value("BREVITAS_KEY_RPM", 300),
            provider_rpm=value("BREVITAS_PROVIDER_RPM", 10000),
            organization_tpm=value("BREVITAS_ORG_TPM", 2_000_000),
            customer_tpm=value("BREVITAS_CUSTOMER_TPM", 200_000),
            key_tpm=value("BREVITAS_KEY_TPM", 200_000),
            organization_concurrency=value("BREVITAS_ORG_CONCURRENCY", 200),
            customer_concurrency=value("BREVITAS_CUSTOMER_CONCURRENCY", 20),
            key_concurrency=value("BREVITAS_KEY_CONCURRENCY", 20),
            provider_concurrency=value("BREVITAS_PROVIDER_CONCURRENCY", 500),
            lease_seconds=value("BREVITAS_LIMIT_LEASE_SECONDS", 900),
        )


@dataclass
class LimitLease:
    allowed: bool
    request_id: str
    retry_after: int = 0
    reason: str = ""
    remaining_requests: int = 0
    reset_seconds: int = 0
    _limiter: "DistributedLimiter | None" = None
    _concurrency_keys: tuple[str, ...] = ()

    async def release(self) -> None:
        if self.allowed and self._limiter and self._concurrency_keys:
            await self._limiter.release(self)
            self._concurrency_keys = ()

    async def renew(self) -> bool:
        if self.allowed and self._limiter and self._concurrency_keys:
            return await self._limiter.renew(self)
        return False


_ACQUIRE = r"""
local now = tonumber(ARGV[1])
local expires = tonumber(ARGV[2])
local rate_reset = tonumber(ARGV[3])
local request_id = ARGV[4]
local rate_count = tonumber(ARGV[5])
local offset = 6

-- Validate every fixed-window counter before mutating anything.
for i = 1, rate_count do
  local key_index = tonumber(ARGV[offset])
  local cost = tonumber(ARGV[offset + 1])
  local limit = tonumber(ARGV[offset + 2])
  local current = tonumber(redis.call('GET', KEYS[key_index]) or '0')
  if current + cost > limit then
    local ttl = redis.call('PTTL', KEYS[key_index])
    if ttl < 1 then ttl = 1000 end
    return {0, math.ceil(ttl / 1000), i, math.max(0, limit - current)}
  end
  offset = offset + 3
end

local concurrency_count = tonumber(ARGV[offset])
offset = offset + 1
for i = 1, concurrency_count do
  local key_index = tonumber(ARGV[offset])
  local limit = tonumber(ARGV[offset + 1])
  redis.call('ZREMRANGEBYSCORE', KEYS[key_index], '-inf', now)
  local current = redis.call('ZCARD', KEYS[key_index])
  if current >= limit then
    local first = redis.call('ZRANGE', KEYS[key_index], 0, 0, 'WITHSCORES')
    local retry = 1
    if #first == 2 then retry = math.max(1, math.ceil((tonumber(first[2]) - now) / 1000)) end
    return {0, retry, rate_count + i, 0}
  end
  offset = offset + 2
end

offset = 6
local minimum_remaining = 2147483647
for i = 1, rate_count do
  local key_index = tonumber(ARGV[offset])
  local cost = tonumber(ARGV[offset + 1])
  local limit = tonumber(ARGV[offset + 2])
  local current = redis.call('INCRBY', KEYS[key_index], cost)
  if current == cost then redis.call('PEXPIRE', KEYS[key_index], math.max(1000, rate_reset - now)) end
  minimum_remaining = math.min(minimum_remaining, math.max(0, limit - current))
  offset = offset + 3
end

concurrency_count = tonumber(ARGV[offset])
offset = offset + 1
for i = 1, concurrency_count do
  local key_index = tonumber(ARGV[offset])
  redis.call('ZADD', KEYS[key_index], expires, request_id)
  redis.call('PEXPIRE', KEYS[key_index], math.max(1000, expires - now + 60000))
  offset = offset + 2
end
return {1, 0, 0, minimum_remaining}
"""

_RELEASE = r"""
for i = 1, #KEYS do redis.call('ZREM', KEYS[i], ARGV[1]) end
return 1
"""

_RENEW = r"""
-- A lease is owned only while this exact, unexpired request member is present
-- in every hierarchical concurrency set. Validate the complete hierarchy
-- before changing any score so a partial lease can never be resurrected.
for i = 1, #KEYS do
  local score = redis.call('ZSCORE', KEYS[i], ARGV[1])
  if not score or tonumber(score) <= tonumber(ARGV[3]) then return 0 end
end
for i = 1, #KEYS do
  redis.call('ZADD', KEYS[i], tonumber(ARGV[2]), ARGV[1])
  redis.call('PEXPIRE', KEYS[i], math.max(1000, tonumber(ARGV[2]) - tonumber(ARGV[3]) + 60000))
end
return 1
"""


class DistributedLimiter:
    def __init__(self, redis_client: Any | None = None, *, policy: LimitPolicy | None = None,
                 clock=time.time):
        self.policy = policy or LimitPolicy.from_env()
        self.redis = redis_client
        self._clock = clock
        self.production = hosted_runtime()
        if self.redis is None and os.getenv("REDIS_URL"):
            started = time.perf_counter()
            try:
                from redis.asyncio import Redis

                self.redis = Redis.from_url(
                    os.environ["REDIS_URL"],
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                    health_check_interval=30,
                )
            except Exception as exc:  # dependency/configuration failure
                _record_redis_dependency(_redis_failure_outcome(exc), started)
                raise LimiterUnavailable("Redis limiter could not be configured") from exc

    @staticmethod
    def _safe(value: str) -> str:
        if (not value or len(value.encode("utf-8")) > 128
                or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                       for c in value)):
            raise ValueError("limit identity must be an opaque identifier")
        return value

    def _keys(self, identity: LimitIdentity, window: int) -> tuple[list[str], list[str]]:
        org = self._safe(identity.organization_id)
        customer = self._safe(identity.customer_id or "unattributed")
        key = self._safe(identity.key_id)
        provider = self._safe(identity.provider or "all")
        prefix = "brevitas:limit"
        rate = [
            f"{prefix}:rpm:org:{org}:{window}",
            f"{prefix}:rpm:customer:{org}:{customer}:{window}",
            f"{prefix}:rpm:key:{key}:{window}",
            f"{prefix}:rpm:provider:{provider}:{window}",
            f"{prefix}:tpm:org:{org}:{window}",
            f"{prefix}:tpm:customer:{org}:{customer}:{window}",
            f"{prefix}:tpm:key:{key}:{window}",
        ]
        concurrent = [
            f"{prefix}:active:org:{org}",
            f"{prefix}:active:customer:{org}:{customer}",
            f"{prefix}:active:key:{key}",
            f"{prefix}:active:provider:{provider}",
        ]
        return rate, concurrent

    async def acquire(self, identity: LimitIdentity, *, tokens: int = 1, request_id: str = "") -> LimitLease:
        # This is a concurrency lease identifier, not the caller's idempotency
        # key. It must always be unique or repeated client request IDs would
        # replace one another in the Redis sorted set and bypass concurrency.
        request_id = uuid.uuid4().hex
        if self.redis is None:
            if self.production:
                started = time.perf_counter()
                _record_redis_dependency("unavailable", started)
                raise LimiterUnavailable("Redis is required in production")
            return LimitLease(True, request_id, remaining_requests=self.policy.key_rpm,
                              reset_seconds=60)

        tokens = max(1, int(tokens))
        now = int(self._clock() * 1000)
        reset = ((now // 60000) + 1) * 60000
        expires = now + self.policy.lease_seconds * 1000
        rate, concurrent = self._keys(identity, now // 60000)
        keys = [*rate, *concurrent]
        rate_specs = [
            (1, 1, self.policy.organization_rpm),
            (2, 1, self.policy.customer_rpm),
            (3, 1, self.policy.key_rpm),
            (4, 1, self.policy.provider_rpm),
            (5, tokens, self.policy.organization_tpm),
            (6, tokens, self.policy.customer_tpm),
            (7, tokens, self.policy.key_tpm),
        ]
        concurrency_specs = [
            (8, self.policy.organization_concurrency),
            (9, self.policy.customer_concurrency),
            (10, self.policy.key_concurrency),
            (11, self.policy.provider_concurrency),
        ]
        argv: list[Any] = [now, expires, reset, request_id, len(rate_specs)]
        for spec in rate_specs:
            argv.extend(spec)
        argv.append(len(concurrency_specs))
        for spec in concurrency_specs:
            argv.extend(spec)
        started = time.perf_counter()
        try:
            result = await self.redis.eval(_ACQUIRE, len(keys), *keys, *argv)
        except Exception as exc:
            _record_redis_dependency(_redis_failure_outcome(exc), started)
            raise LimiterUnavailable("Redis admission check failed") from exc
        _record_redis_dependency("success", started)
        allowed = bool(int(result[0]))
        if not allowed:
            return LimitLease(False, request_id, retry_after=max(1, int(result[1])),
                              reason=f"limit_{int(result[2])}",
                              remaining_requests=max(0, int(result[3])),
                              reset_seconds=max(1, (reset - now + 999) // 1000))
        return LimitLease(True, request_id, remaining_requests=max(0, int(result[3])),
                          reset_seconds=max(1, (reset - now + 999) // 1000),
                          _limiter=self, _concurrency_keys=tuple(concurrent))

    async def healthy(self) -> bool:
        if self.redis is None:
            if self.production:
                started = time.perf_counter()
                _record_redis_dependency("unavailable", started)
            return not self.production
        started = time.perf_counter()
        try:
            healthy = bool(await self.redis.ping())
        except Exception as exc:
            _record_redis_dependency(_redis_failure_outcome(exc), started)
            return False
        _record_redis_dependency("success" if healthy else "unavailable", started)
        return healthy

    async def release(self, lease: LimitLease) -> None:
        if not self.redis:
            return
        started = time.perf_counter()
        try:
            await self.redis.eval(_RELEASE, len(lease._concurrency_keys),
                                  *lease._concurrency_keys, lease.request_id)
        except Exception as exc:
            _record_redis_dependency(_redis_failure_outcome(exc), started)
            raise LimiterUnavailable("Redis concurrency release failed") from exc
        _record_redis_dependency("success", started)

    async def renew(self, lease: LimitLease) -> bool:
        if not self.redis:
            return not self.production
        now = int(self._clock() * 1000)
        expires = now + self.policy.lease_seconds * 1000
        started = time.perf_counter()
        try:
            result = await self.redis.eval(
                _RENEW, len(lease._concurrency_keys), *lease._concurrency_keys,
                lease.request_id, expires, now,
            )
            owned = int(result) == 1
        except Exception as exc:
            _record_redis_dependency(_redis_failure_outcome(exc), started)
            raise LimiterUnavailable("Redis concurrency renewal failed") from exc
        _record_redis_dependency("success", started)
        return owned

    async def close(self) -> None:
        """Release Redis pool resources during replica shutdown."""
        if self.redis is None:
            return
        close = getattr(self.redis, "aclose", None) or getattr(self.redis, "close", None)
        if close is None:
            started = time.perf_counter()
            _record_redis_dependency("unavailable", started)
            return
        started = time.perf_counter()
        try:
            result = close()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:
            _record_redis_dependency(_redis_failure_outcome(exc), started)
            raise
        _record_redis_dependency("success", started)
