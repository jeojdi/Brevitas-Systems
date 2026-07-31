"""Proxy semantic-cache wiring: a repeated request must short-circuit the upstream.

Runs without the optional embedding dependency — exercises the exact-hash layer,
which is the layer the proxy wiring is responsible for. Upstream is mocked so no
network/keys are needed.
"""
import os
import tempfile

os.environ["BREVITAS_CACHE_DB"] = tempfile.mktemp(suffix=".db")
os.environ["BREVITAS_API_KEY"] = ""  # disable billing HTTP calls (report_usage no-ops)

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import brevitas.proxy as proxy
from brevitas.semantic_cache import SemanticCache


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient that counts upstream POSTs and returns a canned
    OpenAI-shaped response."""
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.calls += 1
        # A COMPLETE response — finish_reason "stop". The cache now refuses to store
        # truncated responses, so the fake must look like a naturally-finished one.
        return _FakeResp({
            "id": "chatcmpl-1",
            "choices": [{"message": {"role": "assistant", "content": "42"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 3},
        })


def test_repeated_request_hits_cache(monkeypatch):
    monkeypatch.setenv("BREVITAS_CACHE_ENABLED", "true")
    monkeypatch.setenv("BREVITAS_CACHE_LOCAL", "true")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    # force a fresh cache singleton bound to the temp db
    proxy._cache_init_done = False
    proxy._cache_singleton = None
    _FakeAsyncClient.calls = 0

    client = TestClient(proxy.proxy_app)
    req = {
        "model": "deepseek-chat",
        "temperature": 0,
        "messages": [{"role": "user", "content": "what is 6 times 7"}],
    }

    r1 = client.post("/v1/chat/completions", json=req,
                     headers={"authorization": "test-auth-a"})
    assert r1.status_code == 200
    assert r1.json()["choices"][0]["message"]["content"] == "42"
    assert _FakeAsyncClient.calls == 1, "first call must reach upstream"

    # identical request → exact-hash hit → upstream NOT called again
    r2 = client.post("/v1/chat/completions", json=req,
                     headers={"authorization": "test-auth-a"})
    assert r2.status_code == 200
    assert r2.json()["choices"][0]["message"]["content"] == "42"
    assert _FakeAsyncClient.calls == 1, "repeated call must be served from cache"

    # A hosted proxy may serve many customers. Identical content under a different
    # credential must never reuse the first customer's response.
    r3 = client.post("/v1/chat/completions", json=req,
                     headers={"authorization": "test-auth-other-tenant"})
    assert r3.status_code == 200
    assert _FakeAsyncClient.calls == 2, "cache entries must be tenant-isolated"

    common = {"authorization": "shared-provider", "x-brevitas-key": "shared-service"}
    client.post("/v1/chat/completions", json=req,
                headers={**common, "x-brevitas-customer-id": "customer-a"})
    client.post("/v1/chat/completions", json=req,
                headers={**common, "x-brevitas-customer-id": "customer-b"})
    assert _FakeAsyncClient.calls == 4, "shared service keys must isolate end customers"


def test_high_temperature_not_cached(monkeypatch):
    monkeypatch.setenv("BREVITAS_CACHE_ENABLED", "true")
    monkeypatch.setenv("BREVITAS_CACHE_LOCAL", "true")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    proxy._cache_init_done = False
    proxy._cache_singleton = None
    _FakeAsyncClient.calls = 0

    client = TestClient(proxy.proxy_app)
    req = {
        "model": "deepseek-chat",
        "temperature": 0.9,  # intentional randomness — must never be cached
        "messages": [{"role": "user", "content": "tell me a joke"}],
    }
    client.post("/v1/chat/completions", json=req, headers={"authorization": "test-auth-b"})
    client.post("/v1/chat/completions", json=req, headers={"authorization": "test-auth-b"})
    assert _FakeAsyncClient.calls == 2, "high-temp calls must both reach upstream"


def test_cache_key_includes_every_response_control(tmp_path):
    cache = SemanticCache(str(tmp_path / "cache.db"), semantic_enabled=False)
    base = {
        "model": "gpt-4o-mini", "temperature": 0, "seed": 1,
        "messages": [{"role": "user", "content": "answer"}],
    }
    cache.store(base, "openai", "gpt-4o-mini", {"answer": "a"},
                prompt_tokens=1, completion_tokens=1)
    assert cache.lookup(base, "openai", "gpt-4o-mini") is not None
    assert cache.lookup({**base, "seed": 2}, "openai", "gpt-4o-mini") is None
    assert cache.lookup({**base, "stop": ["END"]}, "openai", "gpt-4o-mini") is None
    assert cache.lookup({**base, "response_format": {"type": "json_object"}},
                        "openai", "gpt-4o-mini") is None


def test_cache_refuses_conversations_that_do_not_end_in_a_user_turn(tmp_path):
    cache = SemanticCache(str(tmp_path / "assistant-tail.db"), semantic_enabled=True)
    assistant_tail = {
        "model": "gpt-4o-mini", "temperature": 0,
        "messages": [
            {"role": "user", "content": "prepare the account answer"},
            {"role": "assistant", "content": "account A is overdue"},
        ],
    }
    cache.store(assistant_tail, "openai", "gpt-4o-mini", {"answer": "A"},
                prompt_tokens=10, completion_tokens=2)

    assert cache.cacheable(assistant_tail) is False
    assert cache.lookup(assistant_tail, "openai", "gpt-4o-mini") is None


def test_hosted_cache_varies_by_provider_credential(monkeypatch):
    monkeypatch.setenv("BREVITAS_CACHE_ENABLED", "true")
    monkeypatch.setenv("BREVITAS_CACHE_LOCAL", "true")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    proxy._cache_init_done = False
    proxy._cache_singleton = None
    _FakeAsyncClient.calls = 0
    client = TestClient(proxy.proxy_app)
    req = {"model": "gpt-4o-mini", "temperature": 0,
           "messages": [{"role": "user", "content": "same account request"}]}
    common = {"X-Brevitas-Key": "bvt_customer"}
    # NB: bare token strings (no "Bearer " prefix) — the proxy namespaces the cache on the
    # raw Authorization value, so distinct values suffice, and this avoids tripping the
    # pre-push secret scanner's "Bearer <token>" pattern on these fake test creds.
    assert client.post("/v1/chat/completions", json=req,
                       headers={**common, "Authorization": "provider-a"}).status_code == 200
    assert client.post("/v1/chat/completions", json=req,
                       headers={**common, "Authorization": "provider-b"}).status_code == 200
    assert _FakeAsyncClient.calls == 2


class _IncompleteClient(_FakeAsyncClient):
    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.calls += 1
        return _FakeResp({                       # truncated: finish_reason "length"
            "id": "chatcmpl-x",
            "choices": [{"message": {"role": "assistant", "content": "partial..."},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 3},
        })


def _fresh_client(monkeypatch, fake=_FakeAsyncClient):
    monkeypatch.setenv("BREVITAS_CACHE_ENABLED", "true")
    monkeypatch.setenv("BREVITAS_CACHE_LOCAL", "true")
    monkeypatch.setattr(httpx, "AsyncClient", fake)
    proxy._cache_init_done = False
    proxy._cache_singleton = None
    _FakeAsyncClient.calls = 0
    return TestClient(proxy.proxy_app)


def test_retrieval_answer_is_not_cached(monkeypatch):
    """The audit's P0 reproduction: an answer produced from a NON-faithful (retrieval/
    reorder) request must never be stored, so a second identical request re-hits upstream
    instead of replaying the degraded answer as a verified cache hit."""
    client = _fresh_client(monkeypatch)

    real_optimize = proxy.optimize_request

    def _prune(body, provider, router, session_id, **kw):
        meta = real_optimize(body, provider, router, session_id, **kw)
        meta["response_faithful"] = False        # simulate retrieval having dropped context
        return meta
    monkeypatch.setattr(proxy, "optimize_request", _prune)

    req = {"model": "gpt-4o-mini", "temperature": 0,
           "messages": [{"role": "user", "content": "summarize the doc"}]}
    client.post("/v1/chat/completions", json=req, headers={"authorization": "auth-p0"})
    client.post("/v1/chat/completions", json=req, headers={"authorization": "auth-p0"})
    assert _FakeAsyncClient.calls == 2, "non-faithful answer must not be cached"


def test_incomplete_response_not_cached(monkeypatch):
    client = _fresh_client(monkeypatch, _IncompleteClient)
    req = {"model": "gpt-4o-mini", "temperature": 0,
           "messages": [{"role": "user", "content": "long answer please"}]}
    client.post("/v1/chat/completions", json=req, headers={"authorization": "auth-inc"})
    client.post("/v1/chat/completions", json=req, headers={"authorization": "auth-inc"})
    assert _FakeAsyncClient.calls == 2, "truncated response must not be cached"


def test_tripped_cache_lever_skips_hits(monkeypatch):
    """Tripping the exact-cache lever must stop the proxy from serving cached hits."""
    from token_efficiency_model.quality import gate
    client = _fresh_client(monkeypatch)
    gate.trip_lever("cache")
    try:
        req = {"model": "gpt-4o-mini", "temperature": 0,
               "messages": [{"role": "user", "content": "same question twice"}]}
        client.post("/v1/chat/completions", json=req, headers={"authorization": "auth-lv"})
        client.post("/v1/chat/completions", json=req, headers={"authorization": "auth-lv"})
        assert _FakeAsyncClient.calls == 2, "tripped cache lever must not serve hits"
    finally:
        gate.reset_lever("cache")


def test_cache_is_off_by_default(monkeypatch):
    monkeypatch.delenv("BREVITAS_CACHE_ENABLED", raising=False)
    monkeypatch.delenv("BREVITAS_CACHE_LOCAL", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    proxy._cache_init_done = False
    proxy._cache_singleton = None
    _FakeAsyncClient.calls = 0
    client = TestClient(proxy.proxy_app)
    req = {"model": "gpt-4o-mini", "temperature": 0,
           "messages": [{"role": "user", "content": "do not retain this"}]}

    client.post("/v1/chat/completions", json=req,
                headers={"authorization": "test-auth-default-off"})
    client.post("/v1/chat/completions", json=req,
                 headers={"authorization": "test-auth-default-off"})
    assert _FakeAsyncClient.calls == 2


# ── The forward link, cache side ──────────────────────────────────────────────
# A replay's savings are zero-spend BY CONSTRUCTION, so the only thing that
# separates one from a fabricated row is a provable paid ancestor. The cache
# entry is the ONLY object that knows which paid request filled it: the proxy at
# replay time never touched an upstream and the API sees a receipt, not a cache.
# So `origin_request_id` is where the link is actually kept, and everything
# downstream (CacheHit -> _report_cache_hit -> usage_log.savings_anchor_request_id)
# is carriage. Empty is always legal and always means UNANCHORED, which is the
# fail-closed value: excluded from the fee basis, billed at zero.


def _anchor_body(text="what did we pay for"):
    return {"model": "gpt-4o-mini", "temperature": 0,
            "messages": [{"role": "user", "content": text}]}


def test_cache_entry_remembers_the_metering_id_that_paid_for_it(tmp_path):
    """The paid call's receipt id survives the round trip and rides the hit."""
    cache = SemanticCache(str(tmp_path / "anchored.db"), semantic_enabled=False)
    body = _anchor_body()
    cache.store(body, "openai", "gpt-4o-mini", {"answer": "42"},
                prompt_tokens=600, completion_tokens=50,
                origin_request_id="proxy:chatcmpl-paid-ancestor")

    hit = cache.lookup(body, "openai", "gpt-4o-mini")
    assert hit is not None and hit.kind == "exact"
    assert hit.origin_request_id == "proxy:chatcmpl-paid-ancestor"

    # A store that names no ancestor produces an UNANCHORED entry rather than
    # inheriting one, and an unanchored replay bills zero.
    other = _anchor_body("nobody paid for this")
    cache.store(other, "openai", "gpt-4o-mini", {"answer": "?"},
                prompt_tokens=1, completion_tokens=1)
    assert cache.lookup(other, "openai", "gpt-4o-mini").origin_request_id == ""


def test_cache_entries_written_before_the_anchor_column_still_serve_hits(tmp_path):
    """An older cache file must keep serving — unanchored, never broken.

    Adding a money-bearing column must not be able to take the cache offline:
    the loss on a pre-anchor entry is revenue attribution (it replays outside the
    fee basis), and that is strictly cheaper than a cache miss, which is a real
    upstream call the customer pays for.
    """
    import sqlite3

    key = Fernet.generate_key()
    path = str(tmp_path / "legacy.db")
    body = _anchor_body("written before the column existed")
    original = SemanticCache(path, semantic_enabled=False, encryption_key=key)
    original.store(body, "openai", "gpt-4o-mini", {"answer": "42"},
                   prompt_tokens=600, completion_tokens=50,
                   origin_request_id="proxy:chatcmpl-paid-ancestor")

    # Rewind the schema to the shape a database written before the anchor had.
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE semantic_cache DROP COLUMN origin_request_id")

    reopened = SemanticCache(path, semantic_enabled=False, encryption_key=key)
    columns = {r[1] for r in sqlite3.connect(path).execute(
        "PRAGMA table_info(semantic_cache)")}
    assert "origin_request_id" in columns, "init must add the column in place"

    hit = reopened.lookup(body, "openai", "gpt-4o-mini")
    assert hit is not None, "a pre-anchor entry must still serve its hit"
    assert hit.origin_request_id == ""


def test_cache_store_survives_a_cache_object_that_predates_the_keyword():
    """A foreign/older cache implementation degrades to unanchored, not to a lost write.

    _cache_store passes origin_request_id optimistically; a `store` that does not
    accept it raises TypeError, and dropping the write there would cost a real
    upstream call on every future repeat of that request.
    """
    complete = {"id": "chatcmpl-1",
                "choices": [{"message": {"content": "42"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 600, "completion_tokens": 50}}

    class _OldCache:
        def __init__(self):
            self.stored = []

        def store(self, body, provider, model, response, *,
                  prompt_tokens, completion_tokens):
            self.stored.append((prompt_tokens, completion_tokens))

    class _NewCache(_OldCache):
        def store(self, body, provider, model, response, *,
                  prompt_tokens, completion_tokens, origin_request_id=""):
            self.stored.append(origin_request_id)

    old = _OldCache()
    proxy._cache_store(old, _anchor_body(), "openai", "gpt-4o-mini", complete,
                       "proxy:chatcmpl-paid-ancestor")
    assert old.stored == [(600, 50)], "the write must survive, merely unanchored"

    new = _NewCache()
    proxy._cache_store(new, _anchor_body(), "openai", "gpt-4o-mini", complete,
                       "proxy:chatcmpl-paid-ancestor")
    assert new.stored == ["proxy:chatcmpl-paid-ancestor"]

    # A truncated response is still not cached, anchor or no anchor.
    truncated = {**complete,
                 "choices": [{"message": {"content": "4"}, "finish_reason": "length"}]}
    fresh = _NewCache()
    proxy._cache_store(fresh, _anchor_body(), "openai", "gpt-4o-mini", truncated,
                       "proxy:chatcmpl-paid-ancestor")
    assert fresh.stored == []


class _Result:
    def __init__(self, data):
        self.data = data


class _Deferred:
    def __init__(self, run):
        self._run = run

    def execute(self):
        return self._run()


class _FakeTable:
    def __init__(self, db):
        self._db = db
        self._columns: list[str] = []
        self._filters: dict[str, object] = {}
        self._delete = False

    def select(self, columns):
        self._db.selected.append(columns)
        self._columns = [c.strip() for c in columns.split(",")]
        return self

    def eq(self, column, value):
        self._filters[column] = value
        return self

    def gt(self, *_a):
        return self

    def lte(self, *_a):
        return self

    def limit(self, *_a):
        return self

    def delete(self):
        self._delete = True
        return self

    def execute(self):
        if self._delete:
            return _Result([])
        if "origin_request_id" in self._columns and not self._db.anchor_deployed:
            # PostgREST's own shape for a column that is not in the schema cache.
            raise RuntimeError("PGRST204: could not find the 'origin_request_id' column")
        rows = [r for r in self._db.rows.values()
                if all(r.get(k) == v for k, v in self._filters.items())]
        return _Result([{c: r.get(c, "") for c in self._columns} for r in rows])


class _FakePostgrest:
    """In-memory stand-in for the hosted cache's PostgREST surface.

    `anchor_deployed=False` reproduces the deploy-ordering state this code has to
    survive: api/ ships on push while supabase/migrations are hand-applied, so
    there is a real window in which neither the column nor the 11-argument
    `semantic_cache_store_bounded` overload exists yet.
    """

    def __init__(self, anchor_deployed=True):
        self.anchor_deployed = anchor_deployed
        self.rows: dict[str, dict] = {}
        self.rpc_calls: list[tuple[str, dict]] = []
        self.selected: list[str] = []

    def rpc(self, name, args):
        self.rpc_calls.append((name, dict(args)))
        return _Deferred(lambda: self._rpc(name, args))

    def _rpc(self, name, args):
        assert name == "semantic_cache_store_bounded", name
        if "p_origin_request_id" in args and not self.anchor_deployed:
            raise RuntimeError("PGRST202: could not find the function")
        self.rows[args["p_exact_hash"]] = {
            "exact_hash": args["p_exact_hash"],
            "tenant_namespace": args["p_tenant_namespace"],
            "model_id": args["p_model_id"],
            "response_ciphertext": args["p_response_ciphertext"],
            "prompt_tokens": args["p_prompt_tokens"],
            "completion_tokens": args["p_completion_tokens"],
            "origin_request_id": str(args.get("p_origin_request_id") or "")[:128],
        }
        return _Result([])

    def table(self, _name):
        return _FakeTable(self)


def _hosted_cache(monkeypatch, fake):
    import supabase

    from brevitas.semantic_cache import SupabaseSemanticCache

    monkeypatch.setattr(supabase, "create_client", lambda _url, _key: fake)
    return SupabaseSemanticCache(
        "https://example.supabase.co", "service-role-not-a-real-key",
        encryption_key=Fernet.generate_key(), jitter_source=lambda _lo, _hi: 0,
    )


def test_hosted_cache_carries_the_paid_ancestor_through_the_rpc(monkeypatch):
    """Hosted is the path that bills, so the link has to survive PostgREST."""
    fake = _FakePostgrest(anchor_deployed=True)
    cache = _hosted_cache(monkeypatch, fake)
    body = _anchor_body("hosted anchor")

    cache.store(body, "openai", "gpt-4o-mini", {"answer": "42"},
                prompt_tokens=600, completion_tokens=50,
                origin_request_id="proxy:chatcmpl-paid-ancestor")
    assert fake.rpc_calls[0][1]["p_origin_request_id"] == "proxy:chatcmpl-paid-ancestor"

    hit = cache.lookup(body, "openai", "gpt-4o-mini")
    assert hit is not None and hit.kind == "exact"
    assert hit.origin_request_id == "proxy:chatcmpl-paid-ancestor"
    assert cache._anchor_supported is True


def test_hosted_cache_degrades_to_unanchored_when_the_migration_is_absent(monkeypatch):
    """Un-migrated database: still caches, still serves hits, bills nothing.

    Every degradation here points the same way — an entry with no anchor, a
    replay with no anchor, and a fee basis that excludes it. What must NOT happen
    is the alternative: a hard 400 escaping into the caller's except clause,
    where it becomes a permanent cache MISS (a real upstream call on every
    request) or a lost cache write.
    """
    fake = _FakePostgrest(anchor_deployed=False)
    cache = _hosted_cache(monkeypatch, fake)
    body = _anchor_body("hosted without the migration")

    cache.store(body, "openai", "gpt-4o-mini", {"answer": "42"},
                prompt_tokens=600, completion_tokens=50,
                origin_request_id="proxy:chatcmpl-paid-ancestor")
    # Anchored overload attempted first, then the 10-argument fallback. The RPC
    # is an idempotent upsert, so the retry cannot double-write.
    assert [("p_origin_request_id" in args) for _n, args in fake.rpc_calls] == [True, False]
    assert cache._anchor_supported is False, "the latch is process-lifetime by design"
    assert fake.rows, "the entry must still be written"

    # Latched off: no re-probe on the next write.
    fake.rpc_calls.clear()
    cache.store(_anchor_body("second write"), "openai", "gpt-4o-mini", {"answer": "43"},
                prompt_tokens=1, completion_tokens=1,
                origin_request_id="proxy:chatcmpl-paid-ancestor")
    assert len(fake.rpc_calls) == 1
    assert "p_origin_request_id" not in fake.rpc_calls[0][1]

    hit = cache.lookup(body, "openai", "gpt-4o-mini")
    assert hit is not None, "an un-migrated database must still serve cache hits"
    assert hit.origin_request_id == ""


def test_hosted_exact_read_retries_without_the_anchor_column(monkeypatch):
    """A select naming an absent column is a 400 — and a 400 here is a cache outage.

    The read is retried once without the column and the probe is remembered, so
    the cost of an un-migrated database is one failed select per process, not one
    per request and not the cache itself.
    """
    fake = _FakePostgrest(anchor_deployed=True)
    cache = _hosted_cache(monkeypatch, fake)
    body = _anchor_body("read path")
    cache.store(body, "openai", "gpt-4o-mini", {"answer": "42"},
                prompt_tokens=600, completion_tokens=50,
                origin_request_id="proxy:chatcmpl-paid-ancestor")

    fake.anchor_deployed = False        # the column disappears from under the read
    fake.selected.clear()
    hit = cache.lookup(body, "openai", "gpt-4o-mini")
    assert hit is not None and hit.origin_request_id == ""
    assert ["origin_request_id" in c for c in fake.selected] == [True, False]

    fake.selected.clear()
    assert cache.lookup(body, "openai", "gpt-4o-mini") is not None
    assert ["origin_request_id" in c for c in fake.selected] == [False]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
