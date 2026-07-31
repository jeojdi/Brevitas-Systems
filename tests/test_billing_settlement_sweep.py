"""Contract for the settlement caller (api/billing_settlement_sweep.py).

This file has two halves and they are load-bearing in different ways.

1. BEHAVIOUR, against FakeSettlementDatabase — a deliberately literal
   transcription of the two RPCs' decision tables (the enumerator at
   202607280013:1190-1247 and the live writer body at 202607280031:1247-1745),
   including the per-organization advisory lock the writer takes at
   202607280031:1311. A transcription is only worth as much as its fidelity, so:

2. JOINS, against the migration sources themselves — the tests at the bottom
   fail if the writer grows an outcome this module does not classify, if the
   advisory lock this module's idempotence argument depends on disappears, if
   the promoter's name ever appears in the module, or if p_allow_revision stops
   being a hardcoded False on the wire. That is the same "missing join" pattern
   tests/test_billing_rpc_names_exist.py exists for, and it is what keeps the
   simulator honest.

The real database behaviour of settle_billing_period itself is proven separately
and for real by scripts/ci/migration-settlement-writer-assertions.sql (§6
idempotence and the one-row/22,500,000-microUSD proof, §12 sweep enumeration,
and the halting-condition table at :475). Nothing here restates that; this file
is about the CALLER.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from api.billing_settlement_sweep import (
    ALLOW_REVISION,
    KNOWN_OUTCOMES,
    SWEEP_ENABLED_ENV,
    BillingSettlementSweep,
    SettlementSweepResult,
    SupabaseSettlementSweepStore,
    billing_settlement_sweep_is_configured,
    billing_settlement_sweep_is_enabled,
    build_settlement_sweep_from_env,
    run_billing_settlement_sweep_loop,
)

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "supabase" / "migrations"
WRITER_MIGRATION = MIGRATIONS / "202607280013_period_settlement_writer.sql"
LIVE_WRITER_MIGRATION = MIGRATIONS / "202607280031_observed_price_cache_fee_basis.sql"
SWEEP_MODULE = REPO / "api" / "billing_settlement_sweep.py"
WORKER_MODULE = REPO / "api" / "worker.py"

WEEK = timedelta(seconds=604800)
NOW = datetime(2026, 8, 14, 6, 15, 13, tzinfo=timezone.utc)
# The anchor Stripe reports for the account: the week that is OPEN right now.
ANCHOR_START = NOW - timedelta(hours=1)
ANCHOR_END = ANCHOR_START + WEEK


# ---------------------------------------------------------------------------
# The simulator.
# ---------------------------------------------------------------------------
@dataclass
class FakeAccount:
    organization_id: str
    attested: bool = True
    subscription_status: str = "active"
    billing_started_at: datetime = ANCHOR_START - 4 * WEEK
    current_period_start: datetime = ANCHOR_START
    current_period_end: datetime = ANCHOR_END
    # 100 USD verified savings, 10 USD warm spend -> 90 * 0.25 * 1e6, the same
    # arithmetic migration-settlement-writer-assertions.sql uses.
    verified_savings_usd: float = 100.0
    warm_spend_usd: float = 10.0


class FakeSettlementDatabase:
    """Transcription of billing_periods_awaiting_settlement + settle_billing_period."""

    MAX_FEE_SHARE = 0.25

    def __init__(self, now: datetime = NOW):
        self._now = now
        self.accounts: dict[str, FakeAccount] = {}
        self.ledger: list[dict] = []
        self._next_id = 1_000_000_001
        self._lock_table = threading.Lock()
        self._org_locks: dict[str, threading.Lock] = {}
        self.settle_calls: list[dict] = []
        self.enumerate_calls = 0
        # Two hooks that together force the exact two-replica interleaving in
        # which a missing fence WOULD double-draft. The barrier (taken BEFORE
        # the advisory lock, as PostgREST would) puts both replicas at the
        # writer's door at once; settle_delay then holds the winner inside the
        # read-then-insert window long enough for the loser to read stale state
        # if — and only if — nothing is serialising them. Without both, the GIL
        # happens to run one settle() to completion and the race test passes
        # even with the lock removed, which is a test that proves nothing.
        self.settle_barrier: threading.Barrier | None = None
        self.settle_delay = 0.0

    # -- helpers ---------------------------------------------------------
    def add(self, account: FakeAccount) -> FakeAccount:
        self.accounts[account.organization_id] = account
        return account

    def _org_lock(self, organization_id: str) -> threading.Lock:
        # pg_advisory_xact_lock(hashtextextended(organization_id::text, 0)).
        with self._lock_table:
            return self._org_locks.setdefault(organization_id, threading.Lock())

    @staticmethod
    def _period_for(anchor: datetime, account: FakeAccount) -> tuple[datetime, datetime]:
        offset = math.floor(
            (anchor - account.current_period_start).total_seconds() / 604800
        )
        start = account.current_period_start + offset * WEEK
        return start, start + WEEK

    def _live_row(self, organization_id: str, period_start: datetime) -> dict | None:
        for row in self.ledger:
            if (row["organization_id"] == organization_id
                    and row["period_start"] == period_start
                    and row["superseded_at"] is None
                    and row["status"] != "void"):
                return row
        return None

    def _highest_row(self, organization_id: str, period_start: datetime) -> dict | None:
        rows = [
            row for row in self.ledger
            if row["organization_id"] == organization_id
            and row["period_start"] == period_start
        ]
        return max(rows, key=lambda row: row["revision"]) if rows else None

    def drafts(self) -> list[dict]:
        return [row for row in self.ledger if row["status"] == "draft"]

    # -- public.billing_periods_awaiting_settlement(integer) -------------
    def awaiting_periods(self, limit: int) -> list[dict]:
        self.enumerate_calls += 1
        rows: list[dict] = []
        for account in self.accounts.values():
            if account.subscription_status not in ("active", "trialing"):
                continue
            if account.billing_started_at is None:
                continue
            if account.current_period_end - account.current_period_start != WEEK:
                continue
            for offset in range(1, 5):
                start, end = self._period_for(
                    account.current_period_start - offset * WEEK, account,
                )
                if start < account.billing_started_at:
                    continue
                if end > self._now:
                    continue
                if self._live_row(account.organization_id, start) is not None:
                    continue
                rows.append({
                    "organization_id": account.organization_id,
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                })
        rows.sort(key=lambda row: (row["organization_id"], row["period_start"]))
        return rows[:limit]

    # -- public.settle_billing_period(uuid,timestamptz,text,boolean) -----
    def settle(
        self,
        organization_id: str,
        period_anchor: str,
        computed_by: str,
        allow_revision: bool = False,
    ) -> dict:
        self.settle_calls.append({
            "organization_id": organization_id,
            "period_anchor": period_anchor,
            "computed_by": computed_by,
            "allow_revision": allow_revision,
        })
        # Step 0: argument errors RAISE rather than returning an outcome.
        if not organization_id or not period_anchor or not computed_by.strip():
            raise ValueError("settle_billing_period argument error")
        anchor = datetime.fromisoformat(period_anchor)
        if self.settle_barrier is not None:
            self.settle_barrier.wait(timeout=10)

        # Step 1: serialise on the organization.
        with self._org_lock(organization_id):
            account = self.accounts.get(organization_id)
            if account is None:
                return {"ok": False, "outcome": "blocked", "code": "no_billing_account"}
            if account.subscription_status not in ("active", "trialing"):
                return {"ok": False, "outcome": "blocked",
                        "code": "subscription_inactive"}
            if account.billing_started_at is None:
                return {"ok": False, "outcome": "blocked",
                        "code": "enrollment_incomplete"}

            start, end = self._period_for(anchor, account)
            # Step 3: an open period is never settleable, whoever asks.
            if end > self._now:
                return {"ok": False, "outcome": "blocked", "code": "period_not_closed",
                        "period_start": start.isoformat(), "period_end": end.isoformat()}
            if start < account.billing_started_at:
                return {"ok": False, "outcome": "blocked",
                        "code": "period_precedes_enrollment"}

            previous = self._highest_row(organization_id, start)
            fee = int(
                max(account.verified_savings_usd - account.warm_spend_usd, 0)
                * self.MAX_FEE_SHARE * 1_000_000
            )

            # Step 8: idempotence, before the guard.
            live = self._live_row(organization_id, start)
            if live is not None and live["fee_microusd"] == fee:
                return {"ok": True, "outcome": "unchanged", "code": "unchanged",
                        "settlement_id": live["id"], "revision": live["revision"],
                        "settlement_status": live["status"],
                        "fee_microusd": live["fee_microusd"]}

            # Step 10: an automated caller never revises.
            if previous is not None and not allow_revision:
                return {"ok": False, "outcome": "blocked",
                        "code": "period_already_settled",
                        "settlement_id": previous["id"],
                        "recomputed_fee_microusd": fee}

            # Step 11: THE GATE. assert_billing_period_settlement_allowed runs
            # the attestation check first (202607280009:281) and every
            # 202607280008 condition after it; the writer converts the raise
            # into outcome='halted' with the halting_condition= tag.
            if not account.attested:
                return {
                    "ok": False, "outcome": "halted",
                    "code": "unattested_billing_arrangement",
                    "halting_condition": "unattested_billing_arrangement",
                    "period_start": start.isoformat(), "period_end": end.isoformat(),
                    "message": "halting_condition=unattested_billing_arrangement: "
                               "organization billing arrangement is not attested",
                }

            # The read-then-insert window. Held open only when a test asks.
            if self.settle_delay:
                time.sleep(self.settle_delay)

            row = {
                "id": self._next_id,
                "organization_id": organization_id,
                "period_start": start,
                "period_end": end,
                "fee_microusd": fee,
                "status": "draft",
                "revision": (previous["revision"] + 1) if previous else 1,
                "superseded_at": None,
                "computed_by": computed_by,
            }
            self._next_id += 1
            if previous is not None and previous["superseded_at"] is None:
                previous["superseded_at"] = self._now
            self.ledger.append(row)
            return {"ok": True, "outcome": "settled", "code": "settled",
                    "settlement_id": row["id"], "revision": row["revision"],
                    "settlement_status": "draft", "fee_microusd": fee}


class FakeSweepStore:
    """Transport stand-in. Passes ALLOW_REVISION through exactly as the real
    store does; the real store's wire payload is asserted separately."""

    def __init__(self, database: FakeSettlementDatabase, candidates=None):
        self.database = database
        self.candidates = candidates
        self.closed = False

    def awaiting_periods(self, limit):
        if self.candidates is not None:
            self.database.enumerate_calls += 1
            return list(self.candidates)
        return self.database.awaiting_periods(limit)

    def settle(self, organization_id, period_anchor, computed_by):
        return self.database.settle(
            organization_id, period_anchor, computed_by,
            allow_revision=ALLOW_REVISION,
        )

    def close(self):
        self.closed = True


def sweep(database, *, owner="replica-a", candidates=None):
    return BillingSettlementSweep(
        FakeSweepStore(database, candidates), owner=owner, interval_seconds=1.0,
    )


def logged_events(caplog) -> list[dict]:
    events = []
    for record in caplog.records:
        try:
            events.append(json.loads(record.getMessage()))
        except (ValueError, TypeError):
            continue
    return events


# ---------------------------------------------------------------------------
# 1. It drafts for a closed period on an attested organization.
# ---------------------------------------------------------------------------
def test_closed_period_on_an_attested_org_is_drafted_once(caplog):
    database = FakeSettlementDatabase()
    org = database.add(FakeAccount("11111111-0000-4000-8000-000000000001"))
    offered = database.awaiting_periods(50)
    caplog.set_level(logging.INFO, logger="brevitas.billing_settlement_sweep")

    result = sweep(database).run_once()

    assert result == SettlementSweepResult(
        enumerated=4, drafted=4, unchanged=0, blocked=0, halted=0, errors=0,
    ), "four closed weeks are inside billing_started_at and none was settled"
    assert result.healthy
    assert len(database.drafts()) == 4
    # The writer decides the money; the caller passes no fee and gets 22.5 USD
    # of microUSD per closed week out of the same 100/10/0.25 arithmetic.
    assert {row["fee_microusd"] for row in database.ledger} == {22_500_000}
    assert {row["status"] for row in database.ledger} == {"draft"}, (
        "a sweep may only produce drafts; 'pending' is a human act"
    )
    assert all(
        row["computed_by"].startswith("sweep:replica-a")
        for row in database.ledger
    )
    # Every candidate was settled at ITS OWN period_start — the caller does no
    # date arithmetic and never substitutes now() for the enumerated anchor.
    assert {call["period_anchor"] for call in database.settle_calls} == {
        candidate["period_start"] for candidate in offered
    }
    drafted = [e for e in logged_events(caplog)
               if e["event"] == "billing_settlement_sweep_drafted"]
    assert len(drafted) == 4
    assert all(e["settlement_status"] == "draft" for e in drafted)
    assert org.organization_id in {e["organization_id"] for e in drafted}


def test_a_converged_sweep_reports_nothing_to_do_on_the_next_cycle():
    """The enumerator drops a settled period, so an hourly sweep converges."""
    database = FakeSettlementDatabase()
    database.add(FakeAccount("11111111-0000-4000-8000-000000000001"))
    worker = sweep(database)

    first = worker.run_once()
    second = worker.run_once()

    assert first.drafted == 4
    assert second == SettlementSweepResult(), "a converged sweep must be a pure no-op"
    assert len(database.ledger) == 4


# ---------------------------------------------------------------------------
# 2. It does NOT draft for an unattested organization, and the halt is logged.
# ---------------------------------------------------------------------------
def test_unattested_org_halts_and_writes_nothing(caplog):
    database = FakeSettlementDatabase()
    database.add(FakeAccount(
        "22222222-0000-4000-8000-000000000002", attested=False,
    ))
    caplog.set_level(logging.INFO, logger="brevitas.billing_settlement_sweep")

    result = sweep(database).run_once()

    assert result.drafted == 0
    assert database.ledger == [], "an unattested organization must not be drafted"
    assert result.halted == 4
    # A halt is a normal outcome. It must not poison the cycle's health, or an
    # unattested customer would make the worker look broken forever.
    assert result.errors == 0
    assert result.healthy

    halts = [e for e in logged_events(caplog)
             if e["event"] == "billing_settlement_sweep_halted"]
    assert len(halts) == 4
    assert {e["halting_condition"] for e in halts} == {"unattested_billing_arrangement"}
    assert all("halting_condition=" in e["message"] for e in halts)
    assert all(
        record.levelno >= logging.WARNING
        for record in caplog.records
        if "billing_settlement_sweep_halted" in record.getMessage()
    )


def test_a_halt_is_not_retried_within_the_cycle():
    database = FakeSettlementDatabase()
    database.add(FakeAccount(
        "22222222-0000-4000-8000-000000000002", attested=False,
    ))

    result = sweep(database).run_once()

    assert result.halted == 4
    assert len(database.settle_calls) == 4, (
        "a halt is deterministic; re-calling the same guard is pure noise"
    )


def test_the_sweep_keeps_going_after_one_org_halts():
    """One customer's halting condition must not stop everyone else's drafts."""
    database = FakeSettlementDatabase()
    database.add(FakeAccount("22222222-0000-4000-8000-000000000002", attested=False))
    database.add(FakeAccount("33333333-0000-4000-8000-000000000003", attested=True))

    result = sweep(database).run_once()

    assert result.halted == 4
    assert result.drafted == 4
    assert {row["organization_id"] for row in database.ledger} == {
        "33333333-0000-4000-8000-000000000003",
    }


# ---------------------------------------------------------------------------
# 3. It does NOT draft twice — including under a genuine two-replica race.
# ---------------------------------------------------------------------------
def test_two_replicas_racing_the_same_period_produce_exactly_one_draft():
    database = FakeSettlementDatabase()
    org = "44444444-0000-4000-8000-000000000004"
    # One settleable week, so the race is unambiguous.
    database.add(FakeAccount(org, billing_started_at=ANCHOR_START - WEEK))
    # Both replicas enumerate, then both arrive at the writer together, and the
    # winner sits in the read-then-insert window while the loser tries to read.
    database.settle_barrier = threading.Barrier(2)
    database.settle_delay = 0.05

    replicas = [sweep(database, owner="replica-a"), sweep(database, owner="replica-b")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda worker: worker.run_once(), replicas))

    assert len(database.settle_calls) == 2, "the race really happened"
    assert len(database.ledger) == 1, (
        "two replicas drafted the same period twice; the writer's advisory lock "
        "is the only fence and it did not hold"
    )
    assert sum(r.drafted for r in results) == 1
    # The loser is a benign outcome, never an error: it saw the winner's row.
    assert sum(r.unchanged + r.blocked for r in results) == 1
    assert sum(r.errors for r in results) == 0
    assert all(r.healthy for r in results)


def test_a_second_sequential_pass_never_appends_a_revision():
    database = FakeSettlementDatabase()
    org = "44444444-0000-4000-8000-000000000004"
    database.add(FakeAccount(org, billing_started_at=ANCHOR_START - WEEK))
    first = sweep(database, owner="replica-a").run_once()
    assert first.drafted == 1
    settled = database.ledger[0]

    # Late evidence arrives, so the recomputed fee moves and the writer can no
    # longer answer 'unchanged'. An operator may revise; a sweep may not.
    database.accounts[org].verified_savings_usd = 200.0
    candidate = {
        "organization_id": org,
        "period_start": settled["period_start"].isoformat(),
        "period_end": settled["period_end"].isoformat(),
    }
    second = sweep(
        database, owner="replica-b", candidates=[candidate],
    ).run_once()

    assert second.blocked == 1 and second.drafted == 0 and second.errors == 0
    assert len(database.ledger) == 1, "the sweep appended a revision on its own authority"
    assert database.ledger[0]["computed_by"].startswith("sweep:replica-a")
    assert all(call["allow_revision"] is False for call in database.settle_calls)


def test_a_duplicated_candidate_is_settled_once_per_cycle():
    database = FakeSettlementDatabase()
    org = "44444444-0000-4000-8000-000000000004"
    database.add(FakeAccount(org, billing_started_at=ANCHOR_START - WEEK))
    start = ANCHOR_START - WEEK
    candidate = {
        "organization_id": org,
        "period_start": start.isoformat(),
        "period_end": ANCHOR_START.isoformat(),
    }

    result = sweep(database, candidates=[candidate, dict(candidate)]).run_once()

    assert result.enumerated == 1
    assert len(database.settle_calls) == 1
    assert len(database.ledger) == 1


# ---------------------------------------------------------------------------
# 4. It does NOT draft a period that is still open.
# ---------------------------------------------------------------------------
def test_the_open_period_is_never_enumerated():
    database = FakeSettlementDatabase()
    org = "55555555-0000-4000-8000-000000000005"
    database.add(FakeAccount(org))

    offered = database.awaiting_periods(200)

    assert offered, "the fixture must offer something, or this proves nothing"
    assert all(
        datetime.fromisoformat(row["period_end"]) <= NOW for row in offered
    )
    assert ANCHOR_START.isoformat() not in {row["period_start"] for row in offered}


def test_an_open_period_forced_at_the_writer_is_refused_and_writes_nothing():
    """Defence in depth: even if enumeration regressed, the writer refuses."""
    database = FakeSettlementDatabase()
    org = "55555555-0000-4000-8000-000000000005"
    database.add(FakeAccount(org))
    open_candidate = {
        "organization_id": org,
        "period_start": ANCHOR_START.isoformat(),
        "period_end": ANCHOR_END.isoformat(),
    }

    result = sweep(database, candidates=[open_candidate]).run_once()

    assert result.drafted == 0
    assert result.blocked == 1
    assert result.errors == 0
    assert database.ledger == [], "the still-open week was drafted"


# ---------------------------------------------------------------------------
# 5. Failure handling: transport errors are errors, outcomes are not.
# ---------------------------------------------------------------------------
def test_enumeration_failure_is_one_error_and_no_settle_call(caplog):
    class Broken(FakeSweepStore):
        def awaiting_periods(self, limit):
            raise ConnectionError("supabase unreachable")

    database = FakeSettlementDatabase()
    caplog.set_level(logging.INFO, logger="brevitas.billing_settlement_sweep")
    worker = BillingSettlementSweep(Broken(database), owner="replica-a")

    result = worker.run_once()

    assert result == SettlementSweepResult(errors=1)
    assert database.settle_calls == []
    assert any(
        e["event"] == "billing_settlement_sweep_enumeration_failed"
        and e["error_type"] == "ConnectionError"
        for e in logged_events(caplog)
    )


def test_one_failing_candidate_does_not_stop_the_rest():
    database = FakeSettlementDatabase()
    good = "66666666-0000-4000-8000-000000000006"
    database.add(FakeAccount(good, billing_started_at=ANCHOR_START - WEEK))

    class FlakyStore(FakeSweepStore):
        def settle(self, organization_id, period_anchor, computed_by):
            if organization_id == "bad":
                raise TimeoutError("rpc timed out")
            return super().settle(organization_id, period_anchor, computed_by)

    start = ANCHOR_START - WEEK
    candidates = [
        {"organization_id": "bad", "period_start": start.isoformat(),
         "period_end": ANCHOR_START.isoformat()},
        {"organization_id": good, "period_start": start.isoformat(),
         "period_end": ANCHOR_START.isoformat()},
    ]
    worker = BillingSettlementSweep(
        FlakyStore(database, candidates), owner="replica-a",
    )

    result = worker.run_once()

    assert result.errors == 1
    assert result.drafted == 1
    assert len(database.ledger) == 1


def test_an_unexpected_outcome_is_an_error_not_a_silent_draft(caplog):
    class RevisingStore(FakeSweepStore):
        def settle(self, organization_id, period_anchor, computed_by):
            return {"ok": True, "outcome": "revised", "code": "revised",
                    "settlement_id": 7}

    database = FakeSettlementDatabase()
    caplog.set_level(logging.INFO, logger="brevitas.billing_settlement_sweep")
    candidates = [{
        "organization_id": "77777777-0000-4000-8000-000000000007",
        "period_start": (ANCHOR_START - WEEK).isoformat(),
        "period_end": ANCHOR_START.isoformat(),
    }]
    worker = BillingSettlementSweep(
        RevisingStore(database, candidates), owner="replica-a",
    )

    result = worker.run_once()

    assert result.errors == 1 and result.drafted == 0
    assert any(
        e["event"] == "billing_settlement_sweep_unexpected_outcome"
        for e in logged_events(caplog)
    )


def test_a_malformed_candidate_is_an_error_and_never_reaches_the_writer():
    database = FakeSettlementDatabase()
    candidates = [{"organization_id": "", "period_start": ""}, "not-a-row"]
    worker = BillingSettlementSweep(
        FakeSweepStore(database, candidates), owner="replica-a",
    )

    result = worker.run_once()

    assert result.errors == 2
    assert database.settle_calls == []


def test_should_stop_ends_the_cycle_between_candidates():
    database = FakeSettlementDatabase()
    database.add(FakeAccount("88888888-0000-4000-8000-000000000008"))
    worker = sweep(database)

    result = worker.run_once(should_stop=lambda: True)

    assert result == SettlementSweepResult()
    assert database.settle_calls == []


# ---------------------------------------------------------------------------
# 6. The flag. OFF by default, exact-"true" only.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value", [None, "", "1", "0", "yes", "True", "TRUE", " true", "true ", "false"],
)
def test_the_sweep_is_off_for_every_value_that_is_not_exactly_true(monkeypatch, value):
    monkeypatch.delenv(SWEEP_ENABLED_ENV, raising=False)
    if value is not None:
        monkeypatch.setenv(SWEEP_ENABLED_ENV, value)
    assert billing_settlement_sweep_is_enabled() is False
    assert billing_settlement_sweep_is_configured() is False
    with pytest.raises(RuntimeError):
        build_settlement_sweep_from_env(owner="replica-a")


def test_exactly_true_arms_it_but_still_needs_service_role_credentials(monkeypatch):
    monkeypatch.setenv(SWEEP_ENABLED_ENV, "true")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_SUPABASE_URL", raising=False)

    assert billing_settlement_sweep_is_enabled() is True
    assert billing_settlement_sweep_is_configured() is False
    with pytest.raises(RuntimeError):
        build_settlement_sweep_from_env(owner="replica-a")

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-key")
    assert billing_settlement_sweep_is_configured() is True
    built = build_settlement_sweep_from_env(owner="replica-a")
    try:
        assert isinstance(built.store, SupabaseSettlementSweepStore)
        assert built.computed_by == "sweep:replica-a"
    finally:
        built.close()


def test_the_worker_only_starts_the_sweep_behind_the_flag_and_a_billing_role():
    source = WORKER_MODULE.read_text()
    assert 'name="worker-settlement-sweep"' in source, (
        "the sweep is implemented but no worker starts it"
    )
    body = source.split("async def settlement_sweep(", 1)[1].split("\ndef ", 1)[0]
    assert "billing_settlement_sweep_is_enabled()" in body
    assert "billing_settlement_sweep_is_configured()" in body
    assert '_BILLING_ROLE == "nonbilling"' in body


def test_the_loop_stops_promptly_and_closes_its_store():
    database = FakeSettlementDatabase()
    database.add(FakeAccount("99999999-0000-4000-8000-000000000009"))
    worker = sweep(database)

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_billing_settlement_sweep_loop(worker, stop, interval_seconds=3600),
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())

    assert database.enumerate_calls >= 1
    assert worker.store.closed is True


def test_a_cycle_that_raises_cannot_kill_the_loop(caplog):
    class Exploding:
        def __init__(self):
            self.calls = 0

        def run_once(self, should_stop=lambda: False):
            self.calls += 1
            raise MemoryError("boom")

        def close(self):
            self.closed = True

    exploding = Exploding()
    exploding.interval_seconds = 0.01
    caplog.set_level(logging.INFO, logger="brevitas.billing_settlement_sweep")

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_billing_settlement_sweep_loop(exploding, stop, interval_seconds=0.01),
        )
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())

    assert exploding.calls >= 2, "the loop stopped after the first failure"
    assert any(
        e["event"] == "billing_settlement_sweep_cycle_failed"
        for e in logged_events(caplog)
    )


# ---------------------------------------------------------------------------
# 7. The wire payload: what the real store actually sends.
# ---------------------------------------------------------------------------
class RecordingResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class RecordingSession:
    def __init__(self, *payloads):
        self.headers: dict[str, str] = {}
        self.payloads = list(payloads)
        self.posts: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, dict(json or {})))
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return RecordingResponse(payload)

    def close(self):
        self.closed = True


def test_the_real_store_sends_allow_revision_false_and_the_two_expected_rpcs():
    session = RecordingSession(
        [], {"ok": True, "outcome": "settled", "code": "settled"},
    )
    store = SupabaseSettlementSweepStore(
        "https://example.supabase.co", "service-role-key", session=session,
    )

    store.awaiting_periods(50)
    store.settle("11111111-0000-4000-8000-000000000001", "2026-08-07T06:15:13+00:00",
                 "sweep:replica-a")

    enumerate_url, enumerate_body = session.posts[0]
    settle_url, settle_body = session.posts[1]
    assert enumerate_url.endswith("/rest/v1/rpc/billing_periods_awaiting_settlement")
    assert enumerate_body == {"p_limit": 50}
    assert settle_url.endswith("/rest/v1/rpc/settle_billing_period")
    assert settle_body == {
        "p_organization_id": "11111111-0000-4000-8000-000000000001",
        "p_period_anchor": "2026-08-07T06:15:13+00:00",
        "p_computed_by": "sweep:replica-a",
        "p_allow_revision": False,
    }
    assert settle_body["p_allow_revision"] is False


def test_the_store_refuses_a_shape_it_cannot_read():
    store = SupabaseSettlementSweepStore(
        "https://example.supabase.co", "k", session=RecordingSession({"ok": True}),
    )
    with pytest.raises(RuntimeError):
        store.awaiting_periods(50)

    store = SupabaseSettlementSweepStore(
        "https://example.supabase.co", "k", session=RecordingSession("nope"),
    )
    with pytest.raises(RuntimeError):
        store.settle("org", "2026-08-07T06:15:13+00:00", "sweep:x")


def test_the_sweep_store_holds_no_ledger_state_machine():
    """It must not inherit claim/complete/release from the per-row ledger store."""
    for forbidden in ("claim_one", "begin_send", "renew", "complete",
                      "release_owner", "release_unsent", "check_health"):
        assert not hasattr(SupabaseSettlementSweepStore, forbidden), (
            f"the sweep store inherited {forbidden}; it holds a ledger surface "
            "it has no business holding"
        )


# ---------------------------------------------------------------------------
# 8. The joins to SQL that keep the simulator above honest.
# ---------------------------------------------------------------------------
def _live_writer_body() -> str:
    source = LIVE_WRITER_MIGRATION.read_text()
    start = source.index("create or replace function public.settle_billing_period")
    return source[start:source.index("\n$$;", start)]


def test_every_outcome_the_writer_can_return_is_classified_by_the_caller():
    """The missing join: a new writer outcome must not fall into a silent bucket."""
    body = _live_writer_body()
    # Two spellings exist in the body: a literal, and the one CASE expression
    # that decides between 'settled' and 'revised'.
    produced: set[str] = set(re.findall(r"'outcome',\s*'([a-z_]+)'", body))
    for tail in re.findall(r"'outcome',\s*(case\s+when[^\n]*)", body):
        produced.update(re.findall(r"'([a-z_]+)'", tail))
    assert produced, "the writer body was not parsed; this test is vacuous"
    unclassified = sorted(produced - KNOWN_OUTCOMES)
    assert not unclassified, (
        "public.settle_billing_period can return these outcomes and "
        "api/billing_settlement_sweep.py does not classify them: "
        f"{unclassified}"
    )
    assert {"settled", "revised", "unchanged", "blocked", "halted"} <= produced


def test_the_advisory_lock_the_idempotence_argument_depends_on_still_exists():
    body = _live_writer_body()
    assert "pg_advisory_xact_lock(hashtextextended(p_organization_id::text, 0))" in body, (
        "settle_billing_period no longer serialises on the organization; the "
        "sweep's two-replica idempotence argument has no fence left"
    )


def test_a_halt_is_returned_by_the_writer_rather_than_raised():
    """Why the caller treats a halt as an outcome and not as a retryable error."""
    body = _live_writer_body()
    assert "'outcome', 'halted'" in body
    assert "when sqlstate '55000' or sqlstate '22023' then" in body, (
        "the writer stopped converting the guard's raise into a halted outcome"
    )


def test_the_caller_never_names_the_promoter_and_never_revises():
    source = SWEEP_MODULE.read_text()
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    # The name may appear only inside the module docstring's explanation of why
    # it is unreachable, never in an RPC map or a call.
    body = code.split('"""', 2)[2] if code.count('"""') >= 2 else code
    assert "promote_billing_period_settlement" not in body, (
        "the sweep references the promoter; draft -> pending is a human act and "
        "the promoter is granted to nobody (202607280013:981-983)"
    )
    assert ALLOW_REVISION is False
    assert "p_allow_revision" in body
    assert 'ALLOW_REVISION = False' in source


def test_the_promoter_is_still_granted_to_nobody_in_the_migration_chain():
    """This change must add no GRANT. If one appeared, the sweep's whole safety
    argument — runtime computes, only a human bills — would be gone."""
    writer = WRITER_MIGRATION.read_text()
    assert re.search(
        r"revoke\s+all\s+on\s+function\s+public\.promote_billing_period_settlement"
        r"\(bigint,\s*text,\s*text\)\s*\n?\s*from\s+public,\s*anon,\s*authenticated,"
        r"\s*service_role",
        writer,
    ), "the promoter's revoke-from-everyone is gone"
    granted = [
        path.name for path in sorted(MIGRATIONS.glob("*.sql"))
        if re.search(
            r"grant\s+execute\s+on\s+function\s+"
            r"public\.promote_billing_period_settlement",
            path.read_text(), re.IGNORECASE,
        )
    ]
    assert granted == [], (
        f"a migration grants EXECUTE on the promoter: {granted}"
    )
