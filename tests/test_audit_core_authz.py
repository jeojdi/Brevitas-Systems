"""Auth, tenant-isolation and audit-trail regressions for the api/ control plane.

Every test here pins a specific confirmed finding:

* device credentials must be revokable, attributable and (optionally) bounded
* a DSR tenant must come from a platform grant, not the operator's own workspace
* a plain `member` must not read whole-company spend or Brevitas fees
* Brevitas staff cross-tenant reads must land in audit_events
* membership probes must ignore removed/disabled rows
* GET /v1/organization/inventory must work and report only current members
* provider/warming/cache-policy writes must leave an audit row
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.store import SupabaseUsageStore, UsageStore
from api.auth import hash_key
from brevitas.security import EnvelopeCipher, LocalTestKMS


ORGANIZATION = "11111111-1111-4111-8111-111111111111"
ACTOR = "22222222-2222-4222-8222-222222222222"
KEY_ID = "33333333-3333-4333-8333-333333333333"


def _dashboard_client(tmp_path, monkeypatch, *, name: str, role: str = "owner",
                      status: str = "active"):
    """Store + client whose single human holds `role` in their own workspace."""
    import api.server as server
    from api.company_admin import company_admin_for_store

    store = UsageStore(str(tmp_path / f"{name}.db"))
    user_id = f"human-{name}"
    organization = store.ensure_organization(user_id, "Company")
    company_admin_for_store(store)
    raw_key = f"bvt_{name}_session_key"
    store.create_key(
        hash_key(raw_key), "dashboard session", owner_id=user_id,
        organization_id=organization["id"], key_type="dashboard_session",
        scopes=["proxy:invoke", "usage:read_own", "provider:read", "provider:manage"],
        environment="dashboard", created_by=user_id,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        request_id=f"request-{name}-session", actor_role="company_owner",
    )
    with store._conn() as db:
        db.execute(
            "UPDATE organization_members SET role=?,status=? "
            "WHERE organization_id=? AND user_id=?",
            (role, status, organization["id"], user_id),
        )
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_user", lambda _request: user_id)
    monkeypatch.setattr(server, "_dashboard_identity", lambda _request: {
        "id": user_id, "email": "", "email_confirmed_at": "",
    })
    server._auth_context_cache.clear()
    server._valid_key_cache.clear()
    return server, store, organization, user_id, raw_key, TestClient(server.app)


def _dollar_keys(payload) -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).endswith("_usd"):
                found.append(str(key))
            found.extend(_dollar_keys(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_dollar_keys(item))
    return found


# ── member least privilege on spend (finding: 'member' reads company spend) ────

def test_member_session_reads_usage_without_company_spend_or_fees(tmp_path, monkeypatch):
    server, store, organization, user_id, raw_key, client = _dashboard_client(
        tmp_path, monkeypatch, name="member-spend", role="member")
    store.record_usage(hash_key(raw_key), 1000, 400, owner_id=user_id,
                       organization_id=organization["id"], project="backend",
                       provider="openai", model="gpt-4o-mini")
    headers = {"X-Brevitas-Key": raw_key}

    overview = client.get("/v1/stats", headers=headers)
    assert overview.status_code == 200
    assert _dollar_keys(overview.json()) == []
    assert overview.json()["spend_redacted"] is True
    # The operational answer a member is entitled to survives redaction.
    assert overview.json()["total_calls"] == 1
    assert overview.json()["total_tokens_saved"] == 600

    breakdown = client.get("/v1/stats/breakdown", headers=headers)
    assert breakdown.status_code == 200
    assert _dollar_keys(breakdown.json()) == []
    assert breakdown.json()["rows"][0]["calls"] == 1

    for path in ("/v1/audit", "/v1/stats/activity", "/v1/stats/cache",
                 "/v1/stats/pipelines"):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, path
        assert _dollar_keys(response.json()) == [], path


# ── CONTRACT A: the per-dimension lists must be able to SAY they are redacted ──
# The three routes returned a bare JSON array, and _spend_filtered has nowhere to
# hang `spend_redacted` on a list. A redacted member therefore received rows with
# their *_usd keys quietly missing, which the dashboard rendered as a confident
# $0.00 — indistinguishable from a pipeline that genuinely saved nothing.

_CONTRACT_A_PATHS = ("/v1/stats/pipelines", "/v1/stats/agents", "/v1/stats/runs")


def test_pipeline_agent_and_run_lists_declare_redaction_to_a_member(tmp_path, monkeypatch):
    server, store, organization, user_id, raw_key, client = _dashboard_client(
        tmp_path, monkeypatch, name="member-rows", role="member")
    store.record_usage(hash_key(raw_key), 1000, 400, owner_id=user_id,
                       organization_id=organization["id"], pipeline="launch",
                       agent="writer", run_id="run-1", provider="openai",
                       model="gpt-4o-mini", verified_savings_usd=0.5,
                       brevitas_fee_usd=0.125)
    headers = {"X-Brevitas-Key": raw_key}

    for path in _CONTRACT_A_PATHS:
        body = client.get(path, headers=headers).json()
        assert isinstance(body, dict), path
        # The rows themselves are unchanged: the envelope is additive.
        assert isinstance(body["rows"], list) and body["rows"], path
        assert _dollar_keys(body) == [], path
        # The whole point: "withheld" is now STATED, not inferred from absence.
        assert body["spend_redacted"] is True, path


def test_pipeline_agent_and_run_lists_state_spend_redacted_false_for_an_owner(
        tmp_path, monkeypatch):
    server, store, organization, user_id, raw_key, client = _dashboard_client(
        tmp_path, monkeypatch, name="owner-rows", role="owner")
    store.record_usage(hash_key(raw_key), 1000, 400, owner_id=user_id,
                       organization_id=organization["id"], pipeline="launch",
                       agent="writer", run_id="run-1", provider="openai",
                       model="gpt-4o-mini", verified_savings_usd=0.5,
                       brevitas_fee_usd=0.125)
    headers = {"X-Brevitas-Key": raw_key}

    for path in _CONTRACT_A_PATHS:
        body = client.get(path, headers=headers).json()
        # False is emitted EXPLICITLY. A consumer must never have to read
        # "key absent" as "not redacted" — that is the same inference the bare
        # list forced, one level up.
        assert body["spend_redacted"] is False, path
        assert body["rows"][0]["cost_saved_usd"] == 0.5, path
        assert body["rows"][0]["brevitas_fee_usd"] == 0.125, path


def test_owner_session_and_workload_keys_still_read_spend(tmp_path, monkeypatch):
    server, store, organization, user_id, raw_key, client = _dashboard_client(
        tmp_path, monkeypatch, name="owner-spend", role="owner")
    store.record_usage(hash_key(raw_key), 1000, 400, owner_id=user_id,
                       organization_id=organization["id"], provider="openai",
                       model="gpt-4o-mini")
    owner = client.get("/v1/stats", headers={"X-Brevitas-Key": raw_key})
    assert owner.status_code == 200
    assert "total_brevitas_fee_usd" in owner.json()
    assert "spend_redacted" not in owner.json()

    # A workload credential carries no human role and must be untouched.
    workload = "bvt_workload_owner_spend"
    store.create_key(hash_key(workload), "workload", owner_id=user_id,
                     organization_id=organization["id"])
    store.record_usage(hash_key(workload), 500, 200, owner_id=user_id,
                       organization_id=organization["id"])
    server._auth_context_cache.clear()
    legacy = client.get("/v1/stats", headers={"X-Brevitas-Key": workload})
    assert legacy.status_code == 200
    assert "total_actual_cost_usd" in legacy.json()


def test_company_admin_session_reads_its_own_tenants_spend(tmp_path, monkeypatch):
    """Finding 27 was about the 'member' role ONLY: a company_admin runs the
    tenant's savings dashboard and must keep the money view. ROLE_PERMISSIONS
    grants billing:manage to company_owner and billing_admin but not
    company_admin, so gating on the permission alone silently rendered a
    confident wrong $0.00 on the admin's own money view. Same rule as
    get_warming(): owner OR admin role, or billing:manage.
    """
    server, store, organization, user_id, raw_key, client = _dashboard_client(
        tmp_path, monkeypatch, name="admin-spend", role="admin")
    store.record_usage(hash_key(raw_key), 1000, 400, owner_id=user_id,
                       organization_id=organization["id"], provider="openai",
                       model="gpt-4o-mini")
    admin = client.get("/v1/stats", headers={"X-Brevitas-Key": raw_key})
    assert admin.status_code == 200
    assert "total_brevitas_fee_usd" in admin.json()
    assert "total_actual_cost_usd" in admin.json()
    assert "spend_redacted" not in admin.json()

    # billing_admin holds billing:manage and reads money through the
    # permission leg of the same rule.
    with store._conn() as db:
        db.execute(
            "UPDATE organization_members SET role='billing_admin' "
            "WHERE organization_id=? AND user_id=?",
            (organization["id"], user_id))
    server._auth_context_cache.clear()
    billing = client.get("/v1/stats", headers={"X-Brevitas-Key": raw_key})
    assert billing.status_code == 200
    assert "total_brevitas_fee_usd" in billing.json()
    assert "spend_redacted" not in billing.json()


def test_member_warming_status_hides_budget_but_keeps_operational_view(
        tmp_path, monkeypatch):
    server, store, organization, user_id, raw_key, client = _dashboard_client(
        tmp_path, monkeypatch, name="member-warming", role="member")
    store.warm_credentials_upsert(
        organization["id"], "anthropic", "encrypted", True, user_id, 12.5, 10, 5)
    status = client.get("/v1/warming", headers={"Authorization": "Bearer session"})
    assert status.status_code == 200
    assert status.json()["providers"][0]["provider"] == "anthropic"
    assert status.json()["providers"][0]["warm_pings"] == 0
    assert _dollar_keys(status.json()) == []
    assert status.json()["spend_redacted"] is True


def test_quality_stream_reset_authorizes_on_live_role_not_unminted_scope(
        tmp_path, monkeypatch):
    """Production dashboard sessions never carry quality:manage — the hosted RPC
    (company_admin_create_dashboard_session_key) hard-codes
    proxy:invoke/usage:read_own/provider:read/provider:manage. A scope-only gate
    would therefore 403 every caller including the owner, leaving a tripped
    mSPRT stream unclearable short of a replica redeploy. The reset must
    authorize on the live company role; a plain member stays denied.
    """
    server, store, organization, user_id, raw_key, client = _dashboard_client(
        tmp_path, monkeypatch, name="reset-role", role="admin")
    # The session key was minted with exactly production's scope list (no
    # quality:manage) by _dashboard_client; the live admin role must carry it.
    reset = client.post("/v1/quality/stream/reset",
                        headers={"X-Brevitas-Key": raw_key})
    assert reset.status_code == 200

    with store._conn() as db:
        db.execute(
            "UPDATE organization_members SET role='member' "
            "WHERE organization_id=? AND user_id=?",
            (organization["id"], user_id))
    server._auth_context_cache.clear()
    denied = client.post("/v1/quality/stream/reset",
                         headers={"X-Brevitas-Key": raw_key})
    assert denied.status_code == 403


# ── platform-admin attribution (finding: staff reads produce no audit row) ─────

def test_platform_admin_reads_are_recorded_in_audit_events(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "platform-audit.db"))
    store.create_key(hash_key("bvt_platform_audit"), "cli", owner_id="user-1")
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_identity", lambda _request: {
        "id": ACTOR, "app_metadata": {"brevitas_admin": True},
    })
    client = TestClient(server.app)
    assert client.get("/v1/admin/stats", headers={
        "Authorization": "Bearer staff"}).status_code == 200
    assert client.get("/v1/admin/keys", headers={
        "Authorization": "Bearer staff"}).status_code == 200
    assert client.get(f"/v1/admin/accounts/{'user-1'}/usage", headers={
        "Authorization": "Bearer staff"}).status_code == 200

    with store._conn() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT organization_id,actor_id,actor_role,action,target_type,target_id,"
            "details,outcome,request_id FROM audit_events ORDER BY id",
        ).fetchall()]
    assert [row["action"] for row in rows] == [
        "platform.usage_overview.read", "platform.key_inventory.read",
        "platform.account_usage.read",
    ]
    for row in rows:
        assert row["actor_id"] == ACTOR
        assert row["actor_role"] == "brevitas_admin"
        assert row["outcome"] == "committed"
        assert row["details"] == "{}"
        assert len(row["request_id"]) >= 8
        # Cross-tenant reads have no single tenant to attribute.
        assert row["organization_id"] == ""
    assert rows[-1]["target_type"] == "account"
    assert rows[-1]["target_id"] == "user-1"


def test_platform_admin_read_fails_closed_when_hosted_audit_write_fails(monkeypatch):
    import api.server as server

    class HostedStore:
        def _request(self, *_args, **_kwargs):
            raise AssertionError("no direct PostgREST call expected")

        def append_audit_event(self, **_kwargs):
            raise RuntimeError("audit trail unreachable")

        def get_admin_stats(self):
            raise AssertionError("tenant data served without attribution")

    monkeypatch.setattr(server, "_store", HostedStore())
    monkeypatch.setattr(server, "_dashboard_identity", lambda _request: {
        "id": ACTOR, "app_metadata": {"brevitas_admin": True},
    })
    client = TestClient(server.app)
    denied = client.get("/v1/admin/stats", headers={"Authorization": "Bearer staff"})
    assert denied.status_code == 503
    assert denied.json()["detail"] == "Audit trail unavailable"


# ── device credentials (findings: never expire, cannot be revoked) ─────────────

def test_supabase_device_key_revocation_uses_the_tenant_rpc_and_admin_roles():
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    calls: list[tuple[str, str, dict]] = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("data") or {}))
        return {"ok": True, "key_id": KEY_ID, "revoked": True}

    store._request = request  # type: ignore[method-assign]
    assert store.revoke_organization_key(
        ORGANIZATION, KEY_ID, ACTOR, request_id="request-revoke-device",
        actor_role="company_admin", key_type="device") is True
    # The dispatcher shipped in 202607280019, not a device-specific RPC: no
    # company_admin_revoke_device_key exists in any migration, so pinning that
    # name here asserted a route that 503s against the real schema.
    assert [call[1] for call in calls] == ["rpc/company_admin_revoke_tenant_key"]

    calls.clear()
    # A plain member may kill their own browser session but not an org-wide
    # device credential.
    with pytest.raises(ValueError, match="device keys"):
        store.revoke_organization_key(
            ORGANIZATION, KEY_ID, ACTOR, request_id="request-revoke-device",
            actor_role="member", key_type="device")
    assert calls == []

    calls.clear()
    assert store.revoke_organization_key(
        ORGANIZATION, KEY_ID, ACTOR, request_id="request-revoke-session",
        actor_role="member") is True
    assert [call[1] for call in calls] == ["rpc/company_admin_revoke_tenant_key"]


def test_supabase_key_revocation_degrades_to_the_session_rpc_before_0019_applies():
    """Code-first deploy safety: the tenant dispatcher ships in 202607280019,
    which is hand-applied after api/ deploys from a push. Until it exists,
    PostgREST answers PGRST202 and revocation must fall back to the strict,
    already-applied session RPC — no worse than the previous deploy — instead
    of 503ing every key type including dashboard sessions that work today.
    Any other error must stay fatal.
    """
    import requests as requests_module

    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    calls: list[str] = []

    def _http_error(status, body):
        response = requests_module.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        return requests_module.HTTPError(response=response)

    def request(method, path, **kwargs):
        calls.append(path)
        if path == "rpc/company_admin_revoke_tenant_key":
            raise _http_error(404, {
                "code": "PGRST202",
                "message": "Could not find the function "
                           "public.company_admin_revoke_tenant_key",
            })
        return {"ok": True, "key_id": KEY_ID, "revoked": True}

    store._request = request  # type: ignore[method-assign]
    assert store.revoke_organization_key(
        ORGANIZATION, KEY_ID, ACTOR, request_id="request-revoke-fallback",
        actor_role="member") is True
    assert calls == ["rpc/company_admin_revoke_tenant_key",
                     "rpc/company_admin_revoke_dashboard_session_key"]

    # A non-PGRST202 failure (here: RLS/permission denied) must not fall back.
    calls.clear()

    def request_denied(method, path, **kwargs):
        calls.append(path)
        raise _http_error(403, {"code": "42501", "message": "permission denied"})

    store._request = request_denied  # type: ignore[method-assign]
    with pytest.raises(requests_module.HTTPError):
        store.revoke_organization_key(
            ORGANIZATION, KEY_ID, ACTOR, request_id="request-revoke-denied",
            actor_role="member")
    assert calls == ["rpc/company_admin_revoke_tenant_key"]


def test_device_key_revocation_is_reachable_from_the_dashboard(tmp_path, monkeypatch):
    server, store, organization, user_id, raw_key, client = _dashboard_client(
        tmp_path, monkeypatch, name="device-revoke", role="owner")
    device_raw = "bvt_device_credential_for_revocation"
    store.create_key(
        hash_key(device_raw), "bvx device", owner_id=user_id,
        organization_id=organization["id"], key_type="device",
        scopes=["proxy:invoke", "usage:write"], created_by=user_id,
    )
    with store._conn() as db:
        device_id = db.execute("SELECT id FROM api_keys WHERE key_hash=?",
                               (hash_key(device_raw),)).fetchone()[0]
    assert store.organization_key_type(organization["id"], device_id) == "device"

    revoked = client.delete(f"/v1/keys/{device_id}",
                            headers={"Authorization": "Bearer session"})
    assert revoked.status_code == 200
    assert store.key_context(hash_key(device_raw)) is None


def test_sqlite_device_revocation_requires_an_administering_role(tmp_path):
    store = UsageStore(str(tmp_path / "device-role.db"))
    organization = store.ensure_organization("owner-1", "Company")
    store.create_key(hash_key("bvt_device_role_gate"), "bvx device",
                     owner_id="owner-1", organization_id=organization["id"],
                     key_type="device", created_by="member-2")
    with store._conn() as db:
        key_id = db.execute("SELECT id FROM api_keys WHERE key_hash=?",
                            (hash_key("bvt_device_role_gate"),)).fetchone()[0]
    with pytest.raises(ValueError, match="device keys"):
        store.revoke_organization_key(
            organization["id"], key_id, "member-2",
            request_id="request-device-denied", actor_role="member")
    assert store.key_context(hash_key("bvt_device_role_gate")) is not None
    assert store.revoke_organization_key(
        organization["id"], key_id, "owner-1",
        request_id="request-device-allowed", actor_role="company_owner")
    assert store.key_context(hash_key("bvt_device_role_gate")) is None


def test_device_credential_lifetime_ceiling_is_opt_in(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "device-age.db"))
    organization = store.ensure_organization("owner-1", "Company")
    raw_key = "bvt_device_age_ceiling"
    store.create_key(hash_key(raw_key), "bvx device", owner_id="owner-1",
                     organization_id=organization["id"], key_type="device",
                     scopes=["proxy:invoke", "usage:read_own"])
    stale = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    with store._conn() as db:
        db.execute("UPDATE api_keys SET created=? WHERE key_hash=?",
                   (stale, hash_key(raw_key)))
    monkeypatch.setattr(server, "_store", store)
    client = TestClient(server.app)

    # Default: inert, because expiring device keys still regresses the onboarding
    # evidence gates and there is no CLI re-login path yet.
    monkeypatch.delenv("BREVITAS_DEVICE_KEY_MAX_AGE_SECONDS", raising=False)
    server._auth_context_cache.clear()
    assert client.get("/v1/stats", headers={
        "X-Brevitas-Key": raw_key}).status_code == 200

    monkeypatch.setenv("BREVITAS_DEVICE_KEY_MAX_AGE_SECONDS", "86400")
    server._auth_context_cache.clear()
    expired = client.get("/v1/stats", headers={"X-Brevitas-Key": raw_key})
    assert expired.status_code == 401
    assert expired.json()["detail"] == "Device credential expired"


def test_activated_device_key_records_the_approving_human(tmp_path):
    store = UsageStore(str(tmp_path / "device-created-by.db"))
    organization = store.ensure_organization("approver-1", "Company")
    device_hash = hash_key("device-attribution-request")
    store.create_device_request(
        device_hash, (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
    raw_key = "bvt_device_attribution"
    assert store.approve_device_request(
        device_hash, "approver-1", hash_key(raw_key), "encrypted",
        organization_id=organization["id"])
    receipt = store.consume_device_request_idempotent(
        device_hash, hash_key(raw_key), "request-device-attribution")
    assert receipt is not None
    with store._conn() as db:
        row = dict(db.execute(
            "SELECT key_type,created_by,owner_id FROM api_keys WHERE key_hash=?",
            (hash_key(raw_key),)).fetchone())
    assert row["key_type"] == "device"
    # created_by is the approver; owner_id stays the billing owner.
    assert row["created_by"] == "approver-1"


# ── compliance tenant authority (finding: DSR lands in the operator's tenant) ──

def _compliance_request(monkeypatch, server, headers: dict):
    from starlette.requests import Request

    return server._compliance_admin_principal(Request({
        "type": "http", "method": "POST", "path": "/v1/admin/compliance/requests",
        "headers": [(key.lower().encode(), value.encode())
                    for key, value in headers.items()],
        "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
        "scheme": "http",
    }))


def test_compliance_tenant_comes_from_a_platform_grant_not_the_own_workspace(
        monkeypatch):
    import api.server as server

    checked: list[tuple[str, str]] = []

    class Store:
        def compliance_tenant_authority(self, actor_user_id, organization_id):
            checked.append((actor_user_id, organization_id))
            return organization_id == ORGANIZATION

    monkeypatch.setattr(server, "_store", Store())
    monkeypatch.setattr(server, "_dashboard_identity", lambda _request: {
        "id": ACTOR, "app_metadata": {"role": "brevitas_admin"},
    })
    monkeypatch.setattr(server, "_active_company_membership", lambda _actor: (
        (_ for _ in ()).throw(AssertionError(
            "a destructive DSR must not use the operator's active workspace"))))

    granted = _compliance_request(monkeypatch, server, {
        server.COMPLIANCE_TENANT_HEADER: ORGANIZATION})
    assert granted.organization_id == ORGANIZATION
    assert granted.role == "brevitas_admin"
    assert checked == [(ACTOR, ORGANIZATION)]

    other = "44444444-4444-4444-8444-444444444444"
    denied = _compliance_request(monkeypatch, server, {
        server.COMPLIANCE_TENANT_HEADER: other})
    assert denied.organization_id == ""
    assert denied.role == "brevitas_admin"


def test_compliance_workspace_fallback_can_be_switched_off(monkeypatch):
    import api.server as server

    monkeypatch.setattr(server, "_store", object())
    monkeypatch.setattr(server, "_dashboard_identity", lambda _request: {
        "id": ACTOR, "app_metadata": {"role": "brevitas_admin"},
    })
    monkeypatch.setattr(server, "_active_company_membership",
                        lambda _actor: (ORGANIZATION, "company_owner"))
    monkeypatch.delenv("BREVITAS_COMPLIANCE_TENANT_AUTHORITY_ONLY", raising=False)
    assert _compliance_request(monkeypatch, server, {}).organization_id == ORGANIZATION

    monkeypatch.setenv("BREVITAS_COMPLIANCE_TENANT_AUTHORITY_ONLY", "true")
    assert _compliance_request(monkeypatch, server, {}).organization_id == ""


def test_supabase_compliance_authority_is_decided_by_the_database():
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    calls: list[tuple[str, dict]] = []

    def request(method, path, **kwargs):
        calls.append((path, kwargs.get("data") or {}))
        return {"ok": True, "organization_id": ORGANIZATION}

    store._request = request  # type: ignore[method-assign]
    assert store.compliance_tenant_authority(ACTOR, ORGANIZATION) is True
    assert calls == [("rpc/compliance_tenant_authority", {
        "p_actor_user_id": ACTOR, "p_organization_id": ORGANIZATION,
    })]

    store._request = lambda *a, **k: {"ok": False, "code": "forbidden"}  # type: ignore
    assert store.compliance_tenant_authority(ACTOR, ORGANIZATION) is False


# ── membership probes ignore removed rows (finding: ensure_organization) ───────

def test_removed_member_can_still_bootstrap_their_own_workspace(tmp_path):
    from api.company_admin import company_admin_for_store

    store = UsageStore(str(tmp_path / "removed-member.db"))
    company_admin_for_store(store)
    employer = store.ensure_organization("founder-1", "Employer")
    with store._conn() as db:
        db.execute(
            "INSERT INTO organization_members(organization_id,user_id,role,status,"
            "created_at) VALUES(?,?,?,?,?)",
            (employer["id"], "engineer-1", "member", "removed", "2026-01-01T00:00:00Z"),
        )
    assert store.member_organization("engineer-1") is None
    own = store.ensure_organization("engineer-1", "Engineer workspace")
    assert own["id"] != employer["id"]
    assert store.member_organization("engineer-1")["id"] == own["id"]


def test_removal_from_a_self_created_workspace_is_not_silently_undone(tmp_path):
    from api.company_admin import company_admin_for_store

    store = UsageStore(str(tmp_path / "removed-owner.db"))
    company_admin_for_store(store)
    organization = store.ensure_organization("solo-1", "Solo")
    with store._conn() as db:
        db.execute(
            "UPDATE organization_members SET status='removed' "
            "WHERE organization_id=? AND user_id=?", (organization["id"], "solo-1"),
        )
    # legacy_owner_id is unique, so the workspace is reused rather than duplicated,
    # and the retained 'removed' row is left exactly as the admin set it.
    reused = store.ensure_organization("solo-1", "Solo")
    assert reused["id"] == organization["id"]
    assert store.member_organization("solo-1") is None
    with store._conn() as db:
        statuses = [row[0] for row in db.execute(
            "SELECT status FROM organization_members WHERE user_id=?", ("solo-1",),
        ).fetchall()]
    assert statuses == ["removed"]


def test_supabase_membership_probe_filters_status_and_orders_deterministically(
        monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    seen: list[dict] = []

    def request(method, path, **kwargs):
        if path == "organization_members":
            seen.append(kwargs.get("params") or {})
            return []
        return [{"id": ORGANIZATION, "name": "Company", "billing_owner_id": ACTOR,
                 "account_type": "company"}]

    store._request = request  # type: ignore[method-assign]
    store.ensure_organization(ACTOR, "Company")
    assert seen[0]["status"] == "eq.active"
    assert seen[0]["order"].startswith("created_at.asc")
    assert "company_owner" in seen[0]["role"]


# ── tenant inventory (finding: /v1/organization/inventory always 500s) ─────────

def test_organization_inventory_lists_only_current_members(tmp_path, monkeypatch):
    server, store, organization, user_id, raw_key, client = _dashboard_client(
        tmp_path, monkeypatch, name="inventory", role="owner")
    with store._conn() as db:
        db.execute(
            "INSERT INTO organization_members(organization_id,user_id,role,status,"
            "created_at) VALUES(?,?,?,?,?)",
            (organization["id"], "departed-1", "member", "removed",
             "2026-01-02T00:00:00Z"),
        )
    inventory = client.get("/v1/organization/inventory",
                           headers={"Authorization": "Bearer session"})
    assert inventory.status_code == 200
    body = inventory.json()
    assert body["counts"]["members"] == 1
    assert [member["user_id"] for member in body["members"]] == [user_id]
    assert body["keys_has_more"] is False
    assert body["keys_next_cursor"] == ""


def test_organization_inventory_reports_store_failure_as_retryable(
        tmp_path, monkeypatch):
    server, store, organization, user_id, raw_key, client = _dashboard_client(
        tmp_path, monkeypatch, name="inventory-down", role="owner")
    monkeypatch.setattr(
        store, "organization_inventory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Supabase key listing requires actor, request, and cursor")),
    )
    unavailable = client.get("/v1/organization/inventory",
                             headers={"Authorization": "Bearer session"})
    assert unavailable.status_code == 503


def test_hosted_inventory_threads_actor_context_into_the_key_page(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    captured: dict = {}

    def request(method, path, **kwargs):
        if path in ("organization_members", "devices", "customers", "installations"):
            return []
        raise AssertionError(f"unexpected call {method} {path}")

    store._request = request  # type: ignore[method-assign]
    monkeypatch.setattr(
        SupabaseUsageStore, "list_organization_keys_page",
        lambda self, organization_id, actor_user_id, **kwargs: captured.update(
            organization_id=organization_id, actor_user_id=actor_user_id, **kwargs,
        ) or {"keys": [], "next_cursor": "", "has_more": False, "limit": 50},
    )
    inventory = store.organization_inventory(
        ORGANIZATION, actor_user_id=ACTOR, request_id="request-inventory-001",
        actor_role="company_owner")
    assert captured["actor_user_id"] == ACTOR
    assert captured["request_id"] == "request-inventory-001"
    assert captured["actor_role"] == "company_owner"
    assert inventory["counts"]["keys"] == 0


# ── credential/policy writes are attributable (finding: unaudited writes) ──────

def test_warming_provider_and_cache_policy_writes_leave_an_audit_row(
        tmp_path, monkeypatch):
    server, store, organization, user_id, raw_key, client = _dashboard_client(
        tmp_path, monkeypatch, name="mutation-audit", role="owner")
    kms = LocalTestKMS(b"m" * 32, environ={"BREVITAS_ENV": "test"})
    monkeypatch.setattr(server, "_credential_cipher", EnvelopeCipher(
        kms, key_id="mutation-audit-key", key_version="1",
        wrap_algorithm=kms.algorithm))
    bearer = {"Authorization": "Bearer session"}

    assert client.put("/v1/warming", headers=bearer, json={
        "provider": "anthropic", "provider_api_key": "sk-warm-secret",
        "enabled": True, "accept_spend_terms": True, "daily_budget_usd": 5.0,
    }).status_code == 200
    assert client.put("/v1/cache-policy", headers=bearer,
                      json={"enabled": False}).status_code == 200
    assert client.delete("/v1/warming/anthropic", headers=bearer).status_code == 200
    assert client.put("/v1/provider", headers={"X-Brevitas-Key": raw_key}, json={
        "provider": "deepseek", "provider_api_key": "sk-provider-secret",
        "model": "deepseek-chat",
    }).status_code == 200

    with store._conn() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT action,target_type,target_id,actor_id,actor_role,details,outcome,"
            "organization_id FROM audit_events WHERE action LIKE 'warming.%' "
            "OR action LIKE 'cache%' OR action LIKE 'provider_credential.%' "
            "ORDER BY id",
        ).fetchall()]
    assert [row["action"] for row in rows] == [
        "warming.consent_granted", "cache_policy.changed", "cache.purged",
        "warming.credential_deleted", "provider_credential.updated",
    ]
    for row in rows:
        assert row["organization_id"] == organization["id"]
        assert row["details"] == "{}"
        assert row["outcome"] == "committed"
        assert row["actor_role"] == "company_owner"
        # Never the raw key hash: the audit trigger rejects 64-hex identifiers.
        assert len(row["target_id"]) <= 200
        assert hash_key(raw_key) not in row["target_id"]
        assert row["actor_id"] == user_id
    assert rows[-1]["target_id"].endswith(":deepseek")
    assert "sk-provider-secret" not in repr(rows)
    assert "sk-warm-secret" not in repr(rows)
