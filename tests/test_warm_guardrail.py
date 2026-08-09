"""The beta spend cap, its control-savings producer and the conservative
guardrail (migration 202608100005, mirrored in api/store.py and api/worker.py).

Three things that bound what the learner is allowed to COST, none of which make
it smarter. The first test in each section is the default-off story: at
beta = 0 and with an empty warm_org_mode the claim path must be byte-for-byte
what it was before this migration, because "the operator did not ask for a cap"
and "the operator asked for a cap of zero" are opposite instructions and the
second one denies everything.

Every test here is offline: no HTTP, no provider key, no live call.
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from api import worker
from api.store import UsageStore, _utc_hour_bucket

ORG = "org-guardrail-1"
ORG_B = "org-guardrail-2"
BREAK_EVEN = {"anthropic": 0.11, "deepseek": round(0.02 / 0.98, 6)}
CUSTOMER_A = "11111111-1111-4111-8111-111111111111"
CUSTOMER_B = "22222222-2222-4222-8222-222222222222"


def make_store(tmp_path, name="guardrail.db"):
    return UsageStore(str(tmp_path / name))


def enable(store, provider="anthropic", budget=10.0, org=ORG):
    return store.warm_credentials_upsert(
        org, provider, "enc:credential", True, "actor-1", budget, 1000, 288)


def seed_prefix(store, *, prefix_hash, customer, provider="anthropic",
                tokens=100_000, arrival_count=10, bucket_count=10,
                reserve=None, misses=0, due_offset_s=-1, org=ORG):
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
             300, int(arrival_count), None, json.dumps({bucket: bucket_count}),
             int(misses), now.isoformat(), now.isoformat(),
             (now + timedelta(seconds=due_offset_s)).isoformat(),
             (now + timedelta(days=1)).isoformat(), float(reserve)))
    return reserve


def seed_savings(store, *, day_offset, savings, provider="anthropic", org=ORG,
                 treated=5, control=5):
    day = (datetime.now(timezone.utc).date() - timedelta(days=day_offset)).isoformat()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO warm_control_savings_daily(organization_id,"
            "provider,day,treated_units,control_units,treated_mean_cost_usd,"
            "control_mean_cost_usd,control_lift_usd,computed_at) "
            "VALUES(?,?,?,?,?,0,0,?,?)",
            (org, provider, day, int(treated), int(control), float(savings),
             datetime.now(timezone.utc).isoformat()))
    return day


def seed_ledger(store, *, day_offset, reserved=0.0, spent=0.0,
                provider="anthropic", org=ORG):
    now = datetime.now(timezone.utc)
    day = (now.date() - timedelta(days=day_offset)).isoformat()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO warm_budget_ledger(organization_id,provider,day,"
            "reserved_usd,spent_usd,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(organization_id,provider,day) DO UPDATE SET "
            "reserved_usd=excluded.reserved_usd,spent_usd=excluded.spent_usd",
            (org, provider, day, float(reserved), float(spent), now.isoformat()))
    return day


def seed_usage(store, *, prefix_hash, customer, cost, day_offset,
               provider="anthropic", org=ORG, strategy="cache_hit",
               authoritative=1):
    ts = (datetime.now(timezone.utc) - timedelta(days=day_offset)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO usage_log(organization_id,customer_id,key_hash,"
            "provider,model,strategy,actual_cost_usd,authoritative,"
            "warm_prefix_hash,ts) VALUES(?,?,'kh',?,?,?,?,?,?,?)",
            (org, customer, provider, "m", strategy, float(cost),
             int(authoritative), prefix_hash, ts.isoformat()))


def seed_decision(store, *, prefix_hash, customer, decision, day_offset,
                  provider="anthropic", org=ORG, realized_net=None):
    ts = (datetime.now(timezone.utc) - timedelta(days=day_offset)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO warm_decision_log(organization_id,customer_id,provider,"
            "prefix_hash,decision,p_return,roi_floor,reserve_usd,prefix_tokens,"
            "arrival_count,realized_net_usd,ts) VALUES(?,?,?,?,?,0.5,0.11,0.1,"
            "1000,10,?,?)",
            (org, customer, provider, prefix_hash, decision,
             (None if realized_net is None else float(realized_net)),
             ts.isoformat()))


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


def decision_rows(store):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT * FROM warm_decision_log ORDER BY id")]


def savings_row(store, day, provider="anthropic", org=ORG):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM warm_control_savings_daily WHERE organization_id=? "
            "AND provider=? AND day=?", (org, provider, day)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# The default state: beta 0, no mode rows, nothing changes.
# ---------------------------------------------------------------------------

def test_beta_zero_gate_absent(tmp_path):
    """beta=0 means the cap does not RUN. Even with control savings of exactly
    zero on file -- which as a literal cap would deny everything -- the claim
    path is what it was before this migration."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    seed_prefix(store, prefix_hash="b" * 64, customer=CUSTOMER_B)
    seed_savings(store, day_offset=3, savings=0.0)

    baseline = claim(store)
    assert len(baseline["rows"]) == 2
    assert set(decision_map(store).values()) == {"pinged"}

    other = make_store(tmp_path, "beta-zero.db")
    enable(other, budget=10.0)
    seed_prefix(other, prefix_hash="a" * 64, customer=CUSTOMER_A)
    seed_prefix(other, prefix_hash="b" * 64, customer=CUSTOMER_B)
    seed_savings(other, day_offset=3, savings=0.0)
    explicit = claim(other, beta=0.0)
    assert len(explicit["rows"]) == 2
    assert decision_map(other) == decision_map(store)
    assert not [row for row in decision_rows(other)
                if row["decision"] == "beta_denied"]


def test_beta_cap_denies_exact_boundary(tmp_path):
    """C28 = 1.00 and beta = 0.5 gives a cap of 0.50. Ledger spend of 0.49 plus
    a 0.02 reservation crosses it; 0.40 does not."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                reserve=0.02, tokens=1000)
    seed_savings(store, day_offset=3, savings=1.0)
    seed_ledger(store, day_offset=5, spent=0.49)

    assert claim(store, beta=0.5)["rows"] == []
    assert decision_map(store)["a" * 64] == "beta_denied"

    under = make_store(tmp_path, "beta-under.db")
    enable(under, budget=10.0)
    seed_prefix(under, prefix_hash="a" * 64, customer=CUSTOMER_A,
                reserve=0.02, tokens=1000)
    seed_savings(under, day_offset=3, savings=1.0)
    seed_ledger(under, day_offset=5, spent=0.40)
    assert len(claim(under, beta=0.5)["rows"]) == 1
    assert decision_map(under)["a" * 64] == "pinged"


def test_beta_cap_in_invocation_accounting(tmp_path):
    """Two candidates whose combined reservations cross the cap: the first is
    admitted and the second denied, from ONE ledger read. Without the local
    W28 bump both would be measured against the same pre-batch sum."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                reserve=0.30, tokens=1000)
    seed_prefix(store, prefix_hash="b" * 64, customer=CUSTOMER_B,
                reserve=0.30, tokens=1000)
    seed_savings(store, day_offset=3, savings=1.0)

    result = claim(store, beta=0.5)
    assert len(result["rows"]) == 1
    decisions = decision_map(store)
    assert sorted(decisions.values()) == ["beta_denied", "pinged"]


def test_beta_cap_window_excludes_open_days(tmp_path):
    """Savings on today and yesterday do not count -- their usage rows have not
    settled. day-28 counts; day-29 has aged out."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                reserve=0.02, tokens=1000)
    seed_savings(store, day_offset=0, savings=100.0)
    seed_savings(store, day_offset=1, savings=100.0)
    seed_savings(store, day_offset=29, savings=100.0)
    # Nothing inside [day-28, day-2], so the cap is 0 and everything is denied.
    assert claim(store, beta=0.5)["rows"] == []
    assert decision_map(store)["a" * 64] == "beta_denied"

    edge = make_store(tmp_path, "beta-edge.db")
    enable(edge, budget=10.0)
    seed_prefix(edge, prefix_hash="a" * 64, customer=CUSTOMER_A,
                reserve=0.02, tokens=1000)
    seed_savings(edge, day_offset=28, savings=1.0)
    assert len(claim(edge, beta=0.5)["rows"]) == 1


def test_beta_cap_bounds_rejected(tmp_path):
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    with pytest.raises(ValueError):
        claim(store, beta=101.0)
    with pytest.raises(ValueError):
        claim(store, beta=-1.0)


# ---------------------------------------------------------------------------
# The producer.
# ---------------------------------------------------------------------------

def test_control_savings_math(tmp_path):
    """Three treated units at $1 and three control units at $3: savings are
    (3 - 1) x 3 = 6, and the counts and means are exact."""
    store = make_store(tmp_path)
    for index in range(3):
        prefix = f"{index:064x}"
        seed_decision(store, prefix_hash=prefix, customer=CUSTOMER_A,
                      decision="pinged", day_offset=3)
        seed_usage(store, prefix_hash=prefix, customer=CUSTOMER_A, cost=1.0,
                   day_offset=3)
    for index in range(3, 6):
        prefix = f"{index:064x}"
        seed_decision(store, prefix_hash=prefix, customer=CUSTOMER_B,
                      decision="holdout", day_offset=3)
        seed_usage(store, prefix_hash=prefix, customer=CUSTOMER_B, cost=3.0,
                   day_offset=3)

    store.warm_control_savings_refresh(28)
    day = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    row = savings_row(store, day)
    assert row["treated_units"] == 3
    assert row["control_units"] == 3
    assert row["treated_mean_cost_usd"] == pytest.approx(1.0)
    assert row["control_mean_cost_usd"] == pytest.approx(3.0)
    assert row["control_lift_usd"] == pytest.approx(6.0)


def test_control_savings_underpowered_is_zero_with_counts(tmp_path):
    """Two control units is under three: savings are forced to zero, but the
    counts survive so a starved comparison is distinguishable from a real one
    that measured nothing."""
    store = make_store(tmp_path)
    for index in range(3):
        prefix = f"{index:064x}"
        seed_decision(store, prefix_hash=prefix, customer=CUSTOMER_A,
                      decision="pinged", day_offset=3)
        seed_usage(store, prefix_hash=prefix, customer=CUSTOMER_A, cost=1.0,
                   day_offset=3)
    for index in range(3, 5):
        prefix = f"{index:064x}"
        seed_decision(store, prefix_hash=prefix, customer=CUSTOMER_B,
                      decision="holdout", day_offset=3)
        seed_usage(store, prefix_hash=prefix, customer=CUSTOMER_B, cost=9.0,
                   day_offset=3)

    store.warm_control_savings_refresh(28)
    day = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    row = savings_row(store, day)
    assert row["treated_units"] == 3
    assert row["control_units"] == 2
    assert row["control_lift_usd"] == pytest.approx(0.0)


def test_control_savings_never_negative(tmp_path):
    """Treated cost above control cost is clamped to zero savings, never a
    negative that would eat into the cap from the other direction."""
    store = make_store(tmp_path)
    for index in range(3):
        prefix = f"{index:064x}"
        seed_decision(store, prefix_hash=prefix, customer=CUSTOMER_A,
                      decision="pinged", day_offset=3)
        seed_usage(store, prefix_hash=prefix, customer=CUSTOMER_A, cost=5.0,
                   day_offset=3)
    for index in range(3, 6):
        prefix = f"{index:064x}"
        seed_decision(store, prefix_hash=prefix, customer=CUSTOMER_B,
                      decision="holdout", day_offset=3)
        seed_usage(store, prefix_hash=prefix, customer=CUSTOMER_B, cost=1.0,
                   day_offset=3)
    store.warm_control_savings_refresh(28)
    day = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    assert savings_row(store, day)["control_lift_usd"] == pytest.approx(0.0)


def test_control_savings_ignores_warm_and_non_authoritative_usage(tmp_path):
    """Only authoritative non-warm rows are the cost of a unit: warm spend is
    the thing being paid for, and a non-authoritative row is not evidence."""
    store = make_store(tmp_path)
    for index in range(3):
        prefix = f"{index:064x}"
        seed_decision(store, prefix_hash=prefix, customer=CUSTOMER_A,
                      decision="pinged", day_offset=3)
        seed_usage(store, prefix_hash=prefix, customer=CUSTOMER_A, cost=1.0,
                   day_offset=3)
        seed_usage(store, prefix_hash=prefix, customer=CUSTOMER_A, cost=50.0,
                   day_offset=3, strategy="cache_warm")
        seed_usage(store, prefix_hash=prefix, customer=CUSTOMER_A, cost=50.0,
                   day_offset=3, authoritative=0)
    for index in range(3, 6):
        prefix = f"{index:064x}"
        seed_decision(store, prefix_hash=prefix, customer=CUSTOMER_B,
                      decision="holdout", day_offset=3)
        seed_usage(store, prefix_hash=prefix, customer=CUSTOMER_B, cost=3.0,
                   day_offset=3)
    store.warm_control_savings_refresh(28)
    day = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    row = savings_row(store, day)
    assert row["treated_mean_cost_usd"] == pytest.approx(1.0)
    assert row["control_lift_usd"] == pytest.approx(6.0)


def test_control_savings_mixed_unit_counts_as_treated(tmp_path):
    """A unit with both a pinged and a holdout row is TREATED, and the mixed
    count is reported. Counting it as control would understate treated cost and
    manufacture savings, which raises a spend ceiling."""
    store = make_store(tmp_path)
    prefix = "f" * 64
    seed_decision(store, prefix_hash=prefix, customer=CUSTOMER_A,
                  decision="pinged", day_offset=3)
    seed_decision(store, prefix_hash=prefix, customer=CUSTOMER_A,
                  decision="holdout", day_offset=3)
    seed_usage(store, prefix_hash=prefix, customer=CUSTOMER_A, cost=1.0,
               day_offset=3)
    result = store.warm_control_savings_refresh(28)
    assert result["mixed_units"] == 1
    day = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    row = savings_row(store, day)
    assert row["treated_units"] == 1
    assert row["control_units"] == 0


def test_control_savings_skips_open_days_and_is_stable(tmp_path):
    """Today and yesterday are never scored, and re-sweeping a day inside the
    revision horizon recomputes it to the SAME values rather than freezing it at
    the first sweep."""
    store = make_store(tmp_path)
    for offset in (0, 1, 3):
        seed_decision(store, prefix_hash=f"{offset:064x}", customer=CUSTOMER_A,
                      decision="pinged", day_offset=offset)
    first = store.warm_control_savings_refresh(28)
    assert first["rows_written"] == 1
    day = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    before = savings_row(store, day)
    second = store.warm_control_savings_refresh(28)
    # The row is REVISED, not skipped -- a day inside the horizon is recomputed
    # so a late-settling authoritative usage row can still reach the arm means.
    assert second["rows_written"] == 1
    after = savings_row(store, day)
    for column in ("treated_units", "control_units", "treated_mean_cost_usd",
                   "control_mean_cost_usd", "control_lift_usd"):
        assert after[column] == before[column]
    today = datetime.now(timezone.utc).date().isoformat()
    assert savings_row(store, today) is None


def test_control_savings_late_usage_revises_inside_horizon_only(tmp_path):
    """FIX 5: a day inside the 14-day horizon picks up late authoritative usage;
    a day outside it stays frozen at what the first sweep computed."""
    store = make_store(tmp_path)
    for offset in (3, 20):
        for index in range(3):
            seed_decision(store, prefix_hash=f"{offset:062x}{index:02x}",
                          customer=CUSTOMER_A, decision="pinged",
                          day_offset=offset)
            seed_decision(store, prefix_hash=f"{offset:062x}{index + 8:02x}",
                          customer=CUSTOMER_B, decision="holdout",
                          day_offset=offset)
    store.warm_control_savings_refresh(28)
    recent = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    ancient = (datetime.now(timezone.utc).date() - timedelta(days=20)).isoformat()
    assert savings_row(store, recent)["control_lift_usd"] == pytest.approx(0.0)
    assert savings_row(store, ancient)["control_lift_usd"] == pytest.approx(0.0)

    # The control arm's cost lands late on BOTH days, which is the only thing
    # that separates them.
    for offset in (3, 20):
        for index in range(3):
            seed_usage(store, prefix_hash=f"{offset:062x}{index + 8:02x}",
                       customer=CUSTOMER_B, cost=1.0, day_offset=offset)
    store.warm_control_savings_refresh(28)

    # Inside the horizon the lift is recomputed and now sees the control cost.
    assert savings_row(store, recent)["control_lift_usd"] == pytest.approx(3.0)
    # Outside it the row is untouched: history the beta cap already priced
    # against does not move under it.
    assert savings_row(store, ancient)["control_lift_usd"] == pytest.approx(0.0)


def test_control_savings_bounds_rejected(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.warm_control_savings_refresh(0)
    with pytest.raises(ValueError):
        store.warm_control_savings_refresh(29)


# ---------------------------------------------------------------------------
# The guardrail.
# ---------------------------------------------------------------------------

class _Stop:
    """An asyncio.Event that is already set, so a loop runs exactly one cycle."""

    def __init__(self):
        self._event = asyncio.Event()

    def is_set(self):
        # False on the first read (so the body runs once), true thereafter.
        if not self._event.is_set():
            self._event.set()
            return False
        return True

    async def wait(self):
        return True


def run_guardrail(store, monkeypatch, **env):
    monkeypatch.setattr(worker, "_store", store)
    monkeypatch.setattr(worker, "_WORKER_ACCEPTING", True)
    monkeypatch.setenv("BREVITAS_WARMING", "true")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    asyncio.run(worker.warm_guardrail(_Stop()))


def test_guardrail_freezes_on_negative_net(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    # The net is MEASURED, not self-reported: control lift on file minus the
    # ledger's reserved+spent. 0.0 of lift against 1.0 of spend is -1.0.
    seed_ledger(store, day_offset=1, spent=1.0)
    for offset in (1, 2, 3):
        seed_savings(store, day_offset=offset, savings=0.0)
    # The policy's own attribution is seeded too, and is deliberately NOT what
    # the freeze reads -- it comes back only as the policy_net_7d_usd
    # diagnostic.
    seed_decision(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                  decision="pinged", day_offset=1, realized_net=-0.6)
    seed_decision(store, prefix_hash="b" * 64, customer=CUSTOMER_A,
                  decision="pinged", day_offset=2, realized_net=-0.4)

    run_guardrail(store, monkeypatch, BREVITAS_WARM_INDEX="true")
    modes = {(row["organization_id"], row["provider"]): row
             for row in store.warm_org_mode_list()}
    assert modes[(ORG, "anthropic")]["mode"] == "frozen"
    assert "net7=-1.0000" in modes[(ORG, "anthropic")]["reason"]

    # Idempotent: a second cycle sees the pair already frozen and leaves the
    # reason -- and therefore the evidence of WHEN it froze -- alone.
    before = modes[(ORG, "anthropic")]["updated_at"]
    run_guardrail(store, monkeypatch, BREVITAS_WARM_INDEX="true")
    after = {(row["organization_id"], row["provider"]): row
             for row in store.warm_org_mode_list()}
    assert after[(ORG, "anthropic")]["updated_at"] == before


def test_guardrail_noop_below_min_usd(tmp_path, monkeypatch):
    """A net of -0.05 is inside the 0.10 dead band: noise, not a policy."""
    store = make_store(tmp_path)
    seed_ledger(store, day_offset=1, spent=1.0)
    # 0.95 of measured lift against 1.0 of spend: -0.05.
    seed_savings(store, day_offset=1, savings=0.95)
    seed_savings(store, day_offset=2, savings=0.0)
    seed_savings(store, day_offset=3, savings=0.0)
    seed_decision(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                  decision="pinged", day_offset=1, realized_net=-0.05)
    run_guardrail(store, monkeypatch, BREVITAS_WARM_INDEX="true")
    assert store.warm_org_mode_list() == []


def test_guardrail_never_freezes_an_unmeasured_pair(tmp_path, monkeypatch):
    """FIX 5b: no experiment, no freeze.

    A pair with no control rows differences zero lift against real spend and
    looks catastrophic when it is merely unmeasured. Two powered days is not
    enough either -- the gate is three.
    """
    store = make_store(tmp_path)
    seed_ledger(store, day_offset=1, spent=5.0)
    run_guardrail(store, monkeypatch, BREVITAS_WARM_INDEX="true")
    assert store.warm_org_mode_list() == []

    # Two powered days: still short of the licence.
    for offset in (1, 2):
        seed_savings(store, day_offset=offset, savings=0.0)
    run_guardrail(store, monkeypatch, BREVITAS_WARM_INDEX="true")
    assert store.warm_org_mode_list() == []

    # An under-powered third day does not count -- the refresher forces its
    # lift to zero, so it carries no evidence either way.
    seed_savings(store, day_offset=3, savings=0.0, treated=1, control=1)
    run_guardrail(store, monkeypatch, BREVITAS_WARM_INDEX="true")
    assert store.warm_org_mode_list() == []

    # A powered third day is the licence, and now the freeze fires.
    seed_savings(store, day_offset=4, savings=0.0)
    run_guardrail(store, monkeypatch, BREVITAS_WARM_INDEX="true")
    modes = {(row["organization_id"], row["provider"]): row
             for row in store.warm_org_mode_list()}
    assert modes[(ORG, "anthropic")]["mode"] == "frozen"


def test_guardrail_ignores_policy_attribution(tmp_path, monkeypatch):
    """FIX 5b: the guardrail cannot be talked out of a freeze by the policy.

    realized_net_usd is the learned policy's own scoring of its own pings. A
    policy that mis-attributes its savings reports a healthy net exactly while
    it is losing money, so that number is a diagnostic here and never the gate.
    """
    store = make_store(tmp_path)
    seed_ledger(store, day_offset=1, spent=5.0)
    for offset in (1, 2, 3):
        seed_savings(store, day_offset=offset, savings=0.0)
    # The policy insists it is making money. The experiment says otherwise.
    for index, offset in enumerate((1, 2, 3)):
        seed_decision(store, prefix_hash=f"{index:064x}", customer=CUSTOMER_A,
                      decision="pinged", day_offset=offset, realized_net=100.0)

    run_guardrail(store, monkeypatch, BREVITAS_WARM_INDEX="true")

    modes = {(row["organization_id"], row["provider"]): row
             for row in store.warm_org_mode_list()}
    assert modes[(ORG, "anthropic")]["mode"] == "frozen"
    # And the scan carries both numbers, so the divergence is observable.
    scanned = {(row["organization_id"], row["provider"]): row
               for row in store.warm_guardrail_scan(7)}[(ORG, "anthropic")]
    assert scanned["net_7d_usd"] == pytest.approx(-5.0)
    assert scanned["policy_net_7d_usd"] == pytest.approx(300.0)


def test_guardrail_noop_learned_flags_off(tmp_path, monkeypatch):
    """With neither the index nor the hazard model on there is nothing learned
    to freeze, so the loop returns before reading anything."""
    store = make_store(tmp_path)
    seed_ledger(store, day_offset=1, spent=1.0)
    seed_decision(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                  decision="pinged", day_offset=1, realized_net=-99.0)
    monkeypatch.delenv("BREVITAS_WARM_INDEX", raising=False)
    monkeypatch.delenv("BREVITAS_WARM_HAZARD_V2", raising=False)
    run_guardrail(store, monkeypatch)
    assert store.warm_org_mode_list() == []


def test_guardrail_ignores_unscored_pings(tmp_path, monkeypatch):
    """A null realized_net_usd is 'not scored yet', never 'earned zero', so a
    pair whose pings are all unscored cannot trip the freeze."""
    store = make_store(tmp_path)
    seed_ledger(store, day_offset=1, spent=1.0)
    seed_decision(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                  decision="pinged", day_offset=1, realized_net=None)
    run_guardrail(store, monkeypatch, BREVITAS_WARM_HAZARD_V2="true")
    assert store.warm_org_mode_list() == []


def test_guardrail_scan_excludes_non_pinged_and_old_rows(tmp_path):
    store = make_store(tmp_path)
    seed_ledger(store, day_offset=1, spent=1.0)
    seed_decision(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                  decision="pinged", day_offset=1, realized_net=-1.0)
    seed_decision(store, prefix_hash="b" * 64, customer=CUSTOMER_A,
                  decision="holdout", day_offset=1, realized_net=-50.0)
    seed_decision(store, prefix_hash="c" * 64, customer=CUSTOMER_A,
                  decision="pinged", day_offset=30, realized_net=-50.0)
    scanned = store.warm_guardrail_scan(7)
    assert len(scanned) == 1
    assert scanned[0]["net_7d_usd"] == pytest.approx(-1.0)
    assert scanned[0]["mode"] == "learned"


def test_org_mode_set_round_trip_and_bounds(tmp_path):
    store = make_store(tmp_path)
    store.warm_org_mode_set(ORG, "anthropic", "frozen", "because")
    listed = store.warm_org_mode_list()
    assert listed[0]["mode"] == "frozen"
    assert listed[0]["reason"] == "because"
    # Unfreezing is manual and this is the whole mechanism for it.
    store.warm_org_mode_set(ORG, "anthropic", "learned", "operator reviewed")
    assert store.warm_org_mode_list()[0]["mode"] == "learned"
    with pytest.raises(ValueError):
        store.warm_org_mode_set(ORG, "anthropic", "off")
    with pytest.raises(ValueError):
        store.warm_org_mode_set(ORG, "nowhere", "frozen")


# ---------------------------------------------------------------------------
# Frozen means flat priors.
# ---------------------------------------------------------------------------

def seed_state(store, customer, *, provider="anthropic", org=ORG,
               events=40, first_seen_days=30, last_seen_days=20,
               hazard_n=None, hazard_e=None):
    now = datetime.now(timezone.utc)
    bucket = _utc_hour_bucket(now)
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO warm_customer_state(organization_id,"
            "customer_id,provider,customer_key_hmac,hazard_n,hazard_e,"
            "events_total,first_seen_at,last_seen_at,last_update_at,regime,"
            "created_at,updated_at) VALUES(?,?,?,'',?,?,?,?,?,?,'unknown',?,?)",
            (org, customer, provider,
             json.dumps(hazard_n if hazard_n is not None else {bucket: 5.0}),
             json.dumps(hazard_e if hazard_e is not None else {bucket: 40.0}),
             int(events),
             (now - timedelta(days=first_seen_days)).isoformat(),
             (now - timedelta(days=last_seen_days)).isoformat(),
             (now - timedelta(days=last_seen_days)).isoformat(),
             now.isoformat(), now.isoformat()))


def test_frozen_org_scored_flat(tmp_path):
    """A frozen pair with the hazard flag ON is scored exactly as a pair with
    no state row: p_alive 1, no chain, and the stop-loss predicate back in
    force so a retired prefix is not even a candidate."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    seed_prefix(store, prefix_hash="b" * 64, customer=CUSTOMER_A, misses=5)
    seed_state(store, CUSTOMER_A)
    store.warm_org_mode_set(ORG, "anthropic", "frozen", "test")

    result = claim(store, index_enabled=True, hazard_v2=True)
    assert len(result["rows"]) == 1
    rows = decision_rows(store)
    assert {row["prefix_hash"] for row in rows} == {"a" * 64}
    assert rows[0]["p_alive"] == pytest.approx(1.0)
    assert rows[0]["chain_cost_usd"] == pytest.approx(0.0)


def test_frozen_matches_no_state_row_exactly(tmp_path):
    """The equivalence itself: frozen-with-state and learned-without-state
    produce identical decisions and identical index components."""
    frozen = make_store(tmp_path, "frozen.db")
    enable(frozen, budget=10.0)
    seed_prefix(frozen, prefix_hash="a" * 64, customer=CUSTOMER_A)
    seed_state(frozen, CUSTOMER_A)
    frozen.warm_org_mode_set(ORG, "anthropic", "frozen", "test")
    frozen_result = claim(frozen, index_enabled=True, hazard_v2=True)

    flat = make_store(tmp_path, "flat.db")
    enable(flat, budget=10.0)
    seed_prefix(flat, prefix_hash="a" * 64, customer=CUSTOMER_A)
    flat_result = claim(flat, index_enabled=True, hazard_v2=True)

    assert len(frozen_result["rows"]) == len(flat_result["rows"]) == 1
    frozen_row = decision_rows(frozen)[0]
    flat_row = decision_rows(flat)[0]
    for column in ("decision", "p_return", "index_score", "index_density",
                   "p_alive", "chain_cost_usd", "c_belief_usd", "v_hit_usd"):
        assert frozen_row[column] == pytest.approx(flat_row[column]) if isinstance(
            frozen_row[column], float) else frozen_row[column] == flat_row[column]


def test_learned_org_still_reads_hazard(tmp_path):
    """The control for the test above: unfrozen, the same state row DOES move
    p_alive off 1, so the frozen result is not vacuously flat."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A)
    seed_state(store, CUSTOMER_A)
    claim(store, index_enabled=True, hazard_v2=True)
    assert decision_rows(store)[0]["p_alive"] < 1.0


def test_empty_org_mode_table_changes_nothing(tmp_path):
    """The LEFT JOIN with no rows is a no-op: same decisions as before D."""
    store = make_store(tmp_path)
    enable(store, budget=10.0)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A, misses=5)
    seed_prefix(store, prefix_hash="b" * 64, customer=CUSTOMER_B)
    # hazard off: the stop-loss predicate excludes the retired prefix.
    assert len(claim(store)["rows"]) == 1
    other = make_store(tmp_path, "hazard-on.db")
    enable(other, budget=10.0)
    seed_prefix(other, prefix_hash="a" * 64, customer=CUSTOMER_A, misses=5)
    seed_prefix(other, prefix_hash="b" * 64, customer=CUSTOMER_B)
    # hazard on WITH the index on and no mode rows: the retired prefix is
    # scored again. Both flags, because P(alive) -- the thing that replaces the
    # counter -- only reaches a decision through the index block.
    assert len(claim(other, hazard_v2=True, index_enabled=True)["rows"]) == 2


def test_claim_kwargs_beta_env_parsing(monkeypatch):
    monkeypatch.delenv("BREVITAS_WARM_SPEND_BETA", raising=False)
    assert worker._warm_claim_kwargs()["beta"] == 0.0
    monkeypatch.setenv("BREVITAS_WARM_SPEND_BETA", "2.5")
    assert worker._warm_claim_kwargs()["beta"] == pytest.approx(2.5)
    monkeypatch.setenv("BREVITAS_WARM_SPEND_BETA", "999")
    assert worker._warm_claim_kwargs()["beta"] == pytest.approx(100.0)
    monkeypatch.setenv("BREVITAS_WARM_SPEND_BETA", "-4")
    assert worker._warm_claim_kwargs()["beta"] == pytest.approx(0.0)
    monkeypatch.setenv("BREVITAS_WARM_SPEND_BETA", "nonsense")
    assert worker._warm_claim_kwargs()["beta"] == 0.0


def test_purge_ages_control_savings_not_recent(tmp_path):
    store = make_store(tmp_path)
    seed_savings(store, day_offset=401, savings=1.0)
    seed_savings(store, day_offset=30, savings=1.0)
    result = store.purge_warm_state(7)
    assert result["control_savings_deleted"] == 1
    recent = (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
    assert savings_row(store, recent) is not None
