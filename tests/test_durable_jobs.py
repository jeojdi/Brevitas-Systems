import asyncio
from datetime import datetime, timedelta, timezone

from api.jobs import (
    InMemoryJobStore, JobCrypto, JobRequest, JobService, JobTenant,
    RedisJobDispatcher,
)
from brevitas.security import EnvelopeCipher, LocalTestKMS
from brevitas.security import KMSUnavailable


def job_crypto():
    kms = LocalTestKMS(b"j" * 32, environ={"BREVITAS_ENV": "test"})
    return JobCrypto(EnvelopeCipher(
        kms, key_id="test-job-key", key_version="1",
        wrap_algorithm=kms.algorithm,
    ))


class ToggleKMS(LocalTestKMS):
    def __init__(self):
        super().__init__(b"o" * 32, environ={"BREVITAS_ENV": "test"})
        self.available = True

    def unwrap_data_key(self, wrapped_key, *, encryption_context):
        if not self.available:
            raise KMSUnavailable("test KMS unavailable")
        return super().unwrap_data_key(
            wrapped_key, encryption_context=encryption_context)


def job_crypto_with_kms(kms):
    return JobCrypto(EnvelopeCipher(
        kms, key_id="test-job-key", key_version="1",
        wrap_algorithm=kms.algorithm,
    ))


class Dispatcher:
    def __init__(self):
        self.ids = []

    async def enqueue(self, job_id):
        self.ids.append(job_id)


class FailingDispatcher:
    async def enqueue(self, _job_id):
        raise RuntimeError("redis unavailable")


class RenewTrackingStore(InMemoryJobStore):
    def __init__(self):
        super().__init__()
        self.renewals = 0

    def renew(self, job_id, worker_id, lease_seconds):
        self.renewals += 1
        return super().renew(job_id, worker_id, lease_seconds)


def service():
    store = InMemoryJobStore()
    dispatcher = Dispatcher()
    return JobService(store, crypto=job_crypto(), dispatcher=dispatcher), store, dispatcher


def tenant(org="org_1", customer="customer_1"):
    return JobTenant(org, customer, "key_hash_1")


def request(task="private customer task", **values):
    return JobRequest(task=task, messages=["message"], retention_seconds=3600, **values)


def test_submit_is_idempotent_and_payload_is_encrypted():
    jobs, store, dispatcher = service()

    async def exercise():
        first, created = await jobs.submit(tenant(), request(), "same-request")
        second, duplicate_created = await jobs.submit(tenant(), request(), "same-request")
        return first, created, second, duplicate_created

    first, created, second, duplicate_created = asyncio.run(exercise())
    assert created is True
    assert duplicate_created is False
    assert first["id"] == second["id"]
    assert dispatcher.ids == [first["id"]]
    row = store.rows[first["id"]]
    assert "private customer task" not in row["payload_ciphertext"]


def test_jobs_are_tenant_isolated_and_spoofed_ids_do_not_work():
    jobs, _, _ = service()

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(), "tenant-request")
        same = await jobs.get(tenant(), submitted["id"])
        other_customer = await jobs.get(tenant(customer="customer_2"), submitted["id"])
        other_org = await jobs.cancel(tenant(org="org_2"), submitted["id"])
        return same, other_customer, other_org

    same, other_customer, other_org = asyncio.run(exercise())
    assert same is not None
    assert other_customer is None
    assert other_org is None


def test_worker_success_and_result_round_trip():
    jobs, _, _ = service()

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(), "success")
        assert await jobs.process_one("worker_1", lambda payload, _row: {
            "answer": payload["task"],
            "_job_metadata": {"provider": "openai", "model": "gpt-test"},
        })
        return await jobs.get(tenant(), submitted["id"])

    result = asyncio.run(exercise())
    assert result["status"] == "succeeded"
    assert result["result"] == {"answer": "private customer task"}
    assert result["provider"] == "openai"
    assert result["model"] == "gpt-test"


def test_worker_failure_retries_then_dead_letters_without_error_text():
    jobs, store, _ = service()

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(max_attempts=2), "failure")

        def fail(_payload, _row):
            raise RuntimeError("SENTINEL SECRET ERROR")

        assert await jobs.process_one("worker_1", fail)
        row = store.rows[submitted["id"]]
        row["available_at"] = datetime.now(timezone.utc).isoformat()
        assert row["status"] == "queued"
        assert await jobs.process_one("worker_2", fail)
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert row["status"] == "dead"
    assert row["last_error_code"] == "RuntimeError"
    assert "SENTINEL" not in str(row)


def test_expired_worker_lease_is_reclaimed():
    jobs, store, _ = service()

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(), "reclaim")
        row = store.rows[submitted["id"]]
        row.update(status="running", attempts=1, lease_owner="dead_worker",
                   lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        assert await jobs.process_one("replacement_worker", lambda *_: {"ok": True})
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert row["status"] == "succeeded"
    assert row["attempts"] == 2


def test_expired_final_attempt_becomes_dead_instead_of_stuck():
    jobs, store, _ = service()

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(max_attempts=1), "dead-lease")
        row = store.rows[submitted["id"]]
        row.update(status="running", attempts=1, lease_owner="dead_worker",
                   lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        assert await jobs.process_one("replacement", lambda *_: {"unexpected": True}) is False
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert row["status"] == "dead"
    assert row["last_error_code"] == "lease_expired"


def test_redis_notification_failure_does_not_lose_durable_job():
    store = InMemoryJobStore()
    jobs = JobService(store, crypto=job_crypto(),
                      dispatcher=FailingDispatcher())

    submitted, created = asyncio.run(
        jobs.submit(tenant(), request(), "durable-before-redis")
    )
    assert created is True
    assert submitted["id"] in store.rows
    assert store.rows[submitted["id"]]["status"] == "queued"


def test_long_running_processor_renews_its_database_lease():
    store = RenewTrackingStore()
    jobs = JobService(store, crypto=job_crypto(),
                      dispatcher=Dispatcher())
    jobs.lease_seconds = 0.09

    async def exercise():
        await jobs.submit(tenant(), request(), "lease-heartbeat")

        async def slow_processor(_payload, _row):
            await asyncio.sleep(0.16)
            return {"ok": True}

        assert await jobs.process_one("worker_heartbeat", slow_processor)

    asyncio.run(exercise())
    assert store.renewals >= 2


def test_expired_queued_job_is_dead_and_never_executed():
    jobs, store, _ = service()

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(), "expired-before-claim")
        store.rows[submitted["id"]]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        called = False

        def processor(*_args):
            nonlocal called
            called = True

        assert await jobs.process_one("worker", processor) is False
        return store.rows[submitted["id"]], called

    row, called = asyncio.run(exercise())
    assert called is False
    assert row["status"] == "dead"
    assert row["last_error_code"] == "expired"


def test_permanent_worker_error_is_not_retried():
    jobs, store, _ = service()

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(max_attempts=5), "permanent")

        def invalid(_payload, _row):
            raise ValueError("invalid provider configuration")

        assert await jobs.process_one("worker", invalid)
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert row["status"] == "failed"
    assert row["attempts"] == 1


def test_kms_outage_retries_without_quarantining_authenticated_job():
    kms = ToggleKMS()
    store = InMemoryJobStore()
    jobs = JobService(store, crypto=job_crypto_with_kms(kms), dispatcher=Dispatcher())

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(), "kms-outage")
        kms.available = False
        assert await jobs.process_one("worker", lambda *_: {"ok": True})
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert row["status"] == "queued"
    assert row["last_error_code"] == "KMSUnavailable"
    assert row["lease_owner"] is None


def test_tampered_job_ciphertext_is_quarantined_without_processing():
    jobs, store, _ = service()
    called = False

    async def exercise():
        nonlocal called
        submitted, _ = await jobs.submit(tenant(), request(), "tampered")
        row = store.rows[submitted["id"]]
        suffix = "A" if row["payload_ciphertext"][-1] != "A" else "B"
        row["payload_ciphertext"] = row["payload_ciphertext"][:-1] + suffix

        def processor(*_args):
            nonlocal called
            called = True
            return {"ok": True}

        assert await jobs.process_one("worker", processor)
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert called is False
    assert row["status"] == "dead"
    assert row["last_error_code"] == "ciphertext_unreadable"


def test_worker_consumes_redis_stream_as_wakeup_only():
    class Redis:
        async def xread(self, streams, count, block):
            assert streams == {"brevitas:jobs": "$"}
            assert count == 100 and block == 50
            return [["brevitas:jobs", [["123-0", {"job_id": "opaque-id"}]]]]

    dispatcher = RedisJobDispatcher(Redis())
    assert asyncio.run(dispatcher.wait_for_notification("$", 50)) == "123-0"


def test_redis_wakeup_stream_has_finite_length_and_ttl():
    calls = []

    class Redis:
        async def xadd(self, stream, fields, *, maxlen, approximate):
            calls.append(("xadd", stream, fields, maxlen, approximate))

        async def expire(self, stream, ttl):
            calls.append(("expire", stream, ttl))

    dispatcher = RedisJobDispatcher(Redis())
    asyncio.run(dispatcher.enqueue("opaque-job-id"))
    assert calls == [
        ("xadd", "brevitas:jobs", {"job_id": "opaque-job-id"}, 100_000, True),
        ("expire", "brevitas:jobs", 3_600),
    ]


def test_one_thousand_concurrent_submissions_have_no_loss_or_duplicates():
    jobs, store, dispatcher = service()

    async def exercise():
        return await asyncio.gather(*(
            jobs.submit(tenant(customer=f"customer_{index % 10}"), request(task=f"task {index}"),
                        f"request-{index}")
            for index in range(1000)
        ))

    results = asyncio.run(exercise())
    ids = [row[0]["id"] for row in results]
    assert len(ids) == 1000
    assert len(set(ids)) == 1000
    assert len(store.rows) == 1000
    assert len(dispatcher.ids) == 1000
