import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import requests
from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import UsageStore
from brevitas.security import EnvelopeCipher, KMSUnavailable, LocalTestKMS


def _local_test_cipher():
    kms = LocalTestKMS(b"b" * 32, environ={"BREVITAS_ENV": "test"})
    return EnvelopeCipher(
        kms, key_id="backend-contract-key", key_version="1",
        wrap_algorithm=kms.algorithm,
    )


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
    store.create_key(
        hash_key(raw_key), "test", owner_id="owner-1",
        scopes=["proxy:invoke", "usage:write", "usage:read_own",
                "provider:read", "provider:manage"],
    )
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_credential_cipher", _local_test_cipher())
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    return server, store, raw_key, TestClient(server.app)


def test_playground_cache_accounting_does_not_double_count_prompt_tokens():
    source = (Path(__file__).resolve().parents[1] / "api/server.py").read_text()
    assert 'baseline_tokens=tokens_saved_total' in source
    assert 'baseline_tokens=pipe["baseline_tokens"] + cache_saved_tokens' not in source


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


def test_saved_provider_stream_kms_failure_is_503_before_sse(tmp_path, monkeypatch):
    server, _, raw_key, client = _server_client(tmp_path, monkeypatch)
    headers = {"X-Brevitas-Key": raw_key}
    saved = client.put("/v1/provider", headers=headers, json={
        "provider": "deepseek", "provider_api_key": "provider-secret",
        "model": "deepseek-chat",
    })
    assert saved.status_code == 200

    monkeypatch.setattr(
        server, "_decrypt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            KMSUnavailable("temporary outage")),
    )
    monkeypatch.setattr(
        server, "_compress_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pipeline must not start before provider preflight")),
    )
    response = client.post("/v1/compress/stream", headers=headers, json={
        "task": "ping", "messages": ["hello"], "prior_context": [],
        "lossy": False,
    })
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["retry-after"] == "1"
    assert response.json() == {
        "detail": "Credential security dependency unavailable",
    }


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


def test_provider_credential_kms_misconfiguration_is_retryable_503(tmp_path, monkeypatch):
    server, store, raw_key, client = _server_client(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_credential_cipher", None)
    monkeypatch.setattr(server, "_managed_kms_adapter", None)
    for name in (
        "BREVITAS_LOCAL_KMS_KEY", "BREVITAS_KMS_PROVIDER",
        "BREVITAS_KMS_KEY_ID", "BREVITAS_KMS_KEY_VERSION",
        "BREVITAS_KMS_REQUIRED",
    ):
        monkeypatch.delenv(name, raising=False)
    response = client.put("/v1/provider", headers={"X-Brevitas-Key": raw_key}, json={
        "provider": "deepseek", "provider_api_key": "never-persisted",
        "model": "deepseek-chat",
    })
    assert response.status_code == 503
    assert response.json() == {"detail": "Credential security dependency unavailable"}
    assert response.headers["retry-after"] == "1"
    assert store.get_provider_config(hash_key(raw_key)) is None


def test_provider_store_failures_are_generic_retryable_before_sse(tmp_path, monkeypatch):
    server, store, raw_key, client = _server_client(tmp_path, monkeypatch)
    headers = {"X-Brevitas-Key": raw_key}

    def unavailable(_key_hash):
        raise requests.ConnectionError("SENTINEL-POSTGREST-DETAIL")

    monkeypatch.setattr(store, "get_provider_config", unavailable)
    responses = [
        client.get("/v1/provider", headers=headers),
        client.put("/v1/provider", headers=headers, json={
            "provider": "deepseek", "provider_api_key": "secret",
            "model": "deepseek-chat",
        }),
        client.post("/v1/compress/stream", headers=headers, json={
            "task": "ping", "messages": ["hello"], "prior_context": [],
        }),
    ]
    for response in responses:
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert response.json() == {"detail": "Provider configuration unavailable"}
        assert "SENTINEL" not in response.text
    assert responses[-1].headers["content-type"].startswith("application/json")


def test_provider_store_invalid_response_and_write_failure_are_retryable(
        tmp_path, monkeypatch):
    server, store, raw_key, client = _server_client(tmp_path, monkeypatch)
    headers = {"X-Brevitas-Key": raw_key}
    monkeypatch.setattr(store, "get_provider_config", lambda _key_hash: [])
    invalid = client.get("/v1/provider", headers=headers)
    assert invalid.status_code == 503
    assert invalid.headers["retry-after"] == "1"
    assert invalid.json() == {"detail": "Provider configuration unavailable"}

    monkeypatch.setattr(store, "get_provider_config", lambda _key_hash: None)
    monkeypatch.setattr(
        store, "set_provider_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            requests.Timeout("SENTINEL-WRITE-DETAIL")),
    )
    failed_write = client.put("/v1/provider", headers=headers, json={
        "provider": "deepseek", "provider_api_key": "secret",
        "model": "deepseek-chat",
    })
    assert failed_write.status_code == 503
    assert failed_write.headers["retry-after"] == "1"
    assert failed_write.json() == {"detail": "Provider configuration unavailable"}
    assert "SENTINEL" not in failed_write.text


def test_device_kms_outage_does_not_consume_one_time_record(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "device-kms.db"))
    device_code = "d" * 48
    device_hash = hash_key(device_code)
    organization_id = store.ensure_organization("verified-user")["id"]
    store.create_device_request(
        device_hash,
        (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    assert store.approve_device_request(
        device_hash, "verified-user", hash_key("bvt_device"), "encrypted-value",
        organization_id=organization_id)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(
        server, "_decrypt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            KMSUnavailable("temporary outage")),
    )

    response = TestClient(server.app).post(
        "/v1/device-auth/token", json={"device_code": device_code})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert store.get_device_request(device_hash) is not None


def test_device_digest_mismatch_quarantines_record(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "device-digest.db"))
    device_code = "e" * 48
    device_hash = hash_key(device_code)
    organization_id = store.ensure_organization("verified-user")["id"]
    store.create_device_request(
        device_hash,
        (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    assert store.approve_device_request(
        device_hash, "verified-user", hash_key("bvt_expected"), "encrypted-value",
        organization_id=organization_id)
    consume_calls = []
    actual_consume = store.consume_device_request_idempotent

    def tracked_consume(value, expected_digest, request_id):
        consume_calls.append((value, expected_digest, request_id))
        return actual_consume(value, expected_digest, request_id)

    monkeypatch.setattr(store, "consume_device_request_idempotent", tracked_consume)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_decrypt", lambda *_args, **_kwargs: "bvt_tampered")
    response = TestClient(server.app).post(
        "/v1/device-auth/token", json={"device_code": device_code})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert consume_calls[0][0] == device_hash
    assert consume_calls[0][1] == hash_key("bvt_tampered")
    assert consume_calls[0][2]
    assert store.get_device_request(device_hash) is None


def test_device_endpoint_replays_receipt_with_new_request_id(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "device-replay.db"))
    owner_id = "verified-user"
    organization_id = store.ensure_organization(owner_id)["id"]
    device_code = "r" * 48
    device_hash = hash_key(device_code)
    raw_key = "bvt_replayed_device"
    encrypted_key = "encrypted-value"
    store.create_device_request(
        device_hash,
        (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    assert store.approve_device_request(
        device_hash, owner_id, hash_key(raw_key), encrypted_key,
        organization_id=organization_id)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_decrypt", lambda *_args, **_kwargs: raw_key)
    client = TestClient(server.app)

    first = client.post("/v1/device-auth/token", json={"device_code": device_code})
    replay = client.post("/v1/device-auth/token", json={"device_code": device_code})
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() == {"api_key": raw_key}


def test_hosted_device_exchange_requires_idempotent_receipt_contract(monkeypatch):
    import api.server as server

    device_code = "f" * 48
    device_hash = hash_key(device_code)
    raw_key = "bvt_hosted_device"

    class LegacyHostedStore:
        def _request(self, *_args, **_kwargs):
            raise AssertionError("legacy hosted consume must not be called")

        def get_device_request(self, value):
            assert value == device_hash
            return {
                "device_hash": device_hash,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "owner_id": "verified-user",
                "key_hash": hash_key(raw_key),
                "encrypted_key": "encrypted-value",
                "approved_at": datetime.now(timezone.utc).isoformat(),
            }

        def consume_device_request(self, _value):
            raise AssertionError("ambiguous legacy RPC must remain fail-closed")

    monkeypatch.setattr(server, "_store", LegacyHostedStore())
    monkeypatch.setattr(server, "_decrypt", lambda *_args, **_kwargs: raw_key)
    response = TestClient(server.app).post(
        "/v1/device-auth/token", json={"device_code": device_code})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json() == {"detail": "Device authorization unavailable"}


def test_device_exchange_store_outage_is_generic_retryable(monkeypatch):
    import api.server as server

    class UnavailableStore:
        def get_device_request(self, _value):
            raise TimeoutError("SENTINEL-STORE-DETAIL")

    monkeypatch.setattr(server, "_store", UnavailableStore())
    response = TestClient(server.app).post(
        "/v1/device-auth/token", json={"device_code": "u" * 48})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json() == {"detail": "Device authorization unavailable"}
    assert "SENTINEL" not in response.text


def _multi_org_device_approval_client(monkeypatch):
    import api.server as server

    organization_id = "11111111-1111-4111-8111-111111111111"
    foreign_id = "22222222-2222-4222-8222-222222222222"
    device_code = "m" * 48
    calls = []
    encryption_contexts = []

    class Store:
        def ensure_organization(self, owner_id):
            calls.append(("ensure", owner_id))

        def get_device_request(self, value):
            assert value == hash_key(device_code)
            return {
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "approved_at": "", "owner_id": "", "organization_id": "",
            }

        def resolve_device_approval_organization(self, owner_id, selected):
            calls.append(("resolve", owner_id, selected))
            if not selected:
                raise ValueError("company_selection_required")
            if selected != organization_id:
                raise ValueError("company_access_denied")
            return {"id": organization_id, "role": "member"}

        def approve_device_request(self, *args, **kwargs):
            calls.append(("approve", *args, kwargs))
            return True

    store = Store()
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_user", lambda _request: "authenticated-user")

    def encrypt(_value, *, context):
        encryption_contexts.append(context)
        return "encrypted-device-key"

    monkeypatch.setattr(server, "_encrypt", encrypt)
    return (
        TestClient(server.app), device_code, organization_id, foreign_id,
        calls, encryption_contexts,
    )


def test_multi_org_device_approval_requires_company_selector(monkeypatch):
    client, device_code, _, _, calls, _ = _multi_org_device_approval_client(monkeypatch)
    response = client.post(
        "/v1/device-auth/approve", json={"device_code": device_code})
    assert response.status_code == 409
    assert response.json() == {"detail": "Select a company for this device"}
    assert not any(call[0] == "approve" for call in calls)


def test_multi_org_device_approval_rejects_foreign_selector(monkeypatch):
    client, device_code, _, foreign_id, calls, _ = _multi_org_device_approval_client(
        monkeypatch)
    response = client.post("/v1/device-auth/approve", json={
        "device_code": device_code, "company_id": foreign_id,
    })
    assert response.status_code == 403
    assert response.json() == {"detail": "Company access denied"}
    assert not any(call[0] == "approve" for call in calls)


def test_multi_org_device_approval_binds_selected_company(monkeypatch):
    client, device_code, organization_id, _, calls, contexts = (
        _multi_org_device_approval_client(monkeypatch))
    response = client.post(
        "/v1/device-auth/approve",
        json={"device_code": device_code},
        headers={"X-Brevitas-Company-ID": organization_id},
    )
    assert response.status_code == 200
    assert calls[0] == ("resolve", "authenticated-user", organization_id)
    approval = next(call for call in calls if call[0] == "approve")
    assert approval[1:3] == (hash_key(device_code), "authenticated-user")
    assert approval[-1] == {"organization_id": organization_id}
    assert contexts == [{
        "purpose": "device_key", "device_hash": hash_key(device_code),
        "organization_id": organization_id,
    }]


def test_sqlite_device_approval_revalidates_exact_selected_membership(tmp_path):
    store = UsageStore(str(tmp_path / "multi-org-device.db"))
    owner_id = "multi-org-owner"
    first = store.ensure_organization(owner_id)["id"]
    second = store.ensure_organization("second-org-owner")["id"]
    with store._conn() as db:
        db.execute(
            "INSERT INTO organization_members(organization_id,user_id,role,created_at) "
            "VALUES(?,?,?,?)",
            (second, owner_id, "member", datetime.now(timezone.utc).isoformat()),
        )

    with pytest.raises(ValueError, match="company_selection_required"):
        store.resolve_device_approval_organization(owner_id)
    with pytest.raises(ValueError, match="company_access_denied"):
        store.resolve_device_approval_organization(
            owner_id, "33333333-3333-4333-8333-333333333333")
    assert store.resolve_device_approval_organization(owner_id, second) == {
        "id": second, "role": "member",
    }

    device_hash = hash_key("multi-org-device-code")
    store.create_device_request(
        device_hash,
        (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    assert store.approve_device_request(
        device_hash, owner_id, hash_key("bvt_multi_org_device"), "encrypted-value",
        organization_id=second,
    )
    assert store.get_device_request(device_hash)["organization_id"] == second
    assert store.get_device_request(device_hash)["organization_id"] != first

    second_device = hash_key("multi-org-disabled-membership-device")
    store.create_device_request(
        second_device,
        (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    with store._conn() as db:
        db.execute(
            "DELETE FROM organization_members "
            "WHERE organization_id=? AND user_id=?", (second, owner_id),
        )
    with pytest.raises(ValueError, match="company_access_denied"):
        store.approve_device_request(
            second_device, owner_id, hash_key("bvt_disabled_device"),
            "encrypted-disabled", organization_id=second,
        )
    assert not store.get_device_request(second_device).get("approved_at")


def _inactive_dashboard_client(tmp_path, monkeypatch, *, status: str, role: str):
    import api.server as server
    from api.company_admin import company_admin_for_store

    store = UsageStore(str(tmp_path / f"dashboard-{status}-{role}.db"))
    user_id = "dashboard-member"
    organization = store.ensure_organization(user_id)
    company_admin_for_store(store)
    raw_key = f"bvt_dashboard_membership_matrix_{status}_{role}"
    store.create_key(
        hash_key(raw_key), "dashboard session", owner_id=user_id,
        organization_id=organization["id"], key_type="dashboard_session",
        scopes=["proxy:invoke", "usage:read_own", "provider:read", "provider:manage"],
        environment="dashboard", created_by=user_id,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        request_id="request-dashboard-matrix", actor_role="company_owner",
    )
    with store._conn() as db:
        db.execute(
            "UPDATE organization_members SET status=?,role=? "
            "WHERE organization_id=? AND user_id=?",
            (status, role, organization["id"], user_id),
        )
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_user", lambda _request: user_id)
    monkeypatch.setattr(server, "_dashboard_identity", lambda _request: {
        "id": user_id, "email": "", "email_confirmed_at": "",
    })
    server._auth_context_cache.clear()
    server._valid_key_cache.clear()
    return server, TestClient(server.app), raw_key


@pytest.mark.parametrize(("status", "role"), [
    ("disabled", "member"),
    ("removed", "member"),
    ("active", "viewer"),
])
def test_dashboard_route_matrix_requires_active_finite_membership(
        tmp_path, monkeypatch, status, role):
    server, client, raw_key = _inactive_dashboard_client(
        tmp_path, monkeypatch, status=status, role=role)
    bearer = {
        "Authorization": "Bearer verified-session",
        "X-API-Key": raw_key,
    }
    key_headers = {"X-Brevitas-Key": raw_key}
    installation_id = "33333333-3333-4333-8333-333333333333"

    human_requests = [
        client.get("/v1/keys", headers=bearer),
        client.post("/v1/keys", headers=bearer, json={
            "purpose": "dashboard_session",
        }),
        client.delete(f"/v1/keys/{installation_id}", headers=bearer),
        client.get("/v1/customers", headers=bearer),
        client.post("/v1/customers/import", headers=bearer, json={
            "customers": [{"external_id": "customer-one"}],
        }),
        client.get("/v1/cache-policy", headers=bearer),
        client.put("/v1/cache-policy", headers=bearer, json={"enabled": False}),
        client.get("/v1/installations", headers=bearer),
        client.get("/v1/organization/inventory", headers=bearer),
        client.delete(f"/v1/installations/{installation_id}", headers=bearer),
    ]
    key_requests = [
        client.get("/v1/provider", headers=key_headers),
        client.put("/v1/provider", headers=key_headers, json={
            "provider": "deepseek", "provider_api_key": "secret",
            "model": "deepseek-chat",
        }),
        client.get("/v1/providers", headers=key_headers),
        client.get("/v1/ollama/models", headers=key_headers),
        client.get("/v1/stats", headers=key_headers),
        client.get("/v1/stats/breakdown", headers=key_headers),
        client.get("/v1/quality/stream", headers=key_headers),
        client.post("/v1/quality/stream/reset", headers=key_headers),
        client.post("/v1/compress", headers=key_headers, json={
            "messages": ["hello"], "prior_context": [], "lossy": False,
        }),
    ]
    for response in [*human_requests, *key_requests]:
        assert response.status_code == 403, response.text

    from starlette.requests import Request
    principal = server._company_admin_principal(Request({
        "type": "http", "method": "GET", "path": "/v1/company/capabilities",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("127.0.0.1", 1), "scheme": "http",
    }))
    assert principal.actor_id == "dashboard-member"
    assert principal.company_id == ""
    assert principal.role == ""


def test_membership_dependency_failures_are_generic_retryable(tmp_path, monkeypatch):
    server, client, raw_key = _inactive_dashboard_client(
        tmp_path, monkeypatch, status="active", role="member")

    monkeypatch.setattr(
        server._store, "member_organization",
        lambda _user_id: (_ for _ in ()).throw(
            requests.ConnectionError("SENTINEL-MEMBERSHIP-DETAIL")),
    )
    human = client.get(
        "/v1/customers", headers={"Authorization": "Bearer verified-session"})
    assert human.status_code == 503
    assert human.headers["retry-after"] == "1"
    assert human.json() == {"detail": "Membership verification unavailable"}
    assert "SENTINEL" not in human.text

    # Restore the human lookup and fail the exact dashboard-key membership RPC.
    store = server._store
    monkeypatch.undo()
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_user", lambda _request: "dashboard-member")
    monkeypatch.setattr(
        store, "resolve_device_approval_organization",
        lambda *_args: (_ for _ in ()).throw(
            requests.Timeout("SENTINEL-ACTIVE-MEMBERSHIP-DETAIL")),
    )
    server._auth_context_cache.clear()
    key_response = client.get(
        "/v1/provider", headers={"X-Brevitas-Key": raw_key})
    assert key_response.status_code == 503
    assert key_response.headers["retry-after"] == "1"
    assert key_response.json() == {"detail": "Membership verification unavailable"}
    assert "SENTINEL" not in key_response.text


def test_hosted_device_exchange_uses_digest_bound_idempotent_receipt(monkeypatch):
    import api.server as server

    device_code = "g" * 48
    device_hash = hash_key(device_code)
    raw_key = "bvt_hosted_idempotent_device"
    expected_key_hash = hash_key(raw_key)
    organization_id = "11111111-1111-4111-8111-111111111111"
    calls = []

    class HostedStore:
        def _request(self, *_args, **_kwargs):
            raise AssertionError("public idempotent contract must own the RPC")

        def get_device_request(self, value):
            assert value == device_hash
            return {
                "device_hash": device_hash,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "owner_id": "verified-user",
                "organization_id": organization_id,
                "key_hash": expected_key_hash,
                "encrypted_key": "encrypted-value",
                "approved_at": datetime.now(timezone.utc).isoformat(),
            }

        def consume_device_request_idempotent(
                self, value, expected_digest, request_id):
            calls.append((value, expected_digest, request_id))
            return {
                "status": "consumed",
                "already_consumed": False,
                "key_hash": expected_key_hash,
                "encrypted_key": "encrypted-value",
                "organization_id": organization_id,
            }

    monkeypatch.setattr(server, "_store", HostedStore())
    monkeypatch.setattr(server, "_decrypt", lambda *_args, **_kwargs: raw_key)
    response = TestClient(server.app).post(
        "/v1/device-auth/token", json={"device_code": device_code})
    assert response.status_code == 200
    assert response.json() == {"api_key": raw_key}
    assert calls[0][:2] == (device_hash, expected_key_hash)
    assert calls[0][2]


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
