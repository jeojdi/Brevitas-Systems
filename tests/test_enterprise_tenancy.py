import sqlite3

from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import SupabaseUsageStore, UsageStore


def _organization_key(store: UsageStore, user_id: str, raw_key: str,
                      *, scopes=None, environment="production"):
    organization = store.ensure_organization(user_id, f"{user_id} org")
    account = store.ensure_service_account(organization["id"], environment, user_id)
    store.create_key(
        hash_key(raw_key), "backend", owner_id=user_id,
        organization_id=organization["id"], service_account_id=account["id"],
        key_type="organization_service", environment=environment,
        scopes=scopes or ["proxy:invoke", "usage:write", "usage:read_own",
                          "customer:route", "customer:auto_provision",
                          "installations:register"],
    )
    return organization, account


def test_customer_routing_is_credential_derived_idempotent_and_cross_tenant_safe(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "tenant.db"))
    org_a, _ = _organization_key(store, "company-a", "bvt_company_a")
    org_b, _ = _organization_key(store, "company-b", "bvt_company_b")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    client = TestClient(server.app)

    headers_a = {"X-Brevitas-Key": "bvt_company_a",
                 "X-Brevitas-Customer-ID": "finance-customer-001"}
    assert client.get("/v1/stats", headers=headers_a).status_code == 200
    first = store.find_customer(org_a["id"], "finance-customer-001")
    assert first is not None
    assert client.get("/v1/stats", headers=headers_a).status_code == 200
    assert store.find_customer(org_a["id"], "finance-customer-001")["id"] == first["id"]

    headers_b = {"X-Brevitas-Key": "bvt_company_b",
                 "X-Brevitas-Customer-ID": "finance-customer-001"}
    assert client.get("/v1/stats", headers=headers_b).status_code == 200
    assert store.find_customer(org_b["id"], "finance-customer-001")["id"] != first["id"]

    store.create_key(hash_key("bvt_legacy"), "legacy")
    forbidden = client.get("/v1/stats", headers={
        "X-Brevitas-Key": "bvt_legacy", "X-Brevitas-Customer-ID": "customer-1"})
    assert forbidden.status_code == 403


def test_management_requires_human_bearer_and_raw_secret_is_never_stored(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "keys.db"))
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_user", lambda request:
                        "company-admin" if request.headers.get("authorization") == "Bearer session" else "")
    client = TestClient(server.app)

    assert client.post("/v1/keys", headers={"X-Brevitas-Key": "workload"},
                       json={"name": "bad"}).status_code == 401
    created = client.post("/v1/keys", headers={"Authorization": "Bearer session"},
                          json={"name": "prod backend", "environment": "production"})
    assert created.status_code == 200
    raw_key = created.json()["api_key"]
    assert created.json()["secret_available_once"] is True
    assert "customer:route" in created.json()["scopes"]

    with sqlite3.connect(store.db_path) as db:
        persisted = repr(db.execute("SELECT * FROM api_keys").fetchall())
    assert raw_key not in persisted
    assert hash_key(raw_key) in persisted
    assert client.get("/v1/keys", headers={"X-Brevitas-Key": raw_key}).status_code == 401
    assert client.get("/v1/keys", headers={"Authorization": "Bearer session"}).status_code == 200

    first_session = client.post("/v1/keys", headers={"Authorization": "Bearer session"},
                                json={"name": "dashboard", "purpose": "dashboard_session"})
    second_session = client.post("/v1/keys", headers={"Authorization": "Bearer session"},
                                 json={"name": "dashboard", "purpose": "dashboard_session"})
    assert first_session.status_code == second_session.status_code == 200
    assert second_session.json()["expires_at"] is not None
    with sqlite3.connect(store.db_path) as db:
        active_sessions = db.execute(
            "SELECT count(*) FROM api_keys WHERE key_type='dashboard_session' AND revoked_at=''"
        ).fetchone()[0]
    assert active_sessions == 1

    organization = store.member_organization("company-admin")
    store.create_key(
        hash_key("bvt_colleague_session"), "colleague dashboard",
        owner_id="company-admin", organization_id=organization["id"],
        key_type="dashboard_session", scopes=["usage:read_own"],
        created_by="colleague-admin",
    )
    third_session = client.post("/v1/keys", headers={"Authorization": "Bearer session"},
                                json={"name": "dashboard", "purpose": "dashboard_session"})
    assert third_session.status_code == 200
    with sqlite3.connect(store.db_path) as db:
        colleague_active = db.execute(
            "SELECT count(*) FROM api_keys WHERE key_type='dashboard_session' "
            "AND created_by='colleague-admin' AND revoked_at=''"
        ).fetchone()[0]
    assert colleague_active == 1


def test_external_usage_is_attributed_but_never_authoritative_or_billable(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "usage.db"))
    organization, _ = _organization_key(store, "company-a", "bvt_company_a")
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()
    client = TestClient(server.app)
    response = client.post("/v1/usage", headers={
        "X-Brevitas-Key": "bvt_company_a",
        "X-Brevitas-Customer-ID": "customer-ledger-1",
    }, json={
        "provider": "openai", "model": "gpt-4o-mini",
        "baseline_tokens": 1000, "compressed_tokens": 100,
        "fresh_input_tokens": 100, "quality_verified": True,
        "strategy": "byte_preserving", "request_id": "caller-reported-1",
    })
    assert response.status_code == 200
    assert response.json()["verified_savings_usd"] == 0
    assert response.json()["brevitas_fee_usd"] == 0
    row = store._rows(hash_key("bvt_company_a"))[0]
    customer = store.find_customer(organization["id"], "customer-ledger-1")
    assert row["organization_id"] == organization["id"]
    assert row["customer_id"] == customer["id"]
    assert row["authoritative"] == 0
    assert row["verified_savings_usd"] == 0


def test_installation_inventory_is_organization_scoped(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "installations.db"))
    org_a, _ = _organization_key(store, "company-a", "bvt_company_a")
    _organization_key(store, "company-b", "bvt_company_b")
    monkeypatch.setattr(server, "_store", store)
    client = TestClient(server.app)
    installation_id = "11111111-1111-4111-8111-111111111111"
    body = {"installation_id": installation_id,
            "device": {"id": "host-hash-1", "platform": "darwin", "arch": "arm64"},
            "repository": {"id": "repo-hash-1", "label": "company-backend"},
            "environment": "production",
            "client": {"name": "bvx", "version": "1.2.3"}}
    registered = client.post("/v1/installations",
                             headers={"X-Brevitas-Key": "bvt_company_a"}, json=body)
    assert registered.status_code == 200
    assert registered.json()["installation_id"] == installation_id
    heartbeat = client.post(f"/v1/installations/{installation_id}/heartbeat",
        headers={"X-Brevitas-Key": "bvt_company_a"}, json={
            "device": body["device"], "environment": "production", "client": body["client"]})
    assert heartbeat.status_code == 200
    assert heartbeat.json()["heartbeat_interval_seconds"] == 300
    assert len(store.list_installations(org_a["id"])) == 1
    assert store.list_installations(org_a["id"])[0]["repository"] == "company-backend"
    monkeypatch.setattr(server, "_dashboard_user", lambda request:
                        "company-a" if request.headers.get("authorization") == "Bearer session" else "")
    assert client.delete(f"/v1/installations/{installation_id}",
                         headers={"Authorization": "Bearer session"}).status_code == 200
    assert client.post(f"/v1/installations/{installation_id}/heartbeat",
        headers={"X-Brevitas-Key": "bvt_company_a"}, json={
            "device": body["device"], "environment": "production", "client": body["client"]
        }).status_code == 409
    org_b = store.member_organization("company-b")
    assert store.list_installations(org_b["id"]) == []

    inventory = client.get("/v1/organization/inventory",
                           headers={"Authorization": "Bearer session"})
    assert inventory.status_code == 200
    assert inventory.json()["counts"] == {
        "members": 1, "customers": 0, "keys": 1, "devices": 1, "installations": 1,
    }


def test_bearer_bulk_import_is_idempotent(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "import.db"))
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_user", lambda request:
                        "admin" if request.headers.get("authorization") == "Bearer session" else "")
    client = TestClient(server.app)
    body = {"customers": [{"external_id": "old-001", "display_name": "Old customer"},
                           {"external_id": "old-002"}]}
    first = client.post("/v1/customers/import", headers={"Authorization": "Bearer session"}, json=body)
    second = client.post("/v1/customers/import", headers={"Authorization": "Bearer session"}, json=body)
    assert first.status_code == second.status_code == 200
    assert {row["id"] for row in first.json()["customers"]} == {
        row["id"] for row in second.json()["customers"]}
    assert len(client.get("/v1/customers", headers={"Authorization": "Bearer session"}).json()["customers"]) == 2


def test_bulk_import_is_tenant_derived_for_each_company_key(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "cross-tenant-import.db"))
    org_a, _ = _organization_key(store, "company-a", "bvt_company_a")
    org_b, _ = _organization_key(store, "company-b", "bvt_company_b")
    monkeypatch.setattr(server, "_store", store)
    server._auth_context_cache.clear()
    server._valid_key_cache.clear()
    client = TestClient(server.app)
    body = {"customers": [{"external_id": "shared-local-id"}]}

    imported_a = client.post("/v1/customers/import", headers={
        "X-Brevitas-Key": "bvt_company_a",
    }, json=body)
    imported_b = client.post("/v1/customers/import", headers={
        "X-Brevitas-Key": "bvt_company_b",
    }, json=body)

    assert imported_a.status_code == imported_b.status_code == 200
    assert imported_a.json()["organization_id"] == org_a["id"]
    assert imported_b.json()["organization_id"] == org_b["id"]
    assert imported_a.json()["customers"][0]["id"] != imported_b.json()["customers"][0]["id"]


def test_supabase_bulk_customer_import_is_one_database_request(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return [{
            "id": f"customer-{index}",
            "external_id": customer["external_id"],
            "display_name": customer.get("display_name", ""),
            "status": "active",
        } for index, customer in enumerate(kwargs["data"]["p_customers"])]

    monkeypatch.setattr(store, "_request", request)
    customers = [{"external_id": f"past-{index:04d}"} for index in range(1000)]
    imported = store.upsert_customers("organization-a", customers)

    assert len(imported) == 1000
    assert len(calls) == 1
    assert calls[0][0:2] == ("POST", "rpc/import_enterprise_customers")
    assert calls[0][2]["data"]["p_organization_id"] == "organization-a"


def test_scopes_are_enforced_on_every_workload_endpoint(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "scope-matrix.db"))
    organization = store.ensure_organization("company-a", "Company A")
    account = store.ensure_service_account(organization["id"], "production", "company-a")
    raw_key = "bvt_installation_only"
    store.create_key(
        hash_key(raw_key), "installation only", owner_id="company-a",
        organization_id=organization["id"], service_account_id=account["id"],
        key_type="device", scopes=["installations:register"],
    )
    monkeypatch.setattr(server, "_store", store)
    server._auth_context_cache.clear()
    server._valid_key_cache.clear()
    client = TestClient(server.app)
    headers = {"X-Brevitas-Key": raw_key}

    assert client.get("/v1/stats", headers=headers).status_code == 403
    assert client.post("/v1/usage", headers=headers, json={
        "baseline_tokens": 2, "compressed_tokens": 1,
    }).status_code == 403
    assert client.post("/v1/repositories", headers=headers,
                       json={"repo": "safe-repo"}).status_code == 403
    assert client.get("/v1/provider", headers=headers).status_code == 403
    assert client.put("/v1/provider", headers=headers, json={
        "provider": "ollama", "model": "llama3.2",
    }).status_code == 403
    assert client.post("/v1/compress", headers=headers, json={
        "messages": ["hello"], "lossy": False,
    }).status_code == 403
    assert client.post("/v1/customers/import", headers=headers, json={
        "customers": [{"external_id": "forbidden-customer"}],
    }).status_code == 403


def test_company_service_proxy_requires_exact_customer_header(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "customer-required.db"))
    _organization_key(store, "company-a", "bvt_company_a")
    monkeypatch.setattr(server, "_store", store)
    server._auth_context_cache.clear()
    server._valid_key_cache.clear()
    client = TestClient(server.app)

    missing = client.post("/v1/chat/completions", headers={
        "X-Brevitas-Key": "bvt_company_a",
        "Authorization": "Bearer provider-key",
    }, json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
    assert missing.status_code == 400
    assert "X-Brevitas-Customer-ID" in missing.json()["detail"]


def test_auth_context_cache_is_bounded_for_high_cardinality_customers(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "bounded-auth-cache.db"))
    _organization_key(store, "company-a", "bvt_company_a")
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setenv("BREVITAS_AUTH_CONTEXT_CACHE_MAX", "100")
    server._auth_context_cache.clear()
    kh = hash_key("bvt_company_a")
    for index in range(125):
        server._auth_context_for_key(kh, f"customer-{index}")
    assert len(server._auth_context_cache) <= 100
