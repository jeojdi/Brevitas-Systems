"""Durable provider ambiguity tests; these never make live provider calls."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from api.jobs import (
    InMemoryJobStore, JobCrypto, JobLeaseLost, JobRequest, JobService,
    JobTenant, PermanentJobError, SQLiteJobStore, SupabaseJobStore,
)
from api.store import UsageStore
from brevitas.observability import job_context
from brevitas.provider_reliability import (
    ProviderCircuitOpen, ProviderReliabilityConfig,
    ProviderSyncHTTPClientPool,
)
from brevitas.security import EnvelopeCipher, LocalTestKMS


class Dispatcher:
    async def enqueue(self, _job_id):
        return None


def job_crypto():
    kms = LocalTestKMS(b"i" * 32, environ={"BREVITAS_ENV": "test"})
    return JobCrypto(EnvelopeCipher(
        kms, key_id="test-job-key", key_version="1",
        wrap_algorithm=kms.algorithm,
    ))


def test_supported_adapters_do_not_invent_provider_idempotency_headers(monkeypatch):
    import api.server as server

    captured = []
    payloads = iter([
        {"response": "ollama", "done_reason": "stop"},
        {"content": [{"text": "anthropic"}], "stop_reason": "end_turn"},
        {"choices": [{
            "message": {"content": "openai-compatible"}, "finish_reason": "stop",
        }]},
    ])

    class Response:
        status_code = 200
        headers = {}

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

        def close(self):
            return None

    class Client:
        def __init__(self, **_kwargs):
            pass

        def request(self, method, url, **kwargs):
            captured.append(dict(kwargs.get("headers") or {}))
            return Response(next(payloads))

        def close(self):
            return None

    monkeypatch.setattr(httpx, "Client", Client)
    pool = ProviderSyncHTTPClientPool(
        ProviderReliabilityConfig(max_retries=0),
    )
    monkeypatch.setattr(server, "provider_sync_http", pool)

    job_id = "95a84c2b-a72b-4df7-a87f-f08f17a988ac"
    with job_context(job_id):
        assert server._make_ollama_backend("model")("prompt", "") == "ollama"
        assert server._make_anthropic_backend(
            "provider-secret", "model",
        )("prompt", "") == "anthropic"
        assert server._make_openai_compat_backend(
            "openai", "provider-secret", "model", "https://provider.invalid/v1",
        )("prompt", "") == "openai-compatible"

    assert len(captured) == 3
    for headers in captured:
        lowered = {name.lower(): value for name, value in headers.items()}
        assert "idempotency-key" not in lowered
        assert lowered["x-brevitas-request-id"] == f"job:{job_id}"
        assert lowered["x-request-id"] == f"job:{job_id}"
        serialized = " ".join(str(value) for value in headers.values())
        assert "customer_42" not in serialized
        assert "client-submission-secret" not in serialized


@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout("provider acceptance unknown"),
    httpx.WriteError("provider acceptance unknown"),
])
def test_ambiguous_transport_failure_is_terminal_for_durable_job(
        monkeypatch, failure):
    import api.server as server

    class Pool:
        def __init__(self):
            self.calls = 0

        def request(self, *_args, **_kwargs):
            self.calls += 1
            raise failure

    pool = Pool()
    monkeypatch.setattr(server, "provider_sync_http", pool)
    store = InMemoryJobStore()
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())

    async def exercise():
        submitted, _ = await jobs.submit(
            JobTenant("org_1", "customer_1", "key_hash_1"),
            JobRequest(task="private task", max_attempts=3),
            "stable-client-submission-key",
        )

        def processor(_payload, _row):
            response = server._make_openai_compat_backend(
                "openai", "provider-secret", "model",
                "https://provider.invalid/v1",
            )("prompt", "")
            return {"model_response": response}

        assert await jobs.process_one("worker_1", processor)
        assert await jobs.process_one("worker_2", processor) is False
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert pool.calls == 1
    assert row["attempts"] == 1
    assert row["status"] == "failed"
    assert row["last_error_code"] == "ProviderOutcomeAmbiguous"
    assert "provider acceptance unknown" not in str(row)


def test_provider_5xx_and_invalid_success_are_ambiguous(monkeypatch):
    import api.server as server

    responses = iter([
        httpx.Response(
            503,
            request=httpx.Request("POST", "https://provider.invalid/v1/chat/completions"),
        ),
        httpx.Response(
            200,
            json={},
            request=httpx.Request("POST", "https://provider.invalid/v1/chat/completions"),
        ),
    ])

    class Pool:
        def request(self, *_args, **_kwargs):
            return next(responses)

    monkeypatch.setattr(server, "provider_sync_http", Pool())
    backend = server._make_openai_compat_backend(
        "openai", "provider-secret", "model", "https://provider.invalid/v1",
    )
    for _ in range(2):
        with pytest.raises(server.ProviderOutcomeAmbiguous) as caught:
            backend("prompt", "")
        assert caught.value.status_code == 502
        assert caught.value.detail == "Model provider request failed"
        assert caught.value.job_retryable is False


def test_definite_connection_failure_remains_safe_to_retry(monkeypatch):
    import api.server as server

    class Pool:
        def request(self, *_args, **_kwargs):
            raise httpx.ConnectError("connection was never established")

    monkeypatch.setattr(server, "provider_sync_http", Pool())
    backend = server._make_openai_compat_backend(
        "openai", "provider-secret", "model", "https://provider.invalid/v1",
    )
    with pytest.raises(server.ProviderRequestNotAccepted) as caught:
        backend("prompt", "")
    assert caught.value.status_code == 502
    assert caught.value.job_retryable is True
    assert caught.value.provider_outbound_not_accepted is True


def _expired() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()


def test_crash_before_outbound_marker_remains_retryable_and_calls_provider_once():
    store = InMemoryJobStore()
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())
    calls = 0

    async def exercise():
        nonlocal calls
        submitted, _ = await jobs.submit(
            JobTenant("org_1", "customer_1", "key_hash_1"),
            JobRequest(task="private task", max_attempts=3),
            "crash-before-provider-fence",
        )
        crashed = store.rows[submitted["id"]]
        crashed.update(
            status="running", attempts=1, lease_owner="crashed_worker",
            lease_expires_at=_expired(),
        )

        async def processor(_payload, row):
            nonlocal calls
            await jobs.mark_provider_outbound_started(row)
            calls += 1
            return {"model_response": "accepted"}

        assert await jobs.process_one("replacement_worker", processor)
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert calls == 1
    assert row["attempts"] == 2
    assert row["status"] == "succeeded"
    assert row["provider_outbound_attempt"] == 2


def test_crash_after_marker_before_provider_call_terminalizes_without_replay():
    store = InMemoryJobStore()
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())
    calls = 0

    async def exercise():
        nonlocal calls
        submitted, _ = await jobs.submit(
            JobTenant("org_1", "customer_1", "key_hash_1"),
            JobRequest(task="private task", max_attempts=3),
            "crash-after-provider-fence",
        )
        claimed = store.claim("crashed_worker", 180)
        assert claimed and store.update(
            submitted["id"], "crashed_worker", {"status": "running"},
        )
        marked = store.mark_provider_outbound_started(
            submitted["id"], "crashed_worker",
        )
        assert marked and marked["provider_outbound_attempt"] == 1
        store.rows[submitted["id"]]["lease_expires_at"] = _expired()

        def processor(*_args):
            nonlocal calls
            calls += 1
            return {"must_not_run": True}

        assert await jobs.process_one("replacement_worker", processor) is False
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert calls == 0
    assert row["status"] == "dead"
    assert row["attempts"] == 1
    assert row["last_error_code"] == "provider_outcome_ambiguous"
    assert row["provider_outbound_started_at"] is not None
    assert store.update(row["id"], "crashed_worker", {"status": "succeeded"}) is None


def test_accepted_call_followed_by_lease_loss_is_never_replayed_or_committed():
    store = InMemoryJobStore()
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())
    provider_calls = 0

    async def exercise():
        nonlocal provider_calls
        submitted, _ = await jobs.submit(
            JobTenant("org_1", "customer_1", "key_hash_1"),
            JobRequest(task="private task", max_attempts=3),
            "accepted-then-lease-lost",
        )

        async def accepted(_payload, row):
            nonlocal provider_calls
            await jobs.mark_provider_outbound_started(row)
            provider_calls += 1
            store.rows[row["id"]]["lease_expires_at"] = _expired()
            return {"model_response": "remote result that cannot be committed"}

        assert await jobs.process_one("original_worker", accepted)
        before_reclaim = dict(store.rows[submitted["id"]])
        assert await jobs.process_one(
            "replacement_worker",
            lambda *_: {"must_not_call_provider": True},
        ) is False
        return before_reclaim, store.rows[submitted["id"]]

    before_reclaim, row = asyncio.run(exercise())
    assert provider_calls == 1
    assert before_reclaim["status"] == "running"
    assert before_reclaim["result_ciphertext"] is None
    assert row["status"] == "dead"
    assert row["last_error_code"] == "provider_outcome_ambiguous"
    assert store.update(row["id"], "original_worker", {"status": "succeeded"}) is None


def test_marker_response_loss_after_commit_cannot_enable_provider_replay():
    class SupabaseCommitThenTimeoutTransport:
        def __init__(self):
            self.database = InMemoryJobStore()
            self.response_losses = 0

        @staticmethod
        def _eq(value):
            return str(value).removeprefix("eq.")

        def _request(self, method, path, *, params=None, data=None, **_kwargs):
            params = params or {}
            if method == "GET" and path == "ai_jobs":
                if "idempotency_key" in params:
                    matches = [
                        dict(row) for row in self.database.rows.values()
                        if row["organization_id"] == self._eq(params["organization_id"])
                        and row["customer_id"] == self._eq(params["customer_id"])
                        and row["idempotency_key"] == self._eq(params["idempotency_key"])
                    ]
                    return matches[:1]
                row = self.database.get(
                    self._eq(params["id"]),
                    self._eq(params["organization_id"]),
                    self._eq(params["customer_id"]),
                )
                return [row] if row else []
            if method == "POST" and path == "ai_jobs":
                created, _ = self.database.create(data)
                return [created]
            if method == "POST" and path == "rpc/claim_ai_job":
                claimed = self.database.claim(
                    data["p_worker_id"], data["p_lease_seconds"],
                )
                return [claimed] if claimed else []
            if (method == "POST"
                    and path == "rpc/mark_ai_job_provider_outbound_started"):
                marked = self.database.mark_provider_outbound_started(
                    data["p_job_id"], data["p_worker_id"],
                )
                assert marked is not None
                self.response_losses += 1
                raise TimeoutError("Supabase marker response lost after commit")
            if method == "PATCH" and path == "ai_jobs":
                updated = self.database.update(
                    self._eq(params["id"]), self._eq(params["lease_owner"]), data,
                )
                return [updated] if updated else []
            raise AssertionError(f"unexpected Supabase request: {method} {path}")

    transport = SupabaseCommitThenTimeoutTransport()
    store = SupabaseJobStore(transport)
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())
    provider_calls = 0

    async def exercise():
        nonlocal provider_calls
        submitted, _ = await jobs.submit(
            JobTenant("org_1", "customer_1", "key_hash_1"),
            JobRequest(task="private task", max_attempts=3),
            "marker-response-loss",
        )

        async def processor(_payload, row):
            nonlocal provider_calls
            await jobs.mark_provider_outbound_started(row)
            provider_calls += 1
            return {"must_not_happen": True}

        assert await jobs.process_one("original_worker", processor)
        after_response_loss = dict(transport.database.rows[submitted["id"]])
        assert await jobs.process_one("replacement_worker", processor) is False
        return after_response_loss, transport.database.rows[submitted["id"]]

    after_response_loss, row = asyncio.run(exercise())
    assert transport.response_losses == 1
    assert provider_calls == 0
    # The worker's local row never received the RPC representation, but the
    # authoritative marker survived its attempted queue update.
    assert after_response_loss["status"] == "queued"
    assert after_response_loss["provider_outbound_started_at"] is not None
    assert after_response_loss["provider_outbound_attempt"] == 1
    assert row["status"] == "dead"
    assert row["last_error_code"] == "provider_outcome_ambiguous"


@pytest.mark.parametrize("failure_kind", ["connect", "circuit", "rate_limit"])
def test_proven_preacceptance_failure_clears_marker_before_requeue(failure_kind):
    import api.server as server

    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    if failure_kind == "connect":
        source = httpx.ConnectError("connection was never established", request=request)
    elif failure_kind == "circuit":
        source = ProviderCircuitOpen(2.0)
    else:
        response = httpx.Response(429, request=request)
        source = httpx.HTTPStatusError(
            "rate limited", request=request, response=response,
        )
    classified = server._provider_unavailable(source, "Model provider")
    assert isinstance(classified, server.ProviderRequestNotAccepted)

    store = InMemoryJobStore()
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())

    async def exercise():
        submitted, _ = await jobs.submit(
            JobTenant("org_1", "customer_1", "key_hash_1"),
            JobRequest(task="private task", max_attempts=3),
            f"safe-preacceptance-{failure_kind}",
        )

        async def processor(_payload, row):
            await jobs.mark_provider_outbound_started(row)
            raise classified

        assert await jobs.process_one("worker_1", processor)
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert row["status"] == "queued"
    assert row["last_error_code"] == "ProviderRequestNotAccepted"
    assert row["provider_outbound_started_at"] is None
    assert row["provider_outbound_attempt"] is None


def test_ambiguous_failure_preserves_marker_and_never_requeues():
    import api.server as server

    store = InMemoryJobStore()
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())

    async def exercise():
        submitted, _ = await jobs.submit(
            JobTenant("org_1", "customer_1", "key_hash_1"),
            JobRequest(task="private task", max_attempts=3),
            "ambiguous-provider-outcome",
        )

        async def processor(_payload, row):
            await jobs.mark_provider_outbound_started(row)
            raise server.ProviderOutcomeAmbiguous("Model provider")

        assert await jobs.process_one("worker_1", processor)
        assert await jobs.process_one("worker_2", processor) is False
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert row["status"] == "dead"
    assert row["last_error_code"] == "provider_outcome_ambiguous"
    assert row["provider_outbound_started_at"] is not None
    assert row["provider_outbound_attempt"] == 1


def test_compression_failure_retries_without_provider_marker():
    store = InMemoryJobStore()
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())

    async def exercise():
        submitted, _ = await jobs.submit(
            JobTenant("org_1", "customer_1", "key_hash_1"),
            JobRequest(operation="compress", task="private task", max_attempts=3),
            "compression-no-provider-fence",
        )

        def processor(*_args):
            raise RuntimeError("compressor dependency unavailable")

        assert await jobs.process_one("worker_1", processor)
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert row["status"] == "queued"
    assert row["provider_outbound_started_at"] is None
    assert row["provider_outbound_attempt"] is None


def test_provider_fence_is_absent_from_public_job_status():
    store = InMemoryJobStore()
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())
    submitted, _ = asyncio.run(jobs.submit(
        JobTenant("org_1", "customer_1", "key_hash_1"),
        JobRequest(task="private task"),
        "content-free-public-status",
    ))
    row = store.rows[submitted["id"]]
    row.update(
        status="succeeded",
        provider_outbound_started_at="2030-01-01T00:00:00+00:00",
        provider_outbound_attempt=1,
    )
    public = jobs.public(row)
    assert "provider_outbound_started_at" not in public
    assert "provider_outbound_attempt" not in public
    assert "payload_ciphertext" not in public
    assert "result_ciphertext" not in public
    assert "2030-01-01" not in str(public)


def test_sqlite_marker_is_ownership_fenced_and_reclaim_terminalizes(tmp_path):
    sqlite = UsageStore(str(tmp_path / "provider-fence.db"))
    store = SQLiteJobStore(sqlite)
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())

    async def submit():
        return await jobs.submit(
            JobTenant("org_1", "customer_1", "key_hash_1"),
            JobRequest(task="private task", max_attempts=3),
            "sqlite-provider-fence",
        )

    submitted, _ = asyncio.run(submit())
    claimed = store.claim("sqlite_worker", 180)
    assert claimed and store.update(
        submitted["id"], "sqlite_worker", {"status": "running"},
    )
    assert store.mark_provider_outbound_started(
        submitted["id"], "wrong_worker",
    ) is None
    marked = store.mark_provider_outbound_started(
        submitted["id"], "sqlite_worker",
    )
    assert marked and marked["provider_outbound_attempt"] == 1
    assert store.mark_provider_outbound_started(
        submitted["id"], "sqlite_worker",
    ) is None
    with sqlite._conn() as database:
        database.execute(
            "UPDATE ai_jobs SET lease_expires_at=? WHERE id=?",
            (_expired(), submitted["id"]),
        )
    assert store.claim("replacement_worker", 180) is None
    row = store.get(submitted["id"], "org_1", "customer_1")
    assert row and row["status"] == "dead"
    assert row["last_error_code"] == "provider_outcome_ambiguous"


def test_supabase_marker_uses_dedicated_service_rpc_and_validates_response():
    calls = []

    class CloudStore:
        def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return [{
                "id": kwargs["data"]["p_job_id"],
                "provider_outbound_started_at": "2030-01-01T00:00:00+00:00",
                "provider_outbound_attempt": 2,
            }]

    adapter = SupabaseJobStore(CloudStore())
    marked = adapter.mark_provider_outbound_started("job-id", "worker_2")
    assert marked and marked["provider_outbound_attempt"] == 2
    assert calls == [(
        "POST", "rpc/mark_ai_job_provider_outbound_started",
        {"data": {"p_job_id": "job-id", "p_worker_id": "worker_2"}},
    )]


def test_service_fails_closed_on_malformed_marker_response():
    class MalformedMarkerStore(InMemoryJobStore):
        def mark_provider_outbound_started(self, job_id, worker_id):
            marked = super().mark_provider_outbound_started(job_id, worker_id)
            assert marked is not None
            return {"id": job_id}

    store = MalformedMarkerStore()
    jobs = JobService(store, crypto=job_crypto(), dispatcher=Dispatcher())

    async def exercise():
        submitted, _ = await jobs.submit(
            JobTenant("org_1", "customer_1", "key_hash_1"),
            JobRequest(task="private task"),
            "malformed-provider-fence-response",
        )
        row = store.claim("worker_1", 180)
        assert row and store.update(row["id"], "worker_1", {"status": "running"})
        with pytest.raises(JobLeaseLost):
            await jobs.mark_provider_outbound_started(row)
        return store.rows[submitted["id"]]

    row = asyncio.run(exercise())
    assert row["provider_outbound_started_at"] is not None
    assert row["provider_outbound_attempt"] == 1


def test_worker_rejects_missing_provider_before_outbound_marker(monkeypatch):
    import api.worker as worker

    class Jobs:
        def __init__(self):
            self.markers = 0

        async def mark_provider_outbound_started(self, _row):
            self.markers += 1

    jobs = Jobs()
    monkeypatch.setattr(worker, "_job_service", jobs)
    monkeypatch.setattr(worker, "_authoritative_service_key_context", lambda _key: {
        "owner_id": "owner_1", "key_type": "organization_service",
    })
    monkeypatch.setattr(
        worker, "_resolve_configured_model_backend", lambda _key: (None, None),
    )
    with pytest.raises(PermanentJobError, match="provider_not_configured"):
        asyncio.run(worker._process_job(
            {"operation": "chat", "task": "private task"},
            {
                "id": "95a84c2b-a72b-4df7-a87f-f08f17a988ac",
                "organization_id": "org_1", "customer_id": "customer_1",
                "key_hash": "key_hash_1",
            },
        ))
    assert jobs.markers == 0


def test_worker_persists_marker_immediately_before_provider_call(monkeypatch):
    import api.worker as worker

    order = []

    class Jobs:
        async def mark_provider_outbound_started(self, _row):
            order.append("marker")

    class Lease:
        allowed = True

        async def release(self):
            order.append("release")

    class Limiter:
        async def acquire(self, *_args, **_kwargs):
            return Lease()

    monkeypatch.setattr(worker, "_job_service", Jobs())
    monkeypatch.setattr(worker, "_distributed_limiter", Limiter())
    monkeypatch.setattr(worker, "_authoritative_service_key_context", lambda _key: {
        "owner_id": "owner_1", "key_type": "organization_service",
    })
    backend = object()
    config = {"provider": "openai", "model": "model"}
    monkeypatch.setattr(
        worker, "_resolve_configured_model_backend",
        lambda _key: (config, backend),
    )

    def run_model(*_args, **kwargs):
        assert kwargs["resolved_config"] is config
        assert kwargs["resolved_backend"] is backend
        order.append("provider")
        return {"provider": "openai", "model": "model", "model_response": "ok"}

    monkeypatch.setattr(worker, "_run_configured_model", run_model)
    monkeypatch.setattr(worker, "_safe_record_usage", lambda **_kwargs: True)
    result = asyncio.run(worker._process_job(
        {"operation": "chat", "task": "private task"},
        {
            "id": "95a84c2b-a72b-4df7-a87f-f08f17a988ac",
            "organization_id": "org_1", "customer_id": "customer_1",
            "key_hash": "key_hash_1",
        },
    ))
    assert result["model_response"] == "ok"
    assert order == ["marker", "provider", "release"]
