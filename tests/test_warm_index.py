"""The dollar index, density claim ordering and the lambda pacing dual
(migration 202608100002, mirrored in api/store.py).

The load-bearing test here is test_index_flat_priors_equivalence_v1: at flat
priors the index reduces to p/b - 1 and lambda starts at 0, which is exactly the
provider break-even the v1 ROI gate already applied, so turning the flag ON must
reproduce the flag-OFF run decision for decision. That equivalence is the
cold-start and fallback story for every later part of Phase 1, and it is
asserted here rather than argued in a comment.
"""
import json
import math
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from api.store import (UsageStore, _utc_hour_bucket, warm_day_fraction,
                       warm_index_components, warm_lambda_update)
from api.worker import _warm_claim_kwargs

ORG = "org-index-1"
# Provider break-evens the worker computes from each spec's read-cost fraction:
# f/(1-f). anthropic keeps the scalar env default it was calibrated to.
BREAK_EVEN = {"anthropic": 0.11, "deepseek": round(0.02 / 0.98, 6)}


def make_store(tmp_path, name="index.db"):
    return UsageStore(str(tmp_path / name))


def enable(store, provider="anthropic", budget=10.0, max_customers=1000,
           max_pings=288, org=ORG):
    return store.warm_credentials_upsert(
        org, provider, "enc:credential", True, "actor-1", budget,
        max_customers, max_pings)


def seed_prefix(store, *, prefix_hash, customer, provider="anthropic",
                tokens=100_000, arrival_count=10, bucket_count=5,
                ewma=None, reserve=None, due_offset_s=-1, org=ORG,
                consecutive_misses=0):
    """Insert one due warm_prefixes row directly.

    warm_prefix_observe cannot express the histogram/arrival combinations these
    cases need (it always credits exactly one arrival at the current bucket), so
    the fixture writes the row the observer would eventually have produced.
    """
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
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,'','active',?,?,?,?,?)",
            (org, customer, provider, prefix_hash, "enc:payload", int(tokens),
             300, int(arrival_count),
             None if ewma is None else float(ewma),
             json.dumps({bucket: bucket_count}), int(consecutive_misses),
             now.isoformat(), now.isoformat(),
             (now + timedelta(seconds=due_offset_s)).isoformat(),
             (now + timedelta(days=1)).isoformat(), float(reserve)))
    return reserve


def claim(store, limit=10, **kwargs):
    return store.warm_due_claim(
        limit, reserve_usd_per_mtok=3.75, roi_min_arrivals=5, roi_min_p=0.35,
        roi_break_even_p=0.11, stop_loss=3, max_gap_seconds=3600,
        safety_margin_seconds=60,
        roi_break_even_by_provider=dict(BREAK_EVEN), **kwargs)


def decisions(store):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT * FROM warm_decision_log ORDER BY id")]


def decision_map(store):
    return {row["prefix_hash"]: row["decision"] for row in decisions(store)}


def ledger(store, provider="anthropic", org=ORG):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM warm_budget_ledger WHERE organization_id=? "
            "AND provider=?", (org, provider)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Flag off: nothing about v1 moves, and the data records that it did not.
# ---------------------------------------------------------------------------

def test_index_flag_off_rows_identical_to_v1(tmp_path):
    store = make_store(tmp_path)
    enable(store, budget=0.5)
    # One admitted, one below the cold-start ROI floor, one the budget cannot
    # afford once the first has reserved.
    seed_prefix(store, prefix_hash="a" * 64, customer="c1", tokens=100_000,
                arrival_count=10, bucket_count=10)
    seed_prefix(store, prefix_hash="b" * 64, customer="c2", tokens=100_000,
                arrival_count=2, bucket_count=0)
    seed_prefix(store, prefix_hash="c" * 64, customer="c3", tokens=100_000,
                arrival_count=10, bucket_count=10, due_offset_s=-1)

    result = claim(store, limit=10)
    assert len(result["rows"]) == 1
    verdicts = decision_map(store)
    assert verdicts["a" * 64] == "pinged"
    assert verdicts["b" * 64] == "skipped_roi"
    assert verdicts["c" * 64] == "budget_denied"

    for row in decisions(store):
        for column in ("index_score", "index_density", "v_hit_usd",
                       "chain_cost_usd", "c_belief_usd", "p_alive",
                       "organic_multiplier", "lambda_index"):
            assert row[column] is None, (row["decision"], column)
    # The dual's state exists (the budget gate created the row) but was never
    # touched: 0 is the provider break-even floor and the no-op value.
    assert ledger(store)["lambda_index"] == pytest.approx(0.0)
    assert ledger(store)["lambda_updated_at"] is None


# ---------------------------------------------------------------------------
# THE EQUIVALENCE TEST.
# ---------------------------------------------------------------------------

def _build_equivalence_fixture(tmp_path, name):
    """200 due prefixes over 40 customers and two providers, all gates slack.

    The order-insensitivity is load-bearing and deliberate: the per-customer cap
    and the daily budget consume shared state in VISIT order, so equality of the
    two runs' decision sets is only a theorem when neither can bind. Budget
    1000 USD against ~150 USD of reservations, 288 pings/customer/day against 5
    prefixes per customer, and claim_limit 500 against 200 candidates.
    """
    store = make_store(tmp_path, name)
    for provider in ("anthropic", "deepseek"):
        enable(store, provider=provider, budget=1000.0, max_customers=1000,
               max_pings=288)
    rng = random.Random(20260810)
    for index in range(200):
        provider = "anthropic" if index % 2 == 0 else "deepseek"
        tokens = rng.randint(500, 200_000)
        arrivals = rng.randint(1, 400)
        seed_prefix(
            store, prefix_hash=f"{index:064x}",
            customer=f"customer-{index % 40}", provider=provider,
            tokens=tokens, arrival_count=arrivals,
            # Single-bucket-weighted: the whole histogram mass sits in the hour
            # being scored, with a randomized share of the arrivals.
            bucket_count=rng.randint(0, arrivals),
            ewma=rng.choice([None, rng.uniform(1.0, 3000.0)]),
            reserve=round(3.75 * tokens / 1_000_000.0, 10),
            due_offset_s=-rng.randint(1, 600))
    return store


def _snapshot(store, result):
    return {
        "verdicts": decision_map(store),
        "claimed": {row["prefix_hash"] for row in result["rows"]},
        "reserved": {row["prefix_hash"]: round(row["reserved_usd"], 10)
                     for row in result["rows"]},
        "total": round(sum(row["reserved_usd"] for row in result["rows"]), 10),
    }


def test_index_flat_priors_equivalence_v1(tmp_path):
    off_store = _build_equivalence_fixture(tmp_path, "off.db")
    off = _snapshot(off_store, claim(off_store, limit=500))

    on_store = _build_equivalence_fixture(tmp_path, "on.db")
    on_result = claim(on_store, limit=500, index_enabled=True,
                      lambda_eta=0.2, lambda_max=1000)
    on = _snapshot(on_store, on_result)

    assert on["verdicts"] == off["verdicts"]
    assert on["claimed"] == off["claimed"]
    assert on["reserved"] == off["reserved"]
    assert on["total"] == pytest.approx(off["total"])
    # The fixture is only a theorem while the shared-state gates stay slack.
    assert set(off["verdicts"].values()) <= {"pinged", "skipped_roi"}
    assert off["claimed"]

    for row in decisions(on_store):
        break_even = BREAK_EVEN[row["provider"]]
        # The dollar components are priced from ping_reserve_usd and the
        # provider write premium, NOT from the (floored) ledger reservation.
        # The fixture seeds ping_reserve_usd == reserve_usd exactly, so
        # price_base is recoverable here by dividing the logged reservation by
        # the same multiplier api/server.py priced it at. f inverts the
        # break-even: b = f/(1-f) <=> f = b/(1+b).
        #
        # These expected values MOVED in the ordering/economics fix: c_belief
        # used to be the whole reservation and v_hit used to be reserve/b, which
        # overstated the saving a return realizes by 12x on anthropic and 50x on
        # deepseek in the compliance exports. index_score is unchanged, which is
        # the point -- v_hit/c_belief = (1-f)/f = 1/b exactly, so the gate the
        # equivalence theorem rests on did not move.
        write_multiplier = 1.25 if row["provider"] == "anthropic" else 1.0
        read_fraction = break_even / (1 + break_even)
        price_base = row["reserve_usd"] / write_multiplier
        assert row["p_alive"] == pytest.approx(1.0)
        assert row["organic_multiplier"] == pytest.approx(1.0)
        assert row["chain_cost_usd"] == pytest.approx(0.0)
        assert row["c_belief_usd"] == pytest.approx(price_base * read_fraction)
        assert row["v_hit_usd"] == pytest.approx(
            price_base * (1 - read_fraction))
        assert row["index_score"] == pytest.approx(
            row["p_return"] / break_even - 1, abs=1e-9)
        # index_density is a diagnostic now, not the ordering key, but it is
        # still the index over the RESERVATION and still logged.
        assert row["index_density"] == pytest.approx(
            row["index_score"] / max(row["reserve_usd"], 1e-10))
    # Under pace all day: the dual never leaves its break-even floor.
    for provider in ("anthropic", "deepseek"):
        assert ledger(on_store, provider)["lambda_index"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Ordering: the whole point of the index.
# ---------------------------------------------------------------------------

def test_index_ordering_under_binding_budget(tmp_path):
    def fixture(name):
        store = make_store(tmp_path, name)
        # 0.75 admits exactly one of the two whichever is visited first:
        # 0.75 + 0.0375 > 0.75 and 0.0375 + 0.75 > 0.75.
        enable(store, budget=0.75)
        # Earlier due, low density: index (0.2/0.11 - 1) = 0.818 over a 0.75
        # reservation.
        seed_prefix(store, prefix_hash="1" * 64, customer="c1", tokens=200_000,
                    arrival_count=10, bucket_count=2, due_offset_s=-600)
        # Later due, high density: index (1/0.11 - 1) = 8.09 over 0.0375.
        seed_prefix(store, prefix_hash="2" * 64, customer="c2", tokens=10_000,
                    arrival_count=10, bucket_count=10, due_offset_s=-1)
        return store

    off_store = fixture("order-off.db")
    off = claim(off_store, limit=10)
    assert [row["prefix_hash"] for row in off["rows"]] == ["1" * 64]
    assert decision_map(off_store)["2" * 64] == "budget_denied"

    on_store = fixture("order-on.db")
    on = claim(on_store, limit=10, index_enabled=True)
    assert [row["prefix_hash"] for row in on["rows"]] == ["2" * 64]
    assert decision_map(on_store)["1" * 64] == "budget_denied"
    # The denied row still carries the belief that lost, and the dual in force.
    denied = [row for row in decisions(on_store)
              if row["decision"] == "budget_denied"][0]
    assert denied["index_score"] == pytest.approx(0.2 / 0.11 - 1)
    assert denied["lambda_index"] == pytest.approx(0.0)


def test_index_ordering_prefers_value_per_dollar_not_cheapness(tmp_path):
    """FIX 1: the ordering key is index_score, NOT index_score / reserve.

    index_score already divides by c_belief, so it is already value per
    belief-dollar. Dividing it a second time by the reservation makes the key
    value-per-dollar-squared, and under a binding budget that systematically
    prefers small cheap arms to the large valuable ones the budget exists to
    allocate.

    The two candidates below are built so the two keys DISAGREE:

        A  p = 0.8  ->  index = 0.8/0.11 - 1 = 6.27   reserve 0.75
        B  p = 0.4  ->  index = 0.4/0.11 - 1 = 2.64   reserve 0.075

        index:          A (6.27) > B (2.64)      -- A wins
        index/reserve:  A (8.36) < B (35.4)      -- B wins

    The budget admits exactly one of them, so which one is claimed is the whole
    assertion. Under the old density key B was claimed and A -- worth more than
    twice as much per ping -- was budget_denied.
    """
    def fixture(name):
        store = make_store(tmp_path, name)
        # 0.8 admits either one alone and never both: 0.75 + 0.075 > 0.8.
        enable(store, budget=0.8)
        # HIGH index, HIGH reserve. 200k tokens at the 3.75/Mtok floor.
        seed_prefix(store, prefix_hash="a" * 64, customer="c1", tokens=200_000,
                    arrival_count=10, bucket_count=8, due_offset_s=-600)
        # LOW index, LOW reserve. Ten times cheaper, and far better on density.
        seed_prefix(store, prefix_hash="b" * 64, customer="c2", tokens=20_000,
                    arrival_count=10, bucket_count=4, due_offset_s=-1)
        return store

    store = fixture("value-order.db")
    result = claim(store, limit=10, index_enabled=True)

    assert [row["prefix_hash"] for row in result["rows"]] == ["a" * 64]
    verdicts = decision_map(store)
    assert verdicts["a" * 64] == "pinged"
    assert verdicts["b" * 64] == "budget_denied"

    scored = {row["prefix_hash"]: row for row in decisions(store)}
    a_row, b_row = scored["a" * 64], scored["b" * 64]
    # The premise: A is the more valuable arm and the more expensive one, and
    # the density key ranks them the other way round. If this ever stops
    # holding, the test above has stopped discriminating.
    assert a_row["index_score"] > b_row["index_score"]
    assert a_row["reserve_usd"] > b_row["reserve_usd"]
    assert a_row["index_density"] < b_row["index_density"]


# ---------------------------------------------------------------------------
# The pacing dual.
# ---------------------------------------------------------------------------

def prefund(store, *, reserved=0.0, spent=0.0, lambda_index=None,
            provider="anthropic", org=ORG):
    day = datetime.now(timezone.utc).date().isoformat()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO warm_budget_ledger(organization_id,provider,day,"
            "reserved_usd,spent_usd,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(organization_id,provider,day) DO UPDATE SET "
            "reserved_usd=excluded.reserved_usd,spent_usd=excluded.spent_usd",
            (org, provider, day, float(reserved), float(spent),
             datetime.now(timezone.utc).isoformat()))
        if lambda_index is not None:
            db.execute(
                "UPDATE warm_budget_ledger SET lambda_index=? WHERE "
                "organization_id=? AND provider=? AND day=?",
                (float(lambda_index), org, provider, day))


def test_lambda_rises_when_overpaced(tmp_path):
    store = make_store(tmp_path)
    budget = 10.0
    enable(store, budget=budget)
    seed_prefix(store, prefix_hash="d" * 64, customer="c1", tokens=100_000,
                arrival_count=10, bucket_count=10)
    # DEVIATION FROM THE LETTER OF THE PLAN, deliberately: the spec's "0.9*B
    # early in the day" is only over-paced when the wall clock is before 21:36
    # UTC, and a test whose verdict depends on the hour it runs at is not a
    # test. A batch in flight (reserved = B) puts S above B for every u, so the
    # over-pace branch is exercised whenever the suite runs.
    prefund(store, reserved=budget, spent=0.9 * budget)

    before = warm_day_fraction(datetime.now(timezone.utc))
    claim(store, limit=10, index_enabled=True)
    after = warm_day_fraction(datetime.now(timezone.utc))

    def expected(day_fraction):
        return warm_lambda_update(0.0, 1.9 * budget, budget, day_fraction,
                                  0.2, 1000.0)

    persisted = ledger(store)["lambda_index"]
    assert persisted > 0
    assert expected(after) - 1e-9 <= persisted <= expected(before) + 1e-9
    assert ledger(store)["lambda_updated_at"]
    # The dual rose but the money gates are unchanged: this candidate is
    # budget-denied, exactly as it would have been with the index off.
    assert decision_map(store)["d" * 64] == "budget_denied"


def test_lambda_stays_zero_underpaced(tmp_path):
    store = make_store(tmp_path)
    enable(store, budget=100.0)
    seed_prefix(store, prefix_hash="e" * 64, customer="c1", tokens=100_000,
                arrival_count=10, bucket_count=10)
    result = claim(store, limit=10, index_enabled=True)
    assert len(result["rows"]) == 1
    assert ledger(store)["lambda_index"] == pytest.approx(0.0)
    row = decisions(store)[0]
    assert row["decision"] == "pinged"
    assert row["lambda_index"] == pytest.approx(0.0)


def test_lambda_denial_logged(tmp_path):
    store = make_store(tmp_path)
    enable(store, budget=100.0)
    for index in range(3):
        seed_prefix(store, prefix_hash=f"{index:064x}", customer=f"c{index}",
                    tokens=100_000, arrival_count=10, bucket_count=10)
    # Above every candidate's index by orders of magnitude, and the decay of one
    # under-paced step cannot bring it near them: the ceiling clamps it to 1000.
    prefund(store, lambda_index=100_000.0)

    result = claim(store, limit=10, index_enabled=True)
    assert result["rows"] == []
    rows = decisions(store)
    assert len(rows) == 3
    for row in rows:
        assert row["decision"] == "skipped_lambda"
        assert row["lambda_index"] == pytest.approx(1000.0)
        assert row["index_score"] == pytest.approx(1.0 / 0.11 - 1)
        assert row["claim_token"] is None
    assert ledger(store)["reserved_usd"] == pytest.approx(0.0)
    assert ledger(store)["spent_usd"] == pytest.approx(0.0)


def test_lambda_floor_zero(tmp_path):
    store = make_store(tmp_path)
    enable(store, budget=100.0)
    for cycle in range(5):
        seed_prefix(store, prefix_hash=f"{cycle:064x}", customer="c1",
                    tokens=1_000, arrival_count=10, bucket_count=10)
        claim(store, limit=10, index_enabled=True)
        assert ledger(store)["lambda_index"] == 0.0
    # The pure function agrees: no sequence of under-paced steps goes negative.
    lam = 0.0
    for step in range(50):
        lam = warm_lambda_update(lam, 0.0, 100.0, 0.5, 2.0, 1000.0)
        assert lam == 0.0


def test_lambda_update_is_multiplicative_and_capped():
    # Rises from exactly 0 (the multiplicative form is in (1 + lambda) space).
    assert warm_lambda_update(0.0, 10.0, 10.0, 0.0, 0.2, 1000.0) == pytest.approx(
        math.exp(0.2) - 1)
    # On pace is the fixed point: S == B*u leaves lambda exactly where it was.
    assert warm_lambda_update(3.0, 5.0, 10.0, 0.5, 0.2, 1000.0) == pytest.approx(3.0)
    # The ceiling binds before the exponent can run away.
    assert warm_lambda_update(900.0, 1000.0, 1.0, 0.0, 2.0, 1000.0) == 1000.0


def test_index_components_break_even_guard():
    # A provider whose reads are free has an unbounded ratio; it is clamped, not
    # divided by, and NO dollar component is derivable from it -- all three are
    # None rather than invented.
    degenerate = warm_index_components(0.5, 0.0, 0.375, ping_reserve_usd=0.375)
    assert degenerate["index_score"] == 1_000_000.0
    assert degenerate["v_hit_usd"] is None
    assert degenerate["c_belief_usd"] is None
    assert degenerate["chain_cost_usd"] is None
    # Expected dollars MOVED with the economics fix: they are priced from
    # ping_reserve_usd / write_multiplier (the prefix's base input dollars), not
    # from the floored ledger reservation. v_hit used to be reserve/b = 3.409,
    # which overstates what a return actually saves by 12x.
    normal = warm_index_components(0.5, 0.11, 0.375, ping_reserve_usd=0.375,
                                   write_multiplier=1.25)
    price_base = 0.375 / 1.25
    read_fraction = 0.11 / 1.11
    assert normal["index_score"] == pytest.approx(0.5 / 0.11 - 1)
    assert normal["v_hit_usd"] == pytest.approx(price_base * (1 - read_fraction))
    assert normal["c_belief_usd"] == pytest.approx(price_base * read_fraction)
    assert normal["chain_cost_usd"] == 0.0
    # The identity the equivalence theorem rests on: v_hit/c_belief = 1/b, so
    # the index is the same number whether it is computed from the dollars or
    # from the ratio.
    assert (normal["v_hit_usd"] / normal["c_belief_usd"]
            == pytest.approx(1 / 0.11))
    # The reservation is NOT the economics; it is only the density denominator.
    assert normal["index_density"] == pytest.approx(
        normal["index_score"] / 0.375)


def test_write_multiplier_mirrors_the_reserve_producer():
    """Mirrors the ping_rate CASE in api/server.py that priced ping_reserve_usd."""
    from api.store import warm_write_multiplier
    assert warm_write_multiplier("anthropic", 300) == 1.25
    assert warm_write_multiplier("anthropic", 3600) == 2.0
    assert warm_write_multiplier("deepseek", 14_400) == 1.0


# ---------------------------------------------------------------------------
# Worker wiring.
# ---------------------------------------------------------------------------

def test_claim_kwargs_env_parsing(monkeypatch):
    for name in ("BREVITAS_WARM_INDEX", "BREVITAS_WARM_LAMBDA_ETA",
                 "BREVITAS_WARM_LAMBDA_MAX"):
        monkeypatch.delenv(name, raising=False)
    kwargs = _warm_claim_kwargs()
    assert kwargs["index_enabled"] is False
    assert kwargs["lambda_eta"] == pytest.approx(0.2)
    assert kwargs["lambda_max"] == pytest.approx(1000.0)

    monkeypatch.setenv("BREVITAS_WARM_INDEX", "true")
    monkeypatch.setenv("BREVITAS_WARM_LAMBDA_ETA", "99")
    monkeypatch.setenv("BREVITAS_WARM_LAMBDA_MAX", "-5")
    clamped = _warm_claim_kwargs()
    assert clamped["index_enabled"] is True
    assert clamped["lambda_eta"] == pytest.approx(2.0)
    assert clamped["lambda_max"] == pytest.approx(0.0)

    monkeypatch.setenv("BREVITAS_WARM_INDEX", "0")
    monkeypatch.setenv("BREVITAS_WARM_LAMBDA_ETA", "not-a-number")
    assert _warm_claim_kwargs()["index_enabled"] is False
    assert _warm_claim_kwargs()["lambda_eta"] == pytest.approx(0.2)


def test_claim_rejects_out_of_range_pacing_arguments(tmp_path):
    store = make_store(tmp_path)
    enable(store)
    with pytest.raises(ValueError):
        claim(store, limit=10, index_enabled=True, lambda_eta=0.0)
    with pytest.raises(ValueError):
        claim(store, limit=10, index_enabled=True, lambda_max=1_000_001)
    # None coalesces to the RPC's own defaults rather than failing, so a caller
    # that predates Phase 1 cannot fail a claim.
    assert claim(store, limit=10, lambda_eta=None, lambda_max=None)["rows"] == []
