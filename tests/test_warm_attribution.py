"""Airport-game attribution (202608100008), SQLite store path.

One keep-alive ping keeps ONE provider cache entry warm, and every customer
whose request lands inside that entry's TTL flew in on a runway somebody else's
ping paid for. The airport game says how to divide that runway: equally among
the arrivals it actually served.

The invariants every test below exists to protect:

  * conservation -- every priced dollar is a cost share, a speculative loss or
    redundancy, and the job raises rather than write a statement that does not
    add up;
  * the dummy axiom -- a window nobody arrived in allocates NOTHING to anybody,
    ever, no matter what the policy predicted about them;
  * MEASURED-ONLY -- nothing here is a billed quantity.
"""
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from api.store import UsageStore

ORG = "00000000-0000-4000-8000-0000000f0001"
ALPHA = "00000000-0000-4000-8000-0000000f0002"
BETA = "00000000-0000-4000-8000-0000000f0003"
GAMMA = "00000000-0000-4000-8000-0000000f0004"
HASH_ALPHA = hashlib.sha256(b"attr-alpha").hexdigest()
HASH_BETA = hashlib.sha256(b"attr-beta").hexdigest()
HASH_GAMMA = hashlib.sha256(b"attr-gamma").hexdigest()

ROOT = hashlib.sha256(b"attr-root").hexdigest()
SHARED = hashlib.sha256(b"attr-shared").hexdigest()
LEAF_ALPHA = hashlib.sha256(b"attr-leaf-alpha").hexdigest()
LEAF_BETA = hashlib.sha256(b"attr-leaf-beta").hexdigest()
LONE_ROOT = hashlib.sha256(b"attr-lone-root").hexdigest()
LONE_LEAF = hashlib.sha256(b"attr-lone-leaf").hexdigest()

TTL = 300


def yesterday():
    return (datetime.now(timezone.utc).date() - timedelta(days=1))


def moment(day, *, hours=12, seconds=0, minutes=0):
    return (datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            + timedelta(hours=hours, minutes=minutes, seconds=seconds))


def make_store(tmp_path, name="attribution.db", provider="anthropic"):
    store = UsageStore(str(tmp_path / name))
    store.warm_credentials_upsert(
        ORG, provider, "enc:credential", True, "actor-1", 10.0, 100, 288)
    return store


def chain(digests, token_cum, *, salt_version=1):
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


def observe(store, customer, prefix_hash, digests, token_cum, *,
            tokens=None, provider="anthropic", salt_version=1):
    nodes, path = chain(digests, token_cum, salt_version=salt_version)
    return store.warm_prefix_observe(
        ORG, customer, provider, prefix_hash, "enc:payload",
        token_cum[-1] if tokens is None else tokens,
        provider_ttl_seconds=TTL, safety_margin_seconds=60, cache_read=False,
        # The observer-priced worst case for one ping. It is what an UNPRICED
        # ping is reported at, so the fixture has to carry one.
        ping_reserve_usd=0.05,
        chain_nodes=nodes, chain_path=path, chain_salt_version=salt_version)


def siblings(store, provider="anthropic"):
    """Two arms sharing a 4096-token block, then forking."""
    observe(store, ALPHA, HASH_ALPHA, [ROOT, SHARED, LEAF_ALPHA],
            (0, 4096, 6000), provider=provider)
    observe(store, BETA, HASH_BETA, [ROOT, SHARED, LEAF_BETA],
            (0, 4096, 8000), provider=provider)


def usage(store, *, customer, prefix_hash, ts, strategy="cache_hit",
          cost=0.5, authoritative=1, attributable=0, discount=0.0,
          verified=0.0, provider="anthropic"):
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO usage_log(organization_id,customer_id,key_hash,"
            "provider,model,strategy,actual_cost_usd,authoritative,"
            "cache_attributable,native_cache_discount_usd,verified_savings_usd,"
            "warm_prefix_hash,ts) VALUES(?,?,'kh',?,?,?,?,?,?,?,?,?,?)",
            (ORG, customer, provider, "m", strategy,
             (None if cost is None else float(cost)), int(authoritative),
             int(attributable), float(discount), float(verified),
             prefix_hash, ts.isoformat()))


def statement(store, day, provider="anthropic"):
    return {row["customer_ref"]: row for row in store.warm_attribution_list(
        ORG, provider, day.isoformat(), day.isoformat())}


def residual(store, day, provider="anthropic"):
    rows = store.warm_attribution_residual_get(
        ORG, provider, day.isoformat(), day.isoformat())
    return rows[0] if rows else None


# --------------------------------------------------------------------------
# Cost: the split telescopes to the ping, exactly.
# --------------------------------------------------------------------------
def test_costs_split_telescopes_to_ping_cost_exactly(tmp_path):
    store = make_store(tmp_path)
    siblings(store)
    day = yesterday()
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=1.0)
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA,
          ts=moment(day, seconds=30))
    store.warm_attribution_run(2, 7)
    footer = residual(store, day)
    assert footer["total_warm_spend_usd"] == pytest.approx(1.0, abs=1e-12)
    # Alpha is the only reader, so the whole ping -- the 4096-token shared
    # block AND the 1904-token leaf AND the zero-token seed -- lands on it.
    rows = statement(store, day)
    assert rows[ALPHA]["warming_cost_share_usd"] == pytest.approx(1.0, abs=1e-12)
    assert (footer["allocated_usd"] + footer["unallocated_speculative_usd"]
            + footer["redundancy_usd"]) == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------
# The airport rule.
# --------------------------------------------------------------------------
def test_airport_equal_split_among_distinct_customers(tmp_path):
    store = make_store(tmp_path)
    siblings(store)
    day = yesterday()
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=1.0)
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA,
          ts=moment(day, seconds=30))
    usage(store, customer=BETA, prefix_hash=HASH_BETA,
          ts=moment(day, minutes=1))
    store.warm_attribution_run(2, 7)
    rows = statement(store, day)
    shared_share = 1.0 * 4096 / 6000
    # Beta only ever touched the shared block, so it pays exactly half of it.
    assert rows[BETA]["warming_cost_share_usd"] == pytest.approx(
        shared_share / 2, abs=1e-9)
    # Alpha pays the other half plus the whole of its own leaf.
    assert rows[ALPHA]["warming_cost_share_usd"] == pytest.approx(
        1.0 - shared_share / 2, abs=1e-9)
    assert rows[BETA]["nodes_shared"] == 1
    assert rows[BETA]["avg_split_denominator"] == pytest.approx(2.0)
    assert rows[ALPHA]["avg_split_denominator"] == pytest.approx(1.5)
    # And the two halves are the whole dollar. Nothing evaporates in rounding.
    assert (rows[ALPHA]["warming_cost_share_usd"]
            + rows[BETA]["warming_cost_share_usd"]) == pytest.approx(1.0, abs=1e-9)


def test_arrival_at_epoch_start_included(tmp_path):
    """An arrival at the ping's own instant is INSIDE the window it opened."""
    store = make_store(tmp_path)
    siblings(store)
    day = yesterday()
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=1.0)
    usage(store, customer=BETA, prefix_hash=HASH_BETA, ts=moment(day))
    store.warm_attribution_run(2, 7)
    rows = statement(store, day)
    # Beta was the sole arrival in the shared block's window and takes all of
    # that block's share; alpha's own leaf went unread and is speculative.
    assert rows[BETA]["warming_cost_share_usd"] == pytest.approx(
        1.0 * 4096 / 6000, abs=1e-9)
    footer = residual(store, day)
    assert footer["unallocated_speculative_usd"] == pytest.approx(
        1.0 - 1.0 * 4096 / 6000, abs=1e-9)


def test_organic_refresh_allocates_nothing(tmp_path):
    """Traffic that keeps its own entry warm pays for it itself."""
    store = make_store(tmp_path)
    siblings(store)
    day = yesterday()
    # Two arrivals, no ping anywhere. Warm by organic traffic alone.
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          attributable=1, discount=0.9)
    usage(store, customer=BETA, prefix_hash=HASH_BETA,
          ts=moment(day, minutes=1), attributable=1, discount=0.9)
    store.warm_attribution_run(2, 7)
    footer = residual(store, day)
    assert footer["total_warm_spend_usd"] == 0.0
    assert footer["allocated_usd"] == 0.0
    rows = statement(store, day)
    assert rows[ALPHA]["warming_cost_share_usd"] == 0.0
    assert rows[BETA]["warming_cost_share_usd"] == 0.0
    # No brevitas window existed, so no discount is Brevitas's to claim.
    assert rows[ALPHA]["warm_attributed_savings_usd"] == 0.0
    assert rows[BETA]["warm_attributed_savings_usd"] == 0.0


# --------------------------------------------------------------------------
# The dummy axiom.
# --------------------------------------------------------------------------
def test_warmed_never_read_goes_to_speculative_with_miss_log(tmp_path):
    store = make_store(tmp_path)
    siblings(store)
    day = yesterday()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO warm_decision_log(organization_id,customer_id,provider,"
            "prefix_hash,decision,p_return,roi_floor,reserve_usd,prefix_tokens,"
            "arrival_count,ts) VALUES(?,?,?,?,'pinged',0.42,0.11,1.0,6000,9,?)",
            (ORG, ALPHA, "anthropic", HASH_ALPHA,
             moment(day, hours=11).isoformat()))
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=1.0)
    store.warm_attribution_run(2, 7)
    footer = residual(store, day)
    assert footer["unallocated_speculative_usd"] == pytest.approx(1.0, abs=1e-9)
    assert footer["allocated_usd"] == 0.0
    # Nobody read anything, so nobody has a statement row at all.
    assert statement(store, day) == {}
    with sqlite3.connect(store.db_path) as db:
        misses = db.execute(
            "SELECT node_digest,usd,predicted_customer_ref,p_return "
            "FROM warm_prefix_cost_miss WHERE organization_id=? AND day=? "
            "ORDER BY node_digest", (ORG, day.isoformat())).fetchall()
    # One row per priced window: the shared block and the leaf. The seed block
    # carries no tokens, so it carries no dollars and needs no apology.
    assert {row[0] for row in misses} == {SHARED, LEAF_ALPHA}
    assert sum(row[1] for row in misses) == pytest.approx(1.0, abs=1e-9)
    # The prediction is kept beside the failed bet so the model can be scored.
    assert {row[2] for row in misses} == {ALPHA}
    assert {row[3] for row in misses} == {0.42}


def test_overlapping_pings_collapse_min_plus_redundancy(tmp_path):
    store = make_store(tmp_path)
    siblings(store)
    day = yesterday()
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=1.0)
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA,
          ts=moment(day, minutes=2), strategy="cache_warm", cost=0.6)
    store.warm_attribution_run(2, 7)
    footer = residual(store, day)
    assert footer["total_warm_spend_usd"] == pytest.approx(1.6, abs=1e-9)
    # One warm window paid for twice. The window is priced at the CHEAPER of
    # the two and the excess is Brevitas's waste, not a customer's bill.
    assert footer["redundancy_usd"] == pytest.approx(1.0, abs=1e-9)
    assert footer["unallocated_speculative_usd"] == pytest.approx(0.6, abs=1e-9)
    assert (footer["allocated_usd"] + footer["unallocated_speculative_usd"]
            + footer["redundancy_usd"]) == pytest.approx(1.6, abs=1e-9)


# --------------------------------------------------------------------------
# Benefit.
# --------------------------------------------------------------------------
def test_benefit_once_per_arrival_not_per_node(tmp_path):
    store = make_store(tmp_path)
    siblings(store)
    day = yesterday()
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=1.0)
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA,
          ts=moment(day, seconds=30), attributable=1, discount=0.2,
          verified=0.15)
    usage(store, customer=BETA, prefix_hash=HASH_BETA,
          ts=moment(day, minutes=1), attributable=1, discount=0.4, verified=0.35)
    store.warm_attribution_run(2, 7)
    rows = statement(store, day)
    # Alpha's chain is three nodes deep and the arrival is credited ONCE, on
    # the leaf window it landed in -- not once per ancestor.
    assert rows[ALPHA]["warm_attributed_savings_usd"] == pytest.approx(0.2)
    # Beta shared the warmed block and PAYS for it, but the ping never warmed
    # beta's own leaf, so beta is credited nothing. Cost flows down the tree;
    # benefit does not flow up it.
    assert rows[BETA]["warm_attributed_savings_usd"] == 0.0
    assert rows[BETA]["warming_cost_share_usd"] > 0
    # The display copy is usage_log's own number and nothing else.
    assert rows[ALPHA]["verified_savings_usd"] == pytest.approx(0.15)
    assert rows[BETA]["verified_savings_usd"] == pytest.approx(0.35)
    assert rows[ALPHA]["net_usd"] == pytest.approx(
        rows[ALPHA]["warm_attributed_savings_usd"]
        - rows[ALPHA]["warming_cost_share_usd"])


def test_deepseek_unattributable_read_earns_zero_credit(tmp_path):
    """DeepSeek caches on its own. Containment is a candidate, not a credit."""
    store = make_store(tmp_path, provider="deepseek")
    siblings(store, provider="deepseek")
    day = yesterday()
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=1.0, provider="deepseek")
    # A read with a real native discount, but cache_attributable false: the
    # provider's automatic cache did this, not a Brevitas marker.
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA,
          ts=moment(day, seconds=30), attributable=0, discount=5.0,
          provider="deepseek")
    store.warm_attribution_run(2, 7)
    rows = statement(store, day, provider="deepseek")
    assert rows[ALPHA]["warm_attributed_savings_usd"] == 0.0
    # The cost is still allocated -- the ping was still paid for.
    assert rows[ALPHA]["warming_cost_share_usd"] == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------
# Unpriced spend, conservation, closed days, depth bound.
# --------------------------------------------------------------------------
def test_unpriced_ping_excluded_and_reported(tmp_path):
    store = make_store(tmp_path)
    siblings(store)
    day = yesterday()
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=None)
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA,
          ts=moment(day, seconds=30))
    store.warm_attribution_run(2, 7)
    footer = residual(store, day)
    # A cost with no number cannot be divided. It is reported beside the
    # allocation, never smuggled into it.
    assert footer["total_warm_spend_usd"] == 0.0
    assert footer["allocated_usd"] == 0.0
    assert footer["unpriced_usd"] > 0.0
    rows = statement(store, day)
    assert rows[ALPHA]["warming_cost_share_usd"] == 0.0


def test_conservation_invariant_raises_on_corruption(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    siblings(store)
    day = yesterday()
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=1.0)
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA,
          ts=moment(day, seconds=30))
    original = UsageStore._warm_attribution_split

    def corrupt(amount, nodes, leaf, prefix_tokens, flat):
        shares = original(amount, nodes, leaf, prefix_tokens, flat)
        # Manufacture a dollar out of nothing, which is exactly the class of
        # bug the invariant exists to make impossible to ship.
        return [(digest, usd * 2) for digest, usd in shares]

    monkeypatch.setattr(UsageStore, "_warm_attribution_split",
                        staticmethod(corrupt))
    with pytest.raises(ValueError, match="conservation"):
        store.warm_attribution_run(2, 7)


def test_closed_days_never_recomputed(tmp_path):
    store = make_store(tmp_path)
    siblings(store)
    day = yesterday()
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=1.0)
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA,
          ts=moment(day, seconds=30))
    store.warm_attribution_run(2, 7)
    assert ALPHA in statement(store, day)
    with sqlite3.connect(store.db_path) as db:
        db.execute("DELETE FROM warm_attribution_daily WHERE day=?",
                   (day.isoformat(),))
    # Yesterday is now closed. History is not the job's to rewrite, hole or no.
    result = store.warm_attribution_run(2, 1)
    assert result["days_scanned"] == 0
    assert statement(store, day) == {}
    # Re-open it and the statement comes back byte for byte.
    store.warm_attribution_run(2, 7)
    assert statement(store, day)[ALPHA]["warming_cost_share_usd"] == pytest.approx(
        1.0, abs=1e-9)


def test_depth_bound_falls_back_flat(tmp_path):
    store = make_store(tmp_path)
    observe(store, GAMMA, HASH_GAMMA, [LONE_ROOT, LONE_LEAF], (0, 4096))
    # Break the ancestry: the leaf can no longer be walked to a root, so the
    # job must attribute FLAT rather than spread the cost over a span it
    # cannot prove was shared.
    with sqlite3.connect(store.db_path) as db:
        db.execute("DELETE FROM warm_prefix_node WHERE node_digest=?",
                   (LONE_ROOT,))
    day = yesterday()
    usage(store, customer=GAMMA, prefix_hash=HASH_GAMMA, ts=moment(day),
          strategy="cache_warm", cost=2.0)
    usage(store, customer=GAMMA, prefix_hash=HASH_GAMMA,
          ts=moment(day, seconds=30))
    store.warm_attribution_run(2, 7)
    rows = statement(store, day)
    assert rows[GAMMA]["warming_cost_share_usd"] == pytest.approx(2.0, abs=1e-9)
    assert rows[GAMMA]["nodes_read"] == 1
    footer = residual(store, day)
    assert (footer["allocated_usd"] + footer["unallocated_speculative_usd"]
            + footer["redundancy_usd"]) == pytest.approx(2.0, abs=1e-9)


def test_idempotent_rerun_restates_the_open_day(tmp_path):
    store = make_store(tmp_path)
    siblings(store)
    day = yesterday()
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=1.0)
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA,
          ts=moment(day, seconds=30))
    first = store.warm_attribution_run(2, 7)
    before = statement(store, day)
    second = store.warm_attribution_run(2, 7)
    assert first["rows_written"] == second["rows_written"]
    assert statement(store, day)[ALPHA]["warming_cost_share_usd"] == pytest.approx(
        before[ALPHA]["warming_cost_share_usd"])
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM warm_attribution_daily").fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM warm_attribution_residual").fetchone()[0] == 1


def test_arms_without_a_chain_are_invisible(tmp_path):
    """No tree, no attribution. Inventing an ancestry is the one guess this
    job refuses to make."""
    store = make_store(tmp_path)
    store.warm_prefix_observe(
        ORG, ALPHA, "anthropic", HASH_ALPHA, "enc:payload", 6000,
        provider_ttl_seconds=TTL, safety_margin_seconds=60, cache_read=False)
    day = yesterday()
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=1.0)
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA,
          ts=moment(day, seconds=30))
    store.warm_attribution_run(2, 7)
    assert statement(store, day) == {}
    assert residual(store, day) is None


def test_subject_erasure_tombstones_refs_dollars_survive(tmp_path):
    """The SQLite mirror has no compliance path (erasure is Postgres-only), so
    what is pinned here is the migration's own text: the tombstone is applied
    to the attribution statement and to the predicted beneficiary, the dollars
    are never touched, and the residual footer -- which carries no customer key
    -- is left alone."""
    migration = (
        "supabase/migrations/202608100008_warm_prefix_tree_attribution.sql")
    with open(migration, encoding="utf-8") as handle:
        text = handle.read()
    assert "update public.warm_attribution_daily statement\n" \
           "           set customer_ref = 'erased:'" in text
    assert "update public.warm_prefix_cost_miss miss\n" \
           "           set predicted_customer_ref = 'erased:'" in text
    # A tombstone must be RANDOM, not an HMAC: an HMAC would leave Brevitas
    # able to re-link an erased subject to their spend.
    assert text.count(
        "'erased:' || encode(sha256(convert_to(\n"
        "                   gen_random_uuid()::text || gen_random_uuid()::text") >= 3
    # Dollars survive: the tombstone sets the key and nothing else.
    for column in ("warming_cost_share_usd", "warm_attributed_savings_usd",
                   "net_usd"):
        assert f"set {column}" not in text
    # Tenant erasure takes all three tables outright.
    for table in ("warm_attribution_daily", "warm_attribution_residual",
                  "warm_prefix_cost_miss"):
        assert f"delete from public.{table} " in text


def test_attribution_never_writes_the_money_path(tmp_path):
    """MEASURED-ONLY, asserted rather than trusted."""
    migration = (
        "supabase/migrations/202608100008_warm_prefix_tree_attribution.sql")
    with open(migration, encoding="utf-8") as handle:
        text = handle.read()
    body = text.split("create or replace function public.warm_attribution_run")[1]
    body = body.split("$$ language plpgsql")[0]
    for forbidden in ("insert into public.usage_log",
                      "update public.usage_log",
                      "public.warm_budget_ledger",
                      "public.billing_ledger",
                      "verified_savings_usd = ",
                      "public.billing_period_settlement"):
        assert forbidden not in body
    # And the same on the mirror.
    with open("api/store.py", encoding="utf-8") as handle:
        store_text = handle.read()
    mirror = store_text.split("def warm_attribution_run")[1].split(
        "def warm_org_mode_set")[0]
    for table in ("usage_log", "warm_budget_ledger", "billing_ledger",
                  "warm_customer_budget", "period_settlement_ledger"):
        for verb in (f"INSERT INTO {table}", f"UPDATE {table}",
                     f"DELETE FROM {table}"):
            assert verb not in mirror


# --------------------------------------------------------------------------
# The token basis. warm_prefixes.prefix_tokens and warm_prefix_node.token_cum
# are two DIFFERENT measurements -- a text-only count on the anthropic path
# versus count_tokens() over canonical JSON -- and every hand-written fixture
# above happens to seed them equal, which is precisely the assumption that hid
# a signed mis-split. These drive the real extractor, where they are not equal.
# --------------------------------------------------------------------------
def real_chain(turns=120, words=8):
    """Real extractor output: an anthropic prefix and the nodes it would store.

    Chosen so token_cum runs measurably ABOVE prefix_tokens -- the direction
    every measured anthropic arm actually runs -- and far enough above that the
    old prefix_tokens-normalized split handed the ancestors more than the whole
    ping and left the leaf a NEGATIVE share.
    """
    from brevitas.warming import extract_warm_prefix

    def filler(seed):
        return " ".join(f"{seed}-{i}" for i in range(words))

    messages = []
    for i in range(turns):
        messages.append({"role": "user", "content": [
            {"type": "text", "text": filler(f"u{i}")}]})
        messages.append({"role": "assistant", "content": [
            {"type": "text", "text": filler(f"a{i}")}]})
    messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    messages.append({"role": "user", "content": "volatile tail"})
    prefix = extract_warm_prefix(
        {"model": "claude-sonnet-4-5",
         "system": [{"type": "text", "text": filler("system")}],
         "messages": messages},
        "anthropic", "claude-sonnet-4-5", {},
        {"cache_control_owner": "caller"})
    assert prefix is not None and prefix.chain is not None
    built = prefix.chain
    assert len(built.digests) >= 3
    # The seed node carries no tokens; block d's counts sit at index d-1.
    nodes, path = chain(list(built.digests), [0] + list(built.token_cum))
    return prefix, built, nodes, path


def test_real_extractor_puts_token_cum_above_prefix_tokens():
    """The precondition the split has to survive, pinned on real output."""
    prefix, built, _nodes, _path = real_chain()
    assert built.token_cum[-1] > prefix.prefix_tokens
    # And the documented "the tail rides on the leaf" never happens on this
    # path: tail_tokens is clamped at zero, so the leaf carries a DEFICIT
    # unless the split is normalized on the chain's own basis.
    assert built.tail_tokens == 0


def test_split_normalizes_on_the_chain_basis_and_never_goes_negative():
    prefix, built, nodes, _path = real_chain()
    leaf = str(nodes[-1]["digest"])
    shares = UsageStore._warm_attribution_split(
        1.0, nodes, leaf, prefix.prefix_tokens, False)
    by_digest = dict(shares)
    # Conservation still holds -- it always did, which is why it could not see
    # this -- but now NO share is negative and the leaf keeps its own block.
    assert sum(usd for _d, usd in shares) == pytest.approx(1.0, abs=1e-9)
    assert all(usd >= 0 for _d, usd in shares)
    assert by_digest[leaf] == pytest.approx(
        built.block_tokens[-1] / built.token_cum[-1], abs=1e-9)
    # The old prefix_tokens-normalized arithmetic on this same real input.
    ancestors = built.token_cum[-2]
    assert 1.0 - ancestors / prefix.prefix_tokens < 0
    # Every ancestor is weighted on the basis its own block_tokens came from.
    for node in nodes[1:-1]:
        assert by_digest[str(node["digest"])] == pytest.approx(
            node["block_tokens"] / built.token_cum[-1], abs=1e-9)


def test_negative_leaf_share_raises_rather_than_redistributing(tmp_path):
    """A basis mismatch is a signed redistribution conservation cannot see."""
    nodes = [
        {"digest": ROOT, "block_tokens": 0, "token_cum": 0},
        {"digest": SHARED, "block_tokens": 5000, "token_cum": 5000},
        {"digest": LEAF_ALPHA, "block_tokens": 100, "token_cum": 4000},
    ]
    with pytest.raises(ValueError, match="leaf share is negative"):
        UsageStore._warm_attribution_split(1.0, nodes, LEAF_ALPHA, 4000, False)


def test_real_chain_statement_allocates_no_negative_dollar(tmp_path):
    """End to end: a real-basis arm produces no negative operator-facing row."""
    store = make_store(tmp_path)
    prefix, built, nodes, path = real_chain()
    leaf_digest = str(nodes[-1]["digest"])
    # A sibling that forks after the first block, so there is a shared ancestor
    # to over-allocate to if the basis is ever wrong again.
    fork = hashlib.sha256(b"attr-real-fork").hexdigest()
    sib_nodes, sib_path = chain(
        [nodes[0]["digest"], nodes[1]["digest"], fork],
        [0, built.token_cum[0], built.token_cum[0] + 900])
    for customer, prefix_hash, node_list, node_path, tokens in (
            (ALPHA, HASH_ALPHA, nodes, path, prefix.prefix_tokens),
            (BETA, HASH_BETA, sib_nodes, sib_path, built.token_cum[0] + 800)):
        store.warm_prefix_observe(
            ORG, customer, "anthropic", prefix_hash, "enc:payload", tokens,
            provider_ttl_seconds=TTL, safety_margin_seconds=60,
            cache_read=False, ping_reserve_usd=0.05,
            chain_nodes=node_list, chain_path=node_path, chain_salt_version=1)
    day = yesterday()
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA, ts=moment(day),
          strategy="cache_warm", cost=1.0)
    usage(store, customer=ALPHA, prefix_hash=HASH_ALPHA,
          ts=moment(day, seconds=30))
    usage(store, customer=BETA, prefix_hash=HASH_BETA,
          ts=moment(day, minutes=1))
    store.warm_attribution_run(2, 7)
    rows = statement(store, day)
    assert rows and all(
        row["warming_cost_share_usd"] >= 0 for row in rows.values())
    footer = residual(store, day)
    assert footer["unallocated_speculative_usd"] >= 0
    assert footer["allocated_usd"] >= 0
    assert (footer["allocated_usd"] + footer["unallocated_speculative_usd"]
            + footer["redundancy_usd"]) == pytest.approx(1.0, abs=1e-9)
    # Beta only ever touched the shared first block, so it pays half of that
    # block's share of the ping -- on the chain's basis, never above it.
    assert rows[BETA]["warming_cost_share_usd"] == pytest.approx(
        (built.block_tokens[0] / built.token_cum[-1]) / 2, abs=1e-9)
    assert leaf_digest not in str(rows)
