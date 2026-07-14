import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import UsageStore


def test_legacy_sqlite_store_accepts_unverified_usage(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as db:
        db.execute("""create table usage_log (
            id integer primary key autoincrement,
            key_hash text not null,
            ts text not null,
            baseline_tokens integer not null,
            optimized_tokens integer not null,
            savings_pct real not null,
            quality_proxy real not null
        )""")

    store = UsageStore(str(db_path))
    store.create_key("legacy-key", "legacy")
    assert store.record_usage(
        key_hash="legacy-key", baseline_tokens=10, optimized_tokens=8,
        quality_proxy=None,
    )
    assert store.get_stats("legacy-key")["total_calls"] == 1
    with sqlite3.connect(db_path) as db:
        quality = next(row for row in db.execute("pragma table_info(usage_log)")
                       if row[1] == "quality_proxy")
    assert quality[3] == 0


def _server_client(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "usage.db"))
    raw_key = "bvt_contract_test"
    store.create_key(hash_key(raw_key), "test", owner_id="owner-1")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    return server, store, raw_key, TestClient(server.app)


def test_saved_provider_powers_compress_and_stream(tmp_path, monkeypatch):
    server, store, raw_key, client = _server_client(tmp_path, monkeypatch)
    seen = []

    def backend(config):
        seen.append(config)
        return lambda prompt, model: f"{model}: {prompt}"

    monkeypatch.setattr(server, "_build_backend", backend)
    headers = {"X-Brevitas-Key": raw_key}
    assert client.get("/v1/provider", headers=headers).json()["configured"] is False
    saved = client.put("/v1/provider", headers=headers, json={
        "provider": "deepseek", "provider_api_key": "provider-secret",
        "model": "deepseek-chat",
    })
    assert saved.status_code == 200
    assert client.get("/v1/provider", headers=headers).json()["configured"] is True

    body = {"task": "ping", "messages": ["hello"], "prior_context": [],
            "lossy": False}
    response = client.post("/v1/compress", headers=headers, json=body)
    assert response.status_code == 200
    data = response.json()
    assert (data["provider"], data["model"], data["routed_model_hint"]) == (
        "deepseek", "deepseek-chat", "deepseek-chat")
    assert data["model_response"] == "deepseek-chat: Task: ping\n\nhello"
    assert seen[0]["provider_api_key"] != "provider-secret"

    stream = client.post("/v1/compress/stream", headers=headers, json={**body, "meter": False})
    events = [json.loads(line[6:]) for line in stream.text.splitlines()
              if line.startswith("data: ")]
    assert [event["stage"] for event in events] == [
        "retrieving", "routed", "compressed", "model_response", "done"]
    assert events[-1]["result"]["model_response"].endswith("hello")

    rows = store._rows(hash_key(raw_key))
    assert len(rows) == 1
    assert rows[0]["owner_id"] == "owner-1"


def test_provider_validation_and_failure_are_not_metered(tmp_path, monkeypatch):
    server, store, raw_key, client = _server_client(tmp_path, monkeypatch)
    headers = {"X-Brevitas-Key": raw_key}
    assert client.put("/v1/provider", headers=headers, json={
        "provider": "deepseek", "provider_api_key": "secret", "model": "not-a-model",
    }).status_code == 400
    assert client.put("/v1/provider", headers=headers, json={
        "provider": "azure_openai", "provider_api_key": "secret", "model": "anything",
    }).status_code == 400
    assert client.put("/v1/provider", headers=headers, json={
        "provider": "deepseek", "provider_api_key": "secret", "model": "deepseek-chat",
    }).status_code == 200

    monkeypatch.setattr(server._requests, "post", lambda *args, **kwargs: (
        _ for _ in ()).throw(httpx.ConnectError("offline")))
    body = {"task": "ping", "messages": ["hello"], "prior_context": [], "lossy": False}
    failed = client.post("/v1/compress", headers=headers, json=body)
    assert failed.status_code == 502
    assert failed.json() == {"detail": "Model provider request failed"}
    assert store.get_stats(hash_key(raw_key))["total_calls"] == 0

    stream = client.post("/v1/compress/stream", headers=headers, json=body)
    events = [json.loads(line[6:]) for line in stream.text.splitlines()
              if line.startswith("data: ")]
    assert events[-1]["stage"] == "error"
    assert not any(event["stage"] == "done" for event in events)
    assert store.get_stats(hash_key(raw_key))["total_calls"] == 0


def test_compress_rejects_non_string_messages(tmp_path, monkeypatch):
    _, _, raw_key, client = _server_client(tmp_path, monkeypatch)
    response = client.post("/v1/compress", headers={"X-Brevitas-Key": raw_key},
                           json={"messages": [123], "prior_context": []})
    assert response.status_code == 422


@pytest.mark.parametrize("path", [
    "/v1/chat/completions", "/v1/responses", "/v1/embeddings", "/v1/messages",
])
def test_proxy_rejects_malformed_json(path):
    import brevitas.proxy as proxy

    response = TestClient(proxy.proxy_app).post(
        path, content=b"{bad", headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Request body must be valid JSON"


def test_failed_proxy_upstream_is_not_metered(monkeypatch):
    import brevitas.proxy as proxy

    events = []
    real = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda *args, **kwargs: real(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            429, json={"error": "rate limited"}))))
    proxy._cache_init_done = True
    proxy._cache_singleton = None
    proxy.set_usage_reporter(lambda key, payload: events.append(payload))
    response = TestClient(proxy.proxy_app).post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider", "X-Brevitas-Key": "bvt"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    proxy.set_usage_reporter(None)
    assert response.status_code == 429
    assert events == []
