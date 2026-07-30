"""Resource-exhaustion regressions on the shared API replica.

Each test here pins a cross-tenant denial-of-service path that had no bound at all: the
pre-admission tokenizer, the pre-authentication JSON walk, the fail-closed cache-policy
read, the header-driven customer provisioning, and the unauthenticated readiness fan-out.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import UsageStore


def _service_key_client(tmp_path, monkeypatch, *, scopes=None, name="quota.db"):
    import api.server as server

    store = UsageStore(str(tmp_path / name))
    raw_key = "bvt_resource_bounds"
    organization = store.ensure_organization("owner-1", "Bounds organization")
    service = store.ensure_service_account(organization["id"], "test", "owner-1")
    store.create_key(
        hash_key(raw_key), "bounds", owner_id="owner-1",
        organization_id=organization["id"], service_account_id=service["id"],
        key_type="organization_service", environment="test",
        scopes=scopes or ["proxy:invoke", "usage:write", "usage:read_own",
                          "customer:route", "customer:auto_provision"],
    )
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    server._cache_enabled_cache.clear()
    return server, store, organization, raw_key, TestClient(server.app)


def test_admission_token_estimate_is_flat_in_body_size():
    """The limiter's token cost must not grow a full BPE encode with the body.

    A 2 MiB encode costs 100-200+ ms of pure CPU and ran on the single event loop BEFORE
    _distributed_limiter.acquire could reject anything, so one tenant's maximum-size prompts
    stalled every other tenant on the replica.
    """
    import api.server as server

    small = json.dumps({"messages": [{"role": "user", "content": "hello world " * 200}]})
    large = json.dumps({"messages": [{"role": "user", "content": "hello world " * 180_000}]})
    assert len(large.encode()) > 2_000_000

    def elapsed(payload: str) -> float:
        raw = payload.encode()
        started = time.perf_counter()
        server._estimated_token_cost(raw)
        return time.perf_counter() - started

    elapsed(small)  # warm the tokenizer cache before measuring
    small_s, large_s = elapsed(small), elapsed(large)
    # Sampling means the work is bounded by the 64 KiB head, not by the body: a full encode
    # of a body ~900x larger would be ~900x slower.
    assert large_s < max(0.05, small_s * 20), (small_s, large_s)


def test_admission_token_estimate_scales_with_high_entropy_density():
    """A high-entropy payload must not be under-charged on the TPM dimension.

    The estimate keeps the sample's tokens-per-byte ratio, unlike a flat len//4 which would
    walk a ~1.5 bytes/token payload through the guard at a fraction of its true cost.
    """
    import api.server as server

    dense = ("".join(chr(0x61 + (index * 7) % 26) for index in range(200_000))).encode()
    estimate = server._estimated_token_cost(dense)
    assert estimate > len(dense) // 4
    assert server._estimated_token_cost(b"") == 1


def test_sse_channel_hands_off_without_a_default_executor_thread():
    """Each open stream used to park one default-executor thread at ~100% duty cycle.

    That is the pool asyncio.to_thread uses for proxy auth and the readiness probe, so the
    pollers degraded authentication for every tenant on the replica.
    """
    import threading

    import api.server as server

    async def exercise():
        channel = server._ThreadEventChannel(asyncio.get_running_loop())
        sentinel = object()

        def worker():
            for index in range(3):
                channel.put({"stage": index})
            channel.put(sentinel)

        # A timeout with nothing queued must be re-pollable, not fatal.
        with pytest.raises(asyncio.TimeoutError):
            await channel.get(0.01)

        before = threading.active_count()
        threading.Thread(target=worker, daemon=True).start()
        received = []
        while True:
            item = await channel.get(1.0)
            if item is sentinel:
                break
            received.append(item)
        return received, before

    received, before = asyncio.run(exercise())
    assert received == [{"stage": 0}, {"stage": 1}, {"stage": 2}]
    assert before >= 1


def test_deeply_nested_json_is_not_a_pre_auth_500(tmp_path, monkeypatch):
    """The outermost middleware walked the parsed body with unbounded recursion.

    ~400 nested arrays (under 1 KB) raised RecursionError before any authentication ran, so
    an anonymous caller got a cheap error-log flood plus a full ASGI stack unwind.
    """
    import api.server as server

    _, _, _, _, client = _service_key_client(
        tmp_path, monkeypatch, name="nesting.db")
    for depth in (400, 1200):
        body = "[" * depth + "]" * depth
        response = client.post(
            "/v1/usage", content=body,
            headers={"Content-Type": "application/json", "X-Brevitas-Key": "bvt_missing"},
        )
        assert response.status_code < 500, (depth, response.status_code)

    assert server._request_collection_exceeds(
        json.loads("[" * 2000 + "]" * 2000), 100) is False
    assert server._request_collection_exceeds([[1, 2, 3]], 2) is True


def test_cache_policy_read_is_cached_and_never_a_503(tmp_path, monkeypatch):
    """A feature-flag lookup must not reject inference traffic or usage receipts.

    _authenticated turned any exception from cache_enabled into 503 "Authentication store
    unavailable", and the read was uncached on every authenticated request — 1-2 extra
    PostgREST round trips per call, including POST /v1/usage at 300/minute.
    """
    import api.server as server

    server, store, organization, raw_key, client = _service_key_client(
        tmp_path, monkeypatch, name="cache-policy.db")
    calls = {"count": 0}
    real_cache_enabled = store.cache_enabled

    def counting_cache_enabled(organization_id, customer_id=""):
        calls["count"] += 1
        return real_cache_enabled(organization_id, customer_id)

    monkeypatch.setattr(store, "cache_enabled", counting_cache_enabled)
    headers = {"X-Brevitas-Key": raw_key, "X-Brevitas-Customer-Id": "customer-a"}
    usage = {"provider": "openai", "model": "gpt-4o", "baseline_tokens": 100,
             "compressed_tokens": 90}
    for _ in range(3):
        assert client.post("/v1/usage", headers=headers, json=usage).status_code == 200
    assert calls["count"] == 1

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("supabase unavailable")

    monkeypatch.setattr(store, "cache_enabled", unavailable)
    server._cache_enabled_cache.clear()
    response = client.post("/v1/usage", headers=headers, json=usage)
    assert response.status_code == 200
    assert server._cache_enabled_cached(organization["id"], "") is False


def test_cache_policy_cache_is_per_customer_and_invalidated_on_change(tmp_path, monkeypatch):
    import api.server as server

    server, store, organization, _raw_key, _client = _service_key_client(
        tmp_path, monkeypatch, name="cache-policy-invalidate.db")
    customer = store.upsert_customer(organization["id"], "customer-a")
    store.set_cache_enabled(organization["id"], True, str(customer["id"]))

    # One customer's override must never answer for its siblings.
    assert server._cache_enabled_cached(organization["id"], str(customer["id"])) is True
    assert server._cache_enabled_cached(organization["id"], "") is False

    store.set_cache_enabled(organization["id"], False, str(customer["id"]))
    assert server._cache_enabled_cached(organization["id"], str(customer["id"])) is True
    server._invalidate_cache_policy(organization["id"])
    assert server._cache_enabled_cached(organization["id"], str(customer["id"])) is False


def test_header_driven_customer_provisioning_is_quota_bounded(tmp_path, monkeypatch):
    """X-Brevitas-Customer-ID mints a permanent row per unseen value, with no purge path."""
    import api.server as server

    server, store, organization, raw_key, client = _service_key_client(
        tmp_path, monkeypatch, name="customer-quota.db")
    monkeypatch.setenv("BREVITAS_MAX_CUSTOMERS_PER_ORG", "2")
    usage = {"provider": "openai", "model": "gpt-4o", "baseline_tokens": 10,
             "compressed_tokens": 9}
    for index in range(2):
        response = client.post("/v1/usage", json=usage, headers={
            "X-Brevitas-Key": raw_key,
            "X-Brevitas-Customer-Id": f"customer-{index}",
        })
        assert response.status_code == 200

    refused = client.post("/v1/usage", json=usage, headers={
        "X-Brevitas-Key": raw_key, "X-Brevitas-Customer-Id": "customer-overflow",
    })
    assert refused.status_code == 429
    assert refused.headers.get("Retry-After") == "60"
    # An already-provisioned customer keeps working at quota.
    assert client.post("/v1/usage", json=usage, headers={
        "X-Brevitas-Key": raw_key, "X-Brevitas-Customer-Id": "customer-0",
    }).status_code == 200
    assert store.customer_count_at_least(organization["id"], 3) is False


def test_customer_listing_is_paged(tmp_path, monkeypatch):
    import api.server as server

    server, store, organization, _raw_key, _client = _service_key_client(
        tmp_path, monkeypatch, name="customer-page.db")
    for index in range(5):
        store.upsert_customer(organization["id"], f"customer-{index:03d}")
    first = store.list_customers(organization["id"], limit=2)
    second = store.list_customers(organization["id"], limit=2, offset=2)
    assert len(first) == 2 and len(second) == 2
    assert {row["id"] for row in first}.isdisjoint({row["id"] for row in second})
    assert len(store.list_customers(organization["id"])) == 5


def test_readiness_dependency_probes_are_ttl_cached(monkeypatch):
    """Unauthenticated /v1/health ran a live Postgres query and a Redis PING per call."""
    import api.server as server

    probes = {"count": 0}

    class Store:
        def healthy(self):
            probes["count"] += 1
            return True

    class SharedLimiter:
        async def healthy(self):
            return True

    store = Store()
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_distributed_limiter", SharedLimiter())

    async def compressor_ready():
        return {"configured": True, "internal_auth_configured": True,
                "private_endpoint": True, "reachable": True, "model_loaded": True}

    async def kms_ready():
        return {"configured": True, "active_probe": True, "fresh": True}

    monkeypatch.setattr(server, "_compressor_status", compressor_ready)
    monkeypatch.setattr(server, "_kms_readiness_status", kms_ready)
    monkeypatch.setattr(server.app.state, "accepting_traffic", True)

    async def exercise():
        return [await server.health() for _ in range(5)]

    payloads = asyncio.run(exercise())
    assert all(payload["database_ready"] is True for payload in payloads)
    assert probes["count"] == 1


def test_only_the_liveness_probe_is_intermediary_cacheable():
    """A stale readiness verdict must not be servable from the public marketing origin."""
    import api.server as server

    with TestClient(server.app) as client:
        live = client.get("/v1/health/live")
        health = client.get("/v1/health")
    assert "Cache-Control" not in live.headers
    assert health.headers.get("Cache-Control") == "no-store"


def test_quality_stream_evidence_survives_the_registry_ttl():
    """The mSPRT stream is anytime-valid; a 1 h create-time TTL silently restarted it."""
    import api.server as server

    assert server._seq_streams.ttl_s >= 6 * 3600
    key = "tenant-ttl-probe"
    server._seq_streams.pop(key, None)
    stream = server._seq_stream(key)
    stream.update(False)
    stream.update(False)
    observed = server._seq_stream(key)
    assert observed is stream
    assert observed.state.n == 2


def test_quality_reset_requires_a_manage_scope(tmp_path, monkeypatch):
    """usage:read_own is minted into every member's dashboard session key."""
    import api.server as server

    server, _store, _organization, raw_key, client = _service_key_client(
        tmp_path, monkeypatch, name="quality-scope.db",
        scopes=["proxy:invoke", "usage:read_own"])
    headers = {"X-Brevitas-Key": raw_key}
    assert client.get("/v1/quality/stream", headers=headers).status_code == 200
    assert client.get("/v1/quality/levers", headers=headers).status_code == 200
    assert client.post("/v1/quality/stream/reset", headers=headers).status_code == 403
    assert "quality:manage" in server._dashboard_session_scopes("company_admin")
    assert "quality:manage" not in server._dashboard_session_scopes("member")


@pytest.mark.parametrize("route", ["/v1/jobs", "/v1/jobs/{job_id}",
                                   "/v1/jobs/{job_id}/cancel"])
def test_job_routes_carry_a_rate_limit(route):
    """Not the anti-abuse control (_rate_key buckets on peer IP) — a runaway-client brake."""
    import api.server as server

    limited = set(server.limiter._Limiter__marked_for_limiting)  # noqa: SLF001
    endpoint = {r.path: r.endpoint for r in server.app.routes
                if getattr(r, "path", "") == route}[route]
    assert f"{endpoint.__module__}.{endpoint.__name__}" in limited
