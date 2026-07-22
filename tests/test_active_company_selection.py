import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.company_admin import (
    CompanyAdminDenied,
    CompanyPrincipal,
    SQLiteCompanyAdminService,
    SupabaseCompanyAdminService,
    configure_company_admin,
    router,
)
from api.store import SupabaseUsageStore, UsageStore


def _setup(tmp_path):
    store = UsageStore(str(tmp_path / "active-company.db"))
    first = store.ensure_organization("actor-a", "First company")
    second = store.ensure_organization("owner-b", "Second company")
    foreign = store.ensure_organization("foreign-owner", "Foreign company")
    service = SQLiteCompanyAdminService(store.db_path, cursor_secret="c" * 40)
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE organization_members SET role='company_owner',status='active' "
            "WHERE user_id IN ('actor-a','owner-b','foreign-owner')",
        )
        db.execute(
            "INSERT INTO organization_members(organization_id,user_id,role,created_at,"
            "status,updated_at) VALUES(?,?,?,'2026-07-19T00:00:00+00:00','active',"
            "'2026-07-19T00:00:00+00:00')",
            (second["id"], "actor-a", "member"),
        )
    principal = CompanyPrincipal("actor-a", first["id"], "company_owner")
    return store, service, principal, first, second, foreign


def test_sqlite_selection_persists_only_live_actor_membership(tmp_path):
    store, service, principal, _, second, foreign = _setup(tmp_path)

    selected = service.select_active_company(
        principal, second["id"], "request-select-company",
    )

    assert selected == {"company_id": second["id"], "role": "member"}
    assert store.member_organization("actor-a") == {
        "id": second["id"],
        "name": "Second company",
        "role": "member",
        "billing_owner_id": "owner-b",
        "account_type": "company",
    }
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT organization_id FROM active_company_selections WHERE user_id='actor-a'",
        ).fetchone()[0] == second["id"]
        assert db.execute(
            "SELECT organization_id,action,target_id FROM audit_events "
            "WHERE request_id='request-select-company'",
        ).fetchone() == (
            second["id"], "company.active_selected", second["id"],
        )

    with pytest.raises(CompanyAdminDenied):
        service.select_active_company(
            principal, foreign["id"], "request-select-foreign",
        )
    assert store.member_organization("actor-a")["id"] == second["id"]


def test_stale_selection_falls_back_after_membership_is_disabled(tmp_path):
    store, service, principal, first, second, _ = _setup(tmp_path)
    service.select_active_company(principal, second["id"], "request-select-before-disable")
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE organization_members SET status='disabled' "
            "WHERE organization_id=? AND user_id='actor-a'",
            (second["id"],),
        )

    resolved = store.member_organization("actor-a")

    assert resolved["id"] == first["id"]
    assert resolved["role"] == "company_owner"
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT organization_id FROM active_company_selections WHERE user_id='actor-a'",
        ).fetchone()[0] == first["id"]


def test_http_switch_uses_injected_verified_principal_and_is_not_cached(tmp_path):
    _, service, principal, _, second, foreign = _setup(tmp_path)
    app = FastAPI()
    app.include_router(router)
    configure_company_admin(
        service, lambda _request: principal,
        lambda _request: "request-http-switch",
    )
    client = TestClient(app)

    spoofed = client.post("/v1/company/active", json={
        "company_id": second["id"], "actor_id": "foreign-owner",
    })
    assert spoofed.status_code == 422

    response = client.post("/v1/company/active", json={"company_id": second["id"]})

    assert response.status_code == 200
    assert response.json() == {"company_id": second["id"], "role": "member"}
    assert response.headers["cache-control"] == "private, no-store"
    denied = client.post("/v1/company/active", json={"company_id": foreign["id"]})
    assert denied.status_code == 403


def test_supabase_switch_and_resolution_are_actor_bound_rpc_calls():
    actor_id = "11111111-1111-4111-8111-111111111111"
    current = "22222222-2222-4222-8222-222222222222"
    requested = "33333333-3333-4333-8333-333333333333"
    calls = []

    class ServiceStore:
        def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs.get("data"), kwargs.get("params")))
            return {"ok": True, "company_id": requested, "role": "billing_admin"}

    service = SupabaseCompanyAdminService(
        ServiceStore(), cursor_secret="c" * 40, invitee_pepper="i" * 40,
    )
    result = service.select_active_company(
        CompanyPrincipal(actor_id, current, "member"), requested,
        "request-supabase-switch",
    )
    assert result == {"company_id": requested, "role": "billing_admin"}
    assert calls == [("POST", "rpc/company_admin_select_active_membership", {
        "p_actor_user_id": actor_id,
        "p_requested_organization_id": requested,
        "p_request_id": "request-supabase-switch",
    }, None)]

    class UnsafeStore:
        def _request(self, _method, _path, **_kwargs):
            return {"ok": True, "company_id": current, "role": "company_owner"}

    unsafe = SupabaseCompanyAdminService(
        UnsafeStore(), cursor_secret="c" * 40, invitee_pepper="i" * 40,
    )
    with pytest.raises(CompanyAdminDenied):
        unsafe.select_active_company(
            CompanyPrincipal(actor_id, current, "member"), requested,
            "request-unsafe-rpc-result",
        )

    store = SupabaseUsageStore("https://example.supabase.co", "service-role-test")
    store_calls = []

    def request(method, path, **kwargs):
        store_calls.append((method, path, kwargs))
        if path == "rpc/company_admin_resolve_active_membership":
            return {"ok": True, "company_id": requested, "role": "billing_admin"}
        return [{"id": requested, "name": "Resolved", "billing_owner_id": actor_id}]

    store._request = request
    assert store.member_organization(actor_id) == {
        "id": requested,
        "name": "Resolved",
        "billing_owner_id": actor_id,
        "role": "billing_admin",
    }
    assert store_calls[0] == (
        "POST", "rpc/company_admin_resolve_active_membership", {
            "data": {"p_actor_user_id": actor_id},
        },
    )


def test_forward_migration_keeps_selection_service_only_and_reauthorizes_actor():
    root = Path(__file__).parent.parent
    migration = (
        root / "supabase/migrations/202607170013_active_company_selection.sql"
    ).read_text().lower()
    compact = "".join(migration.split())

    assert "createtableifnotexistspublic.active_company_selections" in compact
    assert "foreignkey(organization_id,user_id)referencespublic.organization_members(organization_id,user_id)" in compact
    assert "altertablepublic.active_company_selectionsenablerowlevelsecurity" in compact
    assert "revokeallontablepublic.active_company_selectionsfrompublic,anon,authenticated,service_role" in compact
    assert "company_admin_resolve_active_membership(p_actor_user_iduuid)returnsjsonb" in compact
    assert "company_admin_select_active_membership(p_actor_user_iduuid,p_requested_organization_iduuid,p_request_idtext)returnsjsonb" in compact
    assert "member.user_id=p_actor_user_id" in compact
    assert "member.organization_id=p_requested_organization_id" in compact
    assert "member.status='active'" in compact
    assert "forupdate" in compact
    for signature in (
        "public.company_admin_resolve_active_membership(uuid)",
        "public.company_admin_select_active_membership(uuid,uuid,text)",
    ):
        assert f"revoke all on function {signature}" in migration
        assert f"grant execute on function {signature}" in migration


def test_existing_next_bff_forwards_the_active_company_post_safely():
    root = Path(__file__).parent.parent
    route = (root / "src/app/api/admin/[...path]/route.ts").read_text()
    proxy = (root / "src/lib/admin/proxy.ts").read_text()

    assert "export const POST = handler" in route
    assert "path[0] !== 'company'" in proxy
    assert "return `/v1/${path.map(encodeURIComponent).join('/')}`" in proxy
    assert "Authorization: authorization" in proxy
    assert "cache: 'no-store'" in proxy
