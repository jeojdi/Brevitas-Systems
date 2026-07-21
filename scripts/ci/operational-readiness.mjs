import { lstatSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

const SHA256 = /^[0-9a-f]{64}$/
const BUILD_SHA = /^[0-9a-f]{40}$/
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9:._/-]{7,255}$/
const SAFE_NAME = /^[a-z][a-z0-9_-]{1,63}$/
const SAFE_ROLE = /^[a-z][a-z0-9_]{2,63}$/
const INSTANCE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/
const UTC_SECOND = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/

const MINUTE = 60_000
const MAX_AGES = Object.freeze({
  bundle: 60 * MINUTE,
  pitr: 24 * 60 * MINUTE,
  backup: 26 * 60 * MINUTE,
  restore: 90 * 24 * 60 * MINUTE,
  telemetry: 15 * MINUTE,
  alert: 30 * 24 * 60 * MINUTE,
  onCall: 24 * 60 * MINUTE,
  rollback: 30 * 24 * 60 * MINUTE,
})

const FORBIDDEN_PROVIDERS = new Set([
  'example', 'local', 'manual', 'none', 'placeholder', 'repository', 'self', 'test', 'todo',
])
const FORBIDDEN_VALUE = /(?:todo|tbd|placeholder|example\.com|john\s+doe|jane\s+doe)/i
const FORBIDDEN_KEY = /(?:password|secret|token|dsn|email|phone|prompt|response|customer_name)/i

export class OperationalEvidenceError extends Error {
  constructor(issues) {
    super(`Operational readiness evidence failed:\n- ${issues.join('\n- ')}`)
    this.name = 'OperationalEvidenceError'
    this.issues = issues
  }
}

function minutesBetween(later, earlier) {
  return (later.getTime() - earlier.getTime()) / MINUTE
}

function inspectForSensitiveOrPlaceholderValues(value, path, issues) {
  if (Array.isArray(value)) {
    value.forEach((item, index) => inspectForSensitiveOrPlaceholderValues(item, `${path}[${index}]`, issues))
    return
  }
  if (!value || typeof value !== 'object') return
  for (const [key, child] of Object.entries(value)) {
    const childPath = path ? `${path}.${key}` : key
    if (FORBIDDEN_KEY.test(key)) issues.push(`${childPath} is a forbidden evidence field`)
    if (typeof child === 'string') {
      if (FORBIDDEN_VALUE.test(child)) issues.push(`${childPath} contains a placeholder value`)
      if (/^https?:\/\//i.test(child)) {
        try {
          const url = new URL(child)
          if (url.username || url.password || url.search || url.hash) {
            issues.push(`${childPath} URL must not contain credentials, query parameters, or fragments`)
          }
        } catch {
          issues.push(`${childPath} is not a valid URL`)
        }
      }
    }
    inspectForSensitiveOrPlaceholderValues(child, childPath, issues)
  }
}

export function validateOperationalEvidence(document, options) {
  const issues = []
  const timestamps = new Map()
  const evidenceReferences = new Set()
  const expectedEnvironment = options?.environment
  const expectedBuildSha = options?.buildSha
  const now = options?.now instanceof Date ? options.now : new Date(options?.now || Date.now())

  const issue = (path, message) => issues.push(`${path}: ${message}`)
  const obj = (value, path, required, allowed = required) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      issue(path, 'must be an object')
      return false
    }
    for (const key of required) if (!(key in value)) issue(`${path}.${key}`, 'is required')
    for (const key of Object.keys(value)) if (!allowed.includes(key)) issue(`${path}.${key}`, 'is not allowed')
    return true
  }
  const string = (value, path, pattern, description = 'has an invalid format') => {
    if (typeof value !== 'string' || !pattern.test(value)) {
      issue(path, description)
      return false
    }
    return true
  }
  const exact = (value, path, expected) => {
    if (value !== expected) issue(path, `must equal ${JSON.stringify(expected)}`)
  }
  const bool = (value, path, expected = true) => {
    if (value !== expected) issue(path, `must be ${expected}`)
  }
  const integer = (value, path, minimum = 0) => {
    if (!Number.isSafeInteger(value) || value < minimum) {
      issue(path, `must be an integer >= ${minimum}`)
      return false
    }
    return true
  }
  const timestamp = (value, path) => {
    if (typeof value !== 'string' || !UTC_SECOND.test(value)) {
      issue(path, 'must be canonical UTC at whole-second precision (YYYY-MM-DDTHH:mm:ssZ)')
      return null
    }
    const parsed = new Date(value)
    if (Number.isNaN(parsed.getTime()) || parsed.toISOString().replace('.000Z', 'Z') !== value) {
      issue(path, 'must be a real canonical UTC timestamp')
      return null
    }
    if (parsed.getTime() > now.getTime() + 5 * MINUTE) issue(path, 'must not be in the future')
    timestamps.set(path, parsed)
    return parsed
  }
  const fresh = (date, path, maximumAge) => {
    if (date && now.getTime() - date.getTime() > maximumAge) issue(path, 'evidence is stale')
  }
  const ordered = (earlier, later, path, allowEqual = false) => {
    if (earlier && later) {
      const valid = allowEqual ? earlier <= later : earlier < later
      if (!valid) issue(path, `must be ${allowEqual ? 'at or ' : ''}after the preceding event`)
    }
  }
  const provider = (value, path) => {
    if (string(value, path, SAFE_NAME, 'must be a bounded provider identifier') && FORBIDDEN_PROVIDERS.has(value)) {
      issue(path, 'must identify an external authoritative system, not local/manual/test evidence')
    }
  }
  const evidenceRef = (value, path) => {
    if (!obj(value, path, ['provider', 'reference', 'sha256', 'captured_at', 'immutable'])) return null
    provider(value.provider, `${path}.provider`)
    if (string(value.reference, `${path}.reference`, /^evidence:[A-Za-z0-9][A-Za-z0-9:._/-]{7,246}$/,
      'must be an opaque evidence: reference')) {
      const referenceTail = value.reference.slice('evidence:'.length)
      if (FORBIDDEN_VALUE.test(referenceTail)) issue(`${path}.reference`, 'must not be a placeholder')
      if (evidenceReferences.has(value.reference)) issue(`${path}.reference`, 'must be unique to this control')
      evidenceReferences.add(value.reference)
    }
    string(value.sha256, `${path}.sha256`, SHA256, 'must be a lowercase SHA-256 digest')
    const capturedAt = timestamp(value.captured_at, `${path}.captured_at`)
    bool(value.immutable, `${path}.immutable`)
    return capturedAt
  }

  if (!obj(document, 'evidence', [
    'schema', 'evidence_id', 'environment', 'build_sha', 'generated_at', 'release_change_id',
    'evidence_owner_role', 'backup', 'restore', 'observability', 'alert_delivery', 'on_call',
    'rollback', 'approvals',
  ])) throw new OperationalEvidenceError(issues)
  inspectForSensitiveOrPlaceholderValues(document, 'evidence', issues)

  exact(document.schema, 'evidence.schema', 'brevitas.operational-readiness-evidence.v1')
  string(document.evidence_id, 'evidence.evidence_id', SAFE_ID, 'must be a bounded opaque ID')
  if (!['staging', 'production'].includes(expectedEnvironment)) {
    issue('options.environment', 'must be staging or production')
  }
  exact(document.environment, 'evidence.environment', expectedEnvironment)
  string(document.build_sha, 'evidence.build_sha', BUILD_SHA, 'must be a lowercase 40-character Git SHA')
  if (!BUILD_SHA.test(expectedBuildSha || '')) issue('options.buildSha', 'must be a lowercase 40-character Git SHA')
  else exact(document.build_sha, 'evidence.build_sha', expectedBuildSha)
  const generatedAt = timestamp(document.generated_at, 'evidence.generated_at')
  fresh(generatedAt, 'evidence.generated_at', MAX_AGES.bundle)
  string(document.release_change_id, 'evidence.release_change_id', SAFE_ID, 'must be a bounded opaque ID')
  string(document.evidence_owner_role, 'evidence.evidence_owner_role', SAFE_ROLE, 'must be a role, not a person')

  const backup = document.backup
  if (obj(backup, 'evidence.backup', ['pitr', 'logical'])) {
    const pitr = backup.pitr
    if (obj(pitr, 'evidence.backup.pitr', ['enabled', 'window_days', 'checked_at', 'provider_evidence'])) {
      bool(pitr.enabled, 'evidence.backup.pitr.enabled')
      if (integer(pitr.window_days, 'evidence.backup.pitr.window_days', 14) && pitr.window_days < 14) {
        issue('evidence.backup.pitr.window_days', 'must cover at least 14 days')
      }
      const checkedAt = timestamp(pitr.checked_at, 'evidence.backup.pitr.checked_at')
      fresh(checkedAt, 'evidence.backup.pitr.checked_at', MAX_AGES.pitr)
      const receiptAt = evidenceRef(pitr.provider_evidence, 'evidence.backup.pitr.provider_evidence')
      ordered(checkedAt, receiptAt, 'evidence.backup.pitr.provider_evidence.captured_at', true)
    }
    const logical = backup.logical
    if (obj(logical, 'evidence.backup.logical', [
      'completed_at', 'encrypted', 'immutable', 'object_lock_enabled', 'retention_days',
      'source_failure_domain', 'storage_failure_domain', 'manifest_sha256', 'ciphertext_sha256',
      'provider_evidence',
    ])) {
      const completedAt = timestamp(logical.completed_at, 'evidence.backup.logical.completed_at')
      fresh(completedAt, 'evidence.backup.logical.completed_at', MAX_AGES.backup)
      bool(logical.encrypted, 'evidence.backup.logical.encrypted')
      bool(logical.immutable, 'evidence.backup.logical.immutable')
      bool(logical.object_lock_enabled, 'evidence.backup.logical.object_lock_enabled')
      integer(logical.retention_days, 'evidence.backup.logical.retention_days', 35)
      string(logical.source_failure_domain, 'evidence.backup.logical.source_failure_domain', SAFE_NAME)
      string(logical.storage_failure_domain, 'evidence.backup.logical.storage_failure_domain', SAFE_NAME)
      if (logical.source_failure_domain === logical.storage_failure_domain) {
        issue('evidence.backup.logical.storage_failure_domain', 'must be independent of the source failure domain')
      }
      string(logical.manifest_sha256, 'evidence.backup.logical.manifest_sha256', SHA256)
      string(logical.ciphertext_sha256, 'evidence.backup.logical.ciphertext_sha256', SHA256)
      if (logical.manifest_sha256 === logical.ciphertext_sha256) {
        issue('evidence.backup.logical.ciphertext_sha256', 'must not reuse the manifest digest')
      }
      const receiptAt = evidenceRef(logical.provider_evidence, 'evidence.backup.logical.provider_evidence')
      ordered(completedAt, receiptAt, 'evidence.backup.logical.provider_evidence.captured_at', true)
    }
  }

  const restore = document.restore
  let achievedRpoMinutes = null
  let internalRestoreMinutes = null
  let serviceRtoMinutes = null
  if (obj(restore, 'evidence.restore', [
    'exercise_id', 'source_environment', 'source_id', 'destination_environment', 'destination_id',
    'isolated_destination', 'target_mode', 'postgresql_major', 'started_at', 'failure_at',
    'selected_recovery_point_at', 'raw_verified_at', 'deletion_replay', 'ready_at',
    'database_ready_at', 'service_ready_at', 'completed_at', 'backup_manifest_sha256',
    'deletion_artifact_sha256', 'verification_results', 'provider_evidence',
  ])) {
    string(restore.exercise_id, 'evidence.restore.exercise_id', SAFE_ID)
    exact(restore.source_environment, 'evidence.restore.source_environment', expectedEnvironment)
    string(restore.source_id, 'evidence.restore.source_id', SAFE_ID)
    const expectedDestination = expectedEnvironment === 'production' ? 'staging' : 'test'
    exact(restore.destination_environment, 'evidence.restore.destination_environment', expectedDestination)
    string(restore.destination_id, 'evidence.restore.destination_id', SAFE_ID)
    if (restore.source_id === restore.destination_id) {
      issue('evidence.restore.destination_id', 'must differ from the source ID')
    }
    bool(restore.isolated_destination, 'evidence.restore.isolated_destination')
    exact(restore.target_mode, 'evidence.restore.target_mode', 'ephemeral-postgres')
    exact(restore.postgresql_major, 'evidence.restore.postgresql_major', 16)
    const startedAt = timestamp(restore.started_at, 'evidence.restore.started_at')
    const failureAt = timestamp(restore.failure_at, 'evidence.restore.failure_at')
    const recoveryPointAt = timestamp(
      restore.selected_recovery_point_at, 'evidence.restore.selected_recovery_point_at')
    const rawAt = timestamp(restore.raw_verified_at, 'evidence.restore.raw_verified_at')
    const readyAt = timestamp(restore.ready_at, 'evidence.restore.ready_at')
    const databaseAt = timestamp(restore.database_ready_at, 'evidence.restore.database_ready_at')
    const serviceAt = timestamp(restore.service_ready_at, 'evidence.restore.service_ready_at')
    const completedAt = timestamp(restore.completed_at, 'evidence.restore.completed_at')
    fresh(completedAt, 'evidence.restore.completed_at', MAX_AGES.restore)
    ordered(recoveryPointAt, failureAt, 'evidence.restore.failure_at', true)
    ordered(failureAt, startedAt, 'evidence.restore.started_at', true)
    ordered(startedAt, rawAt, 'evidence.restore.raw_verified_at', true)
    ordered(rawAt, readyAt, 'evidence.restore.ready_at')
    ordered(readyAt, databaseAt, 'evidence.restore.database_ready_at', true)
    ordered(databaseAt, serviceAt, 'evidence.restore.service_ready_at', true)
    ordered(serviceAt, completedAt, 'evidence.restore.completed_at', true)
    if (failureAt && recoveryPointAt) {
      achievedRpoMinutes = minutesBetween(failureAt, recoveryPointAt)
      if (achievedRpoMinutes < 0 || achievedRpoMinutes > 15) {
        issue('evidence.restore.selected_recovery_point_at',
          `calculated RPO is ${achievedRpoMinutes.toFixed(2)} minutes; maximum is 15`)
      }
    }
    if (startedAt && databaseAt) {
      internalRestoreMinutes = minutesBetween(databaseAt, startedAt)
      if (internalRestoreMinutes < 0 || internalRestoreMinutes > 60) {
        issue('evidence.restore.database_ready_at',
          `calculated internal restoration time is ${internalRestoreMinutes.toFixed(2)} minutes; maximum is 60`)
      }
    }
    if (startedAt && serviceAt) {
      serviceRtoMinutes = minutesBetween(serviceAt, startedAt)
      if (serviceRtoMinutes < 0 || serviceRtoMinutes > 240) {
        issue('evidence.restore.service_ready_at',
          `calculated service RTO is ${serviceRtoMinutes.toFixed(2)} minutes; maximum is 240`)
      }
    }
    string(restore.backup_manifest_sha256, 'evidence.restore.backup_manifest_sha256', SHA256)
    string(restore.deletion_artifact_sha256, 'evidence.restore.deletion_artifact_sha256', SHA256)
    if (restore.backup_manifest_sha256 === restore.deletion_artifact_sha256) {
      issue('evidence.restore.deletion_artifact_sha256', 'must not reuse the backup manifest digest')
    }
    const replay = restore.deletion_replay
    if (obj(replay, 'evidence.restore.deletion_replay', [
      'verified_at', 'tombstone_count', 'result', 'provider_evidence',
    ])) {
      const replayAt = timestamp(replay.verified_at, 'evidence.restore.deletion_replay.verified_at')
      ordered(rawAt, replayAt, 'evidence.restore.deletion_replay.verified_at')
      ordered(replayAt, readyAt, 'evidence.restore.ready_at', true)
      integer(replay.tombstone_count, 'evidence.restore.deletion_replay.tombstone_count', 0)
      exact(replay.result, 'evidence.restore.deletion_replay.result', 'verified')
      const receiptAt = evidenceRef(replay.provider_evidence, 'evidence.restore.deletion_replay.provider_evidence')
      ordered(replayAt, receiptAt, 'evidence.restore.deletion_replay.provider_evidence.captured_at', true)
    }
    const results = restore.verification_results
    const resultNames = [
      'raw_table_counts', 'tenant_isolation', 'billing_reconciliation', 'durable_job_recovery',
      'audit_append', 'authenticated_health',
    ]
    if (obj(results, 'evidence.restore.verification_results', resultNames)) {
      for (const name of resultNames) exact(results[name], `evidence.restore.verification_results.${name}`, 'passed')
    }
    const restoreReceiptAt = evidenceRef(restore.provider_evidence, 'evidence.restore.provider_evidence')
    ordered(completedAt, restoreReceiptAt, 'evidence.restore.provider_evidence.captured_at', true)
  }

  const observability = document.observability
  if (obj(observability, 'evidence.observability', [
    'provider', 'telemetry_backend', 'checked_at', 'expected_replica_counts', 'observed_replicas',
    'overview_dashboard', 'per_replica_dashboard', 'telemetry_evidence',
  ])) {
    provider(observability.provider, 'evidence.observability.provider')
    provider(observability.telemetry_backend, 'evidence.observability.telemetry_backend')
    const checkedAt = timestamp(observability.checked_at, 'evidence.observability.checked_at')
    fresh(checkedAt, 'evidence.observability.checked_at', MAX_AGES.telemetry)
    const counts = observability.expected_replica_counts
    if (obj(counts, 'evidence.observability.expected_replica_counts', ['api', 'worker', 'compressor'])) {
      integer(counts.api, 'evidence.observability.expected_replica_counts.api', 2)
      integer(counts.worker, 'evidence.observability.expected_replica_counts.worker', 2)
      integer(counts.compressor, 'evidence.observability.expected_replica_counts.compressor', 1)
    }
    if (!Array.isArray(observability.observed_replicas) || observability.observed_replicas.length === 0) {
      issue('evidence.observability.observed_replicas', 'must be a non-empty array')
    } else {
      const seen = new Set()
      const actual = { api: 0, worker: 0, compressor: 0 }
      for (const [index, replica] of observability.observed_replicas.entries()) {
        const path = `evidence.observability.observed_replicas[${index}]`
        if (!obj(replica, path, ['service', 'instance_id', 'last_seen_at'])) continue
        if (!['api', 'worker', 'compressor'].includes(replica.service)) issue(`${path}.service`, 'is unsupported')
        else actual[replica.service] += 1
        if (string(replica.instance_id, `${path}.instance_id`, INSTANCE_ID, 'must be an opaque instance ID')) {
          if (seen.has(replica.instance_id)) issue(`${path}.instance_id`, 'must be unique across services')
          seen.add(replica.instance_id)
        }
        const lastSeenAt = timestamp(replica.last_seen_at, `${path}.last_seen_at`)
        fresh(lastSeenAt, `${path}.last_seen_at`, MAX_AGES.telemetry)
      }
      if (counts && typeof counts === 'object') {
        for (const service of ['api', 'worker', 'compressor']) {
          if (Number.isSafeInteger(counts[service]) && actual[service] < counts[service]) {
            issue('evidence.observability.observed_replicas',
              `observed ${actual[service]} ${service} replicas; expected at least ${counts[service]}`)
          }
        }
      }
    }
    const dashboard = (value, path, expectedUid) => {
      if (!obj(value, path, ['uid', 'provider_evidence'])) return
      exact(value.uid, `${path}.uid`, expectedUid)
      exact(value.provider_evidence?.provider, `${path}.provider_evidence.provider`, observability.provider)
      const receiptAt = evidenceRef(value.provider_evidence, `${path}.provider_evidence`)
      ordered(checkedAt, receiptAt, `${path}.provider_evidence.captured_at`, true)
    }
    dashboard(observability.overview_dashboard, 'evidence.observability.overview_dashboard',
      'brevitas-enterprise-overview')
    dashboard(observability.per_replica_dashboard, 'evidence.observability.per_replica_dashboard',
      'brevitas-per-replica-health')
    exact(observability.telemetry_evidence?.provider,
      'evidence.observability.telemetry_evidence.provider', observability.provider)
    const telemetryReceiptAt = evidenceRef(
      observability.telemetry_evidence, 'evidence.observability.telemetry_evidence')
    ordered(checkedAt, telemetryReceiptAt, 'evidence.observability.telemetry_evidence.captured_at', true)
  }

  const alert = document.alert_delivery
  if (obj(alert, 'evidence.alert_delivery', [
    'provider', 'test_alert_id', 'route_id', 'destination_type', 'sent_at', 'delivered_at',
    'acknowledged_at', 'delivery_receipt',
  ])) {
    provider(alert.provider, 'evidence.alert_delivery.provider')
    string(alert.test_alert_id, 'evidence.alert_delivery.test_alert_id', SAFE_ID)
    string(alert.route_id, 'evidence.alert_delivery.route_id', SAFE_ID)
    if (!['paging', 'incident-management'].includes(alert.destination_type)) {
      issue('evidence.alert_delivery.destination_type', 'must be paging or incident-management')
    }
    const sentAt = timestamp(alert.sent_at, 'evidence.alert_delivery.sent_at')
    const deliveredAt = timestamp(alert.delivered_at, 'evidence.alert_delivery.delivered_at')
    const acknowledgedAt = timestamp(alert.acknowledged_at, 'evidence.alert_delivery.acknowledged_at')
    fresh(acknowledgedAt, 'evidence.alert_delivery.acknowledged_at', MAX_AGES.alert)
    ordered(sentAt, deliveredAt, 'evidence.alert_delivery.delivered_at', true)
    ordered(deliveredAt, acknowledgedAt, 'evidence.alert_delivery.acknowledged_at', true)
    exact(alert.delivery_receipt?.provider, 'evidence.alert_delivery.delivery_receipt.provider', alert.provider)
    const receiptAt = evidenceRef(alert.delivery_receipt, 'evidence.alert_delivery.delivery_receipt')
    ordered(acknowledgedAt, receiptAt, 'evidence.alert_delivery.delivery_receipt.captured_at', true)
  }

  const onCall = document.on_call
  if (obj(onCall, 'evidence.on_call', [
    'provider', 'schedule_id', 'escalation_policy_id', 'primary_owner_role', 'secondary_owner_role',
    'escalation_owner_role', 'checked_at', 'provider_evidence',
  ])) {
    provider(onCall.provider, 'evidence.on_call.provider')
    string(onCall.schedule_id, 'evidence.on_call.schedule_id', SAFE_ID)
    string(onCall.escalation_policy_id, 'evidence.on_call.escalation_policy_id', SAFE_ID)
    const roles = ['primary_owner_role', 'secondary_owner_role', 'escalation_owner_role']
    for (const role of roles) string(onCall[role], `evidence.on_call.${role}`, SAFE_ROLE, 'must be a role, not a person')
    if (new Set(roles.map(role => onCall[role])).size !== roles.length) {
      issue('evidence.on_call', 'primary, secondary, and escalation owner roles must be distinct')
    }
    const checkedAt = timestamp(onCall.checked_at, 'evidence.on_call.checked_at')
    fresh(checkedAt, 'evidence.on_call.checked_at', MAX_AGES.onCall)
    exact(onCall.provider_evidence?.provider, 'evidence.on_call.provider_evidence.provider', onCall.provider)
    const receiptAt = evidenceRef(onCall.provider_evidence, 'evidence.on_call.provider_evidence')
    ordered(checkedAt, receiptAt, 'evidence.on_call.provider_evidence.captured_at', true)
  }

  const rollback = document.rollback
  if (obj(rollback, 'evidence.rollback', [
    'rehearsal_id', 'rehearsal_environment', 'release_target_environment', 'candidate_build_sha',
    'rollback_build_sha', 'candidate_deployed_at', 'started_at', 'rollback_completed_at',
    'service_verified_at', 'result', 'provider_evidence',
  ])) {
    string(rollback.rehearsal_id, 'evidence.rollback.rehearsal_id', SAFE_ID)
    exact(rollback.rehearsal_environment, 'evidence.rollback.rehearsal_environment', 'staging')
    exact(rollback.release_target_environment, 'evidence.rollback.release_target_environment', expectedEnvironment)
    exact(rollback.candidate_build_sha, 'evidence.rollback.candidate_build_sha', expectedBuildSha)
    string(rollback.rollback_build_sha, 'evidence.rollback.rollback_build_sha', BUILD_SHA)
    if (rollback.rollback_build_sha === expectedBuildSha) {
      issue('evidence.rollback.rollback_build_sha', 'must differ from the candidate build SHA')
    }
    const deployedAt = timestamp(rollback.candidate_deployed_at, 'evidence.rollback.candidate_deployed_at')
    const startedAt = timestamp(rollback.started_at, 'evidence.rollback.started_at')
    const completedAt = timestamp(rollback.rollback_completed_at, 'evidence.rollback.rollback_completed_at')
    const verifiedAt = timestamp(rollback.service_verified_at, 'evidence.rollback.service_verified_at')
    fresh(verifiedAt, 'evidence.rollback.service_verified_at', MAX_AGES.rollback)
    ordered(deployedAt, startedAt, 'evidence.rollback.started_at', true)
    ordered(startedAt, completedAt, 'evidence.rollback.rollback_completed_at', true)
    ordered(completedAt, verifiedAt, 'evidence.rollback.service_verified_at', true)
    exact(rollback.result, 'evidence.rollback.result', 'passed')
    const receiptAt = evidenceRef(rollback.provider_evidence, 'evidence.rollback.provider_evidence')
    ordered(verifiedAt, receiptAt, 'evidence.rollback.provider_evidence.captured_at', true)
  }

  if (!Array.isArray(document.approvals) || document.approvals.length < 2) {
    issue('evidence.approvals', 'requires distinct operations_lead and security_lead approvals')
  } else {
    const requiredRoles = new Set(['operations_lead', 'security_lead'])
    const approverIds = new Set()
    for (const [index, approval] of document.approvals.entries()) {
      const path = `evidence.approvals[${index}]`
      if (!obj(approval, path, ['role', 'approver_id', 'approved_at', 'approval_evidence'])) continue
      string(approval.role, `${path}.role`, SAFE_ROLE)
      requiredRoles.delete(approval.role)
      if (string(approval.approver_id, `${path}.approver_id`, SAFE_ID, 'must be an opaque approver ID')) {
        if (approverIds.has(approval.approver_id)) issue(`${path}.approver_id`, 'must be unique')
        approverIds.add(approval.approver_id)
      }
      const approvedAt = timestamp(approval.approved_at, `${path}.approved_at`)
      const receiptAt = evidenceRef(approval.approval_evidence, `${path}.approval_evidence`)
      ordered(approvedAt, receiptAt, `${path}.approval_evidence.captured_at`, true)
    }
    for (const role of requiredRoles) issue('evidence.approvals', `missing ${role} approval`)
  }

  const cutoffPaths = [
    'evidence.backup.pitr.checked_at',
    'evidence.backup.logical.completed_at',
    'evidence.restore.completed_at',
    'evidence.observability.checked_at',
    'evidence.alert_delivery.acknowledged_at',
    'evidence.on_call.checked_at',
    'evidence.rollback.service_verified_at',
  ]
  const cutoffDates = cutoffPaths.map(path => timestamps.get(path)).filter(Boolean)
  const approvalCutoff = cutoffDates.length
    ? new Date(Math.max(...cutoffDates.map(date => date.getTime())))
    : null
  if (approvalCutoff && Array.isArray(document.approvals)) {
    for (const index of document.approvals.keys()) {
      ordered(approvalCutoff, timestamps.get(`evidence.approvals[${index}].approved_at`),
        `evidence.approvals[${index}].approved_at`, true)
    }
  }

  if (generatedAt) {
    for (const [path, date] of timestamps) {
      if (path !== 'evidence.generated_at' && date > generatedAt) {
        issue(path, 'must not be later than evidence.generated_at')
      }
    }
  }

  if (issues.length) throw new OperationalEvidenceError([...new Set(issues)])
  return Object.freeze({
    environment: expectedEnvironment,
    buildSha: expectedBuildSha,
    evidenceId: document.evidence_id,
    achievedRpoMinutes,
    internalRestoreMinutes,
    serviceRtoMinutes,
  })
}

function parseArguments(argv) {
  const result = {}
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index]
    const value = argv[index + 1]
    if (!['--environment', '--build-sha', '--evidence-file', '--evidence-env'].includes(key) || !value) {
      throw new Error('Usage: operational-readiness.mjs --environment staging|production --build-sha SHA (--evidence-file FILE | --evidence-env ENV_NAME)')
    }
    if (key in result) throw new Error(`Duplicate argument ${key}`)
    result[key] = value
  }
  if (!result['--environment'] || !result['--build-sha']) throw new Error('Environment and build SHA are required')
  if (Boolean(result['--evidence-file']) === Boolean(result['--evidence-env'])) {
    throw new Error('Exactly one of --evidence-file or --evidence-env is required')
  }
  return result
}

function loadDocument(args) {
  let raw
  if (args['--evidence-file']) {
    const path = resolve(args['--evidence-file'])
    const stat = lstatSync(path)
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('Evidence file must be a regular non-symlink file')
    if (stat.size > 262_144) throw new Error('Evidence file exceeds 256 KiB')
    raw = readFileSync(path, 'utf8')
  } else {
    const envName = args['--evidence-env']
    if (!/^[A-Z][A-Z0-9_]{2,63}$/.test(envName)) throw new Error('Evidence environment variable name is invalid')
    raw = process.env[envName]
    if (!raw) throw new Error(`Evidence environment variable ${envName} is missing or empty`)
    if (Buffer.byteLength(raw, 'utf8') > 262_144) throw new Error('Evidence document exceeds 256 KiB')
  }
  try {
    return JSON.parse(raw)
  } catch {
    throw new Error('Evidence document is not valid JSON')
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = parseArguments(process.argv.slice(2))
    const document = loadDocument(args)
    const result = validateOperationalEvidence(document, {
      environment: args['--environment'],
      buildSha: args['--build-sha'],
    })
    console.log(
      `Operational readiness passed for ${result.environment} build ${result.buildSha}: ` +
      `RPO ${result.achievedRpoMinutes.toFixed(2)}m, internal restore ${result.internalRestoreMinutes.toFixed(2)}m, ` +
      `service RTO ${result.serviceRtoMinutes.toFixed(2)}m`,
    )
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error))
    process.exitCode = 1
  }
}
