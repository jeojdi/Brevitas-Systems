import sqlite3
import threading
import asyncio
import hashlib
import json
import sys
import types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from brevitas.resource_bounds import (
    BoundedTTLMap,
    ResourceBounds,
    ResourceConfigurationError,
    ResourceLimitExceeded,
    safe_close_resource,
)
from brevitas.semantic_cache import SemanticCache, make_semantic_cache
from brevitas.session import BrevitasSession
from brevitas.chat import read_document


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


def test_environment_bounds_are_positive_and_absolutely_clamped():
    bounds = ResourceBounds.from_env({
        "BREVITAS_CACHE_TTL_SECONDS": "0",
        "BREVITAS_CACHE_MAX_ENTRIES": "-3",
        "BREVITAS_CACHE_CANDIDATE_LIMIT": "0",
        "BREVITAS_JOB_PAYLOAD_TTL_SECONDS": "999999999",
        "BREVITAS_REDIS_STREAM_MAX_ENTRIES": "0",
    })
    assert bounds.semantic_cache_ttl_s == 1
    assert bounds.semantic_cache_max_entries == 1
    assert bounds.semantic_cache_candidate_limit == 1
    assert bounds.job_payload_ttl_s == 24 * 60 * 60
    assert bounds.redis_stream_max_entries == 1

    with pytest.raises(ResourceConfigurationError):
        ResourceBounds.from_env({"BREVITAS_CACHE_TTL_SECONDS": "forever"})


def test_bounded_ttl_map_has_deterministic_expiry_lru_and_byte_eviction():
    clock = Clock()
    values = BoundedTTLMap(
        ttl_s=10,
        max_entries=2,
        max_value_bytes=5,
        max_total_bytes=6,
        clock=clock,
        sizer=lambda value: len(value),
    )
    values.put("a", "aaa")
    values.put("b", "bb")
    assert values.get("a") == "aaa"  # a is now most recently used
    values.put("c", "ccc")
    assert values.get("b") is None
    assert list(values.items()) == [("a", "aaa"), ("c", "ccc")]

    clock.value = 10
    assert len(values) == 0
    assert values.total_bytes == 0


def test_bounded_ttl_map_is_thread_safe_at_capacity():
    values = BoundedTTLMap(
        ttl_s=60,
        max_entries=8,
        max_value_bytes=32,
        max_total_bytes=128,
        sizer=lambda value: len(value),
    )
    barrier = threading.Barrier(9)
    failures = []

    def writer(index):
        try:
            barrier.wait()
            for turn in range(100):
                values.put(f"{index}:{turn}", "x" * 16)
                values.get(f"{index}:{turn}")
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(values) <= 8
    assert values.total_bytes <= 128


def test_bounded_ttl_map_constructs_each_shared_value_once_under_concurrency():
    values = BoundedTTLMap(
        ttl_s=60,
        max_entries=2,
        max_value_bytes=32,
        sizer=lambda value: len(value),
    )
    count = 0
    count_lock = threading.Lock()
    barrier = threading.Barrier(9)
    results = []

    def factory():
        nonlocal count
        with count_lock:
            count += 1
        return "router"

    def reader():
        barrier.wait()
        results.append(values.get_or_create("shared", factory))

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert results == ["router"] * 8
    assert count == 1


def test_bounded_map_checks_size_before_retaining_value():
    values = BoundedTTLMap(
        ttl_s=60,
        max_entries=2,
        max_value_bytes=4,
        sizer=lambda value: len(value),
    )
    with pytest.raises(ResourceLimitExceeded):
        values.put("large", "12345")
    assert len(values) == 0


def test_bounded_map_mutation_is_copy_on_write_and_rolls_back_rejection():
    values = BoundedTTLMap(
        ttl_s=60, max_entries=2, max_value_bytes=5,
        sizer=lambda value: len(value["text"]),
    )
    values.put("session", {"text": "ok"})

    with pytest.raises(ResourceLimitExceeded):
        values.mutate(
            "session", lambda candidate: candidate.update(text="too-large")
        )

    assert values.get("session") == {"text": "ok"}
    assert values.total_bytes == 2


def test_bounded_map_owns_values_and_never_returns_live_mutable_aliases():
    values = BoundedTTLMap(
        ttl_s=60, max_entries=4, max_value_bytes=1024,
    )
    original = {"items": ["stored"]}
    values.put("put", original)
    accounted = values.total_bytes
    original["items"].append("alias-after-put")
    assert values.get("put") == {"items": ["stored"]}
    assert values.total_bytes == accounted

    from_get = values.get("put")
    from_get["items"].append("alias-after-get")
    assert values.get("put") == {"items": ["stored"]}
    assert values.total_bytes == accounted

    factory_value = {"items": ["factory"]}
    from_create = values.get_or_create("factory", lambda: factory_value)
    create_accounted = values.total_bytes
    factory_value["items"].append("factory-source-alias")
    from_create["items"].append("factory-return-alias")
    assert values.get_or_create("factory", lambda: {"wrong": True}) == {
        "items": ["factory"]
    }
    assert values.total_bytes == create_accounted

    from_mutate = values.mutate(
        "put", lambda candidate: candidate["items"].append("committed")
    )
    mutate_accounted = values.total_bytes
    from_mutate["items"].append("mutate-return-alias")
    assert values.get("put") == {"items": ["stored", "committed"]}
    assert values.total_bytes == mutate_accounted

    snapshot_items = dict(values.items())
    items_accounted = values.total_bytes
    snapshot_items["put"]["items"].append("items-alias")
    assert dict(values.items())["put"] == {"items": ["stored", "committed"]}
    assert values.total_bytes == items_accounted


def test_bounded_map_supports_content_aware_session_copier():
    class SharedClient:
        def __deepcopy__(self, _memo):
            raise AssertionError("service handle must not be deep-copied")

    client = SharedClient()

    def copy_session(value):
        return {"history": list(value["history"]), "client": value["client"]}

    values = BoundedTTLMap(
        ttl_s=60, max_entries=2, max_value_bytes=100,
        sizer=lambda value: len(value["history"]),
        copier=copy_session, snapshotter=copy_session,
    )
    source = {"history": ["one"], "client": client}
    values.put("session", source)
    source["history"].append("source-alias")
    snapshot = values.get("session")
    snapshot["history"].append("snapshot-alias")
    assert values.get("session")["history"] == ["one"]
    assert values.get("session")["client"] is client


class CloseOwner:
    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    def close(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("secret close detail")


def _owner_map(*, clock=lambda: 0, max_entries=2, on_remove=None):
    def copy_value(value):
        return {"owner": value["owner"], "items": list(value.get("items", []))}

    return BoundedTTLMap(
        ttl_s=5, max_entries=max_entries, max_value_bytes=100,
        clock=clock, sizer=lambda value: len(value.get("items", [])) + 1,
        copier=copy_value, snapshotter=copy_value,
        resource_key=lambda value: value["owner"],
        on_remove=on_remove or (lambda value: safe_close_resource(value["owner"])),
    )


def test_removal_callback_covers_expiry_replacement_capacity_discard_and_clear():
    clock = Clock()
    values = _owner_map(clock=clock, max_entries=2)
    expired, replaced, capacity, explicit = (CloseOwner() for _ in range(4))

    values.put("expired", {"owner": expired})
    clock.value = 5
    assert values.cleanup() == 1
    assert expired.calls == 1

    values.put("replace", {"owner": replaced})
    values.put("replace", {"owner": capacity})
    assert replaced.calls == 1
    values.put("other", {"owner": explicit})
    overflow = CloseOwner()
    values.put("overflow", {"owner": overflow})
    assert capacity.calls == 1  # oldest entry evicted by count
    assert values.discard("other") is True
    assert explicit.calls == 1
    values.clear()
    assert overflow.calls == 1
    values.clear()
    assert [expired.calls, replaced.calls, capacity.calls,
            explicit.calls, overflow.calls] == [1, 1, 1, 1, 1]


def test_shared_owner_is_not_closed_while_replacement_remains_retained():
    values = _owner_map()
    owner = CloseOwner()
    values.put("session", {"owner": owner, "items": ["one"]})
    snapshot = values.get("session")
    values.mutate("session", lambda value: value["items"].append("two"))
    values.put("session", {"owner": owner, "items": ["three"]})
    assert owner.calls == 0
    assert snapshot["owner"] is owner
    values.clear()
    assert owner.calls == 1


def test_aggregate_byte_eviction_finalizes_oldest_owner():
    def copy_value(value):
        return {"owner": value["owner"], "payload": value["payload"]}

    first, second = CloseOwner(), CloseOwner()
    values = BoundedTTLMap(
        ttl_s=60, max_entries=4, max_value_bytes=4, max_total_bytes=4,
        sizer=lambda value: len(value["payload"]), copier=copy_value,
        snapshotter=copy_value, resource_key=lambda value: value["owner"],
        on_remove=lambda value: safe_close_resource(value["owner"]),
    )
    values.put("first", {"owner": first, "payload": "aaa"})
    values.put("second", {"owner": second, "payload": "bb"})
    assert first.calls == 1
    assert second.calls == 0
    assert values.total_bytes == 2
    values.clear()
    assert second.calls == 1


def test_concurrent_clear_and_duplicate_owner_finalize_exactly_once():
    values = _owner_map(max_entries=4)
    owner = CloseOwner()
    values.put("one", {"owner": owner})
    values.put("two", {"owner": owner})
    barrier = threading.Barrier(9)

    def clear():
        barrier.wait()
        values.clear()

    threads = [threading.Thread(target=clear) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert owner.calls == 1


def test_close_failures_are_suppressed_and_async_finalizers_are_settled():
    failed = CloseOwner(fail=True)
    values = _owner_map()
    values.put("failed", {"owner": failed})
    values.clear()  # exception text and error never escape
    assert failed.calls == 1

    class AsyncOwner:
        def __init__(self):
            self.calls = 0

        async def aclose(self):
            await asyncio.sleep(0)
            self.calls += 1

    async_owner = AsyncOwner()

    async def callback(value):
        task = asyncio.create_task(value["owner"].aclose())
        await task

    async_values = _owner_map(on_remove=callback)
    async_values.put("async", {"owner": async_owner})

    async def clear_inside_running_loop():
        async_values.clear()

    asyncio.run(clear_inside_running_loop())
    assert async_owner.calls == 1
    assert not any(
        thread.name == "brevitas-resource-finalizer" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_chat_demo_closes_client_on_success_and_failure(tmp_path, monkeypatch):
    import brevitas
    from brevitas.chat import run_demo

    class FakeResponse:
        choices = [types.SimpleNamespace(
            message=types.SimpleNamespace(content="answer")
        )]

    class FakeSavings:
        uncached_cost = 1.0
        actual_cost = 0.5
        cached_tokens = 2
        savings_pct = 50.0

    class FakeClient:
        instances = []
        fail = False

        def __init__(self, **_kwargs):
            self.close_calls = 0
            self.instances.append(self)

        def chat(self, **_kwargs):
            if self.fail:
                raise RuntimeError("provider failed")
            return FakeResponse(), FakeSavings()

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(brevitas, "BrevitasClient", FakeClient)
    path = tmp_path / "doc.txt"
    path.write_text("document")
    assert run_demo(str(path), ["question"], api_key="test", printer=lambda *_: None)[
        "turns"
    ] == 1
    assert FakeClient.instances[-1].close_calls == 1

    FakeClient.fail = True
    with pytest.raises(RuntimeError, match="provider failed"):
        run_demo(str(path), ["question"], api_key="test", printer=lambda *_: None)
    assert FakeClient.instances[-1].close_calls == 1


def test_agent_demo_generator_close_releases_client(monkeypatch):
    import brevitas
    import brevitas.demos as demos

    class FakeClient:
        instances = []

        def __init__(self, **_kwargs):
            self.close_calls = 0
            self.instances.append(self)

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(brevitas, "BrevitasClient", FakeClient)
    monkeypatch.setattr(demos, "_AGENT_TURNS", [])
    records = demos.run_agent_session("openai", "test", "model")
    assert next(records) == {"done": True}
    records.close()
    assert FakeClient.instances[-1].close_calls == 1


def test_webchat_registry_explicit_delete_and_shutdown_clear_close_owners():
    from brevitas.webchat import Session, _SESSIONS

    _SESSIONS.clear()
    explicit = CloseOwner()
    shutdown = CloseOwner()
    _SESSIONS.put("explicit", Session(doc="one", client=explicit))
    assert _SESSIONS.discard("explicit") is True
    assert explicit.calls == 1
    _SESSIONS.put("shutdown", Session(doc="two", client=shutdown))
    # FastAPI shutdown and atexit hooks call this same idempotent clear path.
    _SESSIONS.clear()
    assert shutdown.calls == 1
    _SESSIONS.clear()


def test_demo_apps_register_explicit_delete_and_shutdown_hooks():
    root = Path(__file__).parents[1] / "brevitas"
    contracts = {
        "webchat.py": "_SESSIONS.clear",
        "compare.py": "_CMP.clear",
        "demos.py": "_DOCS.clear",
    }
    for filename, clear in contracts.items():
        source = (root / filename).read_text()
        assert f"app.router.on_shutdown.append({clear})" in source
        assert '@app.delete("/api/session")' in source
        assert f"atexit.register({clear})" in source


def test_bounded_map_serializes_concurrent_same_session_mutations():
    values = BoundedTTLMap(
        ttl_s=60, max_entries=2, max_value_bytes=8,
        sizer=lambda _value: 1,
    )
    values.put("session", {"count": 0})
    barrier = threading.Barrier(9)

    def increment():
        barrier.wait()
        for _ in range(200):
            values.mutate(
                "session",
                lambda candidate: candidate.update(count=candidate["count"] + 1),
            )

    threads = [threading.Thread(target=increment) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert values.get("session") == {"count": 1600}


def test_session_expires_and_evicts_prior_content_by_bytes_and_items():
    clock = Clock()
    session = BrevitasSession(
        "session",
        prior_ttl_s=5,
        max_prior_items=2,
        max_prior_bytes=6,
        max_prior_item_bytes=4,
        clock=clock,
    )
    session.record_response("aaa")
    session.record_response("bb")
    session.record_response("ccc")
    assert session.prior_context() == ["bb", "ccc"]

    with pytest.raises(ResourceLimitExceeded):
        session.record_response("12345")
    assert session.prior_context() == ["bb", "ccc"]

    clock.value = 5
    assert session.prior_context() == []


def test_document_reader_never_reads_past_configured_bound(tmp_path):
    path = tmp_path / "large.txt"
    path.write_bytes(b"x" * 11)
    with pytest.raises(ResourceLimitExceeded):
        read_document(str(path), max_bytes=10)


def _cache_body(value="question"):
    return {
        "model": "m",
        "temperature": 0,
        "messages": [{"role": "user", "content": value}],
    }


def test_semantic_cache_ttl_entry_count_and_response_bytes_are_bounded(tmp_path):
    clock = Clock(100)
    cache = SemanticCache(
        str(tmp_path / "bounded.db"),
        encryption_key=Fernet.generate_key(),
        default_ttl_s=999_999,
        max_entries=2,
        max_entry_bytes=1024,
        clock=clock,
        jitter_source=lambda _low, _high: 0,
    )
    assert cache.default_ttl_s == 24 * 60 * 60

    for number in range(3):
        cache.store(
            _cache_body(str(number)), "openai", "m", {"answer": str(number)},
            prompt_tokens=1, completion_tokens=1,
        )
        clock.value += 1

    with sqlite3.connect(cache.db_path) as db:
        rows = db.execute(
            "select created_at, expires_at from semantic_cache order by created_at"
        ).fetchall()
    assert len(rows) == 2
    assert all(0 < expires - created <= 24 * 60 * 60 for created, expires in rows)
    assert cache.lookup(_cache_body("0"), "openai", "m") is None

    cache.store(
        _cache_body("large"), "openai", "m", {"answer": "x" * 2048},
        prompt_tokens=1, completion_tokens=1,
    )
    assert cache.lookup(_cache_body("large"), "openai", "m") is None


def test_semantic_cache_checks_size_before_encryption(tmp_path):
    cache = SemanticCache(
        str(tmp_path / "preflight.db"),
        encryption_key=Fernet.generate_key(),
        max_entry_bytes=1024,
    )

    class MustNotEncrypt:
        def encrypt(self, _value):
            raise AssertionError("oversized content reached encryption")

    cache._cipher = MustNotEncrypt()
    cache.store(
        _cache_body(), "openai", "m", {"answer": "x" * 2048},
        prompt_tokens=1, completion_tokens=1,
    )


class ContextEnvelope:
    """Test adapter that authenticates plaintext against canonicalized context."""
    def __init__(self):
        self.encrypt_calls = 0
        self.plaintexts = []
        self.contexts = []

    @staticmethod
    def _digest(context):
        return hashlib.sha256(
            json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def encrypt_text(self, plaintext, *, context):
        self.encrypt_calls += 1
        self.plaintexts.append(plaintext)
        self.contexts.append(dict(context))
        return f"envelope:{self._digest(context)}:{plaintext[::-1]}"

    def decrypt_text(self, ciphertext, *, context):
        prefix, digest, payload = ciphertext.split(":", 2)
        if prefix != "envelope" or digest != self._digest(context):
            raise ValueError("context mismatch")
        return payload[::-1]


def test_semantic_cache_accepts_managed_envelope_cipher_interface(tmp_path):
    envelope = ContextEnvelope()
    cache = SemanticCache(
        str(tmp_path / "envelope.db"), encryption_cipher=envelope,
        jitter_source=lambda _low, _high: 0,
    )
    cache.store(
        _cache_body(), "openai", "m", {"answer": "protected"},
        prompt_tokens=1, completion_tokens=1,
    )
    assert cache.lookup(_cache_body(), "openai", "m").response == {
        "answer": "protected"
    }
    assert set(envelope.contexts[0]) == {
        "purpose", "tenant_namespace", "exact_hash", "model_identity"
    }
    assert envelope.contexts[0]["model_identity"] == "openai:m"


def test_cache_envelope_rejects_cross_row_and_cross_tenant_ciphertext_swaps(tmp_path):
    path = tmp_path / "swap.db"
    cache = SemanticCache(
        str(path), encryption_cipher=ContextEnvelope(),
        jitter_source=lambda _low, _high: 0,
    )
    bodies = [
        {**_cache_body("one"), "_brevitas_cache_namespace": "tenant-a"},
        {**_cache_body("two"), "_brevitas_cache_namespace": "tenant-a"},
        {**_cache_body("one"), "_brevitas_cache_namespace": "tenant-b"},
    ]
    for number, request in enumerate(bodies):
        cache.store(request, "openai", "m", {"answer": number},
                    prompt_tokens=1, completion_tokens=1)

    exact = [cache._hash(cache._exact_parts(request, "openai", "m", include_last=True))
             for request in bodies]
    with sqlite3.connect(path) as db:
        ciphertext = [db.execute(
            "select response_ciphertext from semantic_cache where exact_hash=?", (key,)
        ).fetchone()[0] for key in exact]
        # Same-tenant cross-row swap.
        db.execute("update semantic_cache set response_ciphertext=? where exact_hash=?",
                   (ciphertext[1], exact[0]))
        # Cross-tenant swap.
        db.execute("update semantic_cache set response_ciphertext=? where exact_hash=?",
                   (ciphertext[2], exact[1]))

    assert cache.lookup(bodies[0], "openai", "m") is None
    assert cache.lookup(bodies[1], "openai", "m") is None
    assert cache.lookup(bodies[2], "openai", "m").response == {"answer": 2}


def test_production_hosted_factory_default_cipher_rejects_swapped_rows(monkeypatch):
    class Result:
        def __init__(self, data=None):
            self.data = data or []

    class Query:
        def __init__(self, client):
            self.client = client
            self.mode = "select"
            self.filters = {}

        def select(self, _columns):
            self.mode = "select"
            return self

        def delete(self):
            self.mode = "delete"
            return self

        def eq(self, key, value):
            self.filters[key] = value
            return self

        def gt(self, _key, _value):
            return self

        def lte(self, _key, _value):
            return self

        def limit(self, _value):
            return self

        def execute(self):
            if self.mode == "delete":
                return Result()
            exact = self.filters.get("exact_hash")
            row = self.client.rows.get(exact)
            if not row or any(row.get(key) != value for key, value in self.filters.items()):
                return Result()
            return Result([{
                "response_ciphertext": row["response_ciphertext"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
            }])

    class Rpc:
        def __init__(self, client, name, payload):
            self.client, self.name, self.payload = client, name, payload

        def execute(self):
            if self.name == "semantic_cache_store_bounded":
                payload = self.payload
                self.client.rows[payload["p_exact_hash"]] = {
                    "exact_hash": payload["p_exact_hash"],
                    "model_id": payload["p_model_id"],
                    "tenant_namespace": payload["p_tenant_namespace"],
                    "response_ciphertext": payload["p_response_ciphertext"],
                    "prompt_tokens": payload["p_prompt_tokens"],
                    "completion_tokens": payload["p_completion_tokens"],
                }
            return Result()

    class Client:
        def __init__(self):
            self.rows = {}

        def table(self, _name):
            return Query(self)

        def rpc(self, name, payload):
            return Rpc(self, name, payload)

    client = Client()
    monkeypatch.setitem(
        sys.modules, "supabase",
        types.SimpleNamespace(create_client=lambda _url, _key: client),
    )
    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.setenv("BREVITAS_CACHE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setenv("BREVITAS_CACHE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    cache = make_semantic_cache()
    requests = [
        {**_cache_body("same"), "_brevitas_cache_namespace": "tenant-a"},
        {**_cache_body("same"), "_brevitas_cache_namespace": "tenant-b"},
    ]
    for number, request in enumerate(requests):
        cache.store(request, "openai", "m", {"answer": number},
                    prompt_tokens=1, completion_tokens=1)
    assert len(client.rows) == 2
    first, second = list(client.rows.values())
    first["response_ciphertext"], second["response_ciphertext"] = (
        second["response_ciphertext"], first["response_ciphertext"]
    )
    assert cache.lookup(requests[0], "openai", "m") is None
    assert cache.lookup(requests[1], "openai", "m") is None


def test_cache_sizes_the_single_canonical_unicode_serialization_before_encrypt(tmp_path):
    envelope = ContextEnvelope()
    cache = SemanticCache(
        str(tmp_path / "canonical.db"), encryption_cipher=envelope,
        max_entry_bytes=1024, jitter_source=lambda _low, _high: 0,
    )
    response = {"z": "line\nquote\"", "a": "é" * 20}
    expected = json.dumps(
        response, separators=(",", ":"), sort_keys=True,
        ensure_ascii=False, allow_nan=False, default=str,
    )
    cache.store(_cache_body(), "openai", "m", response,
                prompt_tokens=1, completion_tokens=1)
    assert envelope.encrypt_calls == 1
    assert envelope.plaintexts == [expected]

    oversized = {"answer": "é" * 506}
    assert len(cache._canonical_response(oversized)) > 1024
    cache.store(_cache_body("oversized"), "openai", "m", oversized,
                prompt_tokens=1, completion_tokens=1)
    assert envelope.encrypt_calls == 1


def test_cache_serializes_non_json_value_once_before_sizing_and_encryption(tmp_path):
    class Counted:
        calls = 0

        def __str__(self):
            self.calls += 1
            return "canonical"

    value = Counted()
    envelope = ContextEnvelope()
    cache = SemanticCache(
        str(tmp_path / "serialize-once.db"), encryption_cipher=envelope,
        jitter_source=lambda _low, _high: 0,
    )
    cache.store(_cache_body(), "openai", "m", {"answer": value},
                prompt_tokens=1, completion_tokens=1)
    assert value.calls == 1
    assert envelope.plaintexts == ['{"answer":"canonical"}']


def test_sqlite_semantic_scan_never_reads_beyond_candidate_cap(tmp_path, monkeypatch):
    import numpy as np
    from brevitas import _embed

    monkeypatch.setenv("BREVITAS_SEMANTIC_CACHE", "1")
    clock = Clock(1)
    vectors = {"old-match": np.array([1.0, 0.0], dtype="float32"),
               "target": np.array([1.0, 0.0], dtype="float32")}
    for number in range(1, 5):
        vectors[f"new-{number}"] = np.array([0.0, 1.0], dtype="float32")
    monkeypatch.setattr(_embed, "embed", lambda text: vectors[text])
    cache = SemanticCache(
        str(tmp_path / "candidates.db"), encryption_key=Fernet.generate_key(),
        semantic_enabled=True, similarity_threshold=0.99, candidate_limit=2,
        max_entries=10, clock=clock, jitter_source=lambda _low, _high: 0,
    )
    for text in ["old-match", "new-1", "new-2", "new-3", "new-4"]:
        cache.store(_cache_body(text), "openai", "m", {"answer": text},
                    prompt_tokens=1, completion_tokens=1)
        clock.value += 1

    assert cache.lookup(_cache_body("target"), "openai", "m") is None
    cache.candidate_limit = 10
    assert cache.lookup(_cache_body("target"), "openai", "m").response == {
        "answer": "old-match"
    }


def test_hosted_sql_serializes_purge_upsert_and_eviction_contract():
    sql = (Path(__file__).parents[1]
           / "supabase/migrations/202607170002_cache_security.sql").read_text()
    store = sql.split("create or replace function public.semantic_cache_store_bounded", 1)[1]
    store = store.split("revoke all on function public.semantic_cache_store_bounded", 1)[0]
    assert store.index("pg_advisory_xact_lock") < store.index(
        "v_now := clock_timestamp()"
    ) < store.index(
        "delete from public.semantic_cache where expires_at <= v_now"
    ) < store.index("insert into public.semantic_cache") < store.rindex(
        "delete from public.semantic_cache"
    )
    assert "p_created_at" not in store
    assert "p_expires_at" not in store
    assert "least(86400, greatest(1, coalesce(p_ttl_seconds, 3600)))" in store
    trigger = sql.split(
        "create or replace function public.enforce_semantic_cache_absolute_bound", 1
    )[1].split("drop trigger if exists", 1)[0]
    assert trigger.index("pg_advisory_xact_lock") < trigger.index(
        "delete from public.semantic_cache where expires_at <= now()"
    ) < trigger.rindex("delete from public.semantic_cache")
    assert "on public.semantic_cache (created_at desc, exact_hash desc)" in sql
    assert "revoke insert, update on table public.semantic_cache from service_role" in sql
    assert "check (response_json is null)" in sql
    normalize = sql.split(
        "create or replace function public.normalize_semantic_cache_write", 1
    )[1].split("drop trigger if exists semantic_cache_normalize_write", 1)[0]
    assert "v_now timestamptz := clock_timestamp()" in normalize
    assert "least(86400, greatest(1, coalesce(" in normalize
    assert "if tg_op = 'INSERT' or new.created_at > v_now" in normalize
    assert "new.created_at := v_now" in normalize
    assert "new.response_json := null" in normalize
    assert store.count("security definer") == 1


def test_hosted_serialized_transaction_model_is_deterministic_under_concurrency():
    """Model the SQL lock scope; W9 runs the same assertion against real Postgres."""
    cap = 3
    now = 100
    rows = {"expired": {"created": 999, "expires": 99}}
    transaction_lock = threading.Lock()
    barrier = threading.Barrier(21)

    def store(number):
        barrier.wait()
        with transaction_lock:  # pg_advisory_xact_lock scope
            for key in [key for key, row in rows.items() if row["expires"] <= now]:
                rows.pop(key)
            rows[f"row-{number:02}"] = {"created": number, "expires": 200}
            survivors = sorted(
                rows, key=lambda key: (rows[key]["created"], key), reverse=True
            )[:cap]
            for key in list(rows):
                if key not in survivors:
                    rows.pop(key)

    threads = [threading.Thread(target=store, args=(number,)) for number in range(20)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert list(sorted(rows, reverse=True)) == ["row-19", "row-18", "row-17"]
