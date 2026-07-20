import asyncio
from types import SimpleNamespace

import pytest

import brevitas.observability as observability
from api.distributed_limits import (
    DistributedLimiter,
    LimitIdentity,
    LimitPolicy,
    LimiterUnavailable,
)


class FakeRedis:
    def __init__(self, acquire_result=None):
        self.acquire_result = acquire_result or [1, 0, 0, 17]
        self.calls = []
        self.closed = False

    async def eval(self, script, key_count, *values):
        self.calls.append((script, key_count, values))
        if "Validate every fixed-window" in script:
            return self.acquire_result
        return 1

    async def ping(self):
        return True

    async def aclose(self):
        self.closed = True


def policy():
    return LimitPolicy(
        organization_rpm=100,
        customer_rpm=50,
        key_rpm=25,
        provider_rpm=200,
        organization_tpm=1000,
        customer_tpm=500,
        key_tpm=250,
        organization_concurrency=10,
        customer_concurrency=5,
        key_concurrency=3,
        provider_concurrency=20,
        lease_seconds=60,
    )


def identity(customer="customer_1"):
    return LimitIdentity("org_1", customer, "key_1", "openai")


def test_hierarchical_keys_and_release_do_not_contain_raw_credentials():
    redis = FakeRedis()
    limiter = DistributedLimiter(redis, policy=policy())

    async def exercise():
        lease = await limiter.acquire(identity(), tokens=42, request_id="request_1")
        assert lease.allowed
        await lease.release()
        await lease.release()

    asyncio.run(exercise())
    _, key_count, values = redis.calls[0]
    keys = values[:key_count]
    assert any("org:org_1" in key for key in keys)
    assert any("customer:org_1:customer_1" in key for key in keys)
    assert any("provider:openai" in key for key in keys)
    assert all("bvt_" not in key for key in keys)
    assert len(redis.calls) == 2


def test_customer_limits_are_isolated():
    limiter = DistributedLimiter(FakeRedis(), policy=policy())
    first, _ = limiter._keys(identity("customer_a"), 1)
    second, _ = limiter._keys(identity("customer_b"), 1)
    assert first[0] == second[0]  # shared organization limit
    assert first[1] != second[1]  # isolated customer limit


def test_denial_returns_accurate_retry_metadata():
    limiter = DistributedLimiter(FakeRedis([0, 7, 2, 0]), policy=policy())
    lease = asyncio.run(limiter.acquire(identity(), tokens=1, request_id="request_2"))
    assert not lease.allowed
    assert lease.retry_after == 7
    assert lease.reason == "limit_2"


def test_rate_window_expiry_is_separate_from_long_concurrency_lease():
    redis = FakeRedis()
    limiter = DistributedLimiter(redis, policy=policy())
    asyncio.run(limiter.acquire(identity(), request_id="request_window"))
    _script, key_count, values = redis.calls[0]
    argv = values[key_count:]
    now, concurrency_expiry, rate_reset = map(int, argv[:3])
    assert concurrency_expiry - now == policy().lease_seconds * 1000
    assert 0 < rate_reset - now <= 60_000


def test_production_fails_closed_without_redis(monkeypatch):
    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.delenv("REDIS_URL", raising=False)
    limiter = DistributedLimiter(policy=policy())
    with pytest.raises(LimiterUnavailable):
        asyncio.run(limiter.acquire(identity()))


def test_railway_runtime_is_production_even_without_manual_env_flag(monkeypatch):
    monkeypatch.delenv("BREVITAS_ENV", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    limiter = DistributedLimiter(policy=policy())
    assert limiter.production is True
    assert asyncio.run(limiter.healthy()) is False


def test_identity_rejects_credential_shaped_values():
    limiter = DistributedLimiter(FakeRedis(), policy=policy())
    bad = LimitIdentity("org_1", "customer_1", "bvt_secret.value", "openai")
    with pytest.raises(ValueError):
        asyncio.run(limiter.acquire(bad))


def test_policy_values_are_positive_and_have_absolute_ceilings(monkeypatch):
    monkeypatch.setenv("BREVITAS_ORG_RPM", "0")
    monkeypatch.setenv("BREVITAS_ORG_TPM", "999999999999")
    monkeypatch.setenv("BREVITAS_LIMIT_LEASE_SECONDS", "-1")
    bounded = LimitPolicy.from_env()
    assert bounded.organization_rpm == 1
    assert bounded.organization_tpm == 2_000_000_000
    assert bounded.lease_seconds == 1


def test_identity_length_is_bounded_before_redis_call():
    redis = FakeRedis()
    limiter = DistributedLimiter(redis, policy=policy())
    oversized = LimitIdentity("o" * 129, "customer", "key", "openai")
    with pytest.raises(ValueError):
        asyncio.run(limiter.acquire(oversized))
    assert redis.calls == []


def test_limiter_uses_injected_clock_for_deterministic_expiry():
    redis = FakeRedis()
    limiter = DistributedLimiter(redis, policy=policy(), clock=lambda: 123.0)
    asyncio.run(limiter.acquire(identity()))
    _script, key_count, values = redis.calls[0]
    now, expires = map(int, values[key_count:][:2])
    assert now == 123_000
    assert expires == 123_000 + policy().lease_seconds * 1000


def test_redis_telemetry_uses_fixed_content_free_labels(monkeypatch):
    events = []

    class Metrics:
        def record_dependency(self, **values):
            events.append(values)

    monkeypatch.setattr(
        observability, "get_runtime",
        lambda **_kwargs: SimpleNamespace(metrics=Metrics()),
    )
    redis = FakeRedis()
    limiter = DistributedLimiter(redis, policy=policy())

    async def exercise():
        lease = await limiter.acquire(identity("SECRET-CUSTOMER"), tokens=2)
        assert await limiter.healthy() is True
        await lease.release()
        await limiter.close()

    asyncio.run(exercise())
    assert redis.closed is True
    assert len(events) == 4
    assert all(set(event) == {"dependency", "outcome", "duration_seconds"}
               for event in events)
    assert all(event["dependency"] == "redis" for event in events)
    assert all(event["outcome"] == "success" for event in events)
    assert all(isinstance(event["duration_seconds"], float)
               and event["duration_seconds"] >= 0 for event in events)
    serialized = repr(events)
    assert "SECRET-CUSTOMER" not in serialized
    assert "org_1" not in serialized
    assert "key_1" not in serialized
    assert "openai" not in serialized


@pytest.mark.parametrize(
    ("failure", "expected"),
    [(TimeoutError("SECRET timeout"), "timeout"),
     (RuntimeError("SECRET failure"), "error")],
)
def test_redis_failure_telemetry_is_classified_without_exception_text(
    monkeypatch, failure, expected,
):
    events = []

    class Metrics:
        def record_dependency(self, **values):
            events.append(values)

    class BrokenRedis:
        async def eval(self, *_args):
            raise failure

    monkeypatch.setattr(
        observability, "get_runtime",
        lambda **_kwargs: SimpleNamespace(metrics=Metrics()),
    )
    limiter = DistributedLimiter(BrokenRedis(), policy=policy())
    with pytest.raises(LimiterUnavailable):
        asyncio.run(limiter.acquire(identity()))
    assert len(events) == 1
    assert events[0]["dependency"] == "redis"
    assert events[0]["outcome"] == expected
    assert "SECRET" not in repr(events)


def test_missing_production_redis_records_unavailable(monkeypatch):
    events = []

    class Metrics:
        def record_dependency(self, **values):
            events.append(values)

    monkeypatch.setattr(
        observability, "get_runtime",
        lambda **_kwargs: SimpleNamespace(metrics=Metrics()),
    )
    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.delenv("REDIS_URL", raising=False)
    limiter = DistributedLimiter(policy=policy())
    with pytest.raises(LimiterUnavailable):
        asyncio.run(limiter.acquire(identity()))
    assert [event["outcome"] for event in events] == ["unavailable"]


def test_redis_telemetry_failure_does_not_change_limiter_behavior(monkeypatch):
    class Metrics:
        def record_dependency(self, **_values):
            raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(
        observability, "get_runtime",
        lambda **_kwargs: SimpleNamespace(metrics=Metrics()),
    )
    lease = asyncio.run(DistributedLimiter(FakeRedis(), policy=policy()).acquire(identity()))
    assert lease.allowed is True
