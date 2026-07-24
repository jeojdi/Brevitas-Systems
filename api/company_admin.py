"""Tenant-derived company administration with append-only audit evidence.

The router is deliberately unconfigured at import time. ``api.server`` must
inject its verified Supabase principal resolver and the store-backed service;
otherwise every endpoint fails closed with 503. Browser-supplied company IDs or
roles are never accepted.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.auth import generate_api_key, hash_key
from brevitas.observability import StructuredLogger
from brevitas.resource_bounds import BoundedTTLMap


logger = logging.getLogger("brevitas.company_admin")
_admin_telemetry = StructuredLogger("brevitas.api")
_admin_audit_telemetry_receipts = BoundedTTLMap[str, bool](
    ttl_s=15 * 60,
    max_entries=4096,
    max_key_bytes=128,
    max_value_bytes=16,
    max_total_bytes=64 * 1024,
)
_admin_audit_telemetry_lock = threading.Lock()

COMPANY_ROLES = ("company_owner", "company_admin", "member", "billing_admin")
MUTABLE_MEMBER_STATUSES = ("active", "disabled", "removed")
SERVICE_SCOPES = frozenset({
    "proxy:invoke", "usage:write", "usage:read_own", "customer:route",
    "customer:auto_provision", "customers:import", "repositories:register",
    "installations:register", "provider:read", "provider:manage",
    "jobs:create", "jobs:read", "jobs:cancel",
})
ROLE_PERMISSIONS: Mapping[str, frozenset[str]] = {
    "company_owner": frozenset({
        "company:read", "members:read", "members:invite", "members:manage",
        "owners:manage", "service_accounts:read", "service_accounts:manage",
        "billing:manage", "audit:read",
    }),
    "company_admin": frozenset({
        "company:read", "members:read", "members:invite", "members:manage",
        "service_accounts:read", "service_accounts:manage", "audit:read",
    }),
    "billing_admin": frozenset({
        "company:read", "members:read", "billing:manage", "audit:read",
    }),
    "member": frozenset({"company:read", "members:read"}),
}

_ROLE_ALIASES = {
    "owner": "company_owner", "admin": "company_admin", "billing": "billing_admin",
}
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_EMAIL = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,189}$")
_AUDIT_ROLES = frozenset({
    "company_owner", "company_admin", "member", "billing_admin",
    "brevitas_admin", "service_account", "system", "legacy", "none",
})
_CREDENTIAL_MATERIAL = re.compile(
    r"@|(^|[._:-])(?:(?:bvt|sk|rk|pk|whsec|sb_secret|xox[baprs]|gh[opusr])[_-]"
    r"|(?:secret|password|token|authorization|api[_-]?key)(?:[._:-]|$))",
    re.IGNORECASE,
)
PAGE_DEFAULT = 50
PAGE_MAX = 100
INVITATION_MAX = 100
SERVICE_ACCOUNT_MAX = 100
DASHBOARD_SESSION_MAX = 1000
DASHBOARD_SESSION_PER_ACTOR_MAX = 8
ACTIVE_COMPANY_MAX = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_role(role: str) -> str:
    return _ROLE_ALIASES.get(str(role or ""), str(role or ""))


def _emit_admin_audit_committed(request_id: str, outcome: str) -> None:
    """Best-effort, content-free, once-per-request audit telemetry."""
    try:
        if not _REQUEST_ID.fullmatch(str(request_id or "")):
            return
        fixed_outcome = "success" if outcome == "success" else "rejected"
        with _admin_audit_telemetry_lock:
            if _admin_audit_telemetry_receipts.get(request_id) is not None:
                return
            _admin_audit_telemetry_receipts.put(request_id, True)
        _admin_telemetry.info(
            "admin_audit_committed",
            outcome=fixed_outcome,
        )
    except Exception:
        # Audit persistence is authoritative; telemetry must never change the
        # administration result or cause a committed mutation to be retried.
        return


def _validated_company_choices(value: Any, active_company_id: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > ACTIVE_COMPANY_MAX:
        raise CompanyAdminDenied
    choices: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise CompanyAdminDenied
        company_id = str(item.get("company_id") or "")
        company_name = str(item.get("company_name") or "").strip()
        role = str(item.get("role") or "")
        account_type = str(item.get("account_type") or "")
        if (not _OPAQUE_ID.fullmatch(company_id) or company_id in seen
                or role not in COMPANY_ROLES
                or account_type not in {"individual", "company"}
                or not company_name or len(company_name) > 200
                or any(ord(character) < 32 for character in company_name)):
            raise CompanyAdminDenied
        seen.add(company_id)
        choices.append({
            "company_id": company_id,
            "company_name": company_name,
            "role": role,
            "account_type": account_type,
        })
    if active_company_id not in seen:
        raise CompanyAdminDenied
    return sorted(choices, key=lambda choice: (
        choice["company_id"] != active_company_id,
        choice["company_name"].casefold(),
        choice["company_id"],
    ))


def _validated_active_selection(value: Any, requested_company_id: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"company_id", "role"}:
        raise CompanyAdminDenied
    try:
        selected = str(uuid.UUID(str(value.get("company_id") or "")))
        requested = str(uuid.UUID(str(requested_company_id or "")))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CompanyAdminDenied from exc
    role = _canonical_role(str(value.get("role") or ""))
    if selected != requested or role not in COMPANY_ROLES:
        raise CompanyAdminDenied
    return {"company_id": selected, "role": role}


@dataclass(frozen=True)
class CompanyPrincipal:
    """Server-verified identity. HTTP input must never instantiate this directly."""

    actor_id: str
    company_id: str
    role: str
    # HMAC of the verified Supabase identity email. The raw address never enters
    # company-admin services, audit events, logs, or database RPC arguments.
    invitee_lookup_hash: str = ""


class CompanyAdminError(Exception):
    status_code = 400
    detail = "Company administration request failed"


class CompanyAdminDenied(CompanyAdminError):
    status_code = 403
    detail = "Company administration access denied"


class CompanyAdminNotFound(CompanyAdminError):
    status_code = 404
    detail = "Company administration resource not found"


class CompanyAdminConflict(CompanyAdminError):
    status_code = 409
    detail = "Company administration request conflicts with current state"


class CursorCodec:
    """HMAC authenticated keyset cursors that clients treat as opaque strings."""

    def __init__(self, secret: str | bytes):
        raw = secret.encode() if isinstance(secret, str) else bytes(secret)
        if len(raw) < 32:
            raw = hashlib.sha256(raw).digest()
        self._secret = raw

    def encode(self, company_id: str, kind: str, timestamp: str, row_id: str) -> str:
        payload = json.dumps({"v": 1, "c": company_id, "k": kind, "t": timestamp,
                              "i": row_id}, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")

    def decode(self, cursor: str, company_id: str, kind: str) -> tuple[str, str] | None:
        if not cursor:
            return None
        if len(cursor) > 512:
            raise ValueError("invalid pagination cursor")
        try:
            packed = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload, signature = packed[:-32], packed[-32:]
            expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            value = json.loads(payload)
            if value != {"v": 1, "c": company_id, "k": kind,
                         "t": value.get("t"), "i": value.get("i")}:
                raise ValueError
            timestamp, row_id = str(value["t"]), str(value["i"])
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if not _OPAQUE_ID.fullmatch(row_id):
                raise ValueError
            return timestamp, row_id
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid pagination cursor") from exc


class CompanyAdminService(Protocol):
    def invitee_lookup(self, verified_email: str) -> str: ...
    def capabilities(self, principal: CompanyPrincipal, request_id: str) -> dict[str, Any]: ...
    def select_active_company(self, principal: CompanyPrincipal, company_id: str,
                              request_id: str) -> dict[str, Any]: ...
    def list_members(self, principal: CompanyPrincipal, cursor: str, limit: int,
                     request_id: str) -> dict[str, Any]: ...
    def list_invitations(self, principal: CompanyPrincipal, cursor: str, limit: int,
                         request_id: str) -> dict[str, Any]: ...
    def invite_member(self, principal: CompanyPrincipal, email: str, role: str,
                      expires_in_hours: int, request_id: str) -> dict[str, Any]: ...
    def cancel_invitation(self, principal: CompanyPrincipal, invitation_id: str,
                          request_id: str) -> dict[str, Any]: ...
    def accept_invitation(self, principal: CompanyPrincipal, token: str,
                          request_id: str) -> dict[str, Any]: ...
    def change_member(self, principal: CompanyPrincipal, member_id: str, role: str,
                      status: str, request_id: str) -> dict[str, Any]: ...
    def list_service_accounts(self, principal: CompanyPrincipal, cursor: str, limit: int,
                              request_id: str) -> dict[str, Any]: ...
    def create_service_account(self, principal: CompanyPrincipal, name: str,
                               environment: str, scopes: list[str], expires_in_days: int,
                               request_id: str) -> dict[str, Any]: ...
    def rotate_service_key(self, principal: CompanyPrincipal, service_account_id: str,
                           expires_in_days: int, request_id: str) -> dict[str, Any]: ...
    def revoke_service_account(self, principal: CompanyPrincipal, service_account_id: str,
                               request_id: str) -> dict[str, Any]: ...
    def list_audit_events(self, principal: CompanyPrincipal, cursor: str, limit: int,
                          request_id: str) -> dict[str, Any]: ...
    def create_dashboard_session_key(self, principal: CompanyPrincipal, key_hash: str,
                                     key_prefix: str, expires_at: str,
                                     request_id: str) -> dict[str, Any]: ...
    def revoke_key(self, principal: CompanyPrincipal, key_id: str,
                   request_id: str) -> dict[str, Any]: ...


class SQLiteCompanyAdminService:
    """Transactional development/test implementation sharing UsageStore's SQLite file."""

    def __init__(self, db_path: str | Path, *, cursor_secret: str = "local-company-admin",
                 invitee_pepper: str = "local-company-invitee"):
        self.db_path = str(db_path)
        self._codec = CursorCodec(cursor_secret)
        self._pepper = hashlib.sha256(invitee_pepper.encode()).digest()
        self._init()

    def invitee_lookup(self, verified_email: str) -> str:
        normalized = verified_email.strip().lower()
        if not _EMAIL.fullmatch(normalized):
            raise ValueError("verified identity email is invalid")
        return hmac.new(self._pepper, normalized.encode(), hashlib.sha256).hexdigest()

    def _conn(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _add_columns(db: sqlite3.Connection, table: str,
                     definitions: Mapping[str, str]) -> None:
        existing = SQLiteCompanyAdminService._columns(db, table)
        for name, definition in definitions.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _init(self) -> None:
        with self._conn() as db:
            db.execute("CREATE TABLE IF NOT EXISTS organization_members (organization_id TEXT NOT NULL,user_id TEXT NOT NULL,role TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(organization_id,user_id))")
            self._add_columns(db, "organization_members", {
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
                "disabled_at": "TEXT NOT NULL DEFAULT ''",
                "removed_at": "TEXT NOT NULL DEFAULT ''",
            })
            db.execute("""UPDATE organization_members SET role=CASE role
                WHEN 'owner' THEN 'company_owner'
                WHEN 'admin' THEN 'company_admin'
                WHEN 'billing' THEN 'billing_admin'
                ELSE role END
                WHERE role IN ('owner','admin','billing')""")
            db.execute("""CREATE TABLE IF NOT EXISTS active_company_selections(
                user_id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(organization_id,user_id)
                    REFERENCES organization_members(organization_id,user_id)
                    ON DELETE CASCADE)""")
            db.execute("CREATE TABLE IF NOT EXISTS service_accounts (id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,name TEXT NOT NULL,environment TEXT NOT NULL,created_by TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,UNIQUE(organization_id,name,environment))")
            self._add_columns(db, "service_accounts", {
                "scopes": "TEXT NOT NULL DEFAULT 'proxy:invoke'",
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "expires_at": "TEXT NOT NULL DEFAULT ''",
                "revoked_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            })
            db.execute("""CREATE TABLE IF NOT EXISTS organization_invitations(
                id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
                email_lookup_hash TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL, status TEXT NOT NULL, invited_by TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                accepted_at TEXT NOT NULL DEFAULT '', cancelled_at TEXT NOT NULL DEFAULT '',
                accepted_by TEXT NOT NULL DEFAULT '')""")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS company_invitation_pending_idx ON organization_invitations(organization_id,email_lookup_hash) WHERE status='pending'")
            db.execute("CREATE INDEX IF NOT EXISTS company_invitation_page_idx ON organization_invitations(organization_id,created_at DESC,id DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS company_member_page_idx ON organization_members(organization_id,created_at DESC,user_id DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS company_service_page_idx ON service_accounts(organization_id,created_at DESC,id DESC)")
            db.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT,organization_id TEXT NOT NULL DEFAULT '',actor_user_id TEXT NOT NULL DEFAULT '',actor_key_hash TEXT NOT NULL DEFAULT '',action TEXT NOT NULL,target_type TEXT NOT NULL DEFAULT '',target_id TEXT NOT NULL DEFAULT '',details TEXT NOT NULL DEFAULT '{}',occurred_at TEXT NOT NULL)")
            self._add_columns(db, "audit_events", {
                "request_id": "TEXT NOT NULL DEFAULT 'legacy'",
                "actor_id": "TEXT NOT NULL DEFAULT 'system'",
                "actor_role": "TEXT NOT NULL DEFAULT 'legacy'",
                "outcome": "TEXT NOT NULL DEFAULT 'committed'",
            })
            db.execute("CREATE INDEX IF NOT EXISTS company_audit_page_idx ON audit_events(organization_id,occurred_at DESC,id DESC)")
            db.execute("DROP TRIGGER IF EXISTS audit_events_validate_insert")
            db.execute("DROP TRIGGER IF EXISTS audit_events_reject_update")
            db.execute("DROP TRIGGER IF EXISTS audit_events_reject_delete")
            db.execute("""CREATE TRIGGER audit_events_validate_insert BEFORE INSERT ON audit_events
                WHEN NEW.details<>'{}'
                  OR coalesce(NEW.actor_key_hash,'')<>''
                  OR length(NEW.request_id) NOT BETWEEN 8 AND 128
                  OR NEW.request_id GLOB '*[^A-Za-z0-9._:-]*'
                  OR length(NEW.actor_id) NOT BETWEEN 1 AND 128
                  OR NEW.actor_id GLOB '*[^A-Za-z0-9._:-]*'
                  OR NEW.actor_role NOT IN ('company_owner','company_admin','member','billing_admin','brevitas_admin','service_account','system','legacy','none')
                  OR length(NEW.target_id) NOT BETWEEN 1 AND 200
                  OR NEW.target_id GLOB '*[^A-Za-z0-9._:-]*'
                  OR instr(NEW.actor_id,'@')>0 OR instr(NEW.target_id,'@')>0
                  OR lower(NEW.actor_id) GLOB '*bvt_*' OR lower(NEW.target_id) GLOB '*bvt_*'
                  OR lower(NEW.actor_id) GLOB '*sk-*' OR lower(NEW.target_id) GLOB '*sk-*'
                  OR lower(NEW.actor_id) GLOB '*secret*' OR lower(NEW.target_id) GLOB '*secret*'
                  OR lower(NEW.actor_id) GLOB '*password*' OR lower(NEW.target_id) GLOB '*password*'
                  OR lower(NEW.actor_id) GLOB '*token*' OR lower(NEW.target_id) GLOB '*token*'
                  OR (length(NEW.actor_id)=64 AND lower(NEW.actor_id) NOT GLOB '*[^0-9a-f]*')
                  OR (length(NEW.target_id)=64 AND lower(NEW.target_id) NOT GLOB '*[^0-9a-f]*')
                BEGIN SELECT RAISE(ABORT,'audit event violates content-free schema'); END""")
            db.execute("""CREATE TRIGGER audit_events_reject_update BEFORE UPDATE ON audit_events
                BEGIN SELECT RAISE(ABORT,'audit_events is append-only'); END""")
            db.execute("""CREATE TRIGGER audit_events_reject_delete BEFORE DELETE ON audit_events
                BEGIN SELECT RAISE(ABORT,'audit_events is append-only'); END""")

    def _begin(self, db: sqlite3.Connection) -> None:
        db.execute("BEGIN IMMEDIATE")

    def _actor_role(self, db: sqlite3.Connection, principal: CompanyPrincipal) -> str:
        row = db.execute(
            "SELECT role,status FROM organization_members WHERE organization_id=? AND user_id=?",
            (principal.company_id, principal.actor_id),
        ).fetchone()
        if not row or str(row["status"] or "active") != "active":
            return ""
        return _canonical_role(str(row["role"]))

    @staticmethod
    def _audit(db: sqlite3.Connection, principal: CompanyPrincipal, actor_role: str,
               request_id: str, action: str, target_type: str, target_id: str,
               outcome: str) -> None:
        resolved_role = actor_role or "none"
        if (not _OPAQUE_ID.fullmatch(principal.actor_id)
                or len(principal.actor_id) > 128
                or not _OPAQUE_ID.fullmatch(target_id)
                or resolved_role not in _AUDIT_ROLES
                or re.fullmatch(r"[0-9a-fA-F]{64}", principal.actor_id)
                or re.fullmatch(r"[0-9a-fA-F]{64}", target_id)
                or _CREDENTIAL_MATERIAL.search(principal.actor_id)
                or _CREDENTIAL_MATERIAL.search(target_id)):
            raise ValueError("audit identifier violates content-free schema")
        db.execute(
            "INSERT INTO audit_events(organization_id,actor_user_id,action,target_type,target_id,details,occurred_at,request_id,actor_id,actor_role,outcome) VALUES(?,?,?,?,?,'{}',?,?,?,?,?)",
            (principal.company_id, principal.actor_id, action, target_type, target_id,
             _now(), request_id, principal.actor_id, resolved_role, outcome),
        )

    def _authorize(self, db: sqlite3.Connection, principal: CompanyPrincipal,
                   permission: str, request_id: str, action: str,
                   target_type: str, target_id: str) -> str:
        role = self._actor_role(db, principal)
        if permission not in ROLE_PERMISSIONS.get(role, frozenset()):
            self._audit(db, principal, role, request_id, f"{action}.denied",
                        target_type, target_id, "denied")
            db.commit()
            raise CompanyAdminDenied
        return role

    def _page(self, db: sqlite3.Connection, principal: CompanyPrincipal, *, table: str,
              id_column: str, fields: str, kind: str, cursor: str, limit: int,
              where: str = "", parameters: tuple[Any, ...] = ()) -> dict[str, Any]:
        decoded = self._codec.decode(cursor, principal.company_id, kind)
        page_limit = min(max(int(limit), 1), PAGE_MAX)
        clauses = ["organization_id=?"]
        values: list[Any] = [principal.company_id]
        if where:
            clauses.append(where)
            values.extend(parameters)
        if decoded:
            clauses.append(f"(created_at < ? OR (created_at = ? AND {id_column} < ?))")
            values.extend((decoded[0], decoded[0], decoded[1]))
        values.append(page_limit + 1)
        rows = db.execute(
            f"SELECT {fields},{id_column} AS __cursor_id,created_at FROM {table} WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC,{id_column} DESC LIMIT ?", values,
        ).fetchall()
        has_more = len(rows) > page_limit
        rows = rows[:page_limit]
        items = []
        for row in rows:
            item = dict(row)
            item.pop("__cursor_id", None)
            items.append(item)
        next_cursor = (self._codec.encode(principal.company_id, kind,
                                         str(rows[-1]["created_at"]),
                                         str(rows[-1]["__cursor_id"]))
                       if has_more and rows else "")
        return {"items": items, "next_cursor": next_cursor,
                "has_more": has_more, "limit": page_limit}

    def capabilities(self, principal: CompanyPrincipal, request_id: str) -> dict[str, Any]:
        with self._conn() as db:
            self._begin(db)
            role = self._authorize(db, principal, "company:read", request_id,
                                   "company.read", "company", principal.company_id)
            rows = db.execute(
                "SELECT member.organization_id AS company_id,"
                "substr(organization.name,1,200) AS company_name,member.role,"
                "organization.account_type "
                "FROM organization_members member JOIN organizations organization "
                "ON organization.id=member.organization_id "
                "WHERE member.user_id=? AND member.status='active' "
                "AND member.role IN ('company_owner','company_admin','member','billing_admin') "
                "ORDER BY CASE WHEN member.organization_id=? THEN 0 ELSE 1 END,"
                "lower(organization.name),member.organization_id LIMIT ?",
                (principal.actor_id, principal.company_id, ACTIVE_COMPANY_MAX),
            ).fetchall()
            companies = _validated_company_choices(
                [dict(row) for row in rows], principal.company_id)
            return {"company_id": principal.company_id, "role": role,
                    "permissions": sorted(ROLE_PERMISSIONS[role]),
                    "companies": companies}

    def select_active_company(self, principal: CompanyPrincipal, company_id: str,
                              request_id: str) -> dict[str, Any]:
        """Persist a requested target only after actor-bound membership validation."""
        with self._conn() as db:
            self._begin(db)
            row = db.execute(
                "SELECT role FROM organization_members "
                "WHERE organization_id=? AND user_id=? AND status='active' "
                "AND role IN ('company_owner','company_admin','member','billing_admin')",
                (company_id, principal.actor_id),
            ).fetchone()
            if not row:
                raise CompanyAdminDenied
            role = _canonical_role(str(row["role"] or ""))
            if role not in COMPANY_ROLES:
                raise CompanyAdminDenied
            db.execute(
                "INSERT INTO active_company_selections(user_id,organization_id,updated_at) "
                "VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "organization_id=excluded.organization_id,updated_at=excluded.updated_at",
                (principal.actor_id, company_id, _now()),
            )
            selected = CompanyPrincipal(principal.actor_id, company_id, role)
            self._audit(db, selected, role, request_id, "company.active_selected",
                        "company", company_id, "committed")
        return _validated_active_selection(
            {"company_id": company_id, "role": role}, company_id)

    def list_members(self, principal: CompanyPrincipal, cursor: str, limit: int,
                     request_id: str) -> dict[str, Any]:
        with self._conn() as db:
            self._begin(db)
            self._authorize(db, principal, "members:read", request_id,
                            "members.read", "company", principal.company_id)
            return self._page(db, principal, table="organization_members",
                              id_column="user_id", fields="user_id AS id,role,status",
                              kind="members", cursor=cursor, limit=limit)

    def list_invitations(self, principal: CompanyPrincipal, cursor: str, limit: int,
                         request_id: str) -> dict[str, Any]:
        with self._conn() as db:
            self._begin(db)
            self._authorize(db, principal, "members:invite", request_id,
                            "invitations.read", "company", principal.company_id)
            return self._page(db, principal, table="organization_invitations",
                              id_column="id", fields="id,role,status,expires_at",
                              kind="invitations", cursor=cursor, limit=limit)

    def invite_member(self, principal: CompanyPrincipal, email: str, role: str,
                      expires_in_hours: int, request_id: str) -> dict[str, Any]:
        lookup = self.invitee_lookup(email)
        token = "bvi_" + secrets.token_urlsafe(32)
        token_hash = hash_key(token)
        invitation_id = str(uuid.uuid4())
        created_at = _now()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)).isoformat()
        with self._conn() as db:
            self._begin(db)
            actor_role = self._authorize(db, principal, "members:invite", request_id,
                                         "member.invite", "invitation", invitation_id)
            if role not in ("company_admin", "member", "billing_admin"):
                self._audit(db, principal, actor_role, request_id, "member.invite.denied",
                            "invitation", invitation_id, "denied")
                db.commit()
                raise CompanyAdminDenied
            # BEGIN IMMEDIATE serializes the namespace. Expire before counting so
            # stale rows never consume the cap in a concurrent create race.
            db.execute(
                "UPDATE organization_invitations SET status='expired' WHERE organization_id=? AND status='pending' AND expires_at<=?",
                (principal.company_id, created_at),
            )
            pending = db.execute(
                "SELECT count(*) FROM organization_invitations WHERE organization_id=? AND status='pending' AND expires_at>?",
                (principal.company_id, created_at),
            ).fetchone()[0]
            if int(pending) >= INVITATION_MAX:
                self._audit(db, principal, actor_role, request_id, "member.invite.denied",
                            "invitation", invitation_id, "denied")
                db.commit()
                raise CompanyAdminConflict
            try:
                db.execute("INSERT INTO organization_invitations(id,organization_id,email_lookup_hash,token_hash,role,status,invited_by,created_at,expires_at) VALUES(?,?,?,?,?,'pending',?,?,?)",
                           (invitation_id, principal.company_id, lookup, token_hash, role,
                            principal.actor_id, created_at, expires_at))
            except sqlite3.IntegrityError as exc:
                self._audit(db, principal, actor_role, request_id, "member.invite.denied",
                            "invitation", invitation_id, "denied")
                db.commit()
                raise CompanyAdminConflict from exc
            self._audit(db, principal, actor_role, request_id, "member.invited",
                        "invitation", invitation_id, "committed")
        return {"id": invitation_id, "role": role, "status": "pending",
                "expires_at": expires_at, "invitation_token": token,
                "secret_available_once": True}

    def cancel_invitation(self, principal: CompanyPrincipal, invitation_id: str,
                          request_id: str) -> dict[str, Any]:
        with self._conn() as db:
            self._begin(db)
            role = self._authorize(db, principal, "members:invite", request_id,
                                   "member.invitation.cancel", "invitation", invitation_id)
            changed = db.execute(
                "UPDATE organization_invitations SET status='cancelled',cancelled_at=? WHERE organization_id=? AND id=? AND status='pending'",
                (_now(), principal.company_id, invitation_id),
            ).rowcount
            if not changed:
                self._audit(db, principal, role, request_id,
                            "member.invitation.cancel.denied", "invitation",
                            invitation_id, "denied")
                db.commit()
                raise CompanyAdminNotFound
            self._audit(db, principal, role, request_id, "member.invitation.cancelled",
                        "invitation", invitation_id, "committed")
        return {"id": invitation_id, "status": "cancelled"}

    def accept_invitation(self, principal: CompanyPrincipal, token: str,
                          request_id: str) -> dict[str, Any]:
        with self._conn() as db:
            self._begin(db)
            row = db.execute(
                "SELECT * FROM organization_invitations WHERE token_hash=?",
                (hash_key(token),),
            ).fetchone()
            if not row:
                db.rollback()
                raise CompanyAdminNotFound
            company_principal = CompanyPrincipal(
                principal.actor_id, str(row["organization_id"]), str(row["role"]),
                principal.invitee_lookup_hash,
            )
            if (row["status"] != "pending" or row["expires_at"] <= _now()
                    or not principal.invitee_lookup_hash
                    or not hmac.compare_digest(str(row["email_lookup_hash"]),
                                               principal.invitee_lookup_hash)):
                self._audit(db, company_principal, "none", request_id,
                            "member.invitation.accept.denied", "invitation",
                            str(row["id"]), "denied")
                db.commit()
                raise CompanyAdminDenied
            existing = db.execute(
                "SELECT role,status FROM organization_members WHERE organization_id=? AND user_id=?",
                (row["organization_id"], principal.actor_id),
            ).fetchone()
            if existing:
                self._audit(db, company_principal, _canonical_role(existing["role"]), request_id,
                            "member.invitation.accept.denied", "invitation",
                            str(row["id"]), "denied")
                db.commit()
                raise CompanyAdminConflict
            db.execute("INSERT INTO organization_members(organization_id,user_id,role,status,created_at,updated_at) VALUES(?,?,?,'active',?,?)",
                       (row["organization_id"], principal.actor_id, row["role"], _now(), _now()))
            db.execute("UPDATE organization_invitations SET status='accepted',accepted_at=?,accepted_by=? WHERE id=?",
                       (_now(), principal.actor_id, row["id"]))
            self._audit(db, company_principal, row["role"], request_id,
                        "member.invitation.accepted", "invitation", row["id"], "committed")
        return {"company_id": row["organization_id"], "role": row["role"],
                "status": "accepted"}

    def change_member(self, principal: CompanyPrincipal, member_id: str, role: str,
                      status: str, request_id: str) -> dict[str, Any]:
        with self._conn() as db:
            self._begin(db)
            actor_role = self._authorize(db, principal, "members:manage", request_id,
                                         "member.change", "member", member_id)
            target = db.execute(
                "SELECT role,status FROM organization_members WHERE organization_id=? AND user_id=?",
                (principal.company_id, member_id),
            ).fetchone()
            target_role = _canonical_role(target["role"]) if target else ""
            allowed = target is not None and role in COMPANY_ROLES and status in MUTABLE_MEMBER_STATUSES
            if actor_role == "company_admin" and (
                    target_role in ("company_owner", "company_admin") or
                    role in ("company_owner", "company_admin")):
                allowed = False
            if not allowed:
                self._audit(db, principal, actor_role, request_id, "member.change.denied",
                            "member", member_id, "denied")
                db.commit()
                raise CompanyAdminDenied
            if target_role == "company_owner" and (role != "company_owner" or status != "active"):
                owners = db.execute(
                    "SELECT count(*) FROM organization_members WHERE organization_id=? AND role IN ('owner','company_owner') AND status='active'",
                    (principal.company_id,),
                ).fetchone()[0]
                if int(owners) <= 1:
                    self._audit(db, principal, actor_role, request_id,
                                "member.change.denied", "member", member_id, "denied")
                    db.commit()
                    raise CompanyAdminConflict
            now = _now()
            db.execute("UPDATE organization_members SET role=?,status=?,updated_at=?,disabled_at=?,removed_at=? WHERE organization_id=? AND user_id=?",
                       (role, status, now, now if status == "disabled" else "",
                        now if status == "removed" else "", principal.company_id, member_id))
            self._audit(db, principal, actor_role, request_id, "member.changed",
                        "member", member_id, "committed")
        return {"id": member_id, "role": role, "status": status}

    def list_service_accounts(self, principal: CompanyPrincipal, cursor: str, limit: int,
                              request_id: str) -> dict[str, Any]:
        with self._conn() as db:
            self._begin(db)
            self._authorize(db, principal, "service_accounts:read", request_id,
                            "service_accounts.read", "company", principal.company_id)
            page = self._page(db, principal, table="service_accounts", id_column="id",
                              fields="id,name,environment,scopes,status,expires_at,revoked_at",
                              kind="service_accounts", cursor=cursor, limit=limit)
            for item in page["items"]:
                item["scopes"] = [scope for scope in str(item["scopes"] or "").split(",") if scope]
                if item.get("expires_at") and str(item["expires_at"]) <= _now():
                    item["status"] = "revoked"
            return page

    def create_service_account(self, principal: CompanyPrincipal, name: str,
                               environment: str, scopes: list[str], expires_in_days: int,
                               request_id: str) -> dict[str, Any]:
        account_id = str(uuid.uuid4())
        key_id = str(uuid.uuid4())
        raw_key = generate_api_key()
        key_hash = hash_key(raw_key)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat()
        with self._conn() as db:
            self._begin(db)
            actor_role = self._authorize(db, principal, "service_accounts:manage", request_id,
                                         "service_account.create", "service_account", account_id)
            now = _now()
            expired_ids = [row[0] for row in db.execute(
                "SELECT id FROM service_accounts WHERE organization_id=? AND status='active' AND expires_at<>'' AND expires_at<=?",
                (principal.company_id, now),
            ).fetchall()]
            if expired_ids:
                placeholders = ",".join("?" for _ in expired_ids)
                db.execute(
                    f"UPDATE service_accounts SET status='revoked',revoked_at=?,updated_at=? WHERE id IN ({placeholders})",
                    (now, now, *expired_ids),
                )
                db.execute(
                    f"UPDATE api_keys SET revoked_at=? WHERE service_account_id IN ({placeholders}) AND revoked_at=''",
                    (now, *expired_ids),
                )
            count = db.execute(
                "SELECT count(*) FROM service_accounts WHERE organization_id=? AND status='active' AND (expires_at='' OR expires_at>?)",
                (principal.company_id, now),
            ).fetchone()[0]
            if int(count) >= SERVICE_ACCOUNT_MAX or not scopes or not set(scopes) <= SERVICE_SCOPES:
                self._audit(db, principal, actor_role, request_id,
                            "service_account.create.denied", "service_account",
                            account_id, "denied")
                db.commit()
                raise CompanyAdminConflict
            organization = db.execute(
                "SELECT billing_owner_id FROM organizations WHERE id=?",
                (principal.company_id,),
            ).fetchone()
            if not organization or not organization["billing_owner_id"]:
                self._audit(db, principal, actor_role, request_id,
                            "service_account.create.denied", "service_account",
                            account_id, "denied")
                db.commit()
                raise CompanyAdminConflict
            try:
                db.execute("INSERT INTO service_accounts(id,organization_id,name,environment,created_by,created_at,scopes,status,expires_at,updated_at) VALUES(?,?,?,?,?, ?,?,'active',?,?)",
                           (account_id, principal.company_id, name, environment,
                            principal.actor_id, now, ",".join(sorted(set(scopes))),
                            expires_at, now))
                db.execute(
                    "INSERT INTO api_keys(id,key_hash,name,created,owner_id,"
                    "organization_id,service_account_id,key_type,scopes,environment,"
                    "key_prefix,expires_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key_id, key_hash, name, now, organization["billing_owner_id"],
                     principal.company_id, account_id, "organization_service",
                     ",".join(sorted(set(scopes))), environment, raw_key[:12],
                     expires_at, principal.actor_id),
                )
            except sqlite3.IntegrityError as exc:
                # A key collision or account uniqueness conflict must not leave
                # a machine identity without its initial credential.
                db.rollback()
                self._begin(db)
                self._audit(db, principal, actor_role, request_id,
                            "service_account.create.denied", "service_account",
                            account_id, "denied")
                db.commit()
                raise CompanyAdminConflict from exc
            self._audit(db, principal, actor_role, request_id, "service_account.created",
                        "service_account", account_id, "committed")
        return {"id": account_id, "name": name, "environment": environment,
                "scopes": sorted(set(scopes)), "status": "active", "expires_at": expires_at,
                "key_id": key_id, "api_key": raw_key, "prefix": raw_key[:12],
                "secret_available_once": True}

    def rotate_service_key(self, principal: CompanyPrincipal, service_account_id: str,
                           expires_in_days: int, request_id: str) -> dict[str, Any]:
        raw_key = generate_api_key()
        key_hash = hash_key(raw_key)
        key_id = str(uuid.uuid4())
        requested_expiry = datetime.now(timezone.utc) + timedelta(days=expires_in_days)
        with self._conn() as db:
            self._begin(db)
            actor_role = self._authorize(db, principal, "service_accounts:manage", request_id,
                                         "service_key.rotate", "service_account",
                                         service_account_id)
            account = db.execute(
                "SELECT account.name,account.environment,account.scopes,account.status,"
                "account.expires_at,organization.billing_owner_id "
                "FROM service_accounts account JOIN organizations organization "
                "ON organization.id=account.organization_id "
                "WHERE account.organization_id=? AND account.id=?",
                (principal.company_id, service_account_id),
            ).fetchone()
            account_expiry = (datetime.fromisoformat(str(account["expires_at"]).replace("Z", "+00:00"))
                              if account and account["expires_at"] else None)
            if (not account or not account["billing_owner_id"]
                    or account["status"] != "active" or
                    (account_expiry is not None and account_expiry <= datetime.now(timezone.utc))):
                self._audit(db, principal, actor_role, request_id,
                            "service_key.rotate.denied", "service_account",
                            service_account_id, "denied")
                db.commit()
                raise CompanyAdminNotFound
            expires_at = min(requested_expiry, account_expiry).isoformat() if account_expiry else requested_expiry.isoformat()
            db.execute("UPDATE api_keys SET revoked_at=? WHERE organization_id=? AND service_account_id=? AND revoked_at=''",
                       (_now(), principal.company_id, service_account_id))
            db.execute("INSERT INTO api_keys(id,key_hash,name,created,owner_id,organization_id,service_account_id,key_type,scopes,environment,key_prefix,expires_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (key_id, key_hash, account["name"], _now(),
                        account["billing_owner_id"],
                        principal.company_id, service_account_id, "organization_service",
                        account["scopes"], account["environment"], raw_key[:12],
                        expires_at, principal.actor_id))
            self._audit(db, principal, actor_role, request_id, "service_key.rotated",
                        "service_account", service_account_id, "committed")
        return {"key_id": key_id, "api_key": raw_key, "prefix": raw_key[:12],
                "expires_at": expires_at, "secret_available_once": True}

    def revoke_service_account(self, principal: CompanyPrincipal, service_account_id: str,
                               request_id: str) -> dict[str, Any]:
        with self._conn() as db:
            self._begin(db)
            actor_role = self._authorize(db, principal, "service_accounts:manage", request_id,
                                         "service_account.revoke", "service_account",
                                         service_account_id)
            changed = db.execute("UPDATE service_accounts SET status='revoked',revoked_at=?,updated_at=? WHERE organization_id=? AND id=? AND status='active'",
                                 (_now(), _now(), principal.company_id, service_account_id)).rowcount
            if not changed:
                self._audit(db, principal, actor_role, request_id,
                            "service_account.revoke.denied", "service_account",
                            service_account_id, "denied")
                db.commit()
                raise CompanyAdminNotFound
            db.execute("UPDATE api_keys SET revoked_at=? WHERE organization_id=? AND service_account_id=? AND revoked_at=''",
                       (_now(), principal.company_id, service_account_id))
            self._audit(db, principal, actor_role, request_id, "service_account.revoked",
                        "service_account", service_account_id, "committed")
        return {"id": service_account_id, "status": "revoked"}

    def list_audit_events(self, principal: CompanyPrincipal, cursor: str, limit: int,
                          request_id: str) -> dict[str, Any]:
        decoded = self._codec.decode(cursor, principal.company_id, "audit_events")
        page_limit = min(max(int(limit), 1), PAGE_MAX)
        with self._conn() as db:
            self._begin(db)
            self._authorize(db, principal, "audit:read", request_id,
                            "audit.read", "company", principal.company_id)
            values: list[Any] = [principal.company_id]
            clause = ""
            if decoded:
                clause = " AND (occurred_at < ? OR (occurred_at = ? AND id < ?))"
                values.extend((decoded[0], decoded[0], int(decoded[1])))
            values.append(page_limit + 1)
            rows = db.execute(
                "SELECT id,request_id,actor_id,actor_role,action,target_type,target_id,outcome,occurred_at FROM audit_events WHERE organization_id=?" + clause + " ORDER BY occurred_at DESC,id DESC LIMIT ?",
                values,
            ).fetchall()
            has_more = len(rows) > page_limit
            rows = rows[:page_limit]
            next_cursor = (self._codec.encode(principal.company_id, "audit_events",
                                              rows[-1]["occurred_at"], str(rows[-1]["id"]))
                           if has_more and rows else "")
            return {"items": [dict(row) for row in rows], "next_cursor": next_cursor,
                    "has_more": has_more, "limit": page_limit}

    def create_dashboard_session_key(self, principal: CompanyPrincipal, key_hash: str,
                                     key_prefix: str, expires_at: str,
                                     request_id: str) -> dict[str, Any]:
        key_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CompanyAdminConflict from exc
        with self._conn() as db:
            self._begin(db)
            actor_role = self._authorize(
                db, principal, "company:read", request_id,
                "dashboard_session.create", "company", principal.company_id)
            valid = (
                re.fullmatch(r"[0-9a-f]{64}", key_hash)
                and re.fullmatch(r"bvt_[A-Za-z0-9_-]{4,12}", key_prefix)
                and now < expiry <= now + timedelta(hours=8)
            )
            if not valid:
                self._audit(db, principal, actor_role, request_id,
                            "dashboard_session.create.denied", "company",
                            principal.company_id, "denied")
                db.commit()
                raise CompanyAdminConflict
            now_text = now.isoformat()
            db.execute(
                "UPDATE api_keys SET revoked_at=? WHERE organization_id=? "
                "AND key_type='dashboard_session' AND revoked_at='' "
                "AND (expires_at='' OR expires_at<=?)",
                (now_text, principal.company_id, now_text),
            )
            if db.execute("SELECT 1 FROM api_keys WHERE key_hash=?", (key_hash,)).fetchone():
                self._audit(db, principal, actor_role, request_id,
                            "dashboard_session.create.denied", "company",
                            principal.company_id, "denied")
                db.commit()
                raise CompanyAdminConflict
            active, actor_active = db.execute(
                "SELECT count(*),sum(CASE WHEN created_by=? THEN 1 ELSE 0 END) "
                "FROM api_keys WHERE organization_id=? AND key_type='dashboard_session' "
                "AND revoked_at='' AND expires_at>?",
                (principal.actor_id, principal.company_id, now_text),
            ).fetchone()
            active_count = int(active or 0)
            actor_active_count = int(actor_active or 0)
            revoke_count = max(
                actor_active_count - (DASHBOARD_SESSION_PER_ACTOR_MAX - 1),
                active_count - (DASHBOARD_SESSION_MAX - 1),
                0,
            )
            if revoke_count > actor_active_count:
                self._audit(db, principal, actor_role, request_id,
                            "dashboard_session.create.denied", "company",
                            principal.company_id, "denied")
                db.commit()
                raise CompanyAdminConflict
            rotated = db.execute(
                "SELECT id FROM api_keys WHERE organization_id=? "
                "AND key_type='dashboard_session' AND created_by=? "
                "AND revoked_at='' AND expires_at>? ORDER BY created,id LIMIT ?",
                (principal.company_id, principal.actor_id, now_text, revoke_count),
            ).fetchall()
            for row in rotated:
                db.execute(
                    "UPDATE api_keys SET revoked_at=? WHERE organization_id=? "
                    "AND id=? AND key_type='dashboard_session' AND created_by=? "
                    "AND revoked_at=''",
                    (now_text, principal.company_id, row["id"], principal.actor_id),
                )
                self._audit(db, principal, actor_role, request_id,
                            "dashboard_session.rotated", "api_key", row["id"],
                            "committed")
            scopes = ["proxy:invoke", "usage:read_own", "provider:read", "provider:manage"]
            db.execute(
                "INSERT INTO api_keys(id,key_hash,name,created,owner_id,organization_id,"
                "service_account_id,key_type,scopes,environment,key_prefix,expires_at,created_by) "
                "VALUES(?,?,?,?,?,?,'',?,?,?,?,?,?)",
                (key_id, key_hash, "dashboard session", now_text, principal.actor_id,
                 principal.company_id, "dashboard_session", ",".join(scopes), "dashboard",
                 key_prefix, expiry.isoformat(), principal.actor_id),
            )
            self._audit(db, principal, actor_role, request_id,
                        "dashboard_session.created", "api_key", key_id, "committed")
        return {"key_id": key_id, "organization_id": principal.company_id,
                "key_type": "dashboard_session", "scopes": scopes,
                "environment": "dashboard", "prefix": key_prefix,
                "expires_at": expiry.isoformat()}

    def revoke_key(self, principal: CompanyPrincipal, key_id: str,
                   request_id: str) -> dict[str, Any]:
        with self._conn() as db:
            self._begin(db)
            actor_role = self._actor_role(db, principal)
            row = db.execute(
                "SELECT id,key_type,created_by,revoked_at FROM api_keys "
                "WHERE organization_id=? AND id=?",
                (principal.company_id, key_id),
            ).fetchone()
            allowed = bool(
                row and row["key_type"] == "dashboard_session"
                and (
                    actor_role in ("company_owner", "company_admin")
                    or (actor_role in ("member", "billing_admin")
                        and row["created_by"] == principal.actor_id)
                )
            )
            if not row or not allowed:
                self._audit(db, principal, actor_role, request_id,
                            "dashboard_session.revoke.denied", "api_key",
                            key_id, "denied")
                db.commit()
                raise CompanyAdminDenied
            if row["revoked_at"]:
                self._audit(db, principal, actor_role, request_id,
                            "dashboard_session.revoke.noop", "api_key",
                            key_id, "committed")
                return {"key_id": key_id, "revoked": False, "already_revoked": True}
            db.execute(
                "UPDATE api_keys SET revoked_at=? WHERE organization_id=? AND id=?",
                (_now(), principal.company_id, key_id),
            )
            self._audit(db, principal, actor_role, request_id,
                        "dashboard_session.revoked", "api_key", key_id, "committed")
        return {"key_id": key_id, "revoked": True, "already_revoked": False}


class SupabaseCompanyAdminService:
    """Service-role implementation using the transaction-safe RPCs from migration 005."""

    def __init__(self, store: Any, *, cursor_secret: str, invitee_pepper: str):
        self._store = store
        self._codec = CursorCodec(cursor_secret)
        self._pepper = hashlib.sha256(invitee_pepper.encode()).digest()

    def invitee_lookup(self, verified_email: str) -> str:
        normalized = verified_email.strip().lower()
        if not _EMAIL.fullmatch(normalized):
            raise ValueError("verified identity email is invalid")
        return hmac.new(self._pepper, normalized.encode(), hashlib.sha256).hexdigest()

    def _rpc(self, name: str, data: dict[str, Any]) -> Any:
        return self._store._request("POST", f"rpc/{name}", data=data)

    def _role(self, principal: CompanyPrincipal) -> str:
        value = self._rpc("lock_company_actor_role", {
            "p_organization_id": principal.company_id,
            "p_actor_user_id": principal.actor_id,
        })
        if isinstance(value, list):
            value = value[0] if value else ""
            if isinstance(value, dict):
                value = next(iter(value.values()), "")
        return _canonical_role(str(value or ""))

    def _require(self, principal: CompanyPrincipal, permission: str, request_id: str,
                 action: str) -> str:
        # A newly confirmed user has no active company until onboarding creates
        # one. Deny before calling UUID-typed RPCs with an empty company ID so
        # the API returns the expected 403 and the dashboard can start onboarding.
        if not principal.company_id:
            raise CompanyAdminDenied
        role = self._role(principal)
        if permission not in ROLE_PERMISSIONS.get(role, frozenset()):
            self._rpc("append_company_audit", {
                "p_organization_id": principal.company_id,
                "p_actor_id": principal.actor_id,
                "p_actor_role": role or "none",
                "p_request_id": request_id,
                "p_action": f"{action}.denied",
                "p_target_type": "company",
                "p_target_id": principal.company_id,
                "p_outcome": "denied",
            })
            raise CompanyAdminDenied
        return role

    @staticmethod
    def _result(value: Any) -> dict[str, Any]:
        if isinstance(value, list):
            value = value[0] if value else {}
        if not isinstance(value, dict) or not value.get("ok"):
            code = str(value.get("code") if isinstance(value, dict) else "")
            if code in {"not_found", "invalid_invitation"}:
                raise CompanyAdminNotFound
            if code in {"duplicate", "already_invited", "last_owner", "limit",
                        "existing_member"}:
                raise CompanyAdminConflict
            raise CompanyAdminDenied
        return {key: item for key, item in value.items() if key != "ok"}

    def _rpc_page(self, principal: CompanyPrincipal, *, rpc: str, kind: str,
                  cursor: str, limit: int, request_id: str,
                  id_field: str = "id") -> dict[str, Any]:
        decoded = self._codec.decode(cursor, principal.company_id, kind)
        page_limit = min(max(int(limit), 1), PAGE_MAX)
        value = self._result(self._rpc(rpc, {
            "p_organization_id": principal.company_id,
            "p_actor_user_id": principal.actor_id,
            "p_cursor_time": decoded[0] if decoded else None,
            "p_cursor_id": decoded[1] if decoded else None,
            "p_limit": page_limit,
            "p_request_id": request_id,
        }))
        rows = value.get("items") or []
        has_more = len(rows) > page_limit
        rows = rows[:page_limit]
        next_cursor = (self._codec.encode(principal.company_id, kind,
                                         rows[-1]["created_at"], str(rows[-1][id_field]))
                       if has_more and rows else "")
        return {"items": rows, "next_cursor": next_cursor,
                "has_more": has_more, "limit": page_limit}

    def capabilities(self, principal: CompanyPrincipal, request_id: str) -> dict[str, Any]:
        role = self._require(principal, "company:read", request_id, "company.read")
        value = self._result(self._rpc("company_admin_active_memberships", {
            "p_actor_user_id": principal.actor_id,
            "p_active_organization_id": principal.company_id,
        }))
        companies = _validated_company_choices(
            value.get("items"), principal.company_id)
        return {"company_id": principal.company_id, "role": role,
                "permissions": sorted(ROLE_PERMISSIONS[role]),
                "companies": companies}

    def select_active_company(self, principal: CompanyPrincipal, company_id: str,
                              request_id: str) -> dict[str, Any]:
        result = self._result(self._rpc("company_admin_select_active_membership", {
            "p_actor_user_id": principal.actor_id,
            "p_requested_organization_id": company_id,
            "p_request_id": request_id,
        }))
        return _validated_active_selection(result, company_id)

    def list_members(self, principal: CompanyPrincipal, cursor: str, limit: int,
                     request_id: str) -> dict[str, Any]:
        return self._rpc_page(
            principal, rpc="company_admin_members_page", kind="members",
            cursor=cursor, limit=limit, request_id=request_id)

    def list_invitations(self, principal: CompanyPrincipal, cursor: str, limit: int,
                         request_id: str) -> dict[str, Any]:
        return self._rpc_page(
            principal, rpc="company_admin_invitations_page", kind="invitations",
            cursor=cursor, limit=limit, request_id=request_id)

    def invite_member(self, principal: CompanyPrincipal, email: str, role: str,
                      expires_in_hours: int, request_id: str) -> dict[str, Any]:
        token = "bvi_" + secrets.token_urlsafe(32)
        lookup = self.invitee_lookup(email)
        invitation_id = str(uuid.uuid4())
        result = self._result(self._rpc("company_admin_invite_member", {
            "p_organization_id": principal.company_id, "p_actor_user_id": principal.actor_id,
            "p_invitation_id": invitation_id,
            "p_role": role, "p_email_lookup_hash": lookup, "p_token_hash": hash_key(token),
            "p_expires_at": (datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)).isoformat(),
            "p_request_id": request_id,
        }))
        return {**result, "invitation_token": token, "secret_available_once": True}

    def cancel_invitation(self, principal: CompanyPrincipal, invitation_id: str,
                          request_id: str) -> dict[str, Any]:
        return self._result(self._rpc("company_admin_cancel_invitation", {
            "p_organization_id": principal.company_id, "p_actor_user_id": principal.actor_id,
            "p_invitation_id": invitation_id, "p_request_id": request_id,
        }))

    def accept_invitation(self, principal: CompanyPrincipal, token: str,
                          request_id: str) -> dict[str, Any]:
        if not principal.invitee_lookup_hash:
            raise CompanyAdminDenied
        result = self._result(self._rpc("company_admin_accept_invitation", {
            "p_actor_user_id": principal.actor_id,
            "p_invitee_lookup_hash": principal.invitee_lookup_hash,
            "p_token_hash": hash_key(token),
            "p_request_id": request_id,
        }))
        return {
            "company_id": str(result.get("organization_id") or ""),
            "role": str(result.get("role") or ""),
            "status": "accepted",
        }

    def change_member(self, principal: CompanyPrincipal, member_id: str, role: str,
                      status: str, request_id: str) -> dict[str, Any]:
        return self._result(self._rpc("company_admin_set_member", {
            "p_organization_id": principal.company_id, "p_actor_user_id": principal.actor_id,
            "p_target_user_id": member_id, "p_role": role, "p_status": status,
            "p_request_id": request_id,
        }))

    def list_service_accounts(self, principal: CompanyPrincipal, cursor: str, limit: int,
                              request_id: str) -> dict[str, Any]:
        return self._rpc_page(
            principal, rpc="company_admin_service_accounts_page", kind="service_accounts",
            cursor=cursor, limit=limit, request_id=request_id)

    def create_service_account(self, principal: CompanyPrincipal, name: str,
                               environment: str, scopes: list[str], expires_in_days: int,
                               request_id: str) -> dict[str, Any]:
        service_account_id = str(uuid.uuid4())
        raw_key = generate_api_key()
        result = self._result(self._rpc("company_admin_create_service_account", {
            "p_organization_id": principal.company_id, "p_actor_user_id": principal.actor_id,
            "p_service_account_id": service_account_id,
            "p_name": name, "p_environment": environment, "p_scopes": scopes,
            "p_key_hash": hash_key(raw_key), "p_key_prefix": raw_key[:12],
            "p_expires_at": (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat(),
            "p_request_id": request_id,
        }))
        return {**result, "api_key": raw_key, "secret_available_once": True}

    def rotate_service_key(self, principal: CompanyPrincipal, service_account_id: str,
                           expires_in_days: int, request_id: str) -> dict[str, Any]:
        raw_key = generate_api_key()
        result = self._result(self._rpc("company_admin_rotate_service_key", {
            "p_organization_id": principal.company_id, "p_actor_user_id": principal.actor_id,
            "p_service_account_id": service_account_id, "p_key_hash": hash_key(raw_key),
            "p_key_prefix": raw_key[:12],
            "p_expires_at": (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat(),
            "p_request_id": request_id,
        }))
        return {**result, "api_key": raw_key, "secret_available_once": True}

    def revoke_service_account(self, principal: CompanyPrincipal, service_account_id: str,
                               request_id: str) -> dict[str, Any]:
        return self._result(self._rpc("company_admin_revoke_service_account", {
            "p_organization_id": principal.company_id, "p_actor_user_id": principal.actor_id,
            "p_service_account_id": service_account_id, "p_request_id": request_id,
        }))

    def list_audit_events(self, principal: CompanyPrincipal, cursor: str, limit: int,
                          request_id: str) -> dict[str, Any]:
        decoded = self._codec.decode(cursor, principal.company_id, "audit_events")
        page_limit = min(max(int(limit), 1), PAGE_MAX)
        value = self._result(self._rpc("company_admin_audit_page", {
            "p_organization_id": principal.company_id, "p_actor_user_id": principal.actor_id,
            "p_cursor_time": decoded[0] if decoded else None,
            "p_cursor_id": decoded[1] if decoded else None,
            "p_limit": page_limit,
            "p_request_id": request_id,
        }))
        rows = value.get("items") or []
        has_more = len(rows) > page_limit
        rows = rows[:page_limit]
        next_cursor = (self._codec.encode(principal.company_id, "audit_events",
                                         rows[-1]["occurred_at"], str(rows[-1]["id"]))
                       if has_more and rows else "")
        return {"items": rows, "next_cursor": next_cursor,
                "has_more": has_more, "limit": page_limit}

    def create_dashboard_session_key(self, principal: CompanyPrincipal, key_hash: str,
                                     key_prefix: str, expires_at: str,
                                     request_id: str) -> dict[str, Any]:
        return self._result(self._rpc(
            "company_admin_create_dashboard_session_key", {
                "p_organization_id": principal.company_id,
                "p_actor_user_id": principal.actor_id,
                "p_key_hash": key_hash,
                "p_key_prefix": key_prefix,
                "p_expires_at": expires_at,
                "p_request_id": request_id,
            }))

    def revoke_key(self, principal: CompanyPrincipal, key_id: str,
                   request_id: str) -> dict[str, Any]:
        return self._result(self._rpc("company_admin_revoke_dashboard_session_key", {
            "p_organization_id": principal.company_id,
            "p_actor_user_id": principal.actor_id,
            "p_key_id": key_id,
            "p_request_id": request_id,
        }))


def service_account_key_context(store: Any, key_hash: str) -> dict[str, Any] | None:
    """W1 auth contract: authorize a service key only through its live account join.

    This must replace key-table-only runtime checks for ``organization_service``
    keys. It rejects revoked/expired keys, revoked/expired accounts, and any
    cross-tenant key/account relationship.
    """
    now = _now()
    if hasattr(store, "_request"):
        value = store._request("POST", "rpc/service_key_authorization", data={
            "p_key_hash": key_hash,
        })
        if isinstance(value, list):
            value = value[0] if value else None
        return dict(value) if isinstance(value, dict) and value.get("key_hash") else None
    db_path = getattr(store, "db_path", "")
    if not db_path:
        return None
    with sqlite3.connect(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT key.key_hash,organization.billing_owner_id AS owner_id,"
            "key.organization_id,key.service_account_id,key.key_type,"
            "key.scopes,key.environment,key.expires_at,account.expires_at AS account_expires_at "
            "FROM api_keys key JOIN service_accounts account "
            "ON account.id=key.service_account_id "
            "AND account.organization_id=key.organization_id "
            "JOIN organizations organization ON organization.id=key.organization_id "
            "WHERE key.key_hash=? AND key.key_type='organization_service' "
            "AND key.revoked_at='' AND (key.expires_at='' OR key.expires_at>?) "
            "AND account.status='active' AND account.revoked_at='' "
            "AND organization.billing_owner_id<>'' "
            "AND (account.expires_at='' OR account.expires_at>?) LIMIT 1",
            (key_hash, now, now),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["scopes"] = [scope for scope in str(result.get("scopes") or "").split(",") if scope]
    return result


def company_admin_for_store(store: Any) -> CompanyAdminService:
    secret = os.getenv("COMPANY_ADMIN_CURSOR_SECRET", "")
    invitee_pepper = os.getenv("COMPANY_ADMIN_INVITEE_PEPPER", "")
    if hasattr(store, "_request"):
        if len(secret) < 32 or len(invitee_pepper) < 32:
            raise RuntimeError(
                "COMPANY_ADMIN_CURSOR_SECRET and COMPANY_ADMIN_INVITEE_PEPPER "
                "must each be at least 32 characters")
        return SupabaseCompanyAdminService(
            store, cursor_secret=secret, invitee_pepper=invitee_pepper)
    if hasattr(store, "db_path"):
        return SQLiteCompanyAdminService(
            store.db_path, cursor_secret=secret or "local-company-admin",
            invitee_pepper=invitee_pepper or "local-company-invitee")
    raise RuntimeError("Unsupported company administration store")


PrincipalResolver = Callable[[Request], CompanyPrincipal | Awaitable[CompanyPrincipal]]
RequestIdResolver = Callable[[Request], str]
_configured_service: CompanyAdminService | None = None
_principal_resolver: PrincipalResolver | None = None
_request_id_resolver: RequestIdResolver | None = None


def configure_company_admin(service: CompanyAdminService, principal_resolver: PrincipalResolver,
                            request_id_resolver: RequestIdResolver | None = None) -> None:
    """Called once by the API composition root; never from request data."""
    global _configured_service, _principal_resolver, _request_id_resolver
    _configured_service = service
    _principal_resolver = principal_resolver
    _request_id_resolver = request_id_resolver


@dataclass(frozen=True)
class _Context:
    service: CompanyAdminService
    principal: CompanyPrincipal
    request_id: str


async def _context(request: Request) -> _Context:
    if _configured_service is None or _principal_resolver is None:
        raise HTTPException(status_code=503, detail="Company administration unavailable")
    resolved = _principal_resolver(request)
    principal = await resolved if inspect.isawaitable(resolved) else resolved
    if not isinstance(principal, CompanyPrincipal) or not principal.actor_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    actor_id = str(principal.actor_id).strip()
    company_id = str(principal.company_id or "").strip()
    invitee_lookup = str(principal.invitee_lookup_hash or "").strip().lower()
    if (not _OPAQUE_ID.fullmatch(actor_id) or len(actor_id) > 128
            or (company_id and not _OPAQUE_ID.fullmatch(company_id))
            or (invitee_lookup and not re.fullmatch(r"[0-9a-f]{64}", invitee_lookup))):
        raise HTTPException(status_code=401, detail="Authentication required")
    principal = CompanyPrincipal(
        actor_id, company_id, _canonical_role(principal.role), invitee_lookup)
    request_id = (_request_id_resolver(request) if _request_id_resolver
                  else str(getattr(request.state, "brevitas_request_id", "")))
    if not _REQUEST_ID.fullmatch(request_id or ""):
        request_id = str(uuid.uuid4())
    return _Context(_configured_service, principal, request_id)


class InviteMemberBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: str = Field(default="member")
    expires_in_hours: int = Field(default=72, ge=1, le=168)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _EMAIL.fullmatch(normalized):
            raise ValueError("invalid email address")
        return normalized

    @field_validator("role")
    @classmethod
    def valid_invited_role(cls, value: str) -> str:
        if value not in ("company_admin", "member", "billing_admin"):
            raise ValueError("invalid invitation role")
        return value


class AcceptInvitationBody(BaseModel):
    invitation_token: str = Field(min_length=40, max_length=128, pattern=r"^bvi_[A-Za-z0-9_-]+$")


class SelectActiveCompanyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: uuid.UUID


class ChangeMemberBody(BaseModel):
    role: str
    status: str = "active"

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str) -> str:
        if value not in COMPANY_ROLES:
            raise ValueError("invalid company role")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in MUTABLE_MEMBER_STATUSES:
            raise ValueError("invalid member status")
        return value


class CreateServiceAccountBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    environment: str = Field(default="production", min_length=1, max_length=32,
                             pattern=r"^[A-Za-z0-9._-]+$")
    scopes: list[str] = Field(min_length=1, max_length=12)
    expires_in_days: int = Field(default=90, ge=1, le=365)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if any(ord(character) < 32 for character in cleaned):
            raise ValueError("invalid service account name")
        return cleaned

    @field_validator("scopes")
    @classmethod
    def valid_scopes(cls, value: list[str]) -> list[str]:
        scopes = sorted(set(value))
        if not scopes or not set(scopes) <= SERVICE_SCOPES:
            raise ValueError("invalid service account scope")
        return scopes


class RotateServiceKeyBody(BaseModel):
    expires_in_days: int = Field(default=90, ge=1, le=365)


router = APIRouter(prefix="/v1/company", tags=["company-administration"])


def _result(call: Callable[[], dict[str, Any]], *, secret: bool = False,
            audit_request_id: str = "", audit_denials: bool = False) -> JSONResponse:
    try:
        value = call()
    except CompanyAdminError as exc:
        if audit_denials and audit_request_id:
            _emit_admin_audit_committed(audit_request_id, "rejected")
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid pagination cursor") from exc
    if audit_request_id:
        _emit_admin_audit_committed(audit_request_id, "success")
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if secret:
        headers["Pragma"] = "no-cache"
    return JSONResponse(value, headers=headers)


def _valid_id(value: str) -> str:
    if not _OPAQUE_ID.fullmatch(value):
        raise HTTPException(status_code=400, detail="Invalid opaque resource id")
    return value


@router.get("/capabilities")
def capabilities(context: _Context = Depends(_context)):
    return _result(lambda: context.service.capabilities(context.principal, context.request_id))


@router.post("/active")
def select_active_company(body: SelectActiveCompanyBody,
                          context: _Context = Depends(_context)):
    return _result(lambda: context.service.select_active_company(
        context.principal, str(body.company_id), context.request_id),
        audit_request_id=context.request_id)


@router.get("/members")
def members(cursor: str = Query("", max_length=512),
            limit: int = Query(PAGE_DEFAULT, ge=1, le=PAGE_MAX),
            context: _Context = Depends(_context)):
    return _result(lambda: context.service.list_members(
        context.principal, cursor, limit, context.request_id))


@router.patch("/members/{member_id}")
def change_member(member_id: str, body: ChangeMemberBody,
                  context: _Context = Depends(_context)):
    target = _valid_id(member_id)
    return _result(lambda: context.service.change_member(
        context.principal, target, body.role, body.status, context.request_id),
        audit_request_id=context.request_id, audit_denials=True)


@router.get("/invitations")
def invitations(cursor: str = Query("", max_length=512),
                limit: int = Query(PAGE_DEFAULT, ge=1, le=PAGE_MAX),
                context: _Context = Depends(_context)):
    return _result(lambda: context.service.list_invitations(
        context.principal, cursor, limit, context.request_id))


@router.post("/invitations")
def invite(body: InviteMemberBody, context: _Context = Depends(_context)):
    return _result(lambda: context.service.invite_member(
        context.principal, body.email, body.role, body.expires_in_hours,
        context.request_id), secret=True, audit_request_id=context.request_id,
        audit_denials=True)


@router.post("/invitations/{invitation_id}/cancel")
def cancel_invitation(invitation_id: str, context: _Context = Depends(_context)):
    target = _valid_id(invitation_id)
    return _result(lambda: context.service.cancel_invitation(
        context.principal, target, context.request_id),
        audit_request_id=context.request_id, audit_denials=True)


@router.post("/invitations/accept")
def accept_invitation(body: AcceptInvitationBody,
                      context: _Context = Depends(_context)):
    return _result(lambda: context.service.accept_invitation(
        context.principal, body.invitation_token, context.request_id),
        audit_request_id=context.request_id)


@router.get("/service-accounts")
def service_accounts(cursor: str = Query("", max_length=512),
                     limit: int = Query(PAGE_DEFAULT, ge=1, le=PAGE_MAX),
                     context: _Context = Depends(_context)):
    return _result(lambda: context.service.list_service_accounts(
        context.principal, cursor, limit, context.request_id))


@router.post("/service-accounts")
def create_service_account(body: CreateServiceAccountBody,
                           context: _Context = Depends(_context)):
    return _result(lambda: context.service.create_service_account(
        context.principal, body.name, body.environment, body.scopes,
        body.expires_in_days, context.request_id),
        secret=True, audit_request_id=context.request_id, audit_denials=True)


@router.post("/service-accounts/{service_account_id}/rotate-key")
def rotate_service_key(service_account_id: str, body: RotateServiceKeyBody,
                       context: _Context = Depends(_context)):
    target = _valid_id(service_account_id)
    return _result(lambda: context.service.rotate_service_key(
        context.principal, target, body.expires_in_days, context.request_id),
        secret=True, audit_request_id=context.request_id, audit_denials=True)


@router.delete("/service-accounts/{service_account_id}")
def revoke_service_account(service_account_id: str,
                           context: _Context = Depends(_context)):
    target = _valid_id(service_account_id)
    return _result(lambda: context.service.revoke_service_account(
        context.principal, target, context.request_id),
        audit_request_id=context.request_id, audit_denials=True)


@router.get("/audit-events")
def audit_events(cursor: str = Query("", max_length=512),
                 limit: int = Query(PAGE_DEFAULT, ge=1, le=PAGE_MAX),
                 context: _Context = Depends(_context)):
    return _result(lambda: context.service.list_audit_events(
        context.principal, cursor, limit, context.request_id))


__all__ = [
    "CompanyPrincipal", "ROLE_PERMISSIONS", "SQLiteCompanyAdminService",
    "SupabaseCompanyAdminService", "company_admin_for_store",
    "configure_company_admin", "router", "service_account_key_context",
]
