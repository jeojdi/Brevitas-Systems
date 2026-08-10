"""GET /metrics and the Prometheus scrape mirror (FUNNEL-FIX 3).

observability/prometheus/alerts.yml carries 25 rules over `brevitas_*` series and
this repository published no scrape endpoint, so every one of them evaluated
absent series -- which the burn-rate rules render as "healthy" rather than as
"blind". These tests pin the three doors on the endpoint (disabled / no token /
wrong token) and prove the counter families the alert rules select on actually
move when work happens.
"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from api.auth import hash_key
from api.observability import reset_prometheus_mirror
from api.store import UsageStore

TOKEN = "prom-scrape-token-value"


def _client(tmp_path, monkeypatch, name):
    import api.server as server

    store = UsageStore(str(tmp_path / f"{name}.db"))
    monkeypatch.setattr(server, "_store", store)
    server._valid_key_cache.clear()
    server._auth_context_cache.clear()
    server._seq_streams.clear()
    reset_prometheus_mirror()
    return server, store, TestClient(server.app)


def test_metrics_is_invisible_until_a_token_is_configured(tmp_path, monkeypatch):
    """An unconfigured deployment must look identical to one without the route.

    404 rather than 401 on a missing token, on purpose: a probe must not be able
    to learn that a credential is the only thing standing between it and the
    series.
    """
    _server, _store, client = _client(tmp_path, monkeypatch, "metrics-off")
    monkeypatch.delenv("BREVITAS_METRICS_TOKEN", raising=False)
    monkeypatch.delenv("BREVITAS_METRICS_ENABLED", raising=False)

    unconfigured = client.get("/metrics")
    with_bearer = client.get("/metrics",
                             headers={"Authorization": f"Bearer {TOKEN}"})

    assert unconfigured.status_code == 404
    assert with_bearer.status_code == 404


def test_metrics_can_be_switched_off_entirely(tmp_path, monkeypatch):
    _server, _store, client = _client(tmp_path, monkeypatch, "metrics-disabled")
    monkeypatch.setenv("BREVITAS_METRICS_TOKEN", TOKEN)
    monkeypatch.setenv("BREVITAS_METRICS_ENABLED", "false")

    response = client.get("/metrics",
                          headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 404


def test_metrics_requires_the_bearer_token(tmp_path, monkeypatch):
    _server, _store, client = _client(tmp_path, monkeypatch, "metrics-authz")
    monkeypatch.setenv("BREVITAS_METRICS_TOKEN", TOKEN)

    missing = client.get("/metrics")
    wrong = client.get("/metrics", headers={"Authorization": "Bearer nope"})
    wrong_scheme = client.get("/metrics", headers={"Authorization": f"Basic {TOKEN}"})
    correct = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert wrong_scheme.status_code == 401
    assert correct.status_code == 200
    assert correct.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in correct.headers["content-type"]


def test_every_alerted_family_is_present_even_at_zero(tmp_path, monkeypatch):
    """Absent and zero are different answers, and only one of them is true.

    A rate() floor over a never-incremented series reads *absent*, and absent
    never fires -- so the families must exist from the first scrape.
    """
    _server, _store, client = _client(tmp_path, monkeypatch, "metrics-families")
    monkeypatch.setenv("BREVITAS_METRICS_TOKEN", TOKEN)

    body = client.get("/metrics",
                      headers={"Authorization": f"Bearer {TOKEN}"}).text

    for family in ("brevitas_api_requests_total",
                   "brevitas_service_operations_total",
                   "brevitas_billing_savings_rows_total",
                   "brevitas_billing_verified_savings_usd_total",
                   "brevitas_warm_pings_total",
                   "brevitas_warm_spend_usd_total"):
        assert f"# TYPE {family} counter" in body


def test_request_and_usage_counters_move_with_real_work(tmp_path, monkeypatch):
    server, store, client = _client(tmp_path, monkeypatch, "metrics-counters")
    monkeypatch.setenv("BREVITAS_METRICS_TOKEN", TOKEN)
    raw_key = "bvt_metrics_reporter"
    store.create_key(hash_key(raw_key), "reporter", owner_id="metrics-owner")

    reported = client.post(
        "/v1/usage", headers={"X-Brevitas-Key": raw_key},
        json={"provider": "openai", "model": "gpt-4o-mini",
              "baseline_tokens": 100, "compressed_tokens": 80,
              "strategy": "native_cache", "request_id": "metrics-usage-0001"})
    denied = client.get("/v1/stats", headers={"X-Brevitas-Key": "bvt_unknown"})
    body = client.get("/metrics",
                      headers={"Authorization": f"Bearer {TOKEN}"}).text

    assert reported.status_code == 200
    assert denied.status_code == 401
    # A usage row was persisted, non-authoritative and therefore non-billable --
    # the exact partition BillableSavingsProductionStalled selects on.
    assert 'brevitas_billing_savings_rows_total{authoritative="false",billable="false"} 1.0' in body
    # ...and the 401 landed in the auth_denied partition, not client_error, so
    # the SLO burn rules keep counting only genuine server faults.
    assert 'outcome="auth_denied"' in body
    assert 'outcome="success"' in body
    assert 'sla_eligible="true"' in body


def test_the_mirror_never_raises_on_a_malformed_amount():
    """Telemetry is not the work: a bad value is dropped, never propagated.

    record_savings_row runs inside the billable receipt path, so a float() on a
    caller-supplied amount must not be able to raise there.
    """
    from api.observability import _prom_add, render_prometheus_text

    reset_prometheus_mirror()
    _prom_add("brevitas_billing_verified_savings_usd_total",
              {"authoritative": "true"}, "not-a-number")
    _prom_add("brevitas_billing_verified_savings_usd_total",
              {"authoritative": "true"}, float("nan"))
    _prom_add("brevitas_billing_verified_savings_usd_total",
              {"authoritative": "true"}, -5.0)

    body = render_prometheus_text()
    assert "brevitas_billing_verified_savings_usd_total" in body
    # The negative was clamped to zero rather than dropped, so the series exists.
    assert 'brevitas_billing_verified_savings_usd_total{authoritative="true"} 0.0' in body


def test_warm_ping_counter_records_outcome_and_spend():
    from api.observability import record_warm_ping, render_prometheus_text

    reset_prometheus_mirror()
    record_warm_ping(outcome="warmed", spent_usd=0.25)
    record_warm_ping(outcome="not-a-real-outcome", spent_usd=0.0)

    body = render_prometheus_text()
    assert 'brevitas_warm_pings_total{outcome="warmed"} 1.0' in body
    assert 'brevitas_warm_pings_total{outcome="unknown"} 1.0' in body
    assert "brevitas_warm_spend_usd_total 0.25" in body


def test_alert_rules_bind_to_the_families_this_endpoint_publishes():
    """The point of the endpoint is that the existing rules stop reading absent.

    Only the families this mirror publishes are asserted; the gauge-based billing
    rules are still fed by the OTel path alone and are deliberately out of scope.
    """
    from api.observability import _PROM_HELP

    rules = json.loads(
        (Path(__file__).parent.parent
         / "observability/prometheus/alerts.yml").read_text())
    expressions = " ".join(
        rule.get("expr", "") + rule.get("record", "")
        for group in rules["groups"] for rule in group["rules"])

    for family in ("brevitas_api_requests_total",
                   "brevitas_service_operations_total",
                   "brevitas_billing_savings_rows_total",
                   "brevitas_billing_verified_savings_usd_total"):
        assert family in expressions
        assert family in _PROM_HELP
