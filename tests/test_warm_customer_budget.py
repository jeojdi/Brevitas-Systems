"""Per-customer monthly spend envelopes (migration 202608100004, mirrored in
api/store.py).

The envelope gate is UNCONDITIONAL in the claim path and vacuous until a row
exists, so the first test here is the default-off story: with no envelope rows
the claim loop must be byte-for-byte what it was before this migration. Every
other test is about money moving in exactly two places at once -- the
organization ledger and the customer envelope -- because a reservation booked on
one and not the other is how a spend ceiling stops being one.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from api.store import UsageStore, _utc_hour_bucket, warm_budget_period

ORG = "org-envelope-1"
BREAK_EVEN = {"anthropic": 0.11, "deepseek": round(0.02 / 0.98, 6)}
CUSTOMER_A = "11111111-1111-4111-8111-111111111111"
CUSTOMER_B = "22222222-2222-4222-8222-222222222222"


def make_store(tmp_path, name="envelope.db"):
    return UsageStore(str(tmp_path / name))


def enable(store, provider="anthropic", budget=10.0, max_customers=1000,
           max_pings=288, org=ORG):
    return store.warm_credentials_upsert(
        org, provider, "enc:credential", True, "actor-1", budget,
        max_customers, max_pings)


def seed_customer(store, customer_id, org=ORG, external=None):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT OR IGNORE INTO customers(id,organization_id,external_id,"
            "display_name,status,cache_enabled,created_at,updated_at) "
            "VALUES(?,?,?,'','active',1,?,?)",
            (customer_id, org, external or customer_id, now, now))


def seed_prefix(store, *, prefix_hash, customer, provider="anthropic",
                tokens=100_000, arrival_count=10, bucket_count=10,
                reserve=None, due_offset_s=-1, org=ORG):
    now = datetime.now(timezone.utc)
    bucket = _utc_hour_bucket(now)
    if reserve is None:
        reserve = round(3.75 * tokens / 1_000_000.0, 10)
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO warm_prefixes(organization_id,customer_id,provider,"
            "prefix_hash,payload_ciphertext,prefix_tokens,provider_ttl_seconds,"
            "arrival_count,ewma_interarrival_s,hour_histogram,consecutive_misses,"
            "pings_today,pings_today_date,state,created_at,last_seen_at,"
            "next_due_at,expires_at,ping_reserve_usd) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,0,0,'','active',?,?,?,?,?)",
            (org, customer, provider, prefix_hash, "enc:payload", int(tokens),
             300, int(arrival_count), None,
             json.dumps({bucket: bucket_count}),
             now.isoformat(), now.isoformat(),
             (now + timedelta(seconds=due_offset_s)).isoformat(),
             (now + timedelta(days=1)).isoformat(), float(reserve)))
    return reserve


def seed_envelope(store, customer, envelope_usd, *, provider="anthropic",
                  org=ORG, period=None, source="auto", reserved=0.0, spent=0.0,
                  reserved_day=None):
    """Insert one envelope row directly.

    reserved_day is the UTC day the seeded reservation was written on -- the
    claim stamps it, the claim's self-heal zeroes anything older than today, and
    warm_ping_settle releases only a reservation whose day matches the day it is
    settling against. It defaults to TODAY, which is what a live reservation
    carries; a test that wants a stranded one passes an earlier day.
    """
    now = datetime.now(timezone.utc)
    period = period or warm_budget_period(now.date().isoformat())
    if reserved_day is None:
        reserved_day = now.date().isoformat()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO warm_customer_budget(organization_id,provider,"
            "period_start,customer_ref,envelope_usd,reserved_usd,reserved_day,"
            "spent_usd,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (org, provider, period, customer, float(envelope_usd),
             float(reserved), str(reserved_day), float(spent), source,
             now.isoformat(), now.isoformat()))
    return period


def envelope_row(store, customer, *, provider="anthropic", org=ORG, period=None):
    period = period or warm_budget_period(
        datetime.now(timezone.utc).date().isoformat())
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM warm_customer_budget WHERE organization_id=? AND "
            "provider=? AND period_start=? AND customer_ref=?",
            (org, provider, period, customer)).fetchone()
    return dict(row) if row else None


def claim(store, limit=10, **kwargs):
    return store.warm_due_claim(
        limit, reserve_usd_per_mtok=3.75, roi_min_arrivals=5, roi_min_p=0.35,
        roi_break_even_p=0.11, stop_loss=3, max_gap_seconds=3600,
        safety_margin_seconds=60,
        roi_break_even_by_provider=dict(BREAK_EVEN), **kwargs)


def decision_map(store):
    with sqlite3.connect(store.db_path) as db:
        return {row[0]: row[1] for row in db.execute(
            "SELECT prefix_hash,decision FROM warm_decision_log ORDER BY id")}


def ledger(store, provider="anthropic", org=ORG):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM warm_budget_ledger WHERE organization_id=? AND "
            "provider=?", (org, provider)).fetchone()
    return dict(row) if row else None


def prefix_row(store, prefix_hash, customer, provider="anthropic", org=ORG):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM warm_prefixes WHERE organization_id=? AND "
            "customer_id=? AND provider=? AND prefix_hash=?",
            (org, customer, provider, prefix_hash)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# The default state: no rows, no constraint.
# ---------------------------------------------------------------------------

def test_envelope_absent_no_gate(tmp_path):
    """A deployment with no envelope rows behaves exactly as 202608100003 did."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    seed_prefix(store, prefix_hash="b" * 64, customer=CUSTOMER_B)

    result = claim(store, limit=10)

    assert len(result["rows"]) == 2
    assert set(decision_map(store).values()) == {"pinged"}
    # The gate ran and wrote nothing: no row means no constraint AND no row.
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM warm_customer_budget").fetchone()[0] == 0


def test_envelope_denied_no_side_effects(tmp_path):
    """A denial must leave the organization ledger, the prefix and the envelope
    itself exactly as it found them -- the denial is the whole effect."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    reserve = seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    assert reserve > 0.001
    seed_envelope(store, CUSTOMER_A, 0.001)

    result = claim(store, limit=10)

    assert result["rows"] == []
    assert decision_map(store) == {"a" * 64: "envelope_denied"}
    assert ledger(store) is None or float(ledger(store)["reserved_usd"]) == 0.0
    envelope = envelope_row(store, CUSTOMER_A)
    assert float(envelope["reserved_usd"]) == 0.0
    assert float(envelope["spent_usd"]) == 0.0
    assert prefix_row(store, "a" * 64, CUSTOMER_A)["claim_token"] == ""


def test_envelope_admits_below_ceiling(tmp_path):
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    reserve = seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    seed_envelope(store, CUSTOMER_A, reserve * 10)

    result = claim(store, limit=10)

    assert len(result["rows"]) == 1
    assert float(envelope_row(store, CUSTOMER_A)["reserved_usd"]) == pytest.approx(
        reserve)


def test_envelope_binds_per_customer_not_per_org(tmp_path):
    """The point of the envelope: one customer at its ceiling cannot starve
    another under the same organization budget."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    reserve = seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    seed_prefix(store, prefix_hash="b" * 64, customer=CUSTOMER_B)
    seed_envelope(store, CUSTOMER_A, 0.0001)
    seed_envelope(store, CUSTOMER_B, reserve * 10)

    claim(store, limit=10)

    verdicts = decision_map(store)
    assert verdicts["a" * 64] == "envelope_denied"
    assert verdicts["b" * 64] == "pinged"


# ---------------------------------------------------------------------------
# Reserve-then-settle: both books move, or neither is a ceiling.
# ---------------------------------------------------------------------------

def test_envelope_reserve_then_settle_warmed(tmp_path):
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    reserve = seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    seed_envelope(store, CUSTOMER_A, reserve * 10)

    row = claim(store, limit=10)["rows"][0]
    assert float(envelope_row(store, CUSTOMER_A)["reserved_usd"]) == pytest.approx(
        reserve)

    actual = round(reserve / 4, 10)
    store.warm_ping_settle(
        ORG, CUSTOMER_A, "anthropic", "a" * 64, row["budget_day"],
        float(row["reserved_usd"]), actual, "warmed", 300, 60,
        claim_token=row["claim_token"])

    envelope = envelope_row(store, CUSTOMER_A)
    assert float(envelope["reserved_usd"]) == pytest.approx(0.0, abs=1e-12)
    assert float(envelope["spent_usd"]) == pytest.approx(actual)
    # Mirrors the organization ledger exactly.
    book = ledger(store)
    assert float(book["reserved_usd"]) == pytest.approx(0.0, abs=1e-12)
    assert float(book["spent_usd"]) == pytest.approx(actual)


def test_envelope_settle_spent_unknown_books_reservation(tmp_path):
    """'spent_unknown' books the RESERVATION on the envelope too: the ping may
    have been charged and nothing priced it, so the admitted upper bound is the
    conservative booking on both books."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    reserve = seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    seed_envelope(store, CUSTOMER_A, reserve * 10)

    row = claim(store, limit=10)["rows"][0]
    store.warm_ping_settle(
        ORG, CUSTOMER_A, "anthropic", "a" * 64, row["budget_day"],
        float(row["reserved_usd"]), 0.0, "spent_unknown", 300, 60,
        claim_token=row["claim_token"])

    envelope = envelope_row(store, CUSTOMER_A)
    assert float(envelope["reserved_usd"]) == pytest.approx(0.0, abs=1e-12)
    assert float(envelope["spent_usd"]) == pytest.approx(reserve)


def test_envelope_settle_release_returns_reservation(tmp_path):
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    reserve = seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    seed_envelope(store, CUSTOMER_A, reserve * 10)

    row = claim(store, limit=10)["rows"][0]
    store.warm_ping_settle(
        ORG, CUSTOMER_A, "anthropic", "a" * 64, row["budget_day"],
        float(row["reserved_usd"]), 0.0, "release", 300, 60,
        claim_token=row["claim_token"])

    envelope = envelope_row(store, CUSTOMER_A)
    assert float(envelope["reserved_usd"]) == pytest.approx(0.0, abs=1e-12)
    assert float(envelope["spent_usd"]) == 0.0


def test_envelope_period_derived_from_budget_day(tmp_path):
    """A ping claimed on the 31st and settled on the 1st settles against the
    month it was CLAIMED in, because both sides derive the period from a day
    already in hand rather than from the wall clock."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    reserve = seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    # Last day of the previous month, and the row that belongs to it.
    today = datetime.now(timezone.utc).date()
    prior_last_day = today.replace(day=1) - timedelta(days=1)
    prior_period = warm_budget_period(prior_last_day.isoformat())
    seed_envelope(store, CUSTOMER_A, reserve * 10, period=prior_period,
                  reserved=reserve,
                  # The reservation was written on the day it is settled
                  # against -- the 31st -- which is exactly the case the
                  # same-day release guard must NOT block.
                  reserved_day=prior_last_day.isoformat())

    store.warm_ping_settle(
        ORG, CUSTOMER_A, "anthropic", "a" * 64, prior_last_day.isoformat(),
        reserve, round(reserve / 2, 10), "warmed", 300, 60)

    prior = envelope_row(store, CUSTOMER_A, period=prior_period)
    assert float(prior["reserved_usd"]) == pytest.approx(0.0, abs=1e-12)
    assert float(prior["spent_usd"]) == pytest.approx(round(reserve / 2, 10))
    # The current month's row was never created and is untouched.
    assert envelope_row(store, CUSTOMER_A) is None


def test_envelope_settle_missing_row_is_noop(tmp_path):
    """Including after a tombstone: the reservation strands into the same
    greatest(0, ...) release semantics the organization ledger has."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    reserve = seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)

    row = claim(store, limit=10)["rows"][0]
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM warm_customer_budget").fetchone()[0] == 0
    store.warm_ping_settle(
        ORG, CUSTOMER_A, "anthropic", "a" * 64, row["budget_day"],
        float(row["reserved_usd"]), reserve, "warmed", 300, 60,
        claim_token=row["claim_token"])
    assert float(ledger(store)["spent_usd"]) == pytest.approx(reserve)


# ---------------------------------------------------------------------------
# The allocator.
# ---------------------------------------------------------------------------

def seed_decision(store, customer, index_score, *, provider="anthropic",
                  org=ORG, age_days=1):
    ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO warm_decision_log(organization_id,customer_id,provider,"
            "prefix_hash,ts,decision,p_return,roi_floor,reserve_usd,"
            "prefix_tokens,arrival_count,index_score) "
            "VALUES(?,?,?,?,?,'pinged',0.5,0.35,0.1,1000,10,?)",
            (org, customer, provider, "f" * 64, ts, float(index_score)))


def days_this_month():
    import calendar
    now = datetime.now(timezone.utc)
    return calendar.monthrange(now.year, now.month)[1]


def test_allocator_mass_shares(tmp_path):
    store = make_store(tmp_path)
    enable(store, budget=1.0)
    seed_decision(store, CUSTOMER_A, 3.0)
    seed_decision(store, CUSTOMER_B, 1.0)

    result = store.warm_customer_budget_allocate()

    budget_month = 1.0 * days_this_month()
    assert result["written"] == 2
    assert float(envelope_row(store, CUSTOMER_A)["envelope_usd"]) == pytest.approx(
        budget_month * 0.75, abs=1e-6)
    assert float(envelope_row(store, CUSTOMER_B)["envelope_usd"]) == pytest.approx(
        budget_month * 0.25, abs=1e-6)
    assert envelope_row(store, CUSTOMER_A)["source"] == "auto"


def test_allocator_ignores_negative_index_mass(tmp_path):
    """greatest(index_score, 0): a customer whose every candidate scored below
    break-even generated no positive demand and gets no share of it."""
    store = make_store(tmp_path)
    enable(store, budget=1.0)
    seed_decision(store, CUSTOMER_A, 2.0)
    seed_decision(store, CUSTOMER_B, -5.0)

    store.warm_customer_budget_allocate()

    budget_month = 1.0 * days_this_month()
    assert float(envelope_row(store, CUSTOMER_A)["envelope_usd"]) == pytest.approx(
        budget_month, abs=1e-6)
    assert envelope_row(store, CUSTOMER_B) is None


def test_allocator_zero_mass_equal_split(tmp_path):
    """No index history at all (BREVITAS_WARM_INDEX never on) falls back to an
    equal split over customers that actually have something to warm."""
    store = make_store(tmp_path)
    enable(store, budget=1.0)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    seed_prefix(store, prefix_hash="b" * 64, customer=CUSTOMER_B)

    store.warm_customer_budget_allocate()

    budget_month = 1.0 * days_this_month()
    for customer in (CUSTOMER_A, CUSTOMER_B):
        assert float(envelope_row(store, customer)["envelope_usd"]) == pytest.approx(
            budget_month / 2, abs=1e-6)


def test_allocator_no_customers_writes_nothing(tmp_path):
    store = make_store(tmp_path)
    enable(store, budget=1.0)
    result = store.warm_customer_budget_allocate()
    assert result["written"] == 0
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM warm_customer_budget").fetchone()[0] == 0


def test_allocator_raise_only_and_override_immune(tmp_path):
    store = make_store(tmp_path)
    enable(store, budget=1.0)
    seed_decision(store, CUSTOMER_A, 3.0)
    seed_decision(store, CUSTOMER_B, 1.0)
    # An existing auto row far above whatever the recompute produces, and an
    # operator's hand-set ceiling far below it.
    seed_envelope(store, CUSTOMER_A, 999.0, source="auto")
    seed_envelope(store, CUSTOMER_B, 0.5, source="org_override")

    store.warm_customer_budget_allocate()

    assert float(envelope_row(store, CUSTOMER_A)["envelope_usd"]) == 999.0
    assert float(envelope_row(store, CUSTOMER_B)["envelope_usd"]) == 0.5
    assert envelope_row(store, CUSTOMER_B)["source"] == "org_override"


def test_allocator_skips_disabled_and_zero_budget(tmp_path):
    """warm_credentials_upsert refuses to enable on a zero budget, so both of
    these states are reachable only by a later edit -- a budget dropped to zero
    or a credential disabled. Neither may produce a ceiling out of nothing."""
    store = make_store(tmp_path)
    enable(store, budget=1.0)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_credentials SET daily_budget_usd=0")
    store.warm_customer_budget_allocate()
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM warm_customer_budget").fetchone()[0] == 0
        db.execute("UPDATE warm_credentials SET daily_budget_usd=1,enabled=0")
    store.warm_customer_budget_allocate()
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM warm_customer_budget").fetchone()[0] == 0


def test_allocator_bounds(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.warm_customer_budget_allocate(0)
    with pytest.raises(ValueError):
        store.warm_customer_budget_allocate(10_001)


# ---------------------------------------------------------------------------
# The store's own put/list contract.
# ---------------------------------------------------------------------------

def test_put_requires_known_customer(tmp_path):
    store = make_store(tmp_path)
    enable(store)
    with pytest.raises(LookupError):
        store.warm_customer_budget_put(ORG, "anthropic", CUSTOMER_A, None, 1.0)
    # The refusal raises out of an open BEGIN IMMEDIATE, so the write lock has
    # to be released -- otherwise one unknown customer would wedge the store.
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM warm_customer_budget").fetchone()[0] == 0
    seed_customer(store, CUSTOMER_A)
    assert store.warm_customer_budget_put(
        ORG, "anthropic", CUSTOMER_A, None, 1.0)["envelope_usd"] == 1.0


def test_put_preserves_booked_money(tmp_path):
    """A ceiling is not an account: lowering it below reserved+spent is allowed
    and moves nothing that is already booked."""
    store = make_store(tmp_path)
    enable(store)
    seed_customer(store, CUSTOMER_A)
    seed_envelope(store, CUSTOMER_A, 5.0, reserved=1.0, spent=2.0)

    saved = store.warm_customer_budget_put(ORG, "anthropic", CUSTOMER_A, None, 0.5)

    assert saved["envelope_usd"] == pytest.approx(0.5)
    assert saved["source"] == "org_override"
    envelope = envelope_row(store, CUSTOMER_A)
    assert float(envelope["reserved_usd"]) == 1.0
    assert float(envelope["spent_usd"]) == 2.0


def test_put_bounds(tmp_path):
    store = make_store(tmp_path)
    seed_customer(store, CUSTOMER_A)
    with pytest.raises(ValueError):
        store.warm_customer_budget_put(ORG, "mistral", CUSTOMER_A, None, 1.0)
    with pytest.raises(ValueError):
        store.warm_customer_budget_put(ORG, "anthropic", "not-a-uuid", None, 1.0)
    with pytest.raises(ValueError):
        store.warm_customer_budget_put(ORG, "anthropic", CUSTOMER_A, None, -1.0)
    with pytest.raises(ValueError):
        store.warm_customer_budget_put(ORG, "anthropic", CUSTOMER_A, None, 1e9)


def test_list_filters_period_and_provider(tmp_path):
    store = make_store(tmp_path)
    enable(store)
    seed_envelope(store, CUSTOMER_A, 1.0)
    seed_envelope(store, CUSTOMER_B, 2.0, provider="deepseek")
    prior = warm_budget_period(
        (datetime.now(timezone.utc).date().replace(day=1)
         - timedelta(days=1)).isoformat())
    seed_envelope(store, CUSTOMER_A, 9.0, period=prior)

    current = store.warm_customer_budget_list(ORG)
    assert len(current) == 2
    only_anthropic = store.warm_customer_budget_list(ORG, "anthropic")
    assert [row["customer_ref"] for row in only_anthropic] == [CUSTOMER_A]
    older = store.warm_customer_budget_list(ORG, None, prior)
    assert [float(row["envelope_usd"]) for row in older] == [9.0]
    with pytest.raises(ValueError):
        store.warm_customer_budget_list(ORG, "mistral")


def test_tombstone_preserves_dollars(tmp_path):
    """SQLite has no compliance_delete_subject mirror, so the erasure itself is
    asserted in the Postgres suite (migration-warm-envelope-assertions.sql).
    What is asserted here is the invariant that makes the tombstone safe: a
    tombstoned ref is a legal key the settle path treats as any other row, and
    warm_customer_budget_put refuses to write one."""
    store = make_store(tmp_path)
    enable(store)
    tombstone = "erased:" + "ab" * 32
    seed_envelope(store, tombstone, 5.0, reserved=1.0, spent=2.0)

    store.warm_ping_settle(
        ORG, str(uuid.UUID(int=7)), "anthropic", "a" * 64,
        datetime.now(timezone.utc).date().isoformat(), 1.0, 0.25, "warmed",
        300, 60)

    # The settle keyed on the (live) customer id found nothing: the tombstoned
    # row keeps its dollars and its unlinkable key.
    row = envelope_row(store, tombstone)
    assert float(row["reserved_usd"]) == 1.0
    assert float(row["spent_usd"]) == 2.0
    with pytest.raises(ValueError):
        store.warm_customer_budget_put(ORG, "anthropic", tombstone, None, 1.0)


def test_warm_budget_period_derivation():
    assert warm_budget_period("2026-08-31") == "2026-08-01"
    assert warm_budget_period("2026-01-01") == "2026-01-01"
    assert warm_budget_period("2026-12-15T04:05:06+00:00") == "2026-12-01"
    with pytest.raises(ValueError):
        warm_budget_period("")


def test_envelope_allocator_env_parsing(monkeypatch):
    from api import worker

    monkeypatch.delenv("BREVITAS_WARM_ENVELOPE_ALLOCATOR", raising=False)
    assert worker._warm_envelope_allocator_enabled() is False
    monkeypatch.setenv("BREVITAS_WARM_ENVELOPE_ALLOCATOR", "true")
    assert worker._warm_envelope_allocator_enabled() is True
    monkeypatch.setenv("BREVITAS_WARM_ENVELOPE_INTERVAL_SECONDS", "1")
    assert worker._warm_bound(
        "BREVITAS_WARM_ENVELOPE_INTERVAL_SECONDS", 86_400, 3_600, 604_800) == 3_600
