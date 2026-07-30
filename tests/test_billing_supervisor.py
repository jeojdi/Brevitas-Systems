"""FIX-8 / WR-1: the billing loop supervisor must actually supervise.

Before this fix, `await inner` in the supervisor was guarded only by
`except asyncio.CancelledError`, so any exception escaping the billing loop
killed the supervisor silently and the advertised restart/backoff/escalation
path never ran. That path was in fact dead under *every* execution, because the
only clean exit from `run_billing_recovery_loop` is `while not stop.is_set()`,
so a normal return always hit the `break` before `inner.exception()` was read.

These tests drive the real supervisor with injected loop factories (no Stripe,
no Supabase, no network) and pin the documented drain contract from
docs/STRIPE_BILLING.md ("W1 worker integration contract"): set `stop` first,
await the task, never cancel it out from under an in-flight bounded call.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

import api.worker as worker

ROOT = Path(__file__).resolve().parents[1]


class LogRecorder:
    """Collects StructuredLogger events without emitting them."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def _record(self, level: str, event: str, **fields: object) -> None:
        self.events.append((level, event, dict(fields)))

    def info(self, event: str, **fields: object) -> None:
        self._record("info", event, **fields)

    def warning(self, event: str, **fields: object) -> None:
        self._record("warning", event, **fields)

    def error(self, event: str, **fields: object) -> None:
        self._record("error", event, **fields)

    def named(self, event: str) -> list[dict]:
        return [fields for _level, name, fields in self.events if name == event]


class CancelWatch:
    """Records every ``.cancel()`` issued on a task created by the supervisor."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, watch_name: str) -> None:
        self.cancels: list[str] = []
        self.tasks: list[asyncio.Task] = []
        real_create_task = asyncio.create_task

        def spy(coro, *, name=None, **kwargs):
            task = real_create_task(coro, name=name, **kwargs)
            if name == watch_name:
                self.tasks.append(task)
                original_cancel = task.cancel

                def cancel(msg=None):
                    self.cancels.append(name)
                    return original_cancel() if msg is None else original_cancel(msg)

                task.cancel = cancel
            return task

        monkeypatch.setattr(asyncio, "create_task", spy)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> LogRecorder:
    log = LogRecorder()
    monkeypatch.setattr(worker, "logger", log)
    monkeypatch.setattr(worker, "_BILLING_LOOP_RUNNING", False)
    monkeypatch.setattr(worker, "_BILLING_HEALTH", {
        "running": False,
        "initial_validation_succeeded": False,
        "catalog_valid": False,
        "last_success_monotonic": 0.0,
        "consecutive_errors": 0,
        "last_error_monotonic": 0.0,
    })
    return log


def test_restart_backoff_is_linear_and_bounded():
    # Bounded by BREVITAS_BILLING_LOOP_RESTART_BACKOFF_SECONDS * restarts, hard
    # capped at 60s so escalation can never be deferred indefinitely.
    assert worker._billing_restart_delay(2.0, 1) == 2.0
    assert worker._billing_restart_delay(2.0, 3) == 6.0
    assert worker._billing_restart_delay(2.0, 1000) == 60.0
    assert worker._billing_restart_delay(0.0, 5) == 0.0
    assert worker._billing_restart_delay(-5.0, 5) == 0.0
    # A zero/negative restart count still yields one backoff unit, never a
    # negative timeout.
    assert worker._billing_restart_delay(1.5, 0) == 1.5


def test_supervisor_restarts_a_raising_loop_then_runs_the_replacement(
    recorder: LogRecorder, monkeypatch: pytest.MonkeyPatch,
):
    async def scenario():
        watch = CancelWatch(monkeypatch, "billing-recovery")
        stop = asyncio.Event()
        starts: list[int] = []
        healthy = asyncio.Event()
        cancelled_bodies: list[int] = []

        async def loop_body() -> None:
            attempt = len(starts) + 1
            starts.append(attempt)
            if attempt <= 2:
                raise RuntimeError("loop body escaped")
            healthy.set()
            try:
                await stop.wait()
            except asyncio.CancelledError:
                cancelled_bodies.append(attempt)
                raise

        supervisor = asyncio.create_task(worker._run_billing_supervisor(
            stop, loop_body, max_restarts=5, restart_backoff=0.0,
        ))
        await asyncio.wait_for(healthy.wait(), timeout=5)
        # Third loop is alive: readiness must see a running loop again.
        assert worker._BILLING_LOOP_RUNNING is True
        assert supervisor.done() is False

        stop.set()
        await asyncio.wait_for(supervisor, timeout=5)
        return starts, watch.cancels, cancelled_bodies

    starts, cancels, cancelled_bodies = asyncio.run(scenario())

    # Two failures -> two restarts -> a third loop that ran until `stop`.
    assert starts == [1, 2, 3]
    restarts = recorder.named("billing_loop_stopped")
    assert [entry["restart"] for entry in restarts] == [1, 2]
    assert [entry["error_type"] for entry in restarts] == ["RuntimeError", "RuntimeError"]
    # The clean, stop-driven return is NOT counted as a failure restart.
    assert len(restarts) == 2
    assert recorder.named("billing_loop_unrecoverable") == []
    # Drain contract: the inner loop is never cancelled out from under itself.
    assert cancels == []
    assert cancelled_bodies == []
    assert worker._BILLING_LOOP_RUNNING is False


def test_supervisor_escalates_to_a_process_restart_at_max_restarts(
    recorder: LogRecorder, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(worker, "_BILLING_REQUIRED", True)

    async def scenario():
        watch = CancelWatch(monkeypatch, "billing-recovery")
        stop = asyncio.Event()
        starts: list[int] = []

        async def loop_body() -> None:
            starts.append(len(starts) + 1)
            raise RuntimeError("loop body escaped")

        await asyncio.wait_for(worker._run_billing_supervisor(
            stop, loop_body, max_restarts=2, restart_backoff=0.0,
        ), timeout=5)
        return starts, watch.cancels, stop.is_set()

    starts, cancels, stop_set = asyncio.run(scenario())

    # max_restarts=2 -> two restarts, and the third failure escalates.
    assert starts == [1, 2, 3]
    assert [entry["restart"] for entry in recorder.named("billing_loop_stopped")] == [1, 2, 3]
    halted = recorder.named("billing_loop_unrecoverable")
    assert len(halted) == 1
    assert halted[0] == {"outcome": "halted", "restarts": 3, "billing_required": True}
    # Authoritative billing cannot silently halt: escalate by setting `stop`,
    # which drains the whole worker so the orchestrator restarts it.
    assert stop_set is True
    assert cancels == []
    assert worker._BILLING_LOOP_RUNNING is False


def test_supervisor_halts_without_escalating_when_billing_is_not_required(
    recorder: LogRecorder, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(worker, "_BILLING_REQUIRED", False)
    monkeypatch.setattr(worker, "_BILLING_CONFIGURED", True)
    monkeypatch.setattr(worker, "_BILLING_ROLE", "optional")

    async def scenario():
        stop = asyncio.Event()
        starts: list[int] = []

        async def loop_body() -> None:
            starts.append(len(starts) + 1)
            raise RuntimeError("loop body escaped")

        await asyncio.wait_for(worker._run_billing_supervisor(
            stop, loop_body, max_restarts=0, restart_backoff=0.0,
        ), timeout=5)
        return starts, stop.is_set()

    starts, stop_set = asyncio.run(scenario())

    # max_restarts=0 -> the first failure escalates immediately, no restart.
    assert starts == [1]
    assert recorder.named("billing_loop_unrecoverable")[0]["billing_required"] is False
    # A non-required worker keeps consuming durable jobs instead of restarting.
    assert stop_set is False
    # ...but it must stop advertising a healthy billing loop (the old /ready
    # hardcoded billing_ready=True for every non-required role).
    assert worker._BILLING_LOOP_RUNNING is False
    status, health = worker._billing_recovery_block()
    assert status == "unavailable"
    assert health["running"] is False
    assert worker._billing_loop_ready() is False


def test_supervisor_awaits_an_in_flight_loop_instead_of_cancelling_it(
    recorder: LogRecorder, monkeypatch: pytest.MonkeyPatch,
):
    async def scenario():
        watch = CancelWatch(monkeypatch, "billing-recovery")
        stop = asyncio.Event()
        started = asyncio.Event()
        finished: list[str] = []

        async def loop_body() -> None:
            started.set()
            await stop.wait()
            # Stands in for the loop's bounded to_thread tail (lease release and
            # client close) that must complete before the process exits.
            await asyncio.sleep(0.05)
            finished.append("drained")

        supervisor = asyncio.create_task(worker._run_billing_supervisor(
            stop, loop_body, max_restarts=5, restart_backoff=0.0,
        ))
        await asyncio.wait_for(started.wait(), timeout=5)
        stop.set()
        await asyncio.wait_for(supervisor, timeout=5)
        return finished, watch.cancels, len(watch.tasks)

    finished, cancels, task_count = asyncio.run(scenario())

    assert finished == ["drained"]
    assert cancels == []
    # A clean, stop-driven exit must not spawn a replacement loop.
    assert task_count == 1
    assert recorder.named("billing_loop_stopped") == []
    assert worker._BILLING_LOOP_RUNNING is False


def test_supervisor_does_not_restart_a_failed_loop_during_shutdown(
    recorder: LogRecorder, monkeypatch: pytest.MonkeyPatch,
):
    # A loop that raises *while* the worker is draining must be reported but
    # never replaced: a fresh loop would claim rows the drain then abandons.
    async def scenario():
        stop = asyncio.Event()
        starts: list[int] = []

        async def loop_body() -> None:
            starts.append(len(starts) + 1)
            stop.set()
            raise RuntimeError("raised during drain")

        await asyncio.wait_for(worker._run_billing_supervisor(
            stop, loop_body, max_restarts=5, restart_backoff=0.0,
        ), timeout=5)
        return starts

    starts = asyncio.run(scenario())
    assert starts == [1]
    failures = recorder.named("billing_loop_stopped")
    assert [entry["error_type"] for entry in failures] == ["RuntimeError"]
    assert recorder.named("billing_loop_unrecoverable") == []


def test_supervisor_cancellation_propagates_and_reports_a_stopped_loop(
    recorder: LogRecorder, monkeypatch: pytest.MonkeyPatch,
):
    # run() never cancels the supervisor, but if something else does, the
    # cancellation must not be swallowed and the loop must be marked stopped.
    async def scenario():
        stop = asyncio.Event()
        started = asyncio.Event()

        async def loop_body() -> None:
            started.set()
            await stop.wait()

        supervisor = asyncio.create_task(worker._run_billing_supervisor(
            stop, loop_body, max_restarts=5, restart_backoff=0.0,
        ))
        await asyncio.wait_for(started.wait(), timeout=5)
        supervisor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await supervisor

    asyncio.run(scenario())
    assert worker._BILLING_LOOP_RUNNING is False


def test_drain_helper_swallows_supervisor_failure_but_not_a_clean_result(
    recorder: LogRecorder,
):
    async def scenario():
        assert await worker._drain_billing_supervisor(None) is None

        async def clean() -> None:
            return None

        await worker._drain_billing_supervisor(asyncio.create_task(clean()))
        assert recorder.named("billing_supervisor_failed") == []

        async def boom() -> None:
            raise RuntimeError("supervisor died")

        after = []
        try:
            await worker._drain_billing_supervisor(asyncio.create_task(boom()))
        finally:
            after.append("cleanup")
        return after

    assert asyncio.run(scenario()) == ["cleanup"]
    assert recorder.named("billing_supervisor_failed") == [
        {"outcome": "degraded", "error_type": "RuntimeError"},
    ]


class _FakeUvicornConfig:
    def __init__(self, app, **kwargs):
        self.app = app
        self.kwargs = kwargs


class _FakeUvicornServer:
    instances: list["_FakeUvicornServer"] = []

    def __init__(self, config):
        self.config = config
        self.should_exit = False
        self.sockets: list = []
        _FakeUvicornServer.instances.append(self)

    async def serve(self, sockets=None):
        self.sockets = list(sockets or [])
        while not self.should_exit:
            await asyncio.sleep(0.005)
        for sock in self.sockets:
            sock.close()


class _FakeRedis:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class _FakeCipherCache:
    def __init__(self) -> None:
        self.cleared = 0

    def clear(self) -> None:
        self.cleared += 1


def test_run_completes_shutdown_cleanup_when_the_supervisor_dies(
    recorder: LogRecorder, monkeypatch: pytest.MonkeyPatch,
):
    """FIX-8 part 2, end to end through run().

    A bare `await billing_task` in run()'s finally re-raised a stored supervisor
    exception and skipped every later shutdown step. Here the supervisor sets
    `stop` (the escalation path) and then dies; run() must still stop the health
    server, drain and close provider clients, close Redis clients, clear the
    credential cipher cache, and flush observability.
    """
    _FakeUvicornServer.instances.clear()
    calls: list[str] = []

    async def exploding_supervisor(stop, loop_factory, *, max_restarts, restart_backoff):
        calls.append("supervisor")
        assert callable(loop_factory)
        stop.set()
        raise RuntimeError("supervisor died")

    class FakeStore:
        def purge_provider_configs(self):
            calls.append("purge_provider_configs")

        def purge_warm_state(self, days):
            calls.append("purge_warm_state")

    class FakeJobStore:
        def purge(self):
            calls.append("purge_jobs")

    limiter_redis = _FakeRedis()
    dispatcher_redis = _FakeRedis()
    cipher_cache = _FakeCipherCache()

    async def dependencies_unavailable():
        return False, False

    async def kms_disabled():
        return {"configured": False, "active_probe": False, "fresh": False}

    monkeypatch.setenv("PORT", "0")
    monkeypatch.setenv("BREVITAS_WORKER_CONCURRENCY", "1")
    monkeypatch.setenv("BREVITAS_WORKER_DRAIN_SECONDS", "5")
    monkeypatch.setenv("BREVITAS_PROVIDER_CLOSE_DRAIN_SECONDS", "0")
    monkeypatch.setenv("BREVITAS_WORKER_BILLING_ROLE", "optional")
    monkeypatch.delenv("BREVITAS_WARMING", raising=False)
    monkeypatch.setattr(worker, "_production_runtime", lambda: False)
    monkeypatch.setattr(worker, "validate_production_build_identity", lambda _p: None)
    monkeypatch.setattr(worker, "_configure_managed_kms_from_deployment", lambda: None)
    monkeypatch.setattr(
        worker, "_initialize_credential_cipher",
        lambda required: types.SimpleNamespace(cache=cipher_cache))
    monkeypatch.setattr(worker, "billing_recovery_is_configured", lambda: True)
    monkeypatch.setattr(
        worker, "build_billing_recovery_processor_from_env",
        lambda **kwargs: object())
    monkeypatch.setattr(worker, "_run_billing_supervisor", exploding_supervisor)
    monkeypatch.setattr(worker, "_dependencies_ready", dependencies_unavailable)
    monkeypatch.setattr(worker, "_kms_readiness_status", kms_disabled)
    monkeypatch.setattr(worker, "uvicorn", types.SimpleNamespace(
        Config=_FakeUvicornConfig, Server=_FakeUvicornServer))
    monkeypatch.setattr(worker, "_store", FakeStore())
    monkeypatch.setattr(worker, "_job_service", types.SimpleNamespace(
        store=FakeJobStore(),
        dispatcher=types.SimpleNamespace(redis=dispatcher_redis),
    ))
    monkeypatch.setattr(worker, "_distributed_limiter", types.SimpleNamespace(
        redis=limiter_redis))
    monkeypatch.setattr(
        worker, "_wait_for_provider_calls",
        lambda drain: calls.append("provider_drain") or True)
    monkeypatch.setattr(
        worker, "close_provider_sync_clients",
        lambda: calls.append("close_provider_clients"))
    monkeypatch.setattr(
        worker, "graceful_observability_shutdown",
        lambda: calls.append("observability_shutdown"))

    asyncio.run(asyncio.wait_for(worker.run(), timeout=30))

    assert calls.count("supervisor") == 1
    # The supervisor's failure was reported instead of escaping run()'s finally.
    assert recorder.named("billing_supervisor_failed") == [
        {"outcome": "degraded", "error_type": "RuntimeError"},
    ]
    # Every shutdown step after the billing await still ran.
    assert len(_FakeUvicornServer.instances) == 1
    assert _FakeUvicornServer.instances[0].should_exit is True
    assert "provider_drain" in calls
    assert "close_provider_clients" in calls
    assert limiter_redis.closed == 1
    assert dispatcher_redis.closed == 1
    assert cipher_cache.cleared == 1
    assert calls[-1] == "observability_shutdown"


def test_run_drains_the_billing_supervisor_before_any_other_cleanup():
    # Ordering is load-bearing and not observable from the unit tests above:
    # the billing loop must be awaited (never cancelled) while the health server
    # and shared clients are still alive.
    source = (ROOT / "api/worker.py").read_text()
    drain = source.index("await _drain_billing_supervisor(billing_task)")
    assert drain < source.index("health_server.should_exit = True")
    assert drain < source.index("await asyncio.to_thread(close_provider_sync_clients)")
    assert drain < source.index("        graceful_observability_shutdown()")
    # The supervisor's inner loop task is created exactly once, and no cancel of
    # it exists outside the supervisor-cancelled path.
    assert source.count('name="billing-recovery"') == 1
    assert "inner.exception()" not in source
