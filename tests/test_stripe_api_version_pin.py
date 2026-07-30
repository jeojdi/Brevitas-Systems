"""G9: the Python billing worker pins ONE Stripe API version, on every call.

tests/stripe_api_version_pin.test.mjs asserts the three literals (config.ts,
this worker, staging-canary.mjs) are the same string. That is a text check. This
file asserts the *behaviour* the literal is supposed to buy: that the header
actually leaves the process on every Stripe REST path the worker has — send,
catalog validation and reconciliation — and that a caller supplying its own
headers cannot drop it.

Sending no Stripe-Version, which is what this worker did before, does not pin
anything to "latest": Stripe applies the account's default version, which an
operator can change in the dashboard with no deploy. So the absence of this
header was a silent dependency on mutable remote configuration.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.billing_recovery import (
    STRIPE_API_VERSION,
    BillingEntry,
    CatalogContractError,
    Reconciliation,
    StripeRestBillingGateway,
)

NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class RecordingSession:
    """Minimal requests.Session stand-in that records outbound headers."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.headers = {}
        self.auth = None

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, dict(kwargs)))
        if not self.responses:
            raise AssertionError("unexpected Stripe request")
        return self.responses.pop(0)

    def close(self):
        pass


def valid_catalog(*summary_pages):
    price = {
        "active": True,
        "type": "recurring",
        "currency": "usd",
        "billing_scheme": "per_unit",
        "unit_amount_decimal": "0.0001",
        "recurring": {"meter": "mtr_test", "interval": "week", "usage_type": "metered"},
    }
    meter = {
        "status": "active",
        "event_name": "brevitas_fee_microusd",
        "default_aggregation": {"formula": "sum"},
        "customer_mapping": {"type": "by_id", "event_payload_key": "stripe_customer_id"},
        "value_settings": {"event_payload_key": "value"},
    }
    return [FakeResponse(price), FakeResponse(meter),
            *(FakeResponse(page) for page in summary_pages)]


def gateway_for(session):
    return StripeRestBillingGateway(
        "sk_test_mock", "price_mock", "brevitas_fee_microusd",
        exclusive_meter_writer=True, session=session, now=lambda: NOW,
    )


def entry(fee=250_000):
    return BillingEntry(
        id=901,
        user_id="user",
        occurred_at=NOW - timedelta(minutes=1),
        fee_microusd=fee,
        stripe_customer_id="cus_mock",
        attempts=0,
        reclaimed=False,
        outbound_started_at=None,
        period_start=NOW - timedelta(days=1),
        period_end=NOW + timedelta(days=1),
        expected_period_microusd=fee,
    )


def sent_versions(session):
    return [kwargs.get("headers", {}).get("Stripe-Version")
            for _, _, kwargs in session.requests]


def test_pinned_version_is_the_validated_dahlia_release():
    # Restated literally, not derived: bumping the pin must be a deliberate edit
    # in every place that names it, because the shapes the Node side parses
    # (item-level subscription periods, invoice.parent.subscription_details) are
    # only validated for this version.
    assert STRIPE_API_VERSION == "2026-06-24.dahlia"


def test_every_send_path_request_carries_the_pinned_version():
    session = RecordingSession([
        *valid_catalog(),
        FakeResponse({"identifier": "brevitas-fee-901"}),
    ])

    gateway_for(session).send(entry())

    # send() validates the catalog first, so this covers GET /v1/prices,
    # GET /v1/billing/meters and POST /v1/billing/meter_events.
    assert [method for method, _, _ in session.requests] == ["GET", "GET", "POST"]
    assert session.requests[-1][1].endswith("/v1/billing/meter_events")
    assert sent_versions(session) == [STRIPE_API_VERSION] * 3


def test_caller_supplied_headers_survive_and_cannot_drop_the_pin():
    session = RecordingSession([
        *valid_catalog(),
        FakeResponse({"identifier": "brevitas-fee-901"}),
    ])

    gateway_for(session).send(entry())

    post_headers = session.requests[-1][2]["headers"]
    # The idempotency key send() passes must still be there — merging the pin
    # must not clobber caller headers ...
    assert post_headers["Idempotency-Key"] == entry().idempotency_key
    # ... and the pin must be present alongside it.
    assert post_headers["Stripe-Version"] == STRIPE_API_VERSION


def test_a_caller_cannot_override_the_pinned_version():
    # Defence in depth: the pin is merged last, so even an explicit attempt to
    # request an older version from inside the process loses. A downgrade here
    # would change response shapes for the whole worker.
    session = RecordingSession([FakeResponse({"ok": True})])
    gateway = gateway_for(session)

    gateway._request("GET", "/v1/prices/price_mock",
                     headers={"Stripe-Version": "2025-06-30.basil"})

    assert session.requests[0][2]["headers"]["Stripe-Version"] == STRIPE_API_VERSION


def test_validate_contract_and_reconcile_paths_carry_the_pinned_version():
    session = RecordingSession(valid_catalog({
        "data": [{"id": "summary", "meter": "mtr_test", "aggregated_value": 250_000}],
        "has_more": False,
    }))
    gateway = gateway_for(session)

    assert gateway.reconcile(entry(250_000), lambda: True) is Reconciliation.ACCEPTED

    urls = [url for _, url, _ in session.requests]
    assert any("/v1/prices/" in url for url in urls)
    assert any(url.endswith("/v1/billing/meters/mtr_test") for url in urls)
    assert any("event_summaries" in url for url in urls)
    assert sent_versions(session) == [STRIPE_API_VERSION] * 3


def test_pinning_the_header_does_not_disturb_error_classification():
    # The header is added on the request side only; the 4xx/5xx taxonomy that
    # decides dead-vs-review-vs-retry must be untouched by this change.
    rejecting = RecordingSession([FakeResponse({"error": {"type": "invalid_request_error"}},
                                               status_code=400)])
    gateway = gateway_for(rejecting)
    with pytest.raises(CatalogContractError):
        gateway.validate_contract()
    assert sent_versions(rejecting) == [STRIPE_API_VERSION]
