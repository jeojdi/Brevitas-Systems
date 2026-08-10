"""Every receipt a valid key produces must name a tenant (FUNNEL-FIX 5).

Roughly 57% of recent client-reported usage_log rows in production carry
organization_id NULL, which strips org linkage off exactly the dogfood and
local-bvx traffic the dashboard is supposed to show. The cause is NOT anonymous
intake -- see test_anonymous_usage_reports_are_rejected below, which pins the
401 that was already there -- it is keys minted before the organization model
existed: key_type='legacy' rows carry an owner_id and no organization_id, so
AuthContext.organization_id is "" and the receipt lands untenanted.

The fix resolves such a key through its owner's own active workspace, at the one
chokepoint both write paths already share (_fill_organization_id).
"""
import sqlite3

from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import UsageStore


def _client(tmp_path, monkeypatch, name):
    import api.server as server

    store = UsageStore(str(tmp_path / f"{name}.db"))
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    server._seq_streams.clear()
    return server, store, TestClient(server.app)


_REPORT = {"provider": "openai", "model": "gpt-4o-mini", "baseline_tokens": 100,
           "compressed_tokens": 80, "strategy": "native_cache"}


def test_legacy_key_report_is_attributed_to_its_owner_workspace(
        tmp_path, monkeypatch):
    server, store, client = _client(tmp_path, monkeypatch, "legacy-attribution")
    organization = store.ensure_organization("dogfood-owner", "Dogfood")
    raw_key = "bvt_legacy_dogfood"
    # A key of the shape that predates organizations: an owner, no tenant.
    store.create_key(hash_key(raw_key), "legacy dogfood", owner_id="dogfood-owner")
    with store._conn() as db:
        assert db.execute(
            "SELECT organization_id FROM api_keys WHERE key_hash=?",
            (hash_key(raw_key),)).fetchone()[0] == ""

    response = client.post("/v1/usage", headers={"X-Brevitas-Key": raw_key},
                           json={**_REPORT, "request_id": "legacy-report-0001"})

    assert response.status_code == 200
    with store._conn() as db:
        stored = db.execute(
            "SELECT organization_id,owner_id FROM usage_log WHERE request_id=?",
            ("legacy-report-0001",)).fetchone()
    assert stored[0] == organization["id"]
    assert stored[1] == "dogfood-owner"


def test_attribution_survives_the_batch_write_path_too(tmp_path, monkeypatch):
    """record_usage and record_usage_batch resolve through the same lookup."""
    _server, store, _client_ = _client(tmp_path, monkeypatch, "legacy-batch")
    organization = store.ensure_organization("batch-owner", "Batch")
    key_hash = hash_key("bvt_legacy_batch")
    store.create_key(key_hash, "legacy batch", owner_id="batch-owner")

    store.record_usage_batch([
        {"key_hash": key_hash, "owner_id": "batch-owner",
         "baseline_tokens": 10, "optimized_tokens": 8,
         "request_id": f"legacy-batch-{index}"}
        for index in range(2)
    ])

    with store._conn() as db:
        rows = db.execute(
            "SELECT DISTINCT organization_id FROM usage_log "
            "WHERE request_id LIKE 'legacy-batch-%'").fetchall()
    assert [row[0] for row in rows] == [organization["id"]]


def test_a_key_whose_owner_has_no_workspace_is_still_recorded(
        tmp_path, monkeypatch):
    """No tenant to name is not a reason to lose the receipt.

    The alternative -- rejecting the report -- would drop a receipt from a key
    the API itself just authenticated, which is the failure mode every guard on
    this path exists to prevent. It stays untenanted and loud by absence.
    """
    server, store, client = _client(tmp_path, monkeypatch, "orphan-key")
    raw_key = "bvt_legacy_orphan"
    store.create_key(hash_key(raw_key), "orphan", owner_id="owner-with-no-workspace")

    response = client.post("/v1/usage", headers={"X-Brevitas-Key": raw_key},
                           json={**_REPORT, "request_id": "orphan-report-0001"})

    assert response.status_code == 200
    with store._conn() as db:
        assert db.execute(
            "SELECT organization_id FROM usage_log WHERE request_id=?",
            ("orphan-report-0001",)).fetchone()[0] == ""


def test_a_removed_membership_does_not_attribute(tmp_path, monkeypatch):
    """Only an ACTIVE membership names a tenant, same rule as member_organization."""
    from api.company_admin import company_admin_for_store

    _server, store, _client_ = _client(tmp_path, monkeypatch, "removed-member")
    # The base development schema has no membership status column; the company
    # admin composition adds it, and only then can a membership be "removed".
    company_admin_for_store(store)
    organization = store.ensure_organization("departed-owner", "Departed")
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE organization_members SET status='removed' "
                   "WHERE organization_id=? AND user_id=?",
                   (organization["id"], "departed-owner"))
    key_hash = hash_key("bvt_legacy_removed")
    store.create_key(key_hash, "removed", owner_id="departed-owner")

    assert store.key_organization(key_hash) == ""


def test_anonymous_usage_reports_are_rejected(tmp_path, monkeypatch):
    """The audit's suspected cause, pinned as already-false.

    POST /v1/usage depends on _authenticated, so a report with no key and a
    report with an unknown key are both refused before any row can be written.
    Orgless rows were never anonymous intake; they were valid keys with no tenant.
    """
    _server, store, client = _client(tmp_path, monkeypatch, "anonymous-usage")

    missing = client.post("/v1/usage", json={**_REPORT, "request_id": "anon-0001"})
    unknown = client.post("/v1/usage", headers={"X-Brevitas-Key": "bvt_not_a_key"},
                          json={**_REPORT, "request_id": "anon-0002"})

    assert missing.status_code == 401
    assert unknown.status_code == 401
    with store._conn() as db:
        assert db.execute("SELECT count(*) FROM usage_log").fetchone()[0] == 0


def test_an_organization_bound_key_is_never_re_resolved(tmp_path, monkeypatch):
    """The owner fallback is a fallback: a key with a tenant keeps that tenant."""
    _server, store, _client_ = _client(tmp_path, monkeypatch, "bound-key")
    first = store.ensure_organization("multi-owner", "First")
    key_hash = hash_key("bvt_bound")
    store.create_key(key_hash, "bound", owner_id="multi-owner",
                     organization_id=first["id"], key_type="device",
                     scopes=["proxy:invoke", "usage:write"])
    # A second workspace the owner also belongs to must not be able to capture
    # the receipt away from the key's own explicit binding.
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO organizations(id,name,legacy_owner_id,billing_owner_id,"
            "account_type,created_at,onboarding_started_at) "
            "VALUES('00000000-0000-4000-8000-0000000000ff','Second','second-legacy',"
            "'multi-owner','company','2020-01-01T00:00:00+00:00',"
            "'2020-01-01T00:00:00+00:00')")
        db.execute(
            "INSERT INTO organization_members(organization_id,user_id,role,created_at)"
            " VALUES('00000000-0000-4000-8000-0000000000ff','multi-owner','owner',"
            "'2020-01-01T00:00:00+00:00')")

    assert store.key_organization(key_hash) == first["id"]
