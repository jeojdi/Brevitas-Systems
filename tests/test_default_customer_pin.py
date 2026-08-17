"""Default-customer pin (onboarding A1).

A single-tenant organization_service key may carry a ``default_customer_external_id``.
When it does, a proxy call that omits ``X-Brevitas-Customer-ID`` resolves to that pinned
customer instead of 400ing. Security invariants under test:

  * an explicitly sent header ALWAYS wins over the pin;
  * an UNPINNED (multi-tenant) key resolves to no customer without a header, so the
    organization_service proxy gate still 400s — behaviour identical to before A1.
"""
import sqlite3

import pytest

from api.auth import hash_key
from api.store import UsageStore

_SCOPES = ["proxy:invoke", "usage:write", "usage:read_own",
           "customer:route", "customer:auto_provision"]


def _org_service_key(store, user_id, raw_key):
    organization = store.ensure_organization(user_id, f"{user_id} org")
    account = store.ensure_service_account(organization["id"], "production", user_id)
    store.create_key(
        hash_key(raw_key), "backend", owner_id=user_id,
        organization_id=organization["id"], service_account_id=account["id"],
        key_type="organization_service", environment="production", scopes=_SCOPES,
    )
    return organization, account


def _set_pin(store, account_id, pin):
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE service_accounts SET default_customer_external_id=? WHERE id=?",
                   (pin, account_id))
        db.commit()


def _use_store(server, store, monkeypatch):
    monkeypatch.setattr(server, "_store", store)
    server._auth_context_cache.clear()
    server._valid_key_cache.clear()


def test_pinned_key_resolves_its_customer_without_a_header(tmp_path, monkeypatch):
    import api.server as server
    store = UsageStore(str(tmp_path / "pin.db"))
    org, account = _org_service_key(store, "company-a", "bvt_pinned")
    _set_pin(store, account["id"], "acme")
    _use_store(server, store, monkeypatch)

    ctx = server._auth_context_for_key(hash_key("bvt_pinned"), "")

    # A resolved customer_id means the org_service proxy gate will NOT 400.
    assert ctx.customer_id
    assert ctx.customer_external_id == "acme"
    assert ctx.customer_id == store.find_customer(org["id"], "acme")["id"]


def test_explicit_header_overrides_the_pin(tmp_path, monkeypatch):
    import api.server as server
    store = UsageStore(str(tmp_path / "pin-override.db"))
    org, account = _org_service_key(store, "company-a", "bvt_override")
    _set_pin(store, account["id"], "acme")
    _use_store(server, store, monkeypatch)

    ctx = server._auth_context_for_key(hash_key("bvt_override"), "other-customer")

    assert ctx.customer_external_id == "other-customer"
    assert ctx.customer_id == store.find_customer(org["id"], "other-customer")["id"]
    # The pinned customer was never provisioned, because the header took precedence.
    assert store.find_customer(org["id"], "acme") is None


def test_unpinned_key_resolves_no_customer_so_the_gate_still_400s(tmp_path, monkeypatch):
    import api.server as server
    store = UsageStore(str(tmp_path / "pin-multitenant.db"))
    _org_service_key(store, "company-a", "bvt_multitenant")  # no pin
    _use_store(server, store, monkeypatch)

    ctx = server._auth_context_for_key(hash_key("bvt_multitenant"), "")

    assert ctx.key_type == "organization_service"
    # No pin + no header => no customer. The proxy gate (api/server.py) 400s on exactly
    # this condition, so multi-tenant safety is preserved.
    assert ctx.customer_id == ""


def test_create_body_validates_the_optional_pin():
    from api.company_admin import CreateServiceAccountBody

    pinned = CreateServiceAccountBody(
        name="k", scopes=["proxy:invoke"], default_customer_external_id="acme")
    assert pinned.default_customer_external_id == "acme"

    # Absent => empty (multi-tenant).
    assert CreateServiceAccountBody(
        name="k", scopes=["proxy:invoke"]).default_customer_external_id == ""

    with pytest.raises(ValueError):
        CreateServiceAccountBody(
            name="k", scopes=["proxy:invoke"], default_customer_external_id="bad id!")
