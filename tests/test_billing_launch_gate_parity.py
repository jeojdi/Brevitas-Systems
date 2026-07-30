"""U4 (docs/STRIPE_TEST_PLAN.md) — the Python half of the launch-gate parity fixture.

`tests/fixtures/billing-gate-values.json` is the single list of
BREVITAS_BILLING_ENABLED values that mean "billing is on". The Node suite
(`tests/billing_launch_gate_parity.test.mjs`) asserts the Next.js gate against
that fixture AND executes this predicate in a subprocess to compare the two.
This file exists so the same list is also enforced inside the Python test suite,
which runs as its own CI job — a fixture that only one job reads is one
`pytest`-only refactor away from silently diverging again.

The pre-existing coverage in tests/test_billing_recovery.py hard-codes its own
eight values and is left untouched; this is additive.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.billing_recovery import billing_recovery_is_configured

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "billing-gate-values.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text())

# Everything the predicate needs BESIDES the gate, so the gate is provably the
# only variable under test.
WORKER_ENVIRONMENT = {
    "STRIPE_SECRET_KEY": "sk_test_localFixture",
    "STRIPE_PRICE_ID": "price_localFixture",
    "STRIPE_METER_EVENT_NAME": "brevitas_fee_microusd",
    "BREVITAS_BILLING_WEEKLY_CAP_USD": "1",
    "SUPABASE_SERVICE_ROLE_KEY": "service-role-fixture",
    "SUPABASE_URL": "https://local.invalid",
}


@pytest.fixture()
def worker_environment(monkeypatch):
    for name, value in WORKER_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    # NEXT_PUBLIC_SUPABASE_URL is an accepted fallback for SUPABASE_URL; clear it
    # so the fixture above is the only source.
    monkeypatch.delenv("NEXT_PUBLIC_SUPABASE_URL", raising=False)
    return monkeypatch


def test_shared_gate_fixture_is_not_vacuous():
    assert FIXTURE["variable"] == "BREVITAS_BILLING_ENABLED"
    assert FIXTURE["enabledValue"] == "true"
    assert FIXTURE["absentIsDisabled"] is True
    disabled = FIXTURE["disabledValues"]
    assert len(disabled) >= 10
    assert len(set(disabled)) == len(disabled)
    assert FIXTURE["enabledValue"] not in disabled
    # The two realistic paste errors the test plan names explicitly.
    assert " true " in disabled
    assert "true " in disabled


def test_worker_gate_enables_on_the_single_fixture_value(worker_environment):
    worker_environment.setenv(FIXTURE["variable"], FIXTURE["enabledValue"])
    assert billing_recovery_is_configured() is True


def test_worker_gate_is_disabled_when_the_variable_is_absent(worker_environment):
    worker_environment.delenv(FIXTURE["variable"], raising=False)
    assert billing_recovery_is_configured() is False


@pytest.mark.parametrize("disabled_value", FIXTURE["disabledValues"], ids=repr)
def test_worker_gate_is_disabled_for_every_shared_fixture_value(
    worker_environment, disabled_value
):
    worker_environment.setenv(FIXTURE["variable"], disabled_value)
    assert billing_recovery_is_configured() is False


def test_worker_gate_is_not_the_only_requirement(worker_environment):
    """A green gate must not be able to enable an otherwise incomplete worker."""
    worker_environment.setenv(FIXTURE["variable"], FIXTURE["enabledValue"])
    for name in WORKER_ENVIRONMENT:
        worker_environment.delenv(name, raising=False)
        assert billing_recovery_is_configured() is False, name
        worker_environment.setenv(name, WORKER_ENVIRONMENT[name])
    assert billing_recovery_is_configured() is True
