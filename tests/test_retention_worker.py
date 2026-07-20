from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "retention_worker", ROOT / "scripts" / "dr" / "retention-worker.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def settings(**values):
    defaults = {
        "supabase_url": "https://example.supabase.co",
        "service_role_key": "restricted-test-key",
        "batch_limit": 5, "max_cycles": 3, "retries": 3,
        "request_timeout_seconds": 1, "retry_base_seconds": 0.01,
        "retry_max_seconds": 0.01, "backlog_retry_seconds": 5,
        "health_port": 8080,
    }
    defaults.update(values)
    return module.Settings(**defaults)


def counts(value=0):
    return {
        "usage_candidates": value, "audit_candidates": 0, "support_candidates": 0,
        "requests_candidates": 0, "holds_candidates": 0,
        "prior_run_evidence_candidates": 0,
    }


def cycle(*, backlog=False, remaining=0):
    return {
        "schema": "brevitas.compliance-retention-cycle.v1", "status": "completed",
        "evidence_contains_customer_content": False,
        "schema_contract_ok": True, "legal_holds_evaluated": True,
        "financial_ledger_preserved": True,
        "remaining_candidates": remaining, "backlog_remaining": backlog,
        "dry_run": counts(1), "post_apply_dry_run": counts(remaining),
    }


class FakeRPC:
    def __init__(self, cycles=None, health=None):
        self.cycles = list(cycles or [])
        self.health = health or {
            "schema": "brevitas.compliance-retention-health.v1",
            "initialized": True, "last_success_at": "2026-07-18T03:15:00Z",
            "remaining_candidates": 0, "backlog_remaining": False,
            "backlog_over_24h": False, "missed_run_24h": False,
            "schema_contract_ok": True, "legal_holds_evaluated": True,
            "financial_ledger_preserved": True,
            "evidence_contains_customer_content": False,
        }
        self.calls = []

    def call(self, name, payload):
        self.calls.append((name, payload))
        if name == "compliance_retention_worker_health":
            return self.health
        return self.cycles.pop(0)


NOW = datetime(2026, 7, 18, 3, 15, tzinfo=timezone.utc)


def test_worker_uses_unique_ids_bounded_cycle_and_content_free_health():
    rpc = FakeRPC(cycles=[cycle()])
    health = module.Health(running=True)
    worker = module.RetentionWorker(settings(), rpc, health=health, now=lambda: NOW)
    result = worker.run_cycle()
    assert result["status"] == "completed"
    name, payload = rpc.calls[-1]
    assert name == "compliance_retention_worker_cycle"
    identifiers = [payload[key] for key in (
        "p_cycle_id", "p_dry_run_id", "p_apply_run_id", "p_post_run_id",
    )]
    assert len(set(identifiers)) == 4
    assert payload["p_batch_limit"] == 5
    assert payload["p_actor_id"] == "system:retention-worker"
    snapshot = health.snapshot()
    assert snapshot["initialized"] is True
    assert snapshot["remaining_candidates"] == 0
    assert snapshot["evidence_contains_customer_content"] is False
    assert health.ready() is True


def test_worker_retries_advisory_lease_and_drains_bounded_backlog():
    unavailable = {
        "schema": "brevitas.compliance-retention-cycle.v1",
        "status": "lease_unavailable", "evidence_contains_customer_content": False,
    }
    rpc = FakeRPC(cycles=[unavailable, cycle(backlog=True, remaining=1), cycle()])
    worker = module.RetentionWorker(
        settings(), rpc, health=module.Health(running=True), now=lambda: NOW,
        jitter=lambda _low, _high: 0,
    )
    assert worker.run_until_drained() is True
    assert len([call for call in rpc.calls if call[0].endswith("worker_cycle")]) == 3


def test_worker_fails_closed_on_schema_or_ledger_contract():
    broken = cycle()
    broken["financial_ledger_preserved"] = False
    rpc = FakeRPC(cycles=[broken, broken, broken])
    worker = module.RetentionWorker(
        settings(), rpc, health=module.Health(running=True), now=lambda: NOW,
        jitter=lambda _low, _high: 0,
    )
    assert worker.run_until_drained() is False
    assert worker.health.snapshot()["consecutive_errors"] == 3
    assert worker.health.ready() is False


def test_schedule_is_exactly_0315_utc_and_infra_alerts_cover_failures():
    assert module._next_schedule(
        datetime(2026, 7, 18, 3, 14, tzinfo=timezone.utc), settings()
    ).isoformat() == "2026-07-18T03:15:00+00:00"
    assert module._next_schedule(NOW, settings()).isoformat() == "2026-07-19T03:15:00+00:00"
    policy = (ROOT / "infra" / "dr" / "resilience-policy.template.yaml").read_text()
    alerts = (ROOT / "infra" / "dr" / "retention-alerts.yaml").read_text()
    railway = json.loads((ROOT / "infra" / "dr" / "railway-retention-worker.json").read_text())
    assert 'schedule_utc: "15 03 * * *"' in policy
    assert "dedicated-railway-retention-worker" in policy
    assert railway["deploy"]["healthcheckPath"] == "/ready"
    for name in {
        "BrevitasRetentionMissedRun24h", "BrevitasRetentionBacklog24h",
        "BrevitasRetentionSchemaContractFailure",
        "BrevitasRetentionLegalHoldEvaluationFailure",
        "BrevitasRetentionLedgerInvariantFailure",
    }:
        assert name in alerts


def test_migration_cycle_holds_one_advisory_authority_and_calls_dry_apply_post():
    migration = (ROOT / "supabase" / "migrations" / "202607170007_compliance_workflows.sql").read_text()
    assert "pg_try_advisory_xact_lock" in migration
    assert "brevitas.compliance.retention.worker.v1" in migration
    assert migration.count("p_dry_run_id,p_actor_id,p_batch_limit,false") == 1
    assert migration.count("p_apply_run_id,p_actor_id,p_batch_limit,true") == 1
    assert migration.count("p_post_run_id,p_actor_id,p_batch_limit,false") == 1
    assert "compliance_retention_worker_health" in migration
    assert "state.last_success_at<now()-interval '24 hours'" in migration
