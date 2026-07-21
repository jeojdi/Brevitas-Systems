import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  OperationalEvidenceError,
  validateOperationalEvidence,
} from '../scripts/ci/operational-readiness.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const buildSha = '1'.repeat(40)
const previousBuildSha = '2'.repeat(40)
const now = new Date('2026-07-20T12:00:00Z')
const digest = character => character.repeat(64)

function evidenceReference(provider, suffix, capturedAt) {
  return {
    provider,
    reference: `evidence:immutable:${suffix}`,
    sha256: digest(suffix.length.toString(16).slice(-1)),
    captured_at: capturedAt,
    immutable: true,
  }
}

function fixture(environment = 'production') {
  const destinationEnvironment = environment === 'production' ? 'staging' : 'test'
  const restoreCompletedAt = '2026-06-01T09:50:00Z'
  return {
    schema: 'brevitas.operational-readiness-evidence.v1',
    evidence_id: 'readiness:release-001',
    environment,
    build_sha: buildSha,
    generated_at: '2026-07-20T11:59:00Z',
    release_change_id: 'change:release-001',
    evidence_owner_role: 'release_evidence_owner',
    backup: {
      pitr: {
        enabled: true,
        window_days: 14,
        checked_at: '2026-07-20T11:30:00Z',
        provider_evidence: evidenceReference('supabase', 'pitr-0001', '2026-07-20T11:30:00Z'),
      },
      logical: {
        completed_at: '2026-07-20T10:00:00Z',
        encrypted: true,
        immutable: true,
        object_lock_enabled: true,
        retention_days: 35,
        source_failure_domain: 'supabase-us',
        storage_failure_domain: 'aws-us-west',
        manifest_sha256: digest('a'),
        ciphertext_sha256: digest('b'),
        provider_evidence: evidenceReference('aws-backup', 'backup-0001', '2026-07-20T10:00:00Z'),
      },
    },
    restore: {
      exercise_id: 'restore:quarterly-001',
      source_environment: environment,
      source_id: `${environment}-us`,
      destination_environment: destinationEnvironment,
      destination_id: 'restore:isolated-001',
      isolated_destination: true,
      target_mode: 'ephemeral-postgres',
      postgresql_major: 16,
      started_at: '2026-06-01T09:00:00Z',
      failure_at: '2026-06-01T09:00:00Z',
      selected_recovery_point_at: '2026-06-01T08:50:00Z',
      raw_verified_at: '2026-06-01T09:20:00Z',
      deletion_replay: {
        verified_at: '2026-06-01T09:25:00Z',
        tombstone_count: 0,
        result: 'verified',
        provider_evidence: evidenceReference('ops-vault', 'replay-0001', '2026-06-01T09:25:00Z'),
      },
      ready_at: '2026-06-01T09:30:00Z',
      database_ready_at: '2026-06-01T09:35:00Z',
      service_ready_at: '2026-06-01T09:45:00Z',
      completed_at: restoreCompletedAt,
      backup_manifest_sha256: digest('c'),
      deletion_artifact_sha256: digest('d'),
      verification_results: {
        raw_table_counts: 'passed',
        tenant_isolation: 'passed',
        billing_reconciliation: 'passed',
        durable_job_recovery: 'passed',
        audit_append: 'passed',
        authenticated_health: 'passed',
      },
      provider_evidence: evidenceReference('ops-vault', 'restore-0001', restoreCompletedAt),
    },
    observability: {
      provider: 'grafana-cloud',
      telemetry_backend: 'prometheus',
      checked_at: '2026-07-20T11:55:00Z',
      expected_replica_counts: { api: 2, worker: 2, compressor: 1 },
      observed_replicas: [
        { service: 'api', instance_id: 'api-instance-0001', last_seen_at: '2026-07-20T11:55:00Z' },
        { service: 'api', instance_id: 'api-instance-0002', last_seen_at: '2026-07-20T11:55:00Z' },
        { service: 'worker', instance_id: 'worker-instance-0001', last_seen_at: '2026-07-20T11:55:00Z' },
        { service: 'worker', instance_id: 'worker-instance-0002', last_seen_at: '2026-07-20T11:55:00Z' },
        { service: 'compressor', instance_id: 'compressor-instance-0001', last_seen_at: '2026-07-20T11:55:00Z' },
      ],
      overview_dashboard: {
        uid: 'brevitas-enterprise-overview',
        provider_evidence: evidenceReference('grafana-cloud', 'dashboard-0001', '2026-07-20T11:55:00Z'),
      },
      per_replica_dashboard: {
        uid: 'brevitas-per-replica-health',
        provider_evidence: evidenceReference('grafana-cloud', 'dashboard-0002', '2026-07-20T11:55:00Z'),
      },
      telemetry_evidence: evidenceReference('grafana-cloud', 'telemetry-0001', '2026-07-20T11:55:00Z'),
    },
    alert_delivery: {
      provider: 'pagerduty',
      test_alert_id: 'alert:test-0001',
      route_id: 'route:primary-0001',
      destination_type: 'incident-management',
      sent_at: '2026-07-10T10:00:00Z',
      delivered_at: '2026-07-10T10:00:02Z',
      acknowledged_at: '2026-07-10T10:01:00Z',
      delivery_receipt: evidenceReference('pagerduty', 'delivery-0001', '2026-07-10T10:01:00Z'),
    },
    on_call: {
      provider: 'pagerduty',
      schedule_id: 'schedule:primary-001',
      escalation_policy_id: 'policy:escalation-001',
      primary_owner_role: 'primary_responder',
      secondary_owner_role: 'secondary_responder',
      escalation_owner_role: 'incident_commander',
      checked_at: '2026-07-20T11:00:00Z',
      provider_evidence: evidenceReference('pagerduty', 'oncall-0001', '2026-07-20T11:00:00Z'),
    },
    rollback: {
      rehearsal_id: 'rollback:release-0001',
      rehearsal_environment: 'staging',
      release_target_environment: environment,
      candidate_build_sha: buildSha,
      rollback_build_sha: previousBuildSha,
      candidate_deployed_at: '2026-07-20T10:30:00Z',
      started_at: '2026-07-20T10:40:00Z',
      rollback_completed_at: '2026-07-20T10:45:00Z',
      service_verified_at: '2026-07-20T10:50:00Z',
      result: 'passed',
      provider_evidence: evidenceReference('railway', 'rollback-0001', '2026-07-20T10:50:00Z'),
    },
    approvals: [
      {
        role: 'operations_lead',
        approver_id: 'actor:operations-0001',
        approved_at: '2026-07-20T11:57:00Z',
        approval_evidence: evidenceReference('ops-vault', 'approval-0001', '2026-07-20T11:57:00Z'),
      },
      {
        role: 'security_lead',
        approver_id: 'actor:security-0002',
        approved_at: '2026-07-20T11:58:00Z',
        approval_evidence: evidenceReference('ops-vault', 'approval-0002', '2026-07-20T11:58:00Z'),
      },
    ],
  }
}

function rejects(document, pattern, environment = 'production') {
  assert.throws(
    () => validateOperationalEvidence(document, { environment, buildSha, now }),
    error => error instanceof OperationalEvidenceError && pattern.test(error.message),
  )
}

test('valid evidence recalculates achieved RPO and RTO from ordered timestamps', () => {
  const result = validateOperationalEvidence(fixture(), { environment: 'production', buildSha, now })
  assert.equal(result.achievedRpoMinutes, 10)
  assert.equal(result.internalRestoreMinutes, 35)
  assert.equal(result.serviceRtoMinutes, 45)

  const staging = validateOperationalEvidence(fixture('staging'), { environment: 'staging', buildSha, now })
  assert.equal(staging.environment, 'staging')
})

test('missing, placeholder, local, mutable, and cross-environment evidence fails closed', () => {
  const missing = fixture()
  delete missing.backup.pitr.provider_evidence
  rejects(missing, /backup\.pitr\.provider_evidence: is required/)

  const placeholder = fixture()
  placeholder.release_change_id = 'todo:placeholder'
  placeholder.backup.logical.provider_evidence.provider = 'manual'
  placeholder.backup.logical.provider_evidence.immutable = false
  rejects(placeholder, /placeholder value[\s\S]*external authoritative system[\s\S]*must be true/)

  const wrongEnvironment = fixture()
  wrongEnvironment.environment = 'staging'
  rejects(wrongEnvironment, /evidence\.environment: must equal "production"/)
})

test('stale backup, PITR, restore, telemetry, alert, and on-call proof is rejected', () => {
  const cases = [
    ['backup.pitr.checked_at', document => { document.backup.pitr.checked_at = '2026-07-18T00:00:00Z' }],
    ['backup.logical.completed_at', document => { document.backup.logical.completed_at = '2026-07-18T00:00:00Z' }],
    ['restore.completed_at', document => { document.restore.completed_at = '2026-01-01T00:00:00Z' }],
    ['observability.checked_at', document => { document.observability.checked_at = '2026-07-20T11:00:00Z' }],
    ['alert_delivery.acknowledged_at', document => { document.alert_delivery.acknowledged_at = '2026-05-01T00:00:00Z' }],
    ['on_call.checked_at', document => { document.on_call.checked_at = '2026-07-18T00:00:00Z' }],
  ]
  for (const [path, mutate] of cases) {
    const document = fixture()
    mutate(document)
    rejects(document, new RegExp(`${path.replaceAll('.', '\\.')}.*stale`))
  }
})

test('restore gate enforces isolated deletion replay, raw ordering, and calculated objectives', () => {
  const document = fixture()
  document.restore.isolated_destination = false
  document.restore.deletion_replay.result = 'skipped'
  document.restore.deletion_replay.verified_at = '2026-06-01T09:10:00Z'
  document.restore.selected_recovery_point_at = '2026-06-01T08:30:00Z'
  document.restore.database_ready_at = '2026-06-01T10:30:00Z'
  document.restore.service_ready_at = '2026-06-01T14:00:00Z'
  document.restore.completed_at = '2026-06-01T14:01:00Z'
  rejects(document, /isolated_destination: must be true[\s\S]*calculated RPO[\s\S]*calculated internal restoration time[\s\S]*calculated service RTO[\s\S]*deletion_replay\.verified_at[\s\S]*result: must equal "verified"/)
})

test('observability requires every expected current replica and both provider dashboards', () => {
  const document = fixture()
  document.observability.observed_replicas.pop()
  document.observability.observed_replicas[1].instance_id = document.observability.observed_replicas[0].instance_id
  document.observability.per_replica_dashboard.uid = 'generic-overview'
  rejects(document, /instance_id: must be unique[\s\S]*observed 0 compressor replicas[\s\S]*brevitas-per-replica-health/)
})

test('alert delivery, distinct escalation roles, exact-build rollback, and two-person approval are mandatory', () => {
  const document = fixture()
  document.alert_delivery.acknowledged_at = '2026-07-10T09:59:00Z'
  document.on_call.escalation_owner_role = document.on_call.primary_owner_role
  document.rollback.candidate_build_sha = previousBuildSha
  document.rollback.result = 'failed'
  document.approvals[1] = structuredClone(document.approvals[0])
  rejects(document, /acknowledged_at[\s\S]*roles must be distinct[\s\S]*candidate_build_sha[\s\S]*result: must equal "passed"[\s\S]*approver_id: must be unique[\s\S]*missing security_lead approval/)
})

test('provider receipts are control-bound, unique, captured after the event, and approved afterward', () => {
  const document = fixture()
  document.observability.telemetry_evidence.provider = 'different-provider'
  document.alert_delivery.delivery_receipt.captured_at = '2026-07-10T09:59:00Z'
  document.on_call.provider_evidence.reference = document.alert_delivery.delivery_receipt.reference
  document.approvals[0].approved_at = '2026-07-20T10:00:00Z'
  rejects(document, /telemetry_evidence\.provider: must equal "grafana-cloud"[\s\S]*delivery_receipt\.captured_at[\s\S]*reference: must be unique[\s\S]*approvals\[0\]\.approved_at/)
})

test('schema, workflow, dashboard, and operator gate remain wired together', () => {
  const schema = JSON.parse(readFileSync(resolve(root, 'docs/enterprise/operational-readiness-evidence.schema.json'), 'utf8'))
  assert.equal(schema.properties.schema.const, 'brevitas.operational-readiness-evidence.v1')
  assert.equal(schema.properties.backup.properties.pitr.properties.window_days.minimum, 14)
  assert.equal(schema.properties.observability.properties.observed_replicas.minItems, 5)

  const workflow = readFileSync(resolve(root, '.github/workflows/operational-readiness.yml'), 'utf8')
  assert.match(workflow, /environment: \$\{\{ inputs\.target \}\}/)
  assert.match(workflow, /OPERATIONAL_READINESS_EVIDENCE_JSON/)
  assert.match(workflow, /--build-sha "\$\{\{ github\.sha \}\}"/)
  assert.match(workflow, /needs: validate-invocation/)
  assert.match(workflow, /test "\$TARGET" = "staging" \|\| test "\$TARGET" = "production"/)
  assert.doesNotMatch(workflow, /continue-on-error: true/)

  const dashboard = JSON.parse(readFileSync(resolve(root, 'observability/grafana/per-replica-health.json'), 'utf8'))
  assert.equal(dashboard.uid, 'brevitas-per-replica-health')
  assert.match(JSON.stringify(dashboard), /service_instance_id/)
})
