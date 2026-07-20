from fastapi.testclient import TestClient

from api.store import UsageStore
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
    assert response.json()["created"] is True
    assert store.member_organization("onboarding-user")["id"] == response.json()["company_id"]


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
