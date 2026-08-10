"""Shared-parent warming dedup (202608100007), SQLite store path.

Two customers of one organization whose requests carry the same long system
prompt are not warming two provider cache entries -- they are warming ONE,
twice. 202608100006's chain tree is what makes the shared span nameable; this
module pins what the claim is then allowed to do with that name.

The invariant every test below exists to protect: dedup may only ever SUBTRACT
a provably redundant ping. It never denies one, it never charges twice, and
with the flag off it does nothing at all.
"""
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from api.store import UsageStore

ORG = "00000000-0000-4000-8000-0000000e0001"
# UUID-shaped because warm_customer_budget keys on a customer REF and
# refuses anything that is neither a uuid nor an erasure tombstone.
LEADER = "00000000-0000-4000-8000-0000000e0002"
PEER = "00000000-0000-4000-8000-0000000e0003"
LEADER_HASH = hashlib.sha256(b"dedup-leader").hexdigest()
PEER_HASH = hashlib.sha256(b"dedup-peer").hexdigest()

ROOT = hashlib.sha256(b"dedup-root").hexdigest()
SHARED = hashlib.sha256(b"dedup-shared").hexdigest()
LEAF_LEADER = hashlib.sha256(b"dedup-leaf-leader").hexdigest()
LEAF_PEER = hashlib.sha256(b"dedup-leaf-peer").hexdigest()
ALT_ROOT = hashlib.sha256(b"dedup-alt-root").hexdigest()
ALT_LEAF = hashlib.sha256(b"dedup-alt-leaf").hexdigest()


def make_store(tmp_path, name="dedup.db"):
    store = UsageStore(str(tmp_path / name))
    store.warm_credentials_upsert(
        ORG, "anthropic", "enc:credential", True, "actor-1", 10.0, 100, 288)
    return store


def chain(digests, *, token_cum=(0, 4096, 6000), salt_version=1):
    """A salted chain as api/server.py's observation boundary would hand it over.

    The store never sees an unsalted chain -- the HMAC happens at the boundary
    -- so these fixtures stand in for whatever that boundary produced.
    """
    labels = [digest[:12] for digest in digests]
    path = ".".join([f"s1.k{salt_version}", "r" + labels[0]] + labels[1:])
    nodes = []
    for depth, digest in enumerate(digests):
        nodes.append({
            "digest": digest, "label": labels[depth],
            "parent": digests[depth - 1] if depth else "",
            "depth": depth,
            "block_tokens": token_cum[depth] - (token_cum[depth - 1] if depth else 0),
            "token_cum": token_cum[depth],
            "block_elements": 0 if depth == 0 else 2,
        })
    return nodes, path


def observe(store, customer, prefix_hash, digests, *, tokens=None,
            token_cum=(0, 4096, 6000), salt_version=1):
    """Observe one arm. `tokens` defaults to the chain's OWN leaf token_cum.

    A fixture whose prefix_tokens is unrelated to its token_cum describes an
    arm no extractor can produce -- the chain covers the whole prefix bar the
    tail -- and it silently disables every rule expressed as a ratio of the
    two, the deferral coverage gate among them.
    """
    nodes, path = chain(digests, token_cum=token_cum, salt_version=salt_version)
    return store.warm_prefix_observe(
        ORG, customer, "anthropic", prefix_hash, "enc:payload",
        token_cum[-1] if tokens is None else tokens,
        provider_ttl_seconds=300, safety_margin_seconds=60, cache_read=False,
        chain_nodes=nodes, chain_path=path, chain_salt_version=salt_version)


def siblings(store, *, leader_tokens=None, peer_tokens=None):
    """Two arms sharing the root and one 4096-token block, then forking."""
    observe(store, LEADER, LEADER_HASH, [ROOT, SHARED, LEAF_LEADER],
            tokens=leader_tokens)
    observe(store, PEER, PEER_HASH, [ROOT, SHARED, LEAF_PEER],
            tokens=peer_tokens, token_cum=(0, 4096, 8000))


def prime(store, *, histogram=20, leader_first=True, touch_age_s=0):
    """Make both arms due, ROI-passing, and (by default) warm.

    The leader is made due FIRST on purpose: the claim loop walks the index
    order, not the group order, and 202608100007 defers only behind a leader
    already claimed in the same invocation.
    """
    now = datetime.now(timezone.utc)
    bucket = str((now.weekday()) * 24 + now.hour)
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE warm_prefixes SET next_due_at=?,claim_token='',"
            "arrival_count=?,hour_histogram=?,last_touch_at=? "
            "WHERE organization_id=?",
            ((now - timedelta(seconds=1)).isoformat(), 20,
             json.dumps({bucket: histogram}),
             (now - timedelta(seconds=touch_age_s)).isoformat(), ORG))
        if leader_first:
            db.execute(
                "UPDATE warm_prefixes SET next_due_at=? WHERE organization_id=? "
                "AND customer_id=?",
                ((now - timedelta(seconds=2)).isoformat(), ORG, LEADER))


def claim(store, **kwargs):
    args = {"reserve_usd_per_mtok": 3.75, "roi_min_arrivals": 5,
            "roi_min_p": 0.35, "roi_break_even_p": 0.11, "stop_loss": 3,
            "max_gap_seconds": 3600, "safety_margin_seconds": 60}
    args.update(kwargs)
    return store.warm_due_claim(10, **args)


def decisions(store):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT * FROM warm_decision_log ORDER BY id")]


def ledger_reserved(store):
    with sqlite3.connect(store.db_path) as db:
        return float(db.execute(
            "SELECT reserved_usd FROM warm_budget_ledger WHERE "
            "organization_id=? AND provider='anthropic'", (ORG,)).fetchone()[0])


def reserved_or_zero(store):
    """ledger_reserved, but a policy that reserved nothing writes no row."""
    with sqlite3.connect(store.db_path) as db:
        row = db.execute(
            "SELECT reserved_usd FROM warm_budget_ledger WHERE "
            "organization_id=? AND provider='anthropic'", (ORG,)).fetchone()
    return 0.0 if row is None else float(row[0])


def reset(store):
    with sqlite3.connect(store.db_path) as db:
        db.execute("DELETE FROM warm_decision_log")
        db.execute("DELETE FROM warm_budget_ledger")


def put_envelope(store, customer, envelope_usd):
    """The envelope row written directly, not through warm_customer_budget_put.

    The public writer requires a real customers row; the gate under test reads
    only this table, so the fixture writes what the gate reads.
    """
    period = datetime.now(timezone.utc).date().replace(day=1).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO warm_customer_budget(organization_id,provider,"
            "period_start,customer_ref,envelope_usd,reserved_usd,reserved_day,"
            "spent_usd,source,created_at,updated_at) "
            "VALUES(?,?,?,?,?,0,'',0,'org_override',?,?)",
            (ORG, "anthropic", period, customer, float(envelope_usd), now, now))


def reserve_of(store, customer):
    """The reservation the claim will make, spelled the way the claim spells it."""
    with sqlite3.connect(store.db_path) as db:
        row = db.execute(
            "SELECT ping_reserve_usd,prefix_tokens FROM warm_prefixes WHERE "
            "organization_id=? AND customer_id=?", (ORG, customer)).fetchone()
    return max(float(row[0] or 0.0), round(3.75 * int(row[1]) / 1_000_000.0, 10))


# --------------------------------------------------------------------------- #
# Off is off.
# --------------------------------------------------------------------------- #
def test_dedup_off_by_default_no_grouping(tmp_path):
    store = make_store(tmp_path)
    siblings(store)
    prime(store)

    result = claim(store)

    assert len(result["rows"]) == 2
    assert {row["decision"] for row in decisions(store)} == {"pinged"}
    assert all(row["dedup_group"] is None and row["dedup_role"] is None
               and row["dedup_node_digest"] is None
               for row in decisions(store))
    # Two arms, two reservations: 202608100005's behaviour, unchanged.
    assert ledger_reserved(store) == pytest.approx(
        reserve_of(store, LEADER) + reserve_of(store, PEER))
    assert all("dedup_group" not in row for row in result["rows"])


# --------------------------------------------------------------------------- #
# The group forms, and it charges once.
# --------------------------------------------------------------------------- #
def test_dedup_groups_due_siblings_under_warmed_ancestor(tmp_path):
    store = make_store(tmp_path)
    siblings(store)
    prime(store)

    result = claim(store, parent_dedup=True)

    assert len(result["rows"]) == 1
    rows = {row["decision"]: row for row in decisions(store)}
    assert set(rows) == {"pinged", "dedup_deferred"}
    assert rows["pinged"]["customer_id"] == LEADER
    assert rows["dedup_deferred"]["customer_id"] == PEER
    assert rows["pinged"]["dedup_role"] == "leader"
    assert rows["dedup_deferred"]["dedup_role"] == "deferred"
    assert rows["pinged"]["dedup_group"] == rows["dedup_deferred"]["dedup_group"]
    assert rows["pinged"]["dedup_group_size"] == 2
    assert rows["dedup_deferred"]["dedup_group_size"] == 2
    # The stamp names the SHARED node -- the one the single keep-alive was
    # bought against -- not either arm's leaf.
    assert rows["pinged"]["dedup_node_digest"] == SHARED
    assert rows["dedup_deferred"]["dedup_node_digest"] == SHARED
    assert rows["dedup_deferred"]["claim_token"] is None
    # The claimed row carries the group additively, on the leader only.
    assert result["rows"][0]["dedup_group"] == rows["pinged"]["dedup_group"]
    assert result["rows"][0]["dedup_group_size"] == 2


def test_dedup_leader_is_cheapest_member(tmp_path):
    """Leader = min prefix_tokens; the whole group rides one write, so buy the
    smallest one. Ties break on prefix_hash, deterministically."""
    store = make_store(tmp_path)
    # The PEER customer is now the cheap arm, and it is due SECOND -- so if the
    # implementation led by loop order rather than by price, this would fail.
    siblings(store, leader_tokens=9000, peer_tokens=5000)
    prime(store)

    claim(store, parent_dedup=True)

    rows = {row["decision"]: row for row in decisions(store)}
    assert rows["pinged"]["customer_id"] == PEER
    assert rows["pinged"]["dedup_role"] == "leader"


def test_dedup_leader_tie_breaks_on_prefix_hash(tmp_path):
    store = make_store(tmp_path)
    siblings(store, leader_tokens=7000, peer_tokens=7000)
    prime(store)

    claim(store, parent_dedup=True)

    rows = {row["decision"]: row for row in decisions(store)}
    expected = min(
        [(LEADER_HASH, LEADER), (PEER_HASH, PEER)])[1]
    assert rows["pinged"]["customer_id"] == expected


def test_dedup_single_charge(tmp_path):
    """One provider write, one reservation. The deferred member's own envelope
    is untouched: it reserved nothing because it spent nothing."""
    store = make_store(tmp_path)
    siblings(store)
    prime(store)
    put_envelope(store, PEER, 5.0)
    put_envelope(store, LEADER, 5.0)

    claim(store, parent_dedup=True)

    assert ledger_reserved(store) == pytest.approx(reserve_of(store, LEADER))
    with sqlite3.connect(store.db_path) as db:
        envelopes = dict(db.execute(
            "SELECT customer_ref,reserved_usd FROM warm_customer_budget "
            "WHERE organization_id=?", (ORG,)).fetchall())
    assert envelopes[PEER] == 0.0
    assert envelopes[LEADER] == pytest.approx(reserve_of(store, LEADER))


def test_dedup_combined_hazard_formula(tmp_path):
    """p_group = 1 - (1 - p_own) * (1 - h_peer), reported and never gated."""
    store = make_store(tmp_path)
    siblings(store)
    now = datetime.now(timezone.utc)
    bucket = str(now.weekday() * 24 + now.hour)
    # Distinct, non-degenerate hazards: the leader returns 5 times in 20
    # arrivals, the peer 8 in 20.
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE warm_prefixes SET next_due_at=?,claim_token='',"
            "arrival_count=20,hour_histogram=?,last_touch_at=? "
            "WHERE organization_id=? AND customer_id=?",
            ((now - timedelta(seconds=2)).isoformat(),
             json.dumps({bucket: 5}), now.isoformat(), ORG, LEADER))
        db.execute(
            "UPDATE warm_prefixes SET next_due_at=?,claim_token='',"
            "arrival_count=20,hour_histogram=?,last_touch_at=? "
            "WHERE organization_id=? AND customer_id=?",
            ((now - timedelta(seconds=1)).isoformat(),
             json.dumps({bucket: 8}), now.isoformat(), ORG, PEER))

    result = claim(store, parent_dedup=True, roi_min_p=0.0)

    rows = {row["decision"]: row for row in decisions(store)}
    p_own, h_peer = 5 / 20, 8 / 20
    # The combination is REPORTED on the leader's claim row.
    leader_row = [row for row in result["rows"]
                  if row["customer_id"] == LEADER][0]
    assert leader_row["dedup_p_group"] == pytest.approx(
        1 - (1 - p_own) * (1 - h_peer), abs=1e-12)
    # And it gates NOTHING: the leader is scored, and logged, on its own
    # probability, exactly as it would be with the flag off. A row is never
    # scored on one number and logged with another -- in either direction.
    assert rows["pinged"]["p_return"] == pytest.approx(p_own, abs=1e-12)
    assert rows["dedup_deferred"]["p_return"] == pytest.approx(h_peer, abs=1e-12)


def test_dedup_never_buys_a_ping_the_flag_off_policy_skipped(tmp_path):
    """Dedup may only SUBTRACT. Two arms at h = 0.06 under a 0.11 floor.

    Gating on the combined 1 - 0.94^2 = 0.1164 clears the floor and buys a ping
    that the flag-off policy refused -- the flag paying for warming rather than
    saving it, which is the opposite of everything this module claims.
    """
    store = make_store(tmp_path)
    siblings(store)
    now = datetime.now(timezone.utc)
    bucket = str(now.weekday() * 24 + now.hour)

    def arm():
        with sqlite3.connect(store.db_path) as db:
            db.execute(
                "UPDATE warm_prefixes SET next_due_at=?,claim_token='',"
                "arrival_count=50,hour_histogram=?,last_touch_at=? "
                "WHERE organization_id=?",
                ((now - timedelta(seconds=1)).isoformat(),
                 json.dumps({bucket: 3}), now.isoformat(), ORG))
            db.execute(
                "UPDATE warm_prefixes SET next_due_at=? WHERE organization_id=? "
                "AND customer_id=?",
                ((now - timedelta(seconds=2)).isoformat(), ORG, LEADER))

    arm()
    claim(store, parent_dedup=False, roi_break_even_p=0.11)
    off_reserved = reserved_or_zero(store)
    off_decisions = [row["decision"] for row in decisions(store)]
    assert off_decisions == ["skipped_roi", "skipped_roi"]
    assert off_reserved == 0.0

    reset(store)
    arm()
    claim(store, parent_dedup=True, roi_break_even_p=0.11)
    on_reserved = reserved_or_zero(store)
    # The property, stated as the inequality the migration promises.
    assert on_reserved <= off_reserved
    assert "pinged" not in {row["decision"] for row in decisions(store)}


def test_dedup_deferred_advances_due(tmp_path):
    store = make_store(tmp_path)
    siblings(store)
    prime(store)

    before = datetime.now(timezone.utc)
    claim(store, parent_dedup=True)

    with sqlite3.connect(store.db_path) as db:
        due = datetime.fromisoformat(db.execute(
            "SELECT next_due_at FROM warm_prefixes WHERE organization_id=? "
            "AND customer_id=?", (ORG, PEER)).fetchone()[0])
    # ttl (300) less the safety margin (60), floored at a minute.
    assert timedelta(seconds=235) <= due - before <= timedelta(seconds=245)


# --------------------------------------------------------------------------- #
# Everything that must NOT group.
# --------------------------------------------------------------------------- #
def test_dedup_member_pings_normally_when_leader_denied(tmp_path):
    """A denied leader authorizes nothing. This is the property that makes the
    flag safe to arm: it can subtract a redundant ping, never a necessary one."""
    store = make_store(tmp_path)
    siblings(store)
    prime(store)
    # An envelope smaller than the leader's own reservation.
    put_envelope(store, LEADER, reserve_of(store, LEADER) / 2)

    result = claim(store, parent_dedup=True)

    rows = [row["decision"] for row in decisions(store)]
    assert "envelope_denied" in rows
    assert "dedup_deferred" not in rows
    assert len(result["rows"]) == 1
    assert result["rows"][0]["customer_id"] == PEER


def test_dedup_never_groups_across_seeds(tmp_path):
    """The seed binds provider, model, TTL tier, vary headers and the
    cache-identity extras, so a different seed is a different root label and no
    shared block label can exist."""
    store = make_store(tmp_path)
    observe(store, LEADER, LEADER_HASH, [ROOT, SHARED, LEAF_LEADER])
    observe(store, PEER, PEER_HASH, [ALT_ROOT, ALT_LEAF],
            token_cum=(0, 8000))
    prime(store)

    result = claim(store, parent_dedup=True)

    assert len(result["rows"]) == 2
    assert all(row["dedup_group"] is None for row in decisions(store))


def test_dedup_cold_ancestor_no_group(tmp_path):
    """No arm under the node has been touched inside the TTL, so there is no
    shared entry to ride and both arms must ping exactly as before."""
    store = make_store(tmp_path)
    siblings(store)
    prime(store, touch_age_s=7200)

    result = claim(store, parent_dedup=True)

    assert len(result["rows"]) == 2
    assert not [row for row in decisions(store)
                if row["decision"] == "dedup_deferred"]


def test_dedup_node_below_provider_floor_no_group(tmp_path):
    """Below the provider's minimum cacheable prefix the provider caches
    nothing, so one keep-alive buys no shared warmth."""
    store = make_store(tmp_path)
    # The shared block is 1023 tokens: one below anthropic's floor.
    observe(store, LEADER, LEADER_HASH, [ROOT, SHARED, LEAF_LEADER],
            token_cum=(0, 1023, 6000))
    observe(store, PEER, PEER_HASH, [ROOT, SHARED, LEAF_PEER],
            token_cum=(0, 1023, 3000))
    prime(store)

    result = claim(store, parent_dedup=True)

    assert len(result["rows"]) == 2
    assert all(row["dedup_group"] is None for row in decisions(store))


def test_dedup_floor_carries_headroom_for_the_token_cum_basis(tmp_path):
    """token_cum is not the provider's count, so the floor cannot be the raw one.

    A node at token_cum 1100 is over anthropic's raw 1024 minimum but its true
    cacheable span, on the text-only basis the provider actually counts, is
    ~980-1050 -- under the minimum. The provider caches nothing, the shared
    entry never exists, and the group is the accounting fiction the floor is
    there to prevent.
    """
    store = make_store(tmp_path)
    observe(store, LEADER, LEADER_HASH, [ROOT, SHARED, LEAF_LEADER],
            token_cum=(0, 1100, 2000))
    observe(store, PEER, PEER_HASH, [ROOT, SHARED, LEAF_PEER],
            token_cum=(0, 1100, 3000))
    prime(store)

    result = claim(store, parent_dedup=True)

    assert len(result["rows"]) == 2
    assert all(row["dedup_group"] is None for row in decisions(store))

    # And a node clear of the headroom still groups: the floor is raised, not
    # switched off.
    over = make_store(tmp_path, name="dedup-floor-over.db")
    observe(over, LEADER, LEADER_HASH, [ROOT, SHARED, LEAF_LEADER],
            token_cum=(0, 1400, 2000))
    observe(over, PEER, PEER_HASH, [ROOT, SHARED, LEAF_PEER],
            token_cum=(0, 1400, 3000))
    prime(over)
    assert len(claim(over, parent_dedup=True)["rows"]) == 1


def test_dedup_does_not_defer_an_arm_the_ping_barely_covers(tmp_path):
    """Deferral is a substitution, not a downgrade.

    The leader's ping warms the leader's own prefix and nothing past the fork,
    and the leader rule (cheapest prefix first) picks precisely the arm that
    covers the least. An arm sharing 4k of a 200k-token prefix keeps warmth
    over 2% of what it needs; deferring it to save the cheapest ping in the
    group trades almost all of its warmth for almost none of the spend.
    """
    store = make_store(tmp_path)
    observe(store, LEADER, LEADER_HASH, [ROOT, SHARED, LEAF_LEADER],
            token_cum=(0, 4096, 5000))
    observe(store, PEER, PEER_HASH, [ROOT, SHARED, LEAF_PEER],
            token_cum=(0, 4096, 200_000))
    prime(store)

    result = claim(store, parent_dedup=True)

    # The group still FORMS -- the node is over the provider floor and warm --
    # but the thinly-covered member runs every gate as it would with the flag
    # off rather than being deferred.
    rows = {row["customer_id"]: row for row in decisions(store)}
    assert rows[PEER]["decision"] == "pinged"
    assert rows[LEADER]["decision"] == "pinged"
    assert len(result["rows"]) == 2
    # Which is still never MORE than the flag-off policy spends.
    off = make_store(tmp_path, name="dedup-coverage-off.db")
    observe(off, LEADER, LEADER_HASH, [ROOT, SHARED, LEAF_LEADER],
            token_cum=(0, 4096, 5000))
    observe(off, PEER, PEER_HASH, [ROOT, SHARED, LEAF_PEER],
            token_cum=(0, 4096, 200_000))
    prime(off)
    claim(off, parent_dedup=False)
    assert ledger_reserved(store) <= ledger_reserved(off)


def test_dedup_never_groups_across_salt_versions(tmp_path):
    """Rotation namespaces the tree. Two arms stamped under different salt
    versions name different nodes for the same bytes, and grouping them would
    treat a rotation boundary as a cache boundary."""
    store = make_store(tmp_path)
    observe(store, LEADER, LEADER_HASH, [ROOT, SHARED, LEAF_LEADER],
            salt_version=1)
    observe(store, PEER, PEER_HASH, [ROOT, SHARED, LEAF_PEER],
            token_cum=(0, 4096, 8000), salt_version=2)
    prime(store)

    result = claim(store, parent_dedup=True)

    assert len(result["rows"]) == 2
    assert all(row["dedup_group"] is None for row in decisions(store))


def test_dedup_seed_alone_is_not_a_group_node(tmp_path):
    """Two arms that share ONLY the seed share no content: the seed carries the
    model, the TTL tier and the vary headers and nothing else, so grouping
    there would group strangers."""
    store = make_store(tmp_path)
    observe(store, LEADER, LEADER_HASH, [ROOT, LEAF_LEADER],
            token_cum=(0, 6000))
    observe(store, PEER, PEER_HASH, [ROOT, LEAF_PEER],
            token_cum=(0, 8000))
    prime(store)

    result = claim(store, parent_dedup=True)

    assert len(result["rows"]) == 2
    assert all(row["dedup_group"] is None for row in decisions(store))


def test_dedup_unchained_arm_is_ungroupable(tmp_path):
    """An arm whose observation produced no chain (degraded tokenizer, no salt,
    kill switch, unserialisable element) has no path to group on, and that is a
    property of the data rather than a policy decision."""
    store = make_store(tmp_path)
    observe(store, LEADER, LEADER_HASH, [ROOT, SHARED, LEAF_LEADER])
    store.warm_prefix_observe(
        ORG, PEER, "anthropic", PEER_HASH, "enc:payload", 200_000,
        provider_ttl_seconds=300, safety_margin_seconds=60, cache_read=False)
    prime(store)

    result = claim(store, parent_dedup=True)

    assert len(result["rows"]) == 2
    assert all(row["dedup_group"] is None for row in decisions(store))


def test_dedup_solitary_arm_forms_no_group(tmp_path):
    """A group of one is not a group: it must reduce exactly to the current
    behaviour rather than log a group of one."""
    store = make_store(tmp_path)
    observe(store, LEADER, LEADER_HASH, [ROOT, SHARED, LEAF_LEADER])
    prime(store)

    result = claim(store, parent_dedup=True)

    assert len(result["rows"]) == 1
    assert decisions(store)[0]["dedup_group"] is None


# --------------------------------------------------------------------------- #
# The recorder's own contract.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kwargs", [
    {"dedup_group": str(uuid.uuid4())},                       # role missing
    {"dedup_role": "leader"},                                  # group missing
    {"dedup_group": str(uuid.uuid4()), "dedup_role": "peer"},  # unknown role
    {"dedup_group": str(uuid.uuid4()), "dedup_role": "leader",
     "dedup_group_size": 1},                                   # group of one
    {"dedup_group": str(uuid.uuid4()), "dedup_role": "leader",
     "dedup_node_digest": "not-a-digest"},
])
def test_decision_record_refuses_malformed_group_stamp(tmp_path, kwargs):
    store = make_store(tmp_path)
    with sqlite3.connect(store.db_path) as db:
        with pytest.raises(ValueError):
            store._warm_decision_record_locked(
                db, ORG, LEADER, "anthropic", LEADER_HASH, "pinged", 0.5, 0.11,
                0.01, 100_000, 60.0, 20, 0, None,
                datetime.now(timezone.utc), **kwargs)


def test_dedup_deferred_is_a_recordable_decision(tmp_path):
    store = make_store(tmp_path)
    with sqlite3.connect(store.db_path) as db:
        store._warm_decision_record_locked(
            db, ORG, PEER, "anthropic", PEER_HASH, "dedup_deferred", 0.5, 0.11,
            0.01, 200_000, 60.0, 20, 0, None, datetime.now(timezone.utc),
            dedup_group=str(uuid.uuid4()), dedup_role="deferred",
            dedup_group_size=2, dedup_node_digest=SHARED)
    assert decisions(store)[0]["decision"] == "dedup_deferred"


def test_dedup_v_hit_is_priced_on_the_shared_span(tmp_path):
    """A grouped leader's logged hit value is the value of the SHARED span, not
    of its whole prefix: the group only rides the bytes it shares.

    Scaled down while the reservation stays the leader's full-prefix worst-case
    write -- conservative in both directions. index_score keeps its ratio form
    (p_eff/b - 1 - n_chain), in which the prefix's dollar value cancels, so this
    is a logged quantity and not a second ranking.
    """
    store = make_store(tmp_path)
    siblings(store)
    prime(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_prefixes SET ping_reserve_usd=0.5 "
                   "WHERE organization_id=?", (ORG,))

    ungrouped = claim(store, index_enabled=True)
    assert len(ungrouped["rows"]) == 2
    whole_prefix = [row for row in decisions(store)
                    if row["customer_id"] == LEADER][0]["v_hit_usd"]

    reset(store)
    prime(store)
    claim(store, index_enabled=True, parent_dedup=True)
    rows = {row["decision"]: row for row in decisions(store)}

    # 4096 shared tokens out of the leader's 6000.
    assert rows["pinged"]["v_hit_usd"] == pytest.approx(
        round(whole_prefix * 4096 / 6000, 10), abs=1e-12)
    # index_score is untouched: the dollar value cancels out of the ratio.
    assert rows["pinged"]["index_score"] is not None
    # And the reservation is still the leader's FULL-prefix worst case.
    assert ledger_reserved(store) == pytest.approx(reserve_of(store, LEADER))
