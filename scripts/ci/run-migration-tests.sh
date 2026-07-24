#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo 'DATABASE_URL is required for the ephemeral migration test.' >&2
  exit 2
fi

node scripts/ci/verify-migrations.mjs
node scripts/ci/validate-migration-dsn.mjs

# The first database session is forced read-only. Parsing the URI is necessary
# but not sufficient: verify the server actually reached is loopback before DDL.
server_address="$({ PGOPTIONS='-c default_transaction_read_only=on' \
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --tuples-only --no-align \
    --command 'select pg_catalog.host(pg_catalog.inet_server_addr())'; } | tr -d '[:space:]')"
case "${server_address}" in
  127.0.0.1|::1) ;;
  *)
    echo 'Migration integration tests refuse a database server not reached over loopback.' >&2
    exit 2
    ;;
esac

fresh_migrations=()
while IFS= read -r migration; do
  fresh_migrations+=("${migration}")
done < <(
  sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' \
    scripts/ci/migration-fresh-manifest.txt
)
upgrade_migrations=()
while IFS= read -r migration; do
  upgrade_migrations+=("${migration}")
done < <(
  sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' \
    scripts/ci/migration-upgrade-manifest.txt
)
if [[ "${#fresh_migrations[@]}" -ne 46 || "${#upgrade_migrations[@]}" -ne 34 ]]; then
  echo 'Migration manifests differ from the verified 46-file fresh / 34-file upgrade contract.' >&2
  exit 1
fi
baseline_count=$((${#fresh_migrations[@]} - ${#upgrade_migrations[@]}))
device_migration="${upgrade_migrations[9]}"
membership_migration="${upgrade_migrations[10]}"
receipt_migration="${upgrade_migrations[11]}"
selection_migration="${upgrade_migrations[12]}"
webhook_migration="${upgrade_migrations[13]}"
waitlist_migration="${upgrade_migrations[14]}"
billing_owner_migration="${upgrade_migrations[15]}"
stripe_ordering_migration="${upgrade_migrations[16]}"
initial_service_key_migration="${upgrade_migrations[17]}"
company_billing_migration="${upgrade_migrations[18]}"
billing_recovery_scope_migration="${upgrade_migrations[19]}"
provider_cleanup_migration="${upgrade_migrations[20]}"
multitab_sessions_migration="${upgrade_migrations[21]}"
shared_limits_migration="${upgrade_migrations[22]}"
compliance_billing_isolation_migration="${upgrade_migrations[23]}"
webhook_lease_renewal_migration="${upgrade_migrations[24]}"
billing_control_limits_migration="${upgrade_migrations[25]}"
checkout_reservation_migration="${upgrade_migrations[26]}"
provider_outbound_migration="${upgrade_migrations[27]}"
durable_onboarding_migration="${upgrade_migrations[28]}"
billing_customer_owner_migration="${upgrade_migrations[29]}"
workspace_experiences_migration="${upgrade_migrations[30]}"
split_savings_migration="${upgrade_migrations[31]}"
service_role_data_plane_migration="${upgrade_migrations[32]}"
supabase_advisor_hardening_migration="${upgrade_migrations[33]}"
if [[ "${device_migration}" != 'supabase/migrations/202607170010_device_delivery_idempotency.sql' \
   || "${membership_migration}" != 'supabase/migrations/202607170011_active_memberships.sql' \
   || "${receipt_migration}" != 'supabase/migrations/202607170012_receipt_accounting_alignment.sql' \
   || "${selection_migration}" != 'supabase/migrations/202607170013_active_company_selection.sql' \
   || "${webhook_migration}" != 'supabase/migrations/202607200001_stripe_webhook_durability.sql' \
   || "${waitlist_migration}" != 'supabase/migrations/202607200002_waitlist_security.sql' \
   || "${billing_owner_migration}" != 'supabase/migrations/202607200003_billing_owner_attribution.sql' \
   || "${stripe_ordering_migration}" != 'supabase/migrations/202607200004_stripe_event_ordering.sql' \
   || "${initial_service_key_migration}" != 'supabase/migrations/202607200005_initial_service_key.sql' \
   || "${company_billing_migration}" != 'supabase/migrations/202607200006_company_billing_authorization.sql' \
   || "${billing_recovery_scope_migration}" != 'supabase/migrations/202607200007_billing_recovery_scope.sql' \
   || "${provider_cleanup_migration}" != 'supabase/migrations/202607200008_provider_credential_cleanup.sql' \
   || "${multitab_sessions_migration}" != 'supabase/migrations/202607200009_multitab_dashboard_sessions.sql' \
   || "${shared_limits_migration}" != 'supabase/migrations/202607200010_shared_endpoint_rate_limits.sql' \
   || "${compliance_billing_isolation_migration}" != 'supabase/migrations/202607200011_compliance_billing_isolation.sql' \
   || "${webhook_lease_renewal_migration}" != 'supabase/migrations/202607200012_stripe_webhook_lease_renewal.sql' \
   || "${billing_control_limits_migration}" != 'supabase/migrations/202607200013_billing_control_rate_limits.sql' \
   || "${checkout_reservation_migration}" != 'supabase/migrations/202607200014_billing_checkout_session_reservations.sql' \
   || "${provider_outbound_migration}" != 'supabase/migrations/202607200015_provider_outbound_ambiguity.sql' \
   || "${durable_onboarding_migration}" != 'supabase/migrations/202607200016_durable_onboarding.sql' \
   || "${billing_customer_owner_migration}" != 'supabase/migrations/202607200017_billing_customer_owner_fencing.sql' \
   || "${workspace_experiences_migration}" != 'supabase/migrations/202607200018_workspace_experiences.sql' \
   || "${split_savings_migration}" != 'supabase/migrations/20260720_split_savings_metrics.sql' \
   || "${service_role_data_plane_migration}" != 'supabase/migrations/202607220001_service_role_data_plane.sql' \
   || "${supabase_advisor_hardening_migration}" != 'supabase/migrations/202607220002_supabase_advisor_hardening.sql' ]]; then
  echo 'Frozen migrations 010-013 or the 20260720 forward suffix are out of order.' >&2
  exit 1
fi

apply_migration() {
  local migration="$1"
  echo "Applying ${migration}"
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --file "${migration}"
}

assert_atomic_migration_rollback() {
  local migration="$1"
  local rollback_query="$2"
  local failure_file failure_log rollback_state
  failure_file="$(mktemp)"
  failure_log="$(mktemp)"
  awk '
    {
      normalized=tolower($0)
      gsub(/[[:space:]]/, "", normalized)
      if (normalized == "commit;") {
        print "select 1/0;"
        injected=1
      }
      print
    }
    END { if (!injected) exit 42 }
  ' "${migration}" >"${failure_file}"
  if psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file "${failure_file}" >"${failure_log}" 2>&1; then
    echo "Failure-injected migration unexpectedly committed: ${migration}" >&2
    return 1
  fi
  if ! grep -q 'division by zero' "${failure_log}"; then
    echo "Failure-injected migration did not reach the pre-COMMIT fault: ${migration}" >&2
    sed -n '1,120p' "${failure_log}" >&2
    return 1
  fi
  rollback_state="$({ PGOPTIONS='-c default_transaction_read_only=on' \
    psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
      --tuples-only --no-align --command "select (${rollback_query})"; } | tr -d '[:space:]')"
  if [[ "${rollback_state}" != 't' ]]; then
    echo "Failure-injected migration left partial state: ${migration}" >&2
    return 1
  fi
  rm -f -- "${failure_file}" "${failure_log}"
}

bootstrap_database() {
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-bootstrap.sql
}

run_forward_assertions() {
  psql "${DATABASE_URL}" --no-psqlrc --file scripts/ci/migration-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --file scripts/ci/migration-cache-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --file scripts/ci/migration-key-audit-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --file scripts/dr/compliance-workflow-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-device-membership-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-receipt-accounting-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-active-company-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-waitlist-security-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-initial-service-key-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-company-billing-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-billing-recovery-scope-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-provider-credential-cleanup-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-multitab-dashboard-session-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/waitlist-shared-rate-limit-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/billing-recovery-shared-rate-limit-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-compliance-billing-isolation-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/billing-control-shared-rate-limit-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-checkout-session-reservation-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-supabase-advisor-hardening-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-provider-outbound-ambiguity-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-durable-onboarding-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc \
    --file scripts/ci/migration-billing-customer-owner-fencing-assertions.sql
}

echo 'Testing the known production-baseline upgrade through migration 017.'
bootstrap_database
for ((index = 0; index < baseline_count; index++)); do
  apply_migration "${fresh_migrations[${index}]}"
done
apply_migration scripts/ci/migration-upgrade-baseline-fixture.sql

prerequisite_log="$(mktemp)"
if psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file supabase/migrations/202607170006_database_scaling.sql \
  >"${prerequisite_log}" 2>&1; then
  echo 'Database-scaling migration unexpectedly succeeded without enterprise tenancy.' >&2
  exit 1
fi
if ! grep -q 'requires Supabase migrations through 202607170003 first' "${prerequisite_log}"; then
  echo 'Database-scaling prerequisite failure did not report its ordering contract.' >&2
  sed -n '1,120p' "${prerequisite_log}" >&2
  exit 1
fi

apply_migration "${upgrade_migrations[0]}"
apply_migration scripts/ci/migration-cache-legacy-fixture.sql
echo 'Testing the retired API cache compatibility guard outside the forward manifests.'
apply_migration api/migrations/002_semantic_cache.sql
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-cache-guard-assertions.sql
apply_migration "${upgrade_migrations[1]}"
apply_migration "${upgrade_migrations[2]}"

echo 'Pre-staging upgrade indexes outside a transaction before migration 006.'
apply_migration api/migrations/004_database_scaling.concurrent_indexes.sql
for ((index = 3; index < 9; index++)); do
  apply_migration "${upgrade_migrations[${index}]}"
done
apply_migration "${device_migration}"
apply_migration scripts/ci/migration-device-null-upgrade-fixture.sql
echo 'Reapplying migration 010 to erase pre-constraint quarantined ciphertext.'
apply_migration "${device_migration}"
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-device-null-upgrade-assertions.sql
echo 'Applying migrations 011-013 twice to prove idempotence.'
apply_migration "${membership_migration}"
apply_migration "${membership_migration}"
apply_migration "${receipt_migration}"
apply_migration "${receipt_migration}"
apply_migration "${selection_migration}"
apply_migration "${selection_migration}"
echo 'Applying migrations 202607200001-200002 twice to prove idempotence.'
assert_atomic_migration_rollback "${webhook_migration}" \
  "to_regprocedure('public.claim_stripe_webhook_event(text,text,uuid,integer)') is null and not exists (select 1 from information_schema.columns where table_schema='public' and table_name='stripe_webhook_events' and column_name='status')"
apply_migration "${webhook_migration}"
apply_migration "${webhook_migration}"
assert_atomic_migration_rollback "${waitlist_migration}" \
  "to_regprocedure('public.submit_waitlist_signup(text,text,text,text,text,text,text,text,boolean)') is null"
apply_migration "${waitlist_migration}"
apply_migration "${waitlist_migration}"
echo 'Applying migrations 202607200003 through the complete release suffix in order.'
assert_atomic_migration_rollback "${billing_owner_migration}" \
  "to_regprocedure('public.enforce_service_key_billing_owner()') is null"
apply_migration "${billing_owner_migration}"
apply_migration "${billing_owner_migration}"
billing_migration_expected_host="$(node --input-type=module -e \
  "import { validateMigrationDsn } from './scripts/ci/validate-migration-dsn.mjs'; console.log(validateMigrationDsn(process.env.DATABASE_URL).hostname)")"
billing_migration_expected_database="$(node --input-type=module -e \
  "import { validateMigrationDsn } from './scripts/ci/validate-migration-dsn.mjs'; console.log(validateMigrationDsn(process.env.DATABASE_URL).database)")"
billing_version_fixture="${PWD}/scripts/ci/billing-maintenance-version-fetch-fixture.mjs"
run_billing_identity_maintenance() {
  NODE_OPTIONS="--import=${billing_version_fixture}" \
  BREVITAS_BILLING_ENABLED=false \
  BREVITAS_BILLING_MIGRATION_PHASE=api-worker-quiesced \
  BREVITAS_BILLING_MAINTENANCE_SHA=0000000000000000000000000000000000000000 \
  BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL=https://dashboard.example.invalid/api/version \
  BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL=http://127.0.0.1:43119/version \
  BREVITAS_BILLING_MAINTENANCE_OFFLINE_LOOPBACK_TEST=true \
  BREVITAS_BILLING_MIGRATION_EXPECTED_HOST="${billing_migration_expected_host}" \
  BREVITAS_BILLING_MIGRATION_EXPECTED_DATABASE="${billing_migration_expected_database}" \
    bash scripts/ci/apply-billing-identity-migrations.sh
}
echo 'Applying the guarded 200004-200006 billing maintenance procedure immediately after 200003.'
run_billing_identity_maintenance
echo 'Rerunning the guarded procedure to prove completed company-scoped state is validated and skipped.'
run_billing_identity_maintenance

billing_identity_complete_query="to_regprocedure('public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],timestamptz,text)') is not null and to_regprocedure('public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text)') is not null and to_regprocedure('public.company_billing_authorize_actor(uuid)') is not null and (select procedure_state.proargnames[1] from pg_proc procedure_state where procedure_state.oid=to_regprocedure('public.compare_and_set_stripe_subscription_snapshot(uuid,bigint,bigint,text,text,text,text,timestamptz,timestamptz,timestamptz)'))='p_organization_id' and (select procedure_state.proargnames[1] from pg_proc procedure_state where procedure_state.oid=to_regprocedure('public.compare_and_set_stripe_invoice_snapshot(uuid,bigint,bigint,text,text,text,text)'))='p_organization_id' and exists (select 1 from pg_constraint constraint_state where constraint_state.conrelid='public.billing_accounts'::regclass and constraint_state.contype='p' and pg_get_constraintdef(constraint_state.oid)='PRIMARY KEY (organization_id)')"
echo 'Failure-injecting reruns of 200004-200006 and requiring the completed company identity to roll back intact.'
assert_atomic_migration_rollback "${stripe_ordering_migration}" \
  "${billing_identity_complete_query}"
assert_atomic_migration_rollback "${initial_service_key_migration}" \
  "${billing_identity_complete_query}"
assert_atomic_migration_rollback "${company_billing_migration}" \
  "${billing_identity_complete_query}"
assert_atomic_migration_rollback "${billing_recovery_scope_migration}" \
  "to_regprocedure('public.manually_resolve_billing_ledger_entry(bigint,text,text)') is not null and to_regprocedure('public.manually_resolve_billing_ledger_entry(uuid,uuid,bigint,text,text,text)') is null and to_regclass('public.billing_recovery_audit') is null"
apply_migration "${billing_recovery_scope_migration}"
apply_migration "${billing_recovery_scope_migration}"
assert_atomic_migration_rollback "${provider_cleanup_migration}" \
  "to_regprocedure('public.purge_expired_provider_configs(integer)') is null and not exists (select 1 from pg_constraint where conrelid='public.provider_config'::regclass and conname='provider_config_key_hash_fkey')"
apply_migration "${provider_cleanup_migration}"
apply_migration "${provider_cleanup_migration}"
assert_atomic_migration_rollback "${multitab_sessions_migration}" \
  "position('rotated_count' in pg_get_functiondef(to_regprocedure('public.company_admin_create_dashboard_session_key(uuid,uuid,text,text,timestamptz,text)')))=0"
apply_migration "${multitab_sessions_migration}"
apply_migration "${multitab_sessions_migration}"
assert_atomic_migration_rollback "${shared_limits_migration}" \
  "to_regclass('public.shared_endpoint_rate_limits') is null and to_regprocedure('public.consume_billing_recovery_attempt(uuid,uuid)') is null and pg_get_function_result(to_regprocedure('public.submit_waitlist_signup(text,text,text,text,text,text,text,text,boolean)'))='boolean'"
apply_migration "${shared_limits_migration}"
apply_migration "${shared_limits_migration}"
assert_atomic_migration_rollback "${compliance_billing_isolation_migration}" \
  "not exists (select 1 from information_schema.columns where table_schema='public' and table_name='billing_events' and column_name='organization_id') and to_regprocedure('public.compliance_export_tenant_pre_company_identity(uuid,uuid,text)') is null and to_regprocedure('public.compliance_export_subject_pre_company_identity(uuid,uuid,text)') is null"
apply_migration "${compliance_billing_isolation_migration}"
apply_migration "${compliance_billing_isolation_migration}"
assert_atomic_migration_rollback "${webhook_lease_renewal_migration}" \
  "to_regprocedure('public.renew_stripe_webhook_event_lease(text,uuid,integer)') is null and position('lease_expires_at >' in pg_get_functiondef(to_regprocedure('public.mark_stripe_webhook_event_processed(text,uuid)')))=0"
apply_migration "${webhook_lease_renewal_migration}"
apply_migration "${webhook_lease_renewal_migration}"
assert_atomic_migration_rollback "${billing_control_limits_migration}" \
  "to_regprocedure('public.consume_billing_control_attempt(uuid,uuid,text)') is null"
apply_migration "${billing_control_limits_migration}"
apply_migration "${billing_control_limits_migration}"
assert_atomic_migration_rollback "${checkout_reservation_migration}" \
  "to_regclass('public.billing_checkout_reservations') is null and to_regprocedure('public.reserve_billing_checkout_generation(uuid,text,uuid,integer)') is null and to_regprocedure('public.persist_billing_checkout_session(uuid,bigint,uuid,text)') is null"
apply_migration "${checkout_reservation_migration}"
apply_migration "${checkout_reservation_migration}"
assert_atomic_migration_rollback "${provider_outbound_migration}" \
  "to_regprocedure('public.mark_ai_job_provider_outbound_started(uuid,text)') is null and not exists (select 1 from information_schema.columns where table_schema='public' and table_name='ai_jobs' and column_name='provider_outbound_started_at')"
apply_migration "${provider_outbound_migration}"
apply_migration "${provider_outbound_migration}"
assert_atomic_migration_rollback "${durable_onboarding_migration}" \
  "to_regprocedure('public.organization_onboarding_status(uuid,uuid)') is null and to_regprocedure('public.register_bvx_installation(uuid,text,uuid,text,text,text,text,text,text,text,text)') is null and not exists (select 1 from information_schema.columns where table_schema='public' and table_name='organizations' and column_name='onboarding_completed_at') and not exists (select 1 from information_schema.columns where table_schema='public' and table_name='installations' and column_name='registration_key_hash')"
apply_migration "${durable_onboarding_migration}"
apply_migration "${durable_onboarding_migration}"
assert_atomic_migration_rollback "${billing_customer_owner_migration}" \
  "to_regprocedure('public.save_billing_customer_identity(uuid,text)') is null"
apply_migration "${billing_customer_owner_migration}"
apply_migration "${billing_customer_owner_migration}"
assert_atomic_migration_rollback "${workspace_experiences_migration}" \
  "not exists (select 1 from information_schema.columns where table_schema='public' and table_name='organizations' and column_name='account_type') and to_regprocedure('public.ensure_workspace_organization(uuid,text,text)') is null"
apply_migration "${workspace_experiences_migration}"
apply_migration "${workspace_experiences_migration}"
assert_atomic_migration_rollback "${split_savings_migration}" \
  "not exists (select 1 from information_schema.columns where table_schema='public' and table_name='usage_log' and column_name='provider_input_tokens_avoided')"
apply_migration "${split_savings_migration}"
apply_migration "${split_savings_migration}"
assert_atomic_migration_rollback "${service_role_data_plane_migration}" \
  "not has_table_privilege('service_role','public.organizations','SELECT')"
apply_migration "${service_role_data_plane_migration}"
apply_migration "${service_role_data_plane_migration}"
assert_atomic_migration_rollback "${supabase_advisor_hardening_migration}" \
  "(select proconfig is null from pg_proc where oid='public.company_role_permissions(text)'::regprocedure) and has_function_privilege('anon','public.handle_new_user()','EXECUTE')"
apply_migration "${supabase_advisor_hardening_migration}"
apply_migration "${supabase_advisor_hardening_migration}"

psql "${DATABASE_URL}" --no-psqlrc --file scripts/ci/migration-upgrade-assertions.sql
run_forward_assertions

echo 'Racing shared billing-recovery attempts from independent PostgreSQL sessions.'
bash scripts/ci/run-billing-recovery-shared-limit-test.sh

echo 'Racing shared Checkout attempts from independent PostgreSQL sessions.'
bash scripts/ci/run-billing-control-shared-limit-test.sh

echo 'Racing billing-owner transfer against customer persistence.'
bash scripts/ci/run-billing-owner-transfer-race-test.sh

echo 'Racing bounded cache writes from independent PostgreSQL sessions.'
cache_pids=()
for cache_index in 101 102 103 104 105 106 107 108; do
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --set cache_index="${cache_index}" \
    --file scripts/ci/migration-cache-concurrent-write.sql &
  cache_pids+=("$!")
done
for cache_pid in "${cache_pids[@]}"; do
  wait "${cache_pid}"
done
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-cache-concurrency-assertions.sql

cache_before="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.semantic_cache')"
echo 'Rolling back and reapplying the encrypted cache RPC/constraint layer.'
apply_migration scripts/ci/migration-cache-rollback.sql
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-cache-rollback-assertions.sql
cache_after="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.semantic_cache')"
if [[ "${cache_after}" != "${cache_before}" ]]; then
  echo 'Cache rollback changed encrypted cache row count.' >&2
  exit 1
fi
apply_migration "${upgrade_migrations[1]}"
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-cache-reapply-assertions.sql
cache_after="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.semantic_cache')"
if [[ "${cache_after}" != "${cache_before}" ]]; then
  echo 'Cache reapply changed encrypted cache row count.' >&2
  exit 1
fi

usage_before="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.usage_log')"
billing_before="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.billing_ledger')"
audit_before="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.audit_events')"

assert_authoritative_counts() {
  local stage="$1"
  local usage_after billing_after audit_after
  usage_after="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.usage_log')"
  billing_after="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.billing_ledger')"
  audit_after="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.audit_events')"
  if [[ "${usage_after}" != "${usage_before}" || "${billing_after}" != "${billing_before}" || "${audit_after}" != "${audit_before}" ]]; then
    echo "Database-scaling ${stage} changed authoritative row counts." >&2
    exit 1
  fi
}

echo 'Rolling back only the database-scaling read path.'
apply_migration api/migrations/004_database_scaling.rollback.sql
psql "${DATABASE_URL}" --no-psqlrc --file scripts/ci/migration-rollback-assertions.sql
assert_authoritative_counts rollback

echo 'Reapplying the generated database-scaling migration.'
apply_migration "${upgrade_migrations[5]}"
psql "${DATABASE_URL}" --no-psqlrc --file scripts/ci/migration-reapply-assertions.sql
assert_authoritative_counts reapply

echo 'Rolling back only the receipt-accounting validation layer.'
apply_migration scripts/ci/migration-receipt-accounting-rollback.sql
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-receipt-accounting-rollback-assertions.sql
assert_authoritative_counts receipt-rollback
echo 'Reapplying the receipt-accounting alignment migration.'
apply_migration "${receipt_migration}"
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-receipt-accounting-assertions.sql
assert_authoritative_counts receipt-reapply

echo 'Resetting the loopback-only database for an isolated fresh install.'
psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file scripts/ci/migration-reset-database.sql
bootstrap_database

echo 'Testing the complete fresh Supabase migration chain.'
for migration in "${fresh_migrations[@]}"; do
  apply_migration "${migration}"
done
echo 'Reapplying frozen migrations 010-013 and the 20260720 forward suffix on the isolated fresh install.'
apply_migration "${device_migration}"
apply_migration "${membership_migration}"
apply_migration "${receipt_migration}"
apply_migration "${selection_migration}"
apply_migration "${webhook_migration}"
apply_migration "${waitlist_migration}"
apply_migration "${billing_owner_migration}"
apply_migration "${stripe_ordering_migration}"
apply_migration "${initial_service_key_migration}"
apply_migration "${company_billing_migration}"
apply_migration "${billing_recovery_scope_migration}"
apply_migration "${provider_cleanup_migration}"
apply_migration "${multitab_sessions_migration}"
apply_migration "${shared_limits_migration}"
apply_migration "${compliance_billing_isolation_migration}"
apply_migration "${webhook_lease_renewal_migration}"
apply_migration "${billing_control_limits_migration}"
apply_migration "${checkout_reservation_migration}"
apply_migration "${provider_outbound_migration}"
apply_migration "${durable_onboarding_migration}"
apply_migration "${billing_customer_owner_migration}"
apply_migration "${workspace_experiences_migration}"
apply_migration "${split_savings_migration}"
apply_migration "${service_role_data_plane_migration}"
apply_migration "${supabase_advisor_hardening_migration}"
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-cache-fresh-assertions.sql
run_forward_assertions

echo 'Ephemeral fresh-install and production-upgrade migration checks passed.'
