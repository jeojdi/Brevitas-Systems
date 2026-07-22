import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.auth import hash_key
from api.company_admin import (
    CompanyAdminConflict,
    CompanyAdminDenied,
    CompanyAdminNotFound,
    CompanyPrincipal,
    DASHBOARD_SESSION_PER_ACTOR_MAX,
    ROLE_PERMISSIONS,
    SQLiteCompanyAdminService,
    SupabaseCompanyAdminService,
    configure_company_admin,
    router,
    service_account_key_context,
)
from api.store import UsageStore


def _setup(tmp_path):
    store = UsageStore(str(tmp_path / "company-admin.db"))
    org_a = store.ensure_organization("owner-a", "Company A")
    org_b = store.ensure_organization("owner-b", "Company B")
    service = SQLiteCompanyAdminService(store.db_path, cursor_secret="x" * 40)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE organization_members SET role='company_owner',status='active' WHERE user_id IN ('owner-a','owner-b')")
        for user_id, role in (("admin-a", "company_admin"), ("member-a", "member"),
                              ("billing-a", "billing_admin"), ("owner-a-2", "company_owner")):
            db.execute(
                "INSERT INTO organization_members(organization_id,user_id,role,created_at,status,updated_at) VALUES(?,?,?,?,?,?)",
                (org_a["id"], user_id, role, "2026-07-18T00:00:00+00:00", "active",
                 "2026-07-18T00:00:00+00:00"),
            )
        db.execute(
            "INSERT INTO organization_members(organization_id,user_id,role,created_at,status,updated_at) VALUES(?,?,?,?,?,?)",
            (org_b["id"], "member-b", "member", "2026-07-18T00:00:00+00:00",
             "active", "2026-07-18T00:00:00+00:00"),
        )
    return store, service, org_a, org_b


def _principal(user_id, organization_id, role, invitee_lookup_hash=""):
    return CompanyPrincipal(user_id, organization_id, role, invitee_lookup_hash)


def _server_invitation_request(spoofed_email="attacker@example.net"):
    body = json.dumps({
        "invitation_token": "bvi_spoofed",
        "email": spoofed_email,
    }).encode()
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "server": ("api.brevitassystems.com", 443),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
        "path": "/v1/company/invitations/accept",
        "raw_path": b"/v1/company/invitations/accept",
        "query_string": f"email={spoofed_email}".encode(),
        "headers": [
            (b"host", b"api.brevitassystems.com"),
            (b"authorization", b"Bearer verified-session"),
            (b"x-invitee-email", spoofed_email.encode()),
            (b"content-type", b"application/json"),
        ],
    }, receive)


def test_role_matrix_is_explicit_and_least_privilege():
    assert set(ROLE_PERMISSIONS) == {
        "company_owner", "company_admin", "member", "billing_admin",
    }
    assert "owners:manage" in ROLE_PERMISSIONS["company_owner"]
    assert "owners:manage" not in ROLE_PERMISSIONS["company_admin"]
    assert "service_accounts:manage" in ROLE_PERMISSIONS["company_admin"]
    assert ROLE_PERMISSIONS["member"] == {"company:read", "members:read"}
    assert "billing:manage" in ROLE_PERMISSIONS["billing_admin"]
    assert "members:manage" not in ROLE_PERMISSIONS["billing_admin"]


def test_capabilities_returns_only_verified_actor_active_company_choices(tmp_path):
    store, service, org_a, org_b = _setup(tmp_path)
    disabled = store.ensure_organization("disabled-company-owner", "Disabled company")
    foreign = store.ensure_organization("foreign-company-owner", "Foreign company")
    created = "2026-07-18T00:00:00+00:00"
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO organization_members(organization_id,user_id,role,created_at,"
            "status,updated_at) VALUES(?,?,?,?,?,?)",
            (org_b["id"], "member-a", "billing_admin", created, "active", created),
        )
        db.execute(
            "INSERT INTO organization_members(organization_id,user_id,role,created_at,"
            "status,updated_at) VALUES(?,?,?,?,?,?)",
            (disabled["id"], "member-a", "member", created, "disabled", created),
        )

    result = service.capabilities(
        _principal("member-a", org_a["id"], "member"),
        "request-active-company-choices",
    )

    assert result["company_id"] == org_a["id"]
    assert result["companies"][0]["company_id"] == org_a["id"]
    assert {item["company_id"] for item in result["companies"]} == {
        org_a["id"], org_b["id"],
    }
    assert all(set(item) == {"company_id", "company_name", "role", "account_type"}
               for item in result["companies"])
    assert all(item["account_type"] == "company" for item in result["companies"])
    assert disabled["id"] not in {item["company_id"] for item in result["companies"]}
    assert foreign["id"] not in {item["company_id"] for item in result["companies"]}


def test_active_company_choices_are_hard_capped_with_current_company_first(tmp_path):
    store, service, org_a, _ = _setup(tmp_path)
    created = "2026-07-18T00:00:00+00:00"
    extra_ids = []
    for index in range(105):
        organization = store.ensure_organization(
            f"cap-owner-{index}", f"Bounded company {index:03d}")
        extra_ids.append(organization["id"])
    with sqlite3.connect(store.db_path) as db:
        db.executemany(
            "INSERT INTO organization_members(organization_id,user_id,role,created_at,"
            "status,updated_at) VALUES(?,?,?,?,?,?)",
            [(company_id, "member-a", "member", created, "active", created)
             for company_id in extra_ids],
        )

    companies = service.capabilities(
        _principal("member-a", org_a["id"], "member"),
        "request-company-choice-cap",
    )["companies"]

    assert len(companies) == 100
    assert companies[0]["company_id"] == org_a["id"]
    assert len({item["company_id"] for item in companies}) == 100


def test_server_principal_rejects_unconfirmed_invitation_email_without_hmac(
        tmp_path, monkeypatch):
    import api.server as server

    store, service, org_a, _ = _setup(tmp_path)
    invitation = service.invite_member(
        _principal("owner-a", org_a["id"], "company_owner"),
        "pending.confirmation@example.com", "member", 24,
        "request-unconfirmed-invite",
    )
    lookups = []
    actual_lookup = service.invitee_lookup

    def tracked_lookup(email):
        lookups.append(email)
        return actual_lookup(email)

    monkeypatch.setattr(service, "invitee_lookup", tracked_lookup)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_company_admin_service", service)
    monkeypatch.setattr(server, "_dashboard_identity", lambda _request: {
        "id": "member-b",
        "email": "pending.confirmation@example.com",
        "email_confirmed_at": "",
    })

    principal = server._company_admin_principal(_server_invitation_request())

    assert principal.invitee_lookup_hash == ""
    assert lookups == []
    with pytest.raises(CompanyAdminDenied):
        service.accept_invitation(
            principal, invitation["invitation_token"],
            "request-unconfirmed-accept",
        )


def test_server_principal_uses_verified_identity_hmac_for_every_cross_tenant_acceptance(
        tmp_path, monkeypatch):
    import api.server as server

    store, service, org_a, org_b = _setup(tmp_path)
    verified_email = "verified.cross-tenant@example.com"
    invitation = service.invite_member(
        _principal("owner-a", org_a["id"], "company_owner"),
        verified_email, "billing_admin", 24, "request-cross-tenant-invite",
    )
    lookups = []
    actual_lookup = service.invitee_lookup
    expected_lookup = actual_lookup(verified_email)

    def tracked_lookup(email):
        lookups.append(email)
        return actual_lookup(email)

    monkeypatch.setattr(service, "invitee_lookup", tracked_lookup)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_company_admin_service", service)
    monkeypatch.setattr(server, "_dashboard_identity", lambda _request: {
        "id": "member-b",
        "email": verified_email,
        "email_confirmed_at": "2026-07-18T00:00:00+00:00",
    })

    first = server._company_admin_principal(
        _server_invitation_request("spoofed.first@example.net"))
    second = server._company_admin_principal(
        _server_invitation_request("spoofed.second@example.net"))

    assert first.company_id == org_b["id"] and first.role == "member"
    assert first.invitee_lookup_hash == expected_lookup
    assert second.invitee_lookup_hash == first.invitee_lookup_hash
    assert lookups == [verified_email, verified_email]
    accepted = service.accept_invitation(
        second, invitation["invitation_token"], "request-cross-tenant-accept")
    assert accepted == {
        "company_id": org_a["id"],
        "role": "billing_admin",
        "status": "accepted",
    }
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT role,status FROM organization_members "
            "WHERE organization_id=? AND user_id='member-b'",
            (org_b["id"],),
        ).fetchone() == ("member", "active")
        assert db.execute(
            "SELECT role,status FROM organization_members "
            "WHERE organization_id=? AND user_id='member-b'",
            (org_a["id"],),
        ).fetchone() == ("billing_admin", "active")


def test_invitations_store_only_keyed_lookup_and_token_hash_and_audit_no_pii(tmp_path):
    store, service, org_a, _ = _setup(tmp_path)
    owner = _principal("owner-a", org_a["id"], "company_owner")
    result = service.invite_member(
        owner, "Sensitive.Person@Company-A.example", "member", 72, "request-invite-001")
    token = result["invitation_token"]
    assert token.startswith("bvi_")
    assert result["secret_available_once"] is True

    with sqlite3.connect(store.db_path) as db:
        invitation = db.execute(
            "SELECT email_lookup_hash,token_hash FROM organization_invitations WHERE id=?",
            (result["id"],),
        ).fetchone()
        audit = db.execute(
            "SELECT request_id,actor_id,organization_id,action,target_id,details FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        persisted = json.dumps(db.execute(
            "SELECT * FROM organization_invitations"
        ).fetchall())
    assert invitation[1] == hash_key(token)
    assert len(invitation[0]) == 64
    assert token not in persisted
    assert "sensitive.person" not in persisted.lower()
    assert audit[0:4] == ("request-invite-001", "owner-a", org_a["id"], "member.invited")
    assert audit[5] == "{}"
    assert "sensitive" not in repr(audit).lower()


def test_member_lifecycle_last_owner_and_cross_tenant_guards_are_transactional(tmp_path):
    store, service, org_a, org_b = _setup(tmp_path)
    owner_a = _principal("owner-a", org_a["id"], "company_owner")
    admin_a = _principal("admin-a", org_a["id"], "company_admin")
    member_a = _principal("member-a", org_a["id"], "member")

    # Admins cannot promote owners or mutate another administrator.
    with pytest.raises(CompanyAdminDenied):
        service.change_member(admin_a, "member-a", "company_owner", "active",
                              "request-admin-promote")
    with pytest.raises(CompanyAdminDenied):
        service.change_member(admin_a, "owner-a-2", "member", "active",
                              "request-admin-demote")

    # Tenant A cannot address Tenant B's member even when the opaque ID is known.
    with pytest.raises(CompanyAdminDenied):
        service.change_member(owner_a, "member-b", "member", "removed",
                              "request-cross-tenant")
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT status FROM organization_members WHERE organization_id=? AND user_id='member-b'",
            (org_b["id"],),
        ).fetchone()[0] == "active"

    service.change_member(owner_a, "owner-a-2", "member", "removed",
                          "request-owner-remove-2")
    with pytest.raises(CompanyAdminConflict):
        service.change_member(owner_a, "owner-a", "member", "removed",
                              "request-last-owner")

    with sqlite3.connect(store.db_path) as db:
        owner = db.execute(
            "SELECT role,status FROM organization_members WHERE organization_id=? AND user_id='owner-a'",
            (org_a["id"],),
        ).fetchone()
        denied = db.execute(
            "SELECT action,outcome,target_id FROM audit_events WHERE request_id='request-last-owner'"
        ).fetchone()
    assert owner == ("company_owner", "active")
    assert denied == ("member.change.denied", "denied", "owner-a")

    with pytest.raises(CompanyAdminDenied):
        service.invite_member(member_a, "other@example.com", "member", 24,
                              "request-member-denied")


def test_concurrent_owner_demotions_cannot_remove_last_owner(tmp_path):
    store, service, org_a, _ = _setup(tmp_path)
    owners = [
        _principal("owner-a", org_a["id"], "company_owner"),
        _principal("owner-a-2", org_a["id"], "company_owner"),
    ]

    def demote(principal):
        try:
            service.change_member(
                principal, principal.actor_id, "member", "active",
                f"request-race-{principal.actor_id}")
            return "changed"
        except CompanyAdminConflict:
            return "last-owner"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(demote, owners))
    assert sorted(results) == ["changed", "last-owner"]
    with sqlite3.connect(store.db_path) as db:
        active_owners = db.execute(
            "SELECT count(*) FROM organization_members WHERE organization_id=? "
            "AND role='company_owner' AND status='active'", (org_a["id"],)
        ).fetchone()[0]
    assert active_owners == 1


def test_invitation_accept_cancel_and_disabled_state_are_retained(tmp_path):
    store, service, org_a, _ = _setup(tmp_path)
    owner = _principal("owner-a", org_a["id"], "company_owner")
    first = service.invite_member(owner, "new.member@example.com", "billing_admin", 24,
                                  "request-invite-accept")
    invitee = _principal(
        "new-member", "wrong-client-company", "member",
        service.invitee_lookup("new.member@example.com"),
    )
    accepted = service.accept_invitation(
        invitee, first["invitation_token"], "request-accept-001")
    assert accepted == {"company_id": org_a["id"], "role": "billing_admin",
                        "status": "accepted"}
    service.change_member(owner, "new-member", "billing_admin", "disabled",
                          "request-disable-001")

    second = service.invite_member(owner, "cancelled@example.com", "member", 24,
                                   "request-invite-cancel")
    assert service.cancel_invitation(owner, second["id"], "request-cancel-001")["status"] == "cancelled"
    with sqlite3.connect(store.db_path) as db:
        lifecycle = db.execute(
            "SELECT role,status,disabled_at,removed_at FROM organization_members WHERE user_id='new-member'"
        ).fetchone()
        invitation_statuses = db.execute(
            "SELECT status FROM organization_invitations ORDER BY created_at,id"
        ).fetchall()
    assert lifecycle[0:2] == ("billing_admin", "disabled")
    assert lifecycle[2] and lifecycle[3] == ""
    assert {row[0] for row in invitation_statuses} == {"accepted", "cancelled"}


def test_invitation_identity_binding_replay_existing_roles_and_cross_tenant_normalization(tmp_path):
    store, service, org_a, org_b = _setup(tmp_path)
    owner = _principal("owner-a", org_a["id"], "company_owner")
    invitation = service.invite_member(
        owner, "bound.person@example.com", "member", 24, "request-bound-invite")

    wrong = _principal(
        "wrong-user", org_b["id"], "company_owner",
        service.invitee_lookup("wrong.person@example.com"),
    )
    with pytest.raises(CompanyAdminDenied):
        service.accept_invitation(wrong, invitation["invitation_token"],
                                  "request-wrong-invitee")

    # The server-derived current company is irrelevant: the verified email HMAC
    # binds the token and the accepted company is normalized from the locked row.
    correct = _principal(
        "member-b", org_b["id"], "member",
        service.invitee_lookup("bound.person@example.com"),
    )
    accepted = service.accept_invitation(
        correct, invitation["invitation_token"], "request-correct-invitee")
    assert accepted["company_id"] == org_a["id"]
    with pytest.raises(CompanyAdminDenied):
        service.accept_invitation(
            correct, invitation["invitation_token"], "request-replayed-invite")

    # An invitation cannot overwrite any existing target-company membership,
    # including disabled members, admins, or the last owner.
    for actor_id, role, email in (
        ("member-a", "member", "existing-member@example.com"),
        ("admin-a", "company_admin", "existing-admin@example.com"),
        ("owner-a", "company_owner", "existing-owner@example.com"),
    ):
        existing = service.invite_member(
            owner, email, "member", 24, f"request-existing-{role}")
        principal = _principal(actor_id, org_a["id"], role, service.invitee_lookup(email))
        with pytest.raises(CompanyAdminConflict):
            service.accept_invitation(
                principal, existing["invitation_token"], f"request-reject-{role}")

    with sqlite3.connect(store.db_path) as db:
        roles = dict(db.execute(
            "SELECT user_id,role FROM organization_members WHERE organization_id=? "
            "AND user_id IN ('member-a','admin-a','owner-a')", (org_a["id"],)
        ).fetchall())
        wrong_denied = db.execute(
            "SELECT outcome FROM audit_events WHERE request_id='request-wrong-invitee'"
        ).fetchone()[0]
        cross_tenant_role = db.execute(
            "SELECT role FROM organization_members WHERE organization_id=? AND user_id='member-b'",
            (org_b["id"],),
        ).fetchone()[0]
    assert roles == {
        "member-a": "member", "admin-a": "company_admin", "owner-a": "company_owner",
    }
    assert wrong_denied == "denied"
    assert cross_tenant_role == "member"


def test_service_account_keys_rotate_once_are_scoped_hashed_and_revocable(tmp_path):
    store, service, org_a, _ = _setup(tmp_path)
    admin = _principal("admin-a", org_a["id"], "company_admin")
    account = service.create_service_account(
        admin, "Production worker", "production",
        ["proxy:invoke", "usage:write", "jobs:create"], 90, "request-service-create")
    initial = account
    assert initial["secret_available_once"] is True
    assert service_account_key_context(
        store, hash_key(initial["api_key"]))["service_account_id"] == account["id"]
    first = service.rotate_service_key(admin, account["id"], 30, "request-key-first")
    second = service.rotate_service_key(admin, account["id"], 30, "request-key-rotate")

    with sqlite3.connect(store.db_path) as db:
        rows = db.execute(
            "SELECT key_hash,scopes,revoked_at FROM api_keys WHERE organization_id=? AND service_account_id=? ORDER BY created",
            (org_a["id"], account["id"]),
        ).fetchall()
        persisted = repr(db.execute("SELECT * FROM api_keys").fetchall())
    assert len(rows) == 3
    assert rows[0][0] == hash_key(initial["api_key"])
    assert rows[0][2]
    assert rows[1][0] == hash_key(first["api_key"])
    assert rows[1][2]
    assert rows[2][0] == hash_key(second["api_key"])
    assert rows[2][1].split(",") == ["jobs:create", "proxy:invoke", "usage:write"]
    assert rows[2][2] == ""
    assert all(secret not in persisted for secret in (
        initial["api_key"], first["api_key"], second["api_key"],
    ))

    service.revoke_service_account(admin, account["id"], "request-service-revoke")
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT status FROM service_accounts WHERE id=?",
                          (account["id"],)).fetchone()[0] == "revoked"
        assert db.execute("SELECT revoked_at FROM api_keys WHERE key_hash=?",
                          (hash_key(second["api_key"]),)).fetchone()[0]


def test_initial_service_key_is_billing_owned_actor_attributed_and_atomic(tmp_path):
    store, service, org_a, _ = _setup(tmp_path)
    admin = _principal("admin-a", org_a["id"], "company_admin")
    created = service.create_service_account(
        admin, "Initial production worker", "production",
        ["proxy:invoke", "usage:write"], 90, "request-initial-service-key")

    with sqlite3.connect(store.db_path) as db:
        credential = db.execute(
            "SELECT owner_id,created_by,key_hash,key_prefix,expires_at "
            "FROM api_keys WHERE id=?", (created["key_id"],),
        ).fetchone()
        audit = db.execute(
            "SELECT actor_user_id,actor_role,action,target_id FROM audit_events "
            "WHERE request_id='request-initial-service-key'",
        ).fetchone()
        persisted = repr(db.execute("SELECT * FROM api_keys").fetchall())

    assert credential == (
        "owner-a", "admin-a", hash_key(created["api_key"]),
        created["prefix"], created["expires_at"],
    )
    assert audit == (
        "admin-a", "company_admin", "service_account.created", created["id"],
    )
    assert created["api_key"] not in persisted

    with sqlite3.connect(store.db_path) as db:
        db.execute("""CREATE TRIGGER fail_initial_service_audit
            BEFORE INSERT ON audit_events
            WHEN NEW.action='service_account.created'
            BEGIN SELECT RAISE(ABORT,'simulated initial key audit failure'); END""")
    with pytest.raises(sqlite3.DatabaseError, match="simulated initial key audit failure"):
        service.create_service_account(
            admin, "Must roll back", "production", ["proxy:invoke"], 90,
            "request-initial-service-rollback")
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT count(*) FROM service_accounts WHERE name='Must roll back'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT count(*) FROM api_keys WHERE name='Must roll back'"
        ).fetchone()[0] == 0


def test_service_key_billing_owner_is_authoritative_and_rotator_is_audit_only(
        tmp_path, monkeypatch):
    from api import server

    store, service, org_a, _ = _setup(tmp_path)
    admin = _principal("admin-a", org_a["id"], "company_admin")
    account = service.create_service_account(
        admin, "Billing owner worker", "production",
        ["proxy:invoke", "usage:write"], 90, "request-billing-account")
    generic_key_hash = hash_key("bvt_generic_service_key")
    store.create_key(
        generic_key_hash, "Generic service key", owner_id="admin-a",
        organization_id=org_a["id"], service_account_id=account["id"],
        key_type="organization_service", scopes=["proxy:invoke", "usage:write"],
        created_by="admin-a", request_id="request-generic-service-key",
        actor_role="company_admin",
    )
    key = service.rotate_service_key(
        admin, account["id"], 30, "request-billing-key")
    key_hash = hash_key(key["api_key"])

    with sqlite3.connect(store.db_path) as db:
        credential = db.execute(
            "SELECT owner_id,created_by FROM api_keys WHERE key_hash=?", (key_hash,),
        ).fetchone()
        generic_credential = db.execute(
            "SELECT owner_id,created_by FROM api_keys WHERE key_hash=?",
            (generic_key_hash,),
        ).fetchone()
        audit = db.execute(
            "SELECT actor_user_id,actor_role FROM audit_events "
            "WHERE request_id='request-billing-key'",
        ).fetchone()
        # Simulate a pre-migration/stale credential row. Runtime authorization
        # must resolve the organization relationship instead of trusting it.
        db.execute(
            "UPDATE api_keys SET owner_id='admin-a' WHERE key_hash=?", (key_hash,),
        )

    assert credential == ("owner-a", "admin-a")
    assert generic_credential == ("owner-a", "admin-a")
    assert audit == ("admin-a", "company_admin")
    assert store.key_owner(key_hash) == "owner-a"
    authoritative = service_account_key_context(store, key_hash)
    assert authoritative and authoritative["owner_id"] == "owner-a"

    monkeypatch.setattr(server, "_store", store)
    server._auth_context_cache.clear()
    context = server._auth_context_for_key(key_hash)
    assert context.billing_owner_id == "owner-a"
    assert context.actor_user_id == ""
    assert len(server._auth_context_cache) == 0
    assert server._safe_record_usage(
        auth_context=context, key_hash=key_hash, owner_id="admin-a",
        baseline_tokens=10, optimized_tokens=8,
    ) is True
    with sqlite3.connect(store.db_path) as db:
        usage_owner = db.execute(
            "SELECT owner_id FROM usage_log WHERE key_hash=? ORDER BY id DESC LIMIT 1",
            (key_hash,),
        ).fetchone()[0]
    assert usage_owner == "owner-a"

    # Ownership transfer does not rotate the service key. Its next request
    # must resolve the new billing owner rather than reuse cached attribution.
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE organizations SET billing_owner_id='owner-a-2' WHERE id=?",
            (org_a["id"],),
        )
    transferred = server._auth_context_for_key(key_hash)
    assert transferred.billing_owner_id == "owner-a-2"
    assert store.key_owner(key_hash) == "owner-a-2"


def test_billing_owner_attribution_migration_preserves_creator_identity():
    migration = (Path(__file__).parent.parent / "supabase/migrations/"
                 "202607200003_billing_owner_attribution.sql").read_text()
    compact = "".join(migration.lower().split())

    assert "new.owner_id:=v_billing_owner_id::text" in compact
    assert "p_key_hash,v_account.name,now(),v_billing_owner_id::text" in compact
    assert "p_key_prefix,v_key_expiry,p_actor_user_id" in compact
    assert "organization.billing_owner_id::text,credential.organization_id" in compact
    assert "credential.owner_id" not in migration.split(
        "create function public.service_key_authorization", 1)[1]
    assert "usage and billing" in migration.lower()


def test_service_account_expiry_bounds_rotation_and_runtime_authorization(tmp_path):
    store, service, org_a, _ = _setup(tmp_path)
    admin = _principal("admin-a", org_a["id"], "company_admin")
    account = service.create_service_account(
        admin, "Short lived", "production", ["proxy:invoke"], 1,
        "request-expiring-account")
    key = service.rotate_service_key(
        admin, account["id"], 365, "request-bounded-key-expiry")
    assert datetime.fromisoformat(key["expires_at"]) <= datetime.fromisoformat(
        account["expires_at"])
    context = service_account_key_context(store, hash_key(key["api_key"]))
    assert context and context["service_account_id"] == account["id"]

    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE service_accounts SET expires_at=? WHERE id=?",
                   (expired, account["id"]))
    assert service_account_key_context(store, hash_key(key["api_key"])) is None
    with pytest.raises(CompanyAdminNotFound):
        service.rotate_service_key(
            admin, account["id"], 1, "request-expired-account-rotate")


def test_company_caps_are_serialized_and_stale_invites_expire_inside_lock(tmp_path):
    store, service, org_a, _ = _setup(tmp_path)
    owner = _principal("owner-a", org_a["id"], "company_owner")
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    created = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(store.db_path) as db:
        for index in range(99):
            db.execute(
                "INSERT INTO organization_invitations(id,organization_id,email_lookup_hash,token_hash,role,status,invited_by,created_at,expires_at) VALUES(?,?,?,?,?,'pending',?,?,?)",
                (f"00000000-0000-4000-8000-{index:012d}", org_a["id"],
                 hash_key(f"email-{index}"), hash_key(f"token-{index}"), "member",
                 "owner-a", created, future),
            )
        db.execute(
            "INSERT INTO organization_invitations(id,organization_id,email_lookup_hash,token_hash,role,status,invited_by,created_at,expires_at) VALUES(?,?,?,?,?,'pending',?,?,?)",
            ("10000000-0000-4000-8000-000000000000", org_a["id"],
             hash_key("stale-email"), hash_key("stale-token"), "member",
             "owner-a", created, past),
        )

    def invite(index):
        try:
            return service.invite_member(
                owner, f"race-{index}@example.com", "member", 24,
                f"request-invite-race-{index}")["id"]
        except CompanyAdminConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        invite_results = list(pool.map(invite, range(2)))
    assert invite_results.count("conflict") == 1

    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT status FROM organization_invitations WHERE id='10000000-0000-4000-8000-000000000000'"
        ).fetchone()[0] == "expired"
        assert db.execute(
            "SELECT count(*) FROM organization_invitations WHERE organization_id=? AND status='pending'",
            (org_a["id"],),
        ).fetchone()[0] == 100
        for index in range(99):
            db.execute(
                "INSERT INTO service_accounts(id,organization_id,name,environment,created_by,created_at,scopes,status,expires_at,updated_at) VALUES(?,?,?,?,?,?,?,'active',?,?)",
                (f"20000000-0000-4000-8000-{index:012d}", org_a["id"], f"worker-{index}",
                 "production", "owner-a", created, "proxy:invoke", future, created),
            )

    def create_account(index):
        try:
            return service.create_service_account(
                owner, f"race-worker-{index}", "production", ["proxy:invoke"], 1,
                f"request-service-race-{index}")["id"]
        except CompanyAdminConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        account_results = list(pool.map(create_account, range(2)))
    assert account_results.count("conflict") == 1


def test_audit_log_is_database_immutable_and_cursor_is_opaque_bound_to_tenant(tmp_path):
    store, service, org_a, org_b = _setup(tmp_path)
    owner = _principal("owner-a", org_a["id"], "company_owner")
    for index in range(3):
        service.invite_member(owner, f"person-{index}@example.com", "member", 24,
                              f"request-cursor-{index}")
    first = service.list_audit_events(owner, "", 2, "request-audit-page-1")
    assert len(first["items"]) == 2 and first["has_more"] is True
    assert first["next_cursor"] and "request-cursor" not in first["next_cursor"]
    second = service.list_audit_events(owner, first["next_cursor"], 2,
                                       "request-audit-page-2")
    assert {row["id"] for row in first["items"]}.isdisjoint(
        {row["id"] for row in second["items"]})
    with pytest.raises(ValueError, match="cursor"):
        replacement = "A" if first["next_cursor"][0] != "A" else "B"
        service.list_audit_events(owner, replacement + first["next_cursor"][1:], 2,
                                  "request-audit-tamper")
    with pytest.raises(ValueError, match="cursor"):
        service.list_audit_events(
            _principal("owner-b", org_b["id"], "company_owner"),
            first["next_cursor"], 2, "request-audit-cross-tenant")

    with sqlite3.connect(store.db_path) as db:
        event_id = db.execute("SELECT id FROM audit_events LIMIT 1").fetchone()[0]
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("UPDATE audit_events SET action='tampered' WHERE id=?", (event_id,))
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("DELETE FROM audit_events WHERE id=?", (event_id,))


def test_audit_insert_trigger_rejects_pii_credentials_hashes_and_unbounded_roles(tmp_path):
    store, _, org_a, _ = _setup(tmp_path)
    base = (org_a["id"], "owner-a", "safe.action", "member", "opaque-target",
            "{}", "2026-07-18T00:00:00+00:00", "request-direct-safe",
            "owner-a", "company_owner", "committed")
    sql = ("INSERT INTO audit_events(organization_id,actor_user_id,action,target_type,"
           "target_id,details,occurred_at,request_id,actor_id,actor_role,outcome,actor_key_hash) "
           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)")
    malicious = [
        {"target": "person@example.com"},
        {"target": "bvt_secret-material"},
        {"target": "a" * 64},
        {"actor": "sk-secret-material"},
        {"role": "unbounded_super_admin"},
        {"actor_key_hash": "b" * 64},
    ]
    with sqlite3.connect(store.db_path) as db:
        for case in malicious:
            values = list(base)
            values[4] = case.get("target", values[4])
            values[8] = case.get("actor", values[8])
            values[9] = case.get("role", values[9])
            values.append(case.get("actor_key_hash", ""))
            with pytest.raises(sqlite3.DatabaseError, match="content-free"):
                db.execute(sql, values)
        # A short fingerprint/opaque row ID is allowed; a full digest is not.
        values = list(base)
        values[4] = "0123456789abcdef"
        values.append("")
        db.execute(sql, values)


def test_supabase_list_uses_single_authorized_keyset_rpc(monkeypatch):
    calls = []

    class Store:
        def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {"ok": True, "items": []}

    service = SupabaseCompanyAdminService(
        Store(), cursor_secret="c" * 40, invitee_pepper="i" * 40)
    principal = CompanyPrincipal(
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
        "company_owner",
    )
    assert service.list_members(principal, "", 50, "request-single-rpc") == {
        "items": [], "next_cursor": "", "has_more": False, "limit": 50,
    }
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "rpc/company_admin_members_page"),
    ]
    assert calls[0][2]["data"]["p_cursor_time"] is None
    assert calls[0][2]["data"]["p_limit"] == 50


def test_supabase_service_account_creation_uses_one_atomic_rpc_and_returns_raw_once(
        monkeypatch):
    import api.company_admin as company_admin

    calls = []
    raw_key = "bvt_InitialServiceSecret123456789"
    principal = CompanyPrincipal(
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
        "company_admin",
    )

    class Store:
        def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs["data"]))
            return {
                "ok": True,
                "id": kwargs["data"]["p_service_account_id"],
                "name": kwargs["data"]["p_name"],
                "environment": kwargs["data"]["p_environment"],
                "scopes": kwargs["data"]["p_scopes"],
                "status": "active",
                "expires_at": kwargs["data"]["p_expires_at"],
                "key_id": "00000000-0000-4000-8000-000000000003",
                "prefix": kwargs["data"]["p_key_prefix"],
            }

    monkeypatch.setattr(company_admin, "generate_api_key", lambda: raw_key)
    service = SupabaseCompanyAdminService(
        Store(), cursor_secret="c" * 40, invitee_pepper="i" * 40)

    created = service.create_service_account(
        principal, "Production worker", "production",
        ["proxy:invoke", "usage:write"], 90, "request-initial-key-rpc")

    assert created["api_key"] == raw_key
    assert created["secret_available_once"] is True
    assert len(calls) == 1
    method, path, data = calls[0]
    assert method == "POST" and path == "rpc/company_admin_create_service_account"
    assert data["p_key_hash"] == hash_key(raw_key)
    assert data["p_key_prefix"] == raw_key[:12]
    assert data["p_actor_user_id"] == principal.actor_id
    assert data["p_organization_id"] == principal.company_id
    assert raw_key not in repr(calls)


def test_initial_service_key_migration_is_atomic_billing_owned_and_service_only():
    migration = (Path(__file__).parent.parent / "supabase/migrations/"
                 "202607200005_initial_service_key.sql").read_text()
    compact = "".join(migration.lower().split())

    old_signature = (
        "public.company_admin_create_service_account("
        "uuid,uuid,uuid,text,text,text[],timestamptz,text)"
    )
    new_signature = (
        "public.company_admin_create_service_account("
        "uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text)"
    )
    assert f"dropfunctionifexists{old_signature}" in compact
    assert f"revokeallonfunction{new_signature}" in compact
    assert f"grantexecuteonfunction{new_signature}toservice_role" in compact
    assert "securitydefinersetsearch_path=public,pg_temp" in compact
    assert "p_key_hash!~'^[0-9a-f]{64}$'" in compact
    assert "v_billing_owner_id::text" in compact
    assert "p_key_prefix,v_account.expires_at,p_actor_user_id" in compact
    assert "exceptionwhenunique_violation" in compact
    account_insert = compact.index("insertintopublic.service_accounts")
    key_insert = compact.index("insertintopublic.api_keys")
    audit_append = compact.index("'service_account.created'")
    assert account_insert < key_insert < audit_append
    returned = compact.split("returnjsonb_build_object(", 2)[2].split(";", 1)[0]
    assert "p_key_hash" not in returned


def test_atomic_dashboard_key_multitab_cap_privilege_and_tenant_isolation(tmp_path):
    store, service, org_a, org_b = _setup(tmp_path)
    member = _principal("member-a", org_a["id"], "member")
    owner = _principal("owner-a", org_a["id"], "company_owner")
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    first_raw = "bvt_atomic_first"
    first = service.create_dashboard_session_key(
        member, hash_key(first_raw), first_raw[:12], expires, "request-atomic-first")
    second_raw = "bvt_atomic_second"
    second = service.create_dashboard_session_key(
        member, hash_key(second_raw), second_raw[:12], expires, "request-atomic-second")
    assert "key_hash" not in first and "key_hash" not in second

    additional = []
    for index in range(2, DASHBOARD_SESSION_PER_ACTOR_MAX):
        raw_key = f"bvt_atomic_tab_{index}"
        additional.append(service.create_dashboard_session_key(
            member, hash_key(raw_key), raw_key[:12], expires,
            f"request-atomic-tab-{index}",
        ))
    ninth_raw = "bvt_atomic_ninth"
    ninth = service.create_dashboard_session_key(
        member, hash_key(ninth_raw), ninth_raw[:12], expires,
        "request-atomic-ninth",
    )

    other_raw = "bvt_atomic_owner"
    other = service.create_dashboard_session_key(
        owner, hash_key(other_raw), other_raw[:12], expires, "request-atomic-owner")
    with pytest.raises(CompanyAdminDenied):
        service.revoke_key(member, other["key_id"], "request-member-revoke-other")
    assert service.revoke_key(
        owner, other["key_id"], "request-owner-revoke")["revoked"] is True

    owner_b = _principal("owner-b", org_b["id"], "company_owner")
    with pytest.raises(CompanyAdminDenied):
        service.revoke_key(owner_b, second["key_id"], "request-cross-tenant-key")

    with sqlite3.connect(store.db_path) as db:
        rows = db.execute(
            "SELECT id,key_hash,revoked_at FROM api_keys WHERE organization_id=? "
            "AND created_by='member-a' ORDER BY created", (org_a["id"],)
        ).fetchall()
        audit = db.execute(
            "SELECT target_id FROM audit_events WHERE request_id='request-atomic-second' "
            "AND action='dashboard_session.created'"
        ).fetchone()[0]
        rotated = db.execute(
            "SELECT target_id FROM audit_events WHERE request_id='request-atomic-ninth' "
            "AND action='dashboard_session.rotated'"
        ).fetchone()[0]
    states = {row[0]: (row[1], row[2]) for row in rows}
    assert states[first["key_id"]][1]
    assert states[second["key_id"]] == (hash_key(second_raw), "")
    assert all(states[item["key_id"]][1] == "" for item in additional)
    assert states[ninth["key_id"]] == (hash_key(ninth_raw), "")
    assert sum(not state[1] for state in states.values()) == DASHBOARD_SESSION_PER_ACTOR_MAX
    assert rotated == first["key_id"]
    assert audit == second["key_id"]
    assert audit != hash_key(second_raw)


def test_legacy_key_revoke_service_denies_service_account_credentials(tmp_path):
    store, service, org_a, _ = _setup(tmp_path)
    owner = _principal("owner-a", org_a["id"], "company_owner")
    account = service.create_service_account(
        owner, "Legacy route isolation", "production", ["proxy:invoke"], 30,
        "request-service-for-legacy-denial",
    )
    credential = service.rotate_service_key(
        owner, account["id"], 30, "request-service-key-for-legacy-denial")

    with pytest.raises(CompanyAdminDenied):
        service.revoke_key(
            owner, credential["key_id"], "request-legacy-service-key-denied")

    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT revoked_at FROM api_keys WHERE id=?", (credential["key_id"],)
        ).fetchone()[0] == ""
        assert db.execute(
            "SELECT action,outcome FROM audit_events "
            "WHERE request_id='request-legacy-service-key-denied'"
        ).fetchone() == ("dashboard_session.revoke.denied", "denied")


def test_atomic_dashboard_key_rolls_back_replacement_when_audit_append_fails(tmp_path):
    store, service, org_a, _ = _setup(tmp_path)
    member = _principal("member-a", org_a["id"], "member")
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    first_raw = "bvt_rollback_first"
    first = service.create_dashboard_session_key(
        member, hash_key(first_raw), first_raw[:12], expires, "request-rollback-first")
    with sqlite3.connect(store.db_path) as db:
        db.execute("""CREATE TRIGGER fail_atomic_key_audit BEFORE INSERT ON audit_events
            WHEN NEW.action='dashboard_session.created'
            BEGIN SELECT RAISE(ABORT,'simulated audit failure'); END""")

    second_raw = "bvt_rollback_second"
    with pytest.raises(sqlite3.DatabaseError, match="simulated audit failure"):
        service.create_dashboard_session_key(
            member, hash_key(second_raw), second_raw[:12], expires,
            "request-rollback-second")
    with sqlite3.connect(store.db_path) as db:
        first_state = db.execute(
            "SELECT revoked_at FROM api_keys WHERE id=?", (first["key_id"],)
        ).fetchone()[0]
        second_count = db.execute(
            "SELECT count(*) FROM api_keys WHERE key_hash=?", (hash_key(second_raw),)
        ).fetchone()[0]
    assert first_state == ""
    assert second_count == 0


def test_atomic_key_migration_sql_privileges_and_w3_rpc_contract():
    root = Path(__file__).parent.parent
    migration = (root / "supabase/migrations/202607170008_atomic_key_audit.sql").read_text()
    documentation = (root / "docs/COMPANY_ADMINISTRATION.md").read_text()
    for signature in (
        "public.company_admin_create_dashboard_session_key(\n    uuid,uuid,text,text,timestamptz,text\n)",
        "public.company_admin_revoke_key(uuid,uuid,uuid,text)",
    ):
        assert f"revoke all on function {signature}" in migration
        assert f"grant execute on function {signature}" in migration
    assert migration.count("security definer") == 2
    assert migration.count("set search_path = public, pg_temp") == 2
    assert "returning id into v_key_id" in migration
    assert "'dashboard_session.created','api_key',v_key_id::text" in migration
    assert "'dashboard_session.created','api_key',p_key_hash" not in migration
    assert "p_key_hash,'dashboard session'" in migration
    assert "v_active_count-v_actor_active_count>=1000" in migration
    assert "v_key.created_by=p_actor_user_id" in migration
    assert "drop function if exists public.company_admin_create_dashboard_session_key" in documentation
    assert "Do not restore keys revoked" in documentation


def test_key_listing_rpc_locks_active_role_applies_matrix_and_never_returns_credentials():
    root = Path(__file__).parent.parent
    migration = (root / "supabase/migrations/202607170009_key_listing_security.sql").read_text().lower()
    membership = (root / "supabase/migrations/202607170005_company_administration.sql").read_text().lower()
    listing = migration.split(
        "create or replace function public.company_admin_dashboard_keys_page", 1
    )[1].split(
        "create or replace function public.company_admin_revoke_dashboard_session_key", 1
    )[0]
    compact = "".join(listing.split())

    assert "securitydefinersetsearch_path=public,pg_temp" in compact
    assert "public.lock_company_actor_role(p_organization_id,p_actor_user_id)" in compact
    assert "andstatus='active'forupdate" in "".join(membership.split())
    assert "v_actor_roleisnullorv_actor_rolenotin('company_owner','company_admin','member','billing_admin')" in compact
    assert "v_actor_rolein('company_owner','company_admin')or(v_actor_rolein('member','billing_admin')andcredential.key_type='dashboard_session'andcredential.created_by=p_actor_user_id)" in compact
    assert "credential.organization_id=p_organization_id" in compact

    # Members and billing admins cannot observe service-account metadata or
    # another actor's session. Owner/admin visibility is the explicit first branch.
    assert "credential.key_type='dashboard_session'andcredential.created_by=p_actor_user_id" in compact
    assert "casewhenv_actor_rolein('company_owner','company_admin')thencredential.service_account_idelsenullendasservice_account_id" in compact
    assert "credential.key_hash" not in listing
    assert "fingerprint" not in listing
    assert "credential.owner_id" not in listing

    assert "((p_cursor_timeisnull)<>(p_cursor_idisnull))" in compact
    assert "(credential.created,credential.id)<(p_cursor_time,p_cursor_id)" in compact
    assert "orderbycredential.createddesc,credential.iddesc" in compact
    assert "limitleast(greatest(coalesce(p_limit,50),1),100)+1" in compact
    assert "jsonb_agg(to_jsonb(page)orderbypage.createddesc,page.iddesc)" in compact

    signature = "public.company_admin_dashboard_keys_page(\n    uuid,uuid,timestamptz,uuid,integer,text\n)"
    assert f"revoke all on function {signature}" in migration
    assert f"grant execute on function {signature}" in migration
    assert listing.count("dashboard_keys.read.denied") == 1


def test_dashboard_session_revoke_rpc_denies_cross_tenant_service_and_disabled_access_atomically():
    root = Path(__file__).parent.parent
    migration = (root / "supabase/migrations/202607170009_key_listing_security.sql").read_text().lower()
    revoke = migration.split(
        "create or replace function public.company_admin_revoke_dashboard_session_key", 1
    )[1]
    compact = "".join(revoke.split())

    assert "securitydefinersetsearch_path=public,pg_temp" in compact
    assert "public.lock_company_admin_namespace(p_organization_id)" in compact
    assert "public.lock_company_actor_role(p_organization_id,p_actor_user_id)" in compact
    assert "v_actor_roleisnullorv_actor_rolenotin('company_owner','company_admin','member','billing_admin')" in compact
    assert "whereorganization_id=p_organization_idandid=p_key_idforupdate" in compact
    assert "v_key.key_type<>'dashboard_session'" in compact
    assert "v_key.created_byisdistinctfromp_actor_user_id" in compact
    assert "'ok',false,'code','forbidden_or_not_found'" in compact

    update_position = compact.index(
        "updatepublic.api_keyssetrevoked_at=now()whereorganization_id=p_organization_idandid=p_key_idandkey_type='dashboard_session'"
    )
    audit_position = compact.index("'dashboard_session.revoked','api_key',p_key_id::text,'committed'"
    )
    assert update_position < audit_position
    assert "dashboard_session.revoke.denied" in revoke
    assert "dashboard_session.revoke.noop" in revoke

    signature = "public.company_admin_revoke_dashboard_session_key(\n    uuid,uuid,uuid,text\n)"
    assert f"revoke all on function {signature}" in migration
    assert f"grant execute on function {signature}" in migration
    assert "revoke all on function public.company_admin_revoke_key(uuid,uuid,uuid,text)" in migration
    assert "drop function if exists public.company_admin_revoke_key(uuid,uuid,uuid,text)" in migration


def test_key_endpoint_store_contract_freezes_opaque_cursor_and_strict_rpc_names():
    root = Path(__file__).parent.parent
    documentation = (root / "docs/COMPANY_ADMINISTRATION.md").read_text()

    assert "GET /v1/keys?cursor=<opaque>&limit=<1..100>" in documentation
    assert "HMAC-authenticated cursor bound to the organization and `dashboard_keys`" in documentation
    assert "It must not issue a service-role `GET api_keys`" in documentation
    assert "it must not pre-list the key" in documentation
    assert "company_admin_dashboard_keys_page(\n  uuid,uuid,timestamptz,uuid,integer,text\n)" in documentation
    assert "company_admin_revoke_dashboard_session_key(uuid,uuid,uuid,text)" in documentation
    assert "Do not restore it" in documentation


def test_active_membership_rpc_is_service_only_actor_bound_locked_and_bounded():
    root = Path(__file__).parent.parent
    migration = (root / "supabase/migrations/202607170011_active_memberships.sql").read_text().lower()
    compact = "".join(migration.split())

    assert "createorreplacefunctionpublic.company_admin_active_memberships(p_actor_user_iduuid,p_active_organization_iduuid)returnsjsonb" in compact
    assert "securitydefinersetsearch_path=public,pg_temp" in compact
    assert "public.lock_company_actor_role(p_active_organization_id,p_actor_user_id)" in compact
    assert "member.user_id=p_actor_user_id" in compact
    assert "member.status='active'" in compact
    assert "member.rolein('company_owner','company_admin','member','billing_admin')" in compact
    assert "orderbyis_currentdesc,company_name,company_idlimit100forshareofmember" in compact
    assert "'company_id',page.company_id,'company_name',page.company_name,'role',page.role" in compact
    signature = "public.company_admin_active_memberships(uuid,uuid)"
    assert f"revoke all on function {signature}" in migration
    assert f"grant execute on function {signature}" in migration
    assert "from public, anon, authenticated, service_role" in migration


def test_supabase_capabilities_uses_verified_actor_active_membership_rpc():
    calls = []
    active_company = "00000000-0000-4000-8000-000000000002"

    class Store:
        def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs["data"]))
            if path.endswith("lock_company_actor_role"):
                return "member"
            return {"ok": True, "items": [{
                "company_id": active_company,
                "company_name": "Verified company",
                "role": "member",
                "account_type": "company",
            }]}

    service = SupabaseCompanyAdminService(
        Store(), cursor_secret="c" * 40, invitee_pepper="i" * 40)
    principal = CompanyPrincipal(
        "00000000-0000-4000-8000-000000000001", active_company, "member")

    result = service.capabilities(principal, "request-active-membership-rpc")

    assert result["companies"] == [{
        "company_id": active_company,
        "company_name": "Verified company",
        "role": "member",
        "account_type": "company",
    }]
    assert calls[-1] == (
        "POST", "rpc/company_admin_active_memberships", {
            "p_actor_user_id": principal.actor_id,
            "p_active_organization_id": active_company,
        },
    )


def test_supabase_atomic_key_rpc_argument_contract():
    calls = []

    class Store:
        def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs["data"]))
            if path.endswith("create_dashboard_session_key"):
                return {"ok": True, "key_id": "00000000-0000-4000-8000-000000000010"}
            return {"ok": True, "key_id": kwargs["data"]["p_key_id"], "revoked": True}

    service = SupabaseCompanyAdminService(
        Store(), cursor_secret="c" * 40, invitee_pepper="i" * 40)
    principal = CompanyPrincipal(
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002", "company_admin")
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    service.create_dashboard_session_key(
        principal, "a" * 64, "bvt_prefix01", expires, "request-w3-create")
    service.revoke_key(
        principal, "00000000-0000-4000-8000-000000000010", "request-w3-revoke")
    assert calls == [
        ("POST", "rpc/company_admin_create_dashboard_session_key", {
            "p_organization_id": principal.company_id,
            "p_actor_user_id": principal.actor_id,
            "p_key_hash": "a" * 64,
            "p_key_prefix": "bvt_prefix01",
            "p_expires_at": expires,
            "p_request_id": "request-w3-create",
        }),
        ("POST", "rpc/company_admin_revoke_dashboard_session_key", {
            "p_organization_id": principal.company_id,
            "p_actor_user_id": principal.actor_id,
            "p_key_id": "00000000-0000-4000-8000-000000000010",
            "p_request_id": "request-w3-revoke",
        }),
    ]


def test_admin_audit_telemetry_is_content_free_fail_open_and_deduplicated(
        tmp_path, monkeypatch):
    import api.company_admin as company_admin

    store, service, org_a, _ = _setup(tmp_path)
    company_admin._admin_audit_telemetry_receipts.clear()
    events = []
    monkeypatch.setattr(
        company_admin._admin_telemetry, "info",
        lambda event, **fields: events.append((event, fields)),
    )
    configure_company_admin(
        service,
        lambda request: (
            _principal("member-a", org_a["id"], "member")
            if request.headers.get("x-test-principal") == "member-a"
            else _principal("owner-a", org_a["id"], "company_owner")
        ),
        lambda request: request.headers.get("x-request-id", "request-generated"),
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    # Successful reads do not append immutable audit evidence and must not emit.
    assert client.get(
        "/v1/company/capabilities",
        headers={"X-Request-ID": "request-telemetry-read"},
    ).status_code == 200
    assert events == []

    headers = {"X-Request-ID": "request-telemetry-mutation"}
    first = client.post("/v1/company/invitations", headers=headers, json={
        "email": "telemetry@example.com", "role": "member",
    })
    assert first.status_code == 200
    assert events == [("admin_audit_committed", {"outcome": "success"})]
    assert not any(
        key in events[0][1]
        for key in ("actor_id", "company_id", "target_id", "email", "payload", "request_id")
    )

    # A client retry with the same correlation ID can append denial evidence,
    # but it never double-counts the fixed telemetry event.
    retry = client.post("/v1/company/invitations", headers=headers, json={
        "email": "telemetry@example.com", "role": "member",
    })
    assert retry.status_code == 409
    assert events == [("admin_audit_committed", {"outcome": "success"})]

    denied = client.post("/v1/company/invitations", headers={
        "X-Request-ID": "request-telemetry-denied",
        "X-Test-Principal": "member-a",
    }, json={"email": "denied-telemetry@example.com", "role": "member"})
    assert denied.status_code == 403
    assert events == [
        ("admin_audit_committed", {"outcome": "success"}),
        ("admin_audit_committed", {"outcome": "rejected"}),
    ]

    def telemetry_outage(*_args, **_kwargs):
        raise RuntimeError("simulated telemetry outage")

    monkeypatch.setattr(company_admin._admin_telemetry, "info", telemetry_outage)
    committed = client.post("/v1/company/invitations", headers={
        "X-Request-ID": "request-telemetry-fail-open",
    }, json={"email": "fail-open@example.com", "role": "billing_admin"})
    assert committed.status_code == 200
    with sqlite3.connect(store.db_path) as db:
        audit = db.execute(
            "SELECT action,outcome FROM audit_events "
            "WHERE request_id='request-telemetry-fail-open'"
        ).fetchone()
    assert audit == ("member.invited", "committed")
    company_admin._admin_audit_telemetry_receipts.clear()


def test_fastapi_router_derives_company_and_role_from_server_principal(tmp_path):
    store, service, org_a, org_b = _setup(tmp_path)
    principals = {
        "owner-a": _principal("owner-a", org_a["id"], "company_owner"),
        "member-a": _principal("member-a", org_a["id"], "member"),
        "owner-b": _principal("owner-b", org_b["id"], "company_owner"),
    }

    def resolve(request: Request):
        return principals.get(request.headers.get("x-test-principal", ""),
                              CompanyPrincipal("", "", ""))

    configure_company_admin(service, resolve, lambda request: request.headers.get(
        "x-request-id", "request-generated"))
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    assert client.get("/v1/company/capabilities").status_code == 401
    owner_headers = {"X-Test-Principal": "owner-a", "X-Request-ID": "request-owner-api"}
    member_headers = {"X-Test-Principal": "member-a", "X-Request-ID": "request-member-api"}
    assert client.get("/v1/company/capabilities", headers=owner_headers).json()["company_id"] == org_a["id"]
    assert client.post("/v1/company/invitations", headers=member_headers, json={
        "email": "denied@example.com", "role": "member",
    }).status_code == 403
    # No request parameter can override the server-derived tenant.
    response = client.get(
        f"/v1/company/members?company_id={org_b['id']}&limit=101", headers=owner_headers)
    assert response.status_code == 422
    members = client.get(
        f"/v1/company/members?company_id={org_b['id']}&limit=50", headers=owner_headers)
    assert members.status_code == 200
    assert "member-b" not in {row["id"] for row in members.json()["items"]}

    service_account = client.post(
        "/v1/company/service-accounts",
        headers={**owner_headers, "X-Request-ID": "request-initial-key-api"},
        json={
            "name": "API production worker", "environment": "production",
            "scopes": ["proxy:invoke", "usage:write"], "expires_in_days": 90,
        },
    )
    assert service_account.status_code == 200
    assert service_account.headers["cache-control"] == "private, no-store"
    assert service_account.headers["pragma"] == "no-cache"
    payload = service_account.json()
    assert payload["secret_available_once"] is True
    assert service_account_key_context(
        store, hash_key(payload["api_key"]))["service_account_id"] == payload["id"]
