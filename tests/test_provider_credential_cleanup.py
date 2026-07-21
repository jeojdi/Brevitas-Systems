import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from api.auth import hash_key
from api.company_admin import CompanyPrincipal, SQLiteCompanyAdminService
from api.store import SupabaseUsageStore, UsageStore


ROOT = Path(__file__).resolve().parents[1]


def _setup_company(tmp_path):
    store = UsageStore(str(tmp_path / "provider-cleanup.db"))
    organization = store.ensure_organization("owner-a", "Company A")
    other = store.ensure_organization("owner-b", "Company B")
    service = SQLiteCompanyAdminService(
        store.db_path, cursor_secret="provider-cleanup-cursor-secret-value"
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE organization_members SET role='company_owner',status='active' "
            "WHERE user_id IN ('owner-a','owner-b')"
        )
    return store, service, organization, other


def _principal(owner_id, organization):
    return CompanyPrincipal(owner_id, organization["id"], "company_owner")


def _future(hours=1):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _provider_rows(store):
    with sqlite3.connect(store.db_path) as db:
        return db.execute(
            "SELECT key_hash,provider_api_key FROM provider_config ORDER BY key_hash"
        ).fetchall()


def _insert_legacy_provider_config(store, key_hash, ciphertext):
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO provider_config(key_hash,provider,provider_api_key,model) "
            "VALUES(?, 'openai', ?, 'gpt-test')",
            (key_hash, ciphertext),
        )


def test_direct_revocation_and_deletion_remove_provider_credentials(tmp_path):
    store, _, organization, _ = _setup_company(tmp_path)
    first_hash = hash_key("bvt_direct_revoke")
    store.create_key(
        first_hash, "direct revoke", owner_id="owner-a",
        organization_id=organization["id"], key_type="dashboard_session",
        created_by="owner-a", expires_at=_future(),
        request_id="request-direct-provider-revoke", actor_role="company_owner",
    )
    store.set_provider_config(first_hash, "openai", "ciphertext-direct", "gpt-test")
    with sqlite3.connect(store.db_path) as db:
        key_id = db.execute(
            "SELECT id FROM api_keys WHERE key_hash=?", (first_hash,)
        ).fetchone()[0]

    assert store.revoke_organization_key(
        organization["id"], key_id, "owner-a",
        "request-provider-revocation", "company_owner",
    ) is True
    assert store.get_provider_config(first_hash) is None
    with pytest.raises(ValueError, match="active key"):
        store.set_provider_config(
            first_hash, "openai", "ciphertext-after-revoke", "gpt-test"
        )

    second_hash = hash_key("bvt_direct_delete")
    store.create_key(second_hash, "direct delete", owner_id="owner-a")
    store.set_provider_config(second_hash, "anthropic", "ciphertext-delete", "model-test")
    assert store.delete_key(second_hash, second_hash) is True
    assert store.get_provider_config(second_hash) is None


def test_dashboard_and_service_account_revocation_remove_only_scoped_credentials(
        tmp_path):
    store, service, organization, other = _setup_company(tmp_path)
    owner = _principal("owner-a", organization)
    dashboard_hash = hash_key("bvt_dashboard_cleanup")
    dashboard = service.create_dashboard_session_key(
        owner, dashboard_hash, "bvt_dashclean", _future(),
        "request-dashboard-provider-create",
    )
    store.set_provider_config(
        dashboard_hash, "openai", "ciphertext-dashboard", "gpt-test"
    )
    service.revoke_key(
        owner, dashboard["key_id"], "request-dashboard-provider-revoke"
    )
    assert store.get_provider_config(dashboard_hash) is None

    account = service.create_service_account(
        owner, "Cleanup service", "production", ["proxy:invoke"], 30,
        "request-provider-service-create",
    )
    first_hash = hash_key(account["api_key"])
    second_hash = hash_key("bvt_second_service_credential")
    store.create_key(
        second_hash, "second service key", organization_id=organization["id"],
        service_account_id=account["id"], key_type="organization_service",
        created_by="owner-a", expires_at=_future(24),
        request_id="request-second-service-credential", actor_role="company_owner",
    )
    store.set_provider_config(first_hash, "openai", "ciphertext-service-one", "gpt-test")
    store.set_provider_config(second_hash, "openai", "ciphertext-service-two", "gpt-test")

    foreign_hash = hash_key("bvt_foreign_active_credential")
    store.create_key(
        foreign_hash, "foreign active", owner_id="owner-b",
        organization_id=other["id"], key_type="dashboard_session",
        created_by="owner-b", expires_at=_future(),
        request_id="request-foreign-active-provider", actor_role="company_owner",
    )
    store.set_provider_config(
        foreign_hash, "openai", "ciphertext-foreign-active", "gpt-test"
    )

    service.revoke_service_account(
        owner, account["id"], "request-provider-service-revoke"
    )
    assert store.get_provider_config(first_hash) is None
    assert store.get_provider_config(second_hash) is None
    assert store.get_provider_config(foreign_hash) is not None

    audit_text = ""
    with sqlite3.connect(store.db_path) as db:
        audit_text = repr(db.execute(
            "SELECT action,target_type,target_id,details FROM audit_events"
        ).fetchall())
    for forbidden in (
        "ciphertext-dashboard", "ciphertext-service-one",
        "ciphertext-service-two", "ciphertext-foreign-active",
    ):
        assert forbidden not in audit_text


def test_rotation_cleans_old_context_without_copying_or_cross_tenant_deletion(tmp_path):
    store, service, organization, other = _setup_company(tmp_path)
    owner = _principal("owner-a", organization)
    account = service.create_service_account(
        owner, "Rotating service", "production", ["proxy:invoke"], 30,
        "request-provider-rotation-account",
    )
    old_hash = hash_key(account["api_key"])
    store.set_provider_config(old_hash, "openai", "ciphertext-old-context", "gpt-test")

    unrelated_hash = hash_key("bvt_unrelated_tenant_key")
    store.create_key(
        unrelated_hash, "unrelated", owner_id="owner-b",
        organization_id=other["id"], key_type="dashboard_session",
        created_by="owner-b", expires_at=_future(),
        request_id="request-unrelated-provider-key", actor_role="company_owner",
    )
    store.set_provider_config(
        unrelated_hash, "anthropic", "ciphertext-unrelated", "model-test"
    )

    replacement = service.rotate_service_key(
        owner, account["id"], 30, "request-provider-key-rotation"
    )
    replacement_hash = hash_key(replacement["api_key"])

    assert store.get_provider_config(old_hash) is None
    assert store.get_provider_config(replacement_hash) is None
    assert store.key_exists(replacement_hash) is True
    assert store.get_provider_config(unrelated_hash) is not None


def test_expiry_cleanup_is_bounded_and_preserves_active_tenants(tmp_path):
    store, _, organization, other = _setup_company(tmp_path)
    expired_hashes = []
    for index in range(3):
        key_hash = hash_key(f"bvt_expired_provider_{index}")
        expired_hashes.append(key_hash)
        store.create_key(
            key_hash, f"expired-{index}", owner_id="owner-a",
            organization_id=organization["id"], key_type="legacy",
            created_by="owner-a", expires_at="2020-01-01T00:00:00+00:00",
            request_id=f"request-expired-provider-{index}",
            actor_role="company_owner",
        )
        _insert_legacy_provider_config(
            store, key_hash, f"ciphertext-expired-{index}"
        )

    active_hashes = []
    for owner_id, tenant in (("owner-a", organization), ("owner-b", other)):
        key_hash = hash_key(f"bvt_active_provider_{owner_id}")
        active_hashes.append(key_hash)
        store.create_key(
            key_hash, "active", owner_id=owner_id,
            organization_id=tenant["id"], key_type="dashboard_session",
            created_by=owner_id, expires_at=_future(),
            request_id=f"request-active-provider-{owner_id}",
            actor_role="company_owner",
        )
        store.set_provider_config(
            key_hash, "openai", f"ciphertext-active-{owner_id}", "gpt-test"
        )

    orphan_hash = hash_key("bvt_missing_provider_key")
    _insert_legacy_provider_config(store, orphan_hash, "ciphertext-orphan")
    assert store.purge_provider_configs(limit=2) == 2
    rows = dict(_provider_rows(store))
    assert sum(
        key_hash in rows for key_hash in (*expired_hashes, orphan_hash)
    ) == 2
    assert all(key_hash in rows for key_hash in active_hashes)

    assert store.purge_provider_configs(limit=10) == 2
    rows = dict(_provider_rows(store))
    assert all(key_hash not in rows for key_hash in expired_hashes)
    assert all(key_hash in rows for key_hash in active_hashes)
    assert "ciphertext-orphan" not in rows.values()


def test_supabase_cleanup_adapter_calls_only_bounded_service_rpc(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role-test")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return 7

    monkeypatch.setattr(store, "_request", request)
    assert store.purge_provider_configs(50_000) == 7
    assert calls == [(
        "POST", "rpc/purge_expired_provider_configs", {"data": {"p_limit": 1000}}
    )]


def test_migration_and_worker_enforce_cleanup_without_credential_logging():
    migration = (ROOT / "supabase/migrations/202607200008_provider_credential_cleanup.sql").read_text()
    compact = "".join(migration.lower().split())
    worker = (ROOT / "api/worker.py").read_text()

    assert "api_keys_provider_config_cleanup" in migration
    assert "provider_config_active_key_guard" in migration
    assert "require_active_provider_config_key" in migration
    assert "for update of credential" in migration.lower()
    assert "after update of revoked_at or delete" in migration.lower()
    assert "on delete cascade" in migration.lower()
    assert "purge_expired_provider_configs" in migration
    assert "for update of config skip locked" in migration.lower()
    assert "least(coalesce(p_limit,500),1000)" in compact
    assert "revokeallonfunctionpublic.purge_expired_provider_configs(integer)frompublic,anon,authenticated" in compact
    assert "grantexecuteonfunctionpublic.purge_expired_provider_configs(integer)toservice_role" in compact
    assert "insert into public.audit" not in migration.lower()
    assert "_store.purge_provider_configs" in worker
    assert '"provider_credential_cleanup_failed"' in worker
    assert "provider_api_key" not in worker
