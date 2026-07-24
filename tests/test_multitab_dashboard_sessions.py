import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from api.auth import hash_key
from api.store import DASHBOARD_SESSION_PER_ACTOR_CAP, UsageStore


def _create_dashboard_key(store, organization_id, actor_id, index):
    raw_key = f"bvt_multitab_{actor_id}_{index}_{organization_id}"
    store.create_key(
        hash_key(raw_key), "dashboard session",
        owner_id=actor_id,
        organization_id=organization_id,
        key_type="dashboard_session",
        scopes=["proxy:invoke", "usage:read_own"],
        environment="dashboard",
        key_prefix=raw_key[:12],
        created_by=actor_id,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        request_id=f"request-multitab-{actor_id}-{index}",
        actor_role="member",
    )
    return raw_key


def test_sqlite_dashboard_sessions_support_tabs_and_rotate_only_same_actor_tenant(tmp_path):
    store = UsageStore(str(tmp_path / "multitab.db"))
    company_a = store.ensure_organization("owner-a")["id"]
    company_b = store.ensure_organization("owner-b")["id"]

    actor_keys = [
        _create_dashboard_key(store, company_a, "actor-a", index)
        for index in range(DASHBOARD_SESSION_PER_ACTOR_CAP)
    ]
    colleague_key = _create_dashboard_key(store, company_a, "colleague-a", 1)
    other_tenant_key = _create_dashboard_key(store, company_b, "actor-a", 1)

    assert all(store.key_exists(hash_key(key)) for key in actor_keys)
    assert store.key_exists(hash_key(colleague_key))
    assert store.key_exists(hash_key(other_tenant_key))

    replacement = _create_dashboard_key(
        store, company_a, "actor-a", DASHBOARD_SESSION_PER_ACTOR_CAP,
    )
    assert not store.key_exists(hash_key(actor_keys[0]))
    assert all(store.key_exists(hash_key(key)) for key in actor_keys[1:])
    assert store.key_exists(hash_key(replacement))
    assert store.key_exists(hash_key(colleague_key))
    assert store.key_exists(hash_key(other_tenant_key))

    with sqlite3.connect(store.db_path) as db:
        active = db.execute(
            "SELECT count(*) FROM api_keys WHERE organization_id=? "
            "AND key_type='dashboard_session' AND created_by=? AND revoked_at=''",
            (company_a, "actor-a"),
        ).fetchone()[0]
        rotated = db.execute(
            "SELECT count(*) FROM audit_events WHERE organization_id=? "
            "AND actor_user_id=? AND action='dashboard_session.rotated'",
            (company_a, "actor-a"),
        ).fetchone()[0]
    assert active == DASHBOARD_SESSION_PER_ACTOR_CAP
    assert rotated == 1


def test_sqlite_dashboard_session_issuance_cleans_expired_rows(tmp_path):
    store = UsageStore(str(tmp_path / "expiry.db"))
    company_id = store.ensure_organization("owner-a")["id"]
    expired_key = "bvt_expired_multitab"
    with store._conn() as db:
        db.execute(
            "INSERT INTO api_keys(id,key_hash,name,created,owner_id,organization_id,"
            "key_type,scopes,environment,key_prefix,created_by,expires_at) "
            "VALUES('expired-session',?, 'dashboard session',?,?,?,?,?,?,?,?,?)",
            (
                hash_key(expired_key),
                (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                "owner-a", company_id, "dashboard_session", "usage:read_own",
                "dashboard", "bvt_expired", "owner-a",
                (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            ),
        )

    current_key = _create_dashboard_key(store, company_id, "owner-a", 1)
    assert not store.key_exists(hash_key(expired_key))
    assert store.key_exists(hash_key(current_key))
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT revoked_at FROM api_keys WHERE id='expired-session'",
        ).fetchone()[0]


def test_rotated_dashboard_session_bypasses_process_auth_cache(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "auth-cache.db"))
    company_id = store.ensure_organization("owner-a")["id"]
    first_key = _create_dashboard_key(store, company_id, "owner-a", 0)
    monkeypatch.setattr(server, "_store", store)
    server._auth_context_cache.clear()
    assert server._auth_context_for_key(hash_key(first_key)).key_type == "dashboard_session"

    for index in range(1, DASHBOARD_SESSION_PER_ACTOR_CAP + 1):
        _create_dashboard_key(store, company_id, "owner-a", index)

    with pytest.raises(HTTPException) as rejected:
        server._auth_context_for_key(hash_key(first_key))
    assert rejected.value.status_code == 401


def test_postgres_forward_migration_uses_bounded_actor_tenant_rotation():
    migration = Path(
        "supabase/migrations/202607200009_multitab_dashboard_sessions.sql",
    ).read_text()
    compact = "".join(migration.lower().split())

    assert "v_actor_active_count-7" in compact
    assert "v_active_count-999" in compact
    assert "ifv_actor_roleisnullorv_actor_rolenotin(" in compact
    assert "credential.created_by=p_actor_user_id" in compact
    assert "credential.organization_id=p_organization_id" in compact
    assert "'dashboard_session.rotated','api_key'" in compact
    assert "and(expires_atisnullorexpires_at<=now())" in compact
    assert "setrevoked_at=now()whereorganization_id=p_organization_idandkey_type='dashboard_session'andcreated_by=p_actor_user_id" not in compact
