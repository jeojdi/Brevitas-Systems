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


class LeaseFailureStore(InMemoryJobStore):
    def __init__(self, *, raises=False):
        super().__init__()
        self.raises = raises
        self.renewals = 0
        self.worker_updates = []

    def renew(self, job_id, worker_id, lease_seconds):
        self.renewals += 1
        if self.raises:
            raise RuntimeError("renew dependency unavailable")
        return False

    def update(self, job_id, worker_id, values):
        updated = super().update(job_id, worker_id, values)
        if updated:
            self.worker_updates.append(dict(values))
        return updated


class FinalUpdateOwnershipLossStore(InMemoryJobStore):
    def update(self, job_id, worker_id, values):
        if values.get("status") in {"succeeded", "queued", "failed", "dead"}:
            with self._lock:
                self.rows[job_id]["lease_owner"] = "replacement_worker"
        return super().update(job_id, worker_id, values)


def service():
    store = InMemoryJobStore()
    dispatcher = Dispatcher()
    return JobService(store, crypto=job_crypto(), dispatcher=dispatcher), store, dispatcher


def _iso():
    return datetime.now(timezone.utc).isoformat()


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


def test_slow_async_processor_is_cancelled_when_lease_renewal_is_rejected():
    store = LeaseFailureStore()
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())
    jobs.lease_seconds = 0.09

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(), "lease-rejected")
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow_processor(_payload, _row):
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
            return {"must_not_commit": True}

        assert await jobs.process_one("worker_lost", slow_processor)
        assert started.is_set()
        assert cancelled.is_set()
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert store.renewals == 1
    assert [update["status"] for update in store.worker_updates] == ["running"]
    assert row["status"] == "running"
    assert row["result_ciphertext"] is None
    assert row["lease_owner"] == "worker_lost"


def test_renewal_exception_cancels_processor_without_error_or_final_update():
    store = LeaseFailureStore(raises=True)
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())
    jobs.lease_seconds = 0.09

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(), "renew-exception")
        cancelled = asyncio.Event()

        async def slow_processor(_payload, _row):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
            return {"must_not_commit": True}

        assert await jobs.process_one("worker_exception", slow_processor)
        assert cancelled.is_set()
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert store.renewals == 1
    assert [update["status"] for update in store.worker_updates] == ["running"]
    assert row["status"] == "running"
    assert row["last_error_code"] == ""
    assert row["lease_owner"] == "worker_exception"


def test_expired_lease_cannot_be_resurrected_or_commit_a_result():
    store = InMemoryJobStore()
    row = {
        "id": "expired-job", "status": "running", "lease_owner": "stale_worker",
        "lease_expires_at": (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(),
    }
    store.rows[row["id"]] = row

    assert store.renew(row["id"], "stale_worker", 180) is False
    assert store.update(
        row["id"], "stale_worker", {"status": "succeeded"},
    ) is None
    assert row["status"] == "running"


def test_success_commit_is_discarded_if_final_update_loses_ownership():
    store = FinalUpdateOwnershipLossStore()
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(), "stale-success")
        assert await jobs.process_one("stale_worker", lambda *_: {"ok": True})
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert row["status"] == "running"
    assert row["result_ciphertext"] is None
    assert row["lease_owner"] == "replacement_worker"


def test_error_commit_is_discarded_if_retry_update_loses_ownership():
    store = FinalUpdateOwnershipLossStore()
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(), "stale-error")

        def fail(*_args):
            raise RuntimeError("provider failed")

        assert await jobs.process_one("stale_worker", fail)
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert row["status"] == "running"
    assert row["last_error_code"] == ""
    assert row["lease_owner"] == "replacement_worker"


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


class CancelWindowTransport:
    """Supabase transport whose row terminalizes between cancel's GET and PATCH.

    Models the real interleaving: cancel reads status='leased', and before its
    write lands the worker commits status='succeeded' with a result.
    """

    def __init__(self, status="leased", raced_into="succeeded"):
        self.row = {
            "id": "job_1", "organization_id": "org_1", "customer_id": "customer_1",
            "status": status, "result_ciphertext": None, "cancel_requested": False,
            "lease_owner": "worker_1", "lease_expires_at": "2099-01-01T00:00:00+00:00",
            "completed_at": None,
        }
        self.raced_into = raced_into
        self.patches = []
        self.reads = 0

    @staticmethod
    def _eq(value):
        return str(value).removeprefix("eq.")

    def _matches(self, params):
        for column, predicate in params.items():
            if column in ("select", "limit"):
                continue
            if predicate.startswith("in.("):
                if self.row[column] not in predicate[4:-1].split(","):
                    return False
            elif predicate.startswith("not.in.("):
                if self.row[column] in predicate[8:-1].split(","):
                    return False
            elif self.row[column] != self._eq(predicate):
                return False
        return True

    def _request(self, method, path, *, params=None, data=None, **_kwargs):
        params = params or {}
        if method == "GET" and path == "ai_jobs":
            self.reads += 1
            row = dict(self.row) if self._matches(params) else None
            if self.reads == 1 and self.raced_into:
                # The worker wins the race immediately after cancel's read.
                self.row.update(status=self.raced_into, result_ciphertext="opaque",
                                lease_owner=None, lease_expires_at=None,
                                completed_at="2026-07-29T00:00:00+00:00")
            return [row] if row else []
        if method == "PATCH" and path == "ai_jobs":
            self.patches.append((dict(params), dict(data)))
            if not self._matches(params):
                return []
            self.row.update(data)
            return [dict(self.row)]
        raise AssertionError(f"unexpected Supabase request: {method} {path}")


def test_cancel_cannot_clobber_a_result_committed_in_the_read_write_window():
    from api.jobs import SupabaseJobStore

    transport = CancelWindowTransport()
    row = SupabaseJobStore(transport).cancel("job_1", "org_1", "customer_1")

    # The PATCH must carry the states the read decided from, so the worker's
    # committed result survives and the caller is told the truth.
    assert transport.patches[0][0]["status"] == "in.(queued,leased)"
    assert row["status"] == "succeeded"
    assert row["result_ciphertext"] == "opaque"
    assert transport.row["completed_at"] == "2026-07-29T00:00:00+00:00"


def test_cancel_of_a_queued_job_terminalizes_and_releases_the_lease():
    from api.jobs import SupabaseJobStore

    transport = CancelWindowTransport(status="queued", raced_into="")
    row = SupabaseJobStore(transport).cancel("job_1", "org_1", "customer_1")

    assert row["status"] == "cancelled"
    assert row["cancel_requested"] is True
    assert (row["lease_owner"], row["lease_expires_at"]) == (None, None)


def test_cancel_of_a_running_job_only_sets_the_cooperative_flag():
    from api.jobs import SupabaseJobStore

    transport = CancelWindowTransport(status="running", raced_into="")
    row = SupabaseJobStore(transport).cancel("job_1", "org_1", "customer_1")

    params, data = transport.patches[0]
    # A live lease is never flipped terminal; process_one honours the flag.
    assert params["status"] == "not.in.(cancelled,dead,failed,succeeded)"
    assert "status" not in data
    assert row["status"] == "running"
    assert row["cancel_requested"] is True


def test_in_memory_cancel_releases_the_lease_like_the_durable_backends():
    jobs, store, _ = service()

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(), "cancel-lease")
        store.rows[submitted["id"]].update(
            status="leased", lease_owner="worker_1",
            lease_expires_at="2099-01-01T00:00:00+00:00")
        return await jobs.cancel(tenant(), submitted["id"])

    cancelled = asyncio.run(exercise())
    assert cancelled["status"] == "cancelled"
    row = store.rows[cancelled["id"]]
    assert (row["lease_owner"], row["lease_expires_at"]) == (None, None)


def test_idempotent_retry_returns_the_existing_row_instead_of_a_quota_error():
    jobs, store, _ = service()
    jobs.max_active_per_org = 1

    async def exercise():
        first, created = await jobs.submit(tenant(), request(), "at-the-ceiling")
        # The organization is now at its quota; a retry of the same key must
        # still resolve to the row it already owns, never a 429.
        second, duplicate = await jobs.submit(tenant(), request(), "at-the-ceiling")
        return first, created, second, duplicate

    first, created, second, duplicate = asyncio.run(exercise())
    assert (created, duplicate) == (True, False)
    assert first["id"] == second["id"]
    assert len(store.rows) == 1


def test_queued_job_quota_is_per_organization_and_refuses_new_keys():
    from brevitas.resource_bounds import ResourceLimitExceeded

    jobs, _, _ = service()
    jobs.max_active_per_org = 2

    async def exercise():
        for index in range(2):
            await jobs.submit(tenant(), request(), f"quota-{index}")
        try:
            await jobs.submit(tenant(), request(), "quota-over")
        except ResourceLimitExceeded:
            refused = True
        else:
            refused = False
        # A different organization is unaffected by this one's backlog.
        other, created = await jobs.submit(
            tenant(org="org_2"), request(), "quota-other-org")
        return refused, other, created

    refused, other, created = asyncio.run(exercise())
    assert refused is True
    assert created is True
    assert other["id"]


def test_quota_is_never_charged_for_terminal_rows():
    jobs, store, _ = service()
    jobs.max_active_per_org = 1

    async def exercise():
        first, _ = await jobs.submit(tenant(), request(), "quota-terminal-1")
        store.rows[first["id"]]["status"] = "succeeded"
        return await jobs.submit(tenant(), request(), "quota-terminal-2")

    second, created = asyncio.run(exercise())
    assert created is True
    assert second["id"]


def test_public_is_a_pure_projection_and_never_decrypts():
    jobs, store, _ = service()

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(), "pure-projection")
        return dict(store.rows[submitted["id"]])

    row = asyncio.run(exercise())
    projection = jobs.public(row)
    assert "result" not in projection
    assert "payload_ciphertext" not in projection
    assert "result_ciphertext" not in projection
    # A crypto-less service can still project a row; only the result path needs
    # the KMS, which is why it lives behind an offloaded helper.
    assert JobService(store, dispatcher=Dispatcher()).public(row)["id"] == row["id"]


def test_result_decryption_and_repair_never_run_on_the_event_loop():
    import threading

    crypto_threads = []
    repair_threads = []

    class ThreadRecordingStore(InMemoryJobStore):
        def migrate_ciphertext(self, row, field, old, new):
            repair_threads.append(threading.get_ident())
            return super().migrate_ciphertext(row, field, old, new)

    class ThreadRecordingCrypto(JobCrypto):
        def decrypt(self, value, *, row, field):
            crypto_threads.append(threading.get_ident())
            decoded, _ = super().decrypt(value, row=row, field=field)
            # Force the rotation branch so the repair write is exercised too.
            return decoded, self.encrypt(decoded, row=row, field=field)

    inner = job_crypto()
    crypto = ThreadRecordingCrypto(inner.cipher)
    store = ThreadRecordingStore()
    jobs = JobService(store, crypto=crypto, dispatcher=Dispatcher())

    async def exercise():
        submitted, _ = await jobs.submit(tenant(), request(), "offloaded-result")
        assert await jobs.process_one("worker_1", lambda payload, row: {"answer": 42})
        fetched = await jobs.get(tenant(), submitted["id"])
        return fetched, threading.get_ident()

    fetched, loop_thread = asyncio.run(exercise())
    assert fetched["result"] == {"answer": 42}
    assert crypto_threads and loop_thread not in crypto_threads
    assert repair_threads and loop_thread not in repair_threads


def test_sqlite_cancel_is_a_compare_and_set_across_backends(tmp_path):
    from api.jobs import SQLiteJobStore
    from api.store import UsageStore

    store = SQLiteJobStore(UsageStore(str(tmp_path / "jobs.db")))
    base = {
        "id": "job_1", "organization_id": "org_1", "customer_id": "customer_1",
        "key_hash": "kh", "idempotency_key": "one", "operation": "chat",
        "provider": "", "model": "", "payload_ciphertext": "opaque",
        "result_ciphertext": None, "status": "leased", "attempts": 1,
        "max_attempts": 3, "available_at": _iso(), "lease_owner": "worker_1",
        "lease_expires_at": "2099-01-01T00:00:00+00:00", "cancel_requested": False,
        "provider_outbound_started_at": None, "provider_outbound_attempt": None,
        "last_error_code": "", "created_at": _iso(), "updated_at": _iso(),
        "completed_at": None, "expires_at": "2099-01-01T00:00:00+00:00",
    }
    store.create(base)
    read_rows = []
    original_dict = store._dict

    def racing_dict(row):
        value = original_dict(row)
        if value and value.get("status") == "leased" and not read_rows:
            read_rows.append(value)
            # The worker commits its result on its own connection while cancel
            # is still deciding what to write. _conn() opens a fresh connection
            # per call, so this really lands between the SELECT and the UPDATE.
            assert store.update(value["id"], "worker_1", {
                "status": "succeeded", "result_ciphertext": "answer"}) is not None
        return value

    store._dict = racing_dict
    row = store.cancel("job_1", "org_1", "customer_1")
    store._dict = original_dict
    assert read_rows and read_rows[0]["status"] == "leased"
    assert row["status"] == "succeeded"
    assert row["result_ciphertext"] == "answer"

    store.create({**base, "id": "job_2", "idempotency_key": "two",
                  "status": "running"})
    running = store.cancel("job_2", "org_1", "customer_1")
    assert running["status"] == "running"
    assert running["cancel_requested"] is True
    assert running["completed_at"] is None
