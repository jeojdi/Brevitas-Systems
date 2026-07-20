from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.compliance_admin import (
    ComplianceAdminPrincipal,
    SupabaseComplianceAdminService,
    configure_compliance_admin,
    router,
)


ORG = "00000000-0000-4000-8000-000000000101"
OTHER_ORG = "00000000-0000-4000-8000-000000000102"
ACTOR = "00000000-0000-4000-8000-000000000103"
REQUEST = "00000000-0000-4000-8000-000000000104"
SUBJECT = "00000000-0000-4000-8000-000000000105"
HOLD = "00000000-0000-4000-8000-000000000106"
HOLD_ACTION = "00000000-0000-4000-8000-000000000107"


@dataclass
class FakeService:
    calls: list[tuple[str, ComplianceAdminPrincipal, tuple[Any, ...]]] = field(default_factory=list)

    def _record(self, name, principal, *args):
        self.calls.append((name, principal, args))

    def submit(self, principal, request_id, request_type, scope, subject_id, evidence_reference):
        self._record("submit", principal, request_id, request_type, scope, subject_id, evidence_reference)
        return {"id": request_id, "status": "pending", "scope": scope}

    def status(self, principal, request_id):
        self._record("status", principal, request_id)
        return {"id": request_id, "status": "pending", "request_scope": "tenant"}

    def approve(self, principal, request_id):
        self._record("approve", principal, request_id)
        return {"id": request_id, "status": "approved"}

    def request_hold_action(self, principal, action_id, action, hold_id, scope,
                            reason_code, expires_at, audit_request_id):
        self._record("request_hold_action", principal, action_id, action, hold_id,
                     scope, reason_code, expires_at, audit_request_id)
        return {"id": action_id, "target_hold_id": hold_id, "action": action,
                "status": "pending"}

    def hold_action_status(self, principal, action_id):
        self._record("hold_action_status", principal, action_id)
        return {"id": action_id, "status": "pending"}

    def approve_hold_action(self, principal, action_id, audit_request_id):
        self._record("approve_hold_action", principal, action_id, audit_request_id)
        return {"id": action_id, "status": "approved"}


def client(service: FakeService, principal: ComplianceAdminPrincipal) -> TestClient:
    configure_compliance_admin(service, lambda _request: principal, lambda _request: "audit-request-0001")
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_router_derives_actor_and_tenant_and_rejects_body_authority():
    service = FakeService()
    principal = ComplianceAdminPrincipal(ACTOR, ORG, "brevitas_admin")
    api = client(service, principal)
    response = api.post("/v1/admin/compliance/requests", json={
        "request_id": REQUEST, "request_type": "export", "scope": "member",
        "subject_id": SUBJECT, "evidence_reference": "evidence:member:001",
    })
    assert response.status_code == 200
    call = service.calls[-1]
    assert call[0] == "submit"
    assert call[1] == principal
    assert call[2] == (REQUEST, "export", "member", SUBJECT, "evidence:member:001")

    injected = api.post("/v1/admin/compliance/requests", json={
        "request_id": REQUEST, "request_type": "delete", "scope": "tenant",
        "subject_id": None, "evidence_reference": "evidence:tenant:001",
        "organization_id": OTHER_ORG, "actor_id": "brevitas_admin:attacker",
    })
    assert injected.status_code == 422
    assert len(service.calls) == 1


def test_router_requires_exact_verified_brevitas_admin_role():
    for role in ("company_owner", "company_admin", "system", ""):
        service = FakeService()
        response = client(
            service, ComplianceAdminPrincipal(ACTOR, ORG, role),
        ).get(f"/v1/admin/compliance/requests/{REQUEST}")
        assert response.status_code == 403
        assert service.calls == []


def test_router_hold_actions_require_request_then_status_or_approval():
    service = FakeService()
    principal = ComplianceAdminPrincipal(ACTOR, ORG, "brevitas_admin")
    api = client(service, principal)
    assert api.post(f"/v1/admin/compliance/requests/{REQUEST}/approve").json()["status"] == "approved"
    assert api.post("/v1/admin/compliance/hold-actions", json={
        "action_id": HOLD_ACTION, "action": "create", "hold_id": HOLD,
        "scope": "delete", "reason_code": "legal_review",
    }).json()["status"] == "pending"
    assert api.get(
        f"/v1/admin/compliance/hold-actions/{HOLD_ACTION}"
    ).json()["status"] == "pending"
    assert api.post(
        f"/v1/admin/compliance/hold-actions/{HOLD_ACTION}/approve"
    ).json()["status"] == "approved"
    assert [call[0] for call in service.calls] == [
        "approve", "request_hold_action", "hold_action_status", "approve_hold_action",
    ]
    assert all(call[1] == principal for call in service.calls)
    assert api.post("/v1/admin/compliance/holds", json={}).status_code == 404
    assert api.post(f"/v1/admin/compliance/holds/{HOLD}/release").status_code == 404


def test_router_release_action_rejects_caller_supplied_hold_fields():
    service = FakeService()
    api = client(service, ComplianceAdminPrincipal(ACTOR, ORG, "brevitas_admin"))
    valid = api.post("/v1/admin/compliance/hold-actions", json={
        "action_id": HOLD_ACTION, "action": "release", "hold_id": HOLD,
    })
    assert valid.status_code == 200
    injected = api.post("/v1/admin/compliance/hold-actions", json={
        "action_id": HOLD_ACTION, "action": "release", "hold_id": HOLD,
        "scope": "all", "reason_code": "substituted",
    })
    assert injected.status_code == 422


class FakeStore:
    def __init__(self):
        self.calls = []

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET":
            requested_id = kwargs["params"]["id"].removeprefix("eq.")
            return [{"id": requested_id, "status": "pending"}]
        if path.endswith("compliance_submit_data_request"):
            return {"id": REQUEST, "status": "pending"}
        if path.endswith("compliance_approve_data_request"):
            return "approved"
        if path.endswith("compliance_request_legal_hold_action"):
            return {"id": HOLD_ACTION, "status": "pending"}
        if path.endswith("compliance_approve_legal_hold_action"):
            return {"id": HOLD_ACTION, "status": "approved"}
        raise AssertionError(path)


def test_supabase_service_binds_status_and_rpc_to_derived_tenant_actor():
    store = FakeStore()
    service = SupabaseComplianceAdminService(store)
    principal = ComplianceAdminPrincipal(ACTOR, ORG, "brevitas_admin")
    service.status(principal, REQUEST)
    params = store.calls[-1][2]["params"]
    assert params["organization_id"] == f"eq.{ORG}"
    assert params["id"] == f"eq.{REQUEST}"
    assert OTHER_ORG not in str(store.calls)

    service.submit(principal, REQUEST, "export", "tenant", None, "evidence:tenant:001")
    payload = store.calls[-1][2]["data"]
    assert payload["p_organization_id"] == ORG
    assert payload["p_actor_id"] == f"brevitas_admin:{ACTOR}"
    assert "organization_id" not in payload and "actor_id" not in payload

    service.request_hold_action(
        principal, HOLD_ACTION, "create", HOLD, "delete", "legal_review",
        None, "audit-request-0001",
    )
    payload = store.calls[-1][2]["data"]
    assert payload["p_organization_id"] == ORG
    assert payload["p_actor_id"] == f"brevitas_admin:{ACTOR}"
    service.hold_action_status(principal, HOLD_ACTION)
    params = store.calls[-1][2]["params"]
    assert params["organization_id"] == f"eq.{ORG}"
    assert params["id"] == f"eq.{HOLD_ACTION}"
    service.approve_hold_action(principal, HOLD_ACTION, "audit-request-0002")


def test_migration_enforces_distinct_submitter_approver_and_audited_role():
    migration = open(
        "supabase/migrations/202607170007_compliance_workflows.sql", encoding="utf-8"
    ).read()
    assertions = open("scripts/dr/compliance-workflow-assertions.sql", encoding="utf-8").read()
    assert "v_request.created_by=p_actor_id" in migration
    assert "two-person approval requires a distinct compliance administrator" in migration
    assert "public.compliance_actor_role(p_actor_id)" in migration
    assert "submitter approved their own compliance request" in assertions
    assert "two-person legal hold approval requires a distinct administrator" in migration
    assert "compliance_request_legal_hold_action" in migration
    assert "compliance_approve_legal_hold_action" in migration
    assert "compliance_create_legal_hold" not in migration.split(
        "-- Remove the former single-actor commit surface"
    )[0]
