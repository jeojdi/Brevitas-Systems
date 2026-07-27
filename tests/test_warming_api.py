import asyncio
import json
import logging
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import UsageStore
from brevitas.security import EnvelopeCipher, LocalTestKMS
from brevitas.warming import WarmPrefix

SECRET = "sk-ant-warm-provider-private-value"


def _setup(tmp_path, monkeypatch, name):
    import api.server as server

    store = UsageStore(str(tmp_path / f"{name}.db"))
    organization = store.ensure_organization("warm-admin", "Warm company")
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO organization_members(organization_id,user_id,role,created_at)"
            " VALUES(?,?,?,?)",
            (organization["id"], "warm-member", "member", "2026-07-27T00:00:00+00:00"),
        )
    kms = LocalTestKMS(b"w" * 32, environ={"BREVITAS_ENV": "test"})
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_credential_cipher", EnvelopeCipher(
        kms, key_id="warm-test-key", key_version="1", wrap_algorithm=kms.algorithm))
    monkeypatch.setattr(server, "_dashboard_user", lambda request: {
        "Bearer admin-session": "warm-admin",
        "Bearer member-session": "warm-member",
    }.get(request.headers.get("authorization", ""), ""))
    server._warm_enabled_cache.clear()
    return server, store, organization, TestClient(server.app)


def _enroll(client, session="Bearer admin-session", **overrides):
    payload = {
        "provider": "anthropic", "provider_api_key": SECRET,
        "enabled": True, "accept_spend_terms": True,
        "daily_budget_usd": 5.0,
        **overrides,
    }
    return client.put("/v1/warming", headers={"Authorization": session}, json=payload)


def test_warming_config_requires_admin_role_and_spend_consent(tmp_path, monkeypatch):
    server, store, organization, client = _setup(tmp_path, monkeypatch, "warm-roles")

    assert _enroll(client, session="").status_code == 401
    assert _enroll(client, session="Bearer member-session").status_code == 403
    assert client.delete("/v1/warming/anthropic",
                         headers={"Authorization": "Bearer member-session"}).status_code == 403

    no_consent = _enroll(client, accept_spend_terms=False)
    assert no_consent.status_code == 400
    assert "accept_spend_terms" in no_consent.json()["detail"]
    assert store.warm_credentials_get(organization["id"], "anthropic") is None

    # OpenAI is schema-ready and gpt-5.6+ economics are favorable, but OpenAI
    # does not document TTL refresh on read and no ping pipeline exists, so
    # consenting to unverifiable keep-alive spend must still be rejected —
    # with the economic reason stated in the detail.
    inactive = _enroll(client, provider="openai")
    assert inactive.status_code == 400
    assert "not active" in inactive.json()["detail"]
    assert "0.10x" in inactive.json()["detail"]
    assert "refresh on read" in inactive.json()["detail"]
    assert store.warm_credentials_get(organization["id"], "openai") is None

    saved = _enroll(client)
    assert saved.status_code == 200
    assert saved.json()["enabled"] is True
    assert saved.json()["credential_state"] == "active"
    assert saved.json()["masked_key"] == "*" * 8 + SECRET[-4:]
    assert SECRET not in saved.text
    stored = store.warm_credentials_get(organization["id"], "anthropic")
    assert stored["enabled"] is True and stored["consent_at"]
    assert store.warm_enabled(organization["id"]) is True

    # Members can read status; the credential never leaves the store.
    status = client.get("/v1/warming", headers={"Authorization": "Bearer member-session"})
    assert status.status_code == 200
    assert status.json()["providers"][0]["provider"] == "anthropic"
    assert SECRET not in status.text
    assert "credential_ciphertext" not in status.text


def test_warming_keep_existing_key_and_unknown_provider(tmp_path, monkeypatch):
    server, store, organization, client = _setup(tmp_path, monkeypatch, "warm-keys")

    # Keep-existing-key semantics require a stored credential first.
    missing = _enroll(client, provider_api_key="")
    assert missing.status_code == 400
    assert _enroll(client).status_code == 200

    updated = _enroll(client, provider_api_key="", daily_budget_usd=9.0)
    assert updated.status_code == 200
    assert updated.json()["credential_state"] == "active"
    assert updated.json()["masked_key"] == ""
    assert store.warm_credentials_get(
        organization["id"], "anthropic")["daily_budget_usd"] == 9.0

    assert _enroll(client, provider="grok").status_code == 400
    assert client.delete("/v1/warming/grok",
                         headers={"Authorization": "Bearer admin-session"}).status_code == 400


def test_warming_deepseek_enable_requires_consent_then_succeeds(tmp_path, monkeypatch):
    # DeepSeek is warm-active (0.02x automatic-cache reads, hours-long
    # lifetime), and the SQLite allowlist admits it natively — no monkeypatch:
    # this exercises the exact store the dev-parity backend ships.
    server, store, organization, client = _setup(tmp_path, monkeypatch, "warm-deepseek")

    no_consent = _enroll(client, provider="deepseek", accept_spend_terms=False)
    assert no_consent.status_code == 400
    assert "accept_spend_terms" in no_consent.json()["detail"]
    assert store.warm_credentials_get(organization["id"], "deepseek") is None

    saved = _enroll(client, provider="deepseek")
    assert saved.status_code == 200
    assert saved.json()["provider"] == "deepseek"
    assert saved.json()["enabled"] is True
    assert saved.json()["credential_state"] == "active"
    stored = store.warm_credentials_get(organization["id"], "deepseek")
    assert stored["enabled"] is True and stored["consent_at"]
    assert store.warm_enabled(organization["id"]) is True

    assert client.delete("/v1/warming/deepseek",
                         headers={"Authorization": "Bearer admin-session"}
                         ).status_code == 200


def test_server_and_store_warm_provider_allowlists_agree():
    # The server's known/active provider tuples and the store's allowlist must
    # move together: a provider the API advertises as enable-able that the
    # SQLite backend rejects turns every gate-passing PUT into a 400 with a
    # self-contradicting detail.
    import api.server as server
    import api.store as store_module

    assert set(server._WARM_PROVIDERS) == set(store_module._WARM_PROVIDERS)
    assert set(server._WARM_ACTIVE_PROVIDERS) <= set(store_module._WARM_PROVIDERS)


def test_warming_delete_purges_credential_and_prefixes(tmp_path, monkeypatch):
    server, store, organization, client = _setup(tmp_path, monkeypatch, "warm-purge")
    assert _enroll(client).status_code == 200
    customer = store.upsert_customer(organization["id"], "customer-1")
    observed = store.warm_prefix_observe(
        organization["id"], str(customer["id"]), "anthropic", "a" * 64,
        "ciphertext", 2048, 300, 60, cache_read=False)
    assert observed["status"] == "observed"

    purged = client.delete("/v1/warming/anthropic",
                           headers={"Authorization": "Bearer admin-session"})
    assert purged.status_code == 200
    assert purged.json() == {"purged": True, "provider": "anthropic",
                             "prefixes_deleted": 1}
    assert store.warm_credentials_get(organization["id"], "anthropic") is None
    assert store.warm_enabled(organization["id"]) is False
    assert client.delete("/v1/warming/anthropic",
                         headers={"Authorization": "Bearer admin-session"}).status_code == 404


def test_warm_enabled_middleware_flag_is_cached_and_best_effort(tmp_path, monkeypatch):
    server, store, organization, client = _setup(tmp_path, monkeypatch, "warm-flag")

    assert server._warm_enabled_cached(organization["id"]) is False
    assert server._warm_enabled_cached("") is False
    assert _enroll(client).status_code == 200
    # The PUT invalidates the negative cache entry immediately.
    assert server._warm_enabled_cached(organization["id"]) is True

    class Broken:
        def warm_enabled(self, organization_id, customer_id=""):
            raise RuntimeError("store unavailable")

    monkeypatch.setattr(server, "_store", Broken())
    # Cached value survives a store outage; an uncached org degrades to False.
    assert server._warm_enabled_cached(organization["id"]) is True
    assert server._warm_enabled_cached("other-org") is False
    assert "other-org" not in server._warm_enabled_cache


def test_hosted_warm_observe_encrypts_payload_and_swallows_failures(
        tmp_path, monkeypatch, caplog):
    server, store, organization, client = _setup(tmp_path, monkeypatch, "warm-observe")
    assert _enroll(client).status_code == 200
    customer = store.upsert_customer(organization["id"], "customer-1")
    payload = {"model": "claude-sonnet-4-6", "tools": [], "system": None,
               "messages_prefix": [{"role": "user", "content": [
                   {"type": "text", "text": "PRIVATE-WARM-PREFIX",
                    "cache_control": {"type": "ephemeral"}}]}],
               "ttl": "", "vary": {"anthropic-version": "2023-06-01"}}
    prefix = WarmPrefix(provider="anthropic", model="claude-sonnet-4-6",
                        prefix_hash="b" * 64, prefix_tokens=2048,
                        provider_ttl_seconds=300, payload=payload)

    def _active_prefixes():
        providers = store.warm_status(organization["id"])["providers"]
        return providers[0]["active_prefixes"] if providers else 0

    # Without a recording organization_service key the worker could never
    # attribute ping spend, so the observation must be dropped entirely.
    server._hosted_warm_observe(
        organization["id"], str(customer["id"]), prefix, cache_read=False)
    assert _active_prefixes() == 0
    token = server._proxy_auth_context.set(server.AuthContext(
        key_hash="kh_warm_member", organization_id=organization["id"],
        customer_id=str(customer["id"]), key_type="organization_member"))
    try:
        server._hosted_warm_observe(
            organization["id"], str(customer["id"]), prefix, cache_read=False)
        assert _active_prefixes() == 0

        server._proxy_auth_context.set(server.AuthContext(
            key_hash="kh_warm_service", organization_id=organization["id"],
            customer_id=str(customer["id"]), key_type="organization_service"))
        server._hosted_warm_observe(
            organization["id"], str(customer["id"]), prefix, cache_read=False)
        assert _active_prefixes() == 1
        with sqlite3.connect(store.db_path) as db:
            # Observer-priced worst case of one keep-alive: a full 5m-tier
            # cache write for claude-sonnet-4-6 (3.75/MTok * 2048 tokens).
            # warm_due_claim reserves at least this, so settle's actual spend
            # can never breach the daily budget ceiling.
            reserve = db.execute(
                "SELECT ping_reserve_usd FROM warm_prefixes WHERE prefix_hash=?",
                ("b" * 64,)).fetchone()[0]
            assert reserve == pytest.approx(3.75 * 2048 / 1_000_000)
            ciphertext = db.execute(
                "SELECT payload_ciphertext FROM warm_prefixes WHERE prefix_hash=?",
                ("b" * 64,)).fetchone()[0]
        assert "PRIVATE-WARM-PREFIX" not in ciphertext
        assert "PRIVATE-WARM-PREFIX".encode() not in Path(store.db_path).read_bytes()
        # The stored plaintext is the worker's attribution envelope, decryptable
        # under the exact context _warm_one uses (api/worker.py).
        assert json.loads(server._decrypt(ciphertext, context={
            "purpose": "warm_prefix_payload",
            "organization_id": organization["id"],
            "customer_id": str(customer["id"]),
        })) == {"recorded_by_key_hash": "kh_warm_service", "payload": payload}

        class Broken:
            def warm_prefix_observe(self, *args, **kwargs):
                raise RuntimeError("PRIVATE-STORE-DETAIL")

        monkeypatch.setattr(server, "_store", Broken())
        # brevitas.api runs with propagate=False under JSON logging
        # (configure_json_logging), so pytest's caplog — a handler on the root
        # logger — only sees this WARNING when propagation is enabled. Without
        # this the positive assertion below is order-dependent: it passes only if
        # an earlier test happened to leave propagation on, and fails in isolation
        # or when the suite is sharded (e.g. CI's backend subset).
        monkeypatch.setattr(logging.getLogger("brevitas.api"), "propagate", True)
        with caplog.at_level(logging.WARNING, logger="brevitas.api"):
            server._hosted_warm_observe(
                organization["id"], str(customer["id"]), prefix, cache_read=True)
        assert "warm prefix observation failed error_type=RuntimeError" in caplog.text
        assert "PRIVATE-STORE-DETAIL" not in caplog.text
        assert "PRIVATE-WARM-PREFIX" not in caplog.text
    finally:
        server._proxy_auth_context.reset(token)


def test_hosted_warm_observe_reserves_provider_aware_ping_cost(tmp_path, monkeypatch):
    server, store, organization, client = _setup(tmp_path, monkeypatch, "warm-reserve")

    class Recorder:
        def __init__(self):
            self.reserves = []

        def warm_prefix_observe(self, *args, ping_reserve_usd=None):
            self.reserves.append(ping_reserve_usd)
            return {"status": "observed"}

    recorder = Recorder()
    monkeypatch.setattr(server, "_store", recorder)
    token = server._proxy_auth_context.set(server.AuthContext(
        key_hash="kh_warm_service", organization_id=organization["id"],
        customer_id="customer-1", key_type="organization_service"))
    try:
        def observe(provider, model, ttl=300):
            server._hosted_warm_observe(
                organization["id"], "customer-1",
                WarmPrefix(provider=provider, model=model,
                           prefix_hash="d" * 64, prefix_tokens=1_000_000,
                           provider_ttl_seconds=ttl, payload={"model": model}),
                cache_read=False)

        # Anthropic worst case stays a full cache write at the TTL premium
        # (5m: 1.25x write row; 1h: 2.0x input fallback), so settle can never
        # overshoot the reservation.
        observe("anthropic", "claude-sonnet-4-6")
        observe("anthropic", "claude-sonnet-4-6", ttl=3600)
        # Automatic caches have no write premium, but a ping whose entry
        # already evicted settles the whole prefix at the FULL input rate
        # (deepseek-chat 0.14, v4-pro 0.435 $/MTok — 39-120x the cached
        # rate), so the reserve carries the miss worst case, never the
        # cached best case. Reserving cached would let daily_budget_usd be
        # overspent whenever BREVITAS_WARM_RESERVE_USD_PER_MTOK is tuned
        # below the model's input rate.
        observe("deepseek", "deepseek-chat")
        observe("deepseek", "deepseek-v4-pro")
        # Unpriced models stay None so any stored reservation is kept.
        observe("deepseek", "deepseek-unknown-future")
    finally:
        server._proxy_auth_context.reset(token)
    assert recorder.reserves == [
        pytest.approx(3.75), pytest.approx(6.0), pytest.approx(0.14),
        pytest.approx(0.435), None]


def test_hosted_warm_observe_reserve_upper_bounds_full_miss_settle(
        tmp_path, monkeypatch):
    """Structural budget invariant: for every priced model of every warmable
    provider, the observer reserve covers a ping that settles as a full
    cache miss (whole prefix at the input rate; anthropic re-writes at the
    TTL-tier premium). warm_ping_settle books actual receipt cost with no
    clamp, and the flat BREVITAS_WARM_RESERVE_USD_PER_MTOK claim floor is a
    0..1000 tunable — so this per-row reserve is the only thing that makes
    reserved+spent+reserve <= daily_budget_usd hold unconditionally."""
    from api.worker import WARM_PROVIDER_SPECS
    from brevitas.receipts import MODEL_PRICES

    server, store, organization, client = _setup(tmp_path, monkeypatch, "warm-bound")
    reserves = []

    class Recorder:
        def warm_prefix_observe(self, *args, ping_reserve_usd=None):
            reserves.append(ping_reserve_usd)
            return {"status": "observed"}

    monkeypatch.setattr(server, "_store", Recorder())
    token = server._proxy_auth_context.set(server.AuthContext(
        key_hash="kh_warm_service", organization_id=organization["id"],
        customer_id="customer-1", key_type="organization_service"))
    try:
        for (provider, model), price in sorted(MODEL_PRICES.items()):
            if provider not in WARM_PROVIDER_SPECS:
                continue
            server._hosted_warm_observe(
                organization["id"], "customer-1",
                WarmPrefix(provider=provider, model=model,
                           prefix_hash="e" * 64, prefix_tokens=1_000_000,
                           provider_ttl_seconds=int(
                               WARM_PROVIDER_SPECS[provider]["ttl_seconds"]),
                           payload={"model": model}),
                cache_read=False)
            reserve = reserves.pop()
            if provider == "anthropic":
                worst_miss = price.get("write", price["input"] * 1.25)
            else:
                worst_miss = max(price["input"],
                                 price.get("write", price["input"]))
            assert reserve is not None and reserve >= worst_miss, (
                provider, model)
    finally:
        server._proxy_auth_context.reset(token)


def test_warm_observe_to_worker_ping_round_trip(tmp_path, monkeypatch):
    """Cross-seam pin: ciphertext produced by the real observer must decrypt,
    unwrap, and warm inside the real worker (context binding + recorded-by
    envelope), with the provider ping carrying the decrypted credential."""
    import api.worker as worker

    server, store, organization, client = _setup(tmp_path, monkeypatch, "warm-seam")
    assert _enroll(client).status_code == 200
    customer = store.upsert_customer(organization["id"], "customer-1")
    payload = {"model": "claude-sonnet-4-6", "tools": [], "system": None,
               "messages_prefix": [{"role": "user", "content": [
                   {"type": "text", "text": "warm prefix body",
                    "cache_control": {"type": "ephemeral"}}]}],
               "ttl": "", "vary": {"anthropic-version": "2023-06-01"}}
    prefix = WarmPrefix(provider="anthropic", model="claude-sonnet-4-6",
                        prefix_hash="c" * 64, prefix_tokens=2048,
                        provider_ttl_seconds=300, payload=payload)
    token = server._proxy_auth_context.set(server.AuthContext(
        key_hash="kh_warm_service", organization_id=organization["id"],
        customer_id=str(customer["id"]), key_type="organization_service"))
    try:
        server._hosted_warm_observe(
            organization["id"], str(customer["id"]), prefix, cache_read=False)
    finally:
        server._proxy_auth_context.reset(token)
    with sqlite3.connect(store.db_path) as db:
        payload_ciphertext = db.execute(
            "SELECT payload_ciphertext FROM warm_prefixes WHERE prefix_hash=?",
            ("c" * 64,)).fetchone()[0]
    assert payload_ciphertext

    class Lease:
        allowed = True

        async def release(self):
            pass

    class Limiter:
        async def acquire(self, identity, *, tokens=1, request_id=""):
            return Lease()

    class Response:
        status_code = 200

        def json(self):
            return {"usage": {"input_tokens": 3, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 2048, "output_tokens": 1}}

        def close(self):
            pass

    class Pool:
        def __init__(self):
            self.calls = []

        def request(self, provider, operation, method, url, *, headers=None, json=None):
            self.calls.append({"headers": headers, "json": json})
            return Response()

    class SettleStore:
        def __init__(self):
            self.settles = []

        def warm_ping_settle(self, *args, claim_token=None):
            self.settles.append((*args, claim_token))
            return {"status": "settled", "outcome": args[7]}

    settle_store = SettleStore()
    pool = Pool()
    recorded_usage = []
    monkeypatch.setattr(worker, "_store", settle_store)
    monkeypatch.setattr(worker, "provider_sync_http", pool)
    monkeypatch.setattr(worker, "_distributed_limiter", Limiter())
    monkeypatch.setattr(worker, "_safe_record_usage",
                        lambda **values: recorded_usage.append(values) or True)
    monkeypatch.setattr(
        worker, "_authoritative_service_key_context",
        lambda kh: ({"owner_id": "warm-admin", "key_type": "organization_service"}
                    if kh == "kh_warm_service" else None))
    row = {
        "organization_id": organization["id"], "customer_id": str(customer["id"]),
        "provider": "anthropic", "prefix_hash": "c" * 64, "prefix_tokens": 2048,
        "provider_ttl_seconds": 300, "payload_ciphertext": payload_ciphertext,
        "credential_ciphertext": server._encrypt(SECRET, context={
            "purpose": "warm_provider_credential",
            "organization_id": organization["id"],
        }),
        "reserved_usd": 0.0096, "budget_day": "2026-07-27",
    }

    asyncio.run(worker._warm_one(row, cycle_ts=1753600000, safety_margin_seconds=60))

    assert len(settle_store.settles) == 1
    assert settle_store.settles[0][7] == "warmed"
    assert len(pool.calls) == 1
    assert pool.calls[0]["headers"]["x-api-key"] == SECRET
    assert pool.calls[0]["json"]["max_tokens"] == 1
    assert pool.calls[0]["json"]["messages"][0] == payload["messages_prefix"][0]
    assert len(recorded_usage) == 1
    assert recorded_usage[0]["strategy"] == "cache_warm"
    assert recorded_usage[0]["key_hash"] == "kh_warm_service"


def test_stats_cache_endpoint_contract(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "stats-cache.db"))
    store.create_key(hash_key("bvt_cache_stats"), "cache-stats")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    server._seq_streams.clear()
    client = TestClient(server.app)
    headers = {"X-Brevitas-Key": "bvt_cache_stats"}

    assert client.get("/v1/stats/cache").status_code == 401

    reported = client.post("/v1/usage", headers=headers, json={
        "provider": "openai", "model": "gpt-4o-mini",
        "baseline_tokens": 100, "compressed_tokens": 100,
        "fresh_input_tokens": 60, "cached_input_tokens": 40, "output_tokens": 10,
        "cache_attributable": True, "strategy": "native_cache",
        "request_id": "cache-stats-row",
    })
    assert reported.status_code == 200

    stats = client.get("/v1/stats/cache", headers=headers)
    assert stats.status_code == 200
    body = stats.json()
    # per_provider is additive and lands with the store-side breakdown; every
    # original top-level field stays put either way.
    assert set(body) - {"per_provider"} == {
        "cached_input_tokens", "fresh_input_tokens", "cache_hit_rate_pct",
        "native_cache_discount_usd", "attributable_discount_usd",
        "calls_avoided", "warm_pings", "warm_spend_usd", "warm_hits", "history",
    }
    assert body["cached_input_tokens"] == 40
    assert body["fresh_input_tokens"] == 60
    assert body["cache_hit_rate_pct"] == 40.0
    assert body["native_cache_discount_usd"] > 0
    # SDK-reported rows are analytics only — they are never billed, so they
    # never appear under the billable tile.
    assert body["attributable_discount_usd"] == 0
    assert body["calls_avoided"] == 0
    # warm_* stays null until the organization enrolls in warming.
    assert body["warm_pings"] is None
    assert body["warm_spend_usd"] is None
    assert body["warm_hits"] is None
    assert len(body["history"]) == 1
    assert set(body["history"][0]) == {
        "week_start", "cached_input_tokens", "native_cache_discount_usd",
        "warm_spend_usd",
    }
    assert body["history"][0]["cached_input_tokens"] == 40

    # An authoritative exact_cache replay has an all-zero receipt (discount 0)
    # yet bills its whole avoided-call cost: the billable tile must carry the
    # billed amount so it reconciles with the 25% fee.
    replay = server._record_usage_report(
        hash_key("bvt_cache_stats"),
        server.UsageReportRequest(
            provider="openai", model="gpt-4o-mini",
            baseline_tokens=100, compressed_tokens=0,
            fresh_input_tokens=0, output_tokens=0,
            baseline_output_tokens=20, strategy="exact_cache",
            request_id="exact-replay",
        ),
        authoritative=True,
    )
    assert replay["verified_savings_usd"] > 0
    assert replay["brevitas_fee_usd"] > 0
    body = client.get("/v1/stats/cache", headers=headers).json()
    assert body["attributable_discount_usd"] == pytest.approx(
        replay["verified_savings_usd"])


def test_stats_cache_per_provider_is_additive_and_normalized(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "stats-cache-providers.db"))
    store.create_key(hash_key("bvt_cache_providers"), "cache-providers")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    client = TestClient(server.app)
    headers = {"X-Brevitas-Key": "bvt_cache_providers"}

    base = {"cached_input_tokens": 40, "fresh_input_tokens": 60,
            "cache_hit_rate_pct": 40.0, "native_cache_discount_usd": 0.1,
            "attributable_discount_usd": 0.0, "calls_avoided": 0,
            "warm_pings": None, "warm_spend_usd": None, "warm_hits": None,
            "history": []}

    # A store that has not learned per_provider keeps the original response
    # shape rather than advertising an empty breakdown it never measured.
    monkeypatch.setattr(store, "cache_stats", lambda kh: dict(base))
    body = client.get("/v1/stats/cache", headers=headers).json()
    assert "per_provider" not in body
    assert body["cached_input_tokens"] == 40

    # Rows pass through normalized to the documented fields — store internals
    # and non-dict noise are dropped, missing fields surface as null, and the
    # original top-level fields are untouched.
    monkeypatch.setattr(store, "cache_stats", lambda kh: {
        **base,
        "per_provider": [
            {"provider": "anthropic", "cached_input_tokens": 30,
             "native_cache_discount_usd": 0.09,
             "attributable_discount_usd": 0.09, "warm_pings": 2,
             "warm_spend_usd": 0.0001, "warm_hits": 1,
             "internal_row_id": "must-not-leak"},
            {"provider": "deepseek", "cached_input_tokens": 10,
             "native_cache_discount_usd": 0.01},
            "not-a-row",
        ],
    })
    body = client.get("/v1/stats/cache", headers=headers).json()
    assert body["cached_input_tokens"] == 40
    assert "must-not-leak" not in json.dumps(body)
    assert body["per_provider"] == [
        {"provider": "anthropic", "cached_input_tokens": 30,
         "native_cache_discount_usd": 0.09, "attributable_discount_usd": 0.09,
         "warm_pings": 2, "warm_spend_usd": 0.0001, "warm_hits": 1},
        {"provider": "deepseek", "cached_input_tokens": 10,
         "native_cache_discount_usd": 0.01, "attributable_discount_usd": None,
         "warm_pings": None, "warm_spend_usd": None, "warm_hits": None},
    ]
