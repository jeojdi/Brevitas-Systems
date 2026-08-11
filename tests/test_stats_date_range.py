"""start/end date-range filtering on the per-dimension stats lists.

Before this, api/store.py accepted start/end on get_stats_by_pipeline/_agent/_run
but api/server.py never passed them, so "what did last Tuesday cost?" was
unanswerable from the API. These tests pin the four things most likely to break
silently once it IS answerable:

1. OMITTING both parameters must call the store exactly as it did before — not
   "with empty strings", which would be a different call.
2. The inclusive-day API surface must convert to the HALF-OPEN timestamptz
   window the usage_grouped RPC actually applies. The +1 day on the exclusive
   end is the whole conversion; drop it and every range silently loses its last
   day, which for a single-day query means silently returning nothing.
3. Bad input (inverted, over-wide, malformed) must be rejected, in the error
   shape neighbouring endpoints already use.
4. A store backend that ACCEPTS start/end and then discards them must refuse the
   request rather than answer a dated cost question with all-time numbers.
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import UsageStore

_STATS_PATHS = ("/v1/stats/pipelines", "/v1/stats/agents", "/v1/stats/runs")
_GETTER_FOR_PATH = {
    "/v1/stats/pipelines": "get_stats_by_pipeline",
    "/v1/stats/agents": "get_stats_by_agent",
    "/v1/stats/runs": "get_stats_by_run",
}


def _client(tmp_path, monkeypatch, *, name: str):
    """Owner-role workspace with one usage row, mirroring the fixture the other
    stats-surface suites use. Local rather than imported so this file does not
    couple to another suite's helper."""
    import api.server as server
    from api.company_admin import company_admin_for_store

    store = UsageStore(str(tmp_path / f"{name}.db"))
    user_id = f"human-{name}"
    organization = store.ensure_organization(user_id, "Company")
    company_admin_for_store(store)
    raw_key = f"bvt_{name}_session_key"
    store.create_key(
        hash_key(raw_key), "dashboard session", owner_id=user_id,
        organization_id=organization["id"], key_type="dashboard_session",
        scopes=["proxy:invoke", "usage:read_own", "provider:read"],
        environment="dashboard", created_by=user_id,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        request_id=f"request-{name}", actor_role="company_owner",
    )
    store.record_usage(
        hash_key(raw_key), 1000, 400, owner_id=user_id,
        organization_id=organization["id"], pipeline="launch",
        agent="writer", run_id="run-1", provider="openai",
        model="gpt-4o-mini", verified_savings_usd=0.5, brevitas_fee_usd=0.125)
    monkeypatch.setattr(server, "_store", store)
    server._auth_context_cache.clear()
    server._valid_key_cache.clear()
    return server, store, raw_key, TestClient(server.app)


def _headers(raw_key: str) -> dict:
    return {"X-Brevitas-Key": raw_key}


def _capture(store, monkeypatch, getter: str) -> list:
    """Replace one stats getter with a spy that ACCEPTS start/end, and declare
    the store date-range capable so the handler threads the window through."""
    seen: list[dict] = []

    def _spy(key_hash, pipeline="", start="", end="", **extra):
        seen.append({"pipeline": pipeline, "start": start, "end": end, **extra})
        return []

    monkeypatch.setattr(store, "supports_stats_date_range", True, raising=False)
    monkeypatch.setattr(store, getter, _spy, raising=False)
    return seen


# ── 1. backward compatibility ────────────────────────────────────────────────

@pytest.mark.parametrize("path", _STATS_PATHS)
def test_omitting_both_parameters_calls_the_store_with_no_date_kwargs(
        tmp_path, monkeypatch, path):
    """The property most likely to break silently.

    The spy deliberately has the PRE-CHANGE signature: no start, no end. If a
    refactor ever passes start=""/end="" unconditionally, this raises TypeError
    instead of quietly changing the store call — which matters because
    SupabaseUsageStore.get_stats_by_run genuinely has no **_ignored to absorb it.
    """
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="compat")
    seen: list[dict] = []

    def _old_signature(key_hash, pipeline=""):
        seen.append({"key_hash": key_hash, "pipeline": pipeline})
        return []

    monkeypatch.setattr(store, _GETTER_FOR_PATH[path], _old_signature)

    response = client.get(path, headers=_headers(raw_key))

    assert response.status_code == 200, response.text
    assert len(seen) == 1


@pytest.mark.parametrize("path", _STATS_PATHS)
def test_omitted_range_returns_the_same_payload_as_before(tmp_path, monkeypatch, path):
    """No range asked for => the untouched all-time aggregate, envelope included."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="unchanged")

    response = client.get(path, headers=_headers(raw_key))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["spend_redacted"] is False
    assert body["rows"] and body["rows"][0]["calls"] == 1
    assert body["rows"][0]["tokens_saved"] == 600


def test_omitted_range_works_on_a_store_that_cannot_filter(tmp_path, monkeypatch):
    """The SQLite store discards start/end. That must not stop the UNDATED call:
    backward compatibility outranks the new feature on every backend."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="nofilter")
    assert not server._store_honors_stats_date_range()

    for path in _STATS_PATHS:
        assert client.get(path, headers=_headers(raw_key)).status_code == 200, path


# ── 2. inclusive days -> half-open UTC window ────────────────────────────────

@pytest.mark.parametrize("path", _STATS_PATHS)
def test_range_is_inclusive_on_both_ends(tmp_path, monkeypatch, path):
    """start=2026-08-04&end=2026-08-06 must cover all three days: the exclusive
    bound handed to the store is 08-07T00:00Z, not 08-06."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="window")
    seen = _capture(store, monkeypatch, _GETTER_FOR_PATH[path])

    response = client.get(f"{path}?start=2026-08-04&end=2026-08-06",
                          headers=_headers(raw_key))

    assert response.status_code == 200, response.text
    assert seen[0]["start"] == "2026-08-04T00:00:00+00:00"
    assert seen[0]["end"] == "2026-08-07T00:00:00+00:00"


@pytest.mark.parametrize("path", _STATS_PATHS)
def test_single_day_range_covers_that_whole_utc_day(tmp_path, monkeypatch, path):
    """"What did last Tuesday cost?" — start == end must be a 24h window, not an
    empty one. A raw pass-through to `ts >= p_start AND ts < p_end` would make
    this span zero seconds and report a confident $0."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="oneday")
    seen = _capture(store, monkeypatch, _GETTER_FOR_PATH[path])

    response = client.get(f"{path}?start=2026-08-04&end=2026-08-04",
                          headers=_headers(raw_key))

    assert response.status_code == 200, response.text
    start = datetime.fromisoformat(seen[0]["start"])
    end = datetime.fromisoformat(seen[0]["end"])
    assert (end - start) == timedelta(days=1)
    assert start == datetime(2026, 8, 4, tzinfo=timezone.utc)


def test_window_is_utc_not_local(tmp_path, monkeypatch):
    """usage_log.ts is timestamptz and the warming holdout keys on the UTC day,
    so the boundary must carry an explicit +00:00 offset."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="utc")
    seen = _capture(store, monkeypatch, "get_stats_by_agent")

    client.get("/v1/stats/agents?start=2026-01-01&end=2026-01-01",
               headers=_headers(raw_key))

    for bound in ("start", "end"):
        assert datetime.fromisoformat(seen[0][bound]).utcoffset() == timedelta(0)


def test_only_end_supplied_defaults_to_a_thirty_day_window(tmp_path, monkeypatch):
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="endonly")
    seen = _capture(store, monkeypatch, "get_stats_by_agent")

    response = client.get("/v1/stats/agents?end=2026-08-31", headers=_headers(raw_key))

    assert response.status_code == 200, response.text
    assert seen[0]["start"] == "2026-08-01T00:00:00+00:00"
    assert seen[0]["end"] == "2026-09-01T00:00:00+00:00"


def test_only_start_supplied_runs_through_today(tmp_path, monkeypatch):
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="startonly")
    seen = _capture(store, monkeypatch, "get_stats_by_agent")
    today = datetime.now(timezone.utc).date()

    response = client.get(f"/v1/stats/agents?start={today.isoformat()}",
                          headers=_headers(raw_key))

    assert response.status_code == 200, response.text
    assert seen[0]["end"] == datetime(
        *(today + timedelta(days=1)).timetuple()[:3], tzinfo=timezone.utc).isoformat()


# /v1/stats/pipelines groups BY pipeline and so has no pipeline filter of its
# own — only the other two take one alongside a range.
@pytest.mark.parametrize("path", ("/v1/stats/agents", "/v1/stats/runs"))
def test_pipeline_filter_still_threads_through_alongside_a_range(
        tmp_path, monkeypatch, path):
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="withpipe")
    seen = _capture(store, monkeypatch, _GETTER_FOR_PATH[path])

    client.get(f"{path}?pipeline=launch&start=2026-08-04&end=2026-08-04",
               headers=_headers(raw_key))

    assert seen[0]["pipeline"] == "launch"


def test_conversion_matches_the_migration_that_defines_the_predicate():
    """Guards the +1 day from the other side.

    _stats_range_window adds a day to the exclusive bound BECAUSE usage_grouped
    filters `usage.ts < p_end`. If that predicate is ever relaxed to `<=`, the
    conversion becomes an off-by-one-day over-count and this fires.
    """
    sql = Path(__file__).resolve().parents[1].joinpath(
        "supabase/migrations/202607280035_usage_read_tenant_scope.sql").read_text()
    assert "p_start is null or usage.ts >= p_start" in sql
    assert "p_end is null or usage.ts < p_end" in sql
    assert "usage.ts <= p_end" not in sql


# ── 3. input validation ──────────────────────────────────────────────────────

@pytest.mark.parametrize("path", _STATS_PATHS)
def test_inverted_range_is_rejected(tmp_path, monkeypatch, path):
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="inverted")
    _capture(store, monkeypatch, _GETTER_FOR_PATH[path])

    response = client.get(f"{path}?start=2026-08-06&end=2026-08-04",
                          headers=_headers(raw_key))

    assert response.status_code == 400
    assert response.json()["detail"] == "start must not exceed end"


@pytest.mark.parametrize("path", _STATS_PATHS)
def test_over_wide_range_is_rejected(tmp_path, monkeypatch, path):
    """An unbounded scan is not orderable from the outside."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="toowide")
    _capture(store, monkeypatch, _GETTER_FOR_PATH[path])
    start = date(2020, 1, 1)
    end = start + timedelta(days=server._STATS_RANGE_MAX_DAYS)  # spans MAX + 1

    response = client.get(f"{path}?start={start.isoformat()}&end={end.isoformat()}",
                          headers=_headers(raw_key))

    assert response.status_code == 400
    assert "366" in response.json()["detail"]


def test_the_widest_allowed_range_is_accepted(tmp_path, monkeypatch):
    """Pins the boundary from the legal side, so the bound cannot drift by a day."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="widest")
    seen = _capture(store, monkeypatch, "get_stats_by_agent")
    start = date(2020, 1, 1)
    end = start + timedelta(days=server._STATS_RANGE_MAX_DAYS - 1)  # spans exactly MAX

    response = client.get(
        f"/v1/stats/agents?start={start.isoformat()}&end={end.isoformat()}",
        headers=_headers(raw_key))

    assert response.status_code == 200, response.text
    assert seen[0]["start"] == "2020-01-01T00:00:00+00:00"


@pytest.mark.parametrize("bad", ["04-08-2026", "yesterday", "2026-13-01", "2026-02-30",
                                 "2026-08-04T00:00:00Z", "2026-08-04 ", "26-08-04"])
@pytest.mark.parametrize("field", ["start", "end"])
def test_malformed_dates_are_rejected(tmp_path, monkeypatch, field, bad):
    """YYYY-MM-DD, naming the offending parameter so the caller knows which one."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="malformed")
    _capture(store, monkeypatch, "get_stats_by_agent")
    other = "end" if field == "start" else "start"

    response = client.get(f"/v1/stats/agents?{field}={bad}&{other}=2026-08-04",
                          headers=_headers(raw_key))

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == f"{field} must be YYYY-MM-DD"


@pytest.mark.parametrize("field", ["start", "end"])
def test_an_empty_parameter_means_not_supplied(tmp_path, monkeypatch, field):
    """?start= is how an HTML form sends "blank", and must not 400."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="blank")
    seen = _capture(store, monkeypatch, "get_stats_by_agent")
    other = "end" if field == "start" else "start"

    response = client.get(f"/v1/stats/agents?{field}=&{other}=2026-08-04",
                          headers=_headers(raw_key))

    assert response.status_code == 200, response.text
    if field == "start":
        # end=2026-08-04 supplied; start falls back to the 30-day default window.
        assert seen[0]["start"] == "2026-07-05T00:00:00+00:00"
        assert seen[0]["end"] == "2026-08-05T00:00:00+00:00"
    else:
        # start=2026-08-04 supplied; end falls back to today, exclusive tomorrow.
        assert seen[0]["start"] == "2026-08-04T00:00:00+00:00"
        tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
        assert seen[0]["end"] == datetime(
            tomorrow.year, tomorrow.month, tomorrow.day,
            tzinfo=timezone.utc).isoformat()


def test_both_parameters_empty_is_treated_as_no_range_at_all(tmp_path, monkeypatch):
    """?start=&end= must stay on the backward-compatible undated path — it must
    NOT become a 501 on a store that cannot filter, because nothing was asked."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="bothblank")
    assert not server._store_honors_stats_date_range()

    response = client.get("/v1/stats/agents?start=&end=", headers=_headers(raw_key))

    assert response.status_code == 200, response.text
    assert response.json()["rows"][0]["calls"] == 1


@pytest.mark.parametrize("value", ["2026-8-4", "2026-08-4"])
def test_non_zero_padded_dates_are_accepted_and_map_to_the_same_day(
        tmp_path, monkeypatch, value):
    """Documented, not accidental: %Y-%m-%d makes zero-padding optional, and every
    form it accepts resolves to the SAME day (trailing junk and 2-digit years are
    rejected above). Leniency that cannot land on the wrong day is safe, and
    matches _warm_attribution_day, which parses the identical way."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="padding")
    seen = _capture(store, monkeypatch, "get_stats_by_agent")

    response = client.get(f"/v1/stats/agents?start={value}&end={value}",
                          headers=_headers(raw_key))

    assert response.status_code == 200, response.text
    assert seen[0]["start"] == "2026-08-04T00:00:00+00:00"
    assert seen[0]["end"] == "2026-08-05T00:00:00+00:00"


# ── 4. a store that cannot filter must not answer with all-time numbers ──────

@pytest.mark.parametrize("path", _STATS_PATHS)
def test_store_that_discards_dates_refuses_instead_of_lying(tmp_path, monkeypatch, path):
    """api/store.py UsageStore.get_stats_by_* accept start/end and then call
    _legacy_group(key_hash, field, pipeline), dropping them. Answering a dated
    request from that store would label the lifetime total as one Tuesday."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="refuse")
    assert not server._store_honors_stats_date_range()

    response = client.get(f"{path}?start=2026-08-04&end=2026-08-04",
                          headers=_headers(raw_key))

    assert response.status_code == 501, response.text
    assert response.json()["detail"] == (
        "Date-range stats are not supported by this store backend")


def test_bad_input_is_rejected_before_the_backend_capability_check(tmp_path, monkeypatch):
    """A malformed request is a client error on every backend, so the 400 must
    not depend on which store happens to be mounted."""
    server, store, raw_key, client = _client(tmp_path, monkeypatch, name="order")
    assert not server._store_honors_stats_date_range()

    response = client.get("/v1/stats/agents?start=2026-08-06&end=2026-08-04",
                          headers=_headers(raw_key))

    assert response.status_code == 400
    assert response.json()["detail"] == "start must not exceed end"


def test_capability_probe_reflects_the_two_real_store_backends(tmp_path, monkeypatch):
    """The probe must distinguish the store that forwards p_start/p_end to the
    usage_grouped RPC from the one that drops them — hasattr cannot, since both
    expose the same getter signature."""
    import api.server as server
    from api.store import SupabaseUsageStore

    store = UsageStore(str(tmp_path / "probe.db"))
    monkeypatch.setattr(server, "_store", store)
    assert server._store_honors_stats_date_range() is False

    # Supabase is identified by the same `_request` discriminator the module
    # already uses to pick the job store.
    assert hasattr(SupabaseUsageStore, "_request")
    monkeypatch.setattr(store, "supports_stats_date_range", True, raising=False)
    assert server._store_honors_stats_date_range() is True
