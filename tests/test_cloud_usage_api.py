import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import pytest
from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import UsageStore
from brevitas.security import EnvelopeCipher, LocalTestKMS

BEARER = "Bearer"


def test_dashboard_identity_prefers_authoritative_service_role(monkeypatch):
    import api.server as server

    captured = {}

    class Response:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {"id": "user-1"}

    def get(url, *, headers, timeout):
        captured.update(url=url, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "stale-anon")
    monkeypatch.setattr(server._requests, "get", get)

    identity = server._dashboard_identity(SimpleNamespace(
        headers={"authorization": f"{BEARER} user-token"}))

    assert identity["id"] == "user-1"
    assert captured["headers"] == {
        "apikey": "service-role", "Authorization": f"{BEARER} user-token"}


def test_bvx_device_login_mints_one_time_account_key(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "device.db"))
    kms = LocalTestKMS(b"d" * 32, environ={"BREVITAS_ENV": "test"})
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(
        server,
        "_credential_cipher",
        EnvelopeCipher(
            kms,
            key_id="device-login-test-key",
            key_version="1",
            wrap_algorithm=kms.algorithm,
        ),
    )
    client = TestClient(server.app)

    started = client.post("/v1/device-auth/start")
    assert started.status_code == 200
    device_code = started.json()["device_code"]
    assert started.json()["verification_uri_complete"].endswith(f"#bvx={device_code}")
    assert device_code not in repr(store.get_device_request(hash_key(device_code)))

    pending = client.post("/v1/device-auth/token", json={"device_code": device_code})
    assert pending.status_code == 202
    monkeypatch.setattr(server, "_dashboard_user", lambda request: "")
    assert client.post("/v1/device-auth/approve", json={"device_code": device_code}).status_code == 401
    monkeypatch.setattr(server, "_dashboard_user", lambda request: "user-device")
    assert client.post("/v1/device-auth/approve", json={"device_code": device_code}).status_code == 200
    assert client.post("/v1/device-auth/approve", json={"device_code": device_code}).status_code == 200
    exchange_expires_at = store.get_device_request(hash_key(device_code))["expires_at"]

    token = client.post(
        "/v1/device-auth/token",
        json={"device_code": device_code},
        headers={"X-Request-ID": "device-token-first-request"},
    )
    assert token.status_code == 200
    api_key = token.json()["api_key"]
    assert api_key.startswith("bvt_")
    assert store.key_owner(hash_key(api_key)) == "user-device"
    imported = client.post("/v1/customers/import", headers={"X-Brevitas-Key": api_key}, json={
        "customers": [{"external_id": "legacy-device-import-001"}],
    })
    assert imported.status_code == 200
    assert imported.json()["count"] == 1
    replay = client.post(
        "/v1/device-auth/token",
        json={"device_code": device_code},
        headers={"X-Request-ID": "device-token-replay-request"},
    )
    assert replay.status_code == 200
    assert replay.json() == {"api_key": api_key}
    assert replay.headers["cache-control"] == "no-store"
    with store._conn() as db:
        receipt = db.execute(
            "SELECT request_id,encrypted_key,expires_at "
            "FROM bvx_device_consumption_receipts WHERE device_hash=?",
            (hash_key(device_code),),
        ).fetchone()
    assert token.headers["x-request-id"] != replay.headers["x-request-id"]
    assert receipt["request_id"] == token.headers["x-request-id"]
    assert receipt["encrypted_key"]
    assert receipt["expires_at"] == exchange_expires_at
    assert datetime.fromisoformat(receipt["expires_at"]) > datetime.now(timezone.utc)

    expired = "expired_" + "x" * 40
    store.create_device_request(hash_key(expired),
                                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    assert client.post("/v1/device-auth/token", json={"device_code": expired}).status_code == 410


@pytest.mark.parametrize("invalidation", ["revoked_key", "disabled_member"])
def test_bvx_device_replay_fails_closed_after_authorization_is_revoked(
        tmp_path, monkeypatch, invalidation):
    import api.server as server

    store = UsageStore(str(tmp_path / f"device-replay-{invalidation}.db"))
    owner_id = f"device-owner-{invalidation}"
    organization_id = store.ensure_organization(owner_id)["id"]
    with store._conn() as db:
        db.execute(
            "ALTER TABLE organization_members "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    device_code = ("r" if invalidation == "revoked_key" else "d") * 48
    device_hash = hash_key(device_code)
    raw_key = f"bvt_device_{invalidation}"
    store.create_device_request(
        device_hash,
        (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    assert store.approve_device_request(
        device_hash, owner_id, hash_key(raw_key), "encrypted-device-key",
        organization_id=organization_id,
    )
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_decrypt", lambda *_args, **_kwargs: raw_key)
    client = TestClient(server.app)

    first = client.post(
        "/v1/device-auth/token", json={"device_code": device_code},
        headers={"X-Request-ID": f"device-first-{invalidation}"},
    )
    assert first.status_code == 200
    assert first.json() == {"api_key": raw_key}

    with store._conn() as db:
        if invalidation == "revoked_key":
            db.execute(
                "UPDATE api_keys SET revoked_at=? WHERE key_hash=?",
                (datetime.now(timezone.utc).isoformat(), hash_key(raw_key)),
            )
        else:
            db.execute(
                "UPDATE organization_members SET status='disabled' "
                "WHERE organization_id=? AND user_id=?",
                (organization_id, owner_id),
            )

    replay = client.post(
        "/v1/device-auth/token", json={"device_code": device_code},
        headers={"X-Request-ID": f"device-replay-{invalidation}"},
    )
    assert replay.status_code == 503
    assert replay.headers["retry-after"] == "1"
    assert replay.json() == {"detail": "Device authorization unavailable"}
    assert raw_key not in replay.text
    assert store.get_device_request(device_hash) is None
    with store._conn() as db:
        receipt = db.execute(
            "SELECT encrypted_key,quarantined_at FROM bvx_device_consumption_receipts "
            "WHERE device_hash=?", (device_hash,),
        ).fetchone()
        denied = db.execute(
            "SELECT action,outcome FROM audit_events "
            "WHERE request_id=? ORDER BY id DESC LIMIT 1",
            (replay.headers["x-request-id"],),
        ).fetchone()
    assert receipt["encrypted_key"] == "" and receipt["quarantined_at"]
    assert tuple(denied) == ("device_key.consume.denied", "denied")


def test_api_keys_are_managed_only_by_human_sessions(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "keys.db"))
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    client = TestClient(server.app)

    monkeypatch.setattr(server, "_dashboard_user", lambda request:
                        "user-1" if request.headers.get("authorization") == "Bearer session" else "")
    account_key = client.post("/v1/keys", headers={"Authorization": "Bearer session"},
                              json={"name": "company backend"})
    assert account_key.status_code == 200
    raw_account_key = account_key.json()["api_key"]

    assert client.post("/v1/keys", headers={"X-Brevitas-Key": raw_account_key},
                       json={"name": "forbidden child"}).status_code == 401
    listed = client.get("/v1/keys", headers={"Authorization": "Bearer session"})
    fingerprint = hash_key(raw_account_key)[:16]
    listed_key = next(key for key in listed.json()["keys"] if key["fingerprint"] == fingerprint)
    key_id = listed_key["id"]
    assert hash_key(raw_account_key) not in str(listed.json())
    assert client.delete(f"/v1/keys/{key_id}",
                         headers={"Authorization": "Bearer session"}).status_code == 200
    assert client.post("/v1/keys", json={"name": "anonymous"}).status_code == 401


def test_repo_registration_and_admin_key_inventory_are_tenant_safe(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "repos.db"))
    raw_key = "bvt_repo_key"
    key_hash = hash_key(raw_key)
    store.create_key(key_hash, "CLI key", owner_id="user-1")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    client = TestClient(server.app)

    registered = client.post("/v1/repositories", headers={"X-Brevitas-Key": raw_key},
                             json={"repo": "/private/customer/checkout.git", "source": "bvx"})
    assert registered.status_code == 200
    assert registered.json()["repo"] == "checkout"
    store.record_usage(key_hash, 100, 80, owner_id="user-1", repo="runtime-repo")

    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "regular-user", "app_metadata": {}})
    assert client.get("/v1/admin/keys").status_code == 403
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "admin-user", "app_metadata": {"brevitas_admin": True}})
    response = client.get("/v1/admin/keys", headers={
        "Authorization": BEARER + " test-admin-session"})
    assert response.status_code == 200
    inventory = response.json()
    assert inventory["total_keys"] == 1
    assert inventory["total_repositories"] == 2
    assert inventory["keys"][0]["key_name"] == "CLI key"
    assert {repo["name"] for repo in inventory["keys"][0]["repositories"]} == {
        "checkout", "runtime-repo"}
    assert raw_key not in response.text
    assert inventory["keys"][0]["key_id"] == key_hash[:12]


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
        "quality_score": .95, "request_id": "same", "project": "/private/work/backend-app",
        "strategy": "native_cache",
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
    assert breakdown[0]["repo"] == "backend-app"
    assert breakdown[0]["source"] == "worker"
    assert breakdown[0]["client"] == "python-sdk"
    assert "must never be stored" not in repr(store._rows(hash_key("bvt_test")))
    assert "/private/work" not in repr(store._rows(hash_key("bvt_test")))
    store.create_key(hash_key("bvt_other"), "other", owner_id="user-2")
    store.record_usage(hash_key("bvt_other"), 50, 40, project="other-app", source="api")
    assert client.get("/v1/stats", headers=headers).json()["total_calls"] == 1
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "user-2", "app_metadata": {}})
    assert client.get("/v1/admin/stats").status_code == 403
    assert client.get("/v1/admin/stats", headers={
        "X-Brevitas-Admin": "legacy-static-token"}).status_code == 403
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "admin-user", "app_metadata": {"brevitas_admin": True}})
    admin = client.get("/v1/admin/stats", headers={
        "Authorization": "Bearer " + "test-admin-session"})
    assert admin.status_code == 200
    assert admin.json()["total_calls"] == 2


def test_usage_receipt_alignment_and_method_based_verification(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "accounting.db"))
    store.create_key(hash_key("bvt_accounting"), "accounting")
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()
    client = TestClient(server.app)
    headers = {"X-Brevitas-Key": "bvt_accounting"}

    # The optimizer saw 100 -> 80 message tokens, while the authoritative
    # provider receipt includes another 920 system/tool/cache tokens. The API
    # must preserve only the 20-token delta and report 1020 -> 1000.
    base = {
        "provider": "openai", "model": "gpt-4o-mini",
        "baseline_tokens": 100, "compressed_tokens": 80,
        "fresh_input_tokens": 600, "cached_input_tokens": 400,
        "output_tokens": 10, "receipt_available": True,
    }
    safe = client.post("/v1/usage", headers=headers, json={
        **base, "request_id": "safe", "strategy": "native_cache",
        "cache_attributable": False,
    })
    assert safe.status_code == 200
    assert safe.json()["baseline_tokens"] == 1020
    assert safe.json()["compressed_tokens"] == 1000
    assert safe.json()["tokens_saved"] == 20
    assert safe.json()["quality_status"] == "verified"
    # OpenAI's automatic cache discount is not credited to Brevitas; only the
    # 20 removed tokens are measured (20 * $0.15 / 1M).
    assert safe.json()["measured_savings_usd"] == 0.000003

    retrieval = client.post("/v1/usage", headers=headers, json={
        **base, "request_id": "retrieval", "strategy": "retrieve",
        "quality_score": .99,
    })
    assert retrieval.json()["quality_status"] == "unverified"
    assert retrieval.json()["verified_savings_usd"] == 0

    paired = client.post("/v1/usage", headers=headers, json={
        **base, "request_id": "paired", "strategy": "retrieve",
        "quality_score": .72, "quality_verified": True,
    })
    assert paired.json()["quality_status"] == "verified"
    # Caller-reported telemetry can be analyzed but is never authoritative billing input.
    assert paired.json()["verified_savings_usd"] == 0
    assert paired.json()["brevitas_fee_usd"] == 0

    # A provider/local tokenizer mismatch with no transformation is zero
    # savings, not a fabricated win or loss.
    unchanged = client.post("/v1/usage", headers=headers, json={
        **base, "request_id": "unchanged", "strategy": "passthrough",
        "baseline_tokens": 100, "compressed_tokens": 100,
    })
    assert unchanged.json()["baseline_tokens"] == 1000
    assert unchanged.json()["compressed_tokens"] == 1000
    assert unchanged.json()["measured_savings_usd"] == 0

    # An attributable 1-hour cache write can be a real temporary loss. Keep it
    # signed for auditability, but never verify it or charge a savings fee.
    write = client.post("/v1/usage", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-6",
        "baseline_tokens": 100, "compressed_tokens": 100,
        "cache_write_tokens": 100, "cache_write_1h_tokens": 100,
        "output_tokens": 0, "receipt_available": True,
        "cache_attributable": True, "request_id": "cache-write",
        "strategy": "cache_only",
    })
    assert write.json()["measured_savings_usd"] == -0.0003
    assert write.json()["verified_savings_usd"] == 0
    assert write.json()["brevitas_fee_usd"] == 0


def test_authoritative_billing_excludes_response_reuse_and_unknown_models(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "billing-quality-boundary.db"))
    store.create_key(hash_key("bvt_billing_boundary"), "billing-boundary")
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()

    response_reuse = server._record_usage_report(
        hash_key("bvt_billing_boundary"),
        server.UsageReportRequest(
            provider="openai", model="gpt-4o-mini",
            baseline_tokens=100, compressed_tokens=0,
            fresh_input_tokens=0, output_tokens=0,
            baseline_output_tokens=20, strategy="exact_cache",
            quality_verified=True, request_id="exact-cache",
        ),
        authoritative=True,
    )
    unknown_model = server._record_usage_report(
        hash_key("bvt_billing_boundary"),
        server.UsageReportRequest(
            provider="openai", model="future-model-without-pricing",
            baseline_tokens=100, compressed_tokens=80,
            fresh_input_tokens=80, output_tokens=0,
            strategy="native_cache", request_id="unknown-price",
        ),
        authoritative=True,
    )
    native_cache = server._record_usage_report(
        hash_key("bvt_billing_boundary"),
        server.UsageReportRequest(
            provider="openai", model="gpt-4o-mini",
            baseline_tokens=100, compressed_tokens=80,
            fresh_input_tokens=80, output_tokens=0,
            strategy="native_cache", request_id="native-cache",
        ),
        authoritative=True,
    )

    assert response_reuse["quality_status"] == "verified"
    assert response_reuse["verified_savings_usd"] == 0
    assert response_reuse["brevitas_fee_usd"] == 0
    assert unknown_model["pricing_status"] == "unpriced"
    assert unknown_model["verified_savings_usd"] == 0
    assert native_cache["pricing_status"] == "priced"
    assert native_cache["verified_savings_usd"] > 0


def test_repo_client_model_breakdown_reconciles(tmp_path):
    store = UsageStore(str(tmp_path / "reconcile.db"))
    rows = [
        ("repo-a", "codex", "openai", "gpt-4o-mini", 100, 80, .10),
        ("repo-a", "codex", "deepseek", "deepseek-chat", 200, 150, .20),
        ("repo-a", "claude-code", "anthropic", "claude-sonnet-4-6", 300, 250, .30),
        ("repo-b", "backend", "openai", "gpt-4o", 400, 300, .40),
    ]
    for repo, client, provider, model, baseline, optimized, usd in rows:
        store.record_usage("key", baseline, optimized, repo=repo, project=repo,
                           client=client, source=client, provider=provider, model=model,
                           measured_savings_usd=usd, verified_savings_usd=usd)

    breakdown = store.get_breakdown("key")
    totals = store.get_stats("key")
    assert sum(row["calls"] for row in breakdown) == totals["total_calls"]
    assert sum(row["tokens_saved"] for row in breakdown) == totals["total_tokens_saved"]
    assert round(sum(row["measured_savings_usd"] for row in breakdown), 8) == totals["total_measured_savings_usd"]
    assert {(row["repo"], row["client"], row["provider"], row["model"]) for row in breakdown} == {
        (repo, client, provider, model) for repo, client, provider, model, *_ in rows
    }


def test_admin_financial_report_is_filtered_paginated_and_protected(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "admin.db"))
    store.record_usage("a", 100, 70, owner_id="user-a", project="alpha", client="codex",
                       provider="openai", model="gpt-4o-mini", baseline_cost_usd=.20,
                       actual_cost_usd=.14, measured_savings_usd=.06,
                       verified_savings_usd=.05, brevitas_fee_usd=.005)
    store.record_usage("b", 200, 150, owner_id="user-b", project="beta", client="backend",
                       provider="anthropic", model="claude", baseline_cost_usd=.40,
                       actual_cost_usd=.30, measured_savings_usd=.10,
                       verified_savings_usd=.08, brevitas_fee_usd=.008)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "regular-user", "app_metadata": {}})
    client = TestClient(server.app)

    assert client.get("/v1/admin/stats/breakdown").status_code == 403
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "admin-user", "app_metadata": {"role": "brevitas_admin"}})
    response = client.get(
        "/v1/admin/stats/breakdown?range=all&account=user-a&limit=1",
        headers={"Authorization": "Bearer " + "test-admin-session"},
    )
    assert response.status_code == 200
    report = response.json()
    assert report["pagination"] == {
        "total": 1, "limit": 1, "next_cursor": "", "has_more": False,
    }
    assert report["rows"][0]["account_id"] == "user-a"
    assert report["rows"][0]["actual_cost_usd"] == .14
    assert report["totals"]["total_actual_cost_usd"] == .14
    assert client.get("/v1/admin/stats/breakdown?range=365d",
                      headers={"Authorization": "Bearer " + "test-admin-session"}).status_code == 422
    billing = client.get("/v1/admin/billing?range=all", headers={
        "Authorization": "Bearer " + "test-admin-session"})
    assert billing.status_code == 200
    assert billing.json()["amount_owed_usd"] == .013
    assert billing.json()["payment_status_tracked"] is False
    assert {account["account_id"] for account in billing.json()["accounts"]} == {
        "user-a", "user-b"}


def test_admin_posthog_summary_keeps_personal_key_server_side(monkeypatch):
    import api.server as server

    personal_key = "phx_" + "private"
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "admin-user", "app_metadata": {"brevitas_admin": True}})
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "42")
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", personal_key)
    server._POSTHOG_CACHE.clear()
    calls = []

    class Response:
        def __init__(self, results):
            self._results = results

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": self._results}

    results = [[[120, 80, 90, 12, 8]], [[45.5, 32.1]],
               [["2026-07-15", 80, 90, 120]]]

    def post(url, *, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        return Response(results[len(calls) - 1])

    monkeypatch.setattr(server._requests, "post", post)
    response = TestClient(server.app).get(
        "/v1/admin/analytics?range=30d",
        headers={"Authorization": "Bearer " + "test-admin-session"},
    )
    assert response.status_code == 200
    assert response.json()["visitors"] == 80
    assert response.json()["avg_session_duration_seconds"] == 45.5
    assert all(call[1]["Authorization"] == f"Bearer {personal_key}" for call in calls)
    assert personal_key not in response.text


def test_admin_posthog_summary_reports_rejected_credentials(monkeypatch):
    import api.server as server

    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "admin-user", "app_metadata": {"brevitas_admin": True}})
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "42")
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_rejected")
    server._POSTHOG_CACHE.clear()

    class Response:
        status_code = 403

        def raise_for_status(self):
            raise AssertionError("credential rejection should be handled first")

        def json(self):
            return {"detail": "invalid personal API key"}

    def post(url, *, headers, json, timeout):
        return Response()

    monkeypatch.setattr(server._requests, "post", post)
    response = TestClient(server.app).get(
        "/v1/admin/analytics?range=30d",
        headers={"Authorization": "Bearer " + "test-admin-session"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "PostHog reporting credentials were rejected; "
        "update POSTHOG_PERSONAL_API_KEY"
    )


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
    headers = {"Authorization": f"{BEARER} provider-key", "X-Brevitas-Key": "bvt_customer",
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
        headers={"Authorization": f"{BEARER} provider", "X-Brevitas-Key": "bvt"},
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
    deepseek = client.post("/v1/chat/completions", headers={**common, "Authorization": f"{BEARER} ds"},
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
    chat = client.post("/v1/chat/completions", headers={"Authorization": f"{BEARER} openai"},
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
    headers = {"X-Brevitas-Key": raw_key, "Authorization": f"{BEARER} provider-key",
               "X-Brevitas-Project": "backend-service", "X-Brevitas-Environment": "prod",
               "X-Brevitas-Client": "api-worker", "X-Brevitas-Request-Id": "e2e-1"}
    assert client.post("/v1/responses", headers={"Authorization": f"{BEARER} provider-key"},
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
