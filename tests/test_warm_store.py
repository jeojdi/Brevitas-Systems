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
