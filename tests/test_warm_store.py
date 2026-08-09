"""SQLite warm-store paths: observe/claim/settle, budget arithmetic, cache_stats."""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from api.store import UsageStore, _WARM_CLAIM_LOCK

ORG = "org-warm-1"
CUSTOMER = "customer-1"
PREFIX_HASH = hashlib.sha256(b"prefix-1").hexdigest()


def make_store(tmp_path, name="warm.db"):
    return UsageStore(str(tmp_path / name))


def enable_warming(store, org=ORG, budget=10.0, max_customers=100,
                   max_pings=288, ciphertext="enc:credential"):
    return store.warm_credentials_upsert(
        org, "anthropic", ciphertext, True, "actor-1", budget,
        max_customers, max_pings)


def observe(store, org=ORG, customer=CUSTOMER, prefix_hash=PREFIX_HASH,
            tokens=100_000, cache_read=False):
    return store.warm_prefix_observe(
        org, customer, "anthropic", prefix_hash, "enc:payload", tokens,
        provider_ttl_seconds=300, safety_margin_seconds=60,
        cache_read=cache_read)


def claim(store, limit=10, reserve_per_mtok=3.75, stop_loss=3, **kwargs):
    return store.warm_due_claim(
        limit, reserve_usd_per_mtok=reserve_per_mtok, roi_min_arrivals=5,
        roi_min_p=0.35, roi_break_even_p=0.11, stop_loss=stop_loss,
        max_gap_seconds=3600, safety_margin_seconds=60, **kwargs)


def backdate_due(store, org=ORG):
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE warm_prefixes SET next_due_at=? WHERE organization_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), org))


def ledger_row(store, org=ORG):
    with sqlite3.connect(store.db_path) as db:
        return db.execute(
            "SELECT reserved_usd,spent_usd FROM warm_budget_ledger "
            "WHERE organization_id=? AND provider='anthropic'", (org,)).fetchone()


def test_observe_requires_enabled_credential(tmp_path):
    store = make_store(tmp_path)
    assert observe(store)["status"] == "not_enabled"
    assert store.warm_enabled(ORG) is False


def test_observe_validates_prefix_hash_and_provider(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    with pytest.raises(ValueError):
        observe(store, prefix_hash="not-a-digest")
    # groq reads at 0.50x: a keep-alive ping costs exactly what a return
    # saves, so the storage layer refuses to represent warming for it at all
    # (measurement only, per migration 202607280003's provider CHECKs).
    with pytest.raises(ValueError):
        store.warm_prefix_observe(ORG, CUSTOMER, "groq", PREFIX_HASH,
                                  "enc:payload", 1000, 300, 60, False)


def test_enable_requires_consent_and_budget(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.warm_credentials_upsert(ORG, "anthropic", "enc:c", True, "",
                                      10.0, 100, 288)
    with pytest.raises(ValueError):
        store.warm_credentials_upsert(ORG, "anthropic", "enc:c", True,
                                      "actor-1", 0.0, 100, 288)
    # Empty ciphertext without a stored credential cannot enroll.
    with pytest.raises(ValueError):
        store.warm_credentials_upsert(ORG, "anthropic", "", True, "actor-1",
                                      10.0, 100, 288)


def test_observe_claim_settle_roundtrip(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    assert store.warm_enabled(ORG) is True
    assert observe(store)["status"] == "observed"

    backdate_due(store)
    result = claim(store)
    assert result["status"] == "ok"
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["organization_id"] == ORG
    assert row["customer_id"] == CUSTOMER
    assert row["prefix_hash"] == PREFIX_HASH
    assert row["payload_ciphertext"] == "enc:payload"
    assert row["credential_ciphertext"] == "enc:credential"
    # 3.75 USD/MTok * 100k tokens = 0.375 reserved.
    assert row["reserved_usd"] == pytest.approx(0.375)
    assert ledger_row(store)[0] == pytest.approx(0.375)

    settled = store.warm_ping_settle(
        ORG, CUSTOMER, "anthropic", PREFIX_HASH, row["budget_day"],
        row["reserved_usd"], 0.31, "warmed", 300, 60)
    assert settled["outcome"] == "warmed"
    reserved, spent = ledger_row(store)
    assert reserved == pytest.approx(0.0)
    assert spent == pytest.approx(0.31)
    status = store.warm_status(ORG)["providers"][0]
    assert status["warm_pings"] == 1
    assert status["spent_today_usd"] == pytest.approx(0.31)


def test_claim_backoff_prevents_immediate_reclaim(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    assert len(claim(store)["rows"]) == 1
    # next_due_at moved forward by the claim; a second sweep sees nothing.
    assert claim(store)["rows"] == []


def test_budget_reservation_blocks_overspend(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store, budget=0.5)
    observe(store)
    backdate_due(store)
    row = claim(store)["rows"][0]
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], row["reserved_usd"], 0.375,
                           "warmed", 300, 60)
    # spent 0.375 + new reserve 0.375 exceeds the 0.5 daily budget.
    backdate_due(store)
    assert claim(store)["rows"] == []
    reserved, spent = ledger_row(store)
    assert reserved == pytest.approx(0.0)
    assert spent == pytest.approx(0.375)


def test_release_returns_reservation_without_spend(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    row = claim(store)["rows"][0]
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], row["reserved_usd"], 0.31,
                           "release", 300, 60)
    reserved, spent = ledger_row(store)
    assert reserved == pytest.approx(0.0)
    assert spent == pytest.approx(0.0)
    assert store.warm_status(ORG)["providers"][0]["warm_pings"] == 0


def test_spent_unknown_books_the_full_reservation_as_spend(tmp_path):
    # A ping whose transport failed after the request was written may already
    # have been accepted and charged. Booking $0 for it (the old 'release'
    # path) understates warm_spend_usd, which raises the settlement fee ceiling
    # and overcharges the org, so the reservation books in full.
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    row = claim(store)["rows"][0]
    assert row["reserved_usd"] == pytest.approx(0.375)
    settled = store.warm_ping_settle(
        ORG, CUSTOMER, "anthropic", PREFIX_HASH, row["budget_day"],
        row["reserved_usd"], 0.0, "spent_unknown", 300, 60,
        claim_token=row["claim_token"])
    assert settled["outcome"] == "spent_unknown"
    reserved, spent = ledger_row(store)
    assert reserved == pytest.approx(0.0)
    assert spent == pytest.approx(0.375)
    status = store.warm_status(ORG)["providers"][0]
    # The ping counts (it may have cost money, so it must count against
    # max_pings_per_customer_day) and stays a miss under the pre-charge
    # convention until an arrival converts it.
    assert status["warm_pings"] == 1
    assert status["warm_hits"] == 0
    assert status["spent_today_usd"] == pytest.approx(0.375)
    # next_due_at moved to the TTL horizon: a flapping transport cannot
    # re-ping, and re-charge, on the very next tick.
    assert claim(store)["rows"] == []


def test_spent_unknown_ignores_caller_supplied_spend(tmp_path):
    # There is no receipt behind a spent_unknown ping, so the store never
    # trusts a spend argument that would book less than the reservation.
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    row = claim(store)["rows"][0]
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], row["reserved_usd"], 0.001,
                           "spent_unknown", 300, 60,
                           claim_token=row["claim_token"])
    assert ledger_row(store)[1] == pytest.approx(row["reserved_usd"])


def test_spent_unknown_counts_toward_the_stop_loss(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    for _ in range(3):
        backdate_due(store)
        rows = claim(store, stop_loss=3)["rows"]
        assert len(rows) == 1
        store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                               rows[0]["budget_day"], rows[0]["reserved_usd"],
                               0.0, "spent_unknown", 300, 60,
                               claim_token=rows[0]["claim_token"])
    backdate_due(store)
    assert claim(store, stop_loss=3)["rows"] == []


def test_spent_unknown_settle_is_claim_token_fenced(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    row = claim(store)["rows"][0]
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], row["reserved_usd"], 0.0,
                           "spent_unknown", 300, 60, claim_token="stale-token")
    # Money always books; the prefix mutation stays fenced on the live claim.
    assert ledger_row(store)[1] == pytest.approx(row["reserved_usd"])
    assert store.warm_status(ORG)["providers"][0]["warm_pings"] == 0


def test_stop_loss_halts_unconverted_prefix(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    for _ in range(3):
        backdate_due(store)
        rows = claim(store, stop_loss=3)["rows"]
        assert len(rows) == 1
        store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                               rows[0]["budget_day"], rows[0]["reserved_usd"],
                               0.01, "warmed", 300, 60)
    # Three settled pings without a cache read trip the stop-loss.
    backdate_due(store)
    assert claim(store, stop_loss=3)["rows"] == []
    # A converting return (cache_read=True) resets consecutive_misses.
    observe(store, cache_read=True)
    backdate_due(store)
    assert len(claim(store, stop_loss=3)["rows"]) == 1
    assert store.warm_status(ORG)["providers"][0]["warm_hits"] == 1


def test_auth_failed_settle_halts_org_and_new_key_recovers(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    row = claim(store)["rows"][0]
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], row["reserved_usd"], 0.0,
                           "auth_failed", 300, 60)
    assert store.warm_credentials_get(ORG, "anthropic")["credential_state"] == "auth_failed"
    assert store.warm_enabled(ORG) is False
    assert observe(store)["status"] == "not_enabled"
    # A fresh ciphertext resets the state; empty ciphertext keeps the old key.
    enable_warming(store, ciphertext="enc:credential-2")
    assert store.warm_credentials_get(ORG, "anthropic")["credential_state"] == "active"
    assert store.warm_enabled(ORG) is True


def test_prefix_invalid_settle_stops_only_that_prefix(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    other_hash = hashlib.sha256(b"prefix-2").hexdigest()
    observe(store)
    observe(store, prefix_hash=other_hash)
    backdate_due(store)
    rows = claim(store)["rows"]
    assert len(rows) == 2
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           rows[0]["budget_day"], rows[0]["reserved_usd"], 0.0,
                           "prefix_invalid", 300, 60)
    backdate_due(store)
    remaining = {r["prefix_hash"] for r in claim(store)["rows"]}
    assert remaining == {other_hash}


def test_customer_cap_rejects_new_customers(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store, max_customers=1)
    assert observe(store)["status"] == "observed"
    assert observe(store, customer="customer-2",
                   prefix_hash=hashlib.sha256(b"p2").hexdigest())["status"] == "customer_cap"
    # The existing customer keeps observing.
    assert observe(store)["status"] == "observed"


def test_claim_lease_unavailable_when_lock_held(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    assert _WARM_CLAIM_LOCK.acquire(blocking=False)
    try:
        assert claim(store) == {"status": "lease_unavailable", "rows": []}
    finally:
        _WARM_CLAIM_LOCK.release()
    assert len(claim(store)["rows"]) == 1


def test_observer_priced_reserve_outbids_flat_floor_and_none_keeps_it(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    assert store.warm_prefix_observe(
        ORG, CUSTOMER, "anthropic", PREFIX_HASH, "enc:payload", 100_000,
        provider_ttl_seconds=300, safety_margin_seconds=60, cache_read=False,
        ping_reserve_usd=0.9)["status"] == "observed"
    backdate_due(store)
    row = claim(store)["rows"][0]
    # max(observer-priced worst case 0.9, flat 3.75/MTok * 100k = 0.375):
    # the reservation must upper-bound actual spend or the ceiling leaks.
    assert row["reserved_usd"] == pytest.approx(0.9)
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], row["reserved_usd"], 0.0,
                           "release", 300, 60, claim_token=row["claim_token"])
    # An observe without a price (None) keeps the stored reserve.
    observe(store)
    backdate_due(store)
    assert claim(store)["rows"][0]["reserved_usd"] == pytest.approx(0.9)


def test_settle_with_stale_claim_token_books_ledger_but_not_prefix(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    row = claim(store)["rows"][0]
    assert row["claim_token"]
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], row["reserved_usd"], 0.31,
                           "warmed", 300, 60, claim_token="stale-token")
    # Money always books: the reservation and the ping were both real.
    reserved, spent = ledger_row(store)
    assert reserved == pytest.approx(0.0)
    assert spent == pytest.approx(0.31)
    # ...but a lapsed claimant cannot apply the prefix mutation.
    assert store.warm_status(ORG)["providers"][0]["warm_pings"] == 0
    # The live token applies once, then clears so it cannot be replayed.
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], 0.0, 0.0, "warmed", 300, 60,
                           claim_token=row["claim_token"])
    assert store.warm_status(ORG)["providers"][0]["warm_pings"] == 1
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], 0.0, 0.0, "warmed", 300, 60,
                           claim_token=row["claim_token"])
    assert store.warm_status(ORG)["providers"][0]["warm_pings"] == 1
    # A stale prefix_invalid cannot stop the row its new owner still runs.
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], 0.0, 0.0, "prefix_invalid",
                           300, 60, claim_token="stale-token")
    backdate_due(store)
    assert len(claim(store)["rows"]) == 1


def test_claim_lease_bounds_and_pushes_next_due_past_a_full_batch(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    with pytest.raises(ValueError):
        store.warm_due_claim(
            10, reserve_usd_per_mtok=3.75, roi_min_arrivals=5, roi_min_p=0.35,
            roi_break_even_p=0.11, stop_loss=3, max_gap_seconds=3600,
            safety_margin_seconds=60, claim_lease_seconds=10)
    assert len(claim(store)["rows"]) == 1
    with sqlite3.connect(store.db_path) as db:
        next_due = db.execute(
            "SELECT next_due_at FROM warm_prefixes WHERE organization_id=?",
            (ORG,)).fetchone()[0]
    lease = (datetime.fromisoformat(next_due)
             - datetime.now(timezone.utc)).total_seconds()
    # Default 900s lease, not the 60s tick backoff that let replicas
    # double-claim mid-batch.
    assert 840 <= lease <= 900


def test_credentials_purge_deletes_prefixes_keeps_ledger(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    claim(store)
    purged = store.warm_credentials_purge(ORG, "anthropic")
    assert purged["prefixes_deleted"] == 1
    assert purged["credentials_deleted"] == 1
    assert store.warm_credentials_get(ORG, "anthropic") is None
    # Ledger survives as financial evidence.
    assert ledger_row(store)[0] == pytest.approx(0.375)


def test_deepseek_admitted_end_to_end_on_sqlite(tmp_path):
    # server._WARM_ACTIVE_PROVIDERS advertises deepseek as enable-able, so the
    # SQLite backend must accept it end to end (upsert -> observe -> claim ->
    # settle -> purge) or PUT /v1/warming 400s on the dev-parity backend.
    store = make_store(tmp_path)
    saved = store.warm_credentials_upsert(
        ORG, "deepseek", "enc:ds-credential", True, "actor-1", 10.0, 100, 288)
    assert saved["provider"] == "deepseek" and saved["enabled"] is True
    assert store.warm_enabled(ORG) is True
    observed = store.warm_prefix_observe(
        ORG, CUSTOMER, "deepseek", PREFIX_HASH, "enc:payload", 100_000,
        provider_ttl_seconds=14_400, safety_margin_seconds=60, cache_read=False)
    assert observed["status"] == "observed"
    backdate_due(store)
    row = claim(store)["rows"][0]
    assert row["provider"] == "deepseek"
    settled = store.warm_ping_settle(
        ORG, CUSTOMER, "deepseek", PREFIX_HASH, row["budget_day"],
        row["reserved_usd"], 0.01, "warmed", 14_400, 60,
        claim_token=row["claim_token"])
    assert settled["outcome"] == "warmed"
    purged = store.warm_credentials_purge(ORG, "deepseek")
    assert purged["credentials_deleted"] == 1
    assert purged["prefixes_deleted"] == 1


def test_claim_break_even_map_gates_per_provider(tmp_path):
    # A 5% hour-of-week return rate clears deepseek's 0.02x-read break-even
    # (~0.0204) but not the flat anthropic-calibrated 0.11: the per-provider
    # map must claim what the scalar-only call over-filters.
    store = make_store(tmp_path)
    store.warm_credentials_upsert(
        ORG, "deepseek", "enc:ds-credential", True, "actor-1", 10.0, 100, 288)
    store.warm_prefix_observe(
        ORG, CUSTOMER, "deepseek", PREFIX_HASH, "enc:payload", 100_000,
        provider_ttl_seconds=14_400, safety_margin_seconds=60, cache_read=False)
    now = datetime.now(timezone.utc)
    bucket = str((now.isoweekday() - 1) * 24 + now.hour)
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE warm_prefixes SET arrival_count=20, hour_histogram=?, "
            "ewma_interarrival_s=300.0 WHERE organization_id=?",
            (json.dumps({bucket: 1}), ORG))
    backdate_due(store)
    assert claim(store)["rows"] == []
    rows = claim(store, roi_break_even_by_provider={
        "anthropic": 0.11, "deepseek": 0.020408})["rows"]
    assert [r["provider"] for r in rows] == ["deepseek"]


def test_claim_break_even_map_rejects_unknown_providers_and_bad_values(tmp_path):
    # Mirrors the warm_due_claim RPC's jsonb validation in 202607280003.
    store = make_store(tmp_path)
    for invalid in ({"groq": 0.5}, {"deepseek": 1.5}, {"deepseek": -0.1},
                    {"deepseek": True}, {"deepseek": "0.02"}):
        with pytest.raises(ValueError):
            claim(store, roi_break_even_by_provider=invalid)


def test_purge_warm_state_drops_expired_rows(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    # The ledger day is relative and far outside any retention horizon in play.
    # 202607280017 floors the Postgres ledger horizon at 365 days -- warm spend is
    # the evidence a 7-day settlement period is recomputed from, so a 7-day
    # maintenance window must not be able to delete it -- and this store is the
    # declared SQLite mirror of that function. A fixed '2026-01-01' asserted the
    # purge under the 7-day window only and would have to be edited again the day
    # the mirror adopts the floor.
    aged_out_day = (datetime.now(timezone.utc) - timedelta(days=400)).date().isoformat()
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_prefixes SET expires_at=?", (past,))
        db.execute(
            "INSERT INTO warm_budget_ledger(organization_id,provider,day,"
            "reserved_usd,spent_usd,updated_at) VALUES(?,?,?,0,0,?)",
            (ORG, "anthropic", aged_out_day, past))
    result = store.purge_warm_state(retention_days=7)
    assert result["prefixes_deleted"] == 1
    assert result["ledger_deleted"] == 1


def test_cache_stats_numbers(tmp_path):
    store = make_store(tmp_path)
    key_hash = "kh-cache-stats"
    store.create_key(key_hash, "stats", owner_id="owner-1", organization_id=ORG)
    ts = "2026-07-20T12:00:00+00:00"
    assert store.record_usage(
        key_hash, 100, 100, ts=ts, organization_id=ORG, authoritative=True,
        cached_input_tokens=750, fresh_input_tokens=250,
        native_cache_discount_usd=0.4, cache_attributable=True,
        verified_savings_usd=0.4, calls_avoided=2, actual_cost_usd=0.1)
    assert store.record_usage(
        key_hash, 100, 100, ts="2026-07-13T12:00:00+00:00", organization_id=ORG,
        cached_input_tokens=100, fresh_input_tokens=900,
        native_cache_discount_usd=0.1, cache_attributable=False,
        calls_avoided=0, actual_cost_usd=0.2)

    stats = store.cache_stats(key_hash)
    assert stats["cached_input_tokens"] == 850
    assert stats["fresh_input_tokens"] == 1150
    assert stats["cache_hit_rate_pct"] == pytest.approx(42.5)
    assert stats["native_cache_discount_usd"] == pytest.approx(0.5)
    assert stats["attributable_discount_usd"] == pytest.approx(0.4)
    assert stats["calls_avoided"] == 2
    # No warm_credentials row yet: every warm_* field is null.
    assert stats["warm_pings"] is None
    assert stats["warm_spend_usd"] is None
    assert stats["warm_hits"] is None
    assert [w["week_start"] for w in stats["history"]] == [
        "2026-07-20", "2026-07-13"]
    assert stats["history"][0]["cached_input_tokens"] == 750
    assert stats["history"][0]["native_cache_discount_usd"] == pytest.approx(0.4)
    assert stats["history"][0]["warm_spend_usd"] is None


def test_cache_stats_billable_slice_matches_billing_policy(tmp_path):
    store = make_store(tmp_path)
    key_hash = "kh-billable"
    store.create_key(key_hash, "billable", owner_id="owner-1", organization_id=ORG)
    ts = "2026-07-20T12:00:00+00:00"
    # exact_cache replay: the receipt is all zeros so the discount is 0, but
    # the whole avoided call is billed as verified savings.
    assert store.record_usage(
        key_hash, 300, 0, ts=ts, organization_id=ORG, authoritative=True,
        strategy="exact_cache", cache_attributable=True, calls_avoided=1,
        native_cache_discount_usd=0.0, verified_savings_usd=0.9)
    # Brevitas cache-write row: the negative discount stays measured for
    # analytics but is never billed, so it cannot drag the tile negative.
    assert store.record_usage(
        key_hash, 100, 100, ts=ts, organization_id=ORG, authoritative=True,
        cache_attributable=True, native_cache_discount_usd=-0.05,
        verified_savings_usd=0.0)
    # SDK-reported row claiming attribution: analytics only, never billed.
    assert store.record_usage(
        key_hash, 100, 100, ts=ts, organization_id=ORG, authoritative=False,
        cache_attributable=True, native_cache_discount_usd=0.2,
        verified_savings_usd=0.0)
    # Attributable read: the tile takes the cache slice of the bill, capped by
    # the row's verified savings.
    assert store.record_usage(
        key_hash, 100, 100, ts=ts, organization_id=ORG, authoritative=True,
        cache_attributable=True, native_cache_discount_usd=0.4,
        verified_savings_usd=0.3)

    stats = store.cache_stats(key_hash)
    # Measure-everything analytics keep the raw signed sum...
    assert stats["native_cache_discount_usd"] == pytest.approx(0.55)
    # ...while the billable tile reconciles with the fee basis:
    # 0.9 (exact replay) + 0 (write row) + 0 (SDK row) + 0.3 (capped read).
    assert stats["attributable_discount_usd"] == pytest.approx(1.2)


def test_cache_stats_warm_fields_after_enrollment(tmp_path):
    store = make_store(tmp_path)
    key_hash = "kh-warm-stats"
    store.create_key(key_hash, "stats", owner_id="owner-1", organization_id=ORG)
    enable_warming(store)
    observe(store, cache_read=False)
    backdate_due(store)
    row = claim(store)["rows"][0]
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], row["reserved_usd"], 0.31,
                           "warmed", 300, 60)
    ts = "2026-07-20T12:00:00+00:00"
    assert store.record_usage(
        key_hash, 100, 100, ts=ts, organization_id=ORG, authoritative=True,
        cached_input_tokens=500, fresh_input_tokens=500,
        native_cache_discount_usd=0.25, cache_attributable=True,
        verified_savings_usd=0.25)
    assert store.record_usage(
        key_hash, 1, 1, ts=ts, organization_id=ORG, strategy="cache_warm",
        actual_cost_usd=0.31, receipt_source="worker")

    stats = store.cache_stats(key_hash)
    assert stats["warm_pings"] == 1
    assert stats["warm_hits"] == 0
    assert stats["warm_spend_usd"] == pytest.approx(0.31)
    assert stats["history"][0]["warm_spend_usd"] == pytest.approx(0.31)
    # Warming spend never contaminates the discount aggregates.
    assert stats["native_cache_discount_usd"] == pytest.approx(0.25)
    assert stats["attributable_discount_usd"] == pytest.approx(0.25)


def test_cache_stats_per_provider_before_warming(tmp_path):
    store = make_store(tmp_path)
    key_hash = "kh-per-provider"
    store.create_key(key_hash, "split", owner_id="owner-1", organization_id=ORG)
    ts = "2026-07-20T12:00:00+00:00"
    # Attributable anthropic read: billable slice is the clamped discount.
    assert store.record_usage(
        key_hash, 100, 100, ts=ts, organization_id=ORG, authoritative=True,
        provider="anthropic", model="claude-opus-5",
        cached_input_tokens=500, native_cache_discount_usd=0.25,
        cache_attributable=True, verified_savings_usd=0.2)
    # DeepSeek automatic-cache row: measured, never attributable.
    assert store.record_usage(
        key_hash, 100, 100, ts=ts, organization_id=ORG, authoritative=True,
        provider="deepseek", model="deepseek-v4-flash",
        cached_input_tokens=800, native_cache_discount_usd=0.1,
        cache_attributable=False, verified_savings_usd=0.0)
    # Legacy row without a provider column value: derived from the model
    # name prefix (documented approximation).
    assert store.record_usage(
        key_hash, 100, 100, ts=ts, organization_id=ORG, authoritative=True,
        provider="", model="gpt-5.6-luna", cached_input_tokens=64,
        native_cache_discount_usd=0.01, cache_attributable=False)

    per = {p["provider"]: p for p in store.cache_stats(key_hash)["per_provider"]}
    assert set(per) == {"anthropic", "deepseek", "openai"}
    assert per["anthropic"]["cached_input_tokens"] == 500
    assert per["anthropic"]["native_cache_discount_usd"] == pytest.approx(0.25)
    assert per["anthropic"]["attributable_discount_usd"] == pytest.approx(0.2)
    assert per["deepseek"]["cached_input_tokens"] == 800
    assert per["deepseek"]["attributable_discount_usd"] == pytest.approx(0.0)
    assert per["openai"]["cached_input_tokens"] == 64
    # No warm_credentials row yet: every warm_* field is null, per provider.
    for entry in per.values():
        assert entry["warm_pings"] is None
        assert entry["warm_spend_usd"] is None
        assert entry["warm_hits"] is None


def test_cache_stats_per_provider_warm_fields_after_enrollment(tmp_path):
    store = make_store(tmp_path)
    key_hash = "kh-per-provider-warm"
    store.create_key(key_hash, "split", owner_id="owner-1", organization_id=ORG)
    enable_warming(store)
    observe(store, cache_read=False)
    backdate_due(store)
    row = claim(store)["rows"][0]
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], row["reserved_usd"], 0.31,
                           "warmed", 300, 60)
    # A converting return within the warm window attributes one hit.
    observe(store, cache_read=True)
    ts = "2026-07-20T12:00:00+00:00"
    assert store.record_usage(
        key_hash, 100, 100, ts=ts, organization_id=ORG, authoritative=True,
        provider="anthropic", model="claude-opus-5",
        cached_input_tokens=500, native_cache_discount_usd=0.25,
        cache_attributable=True, verified_savings_usd=0.25)
    assert store.record_usage(
        key_hash, 1, 1, ts=ts, organization_id=ORG, provider="anthropic",
        model="claude-opus-5", strategy="cache_warm", actual_cost_usd=0.31,
        receipt_source="worker")
    # Measurement-only provider alongside: warming never enabled there.
    assert store.record_usage(
        key_hash, 100, 100, ts=ts, organization_id=ORG, authoritative=True,
        provider="groq", model="openai/gpt-oss-120b", cached_input_tokens=128,
        native_cache_discount_usd=0.0, cache_attributable=False)

    per = {p["provider"]: p for p in store.cache_stats(key_hash)["per_provider"]}
    anthropic = per["anthropic"]
    assert anthropic["warm_pings"] == 1
    assert anthropic["warm_hits"] == 1
    assert anthropic["warm_spend_usd"] == pytest.approx(0.31)
    # The warm ping's spend stays out of the discount aggregates.
    assert anthropic["native_cache_discount_usd"] == pytest.approx(0.25)
    assert anthropic["attributable_discount_usd"] == pytest.approx(0.25)
    # Providers without a warm credential report null pings/hits (warming is
    # not built or cannot pay for itself there) but keep measurement fields.
    groq = per["groq"]
    assert groq["warm_pings"] is None
    assert groq["warm_hits"] is None
    assert groq["warm_spend_usd"] == pytest.approx(0.0)
    assert groq["cached_input_tokens"] == 128


# --- Phase 0 instrumentation: decision log + TTL observations (202608090001) ---


def decision_rows(store, org=ORG):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(
            "SELECT * FROM warm_decision_log WHERE organization_id=? ORDER BY id",
            (org,))]


def ttl_rows(store):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(
            "SELECT * FROM warm_ttl_observations ORDER BY id")]


def test_claim_logs_a_pinged_decision_with_the_belief_snapshot(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    row = claim(store)["rows"][0]

    logged = decision_rows(store)
    assert [entry["decision"] for entry in logged] == ["pinged"]
    entry = logged[0]
    assert entry["customer_id"] == CUSTOMER
    assert entry["provider"] == "anthropic"
    assert entry["prefix_hash"] == PREFIX_HASH
    # The snapshot must be the numbers the scorer used, not a re-derivation.
    assert entry["reserve_usd"] == pytest.approx(row["reserved_usd"])
    assert entry["prefix_tokens"] == 100_000
    assert entry["arrival_count"] == 1
    assert 0.0 <= entry["p_return"] <= 1.0
    assert entry["p_return"] >= entry["roi_floor"]
    # A single arrival is below roi_min_arrivals, so the cold-start floor applies.
    assert entry["roi_floor"] == pytest.approx(0.35)
    assert entry["pings_today"] == 0
    # Only a claimed row carries the token settle will key on.
    assert entry["claim_token"] == row["claim_token"]
    assert entry["settle_outcome"] is None
    assert entry["realized_net_usd"] is None
    assert entry["rng_seed"] is None and entry["propensity"] is None


def test_claim_logs_skipped_roi_without_reading_the_ping_cap(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    # Enough arrivals to leave the cold-start floor, none of them in this
    # hour-of-week bucket: the observed return frequency is zero.
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_prefixes SET arrival_count=10,hour_histogram='{}'")
    backdate_due(store)
    assert claim(store)["rows"] == []

    entry, = decision_rows(store)
    assert entry["decision"] == "skipped_roi"
    # arrival_count clears roi_min_arrivals, so the break-even bar applied.
    assert entry["roi_floor"] == pytest.approx(0.11)
    assert entry["p_return"] == pytest.approx(0.0)
    assert entry["arrival_count"] == 10
    # No claim token, and no ledger row was ever created for a denied candidate.
    assert entry["claim_token"] is None
    # pings_today is null because the ROI gate rejects before the cap is read;
    # paying that query for rejected candidates is the cost this avoids.
    assert entry["pings_today"] is None
    # The reserve is still recorded: it is what warming this row would have cost.
    assert entry["reserve_usd"] == pytest.approx(0.375)
    assert ledger_row(store) is None


def test_claim_logs_cap_denied_and_budget_denied_with_the_cap_reading(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store, max_pings=1)
    observe(store)
    backdate_due(store)
    first = claim(store)["rows"][0]
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           first["budget_day"], first["reserved_usd"], 0.05,
                           "warmed", 300, 60,
                           claim_token=first["claim_token"])
    backdate_due(store)
    assert claim(store)["rows"] == []

    capped = decision_rows(store)[-1]
    assert capped["decision"] == "cap_denied"
    # The cap gate ran, so the reading it used is on the row.
    assert capped["pings_today"] == 1
    assert capped["claim_token"] is None

    # A budget too small for the reserve denies at the last gate instead.
    budget_store = make_store(tmp_path, name="warm-budget.db")
    enable_warming(budget_store, budget=0.0001)
    observe(budget_store)
    backdate_due(budget_store)
    assert claim(budget_store)["rows"] == []
    denied, = decision_rows(budget_store)
    assert denied["decision"] == "budget_denied"
    assert denied["pings_today"] == 0
    assert denied["reserve_usd"] == pytest.approx(0.375)
    # The reservation was never taken: a denial must not consume budget.
    assert ledger_row(budget_store) == (0.0, 0.0)


def test_settle_outcome_is_stamped_onto_the_decision_by_claim_token(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    row = claim(store)["rows"][0]
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], row["reserved_usd"], 0.05,
                           "spent_unknown", 300, 60,
                           claim_token=row["claim_token"])
    result = store.warm_decision_settle_outcome(row["claim_token"], "spent_unknown")
    assert result["updated"] == 1
    assert decision_rows(store)[0]["settle_outcome"] == "spent_unknown"

    # A token with no logged decision is a no-op, not an error: decisions that
    # predate the log or aged out of retention have none.
    assert store.warm_decision_settle_outcome(
        "00000000-0000-4000-8000-000000000000", "warmed")["updated"] == 0
    with pytest.raises(ValueError):
        store.warm_decision_settle_outcome(row["claim_token"], "not_an_outcome")


def test_arrival_records_a_free_ttl_observation_after_the_first_touch(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    # The first arrival has no prior touch, so there is no gap to observe.
    observe(store, cache_read=False)
    assert ttl_rows(store) == []

    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_prefixes SET last_touch_at=?",
                   ((datetime.now(timezone.utc)
                     - timedelta(seconds=90)).isoformat(),))
    store.warm_prefix_observe(
        ORG, CUSTOMER, "anthropic", PREFIX_HASH, "enc:payload", 100_000,
        provider_ttl_seconds=300, safety_margin_seconds=60, cache_read=True,
        model_class="claude-sonnet-4-5")

    observation, = ttl_rows(store)
    assert observation["provider"] == "anthropic"
    assert observation["model_class"] == "claude-sonnet-4-5"
    assert observation["ttl_tier"] == "5m"
    assert observation["outcome"] == "warm"
    assert observation["source"] == "arrival"
    assert observation["gap_seconds"] == pytest.approx(90, abs=5)
    # Plane G: the physics table carries no tenant key at all.
    assert "organization_id" not in observation
    assert "customer_id" not in observation
    assert "prefix_hash" not in observation


def test_arrival_ttl_observation_reports_expired_on_a_cache_miss(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_prefixes SET last_touch_at=?",
                   ((datetime.now(timezone.utc)
                     - timedelta(seconds=600)).isoformat(),))
    observe(store, cache_read=False)
    observation, = ttl_rows(store)
    assert observation["outcome"] == "expired"
    assert observation["gap_seconds"] == pytest.approx(600, abs=5)


def test_arrival_skips_the_observation_when_the_gap_is_out_of_bounds(tmp_path):
    """A clock jump must not write nonsense into the physics table, and must
    never fail the observation the response path depends on."""
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_prefixes SET last_touch_at=?",
                   ((datetime.now(timezone.utc)
                     + timedelta(seconds=3600)).isoformat(),))
    assert observe(store)["status"] == "observed"
    assert ttl_rows(store) == []

    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_prefixes SET last_touch_at=?",
                   ((datetime.now(timezone.utc)
                     - timedelta(days=45)).isoformat(),))
    assert observe(store)["status"] == "observed"
    assert ttl_rows(store) == []


def test_only_warmed_advances_the_provable_touch_clock(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    row = claim(store)["rows"][0]
    before = row["last_touch_at"]
    assert before

    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           row["budget_day"], row["reserved_usd"], 0.0,
                           "spent_unknown", 300, 60,
                           claim_token=row["claim_token"])
    with sqlite3.connect(store.db_path) as db:
        unchanged = db.execute(
            "SELECT last_touch_at FROM warm_prefixes").fetchone()[0]
    # 'spent_unknown' means we do not know the provider processed the ping, so
    # a stamped touch would be a fabricated measurement.
    assert unchanged == before

    backdate_due(store)
    second = claim(store)["rows"][0]
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           second["budget_day"], second["reserved_usd"], 0.05,
                           "warmed", 300, 60,
                           claim_token=second["claim_token"])
    with sqlite3.connect(store.db_path) as db:
        advanced = db.execute(
            "SELECT last_touch_at FROM warm_prefixes").fetchone()[0]
    assert advanced > before


def test_claim_reports_last_seen_at_when_the_touch_clock_is_unset(tmp_path):
    """Prefix rows written before 202608090001 have no touch clock; last_seen_at
    is the bound the clock was seeded from and must be reported instead."""
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_prefixes SET last_touch_at=''")
        seen = db.execute("SELECT last_seen_at FROM warm_prefixes").fetchone()[0]
    backdate_due(store)
    assert claim(store)["rows"][0]["last_touch_at"] == seen


def test_purge_ages_ttl_observations_on_the_aggregate_horizon_only(tmp_path):
    store = make_store(tmp_path)
    store.warm_ttl_observe("anthropic", "claude-sonnet-4-5", "5m", 42.0,
                           "warm", "ping")
    store.warm_ttl_observe("deepseek", "deepseek-chat", "auto", 900.0,
                           "expired", "arrival")
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_ttl_observations SET observed_at=? WHERE id=1",
                   ((datetime.now(timezone.utc) - timedelta(days=400)).isoformat(),))
    # retention_days is the operator's prefix-payload window and must not reach
    # the physics evidence: 7 days here still leaves the 42s row alone.
    result = store.purge_warm_state(7)
    assert result["observations_deleted"] == 1
    assert result["observation_retention_days"] == 365
    assert [r["provider"] for r in ttl_rows(store)] == ["deepseek"]


def test_multibyte_model_class_is_byte_clamped_and_never_aborts_observe(tmp_path):
    # Regression: model ids are tenant-controlled, and validators count BYTES
    # (octet_length) while the old producer truncated by characters. A model of
    # >=65 multibyte characters used to raise inside the open observe
    # transaction, rolling back the whole arrival (last_seen_at, EWMA,
    # next_due_at) — warming silently died for that prefix.
    from api.store import warm_model_class

    multibyte = "\u00e9" * 128  # 128 chars, 256 UTF-8 bytes
    clamped = warm_model_class(multibyte)
    assert len(clamped.encode("utf-8")) <= 128
    assert clamped  # degraded, not emptied

    store = make_store(tmp_path)
    enable_warming(store)
    observe(store, cache_read=False)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_prefixes SET last_touch_at=?",
                   ((datetime.now(timezone.utc)
                     - timedelta(seconds=90)).isoformat(),))
    # Must neither raise nor roll back the arrival, and must record the
    # observation under the byte-clamped class.
    result = store.warm_prefix_observe(
        ORG, CUSTOMER, "anthropic", PREFIX_HASH, "enc:payload", 100_000,
        provider_ttl_seconds=300, safety_margin_seconds=60, cache_read=True,
        model_class=warm_model_class(multibyte))
    assert result["status"] == "observed"
    observation, = ttl_rows(store)
    assert len(observation["model_class"].encode("utf-8")) <= 128
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT arrival_count FROM warm_prefixes").fetchone()
    assert row["arrival_count"] == 2  # the arrival itself survived


def test_ttl_observe_rejects_values_the_table_would_refuse(tmp_path):
    store = make_store(tmp_path)
    for bad in (
        ("groq", "m", "5m", 1.0, "warm", "ping"),
        ("anthropic", "m", "10m", 1.0, "warm", "ping"),
        ("anthropic", "m", "5m", -1.0, "warm", "ping"),
        ("anthropic", "m", "5m", 3_000_000.0, "warm", "ping"),
        ("anthropic", "m", "5m", 1.0, "hot", "ping"),
        ("anthropic", "m", "5m", 1.0, "warm", "guess"),
        ("anthropic", "x" * 129, "5m", 1.0, "warm", "ping"),
    ):
        with pytest.raises(ValueError):
            store.warm_ttl_observe(*bad)
    assert ttl_rows(store) == []


# --- Phase 0 instrumentation: the per-ping reward join (202608090002) --------

REWARD_KEY = "kh-reward-join"


def reward_store(tmp_path, name="reward.db"):
    store = make_store(tmp_path, name)
    store.create_key(REWARD_KEY, "reward", owner_id="owner-1",
                     organization_id=ORG)
    return store


def iso(offset_seconds):
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).isoformat()


def log_decision(store, *, ts, settle_outcome="warmed", decision="pinged",
                 prefix_hash=PREFIX_HASH, customer=CUSTOMER, org=ORG):
    """Insert one synthetic decision row directly.

    warm_due_claim is exercised elsewhere; here the join is under test and the
    decision is a fixture, so the row is written with the timestamp the case
    needs rather than whatever `now()` happens to be.
    """
    with sqlite3.connect(store.db_path) as db:
        cursor = db.execute(
            "INSERT INTO warm_decision_log(organization_id,customer_id,provider,"
            "prefix_hash,ts,decision,p_return,roi_floor,reserve_usd,"
            "prefix_tokens,arrival_count,settle_outcome) "
            "VALUES(?,?,'anthropic',?,?,?,0.5,0.11,0.375,100000,7,?)",
            (org, customer, prefix_hash, ts, decision, settle_outcome))
        return int(cursor.lastrowid)


def log_prefix(store, *, ttl=300, prefix_hash=PREFIX_HASH, customer=CUSTOMER,
               org=ORG):
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO warm_prefixes(organization_id,customer_id,"
            "provider,prefix_hash,payload_ciphertext,prefix_tokens,"
            "provider_ttl_seconds,created_at,last_seen_at,next_due_at,expires_at)"
            " VALUES(?,?,'anthropic',?,'enc',100000,?,?,?,?,?)",
            (org, customer, prefix_hash, ttl, iso(-7200), iso(-7200),
             iso(3600), iso(86400)))


def log_ping_row(store, *, ts, cost, prefix_hash=PREFIX_HASH,
                 customer=CUSTOMER, request_id="warm:ping-1"):
    assert store.record_usage(
        REWARD_KEY, 100, 100, ts=ts, organization_id=ORG, customer_id=customer,
        provider="anthropic", strategy="cache_warm", receipt_source="worker",
        request_id=request_id, actual_cost_usd=cost, baseline_cost_usd=cost,
        measured_savings_usd=0.0)
    assert store.warm_usage_stamp_prefix(
        ORG, REWARD_KEY, request_id, prefix_hash)["updated"] == 1


def log_arrival_row(store, *, ts, discount, attributable=True,
                    prefix_hash=PREFIX_HASH, customer=CUSTOMER,
                    request_id="req-arrival-1"):
    assert store.record_usage(
        REWARD_KEY, 100, 100, ts=ts, organization_id=ORG, customer_id=customer,
        provider="anthropic", strategy="native_cache", receipt_source="proxy",
        authoritative=True, request_id=request_id,
        native_cache_discount_usd=discount, cache_attributable=attributable,
        actual_cost_usd=0.02)
    assert store.warm_usage_stamp_prefix(
        ORG, REWARD_KEY, request_id, prefix_hash)["updated"] == 1


def decision_by_id(store, decision_id):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM warm_decision_log WHERE id=?",
                               (decision_id,)).fetchone())


def test_reward_join_credits_an_attributed_hit(tmp_path):
    store = reward_store(tmp_path)
    log_prefix(store)
    decision_id = log_decision(store, ts=iso(-3600))
    log_ping_row(store, ts=iso(-3595), cost=0.30)
    # One lone earlier arrival, so the organic test has nothing to pair.
    log_arrival_row(store, ts=iso(-4000), discount=0.0,
                    request_id="req-arrival-old")
    log_arrival_row(store, ts=iso(-3500), discount=0.90)

    result = store.warm_reward_join()
    assert (result["scanned"], result["joined"], result["attributed"],
            result["organic"], result["unpriced"]) == (1, 1, 1, 0, 0)
    row = decision_by_id(store, decision_id)
    assert row["realized_net_usd"] == pytest.approx(0.60)
    assert row["organic_counterfactual"] == 0
    # Idempotent: a second pass finds nothing left with a null reward.
    assert store.warm_reward_join()["scanned"] == 0


def test_reward_join_charges_a_self_refreshing_session_in_full(tmp_path):
    """Two real arrivals closer together than the TTL means the customer was
    keeping the entry alive themselves; the discount is theirs, not ours."""
    store = reward_store(tmp_path)
    log_prefix(store, ttl=300)
    decision_id = log_decision(store, ts=iso(-3600))
    # 120s apart, inside the 300s TTL, both before the ping.
    log_arrival_row(store, ts=iso(-3800), discount=0.5,
                    request_id="req-arrival-a")
    log_arrival_row(store, ts=iso(-3680), discount=0.5,
                    request_id="req-arrival-b")
    log_ping_row(store, ts=iso(-3595), cost=0.30)
    # A rich discount right after the ping, which must NOT be credited.
    log_arrival_row(store, ts=iso(-3500), discount=0.90,
                    request_id="req-arrival-c")

    result = store.warm_reward_join()
    assert (result["joined"], result["organic"], result["attributed"]) == (1, 1, 0)
    row = decision_by_id(store, decision_id)
    assert row["realized_net_usd"] == pytest.approx(-0.30)
    assert row["organic_counterfactual"] == 1


def test_reward_join_charges_a_customer_who_never_returned(tmp_path):
    store = reward_store(tmp_path)
    log_prefix(store)
    decision_id = log_decision(store, ts=iso(-3600))
    log_ping_row(store, ts=iso(-3595), cost=0.30)

    result = store.warm_reward_join()
    assert (result["joined"], result["attributed"], result["organic"]) == (1, 0, 0)
    row = decision_by_id(store, decision_id)
    assert row["realized_net_usd"] == pytest.approx(-0.30)
    assert row["organic_counterfactual"] == 0


def test_reward_join_ignores_an_arrival_past_the_ttl(tmp_path):
    store = reward_store(tmp_path)
    log_prefix(store, ttl=300)
    decision_id = log_decision(store, ts=iso(-3600))
    log_ping_row(store, ts=iso(-3595), cost=0.30)
    log_arrival_row(store, ts=iso(-3200), discount=0.90)   # 395s later

    store.warm_reward_join()
    assert decision_by_id(store, decision_id)["realized_net_usd"] == pytest.approx(
        -0.30)


def test_reward_join_ignores_an_unattributable_discount(tmp_path):
    """cache_attributable is the billing gate for a provider-native discount;
    a discount that fails it cannot have been caused by a keep-alive."""
    store = reward_store(tmp_path)
    log_prefix(store)
    decision_id = log_decision(store, ts=iso(-3600))
    log_ping_row(store, ts=iso(-3595), cost=0.30)
    log_arrival_row(store, ts=iso(-3500), discount=0.90, attributable=False)

    store.warm_reward_join()
    assert decision_by_id(store, decision_id)["realized_net_usd"] == pytest.approx(
        -0.30)


def test_reward_join_waits_for_the_attribution_horizon(tmp_path):
    """A ping whose TTL window is still open has an arrival that has not
    happened yet; scoring it now would freeze a censored observation."""
    store = reward_store(tmp_path)
    log_prefix(store, ttl=300)
    decision_id = log_decision(store, ts=iso(-60))
    log_ping_row(store, ts=iso(-55), cost=0.30)

    assert store.warm_reward_join()["scanned"] == 0
    assert decision_by_id(store, decision_id)["realized_net_usd"] is None


def test_reward_join_leaves_outcomes_that_bought_no_receipt_unscored(tmp_path):
    """spent_unknown books the reservation precisely because no priced receipt
    exists (202608080001); a guessed cost would be a fabricated reward."""
    store = reward_store(tmp_path)
    log_prefix(store)
    unknown = log_decision(store, ts=iso(-3600), settle_outcome="spent_unknown")
    released = log_decision(store, ts=iso(-3600), settle_outcome="release",
                            prefix_hash=hashlib.sha256(b"other").hexdigest())
    denied = log_decision(store, ts=iso(-3600), decision="budget_denied",
                          settle_outcome=None)

    assert store.warm_reward_join()["scanned"] == 0
    for decision_id in (unknown, released, denied):
        assert decision_by_id(store, decision_id)["realized_net_usd"] is None


def test_reward_join_does_not_adopt_the_next_cycles_ping(tmp_path):
    """A decision whose own usage row never landed must stay unscored rather
    than borrow the following ping, which is only ttl-minus-margin away."""
    store = reward_store(tmp_path)
    log_prefix(store, ttl=300)
    first = log_decision(store, ts=iso(-3600))
    log_decision(store, ts=iso(-3360))            # next cycle, 240s later
    log_ping_row(store, ts=iso(-3355), cost=0.30, request_id="warm:ping-2")

    result = store.warm_reward_join()
    assert result["unpriced"] == 1
    assert decision_by_id(store, first)["realized_net_usd"] is None


def test_reward_join_never_crosses_a_customer_or_a_prefix(tmp_path):
    store = reward_store(tmp_path)
    other_prefix = hashlib.sha256(b"prefix-2").hexdigest()
    log_prefix(store)
    decision_id = log_decision(store, ts=iso(-3600))
    log_ping_row(store, ts=iso(-3595), cost=0.30)
    # Same window, different customer, and same customer, different prefix.
    log_arrival_row(store, ts=iso(-3500), discount=0.90, customer="customer-2",
                    request_id="req-other-customer")
    log_arrival_row(store, ts=iso(-3500), discount=0.90,
                    prefix_hash=other_prefix, request_id="req-other-prefix")

    store.warm_reward_join()
    assert decision_by_id(store, decision_id)["realized_net_usd"] == pytest.approx(
        -0.30)


def test_usage_stamp_is_write_once_and_tenant_fenced(tmp_path):
    store = reward_store(tmp_path)
    other = hashlib.sha256(b"prefix-2").hexdigest()
    assert store.record_usage(
        REWARD_KEY, 100, 100, organization_id=ORG, customer_id=CUSTOMER,
        provider="anthropic", authoritative=True, request_id="req-1")
    assert store.warm_usage_stamp_prefix(
        ORG, REWARD_KEY, "req-1", PREFIX_HASH)["updated"] == 1
    # Write-once: a retry cannot rewrite the hash.
    assert store.warm_usage_stamp_prefix(
        ORG, REWARD_KEY, "req-1", other)["updated"] == 0
    # Wrong tenant reaches nothing even with the right request id.
    assert store.warm_usage_stamp_prefix(
        "org-other", REWARD_KEY, "req-1", other)["updated"] == 0
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT warm_prefix_hash FROM usage_log WHERE request_id='req-1'"
        ).fetchone()[0] == PREFIX_HASH


def test_usage_stamp_rejects_values_the_column_would_refuse(tmp_path):
    store = reward_store(tmp_path)
    for bad in (
        ("", REWARD_KEY, "req-1", PREFIX_HASH),
        (ORG, "", "req-1", PREFIX_HASH),
        (ORG, REWARD_KEY, "", PREFIX_HASH),
        (ORG, REWARD_KEY, "req-1", ""),
        (ORG, REWARD_KEY, "req-1", "not-a-hash"),
        (ORG, REWARD_KEY, "req-1", PREFIX_HASH.upper()),
    ):
        with pytest.raises(ValueError):
            store.warm_usage_stamp_prefix(*bad)


def test_receipt_insert_never_names_the_reward_join_column(tmp_path):
    """The join key must not ride the receipt INSERT: an unknown column there
    is a 400 that drops the whole billable row, not one field."""
    from api.store import _USAGE_COLUMNS, _usage_row

    assert "warm_prefix_hash" in _USAGE_COLUMNS
    assert "warm_prefix_hash" not in _usage_row(
        "kh", 10, 10, organization_id=ORG, warm_prefix_hash=PREFIX_HASH)


# --- Phase 0 instrumentation: the (org, prefix) control arm (202608100001) ---


def prefix_row(store, org=ORG, customer=CUSTOMER):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute(
            "SELECT * FROM warm_prefixes WHERE organization_id=? AND customer_id=?",
            (org, customer)).fetchone())


def utc_day():
    return datetime.now(timezone.utc).date().isoformat()


def test_holdout_fraction_reads_a_percent_and_fails_to_off(monkeypatch):
    from api.store import warm_holdout_fraction

    monkeypatch.delenv("BREVITAS_WARM_HOLDOUT_PCT", raising=False)
    assert warm_holdout_fraction() == 0.0
    for raw, expected in (("5", 0.05), ("0", 0.0), ("100", 1.0), ("2.5", 0.025)):
        monkeypatch.setenv("BREVITAS_WARM_HOLDOUT_PCT", raw)
        assert warm_holdout_fraction() == pytest.approx(expected)
    # Every unusable value fails to OFF: warming keeps behaving exactly as it
    # did before the arm existed rather than withholding an unknown share.
    for raw in ("", "abc", "-1", "150", "inf", "-inf", "nan", "1e400"):
        monkeypatch.setenv("BREVITAS_WARM_HOLDOUT_PCT", raw)
        assert warm_holdout_fraction() == 0.0


def test_holdout_assignment_is_deterministic_and_customer_independent():
    from api.store import warm_holdout_bucket, warm_is_held_out

    day = utc_day()
    bucket = warm_holdout_bucket(ORG, PREFIX_HASH, day)
    assert 0 <= bucket < 2 ** 32
    # Stable across calls within the day: the whole point is that ticks,
    # replicas and restarts agree without storing an assignment anywhere.
    assert all(warm_holdout_bucket(ORG, PREFIX_HASH, day) == bucket
               for _ in range(5))
    # Case normalization is what keeps the SQLite mirror and Postgres (whose
    # uuid::text is always lower-case) on the same unit.
    assert warm_holdout_bucket(ORG.upper(), PREFIX_HASH.upper(), day) == bucket
    # Re-drawn per day, and per (org, prefix).
    assert warm_holdout_bucket(ORG, PREFIX_HASH, "2026-01-01") != bucket
    assert warm_holdout_bucket("org-warm-2", PREFIX_HASH, day) != bucket
    assert warm_holdout_bucket(ORG, hashlib.sha256(b"other").hexdigest(),
                               day) != bucket
    # The threshold is strict, so a fraction of exactly bucket/2**32 excludes
    # this unit and the next representable step includes it.
    assert warm_is_held_out(ORG, PREFIX_HASH, day, bucket / 2 ** 32) is False
    assert warm_is_held_out(ORG, PREFIX_HASH, day, (bucket + 1) / 2 ** 32) is True
    # 0 is off, and off is checked before anything is hashed.
    assert warm_is_held_out(ORG, PREFIX_HASH, day, 0.0) is False


def test_holdout_at_full_share_claims_nothing_and_logs_the_decision(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    before = prefix_row(store)

    result = claim(store, holdout_fraction=1.0)
    assert result["status"] == "ok"
    assert result["rows"] == []

    entry, = decision_rows(store)
    assert entry["decision"] == "holdout"
    # The fraction in force is stamped on the row, because the environment that
    # produced it is not recoverable later.
    assert entry["propensity"] == pytest.approx(1.0)
    # The belief snapshot is the same one a claim would have recorded, including
    # what the withheld ping would have cost.
    assert entry["reserve_usd"] == pytest.approx(0.375)
    assert entry["p_return"] >= entry["roi_floor"]
    assert entry["pings_today"] == 0
    # Nothing was claimed, so there is no token and no settle can key on it.
    assert entry["claim_token"] is None
    assert entry["settle_outcome"] is None
    assert entry["rng_seed"] is None

    after = prefix_row(store)
    # Only the schedule moved, by the horizon a ping would have set (300-60).
    gap = (datetime.fromisoformat(after["next_due_at"])
           - datetime.now(timezone.utc)).total_seconds()
    assert 200 <= gap <= 240
    # Counters stay put: a day with no ping is no evidence for the stop-loss.
    assert after["warm_pings"] == before["warm_pings"] == 0
    assert after["consecutive_misses"] == before["consecutive_misses"] == 0
    assert after["pings_today"] == before["pings_today"] == 0
    assert after["claim_token"] == before["claim_token"] == ""
    assert after["state"] == "active"
    # No money moved. The ledger row exists because the budget check needs it,
    # but nothing was reserved against it and nothing was spent.
    assert ledger_row(store) == (0.0, 0.0)


def test_holdout_off_is_identical_to_no_arm_at_all(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)

    explicit = claim(store, holdout_fraction=0.0)["rows"]
    assert len(explicit) == 1
    entry, = decision_rows(store)
    assert entry["decision"] == "pinged"
    # No randomization happened, so there is no action probability to record:
    # 1.0 would read as a propensity of one.
    assert entry["propensity"] is None
    assert not [row for row in decision_rows(store)
                if row["decision"] == "holdout"]
    assert ledger_row(store)[0] == pytest.approx(0.375)

    # Omitting the argument entirely is the same thing (the default).
    store.warm_ping_settle(ORG, CUSTOMER, "anthropic", PREFIX_HASH,
                           explicit[0]["budget_day"], explicit[0]["reserved_usd"],
                           0.30, "warmed", 300, 60,
                           claim_token=explicit[0]["claim_token"])
    backdate_due(store)
    assert len(claim(store)["rows"]) == 1
    assert [row["decision"] for row in decision_rows(store)] == ["pinged", "pinged"]
    assert all(row["propensity"] is None for row in decision_rows(store))


def test_holdout_records_the_treatment_arms_propensity_too(tmp_path):
    from api.store import warm_holdout_bucket

    store = make_store(tmp_path)
    enable_warming(store)
    observe(store)
    backdate_due(store)
    # A share just below this unit's coordinate leaves it in the treatment arm
    # while the arm itself is switched on.
    fraction = warm_holdout_bucket(ORG, PREFIX_HASH, utc_day()) / 2 ** 32
    assert len(claim(store, holdout_fraction=fraction)["rows"]) == 1

    entry, = decision_rows(store)
    assert entry["decision"] == "pinged"
    # Both arms carry their own action probability, so an IPS estimator never
    # has to infer one from the other.
    assert entry["propensity"] == pytest.approx(1.0 - fraction)


def test_holdout_is_shared_by_every_customer_on_one_prefix(tmp_path):
    """The SUTVA reason the unit is (org, prefix): provider caches are keyed by
    the org credential, so holding a prefix out for one customer while a sibling
    keeps it warm measures nothing."""
    from api.store import warm_holdout_bucket

    store = make_store(tmp_path)
    enable_warming(store)
    observe(store, customer="customer-a")
    observe(store, customer="customer-b")
    backdate_due(store)
    day = utc_day()
    # Two distinct prefix rows, one shared (org, prefix) unit.
    assert (warm_holdout_bucket(ORG, PREFIX_HASH, day)
            == warm_holdout_bucket(ORG, PREFIX_HASH, day))
    fraction = (warm_holdout_bucket(ORG, PREFIX_HASH, day) + 1) / 2 ** 32

    assert claim(store, holdout_fraction=fraction)["rows"] == []
    logged = decision_rows(store)
    assert [row["decision"] for row in logged] == ["holdout", "holdout"]
    assert {row["customer_id"] for row in logged} == {"customer-a", "customer-b"}


def test_holdout_never_reaches_a_row_the_gates_already_denied(tmp_path):
    """The draw is last, after ROI, cap and budget: a row that was not going to
    be pinged must stay in its own denial class, or the control arm fills up
    with rows the treatment arm would never have warmed."""
    store = make_store(tmp_path)
    enable_warming(store, budget=0.01)
    observe(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE warm_prefixes SET arrival_count=10,hour_histogram='{}'")
    backdate_due(store)
    assert claim(store, holdout_fraction=1.0)["rows"] == []
    assert [row["decision"] for row in decision_rows(store)] == ["skipped_roi"]

    # Same row, now clearing ROI but over the daily budget.
    with sqlite3.connect(store.db_path) as db:
        db.execute("DELETE FROM warm_decision_log")
        db.execute("UPDATE warm_prefixes SET arrival_count=1,"
                   "hour_histogram=hour_histogram")
    observe(store)
    backdate_due(store)
    assert claim(store, holdout_fraction=1.0)["rows"] == []
    assert [row["decision"] for row in decision_rows(store)] == ["budget_denied"]


def test_holdout_fraction_is_bounds_checked(tmp_path):
    store = make_store(tmp_path)
    enable_warming(store)
    for bad in (-0.01, 1.01, 100.0):
        with pytest.raises(ValueError):
            claim(store, holdout_fraction=bad)


def test_warm_status_discloses_the_holdout_share(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    enable_warming(store)
    monkeypatch.setenv("BREVITAS_WARM_HOLDOUT_PCT", "5")
    status = store.warm_status(ORG)
    # Org-facing, and not a spend field: an org is entitled to know what share
    # of its prefixes is deliberately not warmed.
    assert status["holdout_fraction"] == pytest.approx(0.05)
    assert not str("holdout_fraction").endswith("_usd")
    monkeypatch.delenv("BREVITAS_WARM_HOLDOUT_PCT")
    assert store.warm_status(ORG)["holdout_fraction"] == 0.0
