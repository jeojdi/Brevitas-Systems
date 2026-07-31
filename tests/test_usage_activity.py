"""Client activity sessionization for the dashboard (/v1/stats/activity)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.store import ACTIVITY_SCAN_MAX, USAGE_PAGE_MAX, SupabaseUsageStore, UsageStore, _activity

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _row(minutes_ago: int, client: str = "cli") -> dict:
    return {"ts": (NOW - timedelta(minutes=minutes_ago)).isoformat(), "client": client}


def test_activity_splits_sessions_on_idle_gap_and_flags_active_clients():
    rows = [
        # cli: an old session (95-90m ago), then a live one (10m ago .. 2m ago).
        _row(95), _row(90), _row(10), _row(2),
        # sdk: one session that stopped over an hour ago.
        _row(80, client="sdk"), _row(70, client="sdk"),
    ]
    report = _activity(rows, idle_minutes=30, now=NOW)

    assert report["idle_minutes"] == 30
    clients = {c["client"]: c for c in report["clients"]}
    assert clients["cli"]["active"] is True
    assert clients["cli"]["sessions"] == 2
    assert clients["cli"]["total_calls"] == 4
    assert clients["sdk"]["active"] is False
    # Newest activity sorts first.
    assert report["clients"][0]["client"] == "cli"

    sessions = [s for s in report["sessions"] if s["client"] == "cli"]
    assert [s["active"] for s in sessions] == [True, False]
    live, ended = sessions
    assert live["calls"] == 2
    assert live["duration_seconds"] == 8 * 60
    assert ended["started_at"] == (NOW - timedelta(minutes=95)).isoformat()
    assert ended["last_seen_at"] == (NOW - timedelta(minutes=90)).isoformat()


def test_activity_falls_back_to_source_and_skips_unparseable_timestamps():
    rows = [
        {"ts": (NOW - timedelta(minutes=1)).isoformat(), "client": "", "source": "cli"},
        {"ts": "not-a-timestamp", "client": "cli"},
    ]
    report = _activity(rows, idle_minutes=30, now=NOW)
    assert [c["client"] for c in report["clients"]] == ["cli"]
    assert report["clients"][0]["total_calls"] == 1


def test_supabase_activity_pages_through_history_with_cursor():
    store = SupabaseUsageStore("https://example.supabase.co", "service-role-test")
    cursors = []

    def request(method, path, **kwargs):
        if path == "api_keys":  # _usage_scope key-context lookup
            return [{"key_hash": "kh-history", "owner_id": "owner-1"}]
        assert (method, path) == ("POST", "rpc/usage_page")
        data = kwargs["data"]
        cursors.append((data["p_cursor_ts"], data["p_cursor_id"]))
        base = len(cursors) * 10_000
        # USAGE_PAGE_MAX + 1 rows signals another page; the short second page ends the walk.
        count = USAGE_PAGE_MAX + 1 if len(cursors) == 1 else 5
        return [{"id": base + i, "client": "cli",
                 "ts": (NOW - timedelta(minutes=base + i)).isoformat()}
                for i in range(count)]

    store._request = request
    report = store.get_activity("kh-history")

    assert cursors[0] == (None, None)
    assert cursors[1] == ((NOW - timedelta(minutes=10_000 + USAGE_PAGE_MAX - 1)).isoformat(),
                          10_000 + USAGE_PAGE_MAX - 1)
    assert len(cursors) == 2
    assert report["clients"][0]["total_calls"] == USAGE_PAGE_MAX + 5
    assert report["clients"][0]["total_calls"] <= ACTIVITY_SCAN_MAX


def test_sqlite_store_reports_activity_for_recorded_usage(tmp_path):
    store = UsageStore(str(tmp_path / "activity.db"))
    ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert store.record_usage("kh-activity", 100, 60, ts=ts, client="cli", source="cli")

    report = store.get_activity("kh-activity")
    assert [c["client"] for c in report["clients"]] == ["cli"]
    assert report["clients"][0]["active"] is True
    assert report["sessions"][0]["calls"] == 1


def test_sqlite_admin_account_detail_scopes_to_owner(tmp_path):
    store = UsageStore(str(tmp_path / "admin-detail.db"))
    ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert store.record_usage("kh-a", 100, 60, ts=ts, client="cli", owner_id="owner-a")
    assert store.record_usage("kh-b", 500, 300, ts=ts, client="sdk", owner_id="owner-b")

    detail = store.get_admin_account_detail("owner-a")
    assert detail["owner_id"] == "owner-a"
    assert detail["window_calls"] == 1
    assert detail["totals"]["total_baseline_tokens"] == 100
    assert [c["client"] for c in detail["activity"]["clients"]] == ["cli"]


# ── the platform-admin view must survive 202607280035 (202607280037) ──────────
#
# 202607280035 narrowed the ORG-LESS branch of usage_page to
# `usage.organization_id is null`. GET /v1/admin/accounts/{owner_id}/usage was
# the one caller reaching that branch without being a key -- it posted
# p_key_hash="", p_organization_id=None -- so applying 202607280035 would have
# swapped the staff view onto an older, org-less population. On production two
# owners have ALL of their newest ACTIVITY_SCAN_MAX rows org-scoped, so the row
# COUNT would not have moved. Both tests below therefore assert on row
# IDENTITIES, never on counts alone.


def _admin_detail_fixture(store: UsageStore) -> None:
    """One account with a MIX of org-scoped and org-less rows, plus a second
    account inside the same organization. This is the production shape."""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert store.record_usage("kh-org", 100, 60, ts=ts, client="cli",
                              owner_id="owner-a", organization_id="org-1",
                              request_id="org-row")
    assert store.record_usage("kh-free", 200, 120, ts=ts, client="sdk",
                              owner_id="owner-a", organization_id="",
                              request_id="free-row")
    assert store.record_usage("kh-org", 400, 240, ts=ts, client="cli",
                              owner_id="owner-b", organization_id="org-1",
                              request_id="stranger-row")


def test_sqlite_admin_account_detail_still_sees_org_scoped_rows(tmp_path):
    # The local mirror of the same regression: _rows() carries 202607280035's
    # tenant predicate, so the admin path must NOT be built on it.
    store = UsageStore(str(tmp_path / "admin-org.db"))
    _admin_detail_fixture(store)

    detail = store.get_admin_account_detail("owner-a")
    assert sorted(r["request_id"] for r in store._owner_rows("owner-a")) == [
        "free-row", "org-row"]
    assert detail["window_calls"] == 2, (
        "the platform-admin view dropped this account's org-scoped rows")
    assert detail["totals"]["total_baseline_tokens"] == 300
    # ...and it is still ONE account, not the organization.
    assert all(row["owner_id"] == "owner-a" for row in store._owner_rows("owner-a"))


def test_sqlite_admin_account_detail_is_not_bounded_by_platform_wide_traffic(tmp_path):
    # The old implementation was `_all_rows() filtered by owner`, i.e. the
    # newest LOCAL_USAGE_SCAN_MAX rows PLATFORM-WIDE and then this owner's. A
    # quiet account whose traffic is older than that window reported zero calls
    # while the hosted backend reported a full window for it.
    import api.store as store_module

    store = UsageStore(str(tmp_path / "admin-window.db"))
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    new = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert store.record_usage("kh-quiet", 100, 60, ts=old, client="cli",
                              owner_id="owner-quiet", request_id="quiet-row")
    for index in range(5):
        assert store.record_usage("kh-loud", 10, 6, ts=new, client="cli",
                                  owner_id="owner-loud", request_id=f"loud-{index}")

    original = store_module.LOCAL_USAGE_SCAN_MAX
    store_module.LOCAL_USAGE_SCAN_MAX = 3  # the loud account alone overruns it
    try:
        detail = store.get_admin_account_detail("owner-quiet")
    finally:
        store_module.LOCAL_USAGE_SCAN_MAX = original
    assert detail["window_calls"] == 1
    assert detail["totals"]["total_baseline_tokens"] == 100


def test_supabase_admin_account_detail_uses_the_dedicated_admin_rpc():
    store = SupabaseUsageStore("https://example.supabase.co", "service-role-test")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs["data"]))
        return [{"id": 1, "client": "cli", "baseline_tokens": 100, "optimized_tokens": 60,
                 "tokens_saved": 40, "ts": (NOW - timedelta(minutes=1)).isoformat()}]

    store._request = request
    detail = store.get_admin_account_detail("owner-a")

    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "rpc/admin_account_usage_page")], (
        "the platform-admin view is back on usage_page, whose org-less branch "
        "202607280035 narrows")
    payload = calls[0][2]
    assert payload["p_owner_id"] == "owner-a"
    # The narrowed branch is reached by supplying these two; the admin RPC has
    # no such parameters, and sending them would 404 at PostgREST.
    assert "p_key_hash" not in payload and "p_organization_id" not in payload
    assert detail["window_calls"] == 1
    assert detail["totals"]["total_tokens_saved"] == 40
    assert [c["client"] for c in detail["activity"]["clients"]] == ["cli"]


def test_supabase_admin_account_detail_returns_the_pre_0035_row_set():
    """The before/after proof, run against a fake PostgREST that implements both
    predicates over the same table."""
    org_row = {"id": 3, "request_id": "org-row", "organization_id": "org-1",
               "owner_id": "owner-a", "key_hash": "kh-org", "client": "cli",
               "baseline_tokens": 100, "optimized_tokens": 60, "tokens_saved": 40,
               "ts": (NOW - timedelta(minutes=1)).isoformat()}
    self_row = {"id": 2, "request_id": "self-row", "organization_id": "org-1",
                "owner_id": "owner-a", "key_hash": "kh-free", "client": "cli",
                "baseline_tokens": 10, "optimized_tokens": 6, "tokens_saved": 4,
                "ts": (NOW - timedelta(minutes=2)).isoformat()}
    free_row = {"id": 1, "request_id": "free-row", "organization_id": None,
                "owner_id": "owner-a", "key_hash": "kh-free", "client": "sdk",
                "baseline_tokens": 200, "optimized_tokens": 120, "tokens_saved": 80,
                "ts": (NOW - timedelta(minutes=3)).isoformat()}
    stranger = {"id": 4, "request_id": "stranger-row", "organization_id": "org-1",
                "owner_id": "owner-b", "key_hash": "kh-org", "client": "cli",
                "baseline_tokens": 999, "optimized_tokens": 1, "tokens_saved": 998,
                "ts": NOW.isoformat()}
    table = [stranger, org_row, self_row, free_row]

    def fake_postgrest(pre_0035: bool):
        def request(method, path, **kwargs):
            data = kwargs["data"]
            if path == "rpc/usage_page":
                # 202607170006's org-less branch, with 202607280035's extra
                # `usage.organization_id is null` conjunct applied or not.
                owner, key_hash = data["p_owner_id"], data["p_key_hash"]
                assert data["p_organization_id"] is None
                return [r for r in table
                        if (r["owner_id"] == owner
                            and (pre_0035 or r["organization_id"] is None))
                        or r["key_hash"] == key_hash]
            assert path == "rpc/admin_account_usage_page"
            return [r for r in table if r["owner_id"] == data["p_owner_id"]]
        return request

    # What staff saw BEFORE 202607280035, through the route as it was written.
    store = SupabaseUsageStore("https://example.supabase.co", "service-role-test")
    store._request = fake_postgrest(pre_0035=True)
    before = store._collect_usage_rows(
        {"p_key_hash": "", "p_organization_id": None, "p_owner_id": "owner-a"})

    # What staff see AFTER, through the route as it is written now.
    store._request = fake_postgrest(pre_0035=False)
    after = store.get_admin_account_detail("owner-a")
    after_rows = store._collect_usage_rows(
        {"p_owner_id": "owner-a"}, "rpc/admin_account_usage_page")

    assert [r["request_id"] for r in before] == ["org-row", "self-row", "free-row"]
    assert [r["request_id"] for r in after_rows] == [r["request_id"] for r in before]
    assert after["window_calls"] == len(before)
    assert after["totals"]["total_tokens_saved"] == 124

    # Not vacuous: the OLD route code against the NEW predicate loses two rows.
    regressed = store._collect_usage_rows(
        {"p_key_hash": "", "p_organization_id": None, "p_owner_id": "owner-a"})
    assert [r["request_id"] for r in regressed] == ["free-row"]
