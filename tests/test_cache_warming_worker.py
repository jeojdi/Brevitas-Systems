import asyncio
import inspect
import json

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

    def warm_due_claim(self, claim_limit, **kwargs):
        self.claims.append((claim_limit, dict(kwargs)))
        return {"status": self.status, "rows": [dict(row) for row in self.rows]}

    def warm_ping_settle(self, *args, claim_token=None):
        self.settles.append((*args, claim_token))
        return {"schema": "brevitas.warm-settle.v1", "status": "settled",
                "outcome": args[7]}


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
