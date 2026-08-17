"""Credit ledger + per-request debit (B5/B7).

SQLite-backed. The debit ships DARK: no request is debited unless
BREVITAS_CREDIT_PRICE_MICRO is set. Balances may go negative (soft overage).
"""
from api.auth import hash_key
from api.store import UsageStore


def _store(tmp_path, name):
    store = UsageStore(str(tmp_path / f"{name}.db"))
    store.create_key(hash_key(name), name)
    return store


# ─────────────────────────── store-level ───────────────────────────

def test_grant_then_debit_moves_the_balance(tmp_path):
    store = _store(tmp_path, "credit")
    assert store.credit_balance("org-1") == 0
    assert store.grant_credits("org-1", 5000, reason="seed") is True
    assert store.credit_balance("org-1") == 5000
    assert store.debit_credits_for_request("org-1", "cust", "req-1", 1200) is True
    assert store.credit_balance("org-1") == 3800


def test_debit_is_idempotent_per_request(tmp_path):
    store = _store(tmp_path, "credit-idem")
    store.grant_credits("org-1", 5000, reason="seed")
    assert store.debit_credits_for_request("org-1", "cust", "req-1", 1000) is True
    # Same request id again: no second debit.
    assert store.debit_credits_for_request("org-1", "cust", "req-1", 1000) is False
    assert store.credit_balance("org-1") == 4000


def test_debit_allows_soft_overage_negative_balance(tmp_path):
    store = _store(tmp_path, "credit-overage")
    assert store.debit_credits_for_request("org-1", "", "req-1", 2500) is True
    assert store.credit_balance("org-1") == -2500


def test_debit_rejects_unusable_inputs(tmp_path):
    store = _store(tmp_path, "credit-guard")
    assert store.debit_credits_for_request("", "c", "req", 100) is False
    assert store.debit_credits_for_request("org-1", "c", "", 100) is False
    assert store.debit_credits_for_request("org-1", "c", "req", 0) is False
    assert store.credit_balance("org-1") == 0


def test_grant_is_idempotent_on_event_and_trial(tmp_path):
    store = _store(tmp_path, "credit-grant")
    assert store.grant_credits("org-1", 1000, entry_type="purchase", stripe_event_id="evt_1") is True
    assert store.grant_credits("org-1", 1000, entry_type="purchase", stripe_event_id="evt_1") is False
    assert store.credit_balance("org-1") == 1000
    assert store.grant_credits("org-1", 500, reason="trial") is True
    # Only one trial grant per organization.
    assert store.grant_credits("org-1", 500, reason="trial") is False
    assert store.credit_balance("org-1") == 1500


# ─────────────────────── hook-level (_record_usage_report) ───────────────────────

def _ctx(server, kh, org="org-1", customer="cust-1"):
    return server.AuthContext(
        key_hash=kh, organization_id=org, customer_id=customer,
        key_type="organization_service",
        scopes=frozenset({"proxy:invoke", "usage:write"}))


def _report(server, kh, request_id, ctx, authoritative):
    return server._record_usage_report(
        kh,
        server.UsageReportRequest(
            provider="openai", model="gpt-4o-mini",
            baseline_tokens=100, compressed_tokens=100,
            fresh_input_tokens=100, output_tokens=10,
            strategy="passthrough", request_id=request_id),
        auth_context=ctx, authoritative=authoritative)


def _server_with_store(tmp_path, monkeypatch, name):
    import api.server as server
    store = _store(tmp_path, name)
    monkeypatch.setattr(server, "_store", store)
    server._seq_streams.clear()
    return server, store


def test_authoritative_request_debits_the_flat_price(tmp_path, monkeypatch):
    server, store = _server_with_store(tmp_path, monkeypatch, "hook-debit")
    monkeypatch.setenv("BREVITAS_CREDIT_PRICE_MICRO", "1000")
    kh = hash_key("hook-debit")
    _report(server, kh, "req-1", _ctx(server, kh), True)
    assert store.credit_balance("org-1") == -1000  # soft overage from 0


def test_replayed_request_id_does_not_double_debit(tmp_path, monkeypatch):
    server, store = _server_with_store(tmp_path, monkeypatch, "hook-idem")
    monkeypatch.setenv("BREVITAS_CREDIT_PRICE_MICRO", "1000")
    kh = hash_key("hook-idem")
    _report(server, kh, "req-1", _ctx(server, kh), True)
    # A re-reported request dedupes before the debit hook — no second charge.
    _report(server, kh, "req-1", _ctx(server, kh), True)
    assert store.credit_balance("org-1") == -1000


def test_dark_when_price_is_unset(tmp_path, monkeypatch):
    server, store = _server_with_store(tmp_path, monkeypatch, "hook-dark")
    monkeypatch.delenv("BREVITAS_CREDIT_PRICE_MICRO", raising=False)
    kh = hash_key("hook-dark")
    _report(server, kh, "req-1", _ctx(server, kh), True)
    assert store.credit_balance("org-1") == 0


def test_non_authoritative_rows_also_debit(tmp_path, monkeypatch):
    # Policy change: local-proxy (authoritative=False) usage now debits credits too,
    # as long as an organization resolves (here, directly from the key's org).
    server, store = _server_with_store(tmp_path, monkeypatch, "hook-nonauth")
    monkeypatch.setenv("BREVITAS_CREDIT_PRICE_MICRO", "1000")
    kh = hash_key("hook-nonauth")
    _report(server, kh, "req-1", _ctx(server, kh), False)  # local-proxy row
    assert store.credit_balance("org-1") == -1000


def test_debit_resolves_org_from_owner_when_key_has_none(tmp_path, monkeypatch):
    # A local-proxy key with no org on the context still debits: the owner's personal
    # org (organizations.billing_owner_id) is resolved and charged.
    server, store = _server_with_store(tmp_path, monkeypatch, "hook-ownerorg")
    monkeypatch.setenv("BREVITAS_CREDIT_PRICE_MICRO", "1000")
    org = store.ensure_organization("owner-9", "Personal", "individual")
    kh = hash_key("hook-ownerorg")
    ctx = server.AuthContext(
        key_hash=kh, organization_id="", billing_owner_id="owner-9", customer_id="cust",
        key_type="organization_service", scopes=frozenset({"proxy:invoke", "usage:write"}))
    _report(server, kh, "req-1", ctx, False)
    assert store.credit_balance(org["id"]) == -1000


# ─────────────────────── credits endpoint (B10) ───────────────────────

def _credits_client(tmp_path, monkeypatch, name, user="user-a"):
    from fastapi.testclient import TestClient
    import api.server as server
    store = UsageStore(str(tmp_path / f"{name}.db"))
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_dashboard_user", lambda _r: user)
    store.ensure_organization(user, "Org A")
    org_id = store.member_organization(user)["id"]
    return TestClient(server.app), store, org_id


def test_credits_endpoint_reports_balance_and_burndown(tmp_path, monkeypatch):
    client, store, org_id = _credits_client(tmp_path, monkeypatch, "credits-endpoint")
    store.grant_credits(org_id, 700000, reason="seed")
    store.debit_credits_for_request(org_id, "c", "r1", 100000)

    body = client.get("/v1/organization/credits").json()

    assert body["balance_micro"] == 600000
    assert body["spent_7d_micro"] == 100000
    # daily burn 100000/7 -> ~14285/day -> ~42 days left on 600000.
    assert body["days_to_exhaustion"] == 42
    assert body["low_balance"] is False


def test_credits_endpoint_flags_low_balance(tmp_path, monkeypatch):
    client, store, org_id = _credits_client(tmp_path, monkeypatch, "credits-low")
    store.debit_credits_for_request(org_id, "c", "r1", 500)  # no grant -> negative

    body = client.get("/v1/organization/credits").json()

    assert body["balance_micro"] == -500
    assert body["low_balance"] is True
    assert body["days_to_exhaustion"] is None


def test_org_resolves_from_owner_then_debits(tmp_path):
    # Local-proxy usage carries no org on the key; the debit must still find the
    # owner's personal org (organizations.billing_owner_id) and draw its credits down.
    store = UsageStore(str(tmp_path / "owner-debit.db"))
    org = store.ensure_organization("owner-1", "Personal", "individual")
    org_id = org["id"]
    assert store.organization_id_for_owner("owner-1") == org_id
    assert store.organization_id_for_owner("someone-else") == ""
    assert store.organization_id_for_owner("") == ""
    store.grant_credits(org_id, 1000, reason="seed")
    assert store.debit_credits_for_request(org_id, "", "req-owner-1", 100) is True
    assert store.credit_balance(org_id) == 900
