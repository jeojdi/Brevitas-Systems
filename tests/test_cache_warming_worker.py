import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import api.worker as worker
from api.store import SupabaseUsageStore, UsageStore
from brevitas.proxy import _UPSTREAMS


ORG = "00000000-0000-4000-8000-000000000201"
CUSTOMER = "00000000-0000-4000-8000-000000000202"
PREFIX_HASH = "a" * 64
RECORDED_BY = "kh_warm_service"
CLAIM_TOKEN = "00000000-0000-4000-8000-0000000002aa"


class Lease:
    def __init__(self, allowed):
        self.allowed = allowed
        self.released = 0

    async def release(self):
        self.released += 1


class Limiter:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.acquired = []
        self.lease = None

    async def acquire(self, identity, *, tokens=1, request_id=""):
        self.acquired.append((identity, tokens, request_id))
        self.lease = Lease(self.allowed)
        return self.lease


class Store:
    def __init__(self, rows=None, status="ok"):
        self.status = status
        self.rows = rows or []
        self.claims = []
        self.settles = []
        self.observations = []
        self.decision_outcomes = []
        self.usage_stamps = []
        self.reward_joins = []

    def warm_due_claim(self, claim_limit, **kwargs):
        self.claims.append((claim_limit, dict(kwargs)))
        return {"status": self.status, "rows": [dict(row) for row in self.rows]}

    def warm_ping_settle(self, *args, claim_token=None):
        self.settles.append((*args, claim_token))
        return {"schema": "brevitas.warm-settle.v1", "status": "settled",
                "outcome": args[7]}

    def warm_ttl_observe(self, provider, model_class, ttl_tier, gap_seconds,
                         outcome, source):
        self.observations.append({
            "provider": provider, "model_class": model_class,
            "ttl_tier": ttl_tier, "gap_seconds": gap_seconds,
            "outcome": outcome, "source": source})
        return {"schema": "brevitas.warm-ttl-observation.v1", "status": "recorded"}

    def warm_decision_settle_outcome(self, claim_token, settle_outcome):
        self.decision_outcomes.append((claim_token, settle_outcome))
        return {"schema": "brevitas.warm-decision-outcome.v1",
                "status": "recorded", "updated": 1}

    def warm_usage_stamp_prefix(self, organization_id, key_hash, request_id,
                                prefix_hash):
        self.usage_stamps.append(
            (organization_id, key_hash, request_id, prefix_hash))
        return {"schema": "brevitas.warm-usage-stamp.v1", "status": "recorded",
                "updated": 1}

    def warm_reward_join(self, lookback_hours=48, limit=5000):
        self.reward_joins.append((lookback_hours, limit))
        return {"schema": "brevitas.warm-reward-join.v1", "status": "ok",
                "scanned": 1, "joined": 1, "attributed": 1, "organic": 0,
                "unpriced": 0}


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload
        self.closed = False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class Pool:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload if payload is not None else {
            "usage": {"input_tokens": 3, "cache_creation_input_tokens": 2048,
                      "cache_read_input_tokens": 0, "output_tokens": 1},
        }
        self.calls = []

    def request(self, provider, operation, method, url, *, headers=None, json=None):
        self.calls.append({"provider": provider, "operation": operation,
                           "method": method, "url": url, "headers": headers,
                           "json": json})
        return Response(self.status_code, self.payload)


class UntouchableStore:
    def __getattr__(self, name):
        raise AssertionError("warming must not touch the store when disabled")


def _claim_row(model="claude-sonnet-4-6"):
    envelope = {
        "recorded_by_key_hash": RECORDED_BY,
        "payload": {
            "model": model,
            "tools": [],
            "system": [{"type": "text", "text": "cached system prompt",
                        "cache_control": {"type": "ephemeral"}}],
            "messages_prefix": [],
            "ttl": "",
            "vary": {"anthropic-version": "2023-06-01"},
        },
    }
    return {
        "organization_id": ORG, "customer_id": CUSTOMER, "provider": "anthropic",
        "prefix_hash": PREFIX_HASH, "prefix_tokens": 2048,
        "provider_ttl_seconds": 300,
        # The fake _decrypt below is the identity, so ciphertexts hold plaintext.
        "payload_ciphertext": json.dumps(envelope),
        "credential_ciphertext": "sk-ant-warm-test",
        "reserved_usd": 0.0096, "budget_day": "2026-07-27",
        "claim_token": CLAIM_TOKEN,
    }


def _deepseek_claim_row(model="deepseek-chat", upstream=None):
    envelope = {
        "recorded_by_key_hash": RECORDED_BY,
        "payload": {
            "model": model,
            "tools": [],
            # DeepSeek's cache is automatic: no markers, the system turn rides
            # inside the OpenAI-shaped messages prefix.
            "messages_prefix": [{"role": "system", "content": "cached system prompt"}],
            # extract_warm_prefix always records the exact chat endpoint the
            # observed request used; the worker must refuse any mismatch.
            "upstream": (upstream if upstream is not None
                         else f"{_UPSTREAMS['deepseek']}/v1/chat/completions"),
            "vary": {},
        },
    }
    row = _claim_row()
    row.update({
        "provider": "deepseek",
        "provider_ttl_seconds": 14_400,
        "payload_ciphertext": json.dumps(envelope),
        "credential_ciphertext": "sk-deepseek-warm-test",
    })
    return row


def _fake_decrypt(value, *, context):
    assert context["purpose"] in ("warm_provider_credential", "warm_prefix_payload")
    assert context["organization_id"] == ORG
    if context["purpose"] == "warm_prefix_payload":
        # Pins the cross-component AAD contract with _hosted_warm_observe:
        # exactly purpose + organization_id + customer_id, nothing else.
        assert context["customer_id"] == CUSTOMER
        assert set(context) == {"purpose", "organization_id", "customer_id"}
    return value


def _install_warm_fakes(monkeypatch, *, store, pool, limiter, recorded_usage,
                        key_context=None):
    if key_context is None:
        key_context = {"owner_id": "owner_1", "key_type": "service"}
    monkeypatch.setattr(worker, "_store", store)
    monkeypatch.setattr(worker, "provider_sync_http", pool)
    monkeypatch.setattr(worker, "_distributed_limiter", limiter)
    monkeypatch.setattr(worker, "_decrypt", _fake_decrypt)
    monkeypatch.setattr(worker, "_authoritative_service_key_context",
                        lambda kh: key_context if kh == RECORDED_BY else None)
    monkeypatch.setattr(worker, "_safe_record_usage",
                        lambda **values: recorded_usage.append(values) or True)


async def _run_one_warming_cycle(store):
    stop = asyncio.Event()
    task = asyncio.create_task(worker.warming(stop))
    while not store.claims:
        await asyncio.sleep(0.001)
    stop.set()
    await asyncio.wait_for(task, timeout=5)


def test_warming_disabled_returns_without_touching_store(monkeypatch):
    monkeypatch.delenv("BREVITAS_WARMING", raising=False)
    monkeypatch.setattr(worker, "_store", UntouchableStore())
    asyncio.run(worker.warming(asyncio.Event()))

    monkeypatch.setenv("BREVITAS_WARMING", "0")
    asyncio.run(worker.warming(asyncio.Event()))


def test_lease_unavailable_makes_no_provider_calls(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(status="lease_unavailable")
    pool = Pool()
    _install_warm_fakes(monkeypatch, store=store, pool=pool, limiter=Limiter(),
                        recorded_usage=[])

    asyncio.run(_run_one_warming_cycle(store))

    assert store.claims and store.claims[0][0] == 50
    # Default lease covers a full sequential batch (max(900, limit*30)).
    assert store.claims[0][1]["claim_lease_seconds"] == 1500
    assert pool.calls == []
    assert store.settles == []


def test_due_row_pings_provider_records_spend_and_settles_warmed(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])
    pool = Pool()
    limiter = Limiter()
    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=pool, limiter=limiter,
                        recorded_usage=recorded)

    asyncio.run(_run_one_warming_cycle(store))

    assert len(pool.calls) == 1
    call = pool.calls[0]
    assert (call["provider"], call["operation"]) == ("anthropic", "messages")
    body = call["json"]
    assert body["max_tokens"] == 1
    assert "stream" not in body
    # Any sampling param 400s on current Anthropic models, stopping the prefix.
    assert "temperature" not in body
    assert body["metadata"] == {"user_id": "brevitas-cache-warm"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1] == {
        "role": "user", "content": [{"type": "text", "text": "."}]}
    assert call["headers"]["x-api-key"] == "sk-ant-warm-test"
    assert call["headers"]["anthropic-version"] == "2023-06-01"

    identity, tokens, _ = limiter.acquired[0]
    assert (identity.organization_id, identity.customer_id) == (ORG, CUSTOMER)
    assert (identity.key_id, identity.provider) == (RECORDED_BY, "anthropic")
    assert tokens == 2048
    assert limiter.lease.released == 1

    assert len(recorded) == 1
    usage = recorded[0]
    assert usage["strategy"] == "cache_warm"
    assert usage["receipt_source"] == "worker"
    assert usage["key_hash"] == RECORDED_BY
    assert usage["request_id"].startswith(f"warm:{PREFIX_HASH[:16]}:")
    assert usage["cache_write_tokens"] == 2048
    assert usage["measured_savings_usd"] == 0.0
    assert usage["actual_cost_usd"] == usage["baseline_cost_usd"]
    assert usage["actual_cost_usd"] > 0

    assert len(store.settles) == 1
    settle = store.settles[0]
    assert settle[:4] == (ORG, CUSTOMER, "anthropic", PREFIX_HASH)
    assert settle[4:6] == ("2026-07-27", 0.0096)
    assert settle[6] > 0                    # spent_usd booked from the receipt
    assert settle[7] == "warmed"
    # The claim token must ride into settle so a lapsed lease cannot
    # double-apply the prefix mutation after a re-claim.
    assert settle[8:] == (300, 60, CLAIM_TOKEN)


def test_auth_failure_settles_auth_failed_without_recording_usage(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])
    pool = Pool(status_code=401, payload={})
    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=pool, limiter=Limiter(),
                        recorded_usage=recorded)

    asyncio.run(_run_one_warming_cycle(store))

    assert len(pool.calls) == 1
    assert recorded == []
    settle = store.settles[0]
    assert settle[6] == 0.0
    assert settle[7] == "auth_failed"


def test_limiter_denial_releases_reservation_without_provider_call(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])
    pool = Pool()
    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=pool,
                        limiter=Limiter(allowed=False), recorded_usage=recorded)

    asyncio.run(_run_one_warming_cycle(store))

    assert pool.calls == []
    assert recorded == []
    settle = store.settles[0]
    assert settle[5] == 0.0096              # reservation echoed back
    assert settle[6] == 0.0
    assert settle[7] == "release"


def test_transient_provider_status_releases_reservation(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])
    pool = Pool(status_code=429, payload={})
    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=pool, limiter=Limiter(),
                        recorded_usage=recorded)

    asyncio.run(_run_one_warming_cycle(store))

    assert recorded == []
    assert store.settles[0][7] == "release"


class FailingPool:
    """Provider pool whose request raises, as the reliability pool does when a
    transport failure survives its retry policy."""

    def __init__(self, error):
        self.error = error
        self.calls = []

    def request(self, provider, operation, method, url, *, headers=None, json=None):
        self.calls.append(url)
        raise self.error


def _settle_outcome_for_transport_error(monkeypatch, error):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])
    pool = FailingPool(error)
    limiter = Limiter()
    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=pool, limiter=limiter,
                        recorded_usage=recorded)

    asyncio.run(_run_one_warming_cycle(store))

    assert len(pool.calls) == 1
    # No provider response means no receipt, so nothing is ever recorded as
    # usage; the ledger is the only place this ping can be accounted for.
    assert recorded == []
    assert limiter.lease.released == 1
    settle = store.settles[0]
    # The claim token still fences the prefix mutation on every arm.
    assert settle[8:] == (300, 60, CLAIM_TOKEN)
    return settle


@pytest.mark.parametrize("error", [
    httpx.ReadTimeout("read timed out"),
    httpx.WriteTimeout("write timed out"),
    httpx.ReadError("connection reset"),
    httpx.WriteError("broken pipe"),
    httpx.RemoteProtocolError("server disconnected"),
    httpx.TransportError("unclassified transport failure"),
])
def test_post_send_transport_failure_settles_spent_unknown(monkeypatch, error):
    # These failures can only surface after the request bytes were written, so
    # the provider may already have accepted, cached and billed the ping.
    # Settling 'release' would book $0 of real spend, understating warm spend
    # and inflating the settlement fee ceiling against the org.
    settle = _settle_outcome_for_transport_error(monkeypatch, error)
    assert settle[7] == "spent_unknown"
    # No receipt exists to price it; the store books the reservation itself.
    assert settle[5] == 0.0096
    assert settle[6] == 0.0


@pytest.mark.parametrize("error", [
    httpx.ConnectError("connection refused"),
    httpx.ConnectTimeout("connect timed out"),
    httpx.PoolTimeout("no connection available"),
    httpx.ProxyError("proxy refused"),
    httpx.UnsupportedProtocol("unsupported protocol"),
])
def test_pre_send_transport_failure_releases_the_reservation(monkeypatch, error):
    # Nothing reached the provider, so no spend exists to book and the
    # reservation must come back to the daily budget in full.
    settle = _settle_outcome_for_transport_error(monkeypatch, error)
    assert settle[7] == "release"
    assert settle[6] == 0.0


def test_open_circuit_releases_without_a_provider_call(monkeypatch):
    # ProviderCircuitOpen fires before any request is issued: still a release.
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])
    pool = FailingPool(worker.ProviderCircuitOpen(1.0))
    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=pool, limiter=Limiter(),
                        recorded_usage=recorded)

    asyncio.run(_run_one_warming_cycle(store))

    assert recorded == []
    assert store.settles[0][7] == "release"


def test_every_worker_settle_outcome_is_accepted_by_both_stores(monkeypatch):
    # The RPC whitelist, the SQLite mirror and the worker's vocabulary drift
    # apart silently: a settle the store rejects is logged and dropped, and the
    # reservation stays stuck on the ledger.
    from api.store import _WARM_SETTLE_OUTCOMES
    assert {"warmed", "spent_unknown", "release", "prefix_invalid",
            "auth_failed"} <= _WARM_SETTLE_OUTCOMES
    migration = (Path(__file__).resolve().parents[1] / "supabase" / "migrations"
                 / "202608080001_warm_spent_unknown_settle.sql").read_text()
    for outcome in sorted(_WARM_SETTLE_OUTCOMES):
        assert f"'{outcome}'" in migration
    # The Postgres arm books the reservation, not the caller's spend.
    assert "when p_outcome = 'spent_unknown' then p_reserved_usd" in migration
    assert "if p_outcome in ('warmed', 'spent_unknown') then" in migration


def test_unpriced_model_stops_prefix_before_provider_call(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row(model="claude-unreleased-9")])
    pool = Pool()
    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=pool, limiter=Limiter(),
                        recorded_usage=recorded)

    asyncio.run(_run_one_warming_cycle(store))

    # Unmeterable spend would settle warmed with spent_usd=0 and void the
    # budget ledger; the prefix must stop before any provider dollars move.
    assert pool.calls == []
    assert recorded == []
    settle = store.settles[0]
    assert settle[6] == 0.0
    assert settle[7] == "prefix_invalid"


def test_provider_without_keepalive_spec_stops_prefix_before_provider_call(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    # openai: economics work on gpt-5.6+ but TTL refresh on read is
    # undocumented, so no keep-alive spec exists. groq: 0.50x reads mean a
    # ping costs exactly what a return saves. Both stay measurement-only; a
    # credential must never be replayed against another provider's endpoint.
    for provider in ("openai", "groq"):
        row = _claim_row()
        row["provider"] = provider
        store = Store(rows=[row])
        pool = Pool()
        recorded = []
        _install_warm_fakes(monkeypatch, store=store, pool=pool, limiter=Limiter(),
                            recorded_usage=recorded)

        asyncio.run(_run_one_warming_cycle(store))

        assert pool.calls == []
        assert recorded == []
        assert store.settles[0][7] == "prefix_invalid"


def test_deepseek_due_row_pings_chat_completions_and_settles_warmed(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_deepseek_claim_row()])
    pool = Pool(payload={
        "usage": {"prompt_tokens": 2048, "prompt_cache_hit_tokens": 2048,
                  "prompt_cache_miss_tokens": 0, "completion_tokens": 1},
    })
    limiter = Limiter()
    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=pool, limiter=limiter,
                        recorded_usage=recorded)

    asyncio.run(_run_one_warming_cycle(store))

    assert len(pool.calls) == 1
    call = pool.calls[0]
    assert (call["provider"], call["operation"]) == ("deepseek", "chat.completions")
    # The keep-alive must target the same upstream the proxy routes deepseek to.
    assert call["url"] == f"{_UPSTREAMS['deepseek']}/v1/chat/completions"
    body = call["json"]
    assert body["model"] == "deepseek-chat"
    assert body["max_tokens"] == 1
    assert body["stream"] is False
    assert "temperature" not in body
    assert body["messages"] == [
        {"role": "system", "content": "cached system prompt"},
        {"role": "user", "content": "."}]
    assert call["headers"]["Authorization"] == "Bearer sk-deepseek-warm-test"

    identity, tokens, _ = limiter.acquired[0]
    assert (identity.key_id, identity.provider) == (RECORDED_BY, "deepseek")
    assert tokens == 2048
    assert limiter.lease.released == 1

    assert len(recorded) == 1
    usage = recorded[0]
    assert usage["strategy"] == "cache_warm"
    assert usage["provider"] == "deepseek"
    assert usage["cached_input_tokens"] == 2048
    assert usage["measured_savings_usd"] == 0.0
    assert usage["actual_cost_usd"] == usage["baseline_cost_usd"]
    assert usage["actual_cost_usd"] > 0

    settle = store.settles[0]
    assert settle[:4] == (ORG, CUSTOMER, "deepseek", PREFIX_HASH)
    assert settle[6] > 0                    # spent_usd booked from the receipt
    assert settle[7] == "warmed"
    assert settle[8:] == (14_400, 60, CLAIM_TOKEN)


def test_unpriced_deepseek_model_stops_prefix_before_provider_call(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_deepseek_claim_row(model="deepseek-unreleased-9")])
    pool = Pool()
    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=pool, limiter=Limiter(),
                        recorded_usage=recorded)

    asyncio.run(_run_one_warming_cycle(store))

    # The unpriced gate is per provider: an unmeterable deepseek ping would
    # settle warmed with spent_usd=0, so it must stop before dollars move.
    assert pool.calls == []
    assert recorded == []
    settle = store.settles[0]
    assert settle[6] == 0.0
    assert settle[7] == "prefix_invalid"


def test_claim_kwargs_thread_provider_correct_roi(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(status="lease_unavailable")
    _install_warm_fakes(monkeypatch, store=store, pool=Pool(), limiter=Limiter(),
                        recorded_usage=[])

    asyncio.run(_run_one_warming_cycle(store))

    kwargs = store.claims[0][1]
    # The flat provider -> number map is the only shape the store kwarg and
    # the warm_due_claim RPC's jsonb validator accept; the nested provider_roi
    # shape no store ever accepted must stay dead.
    assert "provider_roi" not in kwargs
    by_provider = kwargs["roi_break_even_by_provider"]
    # The scalar env knob stays the anthropic-calibrated global fallback.
    assert by_provider["anthropic"] == kwargs["roi_break_even_p"]
    # DeepSeek break-even follows its own 0.02x reads (f / (1 - f)); reserve
    # has no per-provider channel — ping_reserve_usd is observer-priced per
    # row and the flat floor only ever over-reserves.
    assert by_provider["deepseek"] == 0.020408
    # Providers without a keep-alive spec never get ROI rows: measurement only.
    assert set(by_provider) == {"anthropic", "deepseek"}


def test_claim_kwargs_bind_to_every_real_store_backend(tmp_path):
    """The worker->store claim seam must be exercised against the REAL
    backends: warming() swallows every cycle exception as warming_cycle_error,
    so a kwarg only the fake **kwargs Store absorbs would TypeError each tick
    and silently kill ALL warming (anthropic included)."""
    kwargs = worker._warm_claim_kwargs()
    sqlite_store = UsageStore(str(tmp_path / "warm-claim-seam.db"))
    # The real SQLite path also runs the bounds validator, so a provider key
    # or break-even value the store rejects fails here, not in production.
    assert sqlite_store.warm_due_claim(50, **kwargs)["status"] == "ok"
    inspect.signature(SupabaseUsageStore.warm_due_claim).bind(None, 50, **kwargs)


def test_deepseek_prefix_observed_on_alternate_upstream_stops_before_ping(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    # x-brevitas-upstream can route provider="deepseek" traffic to another
    # allowlisted host; live requests then never read api.deepseek.com's
    # cache, so a ping there is pure spend against nothing — and the
    # credential must never be replayed against a URL it was not observed
    # with. The prefix stops permanently before any lease or provider call.
    row = _deepseek_claim_row(
        upstream=f"{_UPSTREAMS['together']}/v1/chat/completions")
    store = Store(rows=[row])
    pool = Pool()
    limiter = Limiter()
    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=pool, limiter=limiter,
                        recorded_usage=recorded)

    asyncio.run(_run_one_warming_cycle(store))

    assert pool.calls == []
    assert recorded == []
    assert limiter.acquired == []
    settle = store.settles[0]
    assert settle[6] == 0.0
    assert settle[7] == "prefix_invalid"


def test_revoked_recording_key_stops_prefix_before_provider_call(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])
    pool = Pool()
    _install_warm_fakes(monkeypatch, store=store, pool=pool, limiter=Limiter(),
                        recorded_usage=[], key_context=None)
    monkeypatch.setattr(worker, "_authoritative_service_key_context", lambda kh: None)

    asyncio.run(_run_one_warming_cycle(store))

    assert pool.calls == []
    assert store.settles[0][7] == "prefix_invalid"


def test_unreadable_2xx_body_books_the_reservation_instead_of_zero(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])

    class UnparseableResponse(Response):
        def json(self):
            raise ValueError("proxy returned an HTML error page")

    class UnparseablePool(Pool):
        def request(self, provider, operation, method, url, *, headers=None, json=None):
            super().request(provider, operation, method, url,
                            headers=headers, json=json)
            return UnparseableResponse(200, {})

    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=UnparseablePool(),
                        limiter=Limiter(), recorded_usage=recorded)

    asyncio.run(_run_one_warming_cycle(store))

    settle = store.settles[0]
    # Anthropic charged for the cache write, so the ceiling must see the spend.
    # Booking 0 would release the reservation and free the daily budget.
    assert settle[7] == "warmed"
    assert settle[6] == 0.0096
    assert settle[5] == 0.0096


def test_2xx_without_a_usage_block_books_the_reservation(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])
    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=Pool(payload={"id": "msg_1"}),
                        limiter=Limiter(), recorded_usage=recorded)

    asyncio.run(_run_one_warming_cycle(store))

    settle = store.settles[0]
    assert settle[7] == "warmed"
    assert settle[6] == 0.0096


def test_failure_after_the_ping_still_books_the_spend(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])

    def exploding_record_usage(**_values):
        raise RuntimeError("usage recording is down")

    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=Pool(), limiter=Limiter(),
                        recorded_usage=recorded)
    monkeypatch.setattr(worker, "_safe_record_usage", exploding_record_usage)

    asyncio.run(_run_one_warming_cycle(store))

    settle = store.settles[0]
    # The provider was already paid; the outcome must not degrade to a free
    # 'release' just because bookkeeping after the ping failed.
    assert settle[7] == "warmed"
    assert settle[6] > 0


# --- Phase 0 instrumentation: ping TTL sensor + decision outcome (202608090001) ---


def _claim_row_with_touch(gap_seconds=120.0, **overrides):
    row = _claim_row()
    row["last_touch_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=gap_seconds)).isoformat()
    row.update(overrides)
    return row


@pytest.mark.parametrize("usage,expected", [
    ({"input_tokens": 3, "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 2048, "output_tokens": 1}, "warm"),
    ({"input_tokens": 3, "cache_creation_input_tokens": 2048,
      "cache_read_input_tokens": 0, "output_tokens": 1}, "expired"),
])
def test_ping_receipt_records_the_ttl_observation_it_already_parsed(
        monkeypatch, usage, expected):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row_with_touch(gap_seconds=240.0)])
    _install_warm_fakes(monkeypatch, store=store,
                        pool=Pool(payload={"usage": usage}),
                        limiter=Limiter(), recorded_usage=[])

    asyncio.run(_run_one_warming_cycle(store))

    observation, = store.observations
    assert observation["outcome"] == expected
    assert observation["source"] == "ping"
    assert observation["provider"] == "anthropic"
    # The snapshot suffix is stripped so the aggregate is not split by an axis
    # that carries no physics.
    assert observation["model_class"] == "claude-sonnet-4-6"
    assert observation["ttl_tier"] == "5m"
    assert observation["gap_seconds"] == pytest.approx(240, abs=5)
    # Instrumentation never changes the money path.
    assert store.settles[0][7] == "warmed"


def test_ping_without_cache_legs_records_no_ttl_observation(monkeypatch):
    """A receipt that proves neither a read nor a write is not an observation,
    and an invented one would poison the physics table."""
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row_with_touch()])
    _install_warm_fakes(
        monkeypatch, store=store,
        pool=Pool(payload={"usage": {"input_tokens": 3, "output_tokens": 1,
                                     "cache_creation_input_tokens": 0,
                                     "cache_read_input_tokens": 0}}),
        limiter=Limiter(), recorded_usage=[])

    asyncio.run(_run_one_warming_cycle(store))

    assert store.observations == []
    assert store.settles[0][7] == "warmed"


@pytest.mark.parametrize("last_touch_at", [
    "", "not-a-timestamp",
    # Future touch and a gap past the physics table's own 30-day bound.
    (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(),
])
def test_unusable_touch_clock_records_no_ttl_observation(monkeypatch, last_touch_at):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row_with_touch(last_touch_at=last_touch_at)])
    _install_warm_fakes(monkeypatch, store=store, pool=Pool(), limiter=Limiter(),
                        recorded_usage=[])

    asyncio.run(_run_one_warming_cycle(store))

    assert store.observations == []
    assert store.settles[0][7] == "warmed"


def test_ttl_observation_failure_cannot_break_the_settle(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row_with_touch()])

    def exploding_observe(*_args, **_kwargs):
        raise RuntimeError("observation store is down")

    _install_warm_fakes(monkeypatch, store=store, pool=Pool(), limiter=Limiter(),
                        recorded_usage=[])
    monkeypatch.setattr(store, "warm_ttl_observe", exploding_observe)

    asyncio.run(_run_one_warming_cycle(store))

    settle = store.settles[0]
    assert settle[7] == "warmed"
    assert settle[6] > 0


@pytest.mark.parametrize("status,expected", [
    (200, "warmed"), (401, "auth_failed"), (400, "prefix_invalid"),
    (503, "release"),
])
def test_settle_outcome_is_stamped_back_onto_the_logged_decision(
        monkeypatch, status, expected):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row_with_touch()])
    _install_warm_fakes(monkeypatch, store=store, pool=Pool(status_code=status),
                        limiter=Limiter(), recorded_usage=[])

    asyncio.run(_run_one_warming_cycle(store))

    assert store.settles[0][7] == expected
    assert store.decision_outcomes == [(CLAIM_TOKEN, expected)]


def test_decision_outcome_failure_cannot_break_the_settle(monkeypatch):
    """The decision log is analytics: it may not delay, precede or fail money."""
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row_with_touch()])

    def exploding_outcome(*_args, **_kwargs):
        raise RuntimeError("decision log is down")

    _install_warm_fakes(monkeypatch, store=store, pool=Pool(), limiter=Limiter(),
                        recorded_usage=[])
    monkeypatch.setattr(store, "warm_decision_settle_outcome", exploding_outcome)

    asyncio.run(_run_one_warming_cycle(store))

    assert store.settles[0][7] == "warmed"


def test_model_class_and_ttl_tier_helpers_are_the_shared_vocabulary():
    assert worker.warm_model_class("claude-sonnet-4-5-20260514") == "claude-sonnet-4-5"
    assert worker.warm_model_class("claude-sonnet-4-5-latest") == "claude-sonnet-4-5"
    assert worker.warm_model_class("Claude-Sonnet-4-5") == "claude-sonnet-4-5"
    assert worker.warm_model_class("deepseek-chat") == "deepseek-chat"
    assert worker.warm_model_class(None) == ""
    assert len(worker.warm_model_class("m" * 400)) == 128
    assert worker.warm_ttl_tier(300) == "5m"
    assert worker.warm_ttl_tier(3600) == "1h"
    assert worker.warm_ttl_tier(14_400) == "auto"


def test_instrumentation_store_surface_exists_on_every_backend(tmp_path):
    """The RPC and the SQLite mirror must both answer the calls the worker makes,
    with the same keyword names — that drift is a known incident class here."""
    for backend in (UsageStore(str(tmp_path / "warm-surface.db")),
                    SupabaseUsageStore.__new__(SupabaseUsageStore)):
        for name in ("warm_ttl_observe", "warm_decision_settle_outcome"):
            assert callable(getattr(backend, name))
        assert (list(inspect.signature(
                    type(backend).warm_ttl_observe).parameters)
                == ["self", "provider", "model_class", "ttl_tier", "gap_seconds",
                    "outcome", "source"])
        assert (list(inspect.signature(
                    type(backend).warm_decision_settle_outcome).parameters)
                == ["self", "claim_token", "settle_outcome"])
        assert ("model_class" in inspect.signature(
            type(backend).warm_prefix_observe).parameters)


def test_migration_declares_both_instrumentation_tables_and_wires_compliance():
    """Mirror-drift and compliance-wiring guard: a new warming table that no
    compliance function enumerates is silently exempt from every data right."""
    migration = (Path(__file__).resolve().parents[1] / "supabase" / "migrations"
                 / "202608090001_warm_instrumentation_tables.sql").read_text()
    for table in ("public.warm_decision_log", "public.warm_ttl_observations"):
        assert f"create table if not exists {table}" in migration
    for decision in ("pinged", "skipped_roi", "budget_denied", "cap_denied",
                     "stopped", "holdout"):
        assert f"'{decision}'" in migration
    # Every decision the claim loop can actually reach is written by the claim.
    claim_body = migration.split(
        "create or replace function public.warm_due_claim(")[1].split(
        "$$ language plpgsql")[0]
    for decision in ("skipped_roi", "cap_denied", "budget_denied", "pinged"):
        assert f"v_row.prefix_hash, '{decision}'" in claim_body
    assert claim_body.count("public.warm_decision_record(") == 4
    # Tenant erasure removes the behavioral table and leaves the tenant-free one.
    delete_body = migration.split(
        "create or replace function public.compliance_delete_tenant(")[1].split(
        "\n$function$;")[0]
    assert "delete from public.warm_decision_log entry" in delete_body
    assert "delete from public.warm_ttl_observations" not in delete_body
    # Both exports and the retention class.
    assert migration.count("'record_type', 'warming_decision'") == 2
    assert "v_warm_decision_cutoff timestamptz := clock_timestamp()-interval '90 days'" in migration
    assert "warm_decision_candidates" in migration and "warm_decision_deleted" in migration
    # The aggregate table ages on the warming sweeper, not a compliance class.
    assert "v_observation_retention_days integer := 365" in migration


# --- The per-ping reward join (202608090002) ---------------------------------


def test_warmed_ping_stamps_its_own_receipt_with_the_prefix(monkeypatch):
    """The ping's usage row is what prices the reward; without the stamp the
    join cannot find it and the decision stays unscored."""
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])
    recorded = []
    _install_warm_fakes(monkeypatch, store=store, pool=Pool(), limiter=Limiter(),
                        recorded_usage=recorded)
    asyncio.run(_run_one_warming_cycle(store))

    assert len(store.usage_stamps) == 1
    organization_id, key_hash, request_id, prefix_hash = store.usage_stamps[0]
    assert (organization_id, key_hash, prefix_hash) == (ORG, RECORDED_BY,
                                                        PREFIX_HASH)
    # The stamp addresses exactly the row the worker just wrote.
    assert request_id == recorded[0]["request_id"]
    assert request_id.startswith(f"warm:{PREFIX_HASH[:16]}:")


def test_ping_stamp_failure_cannot_change_the_settle(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])

    def explode(*args, **kwargs):
        raise RuntimeError("stamp down")

    store.warm_usage_stamp_prefix = explode
    _install_warm_fakes(monkeypatch, store=store, pool=Pool(), limiter=Limiter(),
                        recorded_usage=[])
    asyncio.run(_run_one_warming_cycle(store))

    assert store.settles[0][7] == "warmed"
    assert store.settles[0][6] > 0
    assert store.decision_outcomes == [(CLAIM_TOKEN, "warmed")]


def test_unsent_ping_stamps_nothing(monkeypatch):
    """A ping the limiter refused wrote no usage row, so there is nothing to
    stamp and no reward to price."""
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store(rows=[_claim_row()])
    _install_warm_fakes(monkeypatch, store=store, pool=Pool(),
                        limiter=Limiter(allowed=False), recorded_usage=[])
    asyncio.run(_run_one_warming_cycle(store))

    assert store.usage_stamps == []


async def _run_one_reward_join_cycle(store):
    stop = asyncio.Event()
    task = asyncio.create_task(worker.warm_reward_join(stop))
    while not store.reward_joins:
        await asyncio.sleep(0.001)
    stop.set()
    await asyncio.wait_for(task, timeout=5)


def test_reward_join_loop_runs_with_warming_on_and_honors_its_bounds(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setenv("BREVITAS_WARM_REWARD_JOIN_LOOKBACK_HOURS", "12")
    monkeypatch.setenv("BREVITAS_WARM_REWARD_JOIN_LIMIT", "7")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    store = Store()
    monkeypatch.setattr(worker, "_store", store)
    asyncio.run(_run_one_reward_join_cycle(store))

    assert store.reward_joins[0] == (12, 7)


def test_reward_join_loop_is_off_without_warming_and_switchable(monkeypatch):
    """Read-only analytics, so it defaults ON -- but only where warming is on
    at all, and one env var still stops it."""
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    monkeypatch.delenv("BREVITAS_WARMING", raising=False)
    monkeypatch.setattr(worker, "_store", UntouchableStore())
    asyncio.run(worker.warm_reward_join(asyncio.Event()))

    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setenv("BREVITAS_WARM_REWARD_JOIN", "false")
    asyncio.run(worker.warm_reward_join(asyncio.Event()))
    assert worker._warm_reward_join_enabled() is False
    monkeypatch.delenv("BREVITAS_WARM_REWARD_JOIN")
    assert worker._warm_reward_join_enabled() is True


def test_reward_join_loop_survives_a_failing_store(monkeypatch):
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setenv("BREVITAS_WARM_REWARD_JOIN_INTERVAL_SECONDS", "60")
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    calls = []

    class Broken:
        def warm_reward_join(self, lookback_hours=48, limit=5000):
            calls.append(1)
            raise RuntimeError("supabase down")

    monkeypatch.setattr(worker, "_store", Broken())

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.warm_reward_join(stop))
        while not calls:
            await asyncio.sleep(0.001)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())
    assert calls == [1]


def test_reward_join_is_registered_as_a_worker_loop():
    source = (Path(__file__).resolve().parents[1] / "api" / "worker.py").read_text()
    assert 'warm_reward_join(stop), name="worker-warm-reward-join"' in source


def test_both_store_backends_expose_the_same_reward_join_surface():
    for backend in (UsageStore, SupabaseUsageStore):
        assert (list(inspect.signature(
                    backend.warm_usage_stamp_prefix).parameters)
                == ["self", "organization_id", "key_hash", "request_id",
                    "prefix_hash"])
        assert (list(inspect.signature(backend.warm_reward_join).parameters)
                == ["self", "lookback_hours", "limit"])


def test_reward_join_migration_wires_columns_compliance_and_the_horizon():
    """Column-level version of the 202607280016 lesson: a jsonb projection and a
    minimization SET are both enumerated, so a new column nobody names is
    silently exempt from export and from erasure."""
    migration = (Path(__file__).resolve().parents[1] / "supabase" / "migrations"
                 / "202608090002_warm_reward_join.sql").read_text()
    assert ("alter table public.usage_log\n"
            "    add column if not exists warm_prefix_hash text;") in migration
    assert ("add column if not exists organic_counterfactual boolean not null "
            "default false") in migration
    # The join is scored only where a priced receipt exists, and only past the
    # attribution horizon.
    join_body = migration.split(
        "create or replace function public.warm_reward_join(")[1].split(
        "$$ language plpgsql")[0]
    assert "entry.settle_outcome = 'warmed'" in join_body
    assert "entry.realized_net_usd is null" in join_body
    assert "< pg_catalog.now()" in join_body
    assert "usage.cache_attributable" in join_body
    # The counterfactual charges the ping and credits nothing.
    assert "v_net := -v_ping.cost_usd;" in join_body
    # It writes analytics columns and nothing else.
    for forbidden in ("billing_ledger", "verified_savings_usd", "brevitas_fee_usd",
                      "warm_budget_ledger", "period_settlement_ledger"):
        assert forbidden not in join_body
    # Erasure and minimization both clear the hash; both exports name the flag.
    assert migration.count("warm_prefix_hash = null") == 3
    assert migration.count("'organic_counterfactual', entry.organic_counterfactual") == 2
    # Retention's dry-run count and its apply must agree on the predicate.
    assert migration.count("or candidate.warm_prefix_hash is not null") == 2
    # Subject erasure gets the decision-log delete the tenant path already had.
    subject_body = migration.split(
        "create or replace function public.compliance_delete_subject(")[1].split(
        "\n$function$;")[0]
    assert "delete from public.warm_decision_log entry" in subject_body
    assert "warm_ttl_observations" not in subject_body


def test_claim_kwargs_carry_the_holdout_share_off_by_default(monkeypatch):
    monkeypatch.delenv("BREVITAS_WARM_HOLDOUT_PCT", raising=False)
    assert worker._warm_claim_kwargs()["holdout_fraction"] == 0.0
    # A percent on the wire, a fraction in the claim contract.
    monkeypatch.setenv("BREVITAS_WARM_HOLDOUT_PCT", "5")
    assert worker._warm_claim_kwargs()["holdout_fraction"] == pytest.approx(0.05)
    # The worker and api/server.py's warm_status must not parse this knob
    # twice: two parsers is how the advertised share and the applied share
    # drift apart.
    from api.store import warm_holdout_fraction
    assert worker.warm_holdout_fraction is warm_holdout_fraction


def test_holdout_migration_mirrors_the_python_assignment(monkeypatch):
    """Mirror-drift guard for the control arm. The assignment lives in exactly
    two places and a disagreement between them silently splits one (org,
    prefix, day) unit across both arms, which is worse than no arm at all."""
    import hashlib

    from api.store import _WARM_DECISIONS, warm_holdout_bucket

    assert "holdout" in _WARM_DECISIONS
    migration = (Path(__file__).resolve().parents[1] / "supabase" / "migrations"
                 / "202608100001_warm_holdout_arm.sql").read_text()
    # The signature is replaced, never overloaded: two candidates make every
    # existing ten-argument call ambiguous.
    assert "p_holdout_fraction double precision default 0" in migration
    assert ("drop function if exists public.warm_due_claim(\n"
            "    integer, numeric, integer, numeric, numeric, integer, integer, integer,\n"
            "    integer, jsonb\n);") in migration

    body = migration.split("create or replace function public.warm_due_claim(")[1]
    block = body.split("if coalesce(p_holdout_fraction, 0) > 0 then")[1].split(
        "\n        update public.warm_budget_ledger ledger")[0]
    # The same digest, the same four bytes, the same strict comparison in
    # double space that api/store.py:warm_is_held_out makes.
    assert "lower(v_row.organization_id::text)" in block
    assert "|| lower(v_row.prefix_hash)" in block
    assert "|| to_char(v_day, 'YYYY-MM-DD'), 'UTF8'" in block
    for term in ("get_byte(v_holdout_digest, 0)::bigint * 16777216",
                 "get_byte(v_holdout_digest, 1) * 65536",
                 "get_byte(v_holdout_digest, 2) * 256",
                 "get_byte(v_holdout_digest, 3)"):
        assert term in block
    assert ("v_holdout_bucket::double precision\n"
            "               < p_holdout_fraction * 4294967296::double precision") in block
    # The Python side is the same formula, spelled independently here so a
    # rewrite of either has to come back and change this line.
    day = "2026-08-09"
    assert warm_holdout_bucket("ORG-A", "ab" * 32, day) == int.from_bytes(
        hashlib.sha256(("org-a" + "ab" * 32 + day).encode()).digest()[:4], "big")

    # A held-out row costs nothing and moves nothing except the schedule.
    for forbidden in ("warm_budget_ledger", "reserved_usd", "spent_usd",
                      "warm_pings", "consecutive_misses", "pings_today =",
                      "claim_token =", "gen_random_uuid"):
        assert forbidden not in block
    assert "prefix.provider_ttl_seconds - p_safety_margin_seconds" in block
    assert "'holdout', v_p_return, v_floor, v_reserve" in block
    assert "p_holdout_fraction::numeric);" in block

    # The draw is the LAST gate: after ROI, cap and budget, before the
    # reservation. Randomizing earlier fills the control arm with rows the
    # treatment arm would never have warmed.
    assert (body.index("'budget_denied'")
            < body.index("if coalesce(p_holdout_fraction, 0) > 0 then")
            < body.index("set reserved_usd = ledger.reserved_usd + v_reserve"))
    # The treatment arm records its own action probability, and only while the
    # arm is on.
    assert ("case when coalesce(p_holdout_fraction, 0) > 0\n"
            "                then (1 - p_holdout_fraction)::numeric else null end);") in body
