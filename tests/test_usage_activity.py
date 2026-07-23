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


def test_supabase_admin_account_detail_uses_owner_scoped_usage_page():
    store = SupabaseUsageStore("https://example.supabase.co", "service-role-test")
    scopes = []

    def request(method, path, **kwargs):
        assert (method, path) == ("POST", "rpc/usage_page")
        data = kwargs["data"]
        scopes.append((data["p_key_hash"], data["p_organization_id"], data["p_owner_id"]))
        return [{"id": 1, "client": "cli", "baseline_tokens": 100, "optimized_tokens": 60,
                 "tokens_saved": 40, "ts": (NOW - timedelta(minutes=1)).isoformat()}]

    store._request = request
    detail = store.get_admin_account_detail("owner-a")

    assert scopes == [("", None, "owner-a")]
    assert detail["window_calls"] == 1
    assert detail["totals"]["total_tokens_saved"] == 40
    assert [c["client"] for c in detail["activity"]["clients"]] == ["cli"]
