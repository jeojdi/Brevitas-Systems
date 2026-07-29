from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import SupabaseUsageStore, UsageStore
from api.company_admin import CompanyPrincipal, SupabaseCompanyAdminService


def _client(tmp_path, monkeypatch, user_id="onboarding-user"):
    import api.server as server

    store = UsageStore(str(tmp_path / "onboarding.db"))
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_user", lambda _request: user_id)
    return TestClient(server.app), store


def test_individual_bootstrap_creates_one_personal_workspace(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/organization/bootstrap", json={"account_type": "individual"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["company_name"] == "Personal workspace"
    assert response.json()["role"] == "company_owner"
    assert response.json()["account_type"] == "individual"
    assert response.json()["created"] is True
    assert store.member_organization("onboarding-user")["id"] == response.json()["company_id"]

    # A later presentation route cannot reclassify the persisted workspace.
    repeated = client.post(
        "/v1/organization/bootstrap",
        json={"account_type": "company", "name": "Untrusted company route"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["company_id"] == response.json()["company_id"]
    assert repeated.json()["account_type"] == "individual"
    assert repeated.json()["company_name"] == "Personal workspace"


def test_workspace_experience_migration_is_bounded_and_service_only():
    migration = (Path(__file__).parent.parent / "supabase/migrations/"
                 "202607200018_workspace_experiences.sql").read_text().lower()
    compact = "".join(migration.split())

    assert "check(account_typein('individual','company'))" in compact
    assert "public.ensure_workspace_organization(uuid,text,text)" in migration
    assert "p_account_type not in ('individual','company')" in migration
    assert "on conflict (legacy_owner_id) do update" in migration
    assert "set legacy_owner_id = excluded.legacy_owner_id" in migration
    assert "set account_type" not in migration
    assert "'account_type',page.account_type" in compact
    assert "from public, anon, authenticated, service_role" in migration
    assert "grant execute on function public.ensure_workspace_organization(uuid,text,text)\n    to service_role" in migration


def test_onboarding_evidence_migration_relaxes_only_authoritative():
    raw = (Path(__file__).parent.parent / "supabase/migrations/"
           "202607280004_onboarding_local_proxy_evidence.sql").read_text()
    migration = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("--"))

    assert "usage.authoritative is true" not in migration
    assert migration.count("usage.receipt_source = 'proxy'") == 2
    assert migration.count("credential.key_type = 'device'") == 4
    assert migration.count("activation.action = 'device_key.activated'") == 4
    assert migration.count("installation.device_auth_receipt_id is not null") == 4
    assert migration.count("usage.ts >= installation.installed_at") == 2
    assert "grant execute on function public.organization_onboarding_status(uuid,uuid)\n    to service_role" in migration
    assert "grant execute on function public.complete_organization_onboarding(uuid,uuid,text)\n    to service_role" in migration
    assert "from public, anon, authenticated, service_role" in migration


def test_company_bootstrap_requires_name_and_is_idempotent(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch, "company-founder")

    missing = client.post(
        "/v1/organization/bootstrap", json={"account_type": "company", "name": "  "})
    assert missing.status_code == 422

    created = client.post(
        "/v1/organization/bootstrap",
        json={"account_type": "company", "name": "  Acme   Systems  "},
    )
    repeated = client.post(
        "/v1/organization/bootstrap",
        json={"account_type": "company", "name": "Untrusted Rename"},
    )

    assert created.status_code == 200
    assert created.json()["company_name"] == "Acme Systems"
    assert created.json()["created"] is True
    assert repeated.status_code == 200
    assert repeated.json()["company_id"] == created.json()["company_id"]
    assert repeated.json()["company_name"] == "Acme Systems"
    assert repeated.json()["account_type"] == "company"
    assert repeated.json()["created"] is False
    assert store.member_organization("company-founder")["name"] == "Acme Systems"


def test_workspace_bootstrap_requires_authentication(tmp_path, monkeypatch):
    client, _store = _client(tmp_path, monkeypatch, "")

    response = client.post(
        "/v1/organization/bootstrap", json={"account_type": "individual"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Sign in to create a workspace"}


def test_workspace_bootstrap_rejects_control_characters(tmp_path, monkeypatch):
    client, _store = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/organization/bootstrap",
        json={"account_type": "company", "name": "Acme\nInjected"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid workspace name"}


def _configured_bvx_evidence(store, organization_id, user_id, *, authoritative=True,
                             receipt_source="proxy", request_id="onboarding-proxy-1"):
    raw_key = f"bvt_device_{user_id}"
    key_hash = hash_key(raw_key)
    device_hash = hash_key(f"device-code-{user_id}")
    store.create_device_request(
        device_hash,
        (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )
    assert store.approve_device_request(
        device_hash, user_id, key_hash, "kms-device-ciphertext", organization_id,
    )
    consumed = store.consume_device_request_idempotent(
        device_hash, key_hash, f"device-activation-{user_id}",
    )
    assert consumed and consumed["key_hash"] == key_hash
    store.register_installation(
        organization_id, "", "11111111-1111-4111-8111-111111111111",
        "workspace", "test", "1.2.3", "device-fingerprint-1",
        client_name="bvx", registration_key_hash=key_hash,
    )
    store.record_usage(
        key_hash, 10, 10, owner_id=user_id,
        organization_id=organization_id, authoritative=authoritative,
        receipt_source=receipt_source, request_id=request_id,
    )
    return raw_key


def test_onboarding_survives_reload_and_rejects_self_attestation(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch, "durable-owner")
    created = client.post(
        "/v1/organization/bootstrap", json={"account_type": "individual"})
    organization_id = created.json()["company_id"]

    reloaded = client.get("/v1/organization/onboarding")
    unchecked = client.post("/v1/organization/onboarding/complete")

    assert reloaded.status_code == 200
    assert reloaded.headers["cache-control"] == "private, no-store"
    assert reloaded.json() == {
        "company_id": organization_id,
        "status": "pending",
        "cli_connected": False,
        "proxied_request_observed": False,
        "completed_at": "",
    }
    assert unchecked.status_code == 409
    assert "bvx install" in unchecked.json()["detail"]
    assert store.onboarding_status("durable-owner", organization_id)["status"] == "pending"


def test_onboarding_accepts_local_proxy_receipt_from_bound_device(
        tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch, "evidence-owner")
    created = client.post(
        "/v1/organization/bootstrap", json={"account_type": "individual"})
    organization_id = created.json()["company_id"]

    raw_key = _configured_bvx_evidence(
        store, organization_id, "evidence-owner", authoritative=False,
        receipt_source="sdk", request_id="onboarding-sdk-only",
    )
    caller_reported = client.post("/v1/organization/onboarding/complete")
    assert caller_reported.status_code == 409
    assert "successful request" in caller_reported.json()["detail"]

    # The released BVX CLI runs a local proxy that reports its provider receipt
    # over POST /v1/usage, which records authoritative=False. That receipt from
    # the bound device key is onboarding evidence.
    store.record_usage(
        hash_key(raw_key), 10, 10, owner_id="evidence-owner",
        organization_id=organization_id, authoritative=False,
        receipt_source="proxy", request_id="onboarding-local-proxy-receipt",
    )
    completed = client.post("/v1/organization/onboarding/complete")
    repeated = client.post("/v1/organization/onboarding/complete")

    assert completed.status_code == 200
    assert completed.json()["status"] == "complete"
    assert completed.json()["cli_connected"] is True
    assert completed.json()["proxied_request_observed"] is True
    assert repeated.status_code == 200
    reopened = UsageStore(store.db_path)
    assert reopened.onboarding_status("evidence-owner", organization_id)["status"] == "complete"
    with reopened._conn() as db:
        organization = db.execute(
            "SELECT onboarding_completed_at,onboarding_evidence_usage_id "
            "FROM organizations WHERE id=?", (organization_id,),
        ).fetchone()
        audits = db.execute(
            "SELECT count(*) FROM audit_events WHERE organization_id=? "
            "AND action='organization.onboarding.completed' AND details='{}'",
            (organization_id,),
        ).fetchone()[0]
        persisted = str(db.execute(
            "SELECT onboarding_completed_by FROM organizations WHERE id=?",
            (organization_id,),
        ).fetchone()[0])
    assert organization[0]
    assert organization[1] > 0
    assert audits == 1
    assert persisted == "evidence-owner"
    assert raw_key not in persisted


def test_device_activation_alone_registers_installation_for_cli_gate(
        tmp_path, monkeypatch):
    """The shipped local-proxy BVX never calls /v1/installations, so device-key
    activation must itself register the installation the cli_connected gate joins
    against. Without a separate register_installation call, activation alone must
    flip cli_connected (202607280005)."""
    client, store = _client(tmp_path, monkeypatch, "device-only-owner")
    organization_id = client.post(
        "/v1/organization/bootstrap", json={"account_type": "individual"},
    ).json()["company_id"]

    raw_key = "bvt_device_only"
    key_hash = hash_key(raw_key)
    device_hash = hash_key("device-code-only")
    store.create_device_request(
        device_hash,
        (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
    assert store.approve_device_request(
        device_hash, "device-only-owner", key_hash, "kms-ct", organization_id)
    consumed = store.consume_device_request_idempotent(
        device_hash, key_hash, "device-only-activation")
    assert consumed and consumed["key_hash"] == key_hash

    # No register_installation call — activation alone registered the row.
    installs = store.list_installations(organization_id)
    assert len(installs) == 1
    assert installs[0]["client_name"] == "bvx"
    assert installs[0]["bvx_version"] == "device-auth"

    status = store.onboarding_status("device-only-owner", organization_id)
    assert status["cli_connected"] is True
    assert status["proxied_request_observed"] is False

    # A local-proxy receipt from the same bound device key then flips the proxy
    # evidence, without any separate installation registration.
    store.record_usage(
        key_hash, 10, 10, owner_id="device-only-owner",
        organization_id=organization_id, authoritative=False,
        receipt_source="proxy", request_id="device-only-proxy-receipt")
    status = store.onboarding_status("device-only-owner", organization_id)
    assert status["proxied_request_observed"] is True

    # Idempotent re-consume returns the retained receipt and never adds a second
    # installation for the same activation.
    again = store.consume_device_request_idempotent(
        device_hash, key_hash, "device-only-activation")
    assert again and again["already_consumed"] is True
    assert len(store.list_installations(organization_id)) == 1


def test_installation_on_activation_migration_registers_and_backfills():
    raw = (Path(__file__).parent.parent / "supabase/migrations/"
           "202607280005_installation_on_device_activation.sql").read_text()
    migration = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("--"))

    # Redefines the atomic consume RPC to register the gate-satisfying row bound
    # to the just-activated device key and its consumption receipt.
    assert ("create or replace function public.consume_bvx_device_idempotent"
            in migration)
    assert migration.count("insert into public.installations") == 2
    assert "'bvx', 'device-auth'" in migration
    assert "registration_key_id" in migration
    assert "device_auth_receipt_id" in migration
    assert "v_exchange.key_hash, v_key_id, v_receipt.id" in migration
    # installations.device_id has a composite FK to devices, so a real devices
    # row is created for both the live path and the backfill.
    assert migration.count("insert into public.devices") == 2
    # Backfill only touches activated device keys with no installation yet.
    assert "not exists (" in migration
    assert "device_key.activated" in migration
    assert ("grant execute on function "
            "public.consume_bvx_device_idempotent(text,text,text)\n"
            "    to service_role" in migration)
    assert "from public, anon, authenticated, service_role" in migration


def test_onboarding_rejects_forged_install_and_mismatched_usage(tmp_path):
    store = UsageStore(str(tmp_path / "forged-onboarding.db"))
    owner_id = "forged-owner"
    organization_id = store.ensure_organization(owner_id, "Forged")["id"]

    # A row inserted without the authenticated registration binding is not a CLI
    # connection, even if it looks like BVX and the company has proxy telemetry.
    forged_key = hash_key("bvt_forged_onboarding_key")
    store.create_key(
        forged_key, "forged device", owner_id=owner_id,
        organization_id=organization_id, key_type="device",
        scopes=["proxy:invoke", "installations:register"],
    )
    store.register_installation(
        organization_id, "", "22222222-2222-4222-8222-222222222222",
        "forged", "test", "9.9.9", "forged-device",
        client_name="bvx",
    )
    store.record_usage(
        forged_key, 10, 10, owner_id=owner_id,
        organization_id=organization_id, authoritative=True,
        receipt_source="proxy", request_id="forged-authoritative-proxy",
    )
    status = store.onboarding_status(owner_id, organization_id)
    assert status["status"] == "pending"
    assert status["cli_connected"] is False
    assert status["proxied_request_observed"] is False

    device_key = _configured_bvx_evidence(
        store, organization_id, owner_id, authoritative=False,
        receipt_source="sdk", request_id="forged-sdk-only",
    )
    status = store.onboarding_status(owner_id, organization_id)
    assert status["cli_connected"] is True
    assert status["proxied_request_observed"] is False

    other_key = hash_key("bvt_wrong_onboarding_key")
    store.create_key(
        other_key, "wrong key", owner_id=owner_id,
        organization_id=organization_id, key_type="legacy",
        scopes=["proxy:invoke", "installations:register"],
    )
    store.record_usage(
        other_key, 10, 10, owner_id=owner_id,
        organization_id=organization_id, authoritative=True,
        receipt_source="proxy", request_id="wrong-key-authoritative",
    )
    assert store.complete_onboarding(
        owner_id, organization_id, "wrong-key-onboarding-check",
    )["status"] == "pending"

    store.record_usage(
        hash_key(device_key), 10, 10, owner_id=owner_id,
        organization_id=organization_id, authoritative=True,
        receipt_source="proxy", request_id="matching-device-authoritative",
    )
    assert store.complete_onboarding(
        owner_id, organization_id, "matching-device-onboarding-check",
    )["status"] == "complete"


def test_onboarding_evidence_cannot_cross_company_boundary(tmp_path):
    store = UsageStore(str(tmp_path / "cross-company-onboarding.db"))
    first = store.ensure_organization("first-owner", "First")
    second = store.ensure_organization("second-owner", "Second")
    _configured_bvx_evidence(store, first["id"], "first-owner")

    try:
        store.complete_onboarding(
            "second-owner", first["id"], "cross-company-onboarding-denied")
    except PermissionError:
        pass
    else:
        raise AssertionError("cross-company actor completed onboarding")
    assert store.onboarding_status("first-owner", first["id"])["status"] == "pending"
    assert store.onboarding_status("second-owner", second["id"])["status"] == "pending"


def test_supabase_installation_registration_is_atomic_and_preserves_repository(
        monkeypatch):
    organization_id = "11111111-1111-4111-8111-111111111111"
    installation_id = "22222222-2222-4222-8222-222222222222"
    key_hash = "a" * 64
    calls = []
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "installations":
            return [{"repository_id": "repo-1", "repository": "owner/repo"}]
        return {
            "ok": True,
            "id": installation_id,
            "last_seen_at": "2026-07-20 12:00:00+00",
            "device_authorization_bound": True,
        }

    monkeypatch.setattr(store, "_request", request)
    result = store.register_installation(
        organization_id, "untrusted-service-account", installation_id,
        None, "production", "1.2.3", "device-1",
        device_platform="darwin", device_arch="arm64", client_name="bvx",
        registration_key_hash=key_hash,
    )

    assert result["id"] == installation_id
    assert result["device_authorization_bound"] is True
    assert [call[1] for call in calls] == [
        "installations", "rpc/register_bvx_installation"]
    assert calls[1][2]["data"] == {
        "p_organization_id": organization_id,
        "p_registration_key_hash": key_hash,
        "p_installation_id": installation_id,
        "p_device_fingerprint": "device-1",
        "p_repository_id": "repo-1",
        "p_repository": "owner/repo",
        "p_environment": "production",
        "p_device_platform": "darwin",
        "p_device_arch": "arm64",
        "p_client_name": "bvx",
        "p_bvx_version": "1.2.3",
    }


def test_supabase_invitation_acceptance_normalizes_frontend_contract():
    organization_id = "11111111-1111-4111-8111-111111111111"
    calls = []

    class Store:
        def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs["data"]))
            return {
                "ok": True,
                "organization_id": organization_id,
                "role": "member",
            }

    service = SupabaseCompanyAdminService(
        Store(), cursor_secret="c" * 40, invitee_pepper="i" * 40)
    result = service.accept_invitation(
        CompanyPrincipal(
            "22222222-2222-4222-8222-222222222222",
            "",
            "",
            "a" * 64,
        ),
        "bvi_" + "x" * 43,
        "request-invitation-contract",
    )

    assert result == {
        "company_id": organization_id,
        "role": "member",
        "status": "accepted",
    }
    assert calls[0][1] == "rpc/company_admin_accept_invitation"
