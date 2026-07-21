#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo 'DATABASE_URL is required for the shared billing control limiter test.' >&2
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
    echo 'Shared billing control limiter tests require loopback PostgreSQL.' >&2
    exit 2
    ;;
esac

psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file supabase/migrations/202607200013_billing_control_rate_limits.sql
psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --command \
  "delete from public.shared_endpoint_rate_limits
    where endpoint_scope like 'billing_control.%';"

result_directory="$(mktemp -d)"
cleanup() {
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --command \
    "delete from public.shared_endpoint_rate_limits
      where endpoint_scope like 'billing_control.%';" \
    >/dev/null 2>&1 || true
  rm -r "${result_directory}"
}
trap cleanup EXIT

# Independent sessions model separate Vercel instances. PostgreSQL must admit
# exactly five Checkout attempts for one verified actor/company/operation.
worker_pids=()
for worker_index in $(seq 1 12); do
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
    --command "select public.consume_billing_control_attempt(
      'bc100000-0000-4000-8000-000000000001',
      'bc200000-0000-4000-8000-000000000001',
      'checkout'
    )->>'code';" >"${result_directory}/${worker_index}.txt" &
  worker_pids+=("$!")
done
for worker_pid in "${worker_pids[@]}"; do
  wait "${worker_pid}"
done

accepted_count="$(awk '$0 == "accepted" { count++ } END { print count + 0 }' "${result_directory}"/*.txt)"
limited_count="$(awk '$0 == "rate_limited" { count++ } END { print count + 0 }' "${result_directory}"/*.txt)"
if [[ "${accepted_count}" -ne 5 || "${limited_count}" -ne 7 ]]; then
  echo "Cross-instance billing control limiter admitted ${accepted_count} and denied ${limited_count}; expected 5/7." >&2
  exit 1
fi

psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file scripts/ci/billing-control-shared-rate-limit-assertions.sql
