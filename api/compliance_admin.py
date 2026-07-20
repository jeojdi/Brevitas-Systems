"""Authority-derived Brevitas compliance administration.

The composition root must inject a resolver backed by a verified Supabase
identity whose application role is exactly ``brevitas_admin``. Organization
and actor identifiers exist only on that principal; request bodies cannot
select either authority boundary.
"""
from __future__ import annotations

import inspect
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator


logger = logging.getLogger("brevitas.compliance_admin")
_OPAQUE = re.compile(r"^[A-Za-z0-9._:-]{3,96}$")
_EVIDENCE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


@dataclass(frozen=True, slots=True)
class ComplianceAdminPrincipal:
    actor_id: str
    organization_id: str
    role: str

    @property
    def audit_actor_id(self) -> str:
        return f"brevitas_admin:{self.actor_id}"


class ComplianceAdminService(Protocol):
    def submit(self, principal: ComplianceAdminPrincipal, request_id: str,
               request_type: str, scope: str, subject_id: str | None,
               evidence_reference: str) -> dict[str, Any]: ...
    def status(self, principal: ComplianceAdminPrincipal,
               request_id: str) -> dict[str, Any]: ...
    def approve(self, principal: ComplianceAdminPrincipal,
                request_id: str) -> dict[str, Any]: ...
    def request_hold_action(self, principal: ComplianceAdminPrincipal,
                            action_id: str, action: str, hold_id: str,
                            scope: str | None, reason_code: str | None,
                            expires_at: str | None,
                            audit_request_id: str) -> dict[str, Any]: ...
    def hold_action_status(self, principal: ComplianceAdminPrincipal,
                           action_id: str) -> dict[str, Any]: ...
    def approve_hold_action(self, principal: ComplianceAdminPrincipal,
                            action_id: str,
                            audit_request_id: str) -> dict[str, Any]: ...


class SupabaseComplianceAdminService:
    """Thin service-role adapter over migration-007 transactional RPCs."""

    def __init__(self, store: Any):
        if not hasattr(store, "_request"):
            raise RuntimeError("Compliance administration requires a Supabase store")
        self._store = store

    @staticmethod
    def _one(value: Any) -> Any:
        if isinstance(value, list):
            value = value[0] if value else None
        return value

    def _rpc(self, name: str, data: dict[str, Any]) -> Any:
        return self._one(self._store._request("POST", f"rpc/{name}", data=data))

    def submit(self, principal: ComplianceAdminPrincipal, request_id: str,
               request_type: str, scope: str, subject_id: str | None,
               evidence_reference: str) -> dict[str, Any]:
        common = {
            "p_organization_id": principal.organization_id,
            "p_request_id": request_id,
            "p_request_type": request_type,
            "p_actor_id": principal.audit_actor_id,
            "p_evidence_reference": evidence_reference,
        }
        if scope == "tenant":
            value = self._rpc("compliance_submit_data_request", common)
        else:
            value = self._rpc("compliance_submit_subject_request", {
                **common, "p_request_scope": scope, "p_subject_id": subject_id,
            })
        if not isinstance(value, dict):
            raise RuntimeError("Compliance submit RPC contract mismatch")
        return value

    def status(self, principal: ComplianceAdminPrincipal,
               request_id: str) -> dict[str, Any]:
        rows = self._store._request("GET", "data_subject_requests", params={
            "select": (
                "id,request_type,request_scope,subject_id,status,requested_at,due_at,"
                "approved_at,started_at,completed_at,deadline_breached"
            ),
            "organization_id": f"eq.{principal.organization_id}",
            "id": f"eq.{request_id}", "limit": "1",
        }) or []
        if not isinstance(rows, list) or not rows:
            raise KeyError("request_not_found")
        row = rows[0]
        if not isinstance(row, dict):
            raise RuntimeError("Compliance status contract mismatch")
        return row

    def approve(self, principal: ComplianceAdminPrincipal,
                request_id: str) -> dict[str, Any]:
        status = self._rpc("compliance_approve_data_request", {
            "p_organization_id": principal.organization_id,
            "p_request_id": request_id,
            "p_actor_id": principal.audit_actor_id,
        })
        if status not in {"approved", "processing", "completed"}:
            raise RuntimeError("Compliance approval RPC contract mismatch")
        return {"id": request_id, "status": status}

    def request_hold_action(self, principal: ComplianceAdminPrincipal,
                            action_id: str, action: str, hold_id: str,
                            scope: str | None, reason_code: str | None,
                            expires_at: str | None,
                            audit_request_id: str) -> dict[str, Any]:
        value = self._rpc("compliance_request_legal_hold_action", {
            "p_organization_id": principal.organization_id,
            "p_action_id": action_id, "p_action": action,
            "p_hold_id": hold_id, "p_scope": scope,
            "p_reason_code": reason_code,
            "p_actor_id": principal.audit_actor_id,
            "p_audit_request_id": audit_request_id,
            "p_expires_at": expires_at,
        })
        if not isinstance(value, dict) or value.get("id") != action_id \
                or value.get("status") not in {"pending", "approved"}:
            raise RuntimeError("Compliance hold request RPC contract mismatch")
        return value

    def hold_action_status(self, principal: ComplianceAdminPrincipal,
                           action_id: str) -> dict[str, Any]:
        rows = self._store._request("GET", "legal_hold_actions", params={
            "select": (
                "id,action,target_hold_id,scope,reason_code,expires_at,status,"
                "requested_by,requested_at,approved_by,approved_at"
            ),
            "organization_id": f"eq.{principal.organization_id}",
            "id": f"eq.{action_id}", "limit": "1",
        }) or []
        if not isinstance(rows, list) or not rows:
            raise KeyError("hold_action_not_found")
        row = rows[0]
        if not isinstance(row, dict):
            raise RuntimeError("Compliance hold action status contract mismatch")
        return row

    def approve_hold_action(self, principal: ComplianceAdminPrincipal,
                            action_id: str,
                            audit_request_id: str) -> dict[str, Any]:
        value = self._rpc("compliance_approve_legal_hold_action", {
            "p_organization_id": principal.organization_id,
            "p_action_id": action_id, "p_actor_id": principal.audit_actor_id,
            "p_audit_request_id": audit_request_id,
        })
        if not isinstance(value, dict) or value.get("id") != action_id \
                or value.get("status") != "approved":
            raise RuntimeError("Compliance hold approval RPC contract mismatch")
        return value


PrincipalResolver = Callable[
    [Request], ComplianceAdminPrincipal | Awaitable[ComplianceAdminPrincipal]
]
RequestIdResolver = Callable[[Request], str]
_service: ComplianceAdminService | None = None
_principal_resolver: PrincipalResolver | None = None
_request_id_resolver: RequestIdResolver | None = None


def configure_compliance_admin(
    service: ComplianceAdminService | None,
    principal_resolver: PrincipalResolver | None,
    request_id_resolver: RequestIdResolver | None = None,
) -> None:
    global _service, _principal_resolver, _request_id_resolver
    _service = service
    _principal_resolver = principal_resolver
    _request_id_resolver = request_id_resolver


@dataclass(frozen=True, slots=True)
class _Context:
    service: ComplianceAdminService
    principal: ComplianceAdminPrincipal
    audit_request_id: str


async def _context(request: Request) -> _Context:
    if _service is None or _principal_resolver is None:
        raise HTTPException(status_code=503, detail="Compliance administration unavailable")
    resolved = _principal_resolver(request)
    principal = await resolved if inspect.isawaitable(resolved) else resolved
    if not isinstance(principal, ComplianceAdminPrincipal) or principal.role != "brevitas_admin":
        raise HTTPException(status_code=403, detail="Compliance administrator role required")
    if (not _OPAQUE.fullmatch(principal.actor_id)
            or len(principal.audit_actor_id) > 110):
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        uuid.UUID(principal.organization_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=403, detail="Compliance tenant authority required") from exc
    audit_id = (_request_id_resolver(request) if _request_id_resolver
                else str(getattr(request.state, "brevitas_request_id", "")))
    if not _EVIDENCE.fullmatch(audit_id or ""):
        audit_id = str(uuid.uuid4())
    return _Context(_service, principal, audit_id)


class SubmitRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    request_type: str = Field(pattern=r"^(export|delete)$")
    scope: str = Field(pattern=r"^(tenant|member|customer)$")
    subject_id: uuid.UUID | None = None
    evidence_reference: str = Field(min_length=8, max_length=128,
                                    pattern=r"^[A-Za-z0-9._:-]+$")

    @model_validator(mode="after")
    def valid_scope(self) -> "SubmitRequestBody":
        if (self.scope == "tenant") != (self.subject_id is None):
            raise ValueError("subject binding does not match request scope")
        return self


class HoldActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: uuid.UUID
    action: str = Field(pattern=r"^(create|release)$")
    hold_id: uuid.UUID
    scope: str | None = Field(default=None, pattern=r"^(all|export|delete)$")
    reason_code: str | None = Field(default=None, min_length=3, max_length=64,
                                    pattern=r"^[a-z0-9_.-]+$")
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def valid_action(self) -> "HoldActionBody":
        create_fields = self.scope is not None and self.reason_code is not None
        if self.action == "create" and not create_fields:
            raise ValueError("create requires scope and reason_code")
        if self.action == "release" and (
            self.scope is not None or self.reason_code is not None
            or self.expires_at is not None
        ):
            raise ValueError("release derives hold fields from the target")
        return self


router = APIRouter(prefix="/v1/admin/compliance", tags=["compliance-administration"])


def _response(call: Callable[[], dict[str, Any]]) -> JSONResponse:
    try:
        value = call()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Compliance request not found") from exc
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail="Compliance workflow conflict") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("compliance_admin_dependency_failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Compliance administration unavailable") from exc
    return JSONResponse(value, headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })


@router.post("/requests")
def submit_request(body: SubmitRequestBody, context: _Context = Depends(_context)):
    return _response(lambda: context.service.submit(
        context.principal, str(body.request_id), body.request_type, body.scope,
        str(body.subject_id) if body.subject_id else None, body.evidence_reference))


@router.get("/requests/{request_id}")
def request_status(request_id: uuid.UUID, context: _Context = Depends(_context)):
    return _response(lambda: context.service.status(context.principal, str(request_id)))


@router.post("/requests/{request_id}/approve")
def approve_request(request_id: uuid.UUID, context: _Context = Depends(_context)):
    return _response(lambda: context.service.approve(context.principal, str(request_id)))


@router.post("/hold-actions")
def request_hold_action(body: HoldActionBody, context: _Context = Depends(_context)):
    expires_at = body.expires_at.isoformat() if body.expires_at else None
    return _response(lambda: context.service.request_hold_action(
        context.principal, str(body.action_id), body.action, str(body.hold_id),
        body.scope, body.reason_code, expires_at, context.audit_request_id))


@router.get("/hold-actions/{action_id}")
def hold_action_status(action_id: uuid.UUID, context: _Context = Depends(_context)):
    return _response(lambda: context.service.hold_action_status(
        context.principal, str(action_id)))


@router.post("/hold-actions/{action_id}/approve")
def approve_hold_action(action_id: uuid.UUID, context: _Context = Depends(_context)):
    return _response(lambda: context.service.approve_hold_action(
        context.principal, str(action_id), context.audit_request_id))


__all__ = [
    "ComplianceAdminPrincipal", "SupabaseComplianceAdminService",
    "configure_compliance_admin", "router",
]
