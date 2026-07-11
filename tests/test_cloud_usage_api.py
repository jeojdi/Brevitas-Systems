import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import UsageStore


def test_usage_api_is_tenant_scoped_and_idempotent(tmp_path, monkeypatch):
    import api.server as server
    store = UsageStore(str(tmp_path / "api.db"))
    store.create_key(hash_key("bvt_test"), "test", owner_id="user-1")
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()
    body = {
        "provider": "openai", "model": "gpt-4o-mini", "operation": "responses",
        "baseline_tokens": 100, "compressed_tokens": 80,
        "fresh_input_tokens": 60, "cached_input_tokens": 20, "output_tokens": 10,
        "quality_score": .95, "request_id": "same", "project": "backend-app",
        "environment": "prod", "source": "worker", "client": "python-sdk",
        "call_site_id": "call_abc", "receipt_source": "sdk",
        "usage_raw": {"prompt": "must never be stored", "response": "also private"},
    }
    client = TestClient(server.app)
    headers = {"X-Brevitas-Key": "bvt_test"}
    first = client.post("/v1/usage", headers=headers, json=body)
    second = client.post("/v1/usage", headers=headers, json=body)
    assert first.status_code == 200
    assert first.json()["quality_status"] == "verified"
    assert second.json()["duplicate"] is True
    overview = client.get("/v1/stats", headers=headers).json()
    breakdown = client.get("/v1/stats/breakdown", headers=headers).json()["rows"]
    assert overview["total_calls"] == sum(row["calls"] for row in breakdown) == 1
    assert breakdown[0]["project"] == "backend-app"
    assert breakdown[0]["source"] == "worker"
    assert "must never be stored" not in repr(store._rows(hash_key("bvt_test")))
    store.create_key(hash_key("bvt_other"), "other", owner_id="user-2")
    store.record_usage(hash_key("bvt_other"), 50, 40, project="other-app", source="api")
    assert client.get("/v1/stats", headers=headers).json()["total_calls"] == 1
    monkeypatch.setenv("BREVITAS_ADMIN_TOKEN", "admin-secret")
    assert client.get("/v1/admin/stats").status_code == 403
    admin = client.get("/v1/admin/stats", headers={"X-Brevitas-Admin": "admin-secret"})
    assert admin.status_code == 200
    assert admin.json()["total_calls"] == 2


def _mock_client(monkeypatch, handler):
    import brevitas.proxy as proxy
    real = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient",
                        lambda *args, **kwargs: real(transport=httpx.MockTransport(handler)))
    proxy._cache_init_done = True
    proxy._cache_singleton = None
    return proxy


def test_streaming_chat_and_responses_are_byte_preserving_and_metered(monkeypatch):
    events = []
    forwarded_responses = []
    chat_bytes = (b'data: {"id":"chat_1","choices":[],"usage":{"prompt_tokens":30,'
                  b'"prompt_tokens_details":{"cached_tokens":10},"completion_tokens":5}}\n\n'
                  b'data: [DONE]\n\n')
    responses_bytes = (b'data: {"type":"response.completed","response":{"id":"resp_1",'
                       b'"usage":{"input_tokens":40,"input_tokens_details":{"cached_tokens":15},'
                       b'"output_tokens":6}}}\n\ndata: [DONE]\n\n')

    def handler(request):
        if request.url.path.endswith("/responses"):
            forwarded_responses.append(request.content)
        content = responses_bytes if request.url.path.endswith("/responses") else chat_bytes
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    proxy = _mock_client(monkeypatch, handler)
    proxy.set_usage_reporter(lambda key, payload: events.append((key, payload)))
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    client = TestClient(proxy.proxy_app)
    headers = {"Authorization": "Bearer provider-key", "X-Brevitas-Key": "bvt_customer",
               "X-Brevitas-Project": "app", "X-Brevitas-Client": "backend"}
    chat = client.post("/v1/chat/completions", headers=headers,
                       json={"model": "gpt-4o-mini", "stream": True,
                             "messages": [{"role": "user", "content": "private prompt"}]})
    deepseek = client.post("/v1/chat/completions", headers=headers,
                           json={"model": "deepseek-chat", "stream": True,
                                 "messages": [{"role": "user", "content": "private prompt"}]})
    responses_request = (b'{ "model" : "gpt-4o-mini", "stream" : true, '
                         b'"input" : "another private prompt" }')
    responses = client.post("/v1/responses",
                            headers={**headers, "Content-Type": "application/json"},
                            content=responses_request)
    assert chat.content == chat_bytes
    assert deepseek.content == chat_bytes
    assert responses.content == responses_bytes
    assert forwarded_responses == [responses_request]
    assert [event[1]["operation"] for event in events] == ["chat.completions", "chat.completions", "responses"]
    assert events[0][1]["cached_input_tokens"] == 10
    assert events[1][1]["provider"] == "deepseek"
    assert events[2][1]["cached_input_tokens"] == 15
    assert all("private prompt" not in repr(payload) for _, payload in events)
    proxy.set_usage_reporter(None)


def test_reporting_failure_never_breaks_provider_response(monkeypatch):
    raw = b'{"id":"x","choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}'
    proxy = _mock_client(monkeypatch, lambda request: httpx.Response(
        200, content=raw, headers={"content-type": "application/json"}))
    proxy.set_usage_reporter(lambda key, payload: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    response = TestClient(proxy.proxy_app).post("/v1/chat/completions",
        headers={"Authorization": "Bearer provider", "X-Brevitas-Key": "bvt"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 200
    assert response.content == raw
    proxy.set_usage_reporter(None)


def test_anthropic_and_deepseek_nonstream_receipts(monkeypatch):
    events = []
    anthropic_raw = b'{"id":"msg_1","content":[{"type":"text","text":"ok"}],"usage":{"input_tokens":8,"cache_read_input_tokens":3,"cache_creation_input_tokens":2,"output_tokens":4}}'
    deepseek_raw = b'{"id":"ds_1","choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":12,"prompt_cache_hit_tokens":5,"completion_tokens":3}}'

    def handler(request):
        return httpx.Response(200, content=anthropic_raw if "anthropic" in request.url.host else deepseek_raw,
                              headers={"content-type": "application/json"})

    proxy = _mock_client(monkeypatch, handler)
    proxy.set_usage_reporter(lambda key, payload: events.append(payload))
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    client = TestClient(proxy.proxy_app)
    common = {"X-Brevitas-Key": "bvt", "X-Brevitas-Project": "app"}
    anthropic = client.post("/v1/messages", headers={**common, "X-Api-Key": "ant"},
        json={"model": "claude-sonnet-4-6", "max_tokens": 10,
              "messages": [{"role": "user", "content": "hello"}]})
    deepseek = client.post("/v1/chat/completions", headers={**common, "Authorization": "Bearer ds"},
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "hello"}]})
    assert anthropic.content == anthropic_raw
    assert deepseek.content == deepseek_raw
    assert [(e["provider"], e["cached_input_tokens"]) for e in events] == [
        ("anthropic", 3), ("deepseek", 5)]
    proxy.set_usage_reporter(None)


def test_anthropic_stream_and_openai_nonstream_receipts(monkeypatch):
    events = []
    stream_raw = (b'data: {"type":"message_start","message":{"id":"msg_stream",'
                  b'"usage":{"input_tokens":9,"cache_read_input_tokens":4}}}\n\n'
                  b'data: {"type":"message_delta","usage":{"output_tokens":3}}\n\n')
    chat_raw = (b'{"id":"chat_nonstream","choices":[{"message":{"content":"ok"}}],'
                b'"usage":{"prompt_tokens":20,"prompt_tokens_details":{"cached_tokens":7},'
                b'"completion_tokens":2}}')

    def handler(request):
        payload = stream_raw if "anthropic" in request.url.host else chat_raw
        media = "text/event-stream" if "anthropic" in request.url.host else "application/json"
        return httpx.Response(200, content=payload, headers={"content-type": media})

    proxy = _mock_client(monkeypatch, handler)
    proxy.set_usage_reporter(lambda key, payload: events.append(payload))
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    client = TestClient(proxy.proxy_app)
    anthropic = client.post("/v1/messages", headers={"X-Api-Key": "ant"},
        json={"model": "claude-sonnet-4-6", "stream": True, "max_tokens": 10,
              "messages": [{"role": "user", "content": "hello"}]})
    chat = client.post("/v1/chat/completions", headers={"Authorization": "Bearer openai"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]})
    assert anthropic.content == stream_raw
    assert chat.content == chat_raw
    assert [(event["operation"], event["cached_input_tokens"]) for event in events] == [
        ("messages", 4), ("chat.completions", 7)]
    proxy.set_usage_reporter(None)


def test_combined_hosted_proxy_writes_customer_dashboard_row(tmp_path, monkeypatch):
    import api.server as server
    import brevitas.proxy as proxy

    store = UsageStore(str(tmp_path / "hosted.db"))
    raw_key = "bvt_hosted_e2e"
    store.create_key(hash_key(raw_key), "e2e", owner_id="customer-e2e")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._seq_streams.clear()

    response_raw = (b'{"id":"resp_e2e","output":[],"usage":{"input_tokens":32,'
                    b'"input_tokens_details":{"cached_tokens":12},"output_tokens":4}}')
    real = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda *args, **kwargs: real(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=response_raw, headers={"content-type": "application/json"}))))
    proxy._cache_init_done = True
    proxy._cache_singleton = None
    proxy.set_usage_reporter(server._hosted_proxy_receipt)
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    monkeypatch.setenv("BREVITAS_PROXY_RPM", "2")
    server._proxy_windows.clear()
    server._proxy_active.clear()

    client = TestClient(server.app)
    headers = {"X-Brevitas-Key": raw_key, "Authorization": "Bearer provider-key",
               "X-Brevitas-Project": "backend-service", "X-Brevitas-Environment": "prod",
               "X-Brevitas-Client": "api-worker", "X-Brevitas-Request-Id": "e2e-1"}
    assert client.post("/v1/responses", headers={"Authorization": "Bearer provider-key"},
                       json={"model": "gpt-4o-mini", "input": "x"}).status_code == 401
    response = client.post("/v1/responses", headers=headers,
        json={"model": "gpt-4o-mini", "input": "private input"})
    assert response.status_code == 200
    assert response.content == response_raw
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/v1/responses", headers=headers,
        json={"model": "gpt-4o-mini", "input": "private input"}).status_code == 200
    assert client.post("/v1/responses", headers={**headers, "X-Brevitas-Request-Id": "e2e-2"},
        json={"model": "gpt-4o-mini", "input": "private input"}).status_code == 429
    breakdown = client.get("/v1/stats/breakdown",
                           headers={"X-Brevitas-Key": raw_key}).json()
    assert breakdown["totals"]["total_calls"] == 1
    assert [(row["project"], row["source"], row["provider"], row["model"])
            for row in breakdown["rows"]] == [
                ("backend-service", "api-worker", "openai", "gpt-4o-mini")]
    assert "private input" not in repr(store._rows(hash_key(raw_key)))
    proxy.set_usage_reporter(None)
