"""Index convergence (migration 202608100010, mirrored in api/store.py): the
simulator-proven learned-index-fixed policy in the shipped scheduler.

Every proof scripts/warm_replay_sim.py's LearnedIndexFixedPolicy established,
re-stated against the store layer. The simulator is the binding reference for
the arithmetic; these tests are the binding reference for the arithmetic
actually reaching a decision.

The seven fixes, and the test that holds each one:

  F1  silence-conditioned p_return   test_silence_collapses_p_return
  F2  organic suppression gate       test_self_refreshing_arm_is_skipped
  F3  I_max abandon rule             test_silence_past_i_max_is_abandoned
  F4  cold-start prior               test_prior_retires_as_exposure_accrues
  F5  periodicity fast-path          test_detected_clock_takes_the_fast_path
                                     test_two_missed_arrivals_falsify_the_clock
  F6  median-floored chain           test_chain_is_capped_at_the_median
  F7  shared-cache-key touch         test_second_arm_on_one_key_is_skipped

plus the hold that makes all of it safe to turn on:

      cold start holds v1            test_cold_start_decisions_match_v1_exactly

THE INVARIANT THESE SIT UNDER, and the reason the file opens by naming it: the
whole delta lives inside the BREVITAS_WARM_INDEX + BREVITAS_WARM_HAZARD_V2 flag
pair. Every test below that turns the policy on has a partner asserting it stays
off with either flag down, because a scheduler that changes behaviour without
its flag is not a scheduler anybody can roll back.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from api.store import (UsageStore, _utc_hour_bucket, warm_convergence_score,
                       warm_hazard_posterior, warm_i_max_seconds,
                       warm_p_return_v2, warm_period_detect,
                       warm_survival_conditional)
from api.worker import _warm_claim_kwargs

ORG = "org-convergence-1"
CUSTOMER_A = "customer-conv-a"
CUSTOMER_B = "customer-conv-b"
BREAK_EVEN = {"anthropic": 0.11, "deepseek": round(0.02 / 0.98, 6)}
TTL = 300
SAFETY = 60
# tau, the keep-alive period: one TTL less the safety margin.
TAU = TTL - SAFETY
# I_max for anthropic at the calibrated break-even: 300 * (1.25/f - 1) with
# f = b/(1+b). Just under an hour.
I_MAX = warm_i_max_seconds(TTL, 1.25, 0.11)


def make_store(tmp_path, name="convergence.db"):
    return UsageStore(str(tmp_path / name))


def enable(store, provider="anthropic", budget=10.0, org=ORG):
    return store.warm_credentials_upsert(
        org, provider, "enc:credential", True, "actor-1", budget, 1000, 288)


def seed_prefix(store, *, prefix_hash, customer, provider="anthropic",
                tokens=100_000, arrival_count=20, bucket_count=20,
                ewma=1200.0, silence_s=TAU, touch_silence_s=None, org=ORG,
                ttl=TTL):
    """One warm_prefixes row in a state the observer could actually produce.

    `silence_s` moves last_seen_at, last_touch_at and next_due_at TOGETHER,
    which is the invariant every writer of this table maintains: a row becomes
    due exactly tau after it was last touched. Fixtures that move only
    next_due_at describe a database state no writer can reach, and the
    shared-key gate is precisely the code that notices.
    """
    now = datetime.now(timezone.utc)
    bucket = _utc_hour_bucket(now)
    last_seen = now - timedelta(seconds=float(silence_s))
    touched = now - timedelta(
        seconds=float(silence_s if touch_silence_s is None else touch_silence_s))
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO warm_prefixes(organization_id,customer_id,provider,"
            "prefix_hash,payload_ciphertext,prefix_tokens,provider_ttl_seconds,"
            "arrival_count,ewma_interarrival_s,hour_histogram,consecutive_misses,"
            "pings_today,pings_today_date,state,created_at,last_seen_at,"
            "last_touch_at,next_due_at,expires_at,ping_reserve_usd) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,0,0,'','active',?,?,?,?,?,?)",
            (org, customer, provider, prefix_hash, "enc:payload", int(tokens),
             int(ttl), int(arrival_count), ewma,
             json.dumps({bucket: bucket_count}),
             now.isoformat(), last_seen.isoformat(), touched.isoformat(),
             (last_seen + timedelta(seconds=int(ttl) - SAFETY)).isoformat(),
             (now + timedelta(days=1)).isoformat(),
             round(3.75 * tokens / 1_000_000.0, 10)))


def seed_state(store, *, customer, provider="anthropic", n_b=20.0, e_b=5.0,
               events_total=20, first_seen_days_ago=30.0,
               last_seen_seconds_ago=TAU, recent_gaps=None, org=ORG,
               mass_bucket_offset_h=0):
    """`mass_bucket_offset_h` puts the customer's hour-of-week mass that many
    hours in the PAST rather than in the current bucket, which is how a fixture
    says "this customer's active window has ended" without depending on what
    time the suite happens to run."""
    now = datetime.now(timezone.utc)
    key = _utc_hour_bucket(now - timedelta(hours=int(mass_bucket_offset_h)))
    first_seen = now - timedelta(days=float(first_seen_days_ago))
    last_seen = now - timedelta(seconds=float(last_seen_seconds_ago))
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO warm_customer_state(organization_id,customer_id,"
            "provider,hazard_n,hazard_e,events_total,first_seen_at,last_seen_at,"
            "last_update_at,created_at,updated_at,recent_gaps) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (org, customer, provider, json.dumps({key: float(n_b)}),
             json.dumps({key: float(e_b)}), int(events_total),
             first_seen.isoformat(), last_seen.isoformat(),
             last_seen.isoformat(), now.isoformat(), now.isoformat(),
             json.dumps(list(recent_gaps or []))))


def claim(store, limit=10, max_gap_seconds=3600, **kwargs):
    return store.warm_due_claim(
        limit, reserve_usd_per_mtok=3.75, roi_min_arrivals=5, roi_min_p=0.35,
        roi_break_even_p=0.11, stop_loss=3, max_gap_seconds=max_gap_seconds,
        safety_margin_seconds=SAFETY,
        roi_break_even_by_provider=dict(BREAK_EVEN), **kwargs)


def decisions(store):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT * FROM warm_decision_log ORDER BY id")]


def decision_map(store):
    return {row["prefix_hash"]: row["decision"] for row in decisions(store)}


def converged(store, **kwargs):
    """The flag pair, on. Nothing in this file is reachable without both."""
    return claim(store, index_enabled=True, hazard_v2=True, **kwargs)


# ---------------------------------------------------------------------------
# F1 -- p_return conditioned on the arm's elapsed silence.
#
# The dominant defect: 68% of Phase-1 pings fired at customers silent more than
# six hours, because the index asked how often this customer arrives in this
# hour-of-week bucket and never asked whether they were still here.
# ---------------------------------------------------------------------------
def test_silence_collapses_p_return(tmp_path):
    """A customer silent for six hours scores ~0 for the next window even
    though their lifetime histogram says they always arrive at this hour.

    This is the dominant Phase-1 defect, executable. The index asked "how often
    does this customer arrive in this hour-of-week bucket" and never asked
    whether they were still here, so 68% of its pings fired at customers who had
    been gone for hours.

    The fixture separates the two questions cleanly. The PREFIX histogram puts
    all twenty arrivals in the current bucket -- so v1, which divides that
    histogram by the arrival count, says 1.0. The customer STATE puts their
    hazard mass in the bucket six hours ago, which is the truth: their session
    ran then and has been silent since. deepseek, because at a four-hour TTL a
    six-hour silence is still well inside I_max and F3 does not get to answer
    first -- F1 has to answer on its own.
    """
    off = make_store(tmp_path, "silence-off.db")
    on = make_store(tmp_path, "silence-on.db")
    for store in (off, on):
        enable(store, provider="deepseek")
        # An EWMA above the four-hour TTL, or F2 would answer first: at
        # deepseek's TTL an arm arriving every thirty minutes really is
        # self-refreshing, and that is a different fix.
        seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                    provider="deepseek", ttl=14400, ewma=21600.0,
                    silence_s=6 * 3600)
        seed_state(store, customer=CUSTOMER_A, provider="deepseek",
                   last_seen_seconds_ago=6 * 3600, mass_bucket_offset_h=6)

    claim(off, index_enabled=True, hazard_v2=False, max_gap_seconds=86400)
    converged(on, max_gap_seconds=86400)

    off_row, on_row = decisions(off)[0], decisions(on)[0]
    # v1: "they always arrive at this hour", and it buys a ping on that.
    assert off_row["p_return"] == pytest.approx(1.0)
    assert off_row["decision"] == "pinged"
    # The converged policy: they have been gone for six hours and this is not
    # even an hour they are usually here. ~0, and no ping.
    assert on_row["p_return"] < 0.01
    assert on_row["decision"] != "pinged"


def test_p_return_falls_monotonically_with_silence(tmp_path):
    """The conditioning is directional, not just different: every hour a
    customer does not arrive is an arrival that did not happen, and it is folded
    into the posterior as evidence against them.

    HONEST ABOUT THE SHAPE. The Gamma-Poisson tail is heavy by construction, so
    silence inside a customer's OWN active bucket moves p_return by a factor
    rather than to zero -- a customer who only ever arrives at 09:00 being quiet
    at 03:00 is not evidence about them at all, and the piecewise hour-of-week
    walk is what keeps it from being counted as such. What takes a long-silent
    arm off the table entirely is F3.
    """
    def _score(silence_s):
        now = datetime.now(timezone.utc)
        key = _utc_hour_bucket(now)
        return warm_convergence_score(
            counts={key: 20.0}, exposure={key: 5.0},
            org_counts={}, org_exposure={}, events_total=20,
            first_seen_at=now - timedelta(days=30),
            state_last_seen_at=now - timedelta(seconds=silence_s),
            prefix_last_seen_at=now - timedelta(seconds=silence_s),
            now=now, recent_gaps=[], ttl_seconds=TTL,
            safety_margin_seconds=SAFETY, break_even=0.11,
            write_multiplier=1.25, prior_hours=2.0, majority_hours=48.0,
            period_max_dispersion=0.20, period_miss_limit=1.5)["p_return"]

    ladder = [_score(seconds) for seconds in (TAU, 3600, 6 * 3600, 24 * 3600)]
    assert ladder == sorted(ladder, reverse=True)
    assert ladder[-1] < ladder[0]
    # THE CONTRAST, and the reason this is a fix rather than a tuning change:
    # the Phase-1 estimator reads the same posterior and cannot express any of
    # those four numbers differently, because silence is not one of its inputs.
    assert warm_p_return_v2(20.0, 5.0, 0.0, 0.0, TTL) == pytest.approx(
        warm_p_return_v2(20.0, 5.0, 0.0, 0.0, TTL))


def test_silence_conditioning_is_inside_the_flag_pair(tmp_path):
    """With the hazard flag down the same arm is scored on v1's histogram, so
    the conditioning cannot reach a decision without its flag."""
    store = make_store(tmp_path, "flagged.db")
    enable(store)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                ewma=900.0, silence_s=6 * 3600)
    seed_state(store, customer=CUSTOMER_A, last_seen_seconds_ago=6 * 3600)
    claim(store, index_enabled=True, hazard_v2=False)
    row = decisions(store)[0]
    # v1's answer: the lifetime histogram ratio, 20/20.
    assert row["p_return"] == pytest.approx(1.0)
    assert row["decision"] == "pinged"


# ---------------------------------------------------------------------------
# F2 -- the organic-suppression gate.
# ---------------------------------------------------------------------------
def test_self_refreshing_arm_is_skipped(tmp_path):
    """An EWMA inter-arrival under the provider TTL means the customer rewrites
    their own entry for free, so every keep-alive bought for them duplicates a
    write their own traffic already made."""
    store = make_store(tmp_path, "organic.db")
    enable(store)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                ewma=TTL - 1, silence_s=TAU)
    seed_state(store, customer=CUSTOMER_A)
    result = converged(store)
    assert result["rows"] == []
    assert decision_map(store) == {"a" * 64: "skipped_organic"}
    # A gate, not a score: nothing was computed, so nothing is reported.
    assert decisions(store)[0]["index_score"] is None
    assert decisions(store)[0]["p_eff"] is None


def test_organic_gate_needs_both_flags(tmp_path):
    for flags in ({"index_enabled": True, "hazard_v2": False},
                  {"index_enabled": False, "hazard_v2": True}):
        store = make_store(tmp_path, f"organic-{flags['index_enabled']}.db")
        enable(store)
        seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                    ewma=TTL - 1, silence_s=TAU)
        seed_state(store, customer=CUSTOMER_A)
        claim(store, **flags)
        assert "skipped_organic" not in decision_map(store).values()


def test_an_arm_at_exactly_the_ttl_is_not_organic(tmp_path):
    """The gate is strict: an arm arriving exactly every TTL does NOT refresh
    its own entry -- it arrives as the entry expires, which is the case warming
    exists for."""
    store = make_store(tmp_path, "boundary.db")
    enable(store)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                ewma=float(TTL), silence_s=TAU)
    seed_state(store, customer=CUSTOMER_A)
    converged(store)
    assert "skipped_organic" not in decision_map(store).values()


# ---------------------------------------------------------------------------
# F3 -- the I_max abandon rule.
# ---------------------------------------------------------------------------
def test_silence_past_i_max_is_abandoned(tmp_path):
    """Past I_max = ttl * (w/f - 1) the chain of keep-alives needed to bridge a
    silence costs more than the single write-priced miss it prevents, for every
    p <= 1. Letting the entry lapse and re-warming on the next real arrival is
    arithmetically better, so this is not a judgement about the customer."""
    store = make_store(tmp_path, "abandon.db")
    enable(store)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                ewma=1800.0, silence_s=I_MAX + 60)
    seed_state(store, customer=CUSTOMER_A,
               last_seen_seconds_ago=I_MAX + 60)
    assert converged(store)["rows"] == []
    assert decision_map(store) == {"a" * 64: "skipped_abandon"}


def test_silence_just_inside_i_max_is_still_scored(tmp_path):
    """The control for the test above: the rule is a horizon, not a mood."""
    store = make_store(tmp_path, "inside.db")
    enable(store)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                ewma=1800.0, silence_s=I_MAX - 60)
    seed_state(store, customer=CUSTOMER_A,
               last_seen_seconds_ago=I_MAX - 60)
    converged(store)
    assert "skipped_abandon" not in decision_map(store).values()


def test_abandon_gate_needs_both_flags(tmp_path):
    store = make_store(tmp_path, "abandon-off.db")
    enable(store)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                ewma=1800.0, silence_s=I_MAX + 60)
    seed_state(store, customer=CUSTOMER_A, last_seen_seconds_ago=I_MAX + 60)
    claim(store, index_enabled=True, hazard_v2=False)
    assert "skipped_abandon" not in decision_map(store).values()


# ---------------------------------------------------------------------------
# F4 -- the cold-start prior, and F6 -- the median-floored chain.
# ---------------------------------------------------------------------------
def test_prior_retires_as_exposure_accrues(tmp_path):
    """A customer's own evidence must reach majority weight in about two active
    days, not the three weeks the pinned eight pseudo-hours took.

    Checked on the estimator, because that is where the quantity lives: the
    prior's weight k is what shrinks, and it shrinks with the customer's own
    accumulated exposure rather than with wall clock.
    """
    # No exposure at all: the prior is capped by the customer's own bucket
    # exposure, which is zero, so the floor carries it.
    _, beta_new = warm_hazard_posterior(1.0, 0.0, 0.0, 0.0, 0.0, 2.0, 48.0)
    assert beta_new == pytest.approx(0.25)
    # Half a majority-horizon of engaged exposure: the prior is half retired,
    # but still capped at the customer's own bucket exposure.
    _, beta_mid = warm_hazard_posterior(20.0, 5.0, 0.0, 0.0, 24.0, 2.0, 48.0)
    assert beta_mid == pytest.approx(5.0 + 1.0)
    # Past it: fully retired, floor only.
    _, beta_old = warm_hazard_posterior(20.0, 5.0, 0.0, 0.0, 96.0, 2.0, 48.0)
    assert beta_old == pytest.approx(5.25)
    # The rate a busy customer is credited with therefore RISES toward their own
    # evidence rather than being held down by the organization prior.
    alpha_new, beta_n = warm_hazard_posterior(20.0, 5.0, 0.0, 0.0, 0.0, 2.0, 48.0)
    alpha_old, beta_o = warm_hazard_posterior(20.0, 5.0, 0.0, 0.0, 96.0, 2.0, 48.0)
    assert alpha_old / beta_o > alpha_new / beta_n


def test_chain_is_capped_at_the_median(tmp_path):
    """F6. n_chain is a mean over a heavy-tailed residual-life distribution,
    dragged up by improbable long silences the I_max rule would abandon long
    before reaching. Inside a live session the typical session's chain is the
    honest number, and it can only ever be shorter."""
    store = make_store(tmp_path, "median.db")
    enable(store)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                ewma=1200.0, silence_s=TAU)
    seed_state(store, customer=CUSTOMER_A)
    converged(store)
    row = decisions(store)[0]
    chain = round(row["chain_cost_usd"] / row["c_belief_usd"])
    # The horizon this provider could commit to at all.
    k_max = int(I_MAX // TAU)
    assert 0 <= chain <= k_max
    # And the index is charged for exactly the chain it priced.
    assert row["index_score"] == pytest.approx(
        row["p_eff"] / 0.11 - 1 - chain, abs=1e-9)


# ---------------------------------------------------------------------------
# F5 -- the periodicity fast-path, and its falsification.
# ---------------------------------------------------------------------------
def test_detected_clock_takes_the_fast_path(tmp_path):
    """A scheduled agent is a clock, not a renewal process with an uncertain
    rate. With a dozen arrivals of evidence the negative-binomial tail is wide
    enough that n_chain exceeds the break-even chain length and the policy
    abandons MID-CHAIN -- buying the first keep-alive of a chain and not the
    second, which is the one spend pattern strictly worse than doing nothing.

    Gaps of ~1202.5s at a 300s TTL with tau (240s) of silence: the next arrival
    is forecast in 962.5s, and at tau = 240s exactly four further keep-alives
    stand between now and it.
    """
    store = make_store(tmp_path, "clock.db")
    enable(store)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                ewma=1200.0, silence_s=TAU)
    seed_state(store, customer=CUSTOMER_A, last_seen_seconds_ago=TAU,
               recent_gaps=[1200.0, 1210.0, 1190.0, 1205.0])
    converged(store)
    row = decisions(store)[0]
    # The phase model reports its own confidence rather than a hazard.
    period, confidence = warm_period_detect([1200.0, 1210.0, 1190.0, 1205.0], 0.20)
    assert period == pytest.approx(1202.5)
    assert row["p_return"] == pytest.approx(confidence)
    assert row["p_return"] == pytest.approx(0.98)
    assert round(row["chain_cost_usd"] / row["c_belief_usd"]) == 4


def test_two_missed_arrivals_falsify_the_clock(tmp_path):
    """Past the miss limit two predicted arrivals have gone unanswered: this is
    a departed agent, not a slow one, and the arm goes back to the index path to
    be priced and abandoned. Without it a churned cron agent is sustained to
    I_max on every window, forever."""
    store = make_store(tmp_path, "falsified.db")
    enable(store)
    silence = 1.5 * 1202.5 + 60
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                ewma=1200.0, silence_s=silence)
    seed_state(store, customer=CUSTOMER_A, last_seen_seconds_ago=silence,
               recent_gaps=[1200.0, 1210.0, 1190.0, 1205.0])
    converged(store)
    row = decisions(store)[0]
    # Not the phase model's confidence: the arm is back on the index.
    assert row["p_return"] != pytest.approx(0.98)


def test_a_ragged_series_never_reaches_the_fast_path(tmp_path):
    """MAD, not standard deviation: one missed cron firing must not disqualify
    an otherwise perfect clock, and a genuinely ragged arm must never qualify."""
    assert warm_period_detect([1200.0, 1210.0, 2400.0, 1205.0], 0.20) is not None
    assert warm_period_detect([1200.0, 600.0, 2400.0, 900.0], 0.20) is None
    # Two gaps is not evidence of a period, however identical they are.
    assert warm_period_detect([1200.0, 1200.0], 0.20) is None


def test_a_period_inside_the_ttl_is_organic_not_periodic(tmp_path):
    """An arm whose clock ticks faster than its entry expires is self-refreshing,
    and F2 owns that case: it must be skipped outright, never scheduled."""
    store = make_store(tmp_path, "fast-clock.db")
    enable(store)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                ewma=120.0, silence_s=TAU)
    seed_state(store, customer=CUSTOMER_A,
               recent_gaps=[120.0, 121.0, 119.0, 120.0])
    converged(store)
    assert decision_map(store) == {"a" * 64: "skipped_organic"}


def test_the_observer_maintains_the_gap_window(tmp_path):
    """F5's evidence is written by the arrival path, not by the scheduler: the
    fast-path cannot exist unless the gaps are already there when it is turned
    on. Bounded at twelve, newest last."""
    store = make_store(tmp_path, "gaps.db")
    enable(store)
    now = datetime.now(timezone.utc)
    for index in range(15):
        store.warm_prefix_observe(
            ORG, CUSTOMER_A, "anthropic", "a" * 64, "enc:payload", 100_000,
            TTL, SAFETY, False, ping_reserve_usd=0.375,
            model_class="claude-sonnet-4-5")
        with sqlite3.connect(store.db_path) as db:
            # Walk the state's clock back so the next observation closes a gap
            # of a known size, exactly as a real arrival would.
            db.execute(
                "UPDATE warm_customer_state SET last_seen_at=?,last_update_at=? "
                "WHERE organization_id=? AND customer_id=?",
                ((datetime.now(timezone.utc)
                  - timedelta(seconds=600)).isoformat(),
                 (datetime.now(timezone.utc)
                  - timedelta(seconds=600)).isoformat(), ORG, CUSTOMER_A))
    raw = store.warm_customer_state_get(
        ORG, CUSTOMER_A, "anthropic")["recent_gaps"]
    gaps = json.loads(raw) if isinstance(raw, str) else list(raw)
    assert len(gaps) == 12
    assert all(gap > 0 for gap in gaps)


# ---------------------------------------------------------------------------
# F7 -- the shared cache key.
# ---------------------------------------------------------------------------
def test_second_arm_on_one_key_is_skipped(tmp_path):
    """Provider caches are ORG-key-scoped, not customer-scoped, so twenty
    customers on one shared system prompt are twenty arms bidding to keep ONE
    entry warm. N-1 of those chains are pure waste, and the periodicity
    fast-path makes all N fire in lockstep.

    Two arms, one prefix_hash. The one whose sibling touched the entry more
    recently than its own schedule assumes is the one that skips.
    """
    store = make_store(tmp_path, "shared.db")
    enable(store)
    shared = "c" * 64
    # A touched the entry a minute ago; B's own schedule assumes the entry
    # lapsed tau ago. B is therefore buying warmth A already holds.
    seed_prefix(store, prefix_hash=shared, customer=CUSTOMER_A,
                ewma=1200.0, silence_s=TAU, touch_silence_s=60)
    seed_prefix(store, prefix_hash=shared, customer=CUSTOMER_B,
                ewma=1200.0, silence_s=TAU)
    seed_state(store, customer=CUSTOMER_A)
    seed_state(store, customer=CUSTOMER_B)

    converged(store)
    verdicts = [row["decision"] for row in decisions(store)]
    assert verdicts.count("skipped_shared_warm") == 1
    # And the other arm is NOT skipped: exactly one ping keeps the shared entry
    # warm, which is the whole point. Skipping both would let it lapse.
    assert len(verdicts) == 2
    assert "skipped_shared_warm" in verdicts
    skipped = [row for row in decisions(store)
               if row["decision"] == "skipped_shared_warm"]
    assert skipped[0]["customer_id"] == CUSTOMER_B


def test_an_arm_never_skips_on_its_own_warmth(tmp_path):
    """The gate is about SHARING. A lone arm on its own key must never skip: its
    own touch and its own due time move together by construction, so its own
    warmth has exactly the safety margin left at the moment it becomes due."""
    store = make_store(tmp_path, "lonely.db")
    enable(store)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                ewma=1200.0, silence_s=TAU)
    seed_state(store, customer=CUSTOMER_A)
    converged(store)
    assert "skipped_shared_warm" not in decision_map(store).values()


def test_shared_key_gate_does_not_cross_organizations(tmp_path):
    """Cache keys are org-scoped by provider design, and so is the touch state.
    Another tenant's warmth is not ours to spend against."""
    store = make_store(tmp_path, "tenants.db")
    other_org = "org-convergence-2"
    enable(store)
    enable(store, org=other_org)
    shared = "c" * 64
    seed_prefix(store, prefix_hash=shared, customer=CUSTOMER_A,
                ewma=1200.0, silence_s=TAU, touch_silence_s=60, org=other_org)
    seed_prefix(store, prefix_hash=shared, customer=CUSTOMER_B,
                ewma=1200.0, silence_s=TAU)
    seed_state(store, customer=CUSTOMER_A, org=other_org)
    seed_state(store, customer=CUSTOMER_B)
    converged(store)
    assert "skipped_shared_warm" not in decision_map(store).values()


def test_shared_key_gate_needs_both_flags(tmp_path):
    store = make_store(tmp_path, "shared-off.db")
    enable(store)
    shared = "c" * 64
    seed_prefix(store, prefix_hash=shared, customer=CUSTOMER_A,
                ewma=1200.0, silence_s=TAU, touch_silence_s=60)
    seed_prefix(store, prefix_hash=shared, customer=CUSTOMER_B,
                ewma=1200.0, silence_s=TAU)
    seed_state(store, customer=CUSTOMER_A)
    seed_state(store, customer=CUSTOMER_B)
    claim(store, index_enabled=True, hazard_v2=False)
    assert "skipped_shared_warm" not in decision_map(store).values()


# ---------------------------------------------------------------------------
# THE COLD-START HOLD. The property that makes the whole thing safe to enable.
# ---------------------------------------------------------------------------
def test_cold_start_decisions_match_v1_exactly(tmp_path):
    """An arm below roi_min_arrivals is scored EXACTLY as v1 scores it, even
    though a state row exists.

    Phase 1 fell back to flat priors only when the state ROW was absent -- for
    exactly one arrival -- after which it scored from a posterior built on a
    single observation and went quiet, which is what left the learned policy
    earning nothing over a customer's first session while v1 was already
    bridging their gaps. The hold now runs until the arm clears the same
    threshold v1 uses to admit it has no evidence.

    The two stores below carry IDENTICAL state; only the flags differ.
    """
    off = make_store(tmp_path, "cold-off.db")
    on = make_store(tmp_path, "cold-on.db")
    for store in (off, on):
        enable(store)
        for index in range(8):
            seed_prefix(store, prefix_hash=f"{index:064x}",
                        customer=f"cold-{index}", arrival_count=3,
                        bucket_count=3, ewma=1200.0, silence_s=TAU)
            seed_state(store, customer=f"cold-{index}")

    claim(off, limit=50, index_enabled=True, hazard_v2=False)
    claim(on, limit=50, index_enabled=True, hazard_v2=True)

    assert decision_map(on) == decision_map(off)
    for on_row, off_row in zip(decisions(on), decisions(off)):
        for column in ("decision", "p_return", "roi_floor", "index_score",
                       "p_alive", "chain_cost_usd", "c_belief_usd",
                       "v_hit_usd", "p_eff"):
            assert on_row[column] == pytest.approx(off_row[column]), column


def test_one_arrival_past_the_threshold_leaves_the_hold(tmp_path):
    """The control: the hold is a threshold, not an off switch. The same arm at
    roi_min_arrivals is scored by the converged policy."""
    store = make_store(tmp_path, "warm-start.db")
    enable(store)
    seed_prefix(store, prefix_hash="a" * 64, customer=CUSTOMER_A,
                arrival_count=5, bucket_count=5, ewma=1200.0, silence_s=TAU)
    seed_state(store, customer=CUSTOMER_A)
    converged(store)
    row = decisions(store)[0]
    # v1 would have said 5/5 = 1.0; the converged policy has an opinion of its
    # own, and it carries P(alive) and a priced chain.
    assert row["p_return"] != pytest.approx(1.0)
    assert row["p_alive"] is not None and row["p_alive"] < 1.0


# ---------------------------------------------------------------------------
# The knobs.
# ---------------------------------------------------------------------------
def test_claim_kwargs_convergence_env_parsing(monkeypatch):
    """The four tunables default to the values the converged simulator run used.
    An operator who turns the flag pair on and configures nothing gets the
    policy the benchmark measured, which is the only default these may have."""
    for name in ("BREVITAS_WARM_HAZARD_PRIOR_HOURS",
                 "BREVITAS_WARM_HAZARD_EXPOSURE_MAJORITY_HOURS",
                 "BREVITAS_WARM_PERIOD_MAX_DISPERSION",
                 "BREVITAS_WARM_PERIOD_MISS_LIMIT"):
        monkeypatch.delenv(name, raising=False)
    kwargs = _warm_claim_kwargs()
    assert kwargs["prior_hours"] == 2.0
    assert kwargs["exposure_majority_hours"] == 48.0
    assert kwargs["period_max_dispersion"] == 0.20
    assert kwargs["period_miss_limit"] == 1.5

    monkeypatch.setenv("BREVITAS_WARM_HAZARD_PRIOR_HOURS", "8")
    monkeypatch.setenv("BREVITAS_WARM_PERIOD_MISS_LIMIT", "3")
    kwargs = _warm_claim_kwargs()
    assert kwargs["prior_hours"] == 8.0
    assert kwargs["period_miss_limit"] == 3.0
    # Out of range is CLAMPED to the bound, not silently accepted: a
    # dispersion of 17 would call every arm a clock, and _warm_bound is the one
    # place in this file that decides what an operator's typo means.
    monkeypatch.setenv("BREVITAS_WARM_PERIOD_MAX_DISPERSION", "17")
    assert _warm_claim_kwargs()["period_max_dispersion"] == 1.0


def test_claim_rejects_out_of_range_convergence_arguments(tmp_path):
    store = make_store(tmp_path, "bounds.db")
    enable(store)
    for bad in ({"prior_hours": 0.0}, {"prior_hours": 10_000.0},
                {"exposure_majority_hours": -1.0},
                {"period_max_dispersion": 0.0},
                {"period_max_dispersion": 2.0},
                {"period_miss_limit": 0.0}):
        with pytest.raises(ValueError):
            claim(store, index_enabled=True, hazard_v2=True, **bad)
