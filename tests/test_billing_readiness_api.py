"""GET /v1/billing/readiness (FUNNEL-FIX 2).

`brevitas billing-check` has called this endpoint since it shipped
(brevitas/cli.py:1011) and no server ever answered it, so the command always took
its own 404 branch and told the customer to ask an operator. These tests pin the
response shape against what that command actually consumes: a `checks` map of
name -> {ok, count?, state?, detail?} plus a top-level `billable` boolean, on
which the command's exit code depends.
"""
from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import UsageStore


def _hosted(tmp_path, monkeypatch, name, *, cache_enabled=True):
    import api.server as server

    store = UsageStore(str(tmp_path / f"{name}.db"))
    organization = store.ensure_organization("readiness-owner", "Readiness Co")
    service_account = store.ensure_service_account(
        organization["id"], "production", created_by="readiness-owner")
    raw_key = "bvt_readiness_service"
    store.create_key(
        hash_key(raw_key), "readiness key", owner_id="readiness-owner",
        organization_id=organization["id"],
        service_account_id=service_account["id"],
        key_type="organization_service",
        scopes=["proxy:invoke", "usage:write", "usage:read_own"],
        created_by="readiness-owner", request_id="readiness-key-0001",
        actor_role="company_owner",
    )
    if cache_enabled:
        store.set_cache_enabled(organization["id"], True)
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    server._cache_enabled_cache.clear()
    server._seq_streams.clear()
    return server, store, organization, raw_key, TestClient(server.app)


def test_readiness_names_every_reason_traffic_is_not_billable(
        tmp_path, monkeypatch):
    server, store, organization, raw_key, client = _hosted(
        tmp_path, monkeypatch, "readiness-cold", cache_enabled=False)
    monkeypatch.delenv("BREVITAS_CACHE_ENABLED", raising=False)

    cold = client.get("/v1/billing/readiness",
                      headers={"X-Brevitas-Key": raw_key})

    assert cold.status_code == 200
    payload = cold.json()
    # The exact shape brevitas/cli.py:1023-1031 iterates.
    assert isinstance(payload["checks"], dict)
    assert all(isinstance(check, dict) for check in payload["checks"].values())
    assert payload["billable"] is False
    checks = payload["checks"]
    assert checks["organization"]["ok"] is True
    assert checks["cache_enabled_process"]["ok"] is False
    assert checks["cache_enabled_organization"]["ok"] is False
    assert checks["traffic_observed"]["ok"] is False
    assert checks["traffic_observed"]["count"] == 0
    assert checks["billable_savings"]["ok"] is False
    # The one fact this process genuinely cannot assert stays "?" rather than
    # becoming a confident tick.
    assert checks["billing_surface"]["ok"] is None


def test_readiness_turns_billable_only_on_authoritative_savings(
        tmp_path, monkeypatch):
    server, store, organization, raw_key, client = _hosted(
        tmp_path, monkeypatch, "readiness-warm")
    monkeypatch.setenv("BREVITAS_CACHE_ENABLED", "true")
    headers = {"X-Brevitas-Key": raw_key}

    # Client-reported traffic: rows exist, verified savings exist, but nothing
    # here is billable -- which is precisely the confusion the command exists to
    # end, so the two lines must disagree.
    store.record_usage(
        hash_key(raw_key), 100, 80, owner_id="readiness-owner",
        organization_id=organization["id"], authoritative=False,
        receipt_source="proxy", request_id="readiness-client-0001",
        strategy="exact_cache", cost_saved_usd=0.25,
        measured_savings_usd=0.25, verified_savings_usd=0.25,
    )
    client_reported = client.get("/v1/billing/readiness", headers=headers).json()

    assert client_reported["checks"]["traffic_observed"]["ok"] is True
    assert client_reported["checks"]["traffic_observed"]["count"] == 1
    assert client_reported["checks"]["verified_savings"]["ok"] is True
    assert client_reported["checks"]["billable_savings"]["ok"] is False
    assert client_reported["billable"] is False

    store.record_usage(
        hash_key(raw_key), 100, 80, owner_id="readiness-owner",
        organization_id=organization["id"], authoritative=True,
        receipt_source="proxy", request_id="readiness-authoritative-0002",
        strategy="exact_cache", cache_attributable=True, cost_saved_usd=0.5,
        measured_savings_usd=0.5, verified_savings_usd=0.5,
    )
    billable = client.get("/v1/billing/readiness", headers=headers).json()

    assert billable["checks"]["billable_savings"]["ok"] is True
    assert billable["billable"] is True
    assert billable["billable_savings_usd"] > 0


def test_readiness_requires_a_key_and_the_read_scope(tmp_path, monkeypatch):
    server, store, organization, raw_key, client = _hosted(
        tmp_path, monkeypatch, "readiness-authz")

    unauthenticated = client.get("/v1/billing/readiness")
    unknown = client.get("/v1/billing/readiness",
                         headers={"X-Brevitas-Key": "bvt_not_a_key"})

    scopeless = "bvt_readiness_scopeless"
    store.create_key(hash_key(scopeless), "no read scope",
                     owner_id="readiness-owner",
                     organization_id=organization["id"],
                     key_type="device", scopes=["proxy:invoke"])
    server._auth_context_cache.clear()
    forbidden = client.get("/v1/billing/readiness",
                           headers={"X-Brevitas-Key": scopeless})

    assert unauthenticated.status_code == 401
    assert unknown.status_code == 401
    assert forbidden.status_code == 403


def test_readiness_is_scoped_to_the_calling_organization(tmp_path, monkeypatch):
    """One tenant's traffic must never appear in another tenant's checklist."""
    server, store, organization, raw_key, client = _hosted(
        tmp_path, monkeypatch, "readiness-tenant")
    other = store.ensure_organization("other-owner", "Other Co")
    store.record_usage(
        hash_key("bvt_other_tenant"), 100, 10, owner_id="other-owner",
        organization_id=other["id"], authoritative=True,
        receipt_source="proxy", request_id="other-tenant-0001",
        strategy="exact_cache", cache_attributable=True,
        verified_savings_usd=9.0, measured_savings_usd=9.0, cost_saved_usd=9.0,
    )

    mine = client.get("/v1/billing/readiness",
                      headers={"X-Brevitas-Key": raw_key}).json()

    assert mine["organization_id"] == organization["id"]
    assert mine["checks"]["traffic_observed"]["count"] == 0
    assert mine["checks"]["billable_savings"]["ok"] is False


def test_cli_consumes_the_response_without_a_404_dead_end(tmp_path, monkeypatch):
    """The route exists, so the CLI's 'this API build does not expose it' branch
    is unreachable for a build that carries this endpoint."""
    import api.server as server

    paths = {getattr(route, "path", "") for route in server.app.routes}
    assert "/v1/billing/readiness" in paths
