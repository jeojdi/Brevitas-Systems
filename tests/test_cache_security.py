import sqlite3
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

import brevitas.observability as observability
from brevitas.semantic_cache import SemanticCache, make_semantic_cache


def body(namespace="org_1:customer_1"):
    return {
        "model": "model",
        "temperature": 0,
        "messages": [{"role": "user", "content": "SENTINEL-PRIVATE-PROMPT"}],
        "_brevitas_cache_namespace": namespace,
    }


def test_cached_content_is_encrypted_and_round_trips(tmp_path):
    path = tmp_path / "cache.db"
    cache = SemanticCache(str(path), encryption_key=Fernet.generate_key())
    response = {"answer": "SENTINEL-PRIVATE-RESPONSE"}
    cache.store(body(), "openai", "model", response, prompt_tokens=4, completion_tokens=2)

    raw = path.read_bytes()
    assert b"SENTINEL-PRIVATE-PROMPT" not in raw
    assert b"SENTINEL-PRIVATE-RESPONSE" not in raw
    assert cache.lookup(body(), "openai", "model").response == response


def test_namespace_purge_physically_deletes_customer_content(tmp_path):
    path = tmp_path / "cache.db"
    cache = SemanticCache(str(path), encryption_key=Fernet.generate_key())
    cache.store(body("org_1:customer_1"), "openai", "model", {"answer": "one"},
                prompt_tokens=1, completion_tokens=1)
    cache.store(body("org_1:customer_2"), "openai", "model", {"answer": "two"},
                prompt_tokens=1, completion_tokens=1)

    assert cache.purge_namespace("org_1:customer_1") == 1
    assert cache.lookup(body("org_1:customer_1"), "openai", "model") is None
    assert cache.lookup(body("org_1:customer_2"), "openai", "model").response == {"answer": "two"}


def test_expired_rows_are_physically_removed(tmp_path):
    path = tmp_path / "cache.db"
    now = [10.0]
    cache = SemanticCache(
        str(path), encryption_key=Fernet.generate_key(), default_ttl_s=1,
        clock=lambda: now[0], jitter_source=lambda _low, _high: 0,
    )
    cache.store(body(), "openai", "model", {"answer": "expired"},
                prompt_tokens=1, completion_tokens=1)
    now[0] = 11.0
    cache.purge_expired(force=True)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from semantic_cache").fetchone()[0] == 0


def test_cache_telemetry_uses_only_fixed_content_free_labels(tmp_path, monkeypatch):
    events = []

    class Metrics:
        def record_cache(self, **values):
            events.append(values)

    monkeypatch.setattr(
        observability, "get_runtime",
        lambda **_kwargs: SimpleNamespace(metrics=Metrics()),
    )
    path = tmp_path / "telemetry.db"
    cache = SemanticCache(
        str(path), encryption_key=Fernet.generate_key(), max_entries=1,
        jitter_source=lambda _low, _high: 0,
    )
    first = body("SECRET-TENANT")
    second = {
        **body("SECRET-TENANT"),
        "messages": [{"role": "user", "content": "SECRET-SECOND-PROMPT"}],
    }

    assert cache.lookup(first, "openai", "SECRET-MODEL") is None
    cache.store(first, "openai", "SECRET-MODEL", {"answer": "SECRET-RESPONSE"},
                prompt_tokens=1, completion_tokens=1)
    assert cache.lookup(first, "openai", "SECRET-MODEL") is not None
    cache.store(second, "openai", "SECRET-MODEL", {"answer": "second"},
                prompt_tokens=1, completion_tokens=1)
    cache.store({**second, "stream": True}, "openai", "SECRET-MODEL", {"answer": "no"},
                prompt_tokens=1, completion_tokens=1)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE semantic_cache SET response_ciphertext='invalid'")
    assert cache.lookup(second, "openai", "SECRET-MODEL") is None

    outcomes = {event["outcome"] for event in events}
    assert {"miss", "write", "hit", "evicted", "disabled", "error"} <= outcomes
    assert all(set(event) == {"cache", "outcome"} for event in events)
    assert all(event["cache"] == "semantic" for event in events)
    assert all(event["outcome"] in {
        "disabled", "error", "evicted", "hit", "miss", "write",
    } for event in events)
    serialized = repr(events)
    assert "SECRET-TENANT" not in serialized
    assert "SECRET-MODEL" not in serialized
    assert "SECRET-SECOND-PROMPT" not in serialized
    assert "SECRET-RESPONSE" not in serialized


def test_cache_telemetry_failure_does_not_change_cache_behavior(tmp_path, monkeypatch):
    cache = SemanticCache(
        str(tmp_path / "fail-open.db"), encryption_key=Fernet.generate_key(),
        jitter_source=lambda _low, _high: 0,
    )

    def unavailable(**_kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(observability, "get_runtime", unavailable)
    cache.store(body(), "openai", "model", {"answer": "ok"},
                prompt_tokens=1, completion_tokens=1)
    assert cache.lookup(body(), "openai", "model").response == {"answer": "ok"}


def test_production_never_falls_back_to_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv("BREVITAS_ENV", "production")
    monkeypatch.setenv("BREVITAS_CACHE_BACKEND", "supabase")
    monkeypatch.setenv("BREVITAS_CACHE_DB", str(tmp_path / "should-not-exist.db"))
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("BREVITAS_CACHE_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="incomplete"):
        make_semantic_cache()
    assert not (tmp_path / "should-not-exist.db").exists()


def test_cache_policy_requires_admin_and_purges_on_disable(tmp_path, monkeypatch):
    import api.server as server
    from api.store import UsageStore
    from fastapi.testclient import TestClient

    store = UsageStore(str(tmp_path / "policy.db"))
    organization = store.ensure_organization("admin", "Company")
    customer = store.upsert_customer(organization["id"], "customer-1")
    cache = SemanticCache(str(tmp_path / "managed-cache.db"), encryption_key=Fernet.generate_key())
    namespace = f"{organization['id']}:{customer['id']}"
    cache.store(body(namespace), "openai", "model", {"answer": "private"},
                prompt_tokens=1, completion_tokens=1)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "make_semantic_cache", lambda: cache)
    monkeypatch.setattr(server, "_dashboard_user", lambda request:
                        "admin" if request.headers.get("authorization") == "Bearer session" else "")
    client = TestClient(server.app)
    payload = {"enabled": True, "customer_external_id": "customer-1"}

    assert client.put("/v1/cache-policy", json=payload).status_code == 401
    assert client.put("/v1/cache-policy", headers={"Authorization": "Bearer session"},
                      json=payload).status_code == 200
    assert store.cache_enabled(organization["id"], customer["id"]) is True
    disabled = client.put("/v1/cache-policy", headers={"Authorization": "Bearer session"},
                          json={**payload, "enabled": False})
    assert disabled.status_code == 200
    assert store.cache_enabled(organization["id"], customer["id"]) is False
    assert cache.lookup(body(namespace), "openai", "model") is None

    class BrokenPurge:
        def purge_namespace(self, _namespace, *, strict=False):
            assert strict is True
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(server, "make_semantic_cache", lambda: BrokenPurge())
    failed = client.put("/v1/cache-policy", headers={"Authorization": "Bearer session"},
                        json={**payload, "enabled": False})
    assert failed.status_code == 503
    assert failed.json() == {"detail": "Cache purge unavailable"}
