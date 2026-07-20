"""End-to-end Company A onboarding workflow with realistic customer counts."""

import sqlite3

from fastapi.testclient import TestClient

from api.auth import hash_key
from api.jobs import JobCrypto, JobService, SQLiteJobStore
from api.store import UsageStore
from brevitas.security import EnvelopeCipher, LocalTestKMS


class _Dispatcher:
    """Postgres/SQLite remains truth; Redis is only a production wake-up channel."""

    async def enqueue(self, _job_id):
        return None


def test_company_a_imports_100_existing_and_auto_onboards_50_new_customers(
        tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "company-a.db"))
    test_kms = LocalTestKMS(b"c" * 32, environ={"BREVITAS_ENV": "test"})
    jobs = JobService(
        SQLiteJobStore(store),
        crypto=JobCrypto(EnvelopeCipher(
            test_kms, key_id="company-a-workflow-test-key", key_version="1",
            wrap_algorithm=test_kms.algorithm,
        )),
        dispatcher=_Dispatcher(),
    )
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_job_service", jobs)
    monkeypatch.setattr(
        server,
        "_dashboard_user",
        lambda request: (
            "company-a-admin"
            if request.headers.get("authorization") == "Bearer company-a-session"
            else ""
        ),
    )
    server._auth_context_cache.clear()
    server._valid_key_cache.clear()
    client = TestClient(server.app)
    admin_headers = {"Authorization": "Bearer company-a-session"}

    # 1. Company A creates one backend credential for its production environment.
    created_key = client.post(
        "/v1/keys",
        headers=admin_headers,
        json={"name": "Company A production backend", "environment": "production"},
    )
    assert created_key.status_code == 200
    service_key = created_key.json()["api_key"]
    assert created_key.json()["secret_available_once"] is True

    # 2. Company A maps its 100 pre-existing database customers by exact stable ID.
    existing_payload = {
        "customers": [
            {
                "external_id": f"existing-customer-{index:03d}",
                "display_name": f"Existing Customer {index:03d}",
            }
            for index in range(1, 101)
        ]
    }
    imported = client.post(
        "/v1/customers/import",
        headers={"X-Brevitas-Key": service_key},
        json=existing_payload,
    )
    assert imported.status_code == 200
    assert imported.json()["count"] == 100
    original_internal_ids = {
        customer["external_id"]: customer["id"]
        for customer in imported.json()["customers"]
    }

    # 3. Every existing customer sends one AI job through Company A's backend.
    submitted_jobs = {}
    for index in range(1, 101):
        external_id = f"existing-customer-{index:03d}"
        response = client.post(
            "/v1/jobs",
            headers={
                "X-Brevitas-Key": service_key,
                "X-Brevitas-Customer-ID": external_id,
                "Idempotency-Key": f"existing-first-job-{index:03d}",
            },
            json={"task": "Generate the customer's scheduled finance summary"},
        )
        assert response.status_code == 202
        assert response.json()["created"] is True
        submitted_jobs[external_id] = response.json()["id"]

    # 4. Fifty new customers are not pre-imported. Their first AI job creates
    #    their customer record automatically under Company A's organization.
    for index in range(1, 51):
        external_id = f"new-customer-{index:03d}"
        response = client.post(
            "/v1/jobs",
            headers={
                "X-Brevitas-Key": service_key,
                "X-Brevitas-Customer-ID": external_id,
                "Idempotency-Key": f"new-first-job-{index:03d}",
            },
            json={"task": "Generate the customer's onboarding finance summary"},
        )
        assert response.status_code == 202
        assert response.json()["created"] is True
        submitted_jobs[external_id] = response.json()["id"]

    # 5. Re-importing old customers and retrying a new customer's request are
    #    idempotent: no duplicate customer and no duplicate job is created.
    imported_again = client.post(
        "/v1/customers/import",
        headers={"X-Brevitas-Key": service_key},
        json=existing_payload,
    )
    assert imported_again.status_code == 200
    assert {
        customer["external_id"]: customer["id"]
        for customer in imported_again.json()["customers"]
    } == original_internal_ids

    retried = client.post(
        "/v1/jobs",
        headers={
            "X-Brevitas-Key": service_key,
            "X-Brevitas-Customer-ID": "new-customer-001",
            "Idempotency-Key": "new-first-job-001",
        },
        json={"task": "Generate the customer's onboarding finance summary"},
    )
    assert retried.status_code == 202
    assert retried.json()["created"] is False
    assert retried.json()["id"] == submitted_jobs["new-customer-001"]

    customers = client.get("/v1/customers", headers=admin_headers)
    assert customers.status_code == 200
    assert len(customers.json()["customers"]) == 150
    assert len({row["id"] for row in customers.json()["customers"]}) == 150

    inventory = client.get("/v1/organization/inventory", headers=admin_headers)
    assert inventory.status_code == 200
    assert inventory.json()["counts"] == {
        "members": 1,
        "customers": 150,
        "keys": 1,
        "devices": 0,
        "installations": 0,
    }

    # 6. Customers never receive Brevitas keys. All 150 jobs belong to the one
    #    Company A organization and to 150 distinct customer records.
    with sqlite3.connect(store.db_path) as database:
        key_types = database.execute(
            "SELECT key_type, count(*) FROM api_keys GROUP BY key_type"
        ).fetchall()
        job_counts = database.execute(
            "SELECT count(*), count(distinct customer_id), count(distinct organization_id) "
            "FROM ai_jobs"
        ).fetchone()
        attributed = database.execute(
            "SELECT count(*) FROM ai_jobs job JOIN customers customer "
            "ON customer.id=job.customer_id AND customer.organization_id=job.organization_id"
        ).fetchone()[0]
    assert key_types == [("organization_service", 1)]
    assert job_counts == (150, 150, 1)
    assert attributed == 150

    # 7. A customer cannot read another customer's job, even with the same
    #    Company A service key.
    old_job = submitted_jobs["existing-customer-001"]
    assert client.get(
        f"/v1/jobs/{old_job}",
        headers={
            "X-Brevitas-Key": service_key,
            "X-Brevitas-Customer-ID": "existing-customer-001",
        },
    ).status_code == 200
    assert client.get(
        f"/v1/jobs/{old_job}",
        headers={
            "X-Brevitas-Key": service_key,
            "X-Brevitas-Customer-ID": "new-customer-001",
        },
    ).status_code == 404

    print(
        "Company A workflow passed: 100 existing customers imported, "
        "50 new customers auto-onboarded, 150 isolated jobs, 1 service key."
    )
