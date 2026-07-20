from fastapi.testclient import TestClient

from api.auth import hash_key
from api.jobs import InMemoryJobStore, JobCrypto, JobService
from api.store import UsageStore
from brevitas.security import EnvelopeCipher, LocalTestKMS
from brevitas.security import KMSUnavailable


def job_crypto():
    kms = LocalTestKMS(b"a" * 32, environ={"BREVITAS_ENV": "test"})
    return JobCrypto(EnvelopeCipher(
        kms, key_id="test-job-key", key_version="1",
        wrap_algorithm=kms.algorithm,
    ))


class Dispatcher:
    async def enqueue(self, _job_id):
        return None


def configure_company(store, user, raw_key):
    organization = store.ensure_organization(user, f"{user} org")
    service = store.ensure_service_account(organization["id"], "production", user)
    store.create_key(
        hash_key(raw_key), "backend", owner_id=user, organization_id=organization["id"],
        service_account_id=service["id"], key_type="organization_service",
        scopes=["proxy:invoke", "customer:route", "customer:auto_provision",
                "jobs:create", "jobs:read", "jobs:cancel"],
    )
    return organization


def test_job_api_is_idempotent_and_customer_scoped(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "jobs.db"))
    configure_company(store, "company-a", "bvt_company_a")
    configure_company(store, "company-b", "bvt_company_b")
    jobs = JobService(InMemoryJobStore(), crypto=job_crypto(),
                      dispatcher=Dispatcher())
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_job_service", jobs)
    client = TestClient(server.app)

    headers_a = {
        "X-Brevitas-Key": "bvt_company_a",
        "X-Brevitas-Customer-ID": "customer-001",
        "Idempotency-Key": "finance-job-1",
    }
    first = client.post("/v1/jobs", headers=headers_a,
                        json={"task": "analyze cash flow", "messages": ["safe data"]})
    second = client.post("/v1/jobs", headers=headers_a,
                         json={"task": "analyze cash flow", "messages": ["safe data"]})
    assert first.status_code == second.status_code == 202
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert first.json()["id"] == second.json()["id"]

    job_id = first.json()["id"]
    assert client.get(f"/v1/jobs/{job_id}", headers=headers_a).status_code == 200
    headers_other = {
        "X-Brevitas-Key": "bvt_company_a",
        "X-Brevitas-Customer-ID": "customer-002",
    }
    assert client.get(f"/v1/jobs/{job_id}", headers=headers_other).status_code == 404
    headers_other_org = {
        "X-Brevitas-Key": "bvt_company_b",
        "X-Brevitas-Customer-ID": "customer-001",
    }
    assert client.post(f"/v1/jobs/{job_id}/cancel", headers=headers_other_org).status_code == 404
    cancelled = client.post(f"/v1/jobs/{job_id}/cancel", headers=headers_a)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_jobs_require_customer_attribution_and_scope(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "jobs-auth.db"))
    org = store.ensure_organization("company-a", "Company A")
    account = store.ensure_service_account(org["id"], "production", "company-a")
    store.create_key(hash_key("bvt_no_jobs"), "limited", owner_id="company-a",
                     organization_id=org["id"], service_account_id=account["id"],
                     key_type="organization_service",
                     scopes=["proxy:invoke", "customer:route", "customer:auto_provision"])
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_job_service", JobService(
        InMemoryJobStore(), crypto=job_crypto(), dispatcher=Dispatcher()))
    client = TestClient(server.app)

    no_customer = client.post("/v1/jobs", headers={"X-Brevitas-Key": "bvt_no_jobs"},
                              json={"task": "task"})
    assert no_customer.status_code == 403
    no_scope = client.post("/v1/jobs", headers={
        "X-Brevitas-Key": "bvt_no_jobs", "X-Brevitas-Customer-ID": "customer-1",
    }, json={"task": "task"})
    assert no_scope.status_code == 403


def test_job_kms_outage_returns_retryable_dependency_503(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "jobs-kms.db"))
    configure_company(store, "company-a", "bvt_company_a")

    class UnavailableJobs:
        async def submit(self, *_args, **_kwargs):
            raise KMSUnavailable("temporary KMS outage")

    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_job_service", UnavailableJobs())
    server._auth_context_cache.clear()
    response = TestClient(server.app).post("/v1/jobs", headers={
        "X-Brevitas-Key": "bvt_company_a",
        "X-Brevitas-Customer-ID": "customer-001",
    }, json={"task": "safe task"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Credential security dependency unavailable"}
    assert response.headers["retry-after"] == "1"
