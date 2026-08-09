"""The TTL canary (migration 202608100005, api/worker.py).

EVERY TEST HERE MOCKS THE TRANSPORT. _send_warm_ping is replaced at the module
level and its call log is asserted on, so a regression that starts spending real
provider dollars from inside pytest fails as a test failure rather than as a
bill. The one live exercise of this code is scripts/canary_live_smoke.py, which
pytest never imports and which refuses to run without an explicit env flag.

The canary is Brevitas's own money against Brevitas's own account. The tests
that matter most are the ones proving it cannot touch anything else: no
usage_log row, no warm_budget_ledger, no warm_customer_budget, no tenant
credential -- and that OpenAI is unreachable no matter what is in the
environment.
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from api import worker
from api.store import UsageStore


class _Receipt:
    """The two cache legs _warm_ttl_outcome reads, and enough of a receipt for
    calculate_costs to price."""

    def __init__(self, *, cached=0, write=0, input_tokens=5000):
        self.cached_input_tokens = cached
        self.cache_write_tokens = write
        self.input_tokens = input_tokens
        self.output_tokens = 1
        self.total_tokens = input_tokens + 1


def make_store(tmp_path, name="canary.db"):
    return UsageStore(str(tmp_path / name))


def table_rows(store, table):
    with sqlite3.connect(store.db_path) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def probes(store):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT * FROM warm_canary_probes ORDER BY id")]


def observations(store):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT * FROM warm_ttl_observations ORDER BY id")]


def canary_ledger(store, provider="anthropic", day=None):
    day = day or datetime.now(timezone.utc).date().isoformat()
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM warm_canary_ledger WHERE day=? AND provider=?",
            (day, provider)).fetchone()
    return dict(row) if row else None


class _Stop:
    """An asyncio.Event stand-in that lets a loop body run exactly once."""

    def __init__(self):
        self._seen = False

    def is_set(self):
        if not self._seen:
            self._seen = True
            return False
        return True

    async def wait(self):
        return True


def arm(monkeypatch, store, *, usage=None, providers=("anthropic",), **env):
    """Wire the worker at the store, mock the transport, arm the flag."""
    calls: list[tuple[str, dict, dict]] = []

    def fake_send(provider, spec, body, headers):
        calls.append((provider, body, headers))
        return 200, {"usage": usage if usage is not None else {}}

    monkeypatch.setattr(worker, "_store", store)
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    monkeypatch.setattr(worker, "_send_warm_ping", fake_send)
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    monkeypatch.setenv("BREVITAS_TTL_CANARY", "true")
    for provider in ("anthropic", "deepseek"):
        if provider in providers:
            monkeypatch.setenv(f"BREVITAS_PROBE_{provider.upper()}_KEY", "sk-probe")
        else:
            monkeypatch.delenv(f"BREVITAS_PROBE_{provider.upper()}_KEY",
                               raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return calls


# ---------------------------------------------------------------------------
# Off by default, and off means no I/O at all.
# ---------------------------------------------------------------------------

def test_canary_off_no_calls(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    calls = arm(monkeypatch, store)
    monkeypatch.delenv("BREVITAS_TTL_CANARY", raising=False)
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    assert calls == []
    assert table_rows(store, "warm_canary_probes") == 0
    assert table_rows(store, "warm_canary_ledger") == 0
    assert table_rows(store, "warm_ttl_observations") == 0


def test_canary_requires_warming_enabled(tmp_path, monkeypatch):
    """The canary rides the warming master switch: measuring TTL for a
    scheduler that is not running is spend with no consumer."""
    store = make_store(tmp_path)
    calls = arm(monkeypatch, store)
    monkeypatch.setenv("BREVITAS_WARMING", "false")
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    assert calls == []


def test_canary_requires_probe_key(tmp_path, monkeypatch):
    """No probe key is not an error: the provider is simply skipped."""
    store = make_store(tmp_path)
    calls = arm(monkeypatch, store, providers=())
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    assert calls == []
    assert table_rows(store, "warm_canary_probes") == 0


def test_canary_openai_unreachable(tmp_path, monkeypatch):
    """OpenAI stays measurement-only. The provider tuple is a literal pair, so
    a probe key in the environment cannot widen it."""
    monkeypatch.setenv("BREVITAS_PROBE_OPENAI_KEY", "sk-should-be-ignored")
    store = make_store(tmp_path)
    calls = arm(monkeypatch, store, providers=("anthropic", "deepseek"))
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    assert worker._CANARY_PROVIDERS == ("anthropic", "deepseek")
    assert {provider for provider, _body, _headers in calls} == {
        "anthropic", "deepseek"}
    assert {row["provider"] for row in probes(store)} <= {"anthropic", "deepseek"}


# ---------------------------------------------------------------------------
# The dollar fence.
# ---------------------------------------------------------------------------

def test_canary_daily_cap_blocks_atomically(tmp_path, monkeypatch):
    """A ledger already at the cap denies the reservation, and nothing is
    sent -- the reserve happens BEFORE the request leaves."""
    store = make_store(tmp_path)
    day = datetime.now(timezone.utc).date().isoformat()
    _prefix, tokens = worker._canary_prefix("anthropic", f"anthropic:{day}:0")
    estimate = worker._canary_estimate_usd("anthropic", "claude-haiku-4-5", tokens)
    # Seed the day to within half an estimate of the cap: one more probe
    # crosses it, so the reserve must refuse and nothing may be sent.
    booked = round(0.25 - estimate / 2, 10)
    store.warm_canary_reserve("anthropic", day, booked, 100.0)
    calls = arm(monkeypatch, store, BREVITAS_TTL_CANARY_DAILY_USD="0.25")
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    assert calls == []
    assert table_rows(store, "warm_canary_probes") == 0
    assert canary_ledger(store)["spent_usd"] == pytest.approx(booked)


def test_canary_reserve_is_conditional(tmp_path):
    """The reserve is one conditional UPDATE: the request that would cross the
    cap loses and says so, rather than both racers winning."""
    store = make_store(tmp_path)
    day = datetime.now(timezone.utc).date().isoformat()
    first = store.warm_canary_reserve("anthropic", day, 0.06, 0.10)
    second = store.warm_canary_reserve("anthropic", day, 0.06, 0.10)
    assert first["allowed"] is True
    assert second["allowed"] is False
    assert canary_ledger(store)["spent_usd"] == pytest.approx(0.06)
    assert canary_ledger(store)["probes"] == 1


def test_canary_settle_adjusts_to_actual(tmp_path):
    store = make_store(tmp_path)
    day = datetime.now(timezone.utc).date().isoformat()
    store.warm_canary_reserve("anthropic", day, 0.05, 1.0)
    store.warm_canary_settle("anthropic", day, 0.05, 0.02)
    assert canary_ledger(store)["spent_usd"] == pytest.approx(0.02)
    # Floored at zero: an over-estimate lands at 0, never below.
    store.warm_canary_settle("anthropic", day, 5.0, 0.0)
    assert canary_ledger(store)["spent_usd"] == pytest.approx(0.0)


def test_canary_reserve_rejects_openai(tmp_path):
    store = make_store(tmp_path)
    day = datetime.now(timezone.utc).date().isoformat()
    with pytest.raises(ValueError):
        store.warm_canary_reserve("openai", day, 0.01, 1.0)
    with pytest.raises(ValueError):
        store.warm_canary_settle("openai", day, 0.01, 0.01)


# ---------------------------------------------------------------------------
# The experiment.
# ---------------------------------------------------------------------------

def test_canary_starts_experiment_and_books_spend(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    calls = arm(monkeypatch, store)
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    assert len(calls) == 1
    pending = probes(store)
    assert len(pending) == 1
    assert pending[0]["state"] == "pending"
    assert pending[0]["provider"] == "anthropic"
    assert pending[0]["gap_target_s"] == pytest.approx(0.5 * 300)
    assert canary_ledger(store)["spent_usd"] > 0


def test_canary_gap_ladder_progression(tmp_path, monkeypatch):
    """The rung is chosen by the count of DONE probes, so the ladder advances
    on measurement and a run of failures cannot walk past the informative
    gaps."""
    store = make_store(tmp_path)
    now = datetime.now(timezone.utc)
    for index, _rung in enumerate(worker._CANARY_LADDER):
        for probe in probes(store):
            if probe["state"] == "pending":
                store.warm_canary_probe_mark(probe["id"], "done")
        arm(monkeypatch, store)
        asyncio.run(worker.warm_ttl_canary(_Stop()))
        pending = [row for row in probes(store) if row["state"] == "pending"]
        assert len(pending) == 1
        assert pending[0]["gap_target_s"] == pytest.approx(
            worker._CANARY_LADDER[index % len(worker._CANARY_LADDER)] * 300)


def test_canary_skips_when_probe_in_flight(tmp_path, monkeypatch):
    """One experiment at a time per provider: a pending probe not yet due means
    no new write leg, so a slow gap cannot be lapped by the next one."""
    store = make_store(tmp_path)
    arm(monkeypatch, store)
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    assert len(probes(store)) == 1
    calls = arm(monkeypatch, store)
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    assert calls == []
    assert len(probes(store)) == 1


def test_canary_probe_records_observation_source_canary(tmp_path, monkeypatch):
    """A cache-read leg on the probe means the entry survived the gap."""
    store = make_store(tmp_path)
    written = datetime.now(timezone.utc) - timedelta(seconds=150)
    store.warm_canary_probe_insert(
        "anthropic", "claude-haiku-4-5", "anthropic:seed:0", 5000,
        written.isoformat(), 150.0)
    arm(monkeypatch, store)
    monkeypatch.setattr(worker, "normalize_usage",
                        lambda usage, provider: _Receipt(cached=5000))
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    rows = observations(store)
    assert len(rows) == 1
    assert rows[0]["source"] == "canary"
    assert rows[0]["outcome"] == "warm"
    assert rows[0]["provider"] == "anthropic"
    assert rows[0]["ttl_tier"] == "5m"
    assert rows[0]["gap_seconds"] == pytest.approx(150, abs=5)
    assert probes(store)[0]["state"] == "done"


def test_canary_probe_records_expired_on_cache_write(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    written = datetime.now(timezone.utc) - timedelta(seconds=400)
    store.warm_canary_probe_insert(
        "anthropic", "claude-haiku-4-5", "anthropic:seed:0", 5000,
        written.isoformat(), 400.0)
    arm(monkeypatch, store)
    monkeypatch.setattr(worker, "normalize_usage",
                        lambda usage, provider: _Receipt(write=5000))
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    rows = observations(store)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "expired"


def test_canary_no_cache_legs_records_nothing(tmp_path, monkeypatch):
    """A receipt with neither leg is not an observation and must not be
    invented -- but the probe is still done and still settled."""
    store = make_store(tmp_path)
    written = datetime.now(timezone.utc) - timedelta(seconds=150)
    store.warm_canary_probe_insert(
        "anthropic", "claude-haiku-4-5", "anthropic:seed:0", 5000,
        written.isoformat(), 150.0)
    arm(monkeypatch, store)
    monkeypatch.setattr(worker, "normalize_usage",
                        lambda usage, provider: _Receipt())
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    assert observations(store) == []
    assert probes(store)[0]["state"] == "done"


def test_canary_failure_marks_failed_and_keeps_est(tmp_path, monkeypatch):
    """A transport failure after a POST is ambiguous: the provider may have run
    and billed it, so the estimate stays booked."""
    store = make_store(tmp_path)
    written = datetime.now(timezone.utc) - timedelta(seconds=150)
    store.warm_canary_probe_insert(
        "anthropic", "claude-haiku-4-5", "anthropic:seed:0", 5000,
        written.isoformat(), 150.0)
    arm(monkeypatch, store)

    def boom(provider, spec, body, headers):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(worker, "_send_warm_ping", boom)
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    assert probes(store)[0]["state"] == "failed"
    assert canary_ledger(store)["spent_usd"] > 0
    assert observations(store) == []


def test_canary_never_touches_money_tables(tmp_path, monkeypatch):
    """The whole point. After a full cycle -- write leg and probe leg -- the
    tenant money tables are untouched."""
    store = make_store(tmp_path)
    written = datetime.now(timezone.utc) - timedelta(seconds=150)
    store.warm_canary_probe_insert(
        "anthropic", "claude-haiku-4-5", "anthropic:seed:0", 5000,
        written.isoformat(), 150.0)
    arm(monkeypatch, store, providers=("anthropic", "deepseek"))
    monkeypatch.setattr(worker, "normalize_usage",
                        lambda usage, provider: _Receipt(cached=5000))
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    assert table_rows(store, "usage_log") == 0
    assert table_rows(store, "warm_budget_ledger") == 0
    assert table_rows(store, "warm_customer_budget") == 0
    assert table_rows(store, "warm_prefixes") == 0
    assert table_rows(store, "warm_decision_log") == 0
    assert table_rows(store, "warm_ttl_observations") == 1


def test_canary_unpriced_model_disables_provider(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    calls = arm(monkeypatch, store,
                BREVITAS_TTL_CANARY_MODEL_ANTHROPIC="not-a-real-model")
    asyncio.run(worker.warm_ttl_canary(_Stop()))
    assert calls == []
    assert table_rows(store, "warm_canary_probes") == 0
    assert table_rows(store, "warm_canary_ledger") == 0


# ---------------------------------------------------------------------------
# The prefix.
# ---------------------------------------------------------------------------

def test_canary_prefix_deterministic_and_sized(tmp_path):
    anthropic, anthropic_tokens = worker._canary_prefix("anthropic", "seed-1")
    again, again_tokens = worker._canary_prefix("anthropic", "seed-1")
    assert anthropic == again and anthropic_tokens == again_tokens
    assert worker._canary_prefix("anthropic", "seed-2")[0] != anthropic
    # Below 4096 tokens haiku creates no cache entry at all, so a shorter
    # prefix would measure nothing.
    assert anthropic_tokens >= 4600
    deepseek, deepseek_tokens = worker._canary_prefix("deepseek", "seed-1")
    assert deepseek_tokens >= 512


def test_canary_prefix_vocabulary_is_closed(tmp_path):
    """The prefix cannot contain customer data BY CONSTRUCTION: every word in
    it comes from the fixed list, and nothing else is interpolated except the
    block header and the seed-driven ordering."""
    text, _tokens = worker._canary_prefix("anthropic", "seed-9")
    vocabulary = set(worker._CANARY_WORDS)
    header = {"brevitas", "ttl", "canary", "block"}
    for token in text.replace(":", " ").replace(".", " ").split():
        cleaned = token.lower()
        assert (cleaned in vocabulary or cleaned in header
                or cleaned.isdigit()), cleaned


def test_canary_body_shapes(tmp_path):
    anthropic = worker._canary_body("anthropic", "claude-haiku-4-5", "PREFIX")
    assert anthropic["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert anthropic["max_tokens"] == 1
    assert anthropic["metadata"]["user_id"] == "brevitas-ttl-canary"
    deepseek = worker._canary_body("deepseek", "deepseek-chat", "PREFIX")
    assert deepseek["messages"][0] == {"role": "system", "content": "PREFIX"}
    assert deepseek["stream"] is False
    assert worker._canary_headers("anthropic", "k")["x-api-key"] == "k"
    assert worker._canary_headers("deepseek", "k")["Authorization"] == "Bearer k"


def test_canary_probe_mark_is_write_once(tmp_path):
    """Marking is fenced on 'pending', so a duplicate cycle cannot flip a
    failed probe to done and advance the ladder on a measurement that never
    happened."""
    store = make_store(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    created = store.warm_canary_probe_insert(
        "anthropic", "claude-haiku-4-5", "seed", 5000, now, 10.0)
    store.warm_canary_probe_mark(created["id"], "failed")
    store.warm_canary_probe_mark(created["id"], "done")
    assert probes(store)[0]["state"] == "failed"
    with pytest.raises(ValueError):
        store.warm_canary_probe_mark(created["id"], "pending")


def test_canary_probe_due_only_returns_due_pending(tmp_path):
    store = make_store(tmp_path)
    now = datetime.now(timezone.utc)
    store.warm_canary_probe_insert(
        "anthropic", "m", "seed-due", 5000,
        (now - timedelta(seconds=600)).isoformat(), 300.0)
    store.warm_canary_probe_insert(
        "anthropic", "m", "seed-future", 5000, now.isoformat(), 3600.0)
    store.warm_canary_probe_insert(
        "deepseek", "m", "seed-other", 5000,
        (now - timedelta(seconds=600)).isoformat(), 300.0)
    due = store.warm_canary_probe_due("anthropic", 50)
    assert [row["prefix_seed"] for row in due] == ["seed-due"]
    stats = store.warm_canary_probe_stats("anthropic")
    assert stats["pending"] == 2 and stats["done"] == 0


def test_canary_purge_horizons(tmp_path):
    """Probes age at 30 days, the dollar ledger at 400 -- it is evidence of
    what the probes cost."""
    store = make_store(tmp_path)
    now = datetime.now(timezone.utc)
    store.warm_canary_probe_insert("anthropic", "m", "seed", 5000,
                                   now.isoformat(), 10.0)
    store.warm_canary_reserve("anthropic", now.date().isoformat(), 0.01, 1.0)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_canary_probes SET created_at=?",
                   ((now - timedelta(days=31)).isoformat(),))
        db.execute("INSERT INTO warm_canary_ledger(day,provider,probes,spent_usd) "
                   "VALUES(?,?,1,0.01)",
                   ((now.date() - timedelta(days=401)).isoformat(), "deepseek"))
    result = store.purge_warm_state(7)
    assert result["canary_probes_deleted"] == 1
    assert result["canary_ledger_deleted"] == 1
    assert canary_ledger(store) is not None


def test_ttl_observe_accepts_canary_source(tmp_path):
    store = make_store(tmp_path)
    store.warm_ttl_observe("anthropic", "claude-haiku-4-5", "5m", 120.0,
                           "warm", "canary")
    assert observations(store)[0]["source"] == "canary"
    with pytest.raises(ValueError):
        store.warm_ttl_observe("anthropic", "m", "5m", 1.0, "warm", "bogus")
