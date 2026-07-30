from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from api.billing_recovery import (
    ENTRY_KIND_LEDGER,
    LEDGER_ALERT_NAMES,
    SETTLEMENT_ALERT_NAMES,
    ENTRY_KIND_SETTLEMENT,
    STRIPE_API_VERSION,
    BillingEntry,
    BillingHealth,
    BillingLoopHealth,
    BillingRecoveryProcessor,
    BillingRecoverySettings,
    CatalogContractError,
    LoggingBillingTelemetry,
    Reconciliation,
    StripeAmbiguous,
    StripeRestBillingGateway,
    StripeRejected,
    StripeUnavailable,
    SupabaseBillingStore,
    SupabaseSettlementStore,
    billing_recovery_is_configured,
    build_billing_recovery_processor_from_env,
    billing_settlement_recovery_is_enabled,
    billing_worker_owner,
    run_billing_recovery_loop,
)


NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)


class SimulatedWorkerCrash(BaseException):
    pass


class FakeStore:
    def __init__(self, now=NOW):
        self.now = now
        self.lock = threading.Lock()
        self.release_calls = 0
        self.release_unsent_calls = 0
        self.claim_calls = 0
        self.health_calls = 0
        self.row = {
            "id": 41,
            "user_id": "00000000-0000-0000-0000-000000000041",
            "occurred_at": now - timedelta(minutes=1),
            "fee_microusd": 250_000,
            "stripe_customer_id": "cus_mock",
            "attempts": 0,
            "status": "pending",
            "lease_owner": None,
            "lease_expires_at": None,
            "outbound_started_at": None,
            "period_start": now - timedelta(days=3),
            "period_end": now + timedelta(days=4),
            "expected_period_microusd": 250_000,
            "last_error": "",
        }

    def claim_one(self, owner, *, lease_seconds, cap_microusd):
        with self.lock:
            self.claim_calls += 1
            row = self.row
            reclaim = (
                row["status"] == "sending"
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] < self.now
            )
            if row["status"] != "pending" and not reclaim:
                return None
            row["status"] = "sending"
            row["lease_owner"] = owner
            row["lease_expires_at"] = self.now + timedelta(seconds=lease_seconds)
            row["attempts"] += 1
            return BillingEntry(
                id=row["id"],
                user_id=row["user_id"],
                occurred_at=row["occurred_at"],
                fee_microusd=row["fee_microusd"],
                stripe_customer_id=row["stripe_customer_id"],
                attempts=row["attempts"],
                reclaimed=reclaim,
                outbound_started_at=row["outbound_started_at"],
                period_start=row["period_start"],
                period_end=row["period_end"],
                expected_period_microusd=row["expected_period_microusd"],
            )

    def begin_send(self, entry_id, owner):
        with self.lock:
            if self.row["id"] != entry_id or self.row["lease_owner"] != owner:
                return False
            if self.row["lease_expires_at"] <= self.now:
                return False
            self.row["outbound_started_at"] = self.row["outbound_started_at"] or self.now
            return True

    def renew(self, entry_id, owner, lease_seconds):
        with self.lock:
            if (
                self.row["id"] != entry_id
                or self.row["lease_owner"] != owner
                or self.row["lease_expires_at"] <= self.now
            ):
                return False
            self.row["lease_expires_at"] = self.now + timedelta(seconds=lease_seconds)
            return True

    def complete(self, entry_id, owner, status, error=""):
        with self.lock:
            if self.row["id"] != entry_id or self.row["lease_owner"] != owner:
                return False
            self.row.update(
                status=status,
                last_error=error,
                lease_owner=None,
                lease_expires_at=None,
            )
            return True

    def release_owner(self, owner):
        with self.lock:
            self.release_calls += 1
            if self.row["lease_owner"] != owner or self.row["outbound_started_at"] is not None:
                return 0
            self.row.update(
                status="pending",
                attempts=max(0, self.row["attempts"] - 1),
                lease_owner=None,
                lease_expires_at=None,
            )
            return 1

    def release_unsent(self, entry_id, owner):
        # Mirrors public.release_billing_ledger_unsent (202607280011): fenced on
        # the entry id, `sending`, and the lease owner; clears the outbound
        # marker and refunds the attempt. Legitimate only after an HTTP 429.
        with self.lock:
            self.release_unsent_calls += 1
            if (
                self.row["id"] != entry_id
                or self.row["status"] != "sending"
                or self.row["lease_owner"] != owner
            ):
                return False
            self.row.update(
                status="pending",
                attempts=max(0, self.row["attempts"] - 1),
                outbound_started_at=None,
                lease_owner=None,
                lease_expires_at=None,
                last_error=(
                    "Stripe rate limited the request; the meter event was not processed"
                ),
            )
            return True

    def check_health(self):
        with self.lock:
            self.health_calls += 1
            status = self.row["status"]
            return BillingHealth(
                pending_count=int(status == "pending"),
                review_count=int(status == "review"),
                dead_count=int(status == "dead"),
                stale_sending_count=int(
                    status == "sending" and self.row["lease_expires_at"] < self.now
                ),
                oldest_pending_seconds=60 if status == "pending" else 0,
            )

    def expire_lease(self, *, advance=timedelta(minutes=3)):
        with self.lock:
            self.now += advance
            self.row["lease_expires_at"] = self.now - timedelta(seconds=1)


class FakeStripe:
    def __init__(self, mode="success"):
        self.mode = mode
        self.send_calls = 0
        self.accepted_identifiers = set()
        self.force_unknown_reconcile = False
        self.reconcile_calls = 0
        self.validate_calls = 0
        self.closed = False
        self.lock = threading.Lock()

    def send(self, entry):
        with self.lock:
            self.send_calls += 1
            if self.mode == "crash_before_accept":
                raise SimulatedWorkerCrash()
            if self.mode == "rejected":
                raise StripeRejected("mock invalid customer")
            # Mock Stripe's stable-identifier deduplication: repeated delivery
            # never creates a second accepted charge.
            self.accepted_identifiers.add(entry.stripe_identifier)
            if self.mode == "crash_after_accept":
                raise SimulatedWorkerCrash()
            if self.mode == "timeout_after_accept":
                raise TimeoutError("mock response timeout")
        time.sleep(0.01)

    def validate_contract(self, heartbeat=None):
        if heartbeat is not None and not heartbeat():
            raise RuntimeError("lease lost")
        self.validate_calls += 1
        return "mtr_mock"

    def reconcile(self, entry, heartbeat=None):
        if heartbeat is not None and not heartbeat():
            return Reconciliation.UNKNOWN
        with self.lock:
            self.reconcile_calls += 1
            if self.force_unknown_reconcile:
                return Reconciliation.UNKNOWN
            return (
                Reconciliation.ACCEPTED
                if entry.stripe_identifier in self.accepted_identifiers
                else Reconciliation.UNKNOWN
            )

    def close(self):
        self.closed = True


class RecordingTelemetry:
    def __init__(self):
        self.metrics = []
        self.alerts = []

    def metric(self, name, value, attributes=None):
        self.metrics.append((name, value, attributes or {}))

    def alert(self, name, severity, fields):
        self.alerts.append((name, severity, dict(fields)))


def processor(store, stripe, *, now=lambda: NOW, telemetry=None, settlement_store=None):
    return BillingRecoveryProcessor(
        store=store,
        stripe=stripe,
        settings=BillingRecoverySettings(
            lease_seconds=120,
            poll_seconds=1,
            cap_microusd=10_000_000,
        ),
        telemetry=telemetry or LoggingBillingTelemetry(),
        now=now,
        settlement_store=settlement_store,
    )


def test_atomic_concurrent_claim_sends_exactly_once():
    store = FakeStore()
    stripe = FakeStripe()
    workers = [processor(store, stripe), processor(store, stripe)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda pair: pair[1].process_once(f"worker-{pair[0]}"),
            enumerate(workers),
        ))

    assert sum(result.claimed for result in results) == 1
    assert sum(result.reported for result in results) == 1
    assert stripe.send_calls == 1
    assert len(stripe.accepted_identifiers) == 1
    assert store.row["status"] == "reported"


def test_crash_after_acceptance_recovers_by_reconciliation_without_resend():
    store = FakeStore()
    stripe = FakeStripe("crash_after_accept")
    recovery = processor(store, stripe, now=lambda: store.now)

    with pytest.raises(SimulatedWorkerCrash):
        recovery.process_once("crashed-worker")
    assert store.row["status"] == "sending"
    assert stripe.send_calls == 1

    store.expire_lease()
    stripe.mode = "success"
    result = recovery.process_once("replacement-worker")

    assert result.reconciled == 1
    assert result.reported == 1
    assert stripe.send_calls == 1
    assert len(stripe.accepted_identifiers) == 1
    assert store.row["status"] == "reported"


def test_crash_after_claim_before_outbound_is_reclaimed_without_reconciliation_or_double_cap():
    store = FakeStore()
    stripe = FakeStripe()
    claimed = store.claim_one(
        "claim-only-crash", lease_seconds=120, cap_microusd=10_000_000,
    )
    assert claimed is not None
    assert store.row["outbound_started_at"] is None
    store.expire_lease()

    result = processor(store, stripe, now=lambda: store.now).process_once("replacement-worker")

    assert result.reported == 1
    assert stripe.reconcile_calls == 0
    assert stripe.send_calls == 1
    assert len(stripe.accepted_identifiers) == 1
    assert store.row["attempts"] == 2
    assert store.row["status"] == "reported"


def test_timeout_is_reconciled_and_never_double_charged():
    store = FakeStore()
    stripe = FakeStripe("timeout_after_accept")

    result = processor(store, stripe).process_once("worker")

    assert result.reported == 1
    assert result.reconciled == 1
    assert result.review == 0
    assert stripe.send_calls == 1
    assert len(stripe.accepted_identifiers) == 1


def test_crash_before_acceptance_replays_same_identifier_inside_safe_window():
    store = FakeStore()
    stripe = FakeStripe("crash_before_accept")
    recovery = processor(store, stripe, now=lambda: store.now)

    with pytest.raises(SimulatedWorkerCrash):
        recovery.process_once("crashed-worker")
    store.expire_lease()
    stripe.mode = "success"

    result = recovery.process_once("replacement-worker")

    assert result.reported == 1
    assert result.reconciled == 0
    assert stripe.send_calls == 2
    assert len(stripe.accepted_identifiers) == 1
    assert store.row["status"] == "reported"


def test_reconciliation_lag_replays_stable_identifier_without_second_charge():
    store = FakeStore()
    stripe = FakeStripe("crash_after_accept")
    recovery = processor(store, stripe, now=lambda: store.now)

    with pytest.raises(SimulatedWorkerCrash):
        recovery.process_once("crashed-worker")
    store.expire_lease()
    stripe.mode = "success"
    stripe.force_unknown_reconcile = True

    result = recovery.process_once("replacement-worker")

    assert result.reported == 1
    assert stripe.send_calls == 2
    assert len(stripe.accepted_identifiers) == 1
    assert store.row["status"] == "reported"


def test_duplicate_invocation_after_terminal_state_is_a_noop():
    store = FakeStore()
    stripe = FakeStripe()
    recovery = processor(store, stripe)

    first = recovery.process_once("worker-a")
    duplicate = recovery.process_once("worker-b")

    assert first.reported == 1
    assert duplicate.claimed == 0
    assert stripe.send_calls == 1


def test_slow_send_expiry_is_fenced_and_second_worker_does_not_double_charge():
    store = FakeStore()
    started = threading.Event()
    release = threading.Event()

    class SlowAcceptedStripe(FakeStripe):
        def send(self, entry):
            with self.lock:
                self.send_calls += 1
                self.accepted_identifiers.add(entry.stripe_identifier)
            store.expire_lease()
            started.set()
            assert release.wait(timeout=2)

    stripe = SlowAcceptedStripe()
    recovery = processor(store, stripe, now=lambda: store.now)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first_future = pool.submit(recovery.process_once, "slow-worker")
        assert started.wait(timeout=2)
        second = recovery.process_once("replacement-worker")
        release.set()
        first = first_future.result(timeout=2)

    assert second.reconciled == 1
    assert second.reported == 1
    assert first.lease_lost == 1
    assert stripe.send_calls == 1
    assert len(stripe.accepted_identifiers) == 1
    assert store.row["status"] == "reported"


def test_reclaimed_send_outside_dedupe_window_requires_review():
    store = FakeStore()
    stripe = FakeStripe("crash_before_accept")
    recovery = processor(store, stripe, now=lambda: store.now)

    with pytest.raises(SimulatedWorkerCrash):
        recovery.process_once("crashed-worker")
    store.expire_lease(advance=timedelta(hours=24))
    stripe.mode = "success"

    result = recovery.process_once("replacement-worker")

    assert result.review == 1
    assert stripe.send_calls == 1
    assert not stripe.accepted_identifiers
    assert store.row["status"] == "review"


def test_definitive_stripe_rejection_goes_dead_and_alertable():
    store = FakeStore()
    stripe = FakeStripe("rejected")
    recovery = processor(store, stripe)

    result = recovery.process_once("worker")

    assert result.dead == 1
    assert store.row["status"] == "dead"
    assert recovery.check_health().dead_count == 1


def test_shutdown_releases_never_sent_claims_and_closes_client():
    store = FakeStore()
    stripe = FakeStripe()
    recovery = processor(store, stripe)
    stop = asyncio.Event()
    stop.set()

    asyncio.run(run_billing_recovery_loop(recovery, stop, owner="stopping-worker"))

    assert store.release_calls == 1
    assert stripe.closed is True


def test_cancellation_waits_for_inflight_thread_before_closing_resources():
    store = FakeStore()
    started = threading.Event()
    release = threading.Event()

    class BlockingStripe(FakeStripe):
        def send(self, entry):
            started.set()
            assert release.wait(timeout=2)
            super().send(entry)

    stripe = BlockingStripe()
    recovery = processor(store, stripe)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_billing_recovery_loop(recovery, stop, owner="cancelled-worker")
        )
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert stripe.closed is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stripe.closed is True

    asyncio.run(scenario())


def test_loop_health_fails_closed_on_invalid_stripe_credentials_and_catalog():
    response_sets = (
        [FakeResponse({"error": {"type": "invalid_request_error"}}, status_code=401)],
        catalog_responses(formula="count"),
    )
    for responses in response_sets:
        store = FakeStore()
        session = FakeSession(responses)
        gateway = StripeRestBillingGateway(
            "sk_test_invalid", "price_mock", "brevitas_fee_microusd",
            session=session, now=lambda: NOW,
        )
        recovery = processor(store, gateway)
        snapshots: list[BillingLoopHealth] = []

        async def scenario():
            stop = asyncio.Event()

            def reporter(snapshot):
                snapshots.append(snapshot)
                if snapshot.consecutive_errors:
                    stop.set()

            await asyncio.wait_for(
                run_billing_recovery_loop(recovery, stop, health_reporter=reporter),
                timeout=2,
            )

        asyncio.run(scenario())

        error = next(snapshot for snapshot in snapshots if snapshot.consecutive_errors)
        assert error.running is True
        assert error.initial_validation_succeeded is False
        assert error.catalog_valid is False
        assert error.last_success_monotonic is None
        assert error.last_error_monotonic is not None
        assert store.health_calls == 0
        assert store.claim_calls == 0
        assert snapshots[-1].running is False


def test_loop_health_fails_closed_when_supabase_health_is_unavailable():
    class UnavailableStore(FakeStore):
        def check_health(self):
            self.health_calls += 1
            raise RuntimeError("mock database unavailable")

    store = UnavailableStore()
    stripe = FakeStripe()
    recovery = processor(store, stripe)
    snapshots = []

    async def scenario():
        stop = asyncio.Event()

        def reporter(snapshot):
            snapshots.append(snapshot)
            if snapshot.consecutive_errors:
                stop.set()

        await asyncio.wait_for(
            run_billing_recovery_loop(recovery, stop, health_reporter=reporter),
            timeout=2,
        )

    asyncio.run(scenario())

    error = next(snapshot for snapshot in snapshots if snapshot.consecutive_errors)
    assert error.initial_validation_succeeded is False
    assert error.catalog_valid is True
    assert error.last_success_monotonic is None
    assert store.health_calls == 1
    assert store.claim_calls == 0
    assert snapshots[-1].running is False


def test_loop_health_repeated_errors_preserve_staleness_then_recovery_resets():
    class RecoveringStore(FakeStore):
        def claim_one(self, owner, *, lease_seconds, cap_microusd):
            self.claim_calls += 1
            if self.claim_calls <= 2:
                raise RuntimeError("mock transient claim failure")
            return None

    store = RecoveringStore()
    stripe = FakeStripe()
    recovery = processor(store, stripe)
    recovery.settings = replace(recovery.settings, poll_seconds=0.01)
    snapshots = []
    saw_two_errors = False

    async def scenario():
        stop = asyncio.Event()

        def reporter(snapshot):
            nonlocal saw_two_errors
            snapshots.append(snapshot)
            if snapshot.consecutive_errors >= 2:
                saw_two_errors = True
            if (
                saw_two_errors
                and snapshot.initial_validation_succeeded
                and snapshot.consecutive_errors == 0
                and snapshot.last_error_monotonic is not None
            ):
                stop.set()

        await asyncio.wait_for(
            run_billing_recovery_loop(recovery, stop, health_reporter=reporter),
            timeout=2,
        )

    asyncio.run(scenario())

    initial = next(snapshot for snapshot in snapshots
                   if snapshot.initial_validation_succeeded and snapshot.last_error_monotonic is None)
    errors = [snapshot for snapshot in snapshots if snapshot.consecutive_errors]
    recovered = next(snapshot for snapshot in snapshots
                     if snapshot.consecutive_errors == 0
                     and snapshot.last_error_monotonic is not None
                     and snapshot.last_success_monotonic != initial.last_success_monotonic)
    assert [snapshot.consecutive_errors for snapshot in errors[:2]] == [1, 2]
    assert errors[0].last_success_monotonic == initial.last_success_monotonic
    assert errors[1].last_success_monotonic == initial.last_success_monotonic
    assert errors[1].last_error_monotonic >= errors[0].last_error_monotonic
    assert recovered.catalog_valid is True
    assert recovered.last_success_monotonic > initial.last_success_monotonic
    assert snapshots[-1].running is False


def test_idle_health_cycle_detects_catalog_or_credential_outage_then_recovers():
    failures = (
        CatalogContractError("mock catalog mismatch"),
        StripeAmbiguous("mock credential endpoint outage"),
    )
    for failure in failures:
        class IdleStore(FakeStore):
            def claim_one(self, owner, *, lease_seconds, cap_microusd):
                self.claim_calls += 1
                return None

        class ScriptedStripe(FakeStripe):
            def validate_contract(self, heartbeat=None):
                if heartbeat is not None and not heartbeat():
                    raise RuntimeError("lease lost")
                self.validate_calls += 1
                if self.validate_calls == 2:
                    raise failure
                return "mtr_mock"

        store = IdleStore()
        stripe = ScriptedStripe()
        recovery = processor(store, stripe)
        recovery.settings = replace(recovery.settings, poll_seconds=0.01)
        snapshots = []
        saw_error = False

        async def scenario():
            stop = asyncio.Event()

            def reporter(snapshot):
                nonlocal saw_error
                snapshots.append(snapshot)
                if snapshot.consecutive_errors:
                    saw_error = True
                if (
                    saw_error
                    and snapshot.initial_validation_succeeded
                    and snapshot.catalog_valid
                    and snapshot.consecutive_errors == 0
                    and snapshot.last_error_monotonic is not None
                ):
                    stop.set()

            await asyncio.wait_for(
                run_billing_recovery_loop(recovery, stop, health_reporter=reporter),
                timeout=2,
            )

        asyncio.run(scenario())

        initial = next(snapshot for snapshot in snapshots
                       if snapshot.initial_validation_succeeded
                       and snapshot.last_error_monotonic is None)
        error = next(snapshot for snapshot in snapshots if snapshot.consecutive_errors)
        recovered = next(snapshot for snapshot in snapshots
                         if snapshot.last_error_monotonic is not None
                         and snapshot.catalog_valid
                         and snapshot.consecutive_errors == 0)
        assert store.claim_calls >= 2
        assert store.health_calls >= 3
        assert error.initial_validation_succeeded is True
        assert error.catalog_valid is False
        assert error.last_success_monotonic == initial.last_success_monotonic
        assert error.last_error_monotonic is not None
        assert recovered.last_success_monotonic > initial.last_success_monotonic
        assert snapshots[-1].running is False


def test_loop_health_reporter_failure_cannot_kill_startup_or_shutdown_reporting():
    store = FakeStore()
    stripe = FakeStripe()
    recovery = processor(store, stripe)
    snapshots = []
    calls = 0

    async def scenario():
        stop = asyncio.Event()

        def reporter(snapshot):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("mock reporter failure")
            snapshots.append(snapshot)
            if snapshot.initial_validation_succeeded:
                stop.set()

        await asyncio.wait_for(
            run_billing_recovery_loop(recovery, stop, health_reporter=reporter),
            timeout=2,
        )

    asyncio.run(scenario())

    assert calls >= 3
    assert any(snapshot.running and snapshot.initial_validation_succeeded for snapshot in snapshots)
    assert snapshots[-1].running is False
    assert stripe.closed is True


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.headers = {}
        self.auth = None

    def request(self, method, url, **kwargs):
        recorded = dict(kwargs)
        if "params" in recorded:
            recorded["params"] = dict(recorded["params"])
        self.requests.append((method, url, recorded))
        if not self.responses:
            raise AssertionError("unexpected Stripe request")
        return self.responses.pop(0)

    def close(self):
        pass


ABSENT = object()


def catalog_responses(*summary_pages, formula="sum", interval_count=ABSENT):
    recurring = {"meter": "mtr_test", "interval": "week", "usage_type": "metered"}
    if interval_count is not ABSENT:
        recurring["interval_count"] = interval_count
    price = {
        "active": True,
        "type": "recurring",
        "currency": "usd",
        "billing_scheme": "per_unit",
        "unit_amount_decimal": "0.0001",
        "recurring": recurring,
    }
    meter = {
        "status": "active",
        "event_name": "brevitas_fee_microusd",
        "default_aggregation": {"formula": formula},
        "customer_mapping": {"type": "by_id", "event_payload_key": "stripe_customer_id"},
        "value_settings": {"event_payload_key": "value"},
    }
    return [FakeResponse(price), FakeResponse(meter), *(FakeResponse(page) for page in summary_pages)]


def reconciliation_entry(expected):
    return BillingEntry(
        id=700,
        user_id="user",
        occurred_at=NOW - timedelta(minutes=1),
        fee_microusd=expected,
        stripe_customer_id="cus_mock",
        attempts=2,
        reclaimed=True,
        outbound_started_at=NOW - timedelta(minutes=1),
        period_start=NOW - timedelta(days=1),
        period_end=NOW + timedelta(days=1),
        expected_period_microusd=expected,
    )


def test_reconciliation_pages_all_stripe_summaries_before_accepting():
    first_rows = [
        {"id": f"summary_{index}", "meter": "mtr_test", "aggregated_value": 1}
        for index in range(100)
    ]
    session = FakeSession(catalog_responses(
        {"data": first_rows, "has_more": True},
        {"data": [{"id": "summary_100", "meter": "mtr_test", "aggregated_value": 1}],
         "has_more": False},
    ))
    gateway = StripeRestBillingGateway(
        "sk_test_mock", "price_mock", "brevitas_fee_microusd",
        exclusive_meter_writer=True, session=session, now=lambda: NOW,
    )

    assert gateway.reconcile(reconciliation_entry(101), lambda: True) is Reconciliation.ACCEPTED
    summary_requests = session.requests[2:]
    assert len(summary_requests) == 2
    assert "starting_after" not in summary_requests[0][2]["params"]
    assert summary_requests[1][2]["params"]["starting_after"] == "summary_99"


def test_reconciliation_with_extra_events_or_nonexclusive_writer_stays_unknown():
    page = {
        "data": [{"id": "summary_extra", "meter": "mtr_test", "aggregated_value": 102}],
        "has_more": False,
    }
    exclusive = StripeRestBillingGateway(
        "sk_test_mock", "price_mock", "brevitas_fee_microusd",
        exclusive_meter_writer=True,
        session=FakeSession(catalog_responses(page)), now=lambda: NOW,
    )
    nonexclusive = StripeRestBillingGateway(
        "sk_test_mock", "price_mock", "brevitas_fee_microusd",
        exclusive_meter_writer=False,
        session=FakeSession(catalog_responses({
            "data": [{"id": "summary_exact", "meter": "mtr_test", "aggregated_value": 101}],
            "has_more": False,
        })),
        now=lambda: NOW,
    )

    assert exclusive.reconcile(reconciliation_entry(101), lambda: True) is Reconciliation.UNKNOWN
    assert nonexclusive.reconcile(reconciliation_entry(101), lambda: True) is Reconciliation.UNKNOWN


def test_reconciliation_rejects_non_sum_meter_even_when_total_would_match():
    session = FakeSession(catalog_responses({
        "data": [{"id": "summary", "meter": "mtr_test", "aggregated_value": 101}],
        "has_more": False,
    }, formula="count"))
    gateway = StripeRestBillingGateway(
        "sk_test_mock", "price_mock", "brevitas_fee_microusd",
        exclusive_meter_writer=True, session=session, now=lambda: NOW,
    )

    with pytest.raises(CatalogContractError, match="meter contract"):
        gateway.reconcile(reconciliation_entry(101), lambda: True)


def test_wrong_meter_contract_is_rejected_before_outbound_marker_or_event_post():
    store = FakeStore()
    session = FakeSession(catalog_responses(formula="count"))
    gateway = StripeRestBillingGateway(
        "sk_test_mock", "price_mock", "brevitas_fee_microusd",
        exclusive_meter_writer=True, session=session, now=lambda: NOW,
    )

    telemetry = RecordingTelemetry()
    recovery = processor(store, gateway, telemetry=telemetry)
    result = recovery.process_once("worker")
    recovery.check_health()

    assert result.dead == 0
    assert result.errors == 1
    assert result.reported == 0
    assert store.row["outbound_started_at"] is None
    assert store.row["status"] == "pending"
    assert store.row["attempts"] == 0
    assert recovery.catalog_contract_valid is False
    assert any(name == "billing_catalog_contract_invalid" and severity == "page"
               for name, severity, _ in telemetry.alerts)
    assert all(not url.endswith("/v1/billing/meter_events") for _, url, _ in session.requests)


def test_catalog_mismatch_on_reclaimed_ambiguous_send_goes_review_never_dead():
    store = FakeStore()
    claimed = store.claim_one("crashed", lease_seconds=120, cap_microusd=10_000_000)
    assert claimed is not None
    assert store.begin_send(claimed.id, "crashed") is True
    store.expire_lease()
    session = FakeSession(catalog_responses(formula="count"))
    gateway = StripeRestBillingGateway(
        "sk_test_mock", "price_mock", "brevitas_fee_microusd",
        exclusive_meter_writer=True, session=session, now=lambda: NOW,
    )

    result = processor(store, gateway, now=lambda: store.now).process_once("replacement")

    assert result.review == 1
    assert result.dead == 0
    assert result.reported == 0
    assert store.row["status"] == "review"
    assert all(not url.endswith("/v1/billing/meter_events") for _, url, _ in session.requests)


def test_valid_catalog_is_cached_before_normal_meter_event_post():
    session = FakeSession([
        *catalog_responses(),
        FakeResponse({"identifier": "brevitas-fee-41"}),
    ])
    gateway = StripeRestBillingGateway(
        "sk_test_mock", "price_mock", "brevitas_fee_microusd",
        exclusive_meter_writer=True, session=session, now=lambda: NOW,
    )
    store = FakeStore()

    result = processor(store, gateway).process_once("worker")
    assert result.reported == 1
    assert [method for method, _, _ in session.requests] == ["GET", "GET", "POST"]
    assert session.requests[-1][1].endswith("/v1/billing/meter_events")
    # The cached validation is lease-checked but performs no additional GET.
    assert gateway.validate_contract(lambda: True) == "mtr_test"
    assert len(session.requests) == 3


def test_prior_period_window_is_used_for_reconciliation_query():
    prior_start = datetime(2026, 7, 8, 10, tzinfo=timezone.utc)
    prior_end = datetime(2026, 7, 15, 10, tzinfo=timezone.utc)
    page = {
        "data": [{"id": "prior", "meter": "mtr_test", "aggregated_value": 250_000}],
        "has_more": False,
    }
    session = FakeSession(catalog_responses(page))
    gateway = StripeRestBillingGateway(
        "sk_test_mock", "price_mock", "brevitas_fee_microusd",
        exclusive_meter_writer=True, session=session, now=lambda: NOW,
    )
    entry = replace(
        reconciliation_entry(250_000),
        occurred_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        period_start=prior_start,
        period_end=prior_end,
    )

    assert gateway.reconcile(entry, lambda: True) is Reconciliation.ACCEPTED
    params = session.requests[-1][2]["params"]
    assert params["start_time"] == int(prior_start.timestamp())
    assert params["end_time"] == int(prior_end.timestamp())


def fixed_anchor_week(occurred_at, anchor_start):
    offset = (occurred_at - anchor_start) // timedelta(days=7)
    start = anchor_start + offset * timedelta(days=7)
    return start, start + timedelta(days=7)


def test_fixed_week_anchor_uses_half_open_utc_intervals_across_dst():
    anchor_start = datetime(2026, 3, 5, 10, tzinfo=timezone.utc)
    anchor_end = anchor_start + timedelta(days=7)

    assert fixed_anchor_week(anchor_start, anchor_start) == (anchor_start, anchor_end)
    assert fixed_anchor_week(anchor_end, anchor_start) == (
        anchor_end, anchor_end + timedelta(days=7),
    )
    assert fixed_anchor_week(anchor_start - timedelta(microseconds=1), anchor_start) == (
        anchor_start - timedelta(days=7), anchor_start,
    )
    # The US daylight-saving transition on March 8 cannot change UTC boundaries.
    assert fixed_anchor_week(datetime(2026, 3, 9, 12, tzinfo=timezone.utc), anchor_start) == (
        anchor_start, anchor_end,
    )


def test_configured_worker_id_is_only_a_prefix_and_owner_is_always_unique(monkeypatch):
    monkeypatch.setenv("BREVITAS_BILLING_WORKER_ID", "railway-billing")
    first = billing_worker_owner(hostname="replica", entropy="a" * 24)
    second = billing_worker_owner(hostname="replica", entropy="b" * 24)

    assert first.startswith("railway-billing_replica_")
    assert second.startswith("railway-billing_replica_")
    assert first != second
    assert first != "railway-billing"


def test_billing_recovery_requires_explicit_weekly_launch_gate(monkeypatch):
    required = {
        "STRIPE_SECRET_KEY": "sk_test_mock",
        "STRIPE_PRICE_ID": "price_weekly",
        "STRIPE_METER_EVENT_NAME": "brevitas_fee_microusd",
        "BREVITAS_BILLING_WEEKLY_CAP_USD": "100",
        "SUPABASE_SERVICE_ROLE_KEY": "service-role",
        "SUPABASE_URL": "https://example.supabase.co",
    }
    for name, value in required.items():
        monkeypatch.setenv(name, value)
    for disabled_value in (None, "", "false", "FALSE", "1", "yes", " true ", "TRUE"):
        if disabled_value is None:
            monkeypatch.delenv("BREVITAS_BILLING_ENABLED", raising=False)
        else:
            monkeypatch.setenv("BREVITAS_BILLING_ENABLED", disabled_value)
        assert billing_recovery_is_configured() is False
    monkeypatch.setenv("BREVITAS_BILLING_ENABLED", "true")
    assert billing_recovery_is_configured() is True


def test_migration_and_vercel_route_enforce_durable_worker_contract():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "supabase/migrations/202607170004_billing_recovery.sql").read_text()
    route = (root / "src/app/api/billing/sync/route.ts").read_text()

    assert "for update skip locked" in migration.lower()
    assert "or p_limit <> 1" in migration
    assert "lease_expires_at" in migration
    assert "outbound_started_at" in migration
    assert "renew_billing_ledger_lease" in migration
    assert "billing_recovery_health" in migration
    assert "prevent_billing_ledger_delete" in migration
    assert "prevent_billing_ledger_identity_change" in migration
    assert "new.usage_log_id is distinct from old.usage_log_id" in migration
    assert "new.fee_microusd is distinct from old.fee_microusd" in migration
    assert "was_reclaimed := candidate.status = 'sending'" in migration
    assert "if not was_reclaimed and committed + candidate.fee_microusd" in migration
    assert "billing_period_for_occurrence" in migration
    assert "p_anchor_end - p_anchor_start <> interval '7 days'" in migration
    assert "week_offset * interval '7 days'" in migration
    assert "end boundary must enter next period" in migration
    assert "UTC period changed across DST" in migration
    assert "ledger.occurred_at >= claim_period_start" in migration
    assert "ledger.occurred_at < claim_period_end" in migration
    assert "ROLLBACK PROCEDURE" in migration
    assert "manuallyResolveBillingLedgerEntry" in route
    assert "manual_only: true" in route
    assert "getStripe" not in route
    assert "export const GET" not in route


def test_stripe_keys_are_stable_and_contain_no_customer_data():
    entry = BillingEntry(
        id=7,
        user_id="user-secret",
        occurred_at=NOW,
        fee_microusd=1,
        stripe_customer_id="cus_secret",
        attempts=1,
        reclaimed=False,
        outbound_started_at=None,
        period_start=NOW,
        period_end=NOW + timedelta(days=7),
        expected_period_microusd=1,
    )
    duplicate = replace(entry, attempts=2, reclaimed=True)

    assert entry.stripe_identifier == duplicate.stripe_identifier == "brevitas-fee-7"
    assert entry.idempotency_key == duplicate.idempotency_key == "brevitas-meter-7"
    assert entry.user_id not in entry.stripe_identifier
    assert entry.stripe_customer_id not in entry.idempotency_key


class NonJsonResponse(FakeResponse):
    """A Stripe error page that is not JSON (proxy/CDN 4xx)."""

    def __init__(self, status_code):
        super().__init__(None, status_code=status_code)

    def json(self):
        raise ValueError("not JSON")


class RaisingSession(FakeSession):
    def __init__(self, error):
        super().__init__([])
        self.error = error

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, dict(kwargs)))
        raise self.error


def send_gateway(session):
    return StripeRestBillingGateway(
        "sk_test_mock", "price_mock", "brevitas_fee_microusd",
        session=session, now=lambda: NOW,
    )


def outbound_entry(entry_id=41, fee_microusd=250_000, occurred_at=None):
    occurred = occurred_at or NOW - timedelta(minutes=1)
    return BillingEntry(
        id=entry_id,
        user_id="00000000-0000-0000-0000-000000000041",
        occurred_at=occurred,
        fee_microusd=fee_microusd,
        stripe_customer_id="cus_mock",
        attempts=1,
        reclaimed=False,
        outbound_started_at=None,
        period_start=NOW - timedelta(days=3),
        period_end=NOW + timedelta(days=4),
        expected_period_microusd=fee_microusd,
    )


# U1: the status -> outcome mapping at api/billing_recovery.py:372-388 decides
# release-and-retry versus leave-in-'sending'-and-reconcile. Every documented
# class is pinned here because a wrong edge either strands revenue or authorises
# a duplicate send.
@pytest.mark.parametrize(("response", "expected"), [
    (FakeResponse({"error": {"type": "rate_limit_error"}}, 429), StripeUnavailable),
    (FakeResponse({"error": {"type": "api_error"}}, 500), StripeAmbiguous),
    (FakeResponse({"error": {"type": "api_error"}}, 503), StripeAmbiguous),
    (FakeResponse({"error": {"type": "api_error"}}, 408), StripeAmbiguous),
    (FakeResponse({"error": {"type": "api_error"}}, 409), StripeAmbiguous),
    (FakeResponse({"error": {"type": "idempotency_error"}}, 400), StripeAmbiguous),
    # A version rejection is a configuration failure, not a fact about the fee:
    # StripeRejected would terminalize the row as 'dead' (silent revenue loss),
    # so it must park for retry instead. Both surfaces Stripe uses are covered.
    (FakeResponse({"error": {"type": "invalid_request_error",
                             "code": "invalid_api_version"}}, 400), StripeUnavailable),
    (FakeResponse({"error": {"type": "invalid_request_error",
                             "message": "Invalid Stripe API version: 2026-06-24.dahlia"}},
                  400), StripeUnavailable),
    (FakeResponse({"error": {"type": "card_error"}}, 402), StripeRejected),
    (FakeResponse({"error": {"type": "invalid_request_error"}}, 400), StripeRejected),
    (FakeResponse({"error": {"type": "invalid_request_error"}}, 401), StripeRejected),
    (FakeResponse({"error": {"type": "invalid_request_error"}}, 403), StripeRejected),
    (FakeResponse({"error": {"type": "invalid_request_error"}}, 404), StripeRejected),
    (NonJsonResponse(400), StripeRejected),
    (FakeResponse([], 400), StripeRejected),
])
def test_stripe_send_status_maps_to_the_documented_outcome(response, expected):
    session = FakeSession([*catalog_responses(), response])
    gateway = send_gateway(session)

    with pytest.raises(expected) as raised:
        gateway.send(outbound_entry())

    # Definitive non-ingestion is exactly two cases: a 429, and a 400 that
    # rejects the pinned Stripe-Version header (the request never reached the
    # meter, so parking for retry cannot double-bill). Every other failure is
    # either ambiguous (reconcile) or a terminal judgment about the event.
    def _is_version_rejection(resp):
        try:
            err = resp.json().get("error", {})
        except Exception:
            return False
        return (err.get("code") == "invalid_api_version"
                or "api version" in str(err.get("message", "")).lower())
    expected_unavailable = (response.status_code == 429
                            or (response.status_code == 400
                                and _is_version_rejection(response)))
    assert (type(raised.value) is StripeUnavailable) == expected_unavailable
    assert session.requests[-1][1].endswith("/v1/billing/meter_events")


@pytest.mark.parametrize("error", [
    requests.Timeout("mock read timeout"),
    requests.ConnectionError("mock connection reset"),
])
def test_transport_failures_are_ambiguous_never_unavailable(error):
    gateway = send_gateway(RaisingSession(error))

    with pytest.raises(StripeAmbiguous):
        gateway.validate_contract()


def test_status_2xx_and_3xx_are_not_errors():
    session = FakeSession([*catalog_responses(), FakeResponse({"ok": True}, 200)])
    send_gateway(session).send(outbound_entry())
    assert len(session.requests) == 3


# U2: the outbound meter POST is a money instruction. Byte-exact.
def test_outbound_meter_event_post_is_byte_exact():
    session = FakeSession([*catalog_responses(), FakeResponse({"identifier": "brevitas-fee-41"})])
    gateway = send_gateway(session)
    # Fractional seconds must truncate to an integer unix timestamp.
    entry = outbound_entry(occurred_at=NOW - timedelta(seconds=90, microseconds=750_000))

    gateway.send(entry)

    method, url, kwargs = session.requests[-1]
    assert method == "POST"
    assert url == "https://api.stripe.com/v1/billing/meter_events"
    assert kwargs == {
        "data": {
            "event_name": "brevitas_fee_microusd",
            "identifier": "brevitas-fee-41",
            "timestamp": str(int(entry.occurred_at.timestamp())),
            "payload[stripe_customer_id]": "cus_mock",
            "payload[value]": "250000",
        },
        # G9: the pinned Stripe-Version is part of the money instruction now.
        # Every outbound call goes through _request, which merges the pin into
        # whatever headers the caller passed — so this byte-exact expectation
        # gains exactly one key and keeps the Idempotency-Key untouched.
        "headers": {
            "Idempotency-Key": "brevitas-meter-41",
            "Stripe-Version": STRIPE_API_VERSION,
        },
        "timeout": (3.0, 10.0),
    }
    assert kwargs["data"]["timestamp"] == "1784375909"
    assert entry.occurred_at.timestamp() == 1784375909.25
    # The event body carries no customer identity beyond the Stripe customer id.
    assert entry.user_id not in str(kwargs)


# U6 (FIX-9): interval_count is absent from the whole tree today, so a
# `week` / `interval_count: 2` price validates, anchors every subscription to
# 14 days, and then fails closed on every single fee row.
@pytest.mark.parametrize("interval_count", [1, ABSENT, None])
def test_weekly_price_with_a_single_interval_is_accepted(interval_count):
    session = FakeSession(catalog_responses(interval_count=interval_count))
    gateway = send_gateway(session)

    assert gateway.validate_contract() == "mtr_test"


@pytest.mark.parametrize("interval_count", [0, 2, 3, 4, 52])
def test_multi_week_price_interval_is_rejected_as_a_catalog_contract_error(interval_count):
    session = FakeSession(catalog_responses(interval_count=interval_count))
    gateway = send_gateway(session)

    with pytest.raises(CatalogContractError, match="violates the billing meter contract"):
        gateway.validate_contract()
    # Rejected on the Price alone; the meter is never even fetched.
    assert len(session.requests) == 1


# FIX-10: a 429 after begin_send must give the attempt back, not burn it.
def test_rate_limited_send_returns_the_row_to_pending_without_burning_an_attempt():
    store = FakeStore()
    session = FakeSession([
        *catalog_responses(),
        FakeResponse({"error": {"type": "rate_limit_error"}}, 429),
    ])
    telemetry = RecordingTelemetry()
    recovery = processor(store, send_gateway(session), telemetry=telemetry)

    result = recovery.process_once("rate-limited-worker")

    assert result.errors == 1
    assert result.review == 0
    assert result.dead == 0
    assert result.reported == 0
    assert store.release_unsent_calls == 1
    # The row is provably unsent: back to pending, marker cleared, and the
    # attempt the claim consumed is refunded (0 -> 1 -> 0).
    assert store.row["status"] == "pending"
    assert store.row["attempts"] == 0
    assert store.row["outbound_started_at"] is None
    assert store.row["lease_owner"] is None
    assert ("billing.stripe_unavailable", 1, {}) in telemetry.metrics

    # ...and it is immediately claimable again, with the same identifier.
    retry_session = FakeSession([
        *catalog_responses(),
        FakeResponse({"identifier": "brevitas-fee-41"}),
    ])
    retry = processor(store, send_gateway(retry_session))
    assert retry.process_once("retry-worker").reported == 1
    assert store.row["status"] == "reported"
    assert retry_session.requests[-1][2]["data"]["identifier"] == "brevitas-fee-41"


def test_sustained_rate_limiting_never_exhausts_attempts_into_review():
    store = FakeStore()
    store.row["max_attempts"] = 5

    for cycle in range(8):
        session = FakeSession([
            *catalog_responses(),
            FakeResponse({"error": {"type": "rate_limit_error"}}, 429),
        ])
        result = processor(store, send_gateway(session)).process_once(f"worker-{cycle}")
        assert result.errors == 1
        # Before FIX-10 this climbed by one every cycle and the DB sweep at
        # attempts >= max_attempts (202607200006:349-353) parked the fee in
        # `review` around cycle 5.
        assert store.row["attempts"] == 0
        assert store.row["status"] == "pending"
        assert store.row["outbound_started_at"] is None


def test_fenced_out_unsent_release_leaves_the_row_untouched():
    store = FakeStore()
    session = FakeSession([
        *catalog_responses(),
        FakeResponse({"error": {"type": "rate_limit_error"}}, 429),
    ])

    class LostLeaseStore:
        """The lease was reclaimed by another worker before the 429 came back."""

        def __init__(self, inner):
            self.inner = inner
            self.fenced = 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def release_unsent(self, entry_id, owner):
            self.fenced += 1
            return self.inner.release_unsent(entry_id, "someone-else")

    fenced = LostLeaseStore(store)
    result = processor(fenced, send_gateway(session)).process_once("worker")

    assert fenced.fenced == 1
    assert result.errors == 1
    # No lease, no write: the row keeps the pre-FIX-10 fate and waits for the
    # database sweeps rather than being rewritten by a worker that lost it.
    assert store.row["status"] == "sending"
    assert store.row["outbound_started_at"] is not None


def test_unavailable_release_rpc_cannot_kill_the_cycle():
    store = FakeStore()
    session = FakeSession([
        *catalog_responses(),
        FakeResponse({"error": {"type": "rate_limit_error"}}, 429),
    ])

    class UnmigratedStore:
        """A store deployed ahead of migration 202607280011."""

        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def release_unsent(self, entry_id, owner):
            raise requests.HTTPError("404 rpc/release_billing_ledger_unsent")

    telemetry = RecordingTelemetry()
    recovery = processor(UnmigratedStore(store), send_gateway(session), telemetry=telemetry)
    result = recovery.process_once("worker")

    assert result.errors == 1
    assert store.row["status"] == "sending"
    assert ("billing.stripe_unavailable", 1, {}) in telemetry.metrics


def test_rate_limited_catalog_validation_still_releases_the_never_sent_claim():
    store = FakeStore()
    session = FakeSession([FakeResponse({"error": {"type": "rate_limit_error"}}, 429)])
    telemetry = RecordingTelemetry()

    result = processor(store, send_gateway(session), telemetry=telemetry).process_once("worker")

    assert result.errors == 1
    assert store.release_calls == 1
    assert store.release_unsent_calls == 0
    assert store.row["status"] == "pending"
    assert store.row["attempts"] == 0
    assert store.row["outbound_started_at"] is None
    assert all(not url.endswith("/v1/billing/meter_events") for _, url, _ in session.requests)


def test_unsent_release_migration_is_lease_fenced_and_worker_only():
    migration = (
        Path(__file__).resolve().parents[1]
        / "supabase/migrations/202607280011_billing_ledger_unsent_release.sql"
    ).read_text()

    assert "create or replace function public.release_billing_ledger_unsent(" in migration
    assert "security definer" in migration
    # The hardened form, matching the sibling money migrations (202607280008,
    # 202607280010, 202607280013). A bare `= public` leaves pg_temp implicitly
    # searched FIRST, letting a caller who can create a temp relation shadow a
    # name this SECURITY DEFINER body resolves. Pin the strict path so it cannot
    # silently regress to the weaker form.
    assert "set search_path = pg_catalog, public, pg_temp" in migration
    assert "where id = p_entry_id" in migration
    assert "and status = 'sending'" in migration
    assert "and lease_owner = p_owner" in migration
    assert "attempts = greatest(0, attempts - 1)" in migration
    assert "outbound_started_at = null" in migration
    assert (
        "revoke all on function public.release_billing_ledger_unsent(bigint, text)"
        in migration
    )
    assert (
        "grant execute on function public.release_billing_ledger_unsent(bigint, text)\n"
        "    to service_role;" in migration
    )
    # Nothing but the worker role may execute it, and no table grant is widened.
    grants = [line for line in migration.splitlines() if line.startswith("grant ")]
    assert grants == [
        "grant execute on function public.release_billing_ledger_unsent(bigint, text)",
    ]
    assert "to service_role;" in migration
    assert "grant select" not in migration
    assert " to anon" not in migration and " to authenticated" not in migration
    # The soundness argument must travel with the function.
    assert "429" in migration
    assert "ROLLBACK PROCEDURE" in migration


# ---------------------------------------------------------------------------
# Period-settlement ledger.
#
# 202607280006 retired the per-row trigger's only writer, so a promoted
# settlement is the only thing that can produce NEW money — and until now no RPC
# read period_settlement_ledger for sending, so a settlement had no path to
# Stripe at all. The rows below travel the SAME send/reconcile/complete machine
# as billing_ledger; only the store (RPC names) and the Stripe identity
# namespace differ.
# ---------------------------------------------------------------------------

# The caestus dress-rehearsal row: settlement 1000000005 for the closed week,
# whose window also contains legacy per-row fee billing_ledger#5 for the same
# Stripe customer. expected_period_microusd must therefore be the SUM of both,
# or reconcile could never ACCEPT.
SETTLEMENT_ID = 1_000_000_005
SETTLEMENT_FEE = 1_235_092
LEGACY_COMMITTED = 1_235_092


class FakeSettlementStore:
    """public.period_settlement_ledger as the new RPCs expose it.

    Three behaviours differ from FakeStore and each one is load-bearing:

    * claim takes a LEASE and leaves the row 'pending'. 202607280010 refuses any
      exit from 'sending' unless outbound_started_at survives — even for a row
      that never had a marker — so a claim that parked the row in 'sending' with
      a NULL marker would strand it forever.
    * ``reclaimed`` is derived from the outbound marker, NOT from
      status == 'sending', because the 429 path returns a marker-carrying row to
      'pending' and the processor's reconcile-first branch keys on the marker.
    * release_unsent maps to release_period_settlement_claim and PRESERVES the
      marker (clearing it is what the CI guardrail and the send latches forbid).
    """

    ENTRY_KIND = ENTRY_KIND_SETTLEMENT

    def __init__(self, now=NOW, *, status="pending"):
        self.now = now
        self.lock = threading.Lock()
        self.claim_calls = 0
        self.release_calls = 0
        self.release_unsent_calls = 0
        self.health_calls = 0
        self.completed = []
        self.legacy_committed_microusd = LEGACY_COMMITTED
        self.row = {
            "id": SETTLEMENT_ID,
            "billing_owner_id": "1827866c-7df6-4816-abd5-a8824b7e2584",
            "fee_microusd": SETTLEMENT_FEE,
            "stripe_customer_id": "cus_UywBHVTvSfUPdo",
            "attempts": 0,
            "max_attempts": 5,
            "status": status,
            "lease_owner": None,
            "lease_expires_at": None,
            "outbound_started_at": None,
            "reported_at": None,
            "settled_at": None,
            "superseded_at": None,
            # A CLOSED prior week, which is the only kind a settlement exists for.
            "period_start": now - timedelta(days=8),
            "period_end": now - timedelta(days=1),
            "last_error": "",
        }

    def committed_microusd(self):
        """202607280008:562-568's predicate, plus the legacy per-row term."""
        with self.lock:
            return self._committed_microusd()

    def _committed_microusd(self):
        row = self.row
        settled = (
            row["fee_microusd"]
            if row["status"] in ("sending", "reported")
            or row["outbound_started_at"] is not None
            else 0
        )
        return settled + self.legacy_committed_microusd

    def claim_one(self, owner, *, lease_seconds, cap_microusd):
        with self.lock:
            self.claim_calls += 1
            row = self.row
            lease_free = (
                row["lease_owner"] is None
                or row["lease_expires_at"] is None
                or row["lease_expires_at"] < self.now
            )
            expired = (
                row["lease_expires_at"] is not None
                and row["lease_expires_at"] < self.now
            )
            claimable = (
                row["superseded_at"] is None
                and row["attempts"] < row["max_attempts"]
                and (
                    (row["status"] == "pending" and lease_free)
                    # A 'sending' row is only reclaimable once its lease expires.
                    or (row["status"] == "sending" and expired)
                )
            )
            if not claimable:
                return None
            reclaimed = row["outbound_started_at"] is not None
            committed = self._committed_microusd()
            if not reclaimed and committed + row["fee_microusd"] > cap_microusd:
                row["status"] = "capped"
                row["last_error"] = "weekly safety cap reached"
                return None
            # The status is deliberately NOT changed here.
            row["lease_owner"] = owner
            row["lease_expires_at"] = self.now + timedelta(seconds=lease_seconds)
            row["attempts"] += 1
            return BillingEntry(
                id=row["id"],
                user_id=row["billing_owner_id"],
                # period_end itself is provably OUTSIDE reconcile's window once
                # end_time is floored to the minute; five minutes earlier is
                # unambiguously inside [period_start, period_end).
                occurred_at=row["period_end"] - timedelta(minutes=5),
                fee_microusd=row["fee_microusd"],
                stripe_customer_id=row["stripe_customer_id"],
                attempts=row["attempts"],
                reclaimed=reclaimed,
                outbound_started_at=row["outbound_started_at"],
                period_start=row["period_start"],
                period_end=row["period_end"],
                expected_period_microusd=(
                    committed + (0 if reclaimed else row["fee_microusd"])
                ),
                kind=ENTRY_KIND_SETTLEMENT,
            )

    def begin_send(self, entry_id, owner):
        with self.lock:
            row = self.row
            if (
                row["id"] != entry_id
                or row["lease_owner"] != owner
                or row["lease_expires_at"] is None
                or row["lease_expires_at"] <= self.now
                or row["status"] not in ("pending", "sending")
            ):
                return False
            # Status and marker are stamped in ONE update, which is the only
            # transition into 'sending' the send latches permit.
            row["status"] = "sending"
            row["outbound_started_at"] = row["outbound_started_at"] or self.now
            return True

    def renew(self, entry_id, owner, lease_seconds):
        with self.lock:
            row = self.row
            if (
                row["id"] != entry_id
                or row["lease_owner"] != owner
                or row["lease_expires_at"] is None
                or row["lease_expires_at"] <= self.now
                # The heartbeat runs BEFORE begin_send, when the row is still
                # 'pending'. A copied `status = 'sending'` predicate here would
                # lease_lost every settlement before it could ever send.
                or row["status"] not in ("pending", "sending")
            ):
                return False
            row["lease_expires_at"] = self.now + timedelta(seconds=lease_seconds)
            return True

    def complete(self, entry_id, owner, status, error=""):
        with self.lock:
            row = self.row
            if status not in ("reported", "review", "dead"):
                raise AssertionError(f"illegal settlement status {status!r}")
            if row["id"] != entry_id or row["lease_owner"] != owner:
                return False
            if row["status"] != "sending":
                return False
            self.completed.append((status, error))
            row.update(
                status=status,
                last_error=error[:500],
                lease_owner=None,
                lease_expires_at=None,
            )
            if status == "reported":
                row["reported_at"] = row["reported_at"] or self.now
                row["settled_at"] = row["settled_at"] or self.now
            return True

    def release_owner(self, owner):
        with self.lock:
            self.release_calls += 1
            row = self.row
            if (
                row["lease_owner"] != owner
                or row["status"] != "pending"
                or row["outbound_started_at"] is not None
            ):
                return 0
            row.update(
                attempts=max(0, row["attempts"] - 1),
                lease_owner=None,
                lease_expires_at=None,
            )
            return 1

    def release_unsent(self, entry_id, owner):
        """release_period_settlement_claim: the marker is NOT cleared."""
        with self.lock:
            self.release_unsent_calls += 1
            row = self.row
            if (
                row["id"] != entry_id
                or row["status"] != "sending"
                or row["lease_owner"] != owner
            ):
                return False
            row.update(
                status="pending",
                attempts=max(0, row["attempts"] - 1),
                lease_owner=None,
                lease_expires_at=None,
                last_error=(
                    "Stripe rate limited the request; the meter event was not processed"
                ),
            )
            return True

    def check_health(self):
        with self.lock:
            self.health_calls += 1
            row = self.row
            return BillingHealth(
                pending_count=int(row["status"] == "pending"),
                review_count=int(row["status"] == "review"),
                dead_count=int(row["status"] == "dead"),
                stale_sending_count=int(
                    row["status"] == "sending"
                    and row["lease_expires_at"] is not None
                    and row["lease_expires_at"] < self.now
                ),
                # Measured from period_end, so it is a full closed week old the
                # moment the row exists.
                oldest_pending_seconds=(
                    int((self.now - row["period_end"]).total_seconds())
                    if row["status"] == "pending" else 0
                ),
            )

    def expire_lease(self, *, advance=timedelta(minutes=3)):
        with self.lock:
            self.now += advance
            self.row["lease_expires_at"] = self.now - timedelta(seconds=1)


class DrainedSettlementStore(FakeSettlementStore):
    """No promoted settlement is waiting."""

    def claim_one(self, owner, *, lease_seconds, cap_microusd):
        with self.lock:
            self.claim_calls += 1
        return None


def settlement_entry(entry_id=SETTLEMENT_ID, fee_microusd=SETTLEMENT_FEE):
    period_end = NOW - timedelta(days=1)
    return BillingEntry(
        id=entry_id,
        user_id="1827866c-7df6-4816-abd5-a8824b7e2584",
        occurred_at=period_end - timedelta(minutes=5),
        fee_microusd=fee_microusd,
        stripe_customer_id="cus_UywBHVTvSfUPdo",
        attempts=1,
        reclaimed=False,
        outbound_started_at=None,
        period_start=NOW - timedelta(days=8),
        period_end=period_end,
        expected_period_microusd=fee_microusd + LEGACY_COMMITTED,
        kind=ENTRY_KIND_SETTLEMENT,
    )


# S1: the whole point of the lane — a promoted settlement reaches Stripe and
# terminalizes, using the same machine the per-row ledger uses.
def test_settlement_claim_send_reported_happy_path():
    store = FakeSettlementStore()
    stripe = FakeStripe()
    telemetry = RecordingTelemetry()
    recovery = processor(
        FakeStore(), stripe, telemetry=telemetry, settlement_store=store,
    )

    result = recovery.process_once("settlement-worker")

    assert result.claimed == 1
    assert result.reported == 1
    assert result.errors == 0
    assert stripe.send_calls == 1
    assert stripe.accepted_identifiers == {f"brevitas-settlement-{SETTLEMENT_ID}"}
    # The lease left the row 'pending'; only begin_send moved it to 'sending',
    # stamping the marker in the same update.
    assert store.row["status"] == "reported"
    assert store.row["outbound_started_at"] is not None
    assert store.row["reported_at"] is not None
    assert store.row["settled_at"] is not None
    assert store.row["lease_owner"] is None
    assert store.row["attempts"] == 1
    assert store.completed == [("reported", "")]
    # Telemetry is tagged so a settlement can never be read as a per-row fee.
    assert ("billing.entries", 1, {"status": "reported",
                                   "ledger": "settlement"}) in telemetry.metrics


def test_settlement_claim_leases_without_entering_sending_and_heartbeats_while_pending():
    store = FakeSettlementStore()
    entry = store.claim_one("owner-1", lease_seconds=120, cap_microusd=10_000_000)

    assert entry is not None
    assert entry.kind == ENTRY_KIND_SETTLEMENT
    assert entry.reclaimed is False
    assert entry.outbound_started_at is None
    assert entry.attempts == 1
    # A row parked in 'sending' with a NULL marker could never be released.
    assert store.row["status"] == "pending"
    assert store.row["lease_owner"] == "owner-1"
    # occurred_at is inside [period_start, period_end), not AT period_end, which
    # reconcile's minute-flooring would put outside the queried window.
    assert entry.occurred_at == store.row["period_end"] - timedelta(minutes=5)
    assert store.row["period_start"] <= entry.occurred_at < store.row["period_end"]
    # expected must carry the legacy per-row fee sharing the window, or a
    # settlement could never reconcile to ACCEPTED.
    assert entry.expected_period_microusd == SETTLEMENT_FEE + LEGACY_COMMITTED
    # The pre-send heartbeat runs while the row is still 'pending'.
    assert store.renew(entry.id, "owner-1", 120) is True
    assert store.renew(entry.id, "someone-else", 120) is False
    assert store.complete(entry.id, "someone-else", "reported") is False


# S2: the outbound meter POST is a money instruction. Byte-exact, and its Stripe
# identity namespace cannot collide with the per-row ledger's.
def test_settlement_outbound_meter_event_post_is_byte_exact():
    session = FakeSession([
        *catalog_responses(),
        FakeResponse({"identifier": f"brevitas-settlement-{SETTLEMENT_ID}"}),
    ])
    gateway = send_gateway(session)
    entry = settlement_entry()

    gateway.send(entry)

    method, url, kwargs = session.requests[-1]
    assert method == "POST"
    assert url == "https://api.stripe.com/v1/billing/meter_events"
    assert kwargs == {
        "data": {
            "event_name": "brevitas_fee_microusd",
            "identifier": "brevitas-settlement-1000000005",
            "timestamp": str(int(entry.occurred_at.timestamp())),
            "payload[stripe_customer_id]": "cus_UywBHVTvSfUPdo",
            "payload[value]": "1235092",
        },
        "headers": {
            "Idempotency-Key": "brevitas-settlement-meter-1000000005",
            "Stripe-Version": STRIPE_API_VERSION,
        },
        "timeout": (3.0, 10.0),
    }
    assert entry.user_id not in str(kwargs)


def test_settlement_and_ledger_stripe_identities_cannot_collide():
    ledger = replace(settlement_entry(entry_id=5), kind=ENTRY_KIND_LEDGER)
    settlement = settlement_entry(entry_id=5)

    # The four literals.
    assert ledger.stripe_identifier == "brevitas-fee-5"
    assert ledger.idempotency_key == "brevitas-meter-5"
    assert settlement.stripe_identifier == "brevitas-settlement-5"
    assert settlement.idempotency_key == "brevitas-settlement-meter-5"

    # Prefix disjointness, which holds for EVERY id forever: both per-row
    # templates are a fixed prefix plus decimal digits, so a collision would
    # need a decimal string equal to "settlement-..." — impossible. This is
    # deliberately independent of 202607280007's id-space argument (settlement
    # ids start at 1e9), which is only a one-shot apply-time assertion.
    for entry_id in (1, 5, 999, SETTLEMENT_ID, 10**12):
        ledger_id = replace(settlement_entry(entry_id=entry_id), kind=ENTRY_KIND_LEDGER)
        settled = settlement_entry(entry_id=entry_id)
        names = {
            ledger_id.stripe_identifier, ledger_id.idempotency_key,
            settled.stripe_identifier, settled.idempotency_key,
        }
        assert len(names) == 4
        for left in names:
            for right in names:
                assert left == right or not left.startswith(right)
    # Identity is derived from the row id alone, so it is stable across reclaims
    # and re-sends — the property Stripe's deduplication relies on.
    assert replace(settlement, attempts=9, reclaimed=True).stripe_identifier == (
        settlement.stripe_identifier
    )


def test_unknown_entry_kind_can_never_reach_stripe():
    with pytest.raises(ValueError, match="known ledger"):
        replace(settlement_entry(), kind="period")


# S3: a 429 after begin_send. The settlement relief KEEPS the outbound marker
# (202607280010 forbids erasing send evidence, and the CI guardrail forbids a
# settlement function that clears it) — which is safe precisely because the
# cumulative ceiling counts committed money on that marker.
def test_rate_limited_settlement_returns_to_pending_without_burning_an_attempt():
    store = FakeSettlementStore()
    session = FakeSession([
        *catalog_responses(),
        FakeResponse({"error": {"type": "rate_limit_error"}}, 429),
    ])
    telemetry = RecordingTelemetry()
    recovery = processor(
        FakeStore(), send_gateway(session), telemetry=telemetry, settlement_store=store,
    )

    result = recovery.process_once("rate-limited-worker")

    assert result.errors == 1
    assert result.reported == 0
    assert result.review == 0
    assert result.dead == 0
    assert store.release_unsent_calls == 1
    assert store.row["status"] == "pending"
    assert store.row["attempts"] == 0
    assert store.row["lease_owner"] is None
    # The marker survives, so the period stays inside the committed sum and can
    # never be double-billed by a second revision.
    assert store.row["outbound_started_at"] is not None
    assert store.committed_microusd() == SETTLEMENT_FEE + LEGACY_COMMITTED
    assert ("billing.stripe_unavailable", 1,
            {"ledger": "settlement"}) in telemetry.metrics

    # The next claim therefore reports reclaimed=True and reconciles BEFORE
    # re-sending, under the same identifier.
    reclaimed = store.claim_one("retry-worker", lease_seconds=120, cap_microusd=10_000_000)
    assert reclaimed is not None
    assert reclaimed.reclaimed is True
    assert reclaimed.outbound_started_at is not None
    # Already-committed money is not counted twice.
    assert reclaimed.expected_period_microusd == SETTLEMENT_FEE + LEGACY_COMMITTED


def test_sustained_rate_limiting_never_exhausts_settlement_attempts():
    store = FakeSettlementStore()
    # The meter aggregate holds only the legacy per-row fee: the settlement's
    # own event was never ingested, so every reconcile is UNKNOWN and the row
    # re-sends under the same identifier.
    unmatched = {
        "data": [{"id": "s", "meter": "mtr_test", "aggregated_value": LEGACY_COMMITTED}],
        "has_more": False,
    }
    for cycle in range(8):
        reclaimed = store.row["outbound_started_at"] is not None
        session = FakeSession([
            # From the second cycle on the row carries its marker, so the
            # processor reconciles BEFORE re-sending — the documented cost of
            # keeping the send evidence: one wasted reconcile per rate-limited
            # cycle, in exchange for never being able to double-bill the period.
            *catalog_responses(*([unmatched] if reclaimed else [])),
            FakeResponse({"error": {"type": "rate_limit_error"}}, 429),
        ])
        gateway = StripeRestBillingGateway(
            "sk_test_mock", "price_mock", "brevitas_fee_microusd",
            exclusive_meter_writer=True, session=session, now=lambda: NOW,
        )
        result = processor(
            FakeStore(), gateway, settlement_store=store,
        ).process_once(f"worker-{cycle}")
        assert result.errors == 1
        assert result.review == 0
        assert result.reported == 0
        # Before the attempt refund this climbed by one every cycle and the DB
        # sweep at attempts >= max_attempts would park the whole week's money in
        # `review` around cycle 5.
        assert store.row["attempts"] == 0
        assert store.row["status"] == "pending"
        assert store.row["outbound_started_at"] is not None
        assert store.committed_microusd() == SETTLEMENT_FEE + LEGACY_COMMITTED


# S4: ambiguous outcome. The row stays in 'sending' with its marker and is
# reconciled; it is never blind-retried and never terminalized this cycle.
def test_ambiguous_settlement_send_is_reconciled_and_never_double_charged():
    store = FakeSettlementStore()
    stripe = FakeStripe("timeout_after_accept")

    result = processor(
        FakeStore(), stripe, settlement_store=store,
    ).process_once("worker")

    assert result.reported == 1
    assert result.reconciled == 1
    assert result.review == 0
    assert stripe.send_calls == 1
    assert stripe.reconcile_calls == 1
    assert stripe.accepted_identifiers == {f"brevitas-settlement-{SETTLEMENT_ID}"}
    assert store.row["status"] == "reported"


def test_ambiguous_settlement_that_cannot_reconcile_stays_sending_with_its_marker():
    store = FakeSettlementStore()
    stripe = FakeStripe("timeout_after_accept")
    stripe.force_unknown_reconcile = True

    result = processor(
        FakeStore(), stripe, settlement_store=store,
    ).process_once("worker")

    assert result.errors == 1
    assert result.reported == 0
    assert result.review == 0
    assert result.dead == 0
    # Left for lease expiry to drive a later reconcile-then-replay against the
    # same identifier. Only the DB sweeps may escalate it to review.
    assert store.row["status"] == "sending"
    assert store.row["outbound_started_at"] is not None
    assert store.committed_microusd() == SETTLEMENT_FEE + LEGACY_COMMITTED

    # ...and the reclaim reconciles first rather than sending blind.
    store.expire_lease()
    stripe.force_unknown_reconcile = False
    replacement = processor(
        FakeStore(), stripe, now=lambda: store.now, settlement_store=store,
    ).process_once("replacement-worker")

    assert replacement.reconciled == 1
    assert replacement.reported == 1
    assert stripe.send_calls == 1
    assert store.row["status"] == "reported"


# S5: lease fencing. A worker that lost its lease may not write a terminal
# status, and the row keeps its evidence for the winner or the DB sweeps.
def test_settlement_worker_that_lost_its_lease_cannot_complete():
    store = FakeSettlementStore()

    class StolenLeaseStore:
        def __init__(self, inner):
            self.inner = inner
            self.stolen = 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def complete(self, entry_id, owner, status, error=""):
            self.stolen += 1
            with self.inner.lock:
                self.inner.row["lease_owner"] = "another-replica"
            return self.inner.complete(entry_id, owner, status, error)

    stripe = FakeStripe()
    telemetry = RecordingTelemetry()
    fenced = StolenLeaseStore(store)
    result = processor(
        FakeStore(), stripe, telemetry=telemetry, settlement_store=fenced,
    ).process_once("evicted-worker")

    assert fenced.stolen == 1
    assert result.lease_lost == 1
    assert result.errors == 1
    assert result.reported == 0
    assert store.completed == []
    assert store.row["status"] == "sending"
    assert store.row["outbound_started_at"] is not None
    assert ("billing.lease_lost", 1, {"ledger": "settlement"}) in telemetry.metrics


def test_settlement_send_is_fenced_when_begin_send_loses_the_lease():
    store = FakeSettlementStore()
    stripe = FakeStripe()

    class LostLeaseBeforeSend:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def begin_send(self, entry_id, owner):
            return self.inner.begin_send(entry_id, "another-replica")

    result = processor(
        FakeStore(), stripe, settlement_store=LostLeaseBeforeSend(store),
    ).process_once("evicted-worker")

    assert result.lease_lost == 1
    assert stripe.send_calls == 0
    assert store.row["status"] == "pending"
    assert store.row["outbound_started_at"] is None


# S6: ordering. Settlements are the only source of NEW money; the per-row ledger
# is a finite drain-down set, so it must never starve them.
def test_settlements_are_claimed_before_the_legacy_per_row_ledger():
    ledger = FakeStore()
    settlement = FakeSettlementStore()
    stripe = FakeStripe()

    result = processor(
        ledger, stripe, settlement_store=settlement,
    ).process_once("worker")

    assert result.claimed == 1
    assert settlement.claim_calls == 1
    assert ledger.claim_calls == 0
    assert settlement.row["status"] == "reported"
    assert ledger.row["status"] == "pending"
    assert ledger.row["attempts"] == 0
    assert stripe.accepted_identifiers == {f"brevitas-settlement-{SETTLEMENT_ID}"}


def test_idle_settlement_queue_falls_through_to_the_per_row_ledger_in_one_cycle():
    ledger = FakeStore()
    settlement = DrainedSettlementStore()
    stripe = FakeStripe()

    result = processor(
        ledger, stripe, settlement_store=settlement,
    ).process_once("worker")

    assert settlement.claim_calls == 1
    assert ledger.claim_calls == 1
    assert result.claimed == 1
    assert result.reported == 1
    # The per-row path is byte-identical to its pre-settlement behaviour.
    assert ledger.row["status"] == "reported"
    assert stripe.accepted_identifiers == {"brevitas-fee-41"}


def test_settlement_store_is_not_claimed_when_the_processor_is_stopping():
    ledger = FakeStore()
    settlement = FakeSettlementStore()

    result = processor(
        ledger, FakeStripe(), settlement_store=settlement,
    ).process_once("worker", lambda: True)

    assert result.claimed == 0
    assert settlement.claim_calls == 0
    assert ledger.claim_calls == 0


# S7: regression — with no settlement store the per-row path is untouched, and
# its telemetry keeps the exact attributes it emits today.
def test_per_row_ledger_path_is_unchanged_without_a_settlement_store():
    ledger = FakeStore()
    stripe = FakeStripe()
    telemetry = RecordingTelemetry()
    recovery = processor(ledger, stripe, telemetry=telemetry)

    result = recovery.process_once("worker")

    assert result.claimed == 1
    assert result.reported == 1
    assert ledger.row["status"] == "reported"
    assert stripe.accepted_identifiers == {"brevitas-fee-41"}
    assert ("billing.entries", 1, {"status": "reported"}) in telemetry.metrics
    assert all(
        "ledger" not in attributes for _, _, attributes in telemetry.metrics
    )


def test_health_is_sampled_and_alerted_independently_per_ledger():
    ledger = FakeStore()
    settlement = FakeSettlementStore()
    telemetry = RecordingTelemetry()
    recovery = processor(
        ledger, FakeStripe(), telemetry=telemetry, settlement_store=settlement,
    )

    health = recovery.check_health()

    # check_health still returns the per-row ledger's health.
    assert health.pending_count == 1
    assert ("billing.pending_count", 1.0, {}) in telemetry.metrics
    assert ("billing.pending_count", 1.0, {"ledger": "settlement"}) in telemetry.metrics
    assert settlement.health_calls == 1
    # oldest_pending_seconds is a whole day here (measured from period_end) and
    # would page continuously against the 300s per-row threshold. It does not.
    assert settlement.check_health().oldest_pending_seconds == 86_400
    assert not any(name.endswith("_processing_lag") for name, _, _ in telemetry.alerts)

    # The settlement threshold is a separate knob, and its alert has its own
    # name so a stuck settlement can never be masked by a clean billing_ledger.
    recovery.settings = replace(recovery.settings, settlement_lag_alert_seconds=60)
    telemetry.alerts.clear()
    recovery.check_health()
    assert [name for name, _, _ in telemetry.alerts] == ["billing_settlement_processing_lag"]

    settlement.row["status"] = "review"
    telemetry.alerts.clear()
    recovery.check_health()
    assert [name for name, _, _ in telemetry.alerts] == [
        "billing_settlement_entries_require_review",
    ]


def test_shutdown_releases_never_sent_claims_on_both_ledgers():
    ledger = FakeStore()
    settlement = FakeSettlementStore()
    stripe = FakeStripe()
    recovery = processor(ledger, stripe, settlement_store=settlement)
    stop = asyncio.Event()
    stop.set()

    asyncio.run(run_billing_recovery_loop(recovery, stop, owner="stopping-worker"))

    assert ledger.release_calls == 1
    assert settlement.release_calls == 1
    assert stripe.closed is True


# S8: the store surface itself — RPC names, the id parameter, and the entry kind
# the claim stamps. A wrong name here is a silent no-op against Postgres.
class FakeRpcResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeRpcSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []
        self.headers = {}

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, dict(json or {})))
        return FakeRpcResponse(self.payloads.pop(0))

    def close(self):
        pass


SETTLEMENT_CLAIM_ROW = {
    "id": SETTLEMENT_ID,
    "user_id": "1827866c-7df6-4816-abd5-a8824b7e2584",
    "occurred_at": "2026-07-30T17:58:53+00:00",
    "fee_microusd": SETTLEMENT_FEE,
    "stripe_customer_id": "cus_UywBHVTvSfUPdo",
    "attempts": 1,
    "reclaimed": False,
    "outbound_started_at": None,
    "period_start": "2026-07-23T18:03:53+00:00",
    "period_end": "2026-07-30T18:03:53+00:00",
    "expected_period_microusd": 2_470_184,
}


def test_settlement_store_calls_the_settlement_rpcs_with_the_settlement_id_param():
    session = FakeRpcSession([[SETTLEMENT_CLAIM_ROW], True, True, True, 1, True,
                             [{"pending_count": 2}]])
    store = SupabaseSettlementStore(
        "https://example.supabase.co", "service-role", session=session,
    )

    entry = store.claim_one("owner", lease_seconds=120, cap_microusd=10_000_000)
    assert entry is not None
    # The claim's column list is byte-identical to the per-row claim, so
    # BillingEntry.from_row parses it unchanged; only the kind differs.
    assert entry.kind == ENTRY_KIND_SETTLEMENT
    assert entry.stripe_identifier == "brevitas-settlement-1000000005"
    assert entry.idempotency_key == "brevitas-settlement-meter-1000000005"
    assert entry.expected_period_microusd == 2_470_184

    assert store.begin_send(SETTLEMENT_ID, "owner") is True
    assert store.renew(SETTLEMENT_ID, "owner", 120) is True
    assert store.complete(SETTLEMENT_ID, "owner", "reported") is True
    assert store.release_owner("owner") == 1
    assert store.release_unsent(SETTLEMENT_ID, "owner") is True
    assert store.check_health().pending_count == 2

    functions = [url.rsplit("/", 1)[-1] for url, _ in session.calls]
    assert functions == [
        "claim_period_settlement_entries",
        "mark_period_settlement_outbound_started",
        "renew_period_settlement_lease",
        "complete_period_settlement_entry",
        "release_period_settlement_leases",
        # NOT release_period_settlement_unsent: the CI guardrail fails the build
        # if a settlement function that clears outbound_started_at exists, and
        # this one deliberately does not clear it.
        "release_period_settlement_claim",
        "period_settlement_recovery_health",
    ]
    assert "release_period_settlement_unsent" not in set(
        SupabaseSettlementStore.RPCS.values()
    )
    for _, payload in session.calls:
        assert "p_entry_id" not in payload
    assert session.calls[1][1] == {"p_settlement_id": SETTLEMENT_ID, "p_owner": "owner"}
    assert session.calls[0][1] == {
        "p_owner": "owner",
        "p_lease_seconds": 120,
        "p_limit": 1,
        "p_cap_microusd": 10_000_000,
    }


def test_per_row_store_rpc_names_and_parameters_are_unchanged():
    session = FakeRpcSession([
        [{**SETTLEMENT_CLAIM_ROW, "id": 5, "expected_period_microusd": SETTLEMENT_FEE}],
        True, True, True, 1, True, [{"pending_count": 1}],
    ])
    store = SupabaseBillingStore(
        "https://example.supabase.co", "service-role", session=session,
    )

    entry = store.claim_one("owner", lease_seconds=120, cap_microusd=10_000_000)
    assert entry is not None
    assert entry.kind == ENTRY_KIND_LEDGER
    assert entry.stripe_identifier == "brevitas-fee-5"
    assert entry.idempotency_key == "brevitas-meter-5"

    store.begin_send(5, "owner")
    store.renew(5, "owner", 120)
    store.complete(5, "owner", "reported")
    store.release_owner("owner")
    store.release_unsent(5, "owner")
    store.check_health()

    functions = [url.rsplit("/", 1)[-1] for url, _ in session.calls]
    assert functions == [
        "claim_billing_ledger_entries",
        "mark_billing_outbound_started",
        "renew_billing_ledger_lease",
        "complete_billing_ledger_entry",
        "release_billing_ledger_leases",
        "release_billing_ledger_unsent",
        "billing_recovery_health",
    ]
    assert session.calls[1][1] == {"p_entry_id": 5, "p_owner": "owner"}
    for _, payload in session.calls:
        assert "p_settlement_id" not in payload


def test_settlement_ledger_is_off_by_default_and_needs_an_explicit_gate(monkeypatch):
    for value in (None, "", "false", "FALSE", "1", "yes", " true ", "TRUE"):
        if value is None:
            monkeypatch.delenv("BREVITAS_BILLING_SETTLEMENT_ENABLED", raising=False)
        else:
            monkeypatch.setenv("BREVITAS_BILLING_SETTLEMENT_ENABLED", value)
        assert billing_settlement_recovery_is_enabled() is False
    monkeypatch.setenv("BREVITAS_BILLING_SETTLEMENT_ENABLED", "true")
    assert billing_settlement_recovery_is_enabled() is True


def test_env_built_processor_only_wires_the_settlement_store_when_gated_on(monkeypatch):
    for name, value in {
        "BREVITAS_BILLING_ENABLED": "true",
        "STRIPE_SECRET_KEY": "sk_test_mock",
        "STRIPE_PRICE_ID": "price_weekly",
        "STRIPE_METER_EVENT_NAME": "brevitas_fee_microusd",
        "BREVITAS_BILLING_WEEKLY_CAP_USD": "100",
        "SUPABASE_SERVICE_ROLE_KEY": "service-role",
        "SUPABASE_URL": "https://example.supabase.co",
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "BREVITAS_BILLING_LEASE_SECONDS", "BREVITAS_BILLING_POLL_SECONDS",
        "BREVITAS_BILLING_HTTP_TIMEOUT_SECONDS", "BREVITAS_BILLING_LAG_ALERT_SECONDS",
        "BREVITAS_BILLING_SETTLEMENT_LAG_ALERT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    # Default OFF: a database that stops short of the settlement migrations must
    # be a no-op, not a stream of RPC-not-found errors on every cycle.
    monkeypatch.delenv("BREVITAS_BILLING_SETTLEMENT_ENABLED", raising=False)
    disabled = build_billing_recovery_processor_from_env()
    assert disabled.settlement_store is None
    assert isinstance(disabled.store, SupabaseBillingStore)
    assert disabled.settings.settlement_lag_alert_seconds == 172_800

    monkeypatch.setenv("BREVITAS_BILLING_SETTLEMENT_ENABLED", "true")
    monkeypatch.setenv("BREVITAS_BILLING_SETTLEMENT_LAG_ALERT_SECONDS", "3600")
    enabled = build_billing_recovery_processor_from_env()
    assert isinstance(enabled.settlement_store, SupabaseSettlementStore)
    assert type(enabled.store) is SupabaseBillingStore
    assert enabled.settings.settlement_lag_alert_seconds == 3600
    for store in (enabled.store, enabled.settlement_store):
        store.close()
    disabled.store.close()


def test_ledger_alert_names_are_unchanged_and_the_settlement_names_are_distinct():
    # The per-row names are load-bearing beyond this module: dashboards and
    # tests/stripe_billing_config.test.mjs both key on them.
    assert tuple(LEDGER_ALERT_NAMES) == (
        "billing_processing_lag",
        "billing_entries_require_review",
        "billing_entries_dead",
        "billing_stale_leases",
    )
    assert tuple(SETTLEMENT_ALERT_NAMES) == (
        "billing_settlement_processing_lag",
        "billing_settlement_entries_require_review",
        "billing_settlement_entries_dead",
        "billing_settlement_stale_leases",
    )
    assert not set(LEDGER_ALERT_NAMES) & set(SETTLEMENT_ALERT_NAMES)

    ledger = FakeStore()
    ledger.row["status"] = "review"
    telemetry = RecordingTelemetry()
    processor(
        ledger, FakeStripe(), telemetry=telemetry,
        settlement_store=FakeSettlementStore(),
    ).check_health()

    assert [name for name, _, _ in telemetry.alerts] == [
        "billing_entries_require_review",
    ]
