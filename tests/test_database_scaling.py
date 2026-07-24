import inspect
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from api.auth import hash_key
from api.import_usage import import_sqlite
from api.store import (AmbiguousUsageBatchError, BoundedUsageWriter,
                       SupabaseUsageStore, USAGE_BATCH_MAX,
                       UsageBatchPartialFailure, UsageStore)


def _receipt(request_id: str, **values):
    return {
        "key_hash": "key-a",
        "baseline_tokens": 100,
        "optimized_tokens": 80,
        "request_id": request_id,
        **values,
    }


def _approved_device_store(tmp_path, *, owner_id="device-owner"):
    store = UsageStore(str(tmp_path / "device-idempotency.db"))
    organization = store.ensure_organization(owner_id)
    device_hash = hash_key("device-code-for-idempotency")
    key_hash = hash_key("bvt_device_delivery_secret")
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    store.create_device_request(device_hash, expires_at)
    assert store.approve_device_request(
        device_hash, owner_id, key_hash, "kms-envelope-ciphertext",
    )
    return store, organization["id"], device_hash, key_hash, expires_at


class _DependencyMetrics:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def record_dependency(self, **values):
        self.calls.append(dict(values))
        if self.fail:
            raise RuntimeError("telemetry unavailable")


def _http_response(status: int = 200, payload: bytes = b'{"ok":true}'):
    response = requests.Response()
    response.status_code = status
    response._content = payload
    response.url = "https://example.supabase.co/rest/v1/test"
    return response


def test_supabase_query_rpc_and_batch_emit_fixed_postgres_dependency_metrics(monkeypatch):
    import brevitas.observability as observability

    metrics = _DependencyMetrics()
    runtime = type("Runtime", (), {"metrics": metrics})()
    monkeypatch.setattr(observability, "get_runtime", lambda **_kwargs: runtime)
    monkeypatch.setattr(requests, "request", lambda *_args, **_kwargs: _http_response())
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")

    assert store._request("GET", "organizations") == {"ok": True}
    assert store._request("POST", "rpc/usage_stats", data={}) == {"ok": True}
    assert store._request("POST", "usage_log", data=[_receipt("metric-batch")]) == {
        "ok": True,
    }
    assert len(metrics.calls) == 3
    for call in metrics.calls:
        assert set(call) == {"dependency", "outcome", "duration_seconds"}
        assert call["dependency"] == "postgres"
        assert call["outcome"] == "success"
        assert isinstance(call["duration_seconds"], float)
        assert call["duration_seconds"] >= 0
        assert not set(call).intersection({
            "path", "method", "sql", "tenant", "cursor", "request_id", "content",
        })


@pytest.mark.parametrize(("failure", "outcome", "exception_type"), [
    (requests.Timeout("timed out"), "timeout", requests.Timeout),
    (requests.ConnectionError("offline"), "unavailable", requests.ConnectionError),
    (_http_response(503), "unavailable", requests.HTTPError),
    (_http_response(400), "error", requests.HTTPError),
])
def test_supabase_dependency_metrics_classify_failures_without_changing_behavior(
        monkeypatch, failure, outcome, exception_type):
    import brevitas.observability as observability

    metrics = _DependencyMetrics()
    runtime = type("Runtime", (), {"metrics": metrics})()
    monkeypatch.setattr(observability, "get_runtime", lambda **_kwargs: runtime)

    def request(*_args, **_kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(requests, "request", request)
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    with pytest.raises(exception_type) as caught:
        store._request("POST", "rpc/device_boundary", data={})
    if isinstance(failure, BaseException):
        assert caught.value is failure
    assert len(metrics.calls) == 1
    assert metrics.calls[0]["dependency"] == "postgres"
    assert metrics.calls[0]["outcome"] == outcome


def test_supabase_dependency_telemetry_is_fail_open(monkeypatch):
    import brevitas.observability as observability

    monkeypatch.setattr(requests, "request", lambda *_args, **_kwargs: _http_response())
    monkeypatch.setattr(
        observability, "get_runtime",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("metrics unavailable")),
    )
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    assert store._request("GET", "organizations") == {"ok": True}

    failing_metrics = _DependencyMetrics(fail=True)
    runtime = type("Runtime", (), {"metrics": failing_metrics})()
    monkeypatch.setattr(observability, "get_runtime", lambda **_kwargs: runtime)
    assert store._request("GET", "organizations") == {"ok": True}
    assert len(failing_metrics.calls) == 1


def test_sqlite_dashboard_uses_monday_utc_week_buckets(tmp_path):
    store = UsageStore(str(tmp_path / "weekly-stats.db"))
    store.record_usage(
        "weekly", 100, 80, ts="2026-07-19T16:59:59-07:00",
        actual_cost_usd=1, verified_savings_usd=.5,
    )
    store.record_usage(
        "weekly", 100, 70, ts="2026-07-20T00:00:00+00:00",
        actual_cost_usd=2, verified_savings_usd=1,
    )

    weeks = store.get_stats("weekly")["billing_by_week"]
    assert [(row["week_start"], row["calls"]) for row in weeks] == [
        ("2026-07-20", 1),
        ("2026-07-13", 1),
    ]


def test_sqlite_device_activation_is_atomic_bounded_and_replay_safe(tmp_path):
    store, organization_id, device_hash, key_hash, expires_at = (
        _approved_device_store(tmp_path)
    )
    first = store.consume_device_request_idempotent(
        device_hash, key_hash, "request-device-consume-001",
    )
    assert first == {
        "status": "consumed", "already_consumed": False,
        "device_hash": device_hash, "key_hash": key_hash,
        "encrypted_key": "kms-envelope-ciphertext", "owner_id": "device-owner",
        "organization_id": organization_id, "consumed_at": first["consumed_at"],
    }

    # Client retries receive a new middleware ID. Digest identity retrieves the
    # same receipt but cannot activate a second credential.
    replay = store.consume_device_request_idempotent(
        device_hash, key_hash, "request-device-consume-002",
    )
    assert replay == {**first, "already_consumed": True}
    recovered = store.get_device_request(device_hash)
    assert recovered["key_hash"] == key_hash
    assert recovered["organization_id"] == organization_id
    assert recovered["approved_at"] == first["consumed_at"]

    with store._conn() as db:
        keys = db.execute(
            "SELECT id,key_hash,organization_id FROM api_keys WHERE key_hash=?",
            (key_hash,),
        ).fetchall()
        receipt = db.execute(
            "SELECT expires_at,request_id,owner_id,approver_id "
            "FROM bvx_device_consumption_receipts "
            "WHERE device_hash=?", (device_hash,),
        ).fetchone()
        audit = db.execute(
            "SELECT target_id,details,request_id FROM audit_events "
            "WHERE action='device_key.activated'",
        ).fetchone()
    assert len(keys) == 1 and keys[0]["organization_id"] == organization_id
    assert receipt["expires_at"] == expires_at
    assert receipt["request_id"] == "request-device-consume-001"
    assert receipt["owner_id"] == receipt["approver_id"] == "device-owner"
    assert audit["target_id"] == keys[0]["id"]
    assert audit["details"] == "{}"
    assert audit["request_id"] == "request-device-consume-001"
    assert all(secret not in repr(audit) for secret in (
        device_hash, key_hash, "kms-envelope-ciphertext",
    ))


def test_sqlite_device_receipt_upgrade_quarantines_unknown_approver_idempotently(tmp_path):
    path = tmp_path / "legacy-device-receipt.db"
    device_hash = hash_key("legacy-device-receipt")
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE bvx_device_consumption_receipts ("
            "device_hash TEXT PRIMARY KEY,key_hash TEXT NOT NULL,"
            "encrypted_key TEXT NOT NULL,owner_id TEXT NOT NULL,"
            "organization_id TEXT NOT NULL,consumed_at TEXT NOT NULL,"
            "expires_at TEXT NOT NULL,request_id TEXT NOT NULL,"
            "quarantined_at TEXT NOT NULL DEFAULT '')"
        )
        db.execute(
            "INSERT INTO bvx_device_consumption_receipts VALUES(?,?,?,?,?,?,?,?,?)",
            (device_hash, hash_key("legacy-key"), "legacy-ciphertext", "key-owner",
             "11111111-1111-4111-8111-111111111111",
             datetime.now(timezone.utc).isoformat(),
             (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
             "request-legacy-receipt", ""),
        )
    store = UsageStore(str(path))
    UsageStore(str(path))  # reapply the local upgrade
    with store._conn() as db:
        receipt = db.execute(
            "SELECT encrypted_key,approver_id,quarantined_at "
            "FROM bvx_device_consumption_receipts WHERE device_hash=?",
            (device_hash,),
        ).fetchone()
    assert receipt["encrypted_key"] == ""
    assert receipt["approver_id"] == ""
    assert receipt["quarantined_at"]
    assert store.get_device_request(device_hash) is None


def test_sqlite_device_digest_mismatch_quarantines_without_minting(tmp_path):
    store, _, device_hash, key_hash, _ = _approved_device_store(tmp_path)
    with pytest.raises(RuntimeError, match="digest mismatch quarantined"):
        store.consume_device_request_idempotent(
            device_hash, hash_key("wrong-device-key"),
            "request-device-mismatch-001",
        )
    assert store.get_device_request(device_hash) is None
    with store._conn() as db:
        exchange = db.execute(
            "SELECT encrypted_key,quarantined_at FROM bvx_device_auth "
            "WHERE device_hash=?", (device_hash,),
        ).fetchone()
        key_count = db.execute(
            "SELECT count(*) FROM api_keys WHERE key_hash=?", (key_hash,),
        ).fetchone()[0]
        denial = db.execute(
            "SELECT organization_id,actor_key_hash,action,target_type,target_id,"
            "details,request_id,actor_id,actor_role,outcome FROM audit_events "
            "WHERE action='device_key.consume.denied'",
        ).fetchone()
    assert exchange["encrypted_key"] == ""
    assert exchange["quarantined_at"]
    assert key_count == 0
    assert denial["action"] == "device_key.consume.denied"
    assert denial["target_type"] == "device_receipt"
    uuid.UUID(denial["target_id"])
    assert denial["details"] == "{}" and denial["outcome"] == "denied"
    assert denial["actor_id"] == denial["actor_role"] == "system"
    assert denial["actor_key_hash"] is None
    assert denial["request_id"] == "request-device-mismatch-001"
    assert all(secret not in repr(dict(denial)) for secret in (
        device_hash, key_hash, "kms-envelope-ciphertext",
    ))


def test_sqlite_device_activation_conflict_revokes_and_audits_atomically(tmp_path):
    store, organization_id, device_hash, key_hash, _ = _approved_device_store(tmp_path)
    store.create_key(
        key_hash, "collision", owner_id="device-owner",
        organization_id=organization_id, request_id="request-collision-setup",
        created_by="device-owner", actor_role="company_owner",
    )
    with store._conn() as db:
        key_id = db.execute(
            "SELECT id FROM api_keys WHERE key_hash=?", (key_hash,),
        ).fetchone()["id"]
    with pytest.raises(RuntimeError, match="activation conflict quarantined"):
        store.consume_device_request_idempotent(
            device_hash, key_hash, "request-activation-conflict",
        )
    with store._conn() as db:
        key = db.execute(
            "SELECT revoked_at FROM api_keys WHERE key_hash=?", (key_hash,),
        ).fetchone()
        exchange = db.execute(
            "SELECT encrypted_key,quarantined_at FROM bvx_device_auth "
            "WHERE device_hash=?", (device_hash,),
        ).fetchone()
        denial = db.execute(
            "SELECT * FROM audit_events WHERE action='device_key.consume.denied'",
        ).fetchone()
    assert key["revoked_at"]
    assert exchange["encrypted_key"] == "" and exchange["quarantined_at"]
    assert denial["target_type"] == "api_key" and denial["target_id"] == key_id
    assert denial["details"] == "{}" and denial["outcome"] == "denied"
    assert denial["actor_key_hash"] is None
    assert all(secret not in repr(dict(denial)) for secret in (
        device_hash, key_hash, "kms-envelope-ciphertext",
    ))


def test_sqlite_device_denial_audit_failure_rolls_back_quarantine(tmp_path):
    store, _, device_hash, _, _ = _approved_device_store(tmp_path)
    with store._conn() as db:
        db.execute(
            "CREATE TRIGGER reject_device_denial BEFORE INSERT ON audit_events "
            "WHEN NEW.action='device_key.consume.denied' BEGIN "
            "SELECT RAISE(ABORT,'audit unavailable'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        store.consume_device_request_idempotent(
            device_hash, hash_key("wrong-key-with-audit-failure"),
            "request-audit-rollback",
        )
    with store._conn() as db:
        exchange = db.execute(
            "SELECT encrypted_key,quarantined_at FROM bvx_device_auth "
            "WHERE device_hash=?", (device_hash,),
        ).fetchone()
    assert exchange["encrypted_key"] == "kms-envelope-ciphertext"
    assert exchange["quarantined_at"] == ""


def test_sqlite_already_quarantined_receipt_idempotently_clears_ciphertext(tmp_path):
    store, _, device_hash, key_hash, _ = _approved_device_store(tmp_path)
    store.consume_device_request_idempotent(
        device_hash, key_hash, "request-quarantine-first",
    )
    with store._conn() as db:
        db.execute(
            "UPDATE bvx_device_consumption_receipts SET quarantined_at=?,"
            "encrypted_key='stale-quarantined-ciphertext' WHERE device_hash=?",
            (datetime.now(timezone.utc).isoformat(), device_hash),
        )
    with pytest.raises(RuntimeError, match="receipt quarantined"):
        store.consume_device_request_idempotent(
            device_hash, key_hash, "request-quarantine-replay",
        )
    with store._conn() as db:
        receipt = db.execute(
            "SELECT encrypted_key,quarantined_at FROM bvx_device_consumption_receipts "
            "WHERE device_hash=?", (device_hash,),
        ).fetchone()
        denial = db.execute(
            "SELECT target_type,target_id,details,outcome FROM audit_events "
            "WHERE request_id='request-quarantine-replay'",
        ).fetchone()
    assert receipt["encrypted_key"] == "" and receipt["quarantined_at"]
    assert denial["target_type"] == "device_receipt"
    uuid.UUID(denial["target_id"])
    assert denial["details"] == "{}" and denial["outcome"] == "denied"


def test_sqlite_device_receipt_expires_and_stays_tenant_bound(tmp_path):
    store, organization_id, device_hash, key_hash, _ = _approved_device_store(tmp_path)
    other_organization = store.ensure_organization("other-device-owner")["id"]
    assert store.approve_device_request(
        device_hash, "other-device-owner", hash_key("other-owner-key"),
        "other-owner-ciphertext",
    ) is False
    first = store.consume_device_request_idempotent(
        device_hash, key_hash, "request-device-tenant-001",
    )
    assert first["organization_id"] == organization_id
    assert first["organization_id"] != other_organization

    with store._conn() as db:
        db.execute(
            "UPDATE bvx_device_consumption_receipts SET expires_at=? "
            "WHERE device_hash=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
             device_hash),
        )
    assert store.get_device_request(device_hash) is None
    assert store.consume_device_request_idempotent(
        device_hash, key_hash, "request-device-expired-001",
    ) is None


def test_device_approval_company_selector_requires_exact_active_membership(tmp_path):
    store = UsageStore(str(tmp_path / "device-company-selector.db"))
    owner_id = "device-company-owner"
    first = store.ensure_organization(owner_id)
    assert store.resolve_device_approval_organization(owner_id) == {
        "id": first["id"], "role": "company_owner",
    }

    second_id = "33333333-3333-4333-8333-333333333333"
    now = datetime.now(timezone.utc).isoformat()
    with store._conn() as db:
        db.execute(
            "INSERT INTO organizations(id,name,legacy_owner_id,billing_owner_id,created_at) "
            "VALUES(?,?,?,?,?)", (second_id, "Second company", "second-company", "", now),
        )
        db.execute(
            "INSERT INTO organization_members(organization_id,user_id,role,created_at) "
            "VALUES(?,?,?,?)", (second_id, owner_id, "member", now),
        )
    with pytest.raises(ValueError, match="^company_selection_required$"):
        store.resolve_device_approval_organization(owner_id)
    assert store.resolve_device_approval_organization(owner_id, second_id) == {
        "id": second_id, "role": "member",
    }
    with pytest.raises(ValueError, match="^company_access_denied$"):
        store.resolve_device_approval_organization(
            "foreign-device-user", second_id,
        )
    with store._conn() as db:
        db.execute(
            "UPDATE organization_members SET role='viewer' "
            "WHERE organization_id=? AND user_id=?", (second_id, owner_id),
        )
    with pytest.raises(ValueError, match="^company_access_denied$"):
        store.resolve_device_approval_organization(owner_id, second_id)
    with store._conn() as db:
        db.execute(
            "UPDATE organization_members SET role='member' "
            "WHERE organization_id=? AND user_id=?", (second_id, owner_id),
        )
        db.execute(
            "ALTER TABLE organization_members ADD COLUMN status TEXT NOT NULL "
            "DEFAULT 'active'"
        )
        db.execute(
            "UPDATE organization_members SET status='disabled' "
            "WHERE organization_id=? AND user_id=?", (second_id, owner_id),
        )
    with pytest.raises(ValueError, match="^company_access_denied$"):
        store.resolve_device_approval_organization(owner_id, second_id)
    with store._conn() as db:
        db.execute(
            "UPDATE organization_members SET status='active' "
            "WHERE organization_id=? AND user_id=?", (second_id, owner_id),
        )

    device_hash = hash_key("selected-company-device")
    key_hash = hash_key("bvt_selected-company-key")
    store.create_device_request(
        device_hash, (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )
    with pytest.raises(ValueError, match="^company_selection_required$"):
        store.approve_device_request(
            device_hash, owner_id, key_hash, "selected-company-ciphertext",
        )
    assert store.approve_device_request(
        device_hash, owner_id, key_hash, "selected-company-ciphertext", second_id,
    )
    assert store.get_device_request(device_hash)["organization_id"] == second_id


@pytest.mark.parametrize("drift", [
    "revoked", "deleted", "key_type", "tenant", "owner", "disabled", "removed",
    "invalid_role", "key_expired",
])
def test_sqlite_device_receipt_replay_quarantines_authority_drift(tmp_path, drift):
    store, organization_id, device_hash, key_hash, _ = _approved_device_store(tmp_path)
    store.consume_device_request_idempotent(
        device_hash, key_hash, "request-device-drift-first",
    )
    with store._conn() as db:
        if drift == "revoked":
            db.execute("UPDATE api_keys SET revoked_at=? WHERE key_hash=?",
                       (datetime.now(timezone.utc).isoformat(), key_hash))
        elif drift == "deleted":
            db.execute("DELETE FROM api_keys WHERE key_hash=?", (key_hash,))
        elif drift == "key_type":
            db.execute("UPDATE api_keys SET key_type='legacy' WHERE key_hash=?",
                       (key_hash,))
        elif drift == "tenant":
            db.execute("UPDATE api_keys SET organization_id=? WHERE key_hash=?",
                       ("44444444-4444-4444-8444-444444444444", key_hash))
        elif drift == "owner":
            db.execute("UPDATE api_keys SET owner_id='other-owner' WHERE key_hash=?",
                       (key_hash,))
        elif drift in ("disabled", "removed", "invalid_role"):
            member_columns = {
                row[1] for row in db.execute("PRAGMA table_info(organization_members)")
            }
            if "status" not in member_columns:
                db.execute(
                    "ALTER TABLE organization_members ADD COLUMN status TEXT NOT NULL "
                    "DEFAULT 'active'"
                )
            if drift == "invalid_role":
                db.execute(
                    "UPDATE organization_members SET role='viewer' "
                    "WHERE organization_id=? AND user_id='device-owner'",
                    (organization_id,),
                )
            else:
                db.execute(
                    "UPDATE organization_members SET status=? "
                    "WHERE organization_id=? AND user_id='device-owner'",
                    (drift, organization_id),
                )
        else:
            db.execute("UPDATE api_keys SET expires_at=? WHERE key_hash=?", (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), key_hash,
            ))

    with pytest.raises(RuntimeError, match="receipt validation failed"):
        store.consume_device_request_idempotent(
            device_hash, key_hash, f"request-device-drift-{drift}",
        )
    assert store.get_device_request(device_hash) is None
    with store._conn() as db:
        receipt = db.execute(
            "SELECT encrypted_key,quarantined_at "
            "FROM bvx_device_consumption_receipts WHERE device_hash=?",
            (device_hash,),
        ).fetchone()
    assert receipt["encrypted_key"] == ""
    assert receipt["quarantined_at"]


@pytest.mark.parametrize("approver_drift", ["disabled", "removed", "swapped", "missing"])
def test_device_receipt_requires_original_active_approver_separate_from_key_owner(
        tmp_path, approver_drift):
    store = UsageStore(str(tmp_path / f"device-approver-{approver_drift}.db"))
    owner_id = "billing-owner"
    approver_id = "non-owner-approver"
    organization_id = store.ensure_organization(owner_id)["id"]
    now = datetime.now(timezone.utc).isoformat()
    with store._conn() as db:
        db.execute(
            "INSERT INTO organization_members(organization_id,user_id,role,created_at) "
            "VALUES(?,?,?,?)", (organization_id, approver_id, "member", now),
        )
    device_hash = hash_key(f"device-approver-{approver_drift}")
    key_hash = hash_key(f"bvt_device-approver-{approver_drift}")
    store.create_device_request(
        device_hash, (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )
    assert store.approve_device_request(
        device_hash, approver_id, key_hash, "approver-envelope-ciphertext",
        organization_id,
    )
    first = store.consume_device_request_idempotent(
        device_hash, key_hash, "request-approver-first",
    )
    assert first["owner_id"] == owner_id
    with store._conn() as db:
        receipt = db.execute(
            "SELECT owner_id,approver_id FROM bvx_device_consumption_receipts "
            "WHERE device_hash=?", (device_hash,),
        ).fetchone()
        audit = db.execute(
            "SELECT actor_id,target_id FROM audit_events "
            "WHERE action='device_key.activated'",
        ).fetchone()
        key = db.execute(
            "SELECT id,owner_id,revoked_at FROM api_keys WHERE key_hash=?", (key_hash,),
        ).fetchone()
    assert receipt["owner_id"] == key["owner_id"] == owner_id
    assert receipt["approver_id"] == audit["actor_id"] == approver_id
    assert audit["target_id"] == key["id"]
    assert store.consume_device_request_idempotent(
        device_hash, key_hash, "request-approver-active-retry",
    )["already_consumed"] is True

    with store._conn() as db:
        if approver_drift in ("disabled", "removed"):
            db.execute(
                "ALTER TABLE organization_members ADD COLUMN status TEXT NOT NULL "
                "DEFAULT 'active'"
            )
            db.execute(
                "UPDATE organization_members SET status=? "
                "WHERE organization_id=? AND user_id=?",
                (approver_drift, organization_id, approver_id),
            )
        elif approver_drift == "swapped":
            replacement = "replacement-approver"
            db.execute(
                "INSERT INTO organization_members(organization_id,user_id,role,created_at) "
                "VALUES(?,?,?,?)", (organization_id, replacement, "member", now),
            )
            db.execute(
                "UPDATE bvx_device_consumption_receipts SET approver_id=? "
                "WHERE device_hash=?", (replacement, device_hash),
            )
        else:
            db.execute(
                "UPDATE bvx_device_consumption_receipts SET approver_id='' "
                "WHERE device_hash=?", (device_hash,),
            )

    with pytest.raises(RuntimeError, match="receipt validation failed"):
        store.consume_device_request_idempotent(
            device_hash, key_hash, f"request-approver-{approver_drift}",
        )
    with store._conn() as db:
        quarantined = db.execute(
            "SELECT encrypted_key,quarantined_at FROM bvx_device_consumption_receipts "
            "WHERE device_hash=?", (device_hash,),
        ).fetchone()
        owner = db.execute(
            "SELECT status FROM organization_members "
            "WHERE organization_id=? AND user_id=?",
            (organization_id, owner_id),
        ).fetchone() if approver_drift in ("disabled", "removed") else None
        active_key = db.execute(
            "SELECT revoked_at FROM api_keys WHERE key_hash=?", (key_hash,),
        ).fetchone()
    assert quarantined["encrypted_key"] == "" and quarantined["quarantined_at"]
    assert active_key["revoked_at"] == ""
    if owner:
        assert owner["status"] == "active"


def test_member_organization_requires_active_finite_canonical_role(tmp_path):
    store = UsageStore(str(tmp_path / "active-membership.db"))
    user_id = "membership-user"
    organization = store.ensure_organization(user_id)
    assert store.member_organization(user_id) == {
        "id": organization["id"], "name": "My organization",
        "role": "company_owner", "billing_owner_id": user_id,
        "account_type": "company",
    }
    with store._conn() as db:
        db.execute(
            "ALTER TABLE organization_members ADD COLUMN status TEXT NOT NULL "
            "DEFAULT 'active'"
        )
        db.execute(
            "UPDATE organization_members SET status='disabled' WHERE user_id=?",
            (user_id,),
        )
    assert store.member_organization(user_id) is None
    with store._conn() as db:
        db.execute(
            "UPDATE organization_members SET status='removed' WHERE user_id=?",
            (user_id,),
        )
    assert store.member_organization(user_id) is None
    with store._conn() as db:
        db.execute(
            "UPDATE organization_members SET status='active',role='viewer' WHERE user_id=?",
            (user_id,),
        )
    assert store.member_organization(user_id) is None


def test_supabase_member_organization_filters_active_finite_roles(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    user_id = "22222222-2222-4222-8222-222222222222"
    organization_id = "11111111-1111-4111-8111-111111111111"
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "rpc/company_admin_resolve_active_membership":
            return {"ok": True, "company_id": organization_id, "role": "member"}
        if path == "organizations":
            return [{"id": organization_id, "name": "Company", "billing_owner_id": user_id}]
        raise AssertionError(path)

    monkeypatch.setattr(store, "_request", request)
    assert store.member_organization(user_id) == {
        "id": organization_id, "name": "Company",
        "billing_owner_id": user_id, "role": "member",
    }
    assert calls[0] == (
        "POST", "rpc/company_admin_resolve_active_membership", {
            "data": {"p_actor_user_id": user_id},
        },
    )
    assert all(path != "organization_members" for _, path, _ in calls)

    calls.clear()
    monkeypatch.setattr(store, "_request", lambda method, path, **kwargs: (
        calls.append((method, path, kwargs))
        or {"ok": False, "code": "no_active_membership"}
    ))
    assert store.member_organization(user_id) is None
    assert len(calls) == 1

    monkeypatch.setattr(store, "_request", lambda method, path, **kwargs: {
        "ok": True, "company_id": organization_id, "role": "owner",
    })
    with pytest.raises(RuntimeError, match="unsafe role"):
        store.member_organization(user_id)


def test_supabase_device_commit_timeout_recovers_same_receipt(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    device_hash = hash_key("hosted-device-code")
    key_hash = hash_key("bvt_hosted-device-key")
    organization_id = "11111111-1111-4111-8111-111111111111"
    owner_id = "22222222-2222-4222-8222-222222222222"
    calls = []
    committed = {
        "ok": True, "status": "consumed", "already_consumed": True,
        "device_hash": device_hash, "key_hash": key_hash,
        "encrypted_key": "kms-hosted-ciphertext", "owner_id": owner_id,
        "organization_id": organization_id,
        "consumed_at": "2026-07-18T12:00:00+00:00",
    }

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if len(calls) == 1:
            raise requests.Timeout("response lost after commit")
        return dict(committed)

    monkeypatch.setattr(store, "_request", request)
    result = store.consume_device_request_idempotent(
        device_hash, key_hash, "0123456789abcdef0123456789abcdef",
    )
    assert result == {key: value for key, value in committed.items() if key != "ok"}
    assert len(calls) == 2
    assert all(call[:2] == ("POST", "rpc/consume_bvx_device_idempotent")
               for call in calls)
    assert all(call[2]["data"] == {
        "p_device_hash": device_hash,
        "p_expected_key_hash": key_hash,
        "p_request_id": "0123456789abcdef0123456789abcdef",
    } for call in calls)


def test_supabase_device_exchange_uses_service_only_receipt_rpc(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    device_hash = hash_key("device-exchange-get")
    calls = []
    exchange = {
        "device_hash": device_hash,
        "expires_at": "2026-07-18T12:10:00+00:00",
        "owner_id": "22222222-2222-4222-8222-222222222222",
        "organization_id": "11111111-1111-4111-8111-111111111111",
        "key_hash": hash_key("bvt_get-device-key"),
        "encrypted_key": "kms-get-ciphertext",
        "approved_at": "2026-07-18T12:00:00+00:00",
    }
    monkeypatch.setattr(store, "_request", lambda method, path, **kwargs: (
        calls.append((method, path, kwargs)) or dict(exchange)
    ))
    assert store.get_device_request(device_hash) == exchange
    assert calls == [("POST", "rpc/get_bvx_device_exchange", {
        "data": {"p_device_hash": device_hash},
    })]


def test_supabase_device_company_resolver_has_stable_selection_errors(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    owner_id = "22222222-2222-4222-8222-222222222222"
    organization_id = "11111111-1111-4111-8111-111111111111"
    calls = []
    responses = iter([
        {"ok": False, "code": "company_selection_required"},
        {"ok": True, "id": organization_id, "role": "company_admin"},
        {"ok": False, "code": "company_access_denied"},
    ])
    monkeypatch.setattr(store, "_request", lambda method, path, **kwargs: (
        calls.append((method, path, kwargs)) or next(responses)
    ))
    with pytest.raises(ValueError, match="^company_selection_required$"):
        store.resolve_device_approval_organization(owner_id)
    assert store.resolve_device_approval_organization(
        owner_id, organization_id,
    ) == {"id": organization_id, "role": "company_admin"}
    with pytest.raises(ValueError, match="^company_access_denied$"):
        store.resolve_device_approval_organization(owner_id, organization_id)
    assert all(call[0:2] == (
        "POST", "rpc/resolve_bvx_device_approval_organization") for call in calls)
    assert calls[0][2]["data"]["p_selected_organization_id"] is None
    assert calls[1][2]["data"]["p_selected_organization_id"] == organization_id


def test_supabase_device_approval_revalidates_exact_selected_company(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    owner_id = "22222222-2222-4222-8222-222222222222"
    organization_id = "11111111-1111-4111-8111-111111111111"
    device_hash = hash_key("selected-hosted-device")
    key_hash = hash_key("bvt_selected-hosted-key")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "rpc/resolve_bvx_device_approval_organization":
            return {"ok": True, "id": organization_id, "role": "company_owner"}
        if path == "rpc/approve_bvx_device":
            return True
        raise AssertionError(path)

    monkeypatch.setattr(store, "_request", request)
    assert store.approve_device_request(
        device_hash, owner_id, key_hash, "selected-hosted-ciphertext",
        organization_id,
    )
    assert calls[-1] == ("POST", "rpc/approve_bvx_device", {"data": {
        "p_device_hash": device_hash,
        "p_owner_id": owner_id,
        "p_key_hash": key_hash,
        "p_encrypted_key": "selected-hosted-ciphertext",
        "p_organization_id": organization_id,
    }})


def test_device_delivery_migration_is_hardened_bounded_and_opaque():
    migration = (Path(__file__).parent.parent / "supabase/migrations/"
                 "202607170010_device_delivery_idempotency.sql").read_text()
    lowered = migration.lower()
    assert "function public.consume_bvx_device_idempotent" in lowered
    assert "function public.get_bvx_device_exchange" in lowered
    assert "function public.resolve_bvx_device_approval_organization" in lowered
    assert lowered.count("set search_path = pg_catalog, public, pg_temp") >= 4
    assert "for update" in lowered and "pg_advisory_xact_lock" in lowered
    assert "grant execute on function public.consume_bvx_device_idempotent" in lowered
    assert "grant execute on function public.get_bvx_device_exchange" in lowered
    assert ("grant execute on function "
            "public.resolve_bvx_device_approval_organization(text,uuid)" in lowered)
    assert "public.approve_bvx_device(text,text,text,text,uuid)" in lowered
    assert "public.approve_bvx_device(text,text,text,text)\n    from public" in lowered
    assert "from public, anon, authenticated, service_role" in lowered
    assert "expires_at <= consumed_at + interval '15 minutes'" in lowered
    assert "v_existing_key.owner_id=v_receipt.owner_id::text" in lowered
    assert "v_existing_key.key_type='device'" in lowered
    assert "v_existing_key.revoked_at is null" in lowered
    assert "set encrypted_key='',quarantined_at=now()" in lowered
    assert "or (quarantined_at is not null and encrypted_key='')" in lowered
    assert "approver_id uuid references auth.users" in lowered
    assert "member.user_id=v_receipt.approver_id" in lowered
    assert "event.actor_id=v_receipt.approver_id::text" in lowered
    assert "id uuid not null default gen_random_uuid()" in lowered
    assert "'device_key.consume.denied','device_receipt'" in lowered
    assert "drop constraint if exists bvx_device_receipt_ciphertext_check" in lowered
    assert "add constraint bvx_device_receipt_ciphertext_check check" in lowered
    assert "receipt.encrypted_key is distinct from ''" in lowered
    assert "not valid" in lowered
    assert "validate constraint bvx_device_receipt_ciphertext_check" in lowered
    assert "coalesce(v_existing_key.id,v_receipt.id)::text" in lowered
    assert "device_key.activated','api_key',v_key_id::text" in lowered
    assert "append_company_audit(\n            v_organization_id" in lowered
    assert "p_device_hash,'denied'" not in lowered
    assert "raw_key" not in lowered and "api_key text" not in lowered


def test_sqlite_keyset_page_is_tenant_scoped_stable_and_capped(tmp_path):
    store = UsageStore(str(tmp_path / "usage.db"))
    store.create_key("key-a", "a", organization_id="org-a")
    store.create_key("key-a-2", "a2", organization_id="org-a")
    store.create_key("key-b", "b", organization_id="org-b")
    timestamp = "2026-07-18T12:00:00+00:00"
    for key, organization, request_id in (
        ("key-a", "org-a", "a-1"),
        ("key-a-2", "org-a", "a-2"),
        ("key-a", "org-a", "a-3"),
        ("key-b", "org-b", "b-1"),
    ):
        assert store.record_usage(key, 100, 80, organization_id=organization,
                                  request_id=request_id, ts=timestamp)

    first = store.list_usage_page("key-a", limit=2)
    assert first["limit"] == 2
    assert len(first["rows"]) == 2
    assert {row["organization_id"] for row in first["rows"]} == {"org-a"}
    assert first["next_cursor"]

    # A concurrent insert sorts before the cursor and cannot duplicate/shift page 2.
    assert store.record_usage("key-a", 100, 80, organization_id="org-a",
                              request_id="a-new", ts=timestamp)
    second = store.list_usage_page("key-a", cursor=first["next_cursor"], limit=2)
    first_ids = {row["id"] for row in first["rows"]}
    assert not first_ids.intersection(row["id"] for row in second["rows"])
    assert [row["request_id"] for row in second["rows"]] == ["a-1"]
    assert store.list_usage_page("key-a", limit=10_000)["limit"] == 200
    with pytest.raises(ValueError, match="invalid usage cursor"):
        store.list_usage_page("key-a", cursor="not-a-cursor")


def test_supabase_aggregates_and_pages_use_tenant_scoped_rpcs(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    organization_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(store, "key_context", lambda key: {
        "key_hash": key, "organization_id": organization_id, "owner_id": "owner-a",
    })
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "rpc/usage_stats":
            return {"total_calls": 3}
        if path == "rpc/usage_grouped":
            field = kwargs["data"]["p_field"]
            return [{field: "group", "calls": 3}]
        if path == "rpc/usage_breakdown":
            return [{"repo": "repo", "calls": 3}]
        if path == "rpc/usage_page":
            return [
                {"id": 9, "ts": "2026-07-18T12:00:00+00:00"},
                {"id": 8, "ts": "2026-07-18T12:00:00+00:00"},
                {"id": 7, "ts": "2026-07-18T12:00:00+00:00"},
            ]
        raise AssertionError(path)

    monkeypatch.setattr(store, "_request", request)
    stats = store.get_stats("key-a")
    assert stats["total_calls"] == 3
    assert store.get_breakdown("key-a")[0]["calls"] == 3
    page = store.list_usage_page("key-a", limit=2)
    assert [row["id"] for row in page["rows"]] == [9, 8]
    assert page["next_cursor"]

    usage_calls = [call for call in calls if call[1].startswith("rpc/usage_")]
    assert usage_calls
    assert all(call[0] == "POST" for call in usage_calls)
    assert all(call[2]["data"]["p_organization_id"] == organization_id
               for call in usage_calls)
    assert all(call[2]["data"]["p_key_hash"] == "key-a" for call in usage_calls)
    assert not any("offset" in call[2].get("data", {}) for call in usage_calls)


def test_admin_reports_aggregate_in_sql_and_bound_inventory(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "rpc/admin_usage_report":
            return {"totals": {"total_calls": 4}, "rows": [], "truncated": False}
        if path == "api_keys":
            return []
        if path == "key_repositories":
            return []
        if path == "rpc/admin_key_repository_usage":
            return []
        raise AssertionError(path)

    monkeypatch.setattr(store, "_request", request)
    report = store.get_admin_report({"organization_id": "tenant-a", "provider": "openai"})
    inventory = store.get_admin_key_inventory()
    assert report["totals"]["total_calls"] == 4
    rpc = next(call for call in calls if call[1] == "rpc/admin_usage_report")
    assert rpc[2]["data"]["p_filters"] == {
        "organization_id": "tenant-a", "provider": "openai",
    }
    assert inventory["total_keys"] == 0
    assert not any(call[1] == "usage_log" for call in calls)
    assert all("offset" not in call[2].get("params", {}) for call in calls)
    assert next(call for call in calls if call[1] == "api_keys")[2]["params"]["limit"] == "500"


def test_admin_report_page_binds_cursor_to_sort_and_has_stable_tie_breaker(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        assert path == "rpc/admin_usage_report_page"
        return {"totals": {"total_calls": 3}, "total": 3, "rows": [
            {"account_id": "a", "actual_cost_usd": 10, "_sort_value": 10,
             "_row_key": "key-c"},
            {"account_id": "b", "actual_cost_usd": 10, "_sort_value": 10,
             "_row_key": "key-b"},
            {"account_id": "c", "actual_cost_usd": 5, "_sort_value": 5,
             "_row_key": "key-a"},
        ]}

    monkeypatch.setattr(store, "_request", request)
    first = store.get_admin_report_page({}, sort="actual_cost_usd",
                                        direction="desc", limit=2)
    assert [row["account_id"] for row in first["rows"]] == ["a", "b"]
    assert first["pagination"] == {
        "total": 3, "limit": 2, "next_cursor": first["pagination"]["next_cursor"],
        "has_more": True,
    }
    assert first["pagination"]["next_cursor"]
    assert "_sort_value" not in first["rows"][0]

    store.get_admin_report_page({}, sort="actual_cost_usd", direction="desc",
                                cursor=first["pagination"]["next_cursor"], limit=2)
    second_data = calls[-1][2]["data"]
    assert second_data["p_cursor_value"] == "10"
    assert second_data["p_cursor_key"] == "key-b"
    assert second_data["p_sort"] == "actual_cost_usd"
    assert second_data["p_direction"] == "desc"
    assert "offset" not in second_data
    with pytest.raises(ValueError, match="sort order"):
        store.get_admin_report_page({}, sort="calls", direction="desc",
                                    cursor=first["pagination"]["next_cursor"])


def test_local_admin_cursor_pages_equal_sort_values_without_duplicates(tmp_path):
    store = UsageStore(str(tmp_path / "admin-page.db"))
    for index in range(3):
        store.record_usage(
            f"key-{index}", 100, 80, owner_id=f"owner-{index}",
            project=f"project-{index}", actual_cost_usd=1,
            request_id=f"request-{index}",
        )
    first = store.get_admin_report_page({}, sort="actual_cost_usd",
                                        direction="desc", limit=2)
    second = store.get_admin_report_page(
        {}, sort="actual_cost_usd", direction="desc",
        cursor=first["pagination"]["next_cursor"], limit=2,
    )
    accounts = [row["account_id"] for row in first["rows"] + second["rows"]]
    assert len(accounts) == len(set(accounts)) == 3
    assert first["pagination"]["total"] == second["pagination"]["total"] == 3


def test_local_batch_caps_duplicates_and_partial_failures(tmp_path):
    store = UsageStore(str(tmp_path / "batch.db"))
    result = store.record_usage_batch([
        _receipt("one"),
        _receipt("one"),
        {"key_hash": "key-a", "baseline_tokens": "bad", "optimized_tokens": 1},
        _receipt("two"),
    ])
    assert {key: result[key] for key in ("read", "inserted", "duplicates", "failed")} == {
        "read": 4, "inserted": 2, "duplicates": 1, "failed": 1,
    }
    assert result["failed_records"] == [
        {"key_hash": "key-a", "baseline_tokens": "bad", "optimized_tokens": 1},
    ]
    assert store.get_stats("key-a")["total_calls"] == 2
    with pytest.raises(ValueError, match="maximum"):
        store.record_usage_batch([_receipt(str(index))
                                  for index in range(USAGE_BATCH_MAX + 1)])


def test_supabase_batch_isolates_failed_rows_after_atomic_failure(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    individual = iter(([{"id": 1}], [], requests.RequestException("invalid row")))

    def request(method, path, **kwargs):
        if isinstance(kwargs.get("data"), list):
            raise requests.RequestException("atomic batch rejected")
        answer = next(individual)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(store, "_request", request)
    result = store.record_usage_batch([_receipt("one"), _receipt("duplicate"),
                                       _receipt("invalid")])
    assert {key: result[key] for key in ("read", "inserted", "duplicates", "failed")} == {
        "read": 3, "inserted": 1, "duplicates": 1, "failed": 1,
    }
    assert result["retry_records"] == [_receipt("invalid")]


def test_ambiguous_bulk_timeout_never_retries_append_only_rows(monkeypatch):
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    calls = []
    committed = []

    def timeout_after_commit(method, path, **kwargs):
        calls.append(kwargs["data"])
        committed.extend(kwargs["data"])
        raise requests.Timeout("response lost after commit")

    monkeypatch.setattr(store, "_request", timeout_after_commit)
    with pytest.raises(AmbiguousUsageBatchError, match="outcome is unknown") as error:
        store.record_usage_batch([_receipt("")])
    assert error.value.records == [_receipt("")]
    assert len(calls) == 1
    assert len(committed) == 1


def test_historical_import_forces_authoritative_priced_rows_non_billable(tmp_path):
    source = UsageStore(str(tmp_path / "source.db"))
    assert source.record_usage(
        "legacy", 100, 50, request_id="legacy-billable", authoritative=True,
        pricing_status="priced", verified_savings_usd=1, brevitas_fee_usd=.25,
    )
    target = UsageStore(str(tmp_path / "target.db"))
    assert import_sqlite(source.db_path, target) == {
        "read": 1, "inserted": 1, "duplicates": 0,
    }
    with target._conn() as db:
        imported = db.execute(
            "SELECT authoritative,receipt_source,pricing_status,brevitas_fee_usd "
            "FROM usage_log WHERE request_id=?", ("legacy-billable",),
        ).fetchone()
    assert dict(imported) == {
        "authoritative": 0, "receipt_source": "import",
        "pricing_status": "priced", "brevitas_fee_usd": 0,
    }


def test_legacy_cache_migration_is_a_purge_only_encrypted_redirect():
    root = Path(__file__).parent.parent
    guard = (root / "api/migrations/002_semantic_cache.sql").read_text().lower()
    canonical = (root / "supabase/migrations/202607170002_cache_security.sql").read_text().lower()
    docs = (root / "docs/DATABASE_SCALING.md").read_text().lower()

    assert "create table" not in guard
    assert "202607170002_cache_security.sql" in guard
    assert "where response_json is not null" in guard
    assert "or response_ciphertext = ''" in guard
    assert "check (response_json is null)" in guard
    assert "revoke insert, update on table public.semantic_cache from service_role" in guard
    assert "drop function if exists public.semantic_cache_lookup(vector, text, float)" in guard
    assert "grant select, insert" not in guard
    assert "response_json     jsonb not null" not in guard
    assert "semantic_cache_store_bounded" in canonical
    assert "p_tenant_namespace" in canonical
    assert "legacy upgrade" in docs and "reverse ordering safety" in docs
    assert "pgvector-enabled" in docs


def test_local_key_audits_use_opaque_ids_and_content_free_fields(tmp_path):
    store = UsageStore(str(tmp_path / "audit-writers.db"))
    organization = store.ensure_organization("actor-123", "Company")
    full_hash = "a" * 64
    store.create_key(
        full_hash, "production", organization_id=organization["id"],
        created_by="actor-123", request_id="request-create-001",
        actor_role="company_admin",
    )
    with store._conn() as db:
        key_id = db.execute("SELECT id FROM api_keys WHERE key_hash=?", (full_hash,)).fetchone()[0]
        created = dict(db.execute(
            "SELECT organization_id,actor_key_hash,action,target_id,details,request_id,"
            "actor_id,actor_role,outcome FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone())
    assert created == {
        "organization_id": organization["id"], "actor_key_hash": None,
        "action": "api_key.created", "target_id": key_id, "details": "{}",
        "request_id": "request-create-001", "actor_id": "actor-123",
        "actor_role": "company_admin", "outcome": "committed",
    }
    assert created["target_id"] != full_hash
    assert len(created["target_id"]) <= 36

    assert store.revoke_organization_key(
        organization["id"], key_id, "actor-123",
        request_id="request-revoke-001", actor_role="company_admin",
    )
    with store._conn() as db:
        revoked = dict(db.execute(
            "SELECT actor_key_hash,action,target_id,request_id,actor_role "
            "FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone())
    assert revoked == {
        "actor_key_hash": None, "action": "api_key.revoked", "target_id": key_id,
        "request_id": "request-revoke-001", "actor_role": "company_admin",
    }


def test_supabase_dashboard_key_store_uses_one_atomic_rpc_and_returns_raw_once(monkeypatch):
    import api.store as store_module

    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    calls = []
    organization_id = "11111111-1111-4111-8111-111111111111"
    actor_id = "22222222-2222-4222-8222-222222222222"
    key_id = "33333333-3333-4333-8333-333333333333"
    raw_key = "bvt_AtomicSessionSecret123456789"
    generated = []

    def generate():
        generated.append(True)
        return raw_key

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        data = kwargs["data"]
        assert method == "POST"
        assert path == "rpc/company_admin_create_dashboard_session_key"
        return {"ok": True, "key_id": key_id, "organization_id": organization_id,
                "key_type": "dashboard_session",
                "scopes": ["proxy:invoke", "usage:read_own"],
                "environment": "dashboard", "prefix": data["p_key_prefix"],
                "expires_at": data["p_expires_at"]}

    monkeypatch.setattr(store_module, "generate_api_key", generate)
    monkeypatch.setattr(store, "_request", request)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = store.create_key(
        "caller-supplied-digest-is-ignored", "dashboard",
        organization_id=organization_id, key_type="dashboard_session",
        created_by=actor_id, request_id="request-create-cloud",
        actor_role="company_admin", expires_at=expires,
    )
    assert generated == [True]
    assert result["api_key"] == raw_key
    assert result["secret_available_once"] is True
    assert result["key_id"] == key_id
    assert result["organization_id"] == organization_id
    assert result["request_id"] == "request-create-cloud"
    assert "key_hash" not in result
    assert len(calls) == 1
    assert calls[0][2]["data"] == {
        "p_organization_id": organization_id,
        "p_actor_user_id": actor_id,
        "p_key_hash": hash_key(raw_key),
        "p_key_prefix": raw_key[:12],
        "p_expires_at": expires,
        "p_request_id": "request-create-cloud",
    }
    assert raw_key not in repr(calls)


def test_supabase_atomic_key_failure_discards_secret_and_revoke_is_one_rpc(monkeypatch):
    import api.store as store_module

    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    organization_id = "11111111-1111-4111-8111-111111111111"
    actor_id = "22222222-2222-4222-8222-222222222222"
    key_id = "33333333-3333-4333-8333-333333333333"
    raw_key = "bvt_FailureSecretMustNotEscape123"
    calls = []
    generated = []

    def generate():
        generated.append(True)
        return raw_key

    def denied(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"ok": False, "code": "duplicate_key"}

    monkeypatch.setattr(store_module, "generate_api_key", generate)
    monkeypatch.setattr(store, "_request", denied)
    with pytest.raises(RuntimeError, match="duplicate_key") as error:
        store.create_key(
            "ignored", "dashboard", organization_id=organization_id,
            key_type="dashboard_session", created_by=actor_id,
            request_id="request-create-denied", actor_role="member",
        )
    assert raw_key not in str(error.value)
    assert raw_key not in repr(calls)
    assert generated == [True]
    assert len(calls) == 1

    calls.clear()
    monkeypatch.setattr(store, "_request", lambda method, path, **kwargs: (
        calls.append((method, path, kwargs)) or
        {"ok": True, "key_id": key_id, "revoked": False, "already_revoked": True}
    ))
    assert store.revoke_organization_key(
        organization_id, key_id, actor_id, request_id="request-revoke-cloud",
        actor_role="billing_admin",
    ) is True
    assert [(call[0], call[1], call[2]["data"]) for call in calls] == [
        ("POST", "rpc/company_admin_revoke_dashboard_session_key", {
            "p_organization_id": organization_id,
            "p_actor_user_id": actor_id,
            "p_key_id": key_id,
            "p_request_id": "request-revoke-cloud",
        }),
    ]

    calls.clear()
    with pytest.raises(ValueError, match="actor_role"):
        store.create_key(
            "ignored", "invalid", organization_id=organization_id,
            key_type="dashboard_session", created_by=actor_id,
            request_id="request-invalid-role", actor_role="unbounded_super_admin",
        )
    assert calls == []
    with pytest.raises(ValueError, match="within 8 hours"):
        store.create_key(
            "ignored", "invalid", organization_id=organization_id,
            key_type="dashboard_session", created_by=actor_id,
            request_id="request-invalid-expiry", actor_role="company_admin",
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=9)).isoformat(),
        )
    with pytest.raises(ValueError, match="request_id is required"):
        store.create_key(
            "ignored", "invalid", organization_id=organization_id,
            key_type="dashboard_session", created_by=actor_id,
            actor_role="company_admin",
        )
    assert generated == [True]  # validation failures never generate a secret


def test_w9_atomic_store_inspection_has_no_split_key_or_audit_writes():
    create_source = inspect.getsource(SupabaseUsageStore.create_key)
    revoke_source = inspect.getsource(SupabaseUsageStore.revoke_organization_key)
    assert create_source.count("generate_api_key()") == 1
    assert '"POST", "rpc/company_admin_create_dashboard_session_key"' in create_source
    assert '"api_keys"' not in create_source
    assert '"audit_events"' not in create_source
    assert '"POST", "rpc/company_admin_revoke_dashboard_session_key"' in revoke_source
    assert '"GET"' not in revoke_source and '"PATCH"' not in revoke_source
    assert '"audit_events"' not in revoke_source
    bulk_source = inspect.getsource(SupabaseUsageStore.revoke_keys_by_type)
    assert "_request" not in bulk_source
    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    with pytest.raises(RuntimeError, match="bulk key revocation is disabled"):
        store.revoke_keys_by_type(
            "11111111-1111-4111-8111-111111111111", "dashboard_session",
            "22222222-2222-4222-8222-222222222222",
        )


def test_supabase_dashboard_key_page_is_hmac_bound_capped_and_rpc_only(monkeypatch):
    organization_id = "11111111-1111-4111-8111-111111111111"
    other_organization = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    actor_id = "22222222-2222-4222-8222-222222222222"
    created = "2026-07-18T10:00:00+00:00"
    ids = [
        "33333333-3333-4333-8333-333333333333",
        "33333333-3333-4333-8333-333333333332",
        "33333333-3333-4333-8333-333333333331",
    ]

    def row(key_id):
        return {"id": key_id, "name": "dashboard session", "created": created,
                "key_type": "dashboard_session", "scopes": ["proxy:invoke"],
                "environment": "dashboard", "prefix": "bvt_prefix01",
                "service_account_id": None, "expires_at": "2026-07-18T11:00:00+00:00",
                "last_used_at": None, "revoked_at": None}

    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        assert method == "POST" and path == "rpc/company_admin_dashboard_keys_page"
        data = kwargs["data"]
        return {"ok": True, "items": ([row(value) for value in ids]
                if data["p_cursor_id"] is None else [row(ids[-1])])}

    store = SupabaseUsageStore(
        "https://example.supabase.co", "service-role", cursor_secret="c" * 40,
    )
    monkeypatch.setattr(store, "_request", request)
    first = store.list_organization_keys_page(
        organization_id, actor_id, limit=2, request_id="request-key-page-1",
        actor_role="company_admin",
    )
    assert [item["id"] for item in first["keys"]] == ids[:2]
    assert first["has_more"] is True and first["limit"] == 2
    assert first["next_cursor"] and len(first["next_cursor"]) <= 512
    assert all("key_hash" not in item and "fingerprint" not in item
               for item in first["keys"])
    assert calls[0][2]["data"] == {
        "p_organization_id": organization_id,
        "p_actor_user_id": actor_id,
        "p_cursor_time": None,
        "p_cursor_id": None,
        "p_limit": 2,
        "p_request_id": "request-key-page-1",
    }

    second = store.list_organization_keys_page(
        organization_id, actor_id, cursor=first["next_cursor"], limit=2,
        request_id="request-key-page-2", actor_role="company_admin",
    )
    assert [item["id"] for item in second["keys"]] == [ids[-1]]
    assert second["next_cursor"] == "" and second["has_more"] is False
    assert calls[1][2]["data"]["p_cursor_time"] == created
    assert calls[1][2]["data"]["p_cursor_id"] == ids[1]

    before = len(calls)
    tampered = first["next_cursor"][:-1] + (
        "A" if first["next_cursor"][-1] != "A" else "B"
    )
    with pytest.raises(ValueError, match="invalid dashboard key cursor"):
        store.list_organization_keys_page(
            organization_id, actor_id, cursor=tampered, limit=2,
            request_id="request-key-page-tamper", actor_role="company_admin",
        )
    with pytest.raises(ValueError, match="invalid dashboard key cursor"):
        store.list_organization_keys_page(
            other_organization, actor_id, cursor=first["next_cursor"], limit=2,
            request_id="request-key-page-cross", actor_role="company_admin",
        )
    assert len(calls) == before


def test_dashboard_key_page_rejects_unsafe_metadata_and_uncontextualized_listing(monkeypatch):
    store = SupabaseUsageStore(
        "https://example.supabase.co", "service-role", cursor_secret="c" * 40,
    )
    with pytest.raises(RuntimeError, match="requires actor"):
        store.list_organization_keys("11111111-1111-4111-8111-111111111111")

    unsafe = {"id": "33333333-3333-4333-8333-333333333333",
              "name": "dashboard session", "created": "2026-07-18T10:00:00+00:00",
              "key_type": "dashboard_session", "scopes": [], "environment": "dashboard",
              "prefix": "bvt_prefix01", "service_account_id": None, "expires_at": None,
              "last_used_at": None, "revoked_at": None, "key_hash": "a" * 64}
    calls = []
    monkeypatch.setattr(store, "_request", lambda method, path, **kwargs: (
        calls.append((method, path, kwargs)) or {"ok": True, "items": [unsafe]}
    ))
    with pytest.raises(RuntimeError, match="unsafe metadata"):
        store.list_organization_keys_page(
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222", limit=1000,
            request_id="request-key-page-unsafe", actor_role="company_owner",
        )
    assert calls[0][2]["data"]["p_limit"] == 100

    list_source = inspect.getsource(SupabaseUsageStore.list_organization_keys_page)
    legacy_source = inspect.getsource(SupabaseUsageStore.list_organization_keys)
    assert '"POST", "rpc/company_admin_dashboard_keys_page"' in list_source
    assert '"GET"' not in list_source and '"api_keys"' not in list_source
    assert "_request" not in legacy_source


class _CapturingStore:
    def __init__(self):
        self.batches = []
        self.synchronous = []
        self.flushed = threading.Event()

    def record_usage_batch(self, records):
        self.batches.append([dict(record) for record in records])
        self.flushed.set()
        return {"read": len(records), "inserted": len(records),
                "duplicates": 0, "failed": 0}

    def record_usage(self, key_hash, baseline, optimized, savings=0,
                     quality=None, **values):
        self.synchronous.append((key_hash, values))
        return True


def test_bounded_writer_flushes_on_size_interval_shutdown_and_authoritative_path():
    capture = _CapturingStore()
    writer = BoundedUsageWriter(capture, max_batch_size=2,
                                flush_interval_seconds=0.05, autostart=False)
    assert writer.add(_receipt("one")) is None
    size_flush = writer.add(_receipt("two"))
    assert size_flush["inserted"] == 2
    writer.add(_receipt("three"))
    assert writer.close()["inserted"] == 1
    assert [len(batch) for batch in capture.batches] == [2, 1]

    timed = _CapturingStore()
    with BoundedUsageWriter(timed, max_batch_size=3,
                            flush_interval_seconds=0.05) as periodic:
        periodic.add(_receipt("periodic"))
        assert timed.flushed.wait(0.5)
    assert len(timed.batches) == 1

    authoritative = _CapturingStore()
    with BoundedUsageWriter(authoritative, autostart=False) as direct:
        direct.add(_receipt("queued"))
        result = direct.add(_receipt("billable", authoritative=True))
        assert result["inserted"] == 1
        assert direct.pending_count == 1
    assert [row[0]["request_id"] for row in authoritative.batches] == ["queued"]
    assert authoritative.synchronous[0][1]["request_id"] == "billable"
    assert authoritative.synchronous[0][1]["authoritative"] is True


class _BlockingStore(_CapturingStore):
    def __init__(self, *, fail_once=False):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail_once = fail_once

    def record_usage_batch(self, records):
        self.batches.append([dict(record) for record in records])
        self.entered.set()
        assert self.release.wait(1)
        if self.fail_once:
            self.fail_once = False
            raise requests.ConnectionError("database unavailable")
        return {"read": len(records), "inserted": len(records),
                "duplicates": 0, "failed": 0}


def test_writer_capacity_includes_inflight_and_restores_transient_failure():
    store = _BlockingStore(fail_once=True)
    writer = BoundedUsageWriter(store, max_batch_size=2, autostart=False)
    writer.add(_receipt("one"))
    outcome = []

    def flush():
        try:
            writer.flush()
        except Exception as exc:
            outcome.append(exc)

    thread = threading.Thread(target=flush)
    thread.start()
    assert store.entered.wait(1)
    assert writer.pending_count == 1  # in-flight still consumes capacity
    writer.add(_receipt("two"))
    with pytest.raises(RuntimeError, match="buffer is full"):
        writer.add(_receipt("three"))
    store.release.set()
    thread.join(1)
    assert isinstance(outcome[0], requests.ConnectionError)
    assert writer.pending_count == 2
    # Only the failed in-flight row and the concurrently queued row are retried.
    assert writer.flush()["inserted"] == 2
    assert [[row["request_id"] for row in batch] for batch in store.batches] == [
        ["one"], ["one", "two"],
    ]
    writer.close()


def test_writer_close_is_atomic_with_add_and_concurrent_close():
    store = _BlockingStore()
    writer = BoundedUsageWriter(store, max_batch_size=3, autostart=False)
    writer.add(_receipt("one"))
    results = []

    def close():
        results.append(writer.close())

    first = threading.Thread(target=close)
    first.start()
    assert store.entered.wait(1)
    second = threading.Thread(target=close)
    second.start()
    with pytest.raises(RuntimeError, match="closing or closed"):
        writer.add(_receipt("late"))
    store.release.set()
    first.join(1)
    second.join(1)
    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 2
    assert len(store.batches) == 1


def test_writer_quarantines_partial_results_without_retrying_successes():
    class PartialStore(_CapturingStore):
        def record_usage_batch(self, records):
            self.batches.append([dict(record) for record in records])
            return {"read": 2, "inserted": 1, "duplicates": 0, "failed": 1,
                    "failed_records": [dict(records[1])]}

    store = PartialStore()
    writer = BoundedUsageWriter(store, max_batch_size=2, autostart=False)
    writer.add(_receipt("success"))
    with pytest.raises(UsageBatchPartialFailure, match="reconciliation required") as error:
        writer.add(_receipt("failed"))
    assert error.value.result["inserted"] == 1
    assert len(store.batches) == 1
    assert writer.pending_count == 1
    assert writer.failed_records == [_receipt("failed")]
    assert writer.take_failed_records() == [_receipt("failed")]
    assert writer.pending_count == 0
    writer.close()


def test_writer_quarantines_whole_batch_when_partial_result_lacks_row_identity():
    class IncompleteProtocolStore(_CapturingStore):
        def record_usage_batch(self, records):
            self.batches.append([dict(record) for record in records])
            return {"read": 2, "inserted": 1, "duplicates": 0, "failed": 1}

    store = IncompleteProtocolStore()
    writer = BoundedUsageWriter(store, max_batch_size=2, autostart=False)
    writer.add(_receipt("possibly-successful"))
    with pytest.raises(UsageBatchPartialFailure):
        writer.add(_receipt("unknown-failure"))
    assert len(store.batches) == 1
    assert writer.pending_count == 2
    assert writer.failed_records == [
        _receipt("possibly-successful"), _receipt("unknown-failure"),
    ]
    # No member is retried automatically, so the reported success cannot duplicate.
    with pytest.raises(UsageBatchPartialFailure):
        writer.close()
    assert len(store.batches) == 1
    writer.take_failed_records()
    writer.close()


def test_writer_blocks_ambiguous_inflight_batch_from_automatic_retry():
    class AmbiguousStore(_CapturingStore):
        def record_usage_batch(self, records):
            self.batches.append([dict(record) for record in records])
            raise AmbiguousUsageBatchError("unknown commit")

    store = AmbiguousStore()
    writer = BoundedUsageWriter(store, max_batch_size=2, autostart=False)
    writer.add(_receipt(""))
    with pytest.raises(AmbiguousUsageBatchError):
        writer.flush()
    assert writer.pending_count == 1
    assert writer.failed_records == [_receipt("")]
    with pytest.raises(AmbiguousUsageBatchError):
        writer.close()
    assert len(store.batches) == 1
    writer.take_failed_records()
    writer.close()


def test_migration_has_matching_indexes_rpc_caps_and_explicit_rollback():
    root = Path(__file__).parent.parent
    migration = (root / "api/migrations/004_database_scaling.sql").read_text()
    rollback = (root / "api/migrations/004_database_scaling.rollback.sql").read_text()
    concurrent = (root / "api/migrations/004_database_scaling.concurrent_indexes.sql").read_text()
    for contract in ("usage_page", "usage_stats", "usage_breakdown",
                     "usage_grouped", "admin_usage_report",
                     "admin_usage_report_page"):
        assert f"function public.{contract}" in migration
        assert f"function if exists public.{contract}" in rollback
    assert "(usage.ts, usage.id) < (p_cursor_ts, p_cursor_id)" in migration
    assert "organization_id, ts desc, id desc" in migration
    assert "organization_id, pipeline, ts desc, id desc" in migration
    assert "limit least(greatest" in migration
    assert "(ranked.sort_value, ranked.row_key)" in migration
    assert "p_sort not in" in migration
    assert "offset" not in migration.lower()
    assert "drop index concurrently if exists" in rollback.lower()
    assert "202607170003_durable_jobs.sql" in migration
    assert concurrent.lower().count("create index concurrently if not exists") >= 10
