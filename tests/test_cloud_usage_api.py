import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import pytest
from fastapi.testclient import TestClient

from api.auth import hash_key
from api.store import UsageStore
from brevitas.security import EnvelopeCipher, LocalTestKMS

BEARER = "Bearer"


def test_dashboard_identity_prefers_authoritative_service_role(monkeypatch):
    import api.server as server

    captured = {}

    class Response:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {"id": "user-1"}

    def get(url, *, headers, timeout):
        captured.update(url=url, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "stale-anon")
    monkeypatch.setattr(server._requests, "get", get)

    identity = server._dashboard_identity(SimpleNamespace(
        headers={"authorization": f"{BEARER} user-token"}))

    assert identity["id"] == "user-1"
    assert captured["headers"] == {
        "apikey": "service-role", "Authorization": f"{BEARER} user-token"}


def test_bvx_device_login_mints_one_time_account_key(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "device.db"))
    kms = LocalTestKMS(b"d" * 32, environ={"BREVITAS_ENV": "test"})
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(
        server,
        "_credential_cipher",
        EnvelopeCipher(
            kms,
            key_id="device-login-test-key",
            key_version="1",
            wrap_algorithm=kms.algorithm,
        ),
    )
    client = TestClient(server.app)

    started = client.post("/v1/device-auth/start")
    assert started.status_code == 200
    device_code = started.json()["device_code"]
    assert started.json()["verification_uri_complete"].endswith(f"#bvx={device_code}")
    assert device_code not in repr(store.get_device_request(hash_key(device_code)))

    pending = client.post("/v1/device-auth/token", json={"device_code": device_code})
    assert pending.status_code == 202
    monkeypatch.setattr(server, "_dashboard_user", lambda request: "")
    assert client.post("/v1/device-auth/approve", json={"device_code": device_code}).status_code == 401
    monkeypatch.setattr(server, "_dashboard_user", lambda request: "user-device")
    assert client.post("/v1/device-auth/approve", json={"device_code": device_code}).status_code == 200
    assert client.post("/v1/device-auth/approve", json={"device_code": device_code}).status_code == 200
    exchange_expires_at = store.get_device_request(hash_key(device_code))["expires_at"]

    token = client.post(
        "/v1/device-auth/token",
        json={"device_code": device_code},
        headers={"X-Request-ID": "device-token-first-request"},
    )
    assert token.status_code == 200
    api_key = token.json()["api_key"]
    assert api_key.startswith("bvt_")
    assert store.key_owner(hash_key(api_key)) == "user-device"
    imported = client.post("/v1/customers/import", headers={"X-Brevitas-Key": api_key}, json={
        "customers": [{"external_id": "legacy-device-import-001"}],
    })
    assert imported.status_code == 200
    assert imported.json()["count"] == 1
    replay = client.post(
        "/v1/device-auth/token",
        json={"device_code": device_code},
        headers={"X-Request-ID": "device-token-replay-request"},
    )
    assert replay.status_code == 200
    assert replay.json() == {"api_key": api_key}
    assert replay.headers["cache-control"] == "no-store"
    with store._conn() as db:
        receipt = db.execute(
            "SELECT request_id,encrypted_key,expires_at "
            "FROM bvx_device_consumption_receipts WHERE device_hash=?",
            (hash_key(device_code),),
        ).fetchone()
    assert token.headers["x-request-id"] != replay.headers["x-request-id"]
    assert receipt["request_id"] == token.headers["x-request-id"]
    assert receipt["encrypted_key"]
    assert receipt["expires_at"] == exchange_expires_at
    assert datetime.fromisoformat(receipt["expires_at"]) > datetime.now(timezone.utc)

    expired = "expired_" + "x" * 40
    store.create_device_request(hash_key(expired),
                                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    assert client.post("/v1/device-auth/token", json={"device_code": expired}).status_code == 410


@pytest.mark.parametrize("invalidation", ["revoked_key", "disabled_member"])
def test_bvx_device_replay_fails_closed_after_authorization_is_revoked(
        tmp_path, monkeypatch, invalidation):
    import api.server as server

    store = UsageStore(str(tmp_path / f"device-replay-{invalidation}.db"))
    owner_id = f"device-owner-{invalidation}"
    organization_id = store.ensure_organization(owner_id)["id"]
    with store._conn() as db:
        db.execute(
            "ALTER TABLE organization_members "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    device_code = ("r" if invalidation == "revoked_key" else "d") * 48
    device_hash = hash_key(device_code)
    raw_key = f"bvt_device_{invalidation}"
    store.create_device_request(
        device_hash,
        (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    assert store.approve_device_request(
        device_hash, owner_id, hash_key(raw_key), "encrypted-device-key",
        organization_id=organization_id,
    )
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_decrypt", lambda *_args, **_kwargs: raw_key)
    client = TestClient(server.app)

    first = client.post(
        "/v1/device-auth/token", json={"device_code": device_code},
        headers={"X-Request-ID": f"device-first-{invalidation}"},
    )
    assert first.status_code == 200
    assert first.json() == {"api_key": raw_key}

    with store._conn() as db:
        if invalidation == "revoked_key":
            db.execute(
                "UPDATE api_keys SET revoked_at=? WHERE key_hash=?",
                (datetime.now(timezone.utc).isoformat(), hash_key(raw_key)),
            )
        else:
            db.execute(
                "UPDATE organization_members SET status='disabled' "
                "WHERE organization_id=? AND user_id=?",
                (organization_id, owner_id),
            )

    replay = client.post(
        "/v1/device-auth/token", json={"device_code": device_code},
        headers={"X-Request-ID": f"device-replay-{invalidation}"},
    )
    assert replay.status_code == 503
    assert replay.headers["retry-after"] == "1"
    assert replay.json() == {"detail": "Device authorization unavailable"}
    assert raw_key not in replay.text
    assert store.get_device_request(device_hash) is None
    with store._conn() as db:
        receipt = db.execute(
            "SELECT encrypted_key,quarantined_at FROM bvx_device_consumption_receipts "
            "WHERE device_hash=?", (device_hash,),
        ).fetchone()
        denied = db.execute(
            "SELECT action,outcome FROM audit_events "
            "WHERE request_id=? ORDER BY id DESC LIMIT 1",
            (replay.headers["x-request-id"],),
        ).fetchone()
    assert receipt["encrypted_key"] == "" and receipt["quarantined_at"]
    assert tuple(denied) == ("device_key.consume.denied", "denied")


def test_api_keys_are_managed_only_by_human_sessions(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "keys.db"))
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    client = TestClient(server.app)

    monkeypatch.setattr(server, "_dashboard_user", lambda request:
                        "user-1" if request.headers.get("authorization") == "Bearer session" else "")
    account_key = client.post("/v1/keys", headers={"Authorization": "Bearer session"},
                              json={"name": "company backend"})
    assert account_key.status_code == 200
    raw_account_key = account_key.json()["api_key"]

    assert client.post("/v1/keys", headers={"X-Brevitas-Key": raw_account_key},
                       json={"name": "forbidden child"}).status_code == 401
    listed = client.get("/v1/keys", headers={"Authorization": "Bearer session"})
    fingerprint = hash_key(raw_account_key)[:16]
    listed_key = next(key for key in listed.json()["keys"] if key["fingerprint"] == fingerprint)
    key_id = listed_key["id"]
    assert hash_key(raw_account_key) not in str(listed.json())
    assert client.delete(f"/v1/keys/{key_id}",
                         headers={"Authorization": "Bearer session"}).status_code == 200
    assert client.post("/v1/keys", json={"name": "anonymous"}).status_code == 401


def test_repo_registration_and_admin_key_inventory_are_tenant_safe(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "repos.db"))
    raw_key = "bvt_repo_key"
    key_hash = hash_key(raw_key)
    store.create_key(key_hash, "CLI key", owner_id="user-1")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    client = TestClient(server.app)

    registered = client.post("/v1/repositories", headers={"X-Brevitas-Key": raw_key},
                             json={"repo": "/private/customer/checkout.git", "source": "bvx"})
    assert registered.status_code == 200
    assert registered.json()["repo"] == "checkout"
    store.record_usage(key_hash, 100, 80, owner_id="user-1", repo="runtime-repo")

    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "regular-user", "app_metadata": {}})
    assert client.get("/v1/admin/keys").status_code == 403
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "admin-user", "app_metadata": {"brevitas_admin": True}})
    response = client.get("/v1/admin/keys", headers={
        "Authorization": BEARER + " test-admin-session"})
    assert response.status_code == 200
    inventory = response.json()
    assert inventory["total_keys"] == 1
    assert inventory["total_repositories"] == 2
    assert inventory["keys"][0]["key_name"] == "CLI key"
    assert {repo["name"] for repo in inventory["keys"][0]["repositories"]} == {
        "checkout", "runtime-repo"}
    assert raw_key not in response.text
    assert inventory["keys"][0]["key_id"] == key_hash[:12]


def test_usage_api_is_tenant_scoped_and_idempotent(tmp_path, monkeypatch):
    import api.server as server
    store = UsageStore(str(tmp_path / "api.db"))
    store.create_key(hash_key("bvt_test"), "test", owner_id="user-1")
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()
    body = {
        "provider": "openai", "model": "gpt-4o-mini", "operation": "responses",
        "baseline_tokens": 100, "compressed_tokens": 80,
        "fresh_input_tokens": 60, "cached_input_tokens": 20, "output_tokens": 10,
        "quality_score": .95, "request_id": "same", "project": "/private/work/backend-app",
        "strategy": "native_cache",
        "environment": "prod", "source": "worker", "client": "python-sdk",
        "call_site_id": "call_abc", "receipt_source": "sdk",
        "usage_raw": {"prompt": "must never be stored", "response": "also private"},
    }
    client = TestClient(server.app)
    headers = {"X-Brevitas-Key": "bvt_test"}
    first = client.post("/v1/usage", headers=headers, json=body)
    second = client.post("/v1/usage", headers=headers, json=body)
    assert first.status_code == 200
    assert first.json()["quality_status"] == "verified"
    assert second.json()["duplicate"] is True
    overview = client.get("/v1/stats", headers=headers).json()
    breakdown = client.get("/v1/stats/breakdown", headers=headers).json()["rows"]
    assert overview["total_calls"] == sum(row["calls"] for row in breakdown) == 1
    assert breakdown[0]["project"] == "backend-app"
    assert breakdown[0]["repo"] == "backend-app"
    assert breakdown[0]["source"] == "worker"
    assert breakdown[0]["client"] == "python-sdk"
    assert "must never be stored" not in repr(store._rows(hash_key("bvt_test")))
    assert "/private/work" not in repr(store._rows(hash_key("bvt_test")))
    store.create_key(hash_key("bvt_other"), "other", owner_id="user-2")
    store.record_usage(hash_key("bvt_other"), 50, 40, project="other-app", source="api")
    assert client.get("/v1/stats", headers=headers).json()["total_calls"] == 1
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "user-2", "app_metadata": {}})
    assert client.get("/v1/admin/stats").status_code == 403
    assert client.get("/v1/admin/stats", headers={
        "X-Brevitas-Admin": "legacy-static-token"}).status_code == 403
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "admin-user", "app_metadata": {"brevitas_admin": True}})
    admin = client.get("/v1/admin/stats", headers={
        "Authorization": "Bearer " + "test-admin-session"})
    assert admin.status_code == 200
    assert admin.json()["total_calls"] == 2


def test_forged_proxy_prefixed_id_cannot_suppress_the_billable_receipt(
        tmp_path, monkeypatch):
    """Negative control for the reserved billing-id namespace (critical finding).

    The streaming paths yield the provider response id to the caller in the
    first SSE chunk BEFORE the receipt is recorded in the generator's finally,
    so an attacker can read `proxy:<provider id>` out of its own in-flight
    stream and pre-insert an analytics row under it via POST /v1/usage. If that
    row occupies the (key_hash, request_id) slot, the authoritative receipt is
    silently dropped as a duplicate: full optimization, zero billed. Two
    independent guards close it — the /v1/usage intake rewrites any
    caller-supplied id claiming the reserved prefix into `client:`, and the
    dedupe probe is scoped to rows of the same authority.
    """
    import api.server as server

    store = UsageStore(str(tmp_path / "forged.db"))
    raw_key = "bvt_forged_prefix"
    store.create_key(hash_key(raw_key), "forged", owner_id="customer-forged")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._seq_streams.clear()
    client = TestClient(server.app)

    forged = "proxy:chatcmpl-XYZ"
    sdk = {"provider": "openai", "model": "gpt-4o-mini", "baseline_tokens": 100,
           "compressed_tokens": 80, "request_id": forged, "strategy": "native_cache"}
    first = client.post("/v1/usage", headers={"X-Brevitas-Key": raw_key}, json=sdk)
    assert first.status_code == 200
    assert first.json().get("duplicate") is not True

    # Guard 1: the forged id was quarantined out of the reserved namespace, and
    # the caller's retry idempotency still works under the rewritten id.
    stored = [row["request_id"] for row in store._rows(hash_key(raw_key))]
    assert stored == ["client:chatcmpl-XYZ"]
    retry = client.post("/v1/usage", headers={"X-Brevitas-Key": raw_key}, json=sdk)
    assert retry.json()["duplicate"] is True

    # The hosted authoritative receipt under the very same id must still record.
    server._hosted_proxy_receipt(raw_key, {
        "provider": "openai", "model": "gpt-4o-mini",
        "baseline_tokens": 100, "compressed_tokens": 80,
        "fresh_input_tokens": 60, "cached_input_tokens": 20, "output_tokens": 10,
        "request_id": forged, "strategy": "passthrough",
        "receipt_source": "proxy", "receipt_available": True,
    })
    authoritative = [row for row in store._rows(hash_key(raw_key))
                     if row["authoritative"]]
    assert [row["request_id"] for row in authoritative] == [forged]

    # Guard 2 (authority-scoped dedupe) also protects the mirror image: the
    # authoritative row must not swallow a later SDK analytics report that
    # arrives under an id sharing the same suffix.
    assert len(store._rows(hash_key(raw_key))) == 2


@pytest.mark.parametrize("label,value", [
    ("pipeline", "P" * 200),          # over max_length=128
    ("environment", "E" * 100),       # over max_length=64
    ("agent", "checkout\x01worker"),  # control character
])
def test_an_unusable_tracking_label_cannot_suppress_the_billable_receipt(
        tmp_path, monkeypatch, label, value):
    """A cosmetic label must never decide whether a call is metered.

    The tracking labels ride the receipt payload, and the API validates the whole
    payload in one pass. A label over its max_length — or carrying a control
    character, which _safe_metadata rejects outright — raised ValidationError
    inside the hosted bridge's try block, which swallowed it and returned without
    writing a row. That handed any caller a metering kill switch cheaper than the
    request_id pin it replaced: it needs no knowledge of a prior id, works on
    every request including ~100%-savings cache hits, and leaves no duplicate
    marker behind. It also misfired by accident on a customer whose pipeline name
    ran long, silently flatlining their savings dashboard.
    """
    import api.server as server

    store = UsageStore(str(tmp_path / f"label-{label}.db"))
    raw_key = "bvt_label_suppression"
    store.create_key(hash_key(raw_key), "labels", owner_id="customer-labels")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()

    server._hosted_proxy_receipt(raw_key, {
        "provider": "openai", "model": "gpt-4o-mini",
        "baseline_tokens": 100, "compressed_tokens": 80,
        "fresh_input_tokens": 60, "cached_input_tokens": 20, "output_tokens": 10,
        "request_id": "proxy:chatcmpl-labelled", "strategy": "passthrough",
        "receipt_source": "proxy", "receipt_available": True,
        label: value,
    })

    # The money is recorded; only the offending label is dropped.
    rows = [row for row in store._rows(hash_key(raw_key)) if row["authoritative"]]
    assert [row["request_id"] for row in rows] == ["proxy:chatcmpl-labelled"]
    assert rows[0]["baseline_tokens"] == 100


def test_proxy_label_parsing_clamps_every_value_to_the_schema(monkeypatch):
    """The SDK-side half: labels are clamped before they ever reach the receipt."""
    from brevitas.proxy import parse_brevitas_headers

    monkeypatch.delenv("BREVITAS_PROJECT", raising=False)
    monkeypatch.delenv("BREVITAS_ENVIRONMENT", raising=False)
    labels = parse_brevitas_headers({
        "x-brevitas-pipeline": "P" * 500,
        "x-brevitas-environment": "E" * 500,
        "x-brevitas-agent": "checkout\x01worker",
        "x-brevitas-project": "billing",
    })
    assert len(labels["pipeline"]) == 128
    assert len(labels["environment"]) == 64
    assert labels["agent"] == "checkoutworker"
    # Whatever comes back must validate, or the receipt is lost downstream.
    from api.server import UsageReportRequest
    UsageReportRequest.model_validate(
        {"baseline_tokens": 1, "compressed_tokens": 1, **labels})


def test_a_label_can_never_override_a_server_minted_receipt_field():
    """Labels are splatted first so request_id/receipt_source cannot be shadowed."""
    import inspect as _inspect
    from brevitas import proxy as _proxy

    source = _inspect.getsource(_proxy)
    for marker in ('"session_id": session.session_id, "receipt_source": "proxy",\n',
                   '"session_id": session.session_id, "receipt_source": "proxy"}'):
        assert marker in source
    # A trailing **labels splat would re-open the original critical finding.
    assert '"receipt_source": "proxy", **labels' not in source


def test_quality_stream_and_reset_are_scoped_to_end_customer(tmp_path, monkeypatch):
    import api.server as server
    from brevitas.identity import tenant_key
    from token_efficiency_model.quality import gate

    store = UsageStore(str(tmp_path / "customer-quality.db"))
    raw_key = "bvt_shared_service"
    organization = store.ensure_organization("owner-1", "Shared service organization")
    service = store.ensure_service_account(
        organization["id"], "test", "owner-1")
    store.create_key(
        hash_key(raw_key), "shared", owner_id="owner-1",
        organization_id=organization["id"], service_account_id=service["id"],
        key_type="organization_service", environment="test",
        # quality:manage is now required by POST /v1/quality/stream/reset: the reset
        # re-enables the risky levers and erases accumulated mSPRT evidence, so it is a
        # mutation and no longer rides on the read scope every member key carries. This
        # test is about per-customer scoping of the reset, not about who may call it.
        scopes=["proxy:invoke", "usage:read_own", "customer:route",
                "customer:auto_provision", "quality:manage"],
    )
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setenv("BREVITAS_RETRIEVAL_ENABLED", "1")
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    server._seq_streams.clear()
    client = TestClient(server.app)
    headers_a = {"X-Brevitas-Key": raw_key, "X-Brevitas-Customer-Id": "customer-a"}
    headers_b = {"X-Brevitas-Key": raw_key, "X-Brevitas-Customer-Id": "customer-b"}
    key_a = tenant_key(raw_key, "customer-a")
    key_b = tenant_key(raw_key, "customer-b")

    gate.trip_lever("retrieval", key=key_a)
    gate.trip_lever("retrieval", key=key_b)
    try:
        assert client.get("/v1/quality/stream", headers=headers_a).status_code == 200
        assert client.get("/v1/quality/stream", headers=headers_b).status_code == 200
        assert {key for key, _value in server._seq_streams.items()} == {key_a, key_b}

        assert client.post("/v1/quality/stream/reset", headers=headers_a).status_code == 200
        assert key_a not in server._seq_streams and key_b in server._seq_streams
        assert gate.lever_allowed("retrieval", key=key_a) is True
        assert gate.lever_allowed("retrieval", key=key_b) is False
    finally:
        gate.reset_all_levers(key=key_a)
        gate.reset_all_levers(key=key_b)


def test_usage_receipt_alignment_and_method_based_verification(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "accounting.db"))
    store.create_key(hash_key("bvt_accounting"), "accounting")
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()
    client = TestClient(server.app)
    headers = {"X-Brevitas-Key": "bvt_accounting"}

    # The optimizer saw 100 -> 80 message tokens, while the authoritative
    # provider receipt includes another 920 system/tool/cache tokens. The API
    # must preserve only the 20-token delta and report 1020 -> 1000.
    base = {
        "provider": "openai", "model": "gpt-4o-mini",
        "baseline_tokens": 100, "compressed_tokens": 80,
        "fresh_input_tokens": 600, "cached_input_tokens": 400,
        "output_tokens": 10, "receipt_available": True,
    }
    safe = client.post("/v1/usage", headers=headers, json={
        **base, "request_id": "safe", "strategy": "native_cache",
        "cache_attributable": False,
    })
    assert safe.status_code == 200
    assert safe.json()["baseline_tokens"] == 1020
    assert safe.json()["compressed_tokens"] == 1000
    assert safe.json()["tokens_saved"] == 20
    assert safe.json()["quality_status"] == "verified"
    # OpenAI's automatic cache discount is not credited to Brevitas; only the
    # 20 removed tokens are measured (20 * $0.15 / 1M).
    assert safe.json()["measured_savings_usd"] == 0.000003

    retrieval = client.post("/v1/usage", headers=headers, json={
        **base, "request_id": "retrieval", "strategy": "retrieve",
        "quality_score": .99,
    })
    assert retrieval.json()["quality_status"] == "unverified"
    assert retrieval.json()["verified_savings_usd"] == 0

    paired = client.post("/v1/usage", headers=headers, json={
        **base, "request_id": "paired", "strategy": "retrieve",
        "quality_score": .72, "quality_verified": True,
    })
    assert paired.json()["quality_status"] == "verified"
    # Caller-reported telemetry can be analyzed but is never authoritative billing input.
    assert paired.json()["verified_savings_usd"] == 0
    assert paired.json()["brevitas_fee_usd"] == 0

    # A provider/local tokenizer mismatch with no transformation is zero
    # savings, not a fabricated win or loss.
    unchanged = client.post("/v1/usage", headers=headers, json={
        **base, "request_id": "unchanged", "strategy": "passthrough",
        "baseline_tokens": 100, "compressed_tokens": 100,
    })
    assert unchanged.json()["baseline_tokens"] == 1000
    assert unchanged.json()["compressed_tokens"] == 1000
    assert unchanged.json()["measured_savings_usd"] == 0

    # An attributable 1-hour cache write can be a real temporary loss. Keep it
    # signed for auditability, but never verify it or charge a savings fee.
    write = client.post("/v1/usage", headers=headers, json={
        "provider": "anthropic", "model": "claude-sonnet-4-6",
        "baseline_tokens": 100, "compressed_tokens": 100,
        "cache_write_tokens": 100, "cache_write_1h_tokens": 100,
        "output_tokens": 0, "receipt_available": True,
        "cache_attributable": True, "request_id": "cache-write",
        "strategy": "cache_only",
    })
    assert write.json()["measured_savings_usd"] == -0.0003
    assert write.json()["verified_savings_usd"] == 0
    assert write.json()["brevitas_fee_usd"] == 0


def test_authoritative_billing_bills_exact_replays_not_fuzzy_reuse_or_unknown_models(
        tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "billing-quality-boundary.db"))
    store.create_key(hash_key("bvt_billing_boundary"), "billing-boundary")
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()

    exact_replay = server._record_usage_report(
        hash_key("bvt_billing_boundary"),
        server.UsageReportRequest(
            provider="openai", model="gpt-4o-mini",
            baseline_tokens=100, compressed_tokens=0,
            fresh_input_tokens=0, output_tokens=0,
            baseline_output_tokens=20, strategy="exact_cache",
            quality_verified=True, request_id="exact-cache",
        ),
        authoritative=True,
    )
    semantic_reuse = server._record_usage_report(
        hash_key("bvt_billing_boundary"),
        server.UsageReportRequest(
            provider="openai", model="gpt-4o-mini",
            baseline_tokens=100, compressed_tokens=0,
            fresh_input_tokens=0, output_tokens=0,
            baseline_output_tokens=20, strategy="semantic_cache",
            quality_verified=True, request_id="semantic-cache",
        ),
        authoritative=True,
    )
    unknown_model = server._record_usage_report(
        hash_key("bvt_billing_boundary"),
        server.UsageReportRequest(
            provider="openai", model="future-model-without-pricing",
            baseline_tokens=100, compressed_tokens=80,
            fresh_input_tokens=80, output_tokens=0,
            strategy="native_cache", request_id="unknown-price",
        ),
        authoritative=True,
    )
    native_cache = server._record_usage_report(
        hash_key("bvt_billing_boundary"),
        server.UsageReportRequest(
            provider="openai", model="gpt-4o-mini",
            baseline_tokens=100, compressed_tokens=80,
            fresh_input_tokens=80, output_tokens=0,
            strategy="native_cache", request_id="native-cache",
        ),
        authoritative=True,
    )

    # An exact-hash replay serves provably identical bytes: (100 input +
    # 20 output tokens avoided) is billable savings under Phase 4.
    assert exact_replay["quality_status"] == "verified"
    assert exact_replay["verified_savings_usd"] == 0.000027
    assert exact_replay["brevitas_fee_usd"] == 0.00000675
    # Fuzzy reuse never proves answer equivalence, so it stays non-billable.
    assert semantic_reuse["verified_savings_usd"] == 0
    assert semantic_reuse["brevitas_fee_usd"] == 0
    assert unknown_model["pricing_status"] == "unpriced"
    assert unknown_model["verified_savings_usd"] == 0
    assert native_cache["pricing_status"] == "priced"
    assert native_cache["verified_savings_usd"] > 0


def test_cache_attribution_and_warming_billing_boundaries(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "cache-attribution-boundary.db"))
    store.create_key(hash_key("bvt_cache_attribution"), "cache-attribution")
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()

    base = dict(
        provider="openai", model="gpt-4o-mini",
        baseline_tokens=100, compressed_tokens=100,
        fresh_input_tokens=0, cached_input_tokens=100, output_tokens=0,
    )
    attributable = server._record_usage_report(
        hash_key("bvt_cache_attribution"),
        server.UsageReportRequest(**base, cache_attributable=True,
                                  request_id="brevitas-owned-read"),
        authoritative=True,
    )
    caller_owned = server._record_usage_report(
        hash_key("bvt_cache_attribution"),
        server.UsageReportRequest(**base, cache_attributable=False,
                                  request_id="caller-owned-read"),
        authoritative=True,
    )
    warm_ping = server._record_usage_report(
        hash_key("bvt_cache_attribution"),
        server.UsageReportRequest(**base, cache_attributable=True,
                                  strategy="cache_warm",
                                  request_id="warm-ping"),
        authoritative=True,
    )

    # A native read on Brevitas-owned breakpoints is byte-preserving even with
    # no strategy label; the discount (100 * (0.15 - 0.075) / 1M) is billable.
    assert attributable["quality_status"] == "verified"
    assert attributable["native_cache_discount_usd"] == 0.0000075
    assert attributable["verified_savings_usd"] == 0.0000075
    # Caller-owned markers: the discount stays measured for analytics but never
    # enters the billable number.
    assert caller_owned["native_cache_discount_usd"] == 0.0000075
    assert caller_owned["verified_savings_usd"] == 0
    assert caller_owned["brevitas_fee_usd"] == 0
    # Warming pings are Brevitas-initiated spend — never savings.
    assert warm_ping["verified_savings_usd"] == 0
    assert warm_ping["brevitas_fee_usd"] == 0


def test_billable_savings_are_signed_per_row_so_a_period_can_net_negative(
    tmp_path, monkeypatch
):
    """A cold cache write costs MORE than the baseline; that loss must survive.

    verified_savings_usd is the quantity the period settlement evidence sums
    (supabase/migrations/202607280008_billing_halting_conditions.sql, and
    202607280007's signed numeric(24,10) column). If it were floored at zero
    per row, the period sum could never go negative, the period-level
    greatest(..., 0) could never bind, and a write-heavy week whose true net is
    negative would still bill every positive row in it. The floor belongs at
    the period level and nowhere else.
    """
    import api.server as server

    store = UsageStore(str(tmp_path / "signed-netting.db"))
    store.create_key(hash_key("bvt_signed_net"), "signed-net")
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()

    base = dict(
        provider="anthropic", model="claude-sonnet-5",
        baseline_tokens=10000, compressed_tokens=10000,
        fresh_input_tokens=0, output_tokens=0, baseline_output_tokens=0,
        strategy="native_cache", cache_attributable=True,
    )
    # Cold write: 10k cache_write tokens priced at 3.75/Mtok against a 10k
    # input-token baseline priced at 3.00/Mtok -> -0.0075.
    cold_write = server._record_usage_report(
        hash_key("bvt_signed_net"),
        server.UsageReportRequest(**base, cached_input_tokens=0,
                                  cache_write_tokens=10000,
                                  request_id="cold-write"),
        authoritative=True,
    )
    # Warm read on the same prefix: 10k cached tokens at 0.30/Mtok -> +0.027.
    warm_read = server._record_usage_report(
        hash_key("bvt_signed_net"),
        server.UsageReportRequest(**base, cached_input_tokens=10000,
                                  cache_write_tokens=0,
                                  request_id="warm-read"),
        authoritative=True,
    )

    # The write is fully billing-eligible — authoritative, byte-preserving,
    # verified, priced, not a warming ping — and its savings are negative.
    assert cold_write["quality_status"] == "verified"
    assert cold_write["pricing_status"] == "priced"
    assert cold_write["measured_savings_usd"] == -0.0075
    assert cold_write["verified_savings_usd"] == -0.0075
    # A negative fee is unrepresentable; netting happens over the period, not
    # as a per-row credit.
    assert cold_write["brevitas_fee_usd"] == 0
    assert warm_read["verified_savings_usd"] == 0.027

    import sqlite3

    with sqlite3.connect(str(tmp_path / "signed-netting.db")) as db:
        rows = db.execute(
            "SELECT measured_savings_usd, verified_savings_usd FROM usage_log "
            "WHERE authoritative = 1 AND pricing_status = 'priced'"
        ).fetchall()
    assert len(rows) == 2
    # What the period evidence function aggregates must equal the customer's
    # true signed net, not the sum of the winning rows only.
    signed_net = round(sum(float(row[1] or 0) for row in rows), 10)
    assert signed_net == round(sum(float(row[0] or 0) for row in rows), 10)
    assert signed_net == 0.0195
    assert round(signed_net * server.BREVITAS_FEE_RATE, 10) == 0.004875


def test_repo_client_model_breakdown_reconciles(tmp_path):
    store = UsageStore(str(tmp_path / "reconcile.db"))
    rows = [
        ("repo-a", "codex", "openai", "gpt-4o-mini", 100, 80, .10),
        ("repo-a", "codex", "deepseek", "deepseek-chat", 200, 150, .20),
        ("repo-a", "claude-code", "anthropic", "claude-sonnet-4-6", 300, 250, .30),
        ("repo-b", "backend", "openai", "gpt-4o", 400, 300, .40),
    ]
    for repo, client, provider, model, baseline, optimized, usd in rows:
        store.record_usage("key", baseline, optimized, repo=repo, project=repo,
                           client=client, source=client, provider=provider, model=model,
                           measured_savings_usd=usd, verified_savings_usd=usd)

    breakdown = store.get_breakdown("key")
    totals = store.get_stats("key")
    assert sum(row["calls"] for row in breakdown) == totals["total_calls"]
    assert sum(row["tokens_saved"] for row in breakdown) == totals["total_tokens_saved"]
    assert round(sum(row["measured_savings_usd"] for row in breakdown), 8) == totals["total_measured_savings_usd"]
    assert {(row["repo"], row["client"], row["provider"], row["model"]) for row in breakdown} == {
        (repo, client, provider, model) for repo, client, provider, model, *_ in rows
    }


def test_admin_financial_report_is_filtered_paginated_and_protected(tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "admin.db"))
    store.record_usage("a", 100, 70, owner_id="user-a", project="alpha", client="codex",
                       provider="openai", model="gpt-4o-mini", baseline_cost_usd=.20,
                       actual_cost_usd=.14, measured_savings_usd=.06,
                       verified_savings_usd=.05, brevitas_fee_usd=.005)
    store.record_usage("b", 200, 150, owner_id="user-b", project="beta", client="backend",
                       provider="anthropic", model="claude", baseline_cost_usd=.40,
                       actual_cost_usd=.30, measured_savings_usd=.10,
                       verified_savings_usd=.08, brevitas_fee_usd=.008)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "regular-user", "app_metadata": {}})
    client = TestClient(server.app)

    assert client.get("/v1/admin/stats/breakdown").status_code == 403
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "admin-user", "app_metadata": {"role": "brevitas_admin"}})
    response = client.get(
        "/v1/admin/stats/breakdown?range=all&account=user-a&limit=1",
        headers={"Authorization": "Bearer " + "test-admin-session"},
    )
    assert response.status_code == 200
    report = response.json()
    assert report["pagination"] == {
        "total": 1, "limit": 1, "next_cursor": "", "has_more": False,
    }
    assert report["rows"][0]["account_id"] == "user-a"
    assert report["rows"][0]["actual_cost_usd"] == .14
    assert report["totals"]["total_actual_cost_usd"] == .14
    assert client.get("/v1/admin/stats/breakdown?range=365d",
                      headers={"Authorization": "Bearer " + "test-admin-session"}).status_code == 422
    billing = client.get("/v1/admin/billing?range=all", headers={
        "Authorization": "Bearer " + "test-admin-session"})
    assert billing.status_code == 200
    # The per-row fee sum is published as gross, un-netted evidence. It is NOT a
    # settlement figure: per-row fees were floored at zero when written, so a
    # period whose true net is negative still bills every positive row in it, and
    # no warm-ping deduction is applied. amount_owed_usd keeps emitting the same
    # value only until the dashboard reads the honest field (the two deploy
    # separately, and the dashboard renders a missing field as "$0.00").
    assert billing.json()["gross_positive_row_fees_usd"] == .013
    assert billing.json()["amount_owed_usd"] == .013
    assert billing.json()["basis"] == "gross_positive_row_fees_unnetted"
    assert billing.json()["netted"] is False
    assert billing.json()["warm_spend_deducted"] is False
    assert billing.json()["settlement_pending"] is True
    assert billing.json()["payment_status_tracked"] is False
    assert {account["account_id"] for account in billing.json()["accounts"]} == {
        "user-a", "user-b"}
    assert [account["gross_positive_row_fees_usd"] == account["amount_owed_usd"]
            for account in billing.json()["accounts"]] == [True, True]


def test_admin_posthog_summary_keeps_personal_key_server_side(monkeypatch):
    import api.server as server

    personal_key = "phx_" + "private"
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "admin-user", "app_metadata": {"brevitas_admin": True}})
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "42")
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", personal_key)
    server._POSTHOG_CACHE.clear()
    calls = []

    class Response:
        def __init__(self, results):
            self._results = results

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": self._results}

    # Seven columns, matching the overview query in _posthog_admin_summary:
    # pageviews, visitors, sessions, signup_started, signup_submitted, then the
    # two the signup-funnel change added -- signup_attempts (raw button presses,
    # so it is >= signup_started) and signup_failures. A five-element row made
    # the route raise IndexError on totals[5] rather than fail an assertion.
    results = [[[120, 80, 90, 12, 8, 14, 2]], [[45.5, 32.1]],
               [["2026-07-15", 80, 90, 120]]]

    def post(url, *, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        return Response(results[len(calls) - 1])

    monkeypatch.setattr(server._requests, "post", post)
    response = TestClient(server.app).get(
        "/v1/admin/analytics?range=30d",
        headers={"Authorization": "Bearer " + "test-admin-session"},
    )
    assert response.status_code == 200
    assert response.json()["visitors"] == 80
    assert response.json()["avg_session_duration_seconds"] == 45.5
    # Pin the funnel columns to their own positions, so a future reordering of
    # the SELECT list fails here instead of silently relabelling the numbers.
    assert response.json()["signup_started"] == 12
    assert response.json()["signup_submitted"] == 8
    assert response.json()["signup_attempts"] == 14
    assert response.json()["signup_failures"] == 2
    assert all(call[1]["Authorization"] == f"Bearer {personal_key}" for call in calls)
    assert personal_key not in response.text


def test_admin_posthog_summary_reports_rejected_credentials(monkeypatch):
    import api.server as server

    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "admin-user", "app_metadata": {"brevitas_admin": True}})
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "42")
    monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_rejected")
    server._POSTHOG_CACHE.clear()

    class Response:
        status_code = 403

        def raise_for_status(self):
            raise AssertionError("credential rejection should be handled first")

        def json(self):
            return {"detail": "invalid personal API key"}

    def post(url, *, headers, json, timeout):
        return Response()

    monkeypatch.setattr(server._requests, "post", post)
    response = TestClient(server.app).get(
        "/v1/admin/analytics?range=30d",
        headers={"Authorization": "Bearer " + "test-admin-session"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "PostHog reporting credentials were rejected; "
        "update POSTHOG_PERSONAL_API_KEY"
    )


def _mock_client(monkeypatch, handler):
    import brevitas.proxy as proxy
    real = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient",
                        lambda *args, **kwargs: real(transport=httpx.MockTransport(handler)))
    proxy._cache_init_done = True
    proxy._cache_singleton = None
    return proxy


def test_streaming_chat_and_responses_are_byte_preserving_and_metered(monkeypatch):
    events = []
    forwarded_responses = []
    chat_bytes = (b'data: {"id":"chat_1","choices":[],"usage":{"prompt_tokens":30,'
                  b'"prompt_tokens_details":{"cached_tokens":10},"completion_tokens":5}}\n\n'
                  b'data: [DONE]\n\n')
    responses_bytes = (b'data: {"type":"response.completed","response":{"id":"resp_1",'
                       b'"usage":{"input_tokens":40,"input_tokens_details":{"cached_tokens":15},'
                       b'"output_tokens":6}}}\n\ndata: [DONE]\n\n')

    def handler(request):
        if request.url.path.endswith("/responses"):
            forwarded_responses.append(request.content)
        content = responses_bytes if request.url.path.endswith("/responses") else chat_bytes
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    proxy = _mock_client(monkeypatch, handler)
    proxy.set_usage_reporter(lambda key, payload: events.append((key, payload)))
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    client = TestClient(proxy.proxy_app)
    headers = {"Authorization": f"{BEARER} provider-key", "X-Brevitas-Key": "bvt_customer",
               "X-Brevitas-Project": "app", "X-Brevitas-Client": "backend"}
    chat = client.post("/v1/chat/completions", headers=headers,
                       json={"model": "gpt-4o-mini", "stream": True,
                             "messages": [{"role": "user", "content": "private prompt"}]})
    deepseek = client.post("/v1/chat/completions", headers=headers,
                           json={"model": "deepseek-chat", "stream": True,
                                 "messages": [{"role": "user", "content": "private prompt"}]})
    responses_request = (b'{ "model" : "gpt-4o-mini", "stream" : true, '
                         b'"input" : "another private prompt" }')
    responses = client.post("/v1/responses",
                            headers={**headers, "Content-Type": "application/json"},
                            content=responses_request)
    assert chat.content == chat_bytes
    assert deepseek.content == chat_bytes
    assert responses.content == responses_bytes
    assert forwarded_responses == [responses_request]
    assert [event[1]["operation"] for event in events] == ["chat.completions", "chat.completions", "responses"]
    assert events[0][1]["cached_input_tokens"] == 10
    assert events[1][1]["provider"] == "deepseek"
    assert events[2][1]["cached_input_tokens"] == 15
    assert all("private prompt" not in repr(payload) for _, payload in events)
    proxy.set_usage_reporter(None)


def test_reporting_failure_never_breaks_provider_response(monkeypatch):
    raw = b'{"id":"x","choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}'
    proxy = _mock_client(monkeypatch, lambda request: httpx.Response(
        200, content=raw, headers={"content-type": "application/json"}))
    proxy.set_usage_reporter(lambda key, payload: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    response = TestClient(proxy.proxy_app).post("/v1/chat/completions",
        headers={"Authorization": f"{BEARER} provider", "X-Brevitas-Key": "bvt"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 200
    assert response.content == raw
    proxy.set_usage_reporter(None)


def test_anthropic_and_deepseek_nonstream_receipts(monkeypatch):
    events = []
    anthropic_raw = b'{"id":"msg_1","content":[{"type":"text","text":"ok"}],"usage":{"input_tokens":8,"cache_read_input_tokens":3,"cache_creation_input_tokens":2,"output_tokens":4}}'
    deepseek_raw = b'{"id":"ds_1","choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":12,"prompt_cache_hit_tokens":5,"completion_tokens":3}}'

    def handler(request):
        return httpx.Response(200, content=anthropic_raw if "anthropic" in request.url.host else deepseek_raw,
                              headers={"content-type": "application/json"})

    proxy = _mock_client(monkeypatch, handler)
    proxy.set_usage_reporter(lambda key, payload: events.append(payload))
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    client = TestClient(proxy.proxy_app)
    common = {"X-Brevitas-Key": "bvt", "X-Brevitas-Project": "app"}
    anthropic = client.post("/v1/messages", headers={**common, "X-Api-Key": "ant"},
        json={"model": "claude-sonnet-4-6", "max_tokens": 10,
              "messages": [{"role": "user", "content": "hello"}]})
    deepseek = client.post("/v1/chat/completions", headers={**common, "Authorization": f"{BEARER} ds"},
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "hello"}]})
    assert anthropic.content == anthropic_raw
    assert deepseek.content == deepseek_raw
    assert [(e["provider"], e["cached_input_tokens"]) for e in events] == [
        ("anthropic", 3), ("deepseek", 5)]
    proxy.set_usage_reporter(None)


def test_anthropic_stream_and_openai_nonstream_receipts(monkeypatch):
    events = []
    stream_raw = (b'data: {"type":"message_start","message":{"id":"msg_stream",'
                  b'"usage":{"input_tokens":9,"cache_read_input_tokens":4}}}\n\n'
                  b'data: {"type":"message_delta","usage":{"output_tokens":3}}\n\n')
    chat_raw = (b'{"id":"chat_nonstream","choices":[{"message":{"content":"ok"}}],'
                b'"usage":{"prompt_tokens":20,"prompt_tokens_details":{"cached_tokens":7},'
                b'"completion_tokens":2}}')

    def handler(request):
        payload = stream_raw if "anthropic" in request.url.host else chat_raw
        media = "text/event-stream" if "anthropic" in request.url.host else "application/json"
        return httpx.Response(200, content=payload, headers={"content-type": media})

    proxy = _mock_client(monkeypatch, handler)
    proxy.set_usage_reporter(lambda key, payload: events.append(payload))
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    client = TestClient(proxy.proxy_app)
    anthropic = client.post("/v1/messages", headers={"X-Api-Key": "ant"},
        json={"model": "claude-sonnet-4-6", "stream": True, "max_tokens": 10,
              "messages": [{"role": "user", "content": "hello"}]})
    chat = client.post("/v1/chat/completions", headers={"Authorization": f"{BEARER} openai"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]})
    assert anthropic.content == stream_raw
    assert chat.content == chat_raw
    assert [(event["operation"], event["cached_input_tokens"]) for event in events] == [
        ("messages", 4), ("chat.completions", 7)]
    proxy.set_usage_reporter(None)


def test_combined_hosted_proxy_writes_customer_dashboard_row(tmp_path, monkeypatch):
    import api.server as server
    import brevitas.proxy as proxy

    store = UsageStore(str(tmp_path / "hosted.db"))
    raw_key = "bvt_hosted_e2e"
    store.create_key(hash_key(raw_key), "e2e", owner_id="customer-e2e")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._seq_streams.clear()

    served = []

    def _respond(request):
        # A distinct provider response id per call, as every real provider gives.
        raw = (b'{"id":"resp_e2e_%d","output":[],"usage":{"input_tokens":32,'
               b'"input_tokens_details":{"cached_tokens":12},"output_tokens":4}}'
               % len(served))
        served.append(raw)
        return httpx.Response(200, content=raw,
                              headers={"content-type": "application/json"})

    real = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda *args, **kwargs: real(
        transport=httpx.MockTransport(_respond)))
    proxy._cache_init_done = True
    proxy._cache_singleton = None
    proxy.set_usage_reporter(server._hosted_proxy_receipt)
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    monkeypatch.setenv("BREVITAS_PROXY_RPM", "2")
    server._proxy_windows.clear()
    server._proxy_active.clear()

    client = TestClient(server.app)
    headers = {"X-Brevitas-Key": raw_key, "Authorization": f"{BEARER} provider-key",
               "X-Brevitas-Project": "backend-service", "X-Brevitas-Environment": "prod",
               "X-Brevitas-Client": "api-worker", "X-Brevitas-Request-Id": "e2e-1"}
    assert client.post("/v1/responses", headers={"Authorization": f"{BEARER} provider-key"},
                       json={"model": "gpt-4o-mini", "input": "x"}).status_code == 401
    response = client.post("/v1/responses", headers=headers,
        json={"model": "gpt-4o-mini", "input": "private input"})
    assert response.status_code == 200
    assert response.content == served[0]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/v1/responses", headers=headers,
        json={"model": "gpt-4o-mini", "input": "private input"}).status_code == 200
    assert client.post("/v1/responses", headers={**headers, "X-Brevitas-Request-Id": "e2e-2"},
        json={"model": "gpt-4o-mini", "input": "private input"}).status_code == 429
    breakdown = client.get("/v1/stats/breakdown",
                           headers={"X-Brevitas-Key": raw_key}).json()
    # BOTH billable calls are metered. This assertion used to read `== 1`: the
    # receipt's request_id was taken from X-Brevitas-Request-Id, which is the
    # billing dedupe key, so pinning one value suppressed every receipt after the
    # first while the proxy kept optimizing every call.
    assert breakdown["totals"]["total_calls"] == 2
    assert [(row["project"], row["source"], row["provider"], row["model"])
            for row in breakdown["rows"]] == [
                ("backend-service", "api-worker", "openai", "gpt-4o-mini")]
    metering_ids = [row["request_id"] for row in store._rows(hash_key(raw_key))]
    assert sorted(metering_ids) == ["proxy:resp_e2e_0", "proxy:resp_e2e_1"]
    assert "e2e-1" not in repr(metering_ids)
    assert "private input" not in repr(store._rows(hash_key(raw_key)))
    proxy.set_usage_reporter(None)


def test_pinned_client_request_id_cannot_suppress_hosted_metering(tmp_path, monkeypatch):
    """One constant caller header must not zero a tenant's metered savings.

    The metering id is minted server-side per request and namespaced `proxy:`, so
    it can neither be chosen by the caller nor collide with a caller-declared
    /v1/usage idempotency key. A cache hit — the row with ~100% savings and no
    provider response id at all — is covered too.
    """
    import api.server as server
    import brevitas.proxy as proxy

    store = UsageStore(str(tmp_path / "pinned.db"))
    raw_key = "bvt_pinned_id"
    store.create_key(hash_key(raw_key), "pinned", owner_id="customer-pinned")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._seq_streams.clear()

    calls = []

    def _respond(request):
        calls.append(request.url.host)
        return httpx.Response(200, headers={"content-type": "application/json"}, content=(
            b'{"id":"chat_%d","choices":[{"message":{"content":"ok"},'
            b'"finish_reason":"stop"}],"usage":{"prompt_tokens":40,'
            b'"prompt_tokens_details":{"cached_tokens":10},"completion_tokens":5}}'
            % len(calls)))

    real = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda *args, **kwargs: real(
        transport=httpx.MockTransport(_respond)))
    proxy._cache_init_done = True
    proxy._cache_singleton = None
    proxy.set_usage_reporter(server._hosted_proxy_receipt)
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    monkeypatch.setenv("BREVITAS_PROXY_RPM", "50")
    server._proxy_windows.clear()
    server._proxy_active.clear()

    client = TestClient(server.app)
    headers = {"X-Brevitas-Key": raw_key, "Authorization": f"{BEARER} provider-key",
               "X-Brevitas-Request-Id": "pinned", "X-Client-Request-Id": "pinned-too"}
    for _ in range(3):
        assert client.post("/v1/chat/completions", headers=headers, json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hello"}]}).status_code == 200

    metering_ids = [row["request_id"] for row in store._rows(hash_key(raw_key))]
    assert len(metering_ids) == len(set(metering_ids)) == 3
    assert all(mid.startswith("proxy:") for mid in metering_ids)
    assert "pinned" not in repr(metering_ids)

    # A caller-declared id on the non-authoritative /v1/usage path keeps working:
    # SDK retries legitimately want caller idempotency there.
    sdk = {"provider": "openai", "model": "gpt-4o-mini", "baseline_tokens": 10,
           "compressed_tokens": 8, "request_id": "pinned", "strategy": "native_cache"}
    assert client.post("/v1/usage", headers={"X-Brevitas-Key": raw_key},
                       json=sdk).status_code == 200
    assert client.post("/v1/usage", headers={"X-Brevitas-Key": raw_key},
                       json=sdk).json()["duplicate"] is True
    proxy.set_usage_reporter(None)


def test_billed_provider_label_follows_the_destination_host(tmp_path, monkeypatch):
    """x-brevitas-provider must not decouple the billed label from the real host.

    Declaring a reseller (no MODEL_PRICES rows -> pricing_status 'unpriced' ->
    zero verified savings -> zero fee) while routing to api.openai.com was free
    use of a paid product. The label now follows the resolved destination, on the
    completion path and on the cache-hit path.
    """
    import api.server as server
    import brevitas.proxy as proxy

    store = UsageStore(str(tmp_path / "label.db"))
    raw_key = "bvt_label"
    store.create_key(hash_key(raw_key), "label", owner_id="customer-label")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._seq_streams.clear()

    hosts = []

    def _respond(request):
        hosts.append(request.url.host)
        return httpx.Response(200, headers={"content-type": "application/json"}, content=(
            b'{"id":"chat_%d","choices":[{"message":{"content":"ok"},'
            b'"finish_reason":"stop"}],"usage":{"prompt_tokens":60,'
            b'"prompt_tokens_details":{"cached_tokens":40},"completion_tokens":5}}'
            % len(hosts)))

    real = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda *args, **kwargs: real(
        transport=httpx.MockTransport(_respond)))
    proxy._cache_init_done = True
    proxy._cache_singleton = None
    proxy.set_usage_reporter(server._hosted_proxy_receipt)
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    monkeypatch.setenv("BREVITAS_PROXY_RPM", "50")
    server._proxy_windows.clear()
    server._proxy_active.clear()

    client = TestClient(server.app)
    assert client.post("/v1/chat/completions", headers={
        "X-Brevitas-Key": raw_key, "Authorization": f"{BEARER} provider-key",
        "X-Brevitas-Provider": "groq", "X-Brevitas-Upstream": "https://api.openai.com",
    }, json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    ).status_code == 200
    assert hosts == ["api.openai.com"]
    row = store._rows(hash_key(raw_key))[0]
    assert row["provider"] == "openai"
    assert row["pricing_status"] == "priced"

    # Cache hits carry ~100% savings and never touch an upstream, so the label
    # has to be derived from the header pair up front.
    class _Hit:
        kind = "exact"
        similarity = 1.0
        prompt_tokens = 60
        completion_tokens = 5
        response = {"id": "cached", "choices": [
            {"message": {"content": "ok"}, "finish_reason": "stop"}]}

    class _Cache:
        def lookup(self, *args, **kwargs):
            return _Hit()

        def store(self, *args, **kwargs):
            return None

    monkeypatch.setattr(proxy, "_cache_for_request", lambda request: _Cache())
    assert client.post("/v1/chat/completions", headers={
        "X-Brevitas-Key": raw_key, "Authorization": f"{BEARER} provider-key",
        "X-Brevitas-Provider": "groq", "X-Brevitas-Upstream": "https://api.openai.com",
    }, json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    ).status_code == 200
    assert hosts == ["api.openai.com"]      # served from cache, no second call
    replay = [r for r in store._rows(hash_key(raw_key))
              if str(r["strategy"]).startswith("exact_cache")][0]
    assert replay["provider"] == "openai"
    assert replay["pricing_status"] == "priced"
    assert float(replay["verified_savings_usd"]) > 0
    proxy.set_usage_reporter(None)


def test_usage_rows_are_attributed_to_the_key_owning_organization(tmp_path, monkeypatch):
    """A receipt written through the normal authenticated path lands tenanted.

    `_usage_row` defaults organization_id to '' when a caller omits it, which
    used to turn a caller-side omission into an untenanted receipt. The key hash
    is required on every write path, so the store derives the tenant from
    api_keys.organization_id instead of silently writing ''.
    """
    import api.server as server

    store = UsageStore(str(tmp_path / "attribution.db"))
    organization = store.ensure_organization("owner-attr", "Attribution organization")
    raw_key = "bvt_attribution"
    store.create_key(hash_key(raw_key), "attribution", owner_id="owner-attr",
                     organization_id=organization["id"])
    # A key that belongs to no organization must stay untenanted: attribution is
    # derived, never invented.
    store.create_key(hash_key("bvt_orgless"), "orgless", owner_id="owner-orgless")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    server._seq_streams.clear()
    client = TestClient(server.app)

    reported = client.post("/v1/usage", headers={"X-Brevitas-Key": raw_key}, json={
        "provider": "openai", "model": "gpt-4o-mini",
        "baseline_tokens": 100, "compressed_tokens": 80,
        "request_id": "attributed-http", "strategy": "native_cache",
    })
    assert reported.status_code == 200

    # Direct store writes that omit the tenant are derived, not defaulted to ''.
    assert store.record_usage(hash_key(raw_key), 100, 80, request_id="attributed-direct")
    batch = store.record_usage_batch([
        {"key_hash": hash_key(raw_key), "baseline_tokens": 100,
         "optimized_tokens": 80, "request_id": "attributed-batch"},
        {"key_hash": hash_key("bvt_orgless"), "baseline_tokens": 100,
         "optimized_tokens": 80, "request_id": "orgless-batch"},
    ])
    assert batch["inserted"] == 2

    with store._conn() as db:
        rows = dict(db.execute(
            "SELECT request_id,organization_id FROM usage_log ORDER BY request_id").fetchall())
    assert rows == {
        "attributed-batch": organization["id"],
        "attributed-direct": organization["id"],
        "attributed-http": organization["id"],
        "orgless-batch": "",
    }


def test_session_id_survives_the_whole_proxy_receipt_path(tmp_path, monkeypatch):
    """Pin session_id end to end: proxy payload -> request model -> stored row.

    Investigating blank `usage_log.session_id` in production turned up no
    code-level drop, so this pins every hop that was suspected of one. If a
    future edit removes the field from `UsageReportRequest`, pydantic would
    silently discard it and only this assertion would notice.
    """
    import api.server as server

    store = UsageStore(str(tmp_path / "session.db"))
    raw_key = "bvt_session_path"
    store.create_key(hash_key(raw_key), "session-path", owner_id="owner-session")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    server._seq_streams.clear()
    client = TestClient(server.app)

    # The request model must declare the field; an undeclared field is dropped.
    parsed = server.UsageReportRequest.model_validate({
        "provider": "anthropic", "model": "claude-3", "baseline_tokens": 100,
        "compressed_tokens": 80, "session_id": "sess_ABC123",
    })
    assert parsed.session_id == "sess_ABC123"

    reported = client.post("/v1/usage", headers={"X-Brevitas-Key": raw_key}, json={
        "provider": "anthropic", "model": "claude-3",
        "baseline_tokens": 100, "compressed_tokens": 80,
        "request_id": "session-http", "session_id": "sess_ABC123",
        "receipt_source": "proxy",
    })
    assert reported.status_code == 200

    # Callers that report no session (the direct SDK/compress endpoints) still
    # store '' -- blank is a legitimate value there, not evidence of a drop.
    assert store.record_usage(hash_key(raw_key), 100, 80, request_id="session-absent")

    with store._conn() as db:
        rows = dict(db.execute(
            "SELECT request_id,session_id FROM usage_log ORDER BY request_id").fetchall())
    assert rows == {"session-http": "sess_ABC123", "session-absent": ""}


def test_proxy_session_id_is_stable_for_a_session_key():
    """A proxy session key must map to one receipt id, not a fresh random one.

    `_session_for` passed `_new_session` to `get_or_create` as a zero-arg
    factory, so the key never reached the session and `BrevitasSession` fell
    back to `secrets.token_urlsafe`. Receipts for one logical session then
    carried unrelated ids across a restart or an LRU/TTL eviction, which is
    indistinguishable from a dropped session_id when joining usage_log.
    """
    from brevitas import proxy

    key = "ant:tenantX:anthropic:claude-3:messages:auto:default"
    first = proxy._session_for(key).session_id
    assert first and first.startswith("sess_")
    # Bounded: UsageReportRequest.session_id rejects anything over 128 chars,
    # and an unbounded model name in the key would otherwise breach it.
    assert len(first) <= 128
    assert first == proxy._session_for(key).session_id

    # Eviction of the bucket must not mint a new identity for the same session.
    proxy._sessions.discard(key)
    assert proxy._session_for(key).session_id == first

    # Distinct buckets stay distinct, and the tenant credential digest embedded
    # in the key is not republished in the receipt column.
    other = proxy._session_for(
        "ant:tenantY:anthropic:claude-3:messages:auto:default").session_id
    assert other != first
    assert "tenantX" not in first
    proxy._sessions.clear()


def test_fallback_usage_transport_reports_the_requests_own_session_id():
    """The httpx reporting path must not substitute a different session.

    `report_usage` rebuilds the receipt from the session object and overwrites
    session_id with `session.session_id`, so whatever handle `_emit_usage`
    resolves is the id that reaches usage_log. Keying the lookup by the
    payload's session_id is not enough -- the resolved session carries its own
    identity, so the id the request computed was replaced by an unrelated one.
    """
    from brevitas import proxy

    proxy._sessions.clear()
    computed = "sess_PROXY_COMPUTED_ID"
    assert proxy._session_for(computed, computed).session_id == computed
    # Without the pin, the same lookup yields a different identity entirely.
    proxy._sessions.clear()
    assert proxy._session_for(computed).session_id != computed
    proxy._sessions.clear()


def test_receipt_anchoring_moves_level_not_delta(tmp_path, monkeypatch):
    """Pin the anchoring invariant: the LEVEL of both cost legs comes from the
    provider receipt, the DELTA between them comes from the caller and is
    mathematically untouched by anchoring."""
    import api.server as server

    store = UsageStore(str(tmp_path / "anchor.db"))
    store.create_key(hash_key("bvt_anchor"), "anchor")
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()
    client = TestClient(server.app)
    headers = {"X-Brevitas-Key": "bvt_anchor"}
    base = {"provider": "openai", "model": "gpt-4o-mini", "strategy": "native_cache",
            "receipt_available": True, "output_tokens": 10, "cache_attributable": False}

    # One local report (100 -> 80), three different provider receipts. The
    # optimized level follows the receipt; the 20-token delta never moves.
    for index, (fresh, cached) in enumerate(((90, 0), (600, 400), (5000, 0))):
        out = client.post("/v1/usage", headers=headers, json={
            **base, "baseline_tokens": 100, "compressed_tokens": 80,
            "fresh_input_tokens": fresh, "cached_input_tokens": cached,
            "request_id": f"anchor-delta-{index}"}).json()
        assert out["token_basis"] == "provider_receipt"
        assert out["compressed_tokens"] == fresh + cached
        assert out["baseline_tokens"] == fresh + cached + 20
        assert out["tokens_saved"] == out["reported_token_delta"] == 20

    # Every receipt component reaching storage is receipt-sourced, and the two
    # stored legs are the anchored ones — not the caller's local 100/80.
    with store._conn() as db:
        row = db.execute(
            "SELECT baseline_tokens,optimized_tokens,tokens_saved,fresh_input_tokens,"
            "cached_input_tokens,cache_write_tokens,output_tokens FROM usage_log "
            "WHERE request_id=?", ("anchor-delta-1",)).fetchone()
    assert (row["fresh_input_tokens"], row["cached_input_tokens"]) == (600, 400)
    assert (row["cache_write_tokens"], row["output_tokens"]) == (0, 10)
    assert (row["baseline_tokens"], row["optimized_tokens"]) == (1020, 1000)
    assert row["tokens_saved"] == 20

    # A caller-reported ZERO delta stays zero however large the receipt is.
    # Anchoring shifts both endpoints by the same constant, so it cannot create
    # a difference that the wire format never carried. Recovering savings from
    # such a report needs a NEW independent pre-optimization field, not more
    # anchoring — do not "fix" this by inventing a saving here.
    zero = client.post("/v1/usage", headers=headers, json={
        **base, "baseline_tokens": 100, "compressed_tokens": 100,
        "fresh_input_tokens": 600, "cached_input_tokens": 400,
        "request_id": "anchor-zero-delta"}).json()
    assert zero["reported_token_delta"] == 0
    assert zero["tokens_saved"] == 0
    assert zero["measured_savings_usd"] == 0
    assert zero["verified_savings_usd"] == 0

    # Without a receipt the caller's own basis is used and labelled as such.
    local = client.post("/v1/usage", headers=headers, json={
        **base, "baseline_tokens": 100, "compressed_tokens": 80,
        "receipt_available": False, "request_id": "anchor-no-receipt"}).json()
    assert local["token_basis"] == "caller_local"
    assert (local["baseline_tokens"], local["compressed_tokens"]) == (100, 80)
    assert local["tokens_saved"] == 20


def test_receipt_plausibility_quarantine_is_observe_only_by_default(tmp_path, monkeypatch):
    """The quarantine threshold/action are named parameters whose default is
    observe-only. The policy is UNVALIDATED pending Q1, so the default must
    never change a billable number."""
    import api.server as server

    assert server.RECEIPT_ANCHOR_IMPLAUSIBLE_ACTION == "observe"
    assert server.RECEIPT_ANCHOR_IMPLAUSIBLE_RATIO == 3.0

    store = UsageStore(str(tmp_path / "quarantine.db"))
    store.create_key(hash_key("bvt_quarantine"), "quarantine")
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()
    client = TestClient(server.app)
    headers = {"X-Brevitas-Key": "bvt_quarantine"}
    # Receipt input is 10x the local baseline: system prompt and tool schemas
    # the local counter never saw. Legitimate today, so only labelled.
    report = {"provider": "openai", "model": "gpt-4o-mini", "strategy": "native_cache",
              "receipt_available": True, "cache_attributable": False,
              "baseline_tokens": 100, "compressed_tokens": 80,
              "fresh_input_tokens": 1000, "output_tokens": 10}

    observed = client.post("/v1/usage", headers=headers, json={
        **report, "request_id": "quarantine-observe"}).json()
    assert observed["receipt_plausibility"] == "implausible"
    assert observed["tokens_saved"] == 20          # observe-only: nothing dropped
    assert observed["baseline_tokens"] == 1020
    assert observed["measured_savings_usd"] > 0

    # A ratio inside the threshold is not flagged at all.
    ok = client.post("/v1/usage", headers=headers, json={
        **report, "fresh_input_tokens": 200, "request_id": "quarantine-ok"}).json()
    assert ok["receipt_plausibility"] == "ok"

    # The mutating action exists but is opt-in and drops savings, which is why
    # it stays off until Q1 validates the threshold.
    monkeypatch.setattr(server, "RECEIPT_ANCHOR_IMPLAUSIBLE_ACTION", "drop_savings")
    dropped = client.post("/v1/usage", headers=headers, json={
        **report, "request_id": "quarantine-drop"}).json()
    assert dropped["receipt_plausibility"] == "implausible"
    assert dropped["tokens_saved"] == 0
    assert dropped["reported_token_delta"] == 20   # the caller's claim is kept for audit


# ── GET /v1/admin/billing/settlement ──────────────────────────────────────────
# The honest, netted, period-scoped figure. Every test below is about the SAME
# rule: this route may refuse to state a number, and must never state a WRONG
# one. A $0 on an invoice screen reads as "nothing is owed"; that specific lie is
# what 202607280007/0008/0012/0013 (and this endpoint) exist to prevent.

ORGANIZATION_A = "11111111-1111-4111-8111-1111111111aa"
ORGANIZATION_B = "11111111-1111-4111-8111-1111111111bb"
PERIOD_START = "2026-07-27T00:00:00+00:00"


def _postgrest_error(status: int, code: str) -> Exception:
    """A `requests` HTTPError carrying PostgREST's own error body."""
    import requests

    response = SimpleNamespace(status_code=status, json=lambda: {
        "code": code, "message": "synthetic", "details": "", "hint": ""})
    return requests.HTTPError("synthetic", response=response)


def _settlement_store(handler):
    from api.store import SupabaseUsageStore

    store = SupabaseUsageStore("https://example.supabase.co", "service-role")
    store._request = handler  # type: ignore[method-assign]
    return store


def _ok_summary(**overrides) -> dict:
    return {
        "ok": True, "organization_id": ORGANIZATION_A,
        "period_start": PERIOD_START, "period_end": "2026-08-03T00:00:00+00:00",
        "settlement_status": "accruing", "settlement_id": None, "revision": None,
        "estimated_fee_microusd": 22_500_000, "settled_fee_microusd": 0,
        "reported_fee_microusd": 0, "committed_fee_microusd": 0,
        "needs_review_count": 0, "attention_count": 0,
        "billing_arrangement": "marginal_per_call", "billable": True,
        "evidence": {"eligible_rows": 12, "net_verified_savings_usd": 90},
        **overrides,
    }


def test_settlement_refuses_when_the_summary_rpc_is_not_applied_yet():
    """PGRST202 is the EXPECTED state, not an error: api/ deploys on push while
    202607280013 is hand-applied. It must degrade to a stated refusal."""
    calls = []

    def handler(method, path, **kwargs):
        calls.append(path)
        if path == "usage_log":
            return []
        if path == "billing_accounts":
            return [{"organization_id": ORGANIZATION_A,
                     "current_period_start": PERIOD_START}]
        raise _postgrest_error(404, "PGRST202")

    settlement = _settlement_store(handler).get_billing_settlement()
    assert settlement["settleable"] is False
    assert settlement["reason"] == "settlement_summary_rpc_not_deployed"
    # No money stated at all — not a zero, not a null-shaped fee field.
    assert not [key for key in _flatten_keys(settlement) if key.endswith("_usd")]
    assert "rpc/billing_period_settlement_summary" in calls


def test_settlement_refuses_when_a_dependency_relation_is_missing():
    """A missing table/view/column is the same deploy-ordering state as a missing
    function, and 42501/5xx deliberately are NOT: they need a different fix."""
    import pytest as _pytest
    import requests

    for status, code in ((404, "PGRST205"), (400, "42P01"), (400, "42703")):
        def handler(method, path, _code=code, _status=status, **kwargs):
            if path == "usage_log":
                return []
            raise _postgrest_error(_status, _code)

        settlement = _settlement_store(handler).get_billing_settlement()
        assert settlement["settleable"] is False, code
        assert settlement["reason"] == "billing_accounts_unavailable", code

    def denied(method, path, **kwargs):
        if path == "usage_log":
            return []
        raise _postgrest_error(403, "42501")

    with _pytest.raises(requests.HTTPError):
        _settlement_store(denied).get_billing_settlement()


def test_settlement_refuses_while_any_authoritative_row_has_no_organization():
    """THE risk note. usage_log.organization_id is a nullable uuid and every
    evidence query is `where organization_id = $1`, so a null row is not a small
    fee — it is NO row in anybody's evidence. Summing the per-organization
    answers anyway would understate the platform total by an unknown amount and
    look exactly like a correct sum."""
    def handler(method, path, **kwargs):
        if path == "usage_log":
            assert kwargs["params"]["organization_id"] == "is.null"
            assert kwargs["params"]["authoritative"] == "is.true"
            return [{"id": 4171}]
        if path == "billing_accounts":
            return [{"organization_id": ORGANIZATION_A,
                     "current_period_start": PERIOD_START}]
        return _ok_summary()

    settlement = _settlement_store(handler).get_billing_settlement()
    assert settlement["settleable"] is False
    assert settlement["reason"] == "unattributed_authoritative_usage"
    assert settlement["unattributed_authoritative_usage"] is True
    # The organization that DID price cleanly is still reported, so an operator
    # can see the platform total is the thing that is unstatable.
    assert settlement["organizations"][0]["settleable"] is True
    assert settlement["organizations"][0]["estimated_fee_usd"] == 22.5


def test_settlement_reports_netted_micro_dollar_buckets_separately():
    def handler(method, path, **kwargs):
        if path == "usage_log":
            return []
        if path == "billing_accounts":
            return [{"organization_id": ORGANIZATION_A,
                     "current_period_start": PERIOD_START}]
        return _ok_summary(settled_fee_microusd=22_500_000,
                           reported_fee_microusd=1_000_000,
                           committed_fee_microusd=22_500_000,
                           settlement_status="reported", settlement_id=7,
                           revision=1)

    settlement = _settlement_store(handler).get_billing_settlement()
    assert settlement["settleable"] is True
    assert settlement["reason"] == ""
    entry = settlement["organizations"][0]
    assert entry["organization_id"] == ORGANIZATION_A
    # Four DIFFERENT numbers a single "amount owed" would conflate.
    assert entry["estimated_fee_usd"] == 22.5
    assert entry["settled_fee_usd"] == 22.5
    assert entry["reported_fee_usd"] == 1.0
    assert entry["committed_fee_usd"] == 22.5
    assert entry["settlement_status"] == "reported"
    assert entry["billable"] is True
    assert entry["evidence"]["eligible_rows"] == 12


def test_settlement_carries_the_rpcs_own_refusal_instead_of_flattening_it():
    """billing_period_settlement_summary answers {'ok': false, 'code': ...} for
    the states it will not price. Those must not collapse into zeros."""
    def handler(method, path, **kwargs):
        if path == "usage_log":
            return []
        if path == "billing_accounts":
            return [{"organization_id": ORGANIZATION_A,
                     "current_period_start": PERIOD_START},
                    {"organization_id": ORGANIZATION_B,
                     "current_period_start": None}]
        return {"ok": False, "code": "period_anchor_mismatch"}

    settlement = _settlement_store(handler).get_billing_settlement()
    assert settlement["settleable"] is False
    assert settlement["reason"] == "organization_not_settleable"
    assert [entry["reason"] for entry in settlement["organizations"]] == [
        "period_anchor_mismatch", "no_current_period"]
    assert not [key for key in _flatten_keys(settlement) if key.endswith("_usd")]


def _flatten_keys(value, prefix=""):
    keys = []
    if isinstance(value, dict):
        for key, item in value.items():
            keys.append(str(key))
            keys.extend(_flatten_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_flatten_keys(item))
    return keys


def test_settlement_route_degrades_instead_of_500_and_never_shows_a_zero(
        tmp_path, monkeypatch):
    import api.server as server

    store = UsageStore(str(tmp_path / "settlement-route.db"))
    monkeypatch.setattr(server, "_store", store)
    client = TestClient(server.app)
    headers = {"Authorization": "Bearer test-admin-session"}

    # Staff-only before anything else: a settlement view is cross-tenant money.
    assert client.get("/v1/admin/billing/settlement",
                      headers=headers).status_code == 403

    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "admin-user", "app_metadata": {"brevitas_admin": True}})

    # 1. SQLite has no settlement ledger at all, and says so rather than
    #    synthesising the un-netted per-row sum /v1/admin/billing already shows.
    sqlite = client.get("/v1/admin/billing/settlement", headers=headers)
    assert sqlite.status_code == 200
    assert sqlite.json()["settleable"] is False
    assert sqlite.json()["reason"] == "settlement_ledger_requires_postgres"
    assert sqlite.json()["organizations"] == []
    assert "amount_owed_usd" not in sqlite.json()

    # 2. A store that blows up must still not 500 and must still not imply $0.
    class Exploding(UsageStore):
        def get_billing_settlement(self):
            raise RuntimeError("SENTINEL-SETTLEMENT")

    monkeypatch.setattr(server, "_store", Exploding(str(tmp_path / "boom.db")))
    broken = client.get("/v1/admin/billing/settlement", headers=headers)
    assert broken.status_code == 200
    assert broken.json()["settleable"] is False
    assert broken.json()["reason"] == "settlement_read_failed"
    assert not [key for key in _flatten_keys(broken.json())
                if key.endswith("_usd")]

    # 3. Attributed: a cross-tenant staff read leaves an audit row.
    with store._conn() as db:
        actions = [row[0] for row in db.execute(
            "SELECT action FROM audit_events ORDER BY id").fetchall()]
    assert actions == ["platform.billing_settlement.read"]


def test_admin_billing_keeps_amount_owed_until_the_dashboard_moves(
        tmp_path, monkeypatch):
    """Contract with the dashboard lane: /v1/admin/billing must NOT drop
    amount_owed_usd yet. The API (Railway) and the dashboard (Vercel) ship
    separately and dashboard billingUsd() renders `Number(undefined || 0)` as a
    confident $0.00, so removing it before the dashboard reads the new route
    would create the exact wrong number in the other direction."""
    import api.server as server

    store = UsageStore(str(tmp_path / "amount-owed.db"))
    store.create_key(hash_key("bvt_owed"), "cli", owner_id="user-a")
    store.record_usage(hash_key("bvt_owed"), 1000, 400, owner_id="user-a",
                       provider="openai", model="gpt-4o-mini",
                       verified_savings_usd=0.4, brevitas_fee_usd=0.1)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_identity", lambda request: {
        "id": "admin-user", "app_metadata": {"brevitas_admin": True}})
    body = TestClient(server.app).get("/v1/admin/billing?range=all", headers={
        "Authorization": "Bearer test-admin-session"}).json()
    assert body["amount_owed_usd"] == 0.1
    assert body["settlement_pending"] is True
    assert body["settlement_endpoint"] == "/v1/admin/billing/settlement"


# ── receipt bridge: no OTHER swallow-everything path may drop a billable row ───

@pytest.mark.parametrize("fault", ["seq_stream", "snapshot"])
def test_a_quality_stream_fault_cannot_suppress_the_billable_receipt(
        tmp_path, monkeypatch, fault):
    """Second member of the same class as the tracking-label kill switch.

    _hosted_proxy_receipt's caller (brevitas.proxy._emit_usage) swallows
    everything by contract, so ANY exception raised inside _record_usage_report
    before _store.record_usage means the authoritative row is never written and
    the only trace is one 'write failed' line. The sequential quality stream sat
    on that path and could raise three different ways: _seq_stream goes through
    a BoundedTTLMap that raises ResourceLimitExceeded and constructs the gate
    from env-var floats, the trip branch does a LAZY import of
    token_efficiency_model.quality.gate, and to_dict() runs on the way out.

    None of it is money -- a byte-preserving receipt's quality_status is fixed by
    the MODE, not by the stream -- so a fault there must degrade the analytics
    and still bank the fee.

    Only the two calls a BYTE-PRESERVING (billable) receipt actually makes are
    exercised here. stream.update and the lazy trip_lever import belong to the
    non-byte-preserving branch, which the next test covers.
    """
    import api.server as server

    store = UsageStore(str(tmp_path / f"stream-{fault}.db"))
    raw_key = "bvt_stream_fault"
    store.create_key(hash_key(raw_key), "stream", owner_id="customer-stream")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._seq_streams.clear()

    def explode(*_args, **_kwargs):
        raise RuntimeError("SENTINEL-QUALITY-STREAM")

    if fault == "seq_stream":
        monkeypatch.setattr(server, "_seq_stream", explode)
    else:
        monkeypatch.setattr(server, "_stream_snapshot", explode)

    payload = {
        "provider": "openai", "model": "gpt-4o-mini",
        "baseline_tokens": 1000, "compressed_tokens": 200,
        "fresh_input_tokens": 200, "cached_input_tokens": 0, "output_tokens": 10,
        "request_id": "proxy:chatcmpl-streamfault", "strategy": "exact_cache",
        "receipt_source": "proxy", "receipt_available": True,
        "cache_attributable": True, "quality_verified": False,
    }
    if fault == "snapshot":
        # _stream_snapshot runs AFTER the write, so a raise there cannot lose
        # the row -- it can only 500 the caller for a row that was persisted.
        # Prove the row survives and the bridge does not log a write failure.
        with pytest.raises(RuntimeError):
            server._record_usage_report(
                hash_key(raw_key), server.UsageReportRequest(**payload),
                authoritative=True)
        rows = [row for row in store._rows(hash_key(raw_key)) if row["authoritative"]]
        assert [row["request_id"] for row in rows] == ["proxy:chatcmpl-streamfault"]
        assert rows[0]["brevitas_fee_usd"] > 0
        return

    server._hosted_proxy_receipt(raw_key, payload)

    rows = [row for row in store._rows(hash_key(raw_key)) if row["authoritative"]]
    assert [row["request_id"] for row in rows] == ["proxy:chatcmpl-streamfault"]
    # THE assertion: the money is banked, not merely the row.
    assert rows[0]["verified_savings_usd"] > 0
    assert rows[0]["brevitas_fee_usd"] > 0
    # And the degraded field is the cosmetic one, never upgraded to 'verified'
    # by the failure itself.
    assert rows[0]["quality_status"] in ("verified", "unverified")


@pytest.mark.parametrize("fault", ["seq_stream", "update"])
def test_a_quality_stream_fault_never_manufactures_a_verified_status(
        tmp_path, monkeypatch, fault):
    """The conservative half of the degrade.

    A NON byte-preserving receipt reaches stream.update and, if the martingale
    trips, the LAZY `from token_efficiency_model.quality.gate import trip_lever`
    — the two calls the previous test cannot reach. Its analytics row still
    feeds the settlement evidence (actual_spend_usd and the zero-spend share
    that drives the halting conditions), so losing it distorts a ceiling.

    And whatever fails, the receipt must not inherit 'verified': that is the one
    status that would let a caller-reported strategy become billable.
    """
    import api.server as server

    store = UsageStore(str(tmp_path / f"stream-conservative-{fault}.db"))
    raw_key = "bvt_stream_conservative"
    store.create_key(hash_key(raw_key), "stream", owner_id="customer-stream2")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._seq_streams.clear()

    def explode(*_args, **_kwargs):
        raise RuntimeError("SENTINEL-QUALITY-STREAM")

    if fault == "seq_stream":
        monkeypatch.setattr(server, "_seq_stream", explode)
    else:
        from token_efficiency_model.quality.sequential import SequentialQualityGate
        monkeypatch.setattr(SequentialQualityGate, "update", explode)

    server._hosted_proxy_receipt(raw_key, {
        "provider": "openai", "model": "gpt-4o-mini",
        "baseline_tokens": 1000, "compressed_tokens": 200,
        "fresh_input_tokens": 200, "output_tokens": 10,
        "request_id": "proxy:chatcmpl-reorder", "strategy": "reorder",
        "receipt_source": "proxy", "receipt_available": True,
        "quality_verified": True,
    })
    rows = [row for row in store._rows(hash_key(raw_key)) if row["authoritative"]]
    assert len(rows) == 1
    assert rows[0]["quality_status"] == "unverified"
    # Analytics are kept; nothing billable was invented out of a failure.
    assert rows[0]["verified_savings_usd"] == 0
    assert rows[0]["brevitas_fee_usd"] == 0
    assert rows[0]["tokens_saved"] == 800
    # The stream state is STATED as unavailable, not silently absent -- for a
    # stream that was never built AND for one whose serializer failed.
    class _Broken:
        def to_dict(self):
            raise RuntimeError("SENTINEL-SNAPSHOT")

    assert server._stream_snapshot(None) == {"available": False}
    assert server._stream_snapshot(_Broken()) == {"available": False}


# ── Anchored zero-spend savings ───────────────────────────────────────────────
# A Brevitas exact-cache replay has actual_cost_usd = 0 BY CONSTRUCTION: it never
# touches an upstream. That makes "zero spend" useless as a fraud signal on its
# own -- a legitimate replay and a forged row look identical on cost. What
# separates them is provenance: a real replay descends from an earlier real,
# receipted, PAID request, and a forged one descends from nothing. These tests
# pin the forward link that makes the difference machine-checkable.


def _anchor_proxy_env(server, proxy, store, monkeypatch, respond):
    """Wire a TestClient whose proxy reports authoritative receipts into `store`."""
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    server._seq_streams.clear()
    real = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda *args, **kwargs: real(
        transport=httpx.MockTransport(respond)))
    proxy._cache_init_done = True
    proxy._cache_singleton = None
    proxy.set_usage_reporter(server._hosted_proxy_receipt)
    monkeypatch.setenv("BREVITAS_PASSTHROUGH", "1")
    monkeypatch.setenv("BREVITAS_PROXY_RPM", "50")
    server._proxy_windows.clear()
    server._proxy_active.clear()
    return TestClient(server.app)


def test_cache_replay_row_anchors_to_the_paid_request_that_filled_the_cache(
        tmp_path, monkeypatch):
    """The replay receipt names the metering id of its PAID ancestor.

    Not "an ancestor existed" as an inference from strategy -- the id itself,
    written at replay time from the cache entry that served the hit, resolvable
    to a row in the same tenant's usage_log that really did carry provider spend.
    """
    import api.server as server
    import brevitas.proxy as proxy
    from brevitas.semantic_cache import SemanticCache

    store = UsageStore(str(tmp_path / "anchor.db"))
    raw_key = "bvt_anchor"
    store.create_key(hash_key(raw_key), "anchor", owner_id="customer-anchor")

    calls = []

    def _respond(request):
        calls.append(request.url.host)
        # A distinct provider id per call, as a real upstream returns: the
        # metering id is derived from it, so reusing one would collide.
        return httpx.Response(200, headers={"content-type": "application/json"}, content=(
            b'{"id":"chatcmpl-paid-ancestor-%d","choices":[{"message":{"content":"ok"},'
            b'"finish_reason":"stop"}],"usage":{"prompt_tokens":600,'
            b'"completion_tokens":50}}' % len(calls)))

    client = _anchor_proxy_env(server, proxy, store, monkeypatch, _respond)
    cache = SemanticCache(db_path=str(tmp_path / "cache.db"))
    monkeypatch.setattr(proxy, "_cache_for_request", lambda request: cache)

    # temperature 0 is a precondition of cacheability: a caller who may expect
    # fresh sampling never gets a replay.
    payload = {"model": "gpt-4o", "temperature": 0,
               "messages": [{"role": "user", "content": "anchor me"}]}
    headers = {"X-Brevitas-Key": raw_key, "Authorization": f"{BEARER} provider-key"}
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 200
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 200
    # The second request was served from cache: exactly one upstream call.
    assert len(calls) == 1

    rows = store._rows(hash_key(raw_key))
    paid = [r for r in rows if not str(r["strategy"]).startswith("exact_cache")]
    replay = [r for r in rows if str(r["strategy"]).startswith("exact_cache")]
    assert len(paid) == 1 and len(replay) == 1
    paid, replay = paid[0], replay[0]

    # The ancestor is real and PAID -- this is what the anchor is worth pointing at.
    assert paid["authoritative"]
    assert float(paid["actual_cost_usd"]) > 0
    # The replay is zero-spend by construction, which is exactly why it needs one.
    assert float(replay["actual_cost_usd"]) == 0
    assert replay["cache_attributable"]
    # And it is the shape the fee basis is about: real verified savings with no
    # spend behind them. Without an anchor this row is indistinguishable from a
    # fabricated one and has to bill zero.
    assert replay["authoritative"]
    assert float(replay["verified_savings_usd"]) > 0

    # THE assertion: a first-class forward link, not an inference.
    assert replay["savings_anchor_request_id"] == paid["request_id"]
    assert replay["savings_anchor_request_id"].startswith(proxy.RECEIPT_ID_PREFIX)
    # And the ancestor does not anchor to anything: it has spend of its own.
    assert paid["savings_anchor_request_id"] == ""
    # A row is never its own ancestor.
    assert replay["savings_anchor_request_id"] != replay["request_id"]
    proxy.set_usage_reporter(None)


def test_native_provider_cache_discount_carries_no_anchor(tmp_path, monkeypatch):
    """A provider-NATIVE discount is not a Brevitas replay and never anchors.

    DeepSeek's prompt_cache_hit_tokens (and every automatic prefix cache) would
    have happened without Brevitas, so it reports cache_attributable=False and
    must stay outside the fee basis. Anchoring one would launder exactly the
    saving the attributable flag exists to exclude -- so the gate refuses it even
    when the payload names a perfectly well-formed proxy-minted ancestor.
    """
    import api.server as server

    store = UsageStore(str(tmp_path / "native.db"))
    raw_key = "bvt_native"
    store.create_key(hash_key(raw_key), "native", owner_id="customer-native")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._seq_streams.clear()

    server._hosted_proxy_receipt(raw_key, {
        "provider": "deepseek", "model": "deepseek-chat",
        "baseline_tokens": 1000, "compressed_tokens": 1000,
        "fresh_input_tokens": 200, "cached_input_tokens": 800, "output_tokens": 10,
        "request_id": "proxy:chatcmpl-native", "strategy": "native_cache",
        "receipt_source": "proxy", "receipt_available": True,
        # False is the whole point: Brevitas did not cause this discount.
        "cache_attributable": False,
        "savings_anchor_request_id": "proxy:chatcmpl-paid-ancestor",
    })

    rows = [r for r in store._rows(hash_key(raw_key)) if r["authoritative"]]
    assert len(rows) == 1
    assert rows[0]["request_id"] == "proxy:chatcmpl-native"
    # Spend-backed row, so it is not a zero-spend saving at all -- and it holds
    # no anchor that could make one look organic.
    assert rows[0]["savings_anchor_request_id"] == ""


@pytest.mark.parametrize("claim,reason", [
    ("proxy:chatcmpl-paid-ancestor", "well_formed_proxy_id"),
    ("client:chatcmpl-paid-ancestor", "outside_the_minted_namespace"),
    ("proxy:chatcmpl-self", "self_reference"),
    ("proxy:bad\nid", "control_characters"),
])
def test_client_reported_anchor_over_v1_usage_is_never_stored(
        tmp_path, monkeypatch, claim, reason):
    """A caller cannot anchor a row, for the same reason it cannot authorize one.

    POST /v1/usage is analytics: it always records authoritative=False, so its
    rows can never carry verified savings. The anchor is money-bearing evidence
    and gets the same boundary -- otherwise a tenant could POST a self-serving
    zero-spend row claiming descent from a paid request and re-enter the fee
    basis through the exact door the concentration guard was built to watch.
    """
    import api.server as server

    store = UsageStore(str(tmp_path / f"forge-{reason}.db"))
    raw_key = "bvt_forge"
    store.create_key(hash_key(raw_key), "forge", owner_id="customer-forge")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    server._seq_streams.clear()
    client = TestClient(server.app)

    reported = client.post("/v1/usage", headers={"X-Brevitas-Key": raw_key}, json={
        "provider": "openai", "model": "gpt-4o",
        "baseline_tokens": 600, "compressed_tokens": 0,
        "fresh_input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
        "baseline_output_tokens": 50,
        "request_id": "proxy:chatcmpl-self", "strategy": "exact_cache",
        "receipt_source": "proxy", "cache_attributable": True,
        "savings_anchor_request_id": claim,
    })
    # Accepted and recorded -- rejecting it would be a way to probe the gate,
    # and analytics are not the thing being protected here.
    assert reported.status_code == 200
    assert reported.json()["savings_anchor_request_id"] == ""

    rows = store._rows(hash_key(raw_key))
    assert len(rows) == 1
    assert not rows[0]["authoritative"]
    assert rows[0]["verified_savings_usd"] == 0
    assert rows[0]["savings_anchor_request_id"] == ""
    # The reserved-namespace rewrite still applies to the row's OWN id, so the
    # anchor gate is not the only thing standing between a caller and a
    # billable slot.
    assert rows[0]["request_id"].startswith("client:")


def test_authoritative_anchor_claims_are_pinned_not_trusted(tmp_path, monkeypatch):
    """Even on the authoritative bridge the anchor is DECIDED, never accepted.

    _hosted_proxy_receipt is in-process, but its payload is assembled next to
    caller-controlled tracking labels and a caller-selectable upstream, so the
    shape of the claim is re-derived rather than believed.
    """
    import api.server as server

    store = UsageStore(str(tmp_path / "pinned.db"))
    raw_key = "bvt_pinned"
    store.create_key(hash_key(raw_key), "pinned", owner_id="customer-pinned")
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._seq_streams.clear()

    def _receipt(request_id, **overrides):
        payload = {
            "provider": "openai", "model": "gpt-4o",
            "baseline_tokens": 600, "compressed_tokens": 0,
            "baseline_output_tokens": 50,
            "fresh_input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "request_id": request_id, "strategy": "exact_cache",
            "receipt_source": "proxy", "receipt_available": True,
            "cache_attributable": True,
        }
        payload.update(overrides)
        server._hosted_proxy_receipt(raw_key, payload)
        return next(r for r in store._rows(hash_key(raw_key))
                    if r["request_id"] == request_id)

    good = _receipt("proxy:a", savings_anchor_request_id="proxy:ancestor")
    assert good["savings_anchor_request_id"] == "proxy:ancestor"
    # The row is genuinely billable, so the anchor is load-bearing here.
    assert float(good["verified_savings_usd"]) > 0

    # A self-anchor proves nothing: it would let one forged row bootstrap itself.
    assert _receipt("proxy:b", savings_anchor_request_id="proxy:b")[
        "savings_anchor_request_id"] == ""
    # An id outside the server-minted namespace cannot name an authoritative row.
    assert _receipt("proxy:c", savings_anchor_request_id="client:ancestor")[
        "savings_anchor_request_id"] == ""
    # A quality-affecting strategy has no replayed ancestor to point at.
    assert _receipt("proxy:d", strategy="reorder",
                    savings_anchor_request_id="proxy:ancestor")[
        "savings_anchor_request_id"] == ""
    # A hostile upstream id must cost the ANCHOR, never the receipt: the row is
    # still written and still billable.
    control = _receipt("proxy:e", savings_anchor_request_id="proxy:anc\nestor")
    assert control["savings_anchor_request_id"] == ""
    assert float(control["verified_savings_usd"]) > 0


def test_store_drops_an_anchor_on_a_row_shaped_to_have_none(tmp_path):
    """The storage floor holds on every write path, not just the HTTP one.

    record_usage_batch reaches _usage_row with whatever keys its caller sent, so
    the authoritative/attributable/replay shape is re-checked here as well.
    """
    from api.store import _usage_row

    base = {
        "authoritative": True, "cache_attributable": True, "strategy": "exact_cache",
        "request_id": "proxy:row", "savings_anchor_request_id": "proxy:ancestor",
    }
    assert _usage_row("kh", 600, 0, **base)["savings_anchor_request_id"] == "proxy:ancestor"
    assert _usage_row("kh", 600, 0, **{**base, "authoritative": False})[
        "savings_anchor_request_id"] == ""
    assert _usage_row("kh", 600, 0, **{**base, "cache_attributable": False})[
        "savings_anchor_request_id"] == ""
    assert _usage_row("kh", 600, 0, **{**base, "strategy": "passthrough"})[
        "savings_anchor_request_id"] == ""
    assert _usage_row("kh", 600, 0, **{**base, "savings_anchor_request_id": "proxy:row"})[
        "savings_anchor_request_id"] == ""
    # Absent is the default, and the default is unanchored.
    assert _usage_row("kh", 600, 0)["savings_anchor_request_id"] == ""


def test_anchored_receipt_survives_a_usage_log_without_the_anchor_column(monkeypatch):
    """A migration that has not been applied yet must not eat a billable receipt.

    api/ deploys on push while usage_log migrations are hand-applied, so there is
    a real window in which savings_anchor_request_id does not exist. Naming a
    missing column in an INSERT is a 400, and a 400 on this path does not degrade
    a field -- it drops the whole authoritative row, which is lost revenue and
    understated customer savings. The row must land unanchored instead.
    """
    import requests
    from api.store import SupabaseUsageStore

    attempts = []

    class _ColumnMissing:
        status_code = 400

        @staticmethod
        def json():
            return {"code": "PGRST204",
                    "message": "Could not find the 'savings_anchor_request_id' column"}

    class _Store(SupabaseUsageStore):
        def __init__(self):
            pass

        def key_organization(self, key_hash):
            return ""

        def _request(self, method, path, *, params=None, data=None, prefer=""):
            attempts.append(dict(data))
            if "savings_anchor_request_id" in data:
                raise requests.HTTPError(response=_ColumnMissing())
            return [{"id": len(attempts)}]

    store = _Store()
    assert store.record_usage(
        "kh", 600, 0, authoritative=True, cache_attributable=True,
        strategy="exact_cache", request_id="proxy:row",
        savings_anchor_request_id="proxy:ancestor") is True
    # Tried anchored, then retried without -- the receipt is recorded either way.
    assert len(attempts) == 2
    assert "savings_anchor_request_id" in attempts[0]
    assert "savings_anchor_request_id" not in attempts[1]

    # An ordinary receipt never names the column at all, so deploying this code
    # against a usage_log that predates the migration changes nothing for the
    # 99.9% of rows that have no anchor to carry.
    attempts.clear()
    assert store.record_usage("kh", 600, 400, authoritative=True,
                              strategy="passthrough", request_id="proxy:plain") is True
    assert len(attempts) == 1
    assert "savings_anchor_request_id" not in attempts[0]


def test_a_real_insert_failure_is_not_mistaken_for_a_missing_column(monkeypatch):
    """The retry is scoped to the deploy-ordering signature, nothing else.

    Swallowing an arbitrary 4xx here would silently convert a genuine write
    failure into a half-written row and a success return.
    """
    import requests
    from api.store import SupabaseUsageStore

    class _Conflict:
        status_code = 409

        @staticmethod
        def json():
            return {"code": "23505", "message": "duplicate key value"}

    class _Store(SupabaseUsageStore):
        def __init__(self):
            pass

        def key_organization(self, key_hash):
            return ""

        def _request(self, method, path, *, params=None, data=None, prefer=""):
            raise requests.HTTPError(response=_Conflict())

    with pytest.raises(requests.HTTPError):
        _Store().record_usage(
            "kh", 600, 0, authoritative=True, cache_attributable=True,
            strategy="exact_cache", request_id="proxy:row",
            savings_anchor_request_id="proxy:ancestor")
