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
    --tuples-only --no-align --command 'select inet_server_addr()::text'; } | tr -d '[:space:]')"
case "${server_address}" in
  127.0.0.1|::1) ;;
  *)
    echo 'Migration integration tests refuse a database server not reached over loopback.' >&2
    exit 2
    ;;
esac

mapfile -t fresh_migrations < <(
  sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' \
    scripts/ci/migration-fresh-manifest.txt
)
mapfile -t upgrade_migrations < <(
  sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' \
    scripts/ci/migration-upgrade-manifest.txt
)
if [[ "${#fresh_migrations[@]}" -ne 25 || "${#upgrade_migrations[@]}" -ne 13 ]]; then
  echo 'Migration manifests differ from the verified 25-file fresh / 13-file upgrade contract.' >&2
  exit 1
fi
baseline_count=$((${#fresh_migrations[@]} - ${#upgrade_migrations[@]}))
device_migration="${upgrade_migrations[9]}"
membership_migration="${upgrade_migrations[10]}"
receipt_migration="${upgrade_migrations[11]}"
selection_migration="${upgrade_migrations[12]}"
if [[ "${device_migration}" != 'supabase/migrations/202607170010_device_delivery_idempotency.sql' \
   || "${membership_migration}" != 'supabase/migrations/202607170011_active_memberships.sql' \
   || "${receipt_migration}" != 'supabase/migrations/202607170012_receipt_accounting_alignment.sql' \
   || "${selection_migration}" != 'supabase/migrations/202607170013_active_company_selection.sql' ]]; then
  echo 'Frozen migrations 010-013 are not the final enterprise suffix.' >&2
  exit 1
fi

apply_migration() {
  local migration="$1"
  echo "Applying ${migration}"
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --file "${migration}"
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
}

echo 'Testing the known production-baseline upgrade through migration 013.'
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

psql "${DATABASE_URL}" --no-psqlrc --file scripts/ci/migration-upgrade-assertions.sql
run_forward_assertions

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
echo 'Reapplying frozen migrations 010-013 on the isolated fresh install.'
apply_migration "${device_migration}"
apply_migration "${membership_migration}"
apply_migration "${receipt_migration}"
apply_migration "${selection_migration}"
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-cache-fresh-assertions.sql
run_forward_assertions

echo 'Ephemeral fresh-install and production-upgrade migration checks passed.'
