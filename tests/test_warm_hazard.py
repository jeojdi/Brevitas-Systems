"""The decayed hierarchical hazard, P(alive), the keep-alive chain and the
periodicity flag (migration 202608100003, mirrored in api/store.py).

The load-bearing test here is test_hazard_v2_no_state_falls_back_flat: a
customer the model has never seen -- or one erased and suppressed -- must be
scored exactly as v1 scored them, because that fallback is the cold-start story
for the whole hazard model. Task A proved the flat-prior index equals v1; this
file proves the hazard path lands on those flat priors whenever it has nothing
to say, so the equivalence chains all the way through.
"""
import json
import math
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from api.store import (WARM_ORG_AGGREGATE_CUSTOMER_ID, UsageStore,
                       _utc_hour_bucket, warm_chain_pings, warm_hazard_rate,
                       warm_hazard_touch, warm_p_alive, warm_p_return_v2,
                       warm_regime_classify)
from api.worker import _warm_claim_kwargs

ORG = "org-hazard-1"
BREAK_EVEN = {"anthropic": 0.11, "deepseek": round(0.02 / 0.98, 6)}
# The uniform 1-arrival-per-week prior, and the org-prior weight in hours.
ALPHA0, BETA0, KAPPA = 0.25, 42.0, 8.0


def make_store(tmp_path, name="hazard.db"):
    return UsageStore(str(tmp_path / name))


def enable(store, provider="anthropic", budget=10.0, max_customers=1000,
           max_pings=288, org=ORG):
    return store.warm_credentials_upsert(
        org, provider, "enc:credential", True, "actor-1", budget,
        max_customers, max_pings)


def seed_prefix(store, *, prefix_hash, customer, provider="anthropic",
                tokens=100_000, arrival_count=10, bucket_count=5,
                reserve=None, due_offset_s=-1, org=ORG, consecutive_misses=0):
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
            "VALUES(?,?,?,?,?,?,?,?,NULL,?,?,0,'','active',?,?,?,?,?)",
            (org, customer, provider, prefix_hash, "enc:payload", int(tokens),
             300, int(arrival_count), json.dumps({bucket: bucket_count}),
             int(consecutive_misses), now.isoformat(), now.isoformat(),
             (now + timedelta(seconds=due_offset_s)).isoformat(),
             (now + timedelta(days=1)).isoformat(), float(reserve)))
    return reserve


def seed_state(store, *, customer, provider="anthropic", n_b=0.0, e_b=0.0,
               events_total=1, first_seen_days_ago=30.0,
               last_seen_seconds_ago=1.0, org=ORG, bucket=None):
    """Write one warm_customer_state row the observer would eventually produce.

    warm_prefix_observe cannot express an arbitrary (n_b, e_b) pair -- it always
    credits exactly one arrival at the current bucket -- so the fixture writes
    the posterior directly, exactly as the prefix fixtures do.
    """
    now = datetime.now(timezone.utc)
    key = bucket or _utc_hour_bucket(now)
    first_seen = now - timedelta(days=float(first_seen_days_ago))
    last_seen = now - timedelta(seconds=float(last_seen_seconds_ago))
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO warm_customer_state(organization_id,customer_id,"
            "provider,hazard_n,hazard_e,events_total,first_seen_at,last_seen_at,"
            "last_update_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (org, customer, provider, json.dumps({key: float(n_b)}),
             json.dumps({key: float(e_b)}), int(events_total),
             first_seen.isoformat(), last_seen.isoformat(),
             last_seen.isoformat(), now.isoformat(), now.isoformat()))


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


def state_row(store, customer, provider="anthropic", org=ORG):
    return store.warm_customer_state_get(org, customer, provider)


# ---------------------------------------------------------------------------
# The touch: decay, exposure accrual, sparsity.
# ---------------------------------------------------------------------------

def test_hazard_touch_decay_and_accrual():
    # Aligned to the hour so the walk credits whole hours and the arithmetic is
    # exact rather than approximately exact.
    start = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
    first = warm_hazard_touch(None, start)
    assert first["hazard_n"] == {_utc_hour_bucket(start): 1.0}
    # A first arrival has no exposure history to accrue over.
    assert first["hazard_e"] == {}
    assert first["events_total"] == 1

    # Exactly two weeks: one half-life, and 336 hours == 2 whole weeks, so the
    # arrival lands back in the same hour-of-week bucket.
    later = start + timedelta(days=14)
    assert _utc_hour_bucket(later) == _utc_hour_bucket(start)
    second = warm_hazard_touch(first, later)
    # 1 decayed to 0.5, plus this arrival.
    assert second["hazard_n"][_utc_hour_bucket(start)] == pytest.approx(1.5, abs=1e-9)
    assert sum(second["hazard_e"].values()) == pytest.approx(336.0, abs=1e-9)
    # 336 hours over two whole weeks: every bucket gets exactly two.
    assert len(second["hazard_e"]) == 168
    assert all(value == pytest.approx(2.0, abs=1e-9)
               for value in second["hazard_e"].values())
    assert second["events_total"] == 2
    assert second["first_seen_at"] == start
    assert second["last_seen_at"] == later

    # Sparsity: a bucket already below the floor after decay is dropped rather
    # than carried forever at eleven decimal places of nothing.
    seeded = dict(second)
    seeded["hazard_n"] = dict(second["hazard_n"])
    seeded["hazard_n"]["99"] = 1.5e-6
    third = warm_hazard_touch(seeded, later + timedelta(days=14))
    assert "99" not in third["hazard_n"]


def test_hazard_touch_long_gap_uniform_exposure():
    start = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
    first = warm_hazard_touch(None, start)
    # 90 days > the 672-hour walk bound: every bucket gets the decayed
    # exposure supremum spread uniformly instead of 2160 loop iterations.
    later = start + timedelta(days=90)
    second = warm_hazard_touch(first, later)
    assert len(second["hazard_e"]) == 168
    for value in second["hazard_e"].values():
        assert value == pytest.approx(484.8 / 168.0, abs=1e-9)


# ---------------------------------------------------------------------------
# The read side, against hand arithmetic.
# ---------------------------------------------------------------------------

def test_p_return_v2_shrinkage_exact():
    n_b, e_b, org_n, org_e = 12.0, 30.0, 400.0, 900.0
    h_org = (org_n + ALPHA0) / (org_e + BETA0)
    h = (n_b + KAPPA * h_org) / (e_b + KAPPA)
    assert warm_hazard_rate(n_b, e_b, org_n, org_e) == pytest.approx(h, abs=1e-12)
    for ttl in (300, 3600, 14_400):
        assert warm_p_return_v2(n_b, e_b, org_n, org_e, ttl) == pytest.approx(
            1.0 - math.exp(-h * ttl / 3600.0), abs=1e-12)
    # No organization row at all: the org term degenerates to the uniform
    # 1-arrival-per-week prior rather than to a division by zero.
    assert warm_hazard_rate(0.0, 0.0, 0.0, 0.0) == pytest.approx(
        (KAPPA * (ALPHA0 / BETA0)) / KAPPA, abs=1e-12)


def test_p_alive_closed_form():
    def closed_form(x, t_x, horizon):
        z = (math.log(1.0 / (2.5 + max(x - 1, 0)))
             + min(50.0, 0.5 + x) * math.log((7.0 + horizon) / (7.0 + t_x)))
        return max(0.01, min(1.0, 1.0 / (1.0 + math.exp(z))))

    assert warm_p_alive(5, 10.0, 12.0) == pytest.approx(
        closed_form(5, 10.0, 12.0), abs=1e-12)
    assert warm_p_alive(1, 0.0, 30.0) == pytest.approx(
        closed_form(1, 0.0, 30.0), abs=1e-12)
    # Monotone decreasing in the silence T - t_x, and floored at 1%: a customer
    # that goes quiet becomes cheap to skip, never permanently unscorable, which
    # is exactly what the stop-loss counter got wrong.
    silences = [warm_p_alive(30, 20.0, 20.0 + gap)
                for gap in (0.0, 5.0, 20.0, 200.0, 5000.0)]
    assert silences == sorted(silences, reverse=True)
    assert silences[-1] == pytest.approx(0.01)
    assert all(0.01 <= value <= 1.0 for value in silences)


def test_chain_truncation_index_negative_beyond_imax():
    break_even = 0.11
    fraction = break_even / (1.0 + break_even)
    tau = 300 - 60
    i_max = tau * (1.0 / fraction - 1.0)
    # A rate this low puts the expected next arrival far past I_max, so the
    # chain truncates at the point where its cost exceeds anything a hit could
    # save.
    rate = 1e-4
    chain = warm_chain_pings(rate, 0.0, 300, 60, break_even)
    assert chain == max(0, math.ceil(i_max / tau) - 1)
    # THE PROPERTY: past I_max the chain alone drives the index below zero for
    # every attainable p_eff, so "never warm a dead session" is arithmetic.
    assert 1.0 / break_even - 1 - chain < 0

    # And a session about to return commits to no chain at all.
    assert warm_chain_pings(3600.0 / 100.0, 0.0, 300, 60, break_even) == 0


# ---------------------------------------------------------------------------
# The write side: what an arrival puts in the table.
# ---------------------------------------------------------------------------

def observe(store, customer, prefix_hash, org=ORG, provider="anthropic"):
    return store.warm_prefix_observe(
        org, customer, provider, prefix_hash, "enc:payload", 100_000, 300, 60,
        False, ping_reserve_usd=0.375, model_class="claude-sonnet-4-5")


def test_observe_writes_customer_and_org_state(tmp_path):
    store = make_store(tmp_path)
    enable(store)
    observe(store, "c1", "a" * 64)
    observe(store, "c2", "b" * 64)
    observe(store, "c1", "a" * 64)

    first = state_row(store, "c1")
    second = state_row(store, "c2")
    aggregate = state_row(store, WARM_ORG_AGGREGATE_CUSTOMER_ID)
    assert first["events_total"] == 2
    assert second["events_total"] == 1
    # The aggregate is the sum over customers, which is what the per-customer
    # posterior shrinks toward.
    assert aggregate["events_total"] == 3
    bucket = _utc_hour_bucket(datetime.now(timezone.utc))
    assert first["hazard_n"][bucket] == pytest.approx(2.0, abs=1e-6)
    assert aggregate["hazard_n"][bucket] == pytest.approx(3.0, abs=1e-6)
    assert first["regime"] == "unknown"


def test_suppression_blocks_state_writes_and_reads(tmp_path):
    store = make_store(tmp_path)
    enable(store)
    observe(store, "c1", "a" * 64)
    observe(store, "c2", "b" * 64)
    assert state_row(store, "c1") is not None
    aggregate_before = state_row(store, WARM_ORG_AGGREGATE_CUSTOMER_ID)

    store.warm_suppression_add(ORG, "c1")
    # The state this subject accumulated goes with the request.
    assert state_row(store, "c1") is None
    assert [row["customer_id"] for row in store.warm_suppression_list(ORG)] == ["c1"]

    # And the next arrival does NOT rebuild it -- not for the customer and not
    # through the organization aggregate, which would otherwise be where an
    # erased subject's behaviour survived erasure.
    observe(store, "c1", "a" * 64)
    assert state_row(store, "c1") is None
    aggregate_after = state_row(store, WARM_ORG_AGGREGATE_CUSTOMER_ID)
    assert aggregate_after["events_total"] == aggregate_before["events_total"]

    # An unsuppressed sibling is untouched by any of this.
    observe(store, "c2", "b" * 64)
    assert state_row(store, "c2")["events_total"] == 2

    # And the scorer treats the suppressed customer as one it has never seen:
    # flat priors, not a stale posterior.
    seed_state(store, customer="c1", n_b=20.0, e_b=5.0, events_total=20)
    seed_prefix(store, prefix_hash="c" * 64, customer="c1", arrival_count=10,
                bucket_count=10)
    result = claim(store, limit=10, index_enabled=True, hazard_v2=True)
    assert len(result["rows"]) == 1
    scored = [row for row in decisions(store) if row["prefix_hash"] == "c" * 64]
    assert scored[-1]["p_alive"] == pytest.approx(1.0)
    assert scored[-1]["chain_cost_usd"] == pytest.approx(0.0)
    # The lifetime histogram ratio, not the seeded posterior.
    assert scored[-1]["p_return"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# The claim path.
# ---------------------------------------------------------------------------

def _stop_loss_fixture(tmp_path, name, *, with_state):
    store = make_store(tmp_path, name)
    enable(store, budget=1000.0)
    seed_prefix(store, prefix_hash="a" * 64, customer="c1", arrival_count=10,
                bucket_count=10)
    # Retired by the v1 stop-loss (3) and therefore never even a candidate.
    seed_prefix(store, prefix_hash="b" * 64, customer="c2", arrival_count=10,
                bucket_count=10, consecutive_misses=5)
    if with_state:
        for customer in ("c1", "c2"):
            seed_state(store, customer=customer, n_b=20.0, e_b=5.0,
                       events_total=20, first_seen_days_ago=30.0,
                       last_seen_seconds_ago=1.0)
    return store


def test_hazard_v2_off_is_v1(tmp_path):
    bare = _stop_loss_fixture(tmp_path, "bare.db", with_state=False)
    bare_result = claim(bare, limit=10, index_enabled=True, hazard_v2=False)
    stateful = _stop_loss_fixture(tmp_path, "stateful.db", with_state=True)
    stateful_result = claim(stateful, limit=10, index_enabled=True,
                            hazard_v2=False)

    # With the flag off the state rows are dead weight: present or absent, the
    # claim cannot tell.
    assert decision_map(bare) == decision_map(stateful)
    assert ({row["prefix_hash"] for row in bare_result["rows"]}
            == {row["prefix_hash"] for row in stateful_result["rows"]})
    # And the stop-lossed prefix is still excluded from the candidate query
    # entirely -- not denied, never scored.
    assert "b" * 64 not in decision_map(stateful)
    assert decision_map(stateful)["a" * 64] == "pinged"
    for row in decisions(stateful):
        assert row["p_alive"] == pytest.approx(1.0)
        assert row["chain_cost_usd"] == pytest.approx(0.0)


def test_hazard_v2_on_scores_stop_lossed_arm(tmp_path):
    store = _stop_loss_fixture(tmp_path, "scored.db", with_state=True)
    claim(store, limit=10, index_enabled=True, hazard_v2=True)
    verdicts = decision_map(store)
    # The retired prefix is scored again. P(alive), not a monotone counter, is
    # what decides whether it is worth warming.
    assert "b" * 64 in verdicts

    for row in decisions(store):
        break_even = BREAK_EVEN[row["provider"]]
        assert row["p_alive"] is not None and row["p_alive"] < 1.0
        assert row["chain_cost_usd"] > 0
        # chain_cost is n_chain PINGS priced at c_belief each -- the expected
        # keep-alive dollars -- not at the (floored) ledger reservation, which
        # is money safety for the daily ceiling and a different quantity.
        chain = round(row["chain_cost_usd"] / row["c_belief_usd"])
        # THE INDEX COMPOSITION: p_eff/b - 1 - n_chain, with p_eff carrying
        # P(alive) and the chain pricing the whole commitment.
        assert row["index_score"] == pytest.approx(
            row["p_return"] * row["p_alive"] / break_even - 1 - chain, abs=1e-9)
        # p_return is the hazard posterior, not the histogram ratio (which is
        # 1.0 for this fixture), and it depends on no wall clock.
        assert row["p_return"] == pytest.approx(
            warm_p_return_v2(20.0, 5.0, 0.0, 0.0, 300), abs=1e-9)
        assert row["p_return"] < 1.0


def test_hazard_v2_without_index_keeps_stop_loss(tmp_path):
    """FIX 3: hazard_v2 is an EXTENSION of the index policy, inert without it.

    The stop-loss is retired in favour of P(alive) -- but P(alive) only ever
    reaches a decision through the index block, which runs under index_enabled.
    With the hazard flag on and the index flag off, retiring the counter would
    remove the churn stop-loss and put NOTHING in its place, and a dead prefix
    would be re-claimed forever. So the predicate requires BOTH flags.
    """
    store = _stop_loss_fixture(tmp_path, "hazard-no-index.db", with_state=True)
    result = claim(store, limit=10, index_enabled=False, hazard_v2=True)

    verdicts = decision_map(store)
    # consecutive_misses (5) >= stop_loss (3): filtered out of the candidate
    # query entirely -- not denied, never scored, never claimed.
    assert "b" * 64 not in verdicts
    assert {row["prefix_hash"] for row in result["rows"]} == {"a" * 64}
    # And nothing about the index machinery ran, so the row is a v1 row.
    for row in decisions(store):
        assert row["index_score"] is None
        assert row["p_alive"] is None

    # The SAME fixture with the index on does retire it, which is what makes
    # the assertion above about the flag pair rather than about the fixture.
    both = _stop_loss_fixture(tmp_path, "hazard-and-index.db", with_state=True)
    claim(both, limit=10, index_enabled=True, hazard_v2=True)
    assert "b" * 64 in decision_map(both)


def test_hazard_v2_no_state_falls_back_flat(tmp_path):
    """THE COLD-START STORY, executable.

    A customer with no state row is scored exactly as v1 scored them, so
    turning the hazard flag on cannot move a single decision until the model
    has actually learned something.
    """
    off = make_store(tmp_path, "flat-off.db")
    on = make_store(tmp_path, "flat-on.db")
    rng = random.Random(20260810)
    plan = [(f"{index:064x}", f"customer-{index % 12}",
             rng.randint(1, 200), rng.randint(0, 40)) for index in range(60)]
    for store in (off, on):
        enable(store, budget=1000.0)
        for prefix_hash, customer, arrivals, bucket_count in plan:
            seed_prefix(store, prefix_hash=prefix_hash, customer=customer,
                        arrival_count=arrivals,
                        bucket_count=min(bucket_count, arrivals))

    off_result = claim(off, limit=500, index_enabled=True, hazard_v2=False)
    on_result = claim(on, limit=500, index_enabled=True, hazard_v2=True)

    assert decision_map(on) == decision_map(off)
    assert ({row["prefix_hash"] for row in on_result["rows"]}
            == {row["prefix_hash"] for row in off_result["rows"]})
    assert on_result["rows"], "the fallback fixture claimed nothing and is vacuous"
    for row in decisions(on):
        assert row["p_alive"] == pytest.approx(1.0)
        assert row["chain_cost_usd"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# The regime flag. Informational in Phase 1: written, and read by nobody.
# ---------------------------------------------------------------------------

def test_regime_classify_daily_weekly_aperiodic_unknown():
    hours = 672
    # A working-hours shape: eight active hours a day, every day.
    daily = [5 if 9 <= (hour % 24) < 17 else 0 for hour in range(hours)]
    regime, score = warm_regime_classify(daily)
    assert regime == "periodic_daily"
    assert score >= 0.4

    weekly = [5 if hour % 168 == 30 else 0 for hour in range(hours)]
    # Four weekly spikes is fewer than 20 populated hours, so the series is
    # 'unknown' until it has enough to say anything -- absence of evidence.
    assert warm_regime_classify(weekly)[0] == "unknown"
    # A weekly BURST (one busy day a week) clears the population floor and
    # correlates at 168 but not at 24.
    weekly_burst = [3 if (hour % 168) < 24 else 0 for hour in range(hours)]
    regime, score = warm_regime_classify(weekly_burst)
    assert regime == "periodic_weekly"
    assert score >= 0.4

    # A DOCUMENTED PROPERTY of winsorizing at the 95th percentile, recorded
    # here rather than discovered later: a series active in under 5% of its
    # hours has a 95th percentile of zero, so every value is clipped to zero,
    # the variance vanishes and the label is 'aperiodic' with score 0 no matter
    # how regular the spikes are. Twenty-eight single-hour daily spikes over 28
    # days is exactly that series (4.2% populated). The floor for calling
    # something periodic is therefore "active in at least 5% of hours", not the
    # 20-nonzero population gate, and a sparser customer is simply never
    # labelled periodic. Phase 1 reads the label nowhere, so this costs nothing
    # today; the fast-path scheduler that will read it must not treat
    # 'aperiodic' as evidence of aperiodicity for a sparse series.
    sparse_spikes = [5 if hour % 24 == 9 else 0 for hour in range(hours)]
    assert sum(1 for value in sparse_spikes if value) >= 20
    assert warm_regime_classify(sparse_spikes) == ("aperiodic", 0.0)

    rng = random.Random(4242)
    noisy = [rng.randint(0, 4) for _ in range(hours)]
    assert warm_regime_classify(noisy)[0] == "aperiodic"

    assert warm_regime_classify([0] * hours) == ("unknown", 0.0)
    assert warm_regime_classify([1] * 19 + [0] * (hours - 19)) == ("unknown", 0.0)
    # Populated but perfectly flat: no variance to correlate.
    assert warm_regime_classify([1] * hours) == ("aperiodic", 0.0)


def test_arrival_buckets_skip_aggregate_and_suppressed(tmp_path):
    store = make_store(tmp_path)
    enable(store)
    observe(store, "c1", "a" * 64)
    observe(store, "c2", "b" * 64)
    store.warm_suppression_add(ORG, "c2")

    series = store.warm_customer_arrival_buckets(lookback_days=28, limit=100)
    keys = {(entry["customer_id"], entry["provider"]) for entry in series}
    assert keys == {("c1", "anthropic")}
    assert len(series[0]["counts"]) == 28 * 24

    # A freshly labelled row drops out of the next scan.
    assert store.warm_customer_state_set_regime(
        ORG, "c1", "anthropic", "aperiodic", 0.1)["status"] == "recorded"
    assert store.warm_customer_arrival_buckets(lookback_days=28, limit=100) == []
    # A label for a customer with no state row is refused as a no-op rather
    # than inventing the row.
    assert store.warm_customer_state_set_regime(
        ORG, "nobody", "anthropic", "aperiodic")["status"] == "missing"
    with pytest.raises(ValueError):
        store.warm_customer_state_set_regime(ORG, "c1", "anthropic", "bogus")


def test_claim_kwargs_hazard_env_parsing(monkeypatch):
    monkeypatch.delenv("BREVITAS_WARM_HAZARD_V2", raising=False)
    assert _warm_claim_kwargs()["hazard_v2"] is False
    monkeypatch.setenv("BREVITAS_WARM_HAZARD_V2", "true")
    assert _warm_claim_kwargs()["hazard_v2"] is True
    monkeypatch.setenv("BREVITAS_WARM_HAZARD_V2", "nonsense")
    assert _warm_claim_kwargs()["hazard_v2"] is False
