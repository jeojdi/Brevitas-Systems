#!/usr/bin/env python3
"""Verify a LIVE Stripe catalog against BOTH validators, printing no secrets.

Run:  STRIPE_SECRET_KEY="$(pbpaste)" ./.venv/bin/python scripts/db/verify_live_stripe_catalog.py price_XXXX

Why both: the Node predicate (src/lib/billing/config.ts) and the Python worker's
validate_contract() (api/billing_recovery.py) check DIFFERENT things. The worker
additionally verifies four meter fields the JS side never looks at -- aggregation
formula, customer mapping type, customer mapping key, and value key. A price that
passes only one of them bills wrongly or not at all, and the failure surfaces as
every fee parking in 'review' with no obvious cause.

This prints field values and booleans only. It never prints the key, and it makes
no write calls -- two GETs.
"""
import json, os, sys, urllib.parse, urllib.request

key = os.environ.get("STRIPE_SECRET_KEY", "")
price_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("STRIPE_PRICE_ID", "")
event_name = os.environ.get("STRIPE_METER_EVENT_NAME", "brevitas_fee_microusd")

if not key or not price_id:
    print("usage: STRIPE_SECRET_KEY=... verify_live_stripe_catalog.py <price_id>")
    raise SystemExit(2)
print(f"key mode      : {'LIVE' if key.startswith('sk_live_') else 'test'}")
print(f"price         : {price_id}")
print(f"meter event   : {event_name}")
print()


def stripe_get(path):
    req = urllib.request.Request("https://api.stripe.com" + path)
    req.add_header("Authorization", "Bearer " + key)
    # Pin the same version the code speaks, so this checks what production sees.
    req.add_header("Stripe-Version", "2026-06-24.dahlia")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


price = stripe_get("/v1/prices/" + urllib.parse.quote(price_id))
recurring = price.get("recurring") or {}
meter_id = recurring.get("meter")
meter = stripe_get("/v1/billing/meters/" + urllib.parse.quote(meter_id)) if meter_id else {}

checks = [
    ("price is active",              price.get("active") is True),
    ("currency is usd",              price.get("currency") == "usd"),
    ("billing_scheme per_unit",      price.get("billing_scheme") == "per_unit"),
    ("unit_amount_decimal 0.0001",   price.get("unit_amount_decimal") == "0.0001"),
    ("interval is week",             recurring.get("interval") == "week"),
    ("interval_count is 1",          (recurring.get("interval_count") or 1) == 1),
    ("usage_type metered",           recurring.get("usage_type") == "metered"),
    ("price carries a meter",        bool(meter_id)),
    ("meter is active",              meter.get("status") == "active"),
    ("meter event name matches",     meter.get("event_name") == event_name),
    # The four the JS validator never checks:
    ("meter aggregation = sum",
     ((meter.get("default_aggregation") or {}).get("formula") == "sum")),
    ("customer mapping type by_id",
     ((meter.get("customer_mapping") or {}).get("type") == "by_id")),
    ("customer mapping key",
     ((meter.get("customer_mapping") or {}).get("event_payload_key") == "stripe_customer_id")),
    ("value settings key = value",
     ((meter.get("value_settings") or {}).get("event_payload_key") == "value")),
]

width = max(len(name) for name, _ in checks)
failed = 0
for name, ok in checks:
    print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}")
    failed += 0 if ok else 1

print()
print(f"meter id      : {meter_id}")
print(f"product       : {price.get('product')}")
print(f"lookup_key    : {price.get('lookup_key')}")
print()
if failed:
    print(f"{failed} CHECK(S) FAILED -- do not wire this price into production.")
    raise SystemExit(1)
print("ALL CHECKS PASSED -- both validators accept this live catalog.")
