#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo 'DATABASE_URL is required for the billing owner-transfer race test.' >&2
  exit 2
fi

node scripts/ci/validate-migration-dsn.mjs
server_address="$({ PGOPTIONS='-c default_transaction_read_only=on' \
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --tuples-only --no-align \
    --command 'select pg_catalog.host(pg_catalog.inet_server_addr())'; } | tr -d '[:space:]')"
case "${server_address}" in
  127.0.0.1|::1) ;;
  *)
    echo 'Billing owner-transfer race tests require loopback PostgreSQL.' >&2
    exit 2
    ;;
esac

psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file supabase/migrations/202607200017_billing_customer_owner_fencing.sql
psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file scripts/ci/billing-owner-transfer-race-setup.sql

result_directory="$(mktemp -d)"
persistence_pid=''
cleanup() {
  if [[ -n "${persistence_pid}" ]] && kill -0 "${persistence_pid}" 2>/dev/null; then
    kill "${persistence_pid}" 2>/dev/null || true
    wait "${persistence_pid}" 2>/dev/null || true
  fi
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --command "delete from public.billing_checkout_reservations
      where organization_id='cf300000-0000-4000-8000-000000000001';
      delete from public.billing_accounts
      where organization_id='cf300000-0000-4000-8000-000000000001';
      delete from public.organizations
      where id='cf300000-0000-4000-8000-000000000001';
      delete from auth.users where id in (
        'cf200000-0000-4000-8000-000000000001',
        'cf200000-0000-4000-8000-000000000002'
      );" >/dev/null 2>&1 || true
  rm -r "${result_directory}"
}
trap cleanup EXIT

# The advisory lock is only a test barrier. It is acquired after the RPC has
# returned, while the transaction still retains the organization/member locks.
psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --command "begin;
    select public.save_billing_customer_identity(
      'cf300000-0000-4000-8000-000000000001',
      'cus_owner_transfer_race'
    );
    select pg_catalog.pg_advisory_lock(170017);
    select pg_catalog.pg_sleep(4);
    select pg_catalog.pg_advisory_unlock(170017);
    commit;" >"${result_directory}/persistence.txt" 2>&1 &
persistence_pid="$!"

barrier_observed=false
for _barrier_attempt in $(seq 1 40); do
  barrier_count="$(psql "${DATABASE_URL}" --no-psqlrc \
    --set ON_ERROR_STOP=1 --tuples-only --no-align \
    --command "select count(*) from pg_catalog.pg_locks
      where locktype='advisory' and classid=0 and objid=170017
        and granted and pid<>pg_catalog.pg_backend_pid();" | tr -d '[:space:]')"
  if [[ "${barrier_count}" == '1' ]]; then
    barrier_observed=true
    break
  fi
  if ! kill -0 "${persistence_pid}" 2>/dev/null; then
    echo 'Customer persistence exited before establishing the race barrier.' >&2
    sed -n '1,120p' "${result_directory}/persistence.txt" >&2
    exit 1
  fi
  sleep 0.1
done
if [[ "${barrier_observed}" != true ]]; then
  echo 'Customer persistence did not establish the owner-transfer race barrier.' >&2
  exit 1
fi

# Transfer must block while customer persistence owns the organization lock.
if psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --command "set lock_timeout='750ms';
    update public.organizations
       set billing_owner_id='cf200000-0000-4000-8000-000000000002'
     where id='cf300000-0000-4000-8000-000000000001';" \
  >"${result_directory}/blocked-transfer.txt" 2>&1; then
  echo 'Owner transfer bypassed the customer-persistence organization lock.' >&2
  exit 1
fi
if ! grep -q 'canceling statement due to lock timeout' \
  "${result_directory}/blocked-transfer.txt"; then
  echo 'Owner transfer failed for a reason other than persistence serialization.' >&2
  sed -n '1,120p' "${result_directory}/blocked-transfer.txt" >&2
  exit 1
fi

wait "${persistence_pid}"
persistence_pid=''

# Once persistence commits, transfer proceeds and its existing account-snapshot
# trigger updates only user attribution; company/customer/ledger identity stays.
psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --command "update public.organizations
    set billing_owner_id='cf200000-0000-4000-8000-000000000002'
    where id='cf300000-0000-4000-8000-000000000001';"
psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file scripts/ci/billing-owner-transfer-race-assertions.sql
