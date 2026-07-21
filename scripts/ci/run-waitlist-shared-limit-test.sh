#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo 'DATABASE_URL is required for the shared waitlist limiter test.' >&2
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
    echo 'Shared waitlist limiter tests require loopback PostgreSQL.' >&2
    exit 2
    ;;
esac

psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file supabase/migrations/202607200010_shared_endpoint_rate_limits.sql
psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --command \
  "delete from public.shared_endpoint_rate_limits;
   delete from public.waitlist where email='shared-limit-concurrent@example.invalid';"

result_directory="$(mktemp -d)"
cleanup() {
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --command \
    "delete from public.shared_endpoint_rate_limits;
     delete from public.waitlist where email like 'shared-limit-%@example.invalid';" \
    >/dev/null 2>&1 || true
  rm -r "${result_directory}"
}
trap cleanup EXIT

# Independent psql processes model separate Vercel instances racing the same
# normalized identity. PostgreSQL must admit exactly three across all sessions.
worker_pids=()
for worker_index in $(seq 1 12); do
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
    --command "select public.submit_waitlist_signup(
      'shared-limit-concurrent@example.invalid',null,null,null,null,null,null,null,false
    )->>'code';" >"${result_directory}/${worker_index}.txt" &
  worker_pids+=("$!")
done
for worker_pid in "${worker_pids[@]}"; do
  wait "${worker_pid}"
done

accepted_count="$(awk '$0 == "accepted" { count++ } END { print count + 0 }' "${result_directory}"/*.txt)"
limited_count="$(awk '$0 == "rate_limited" { count++ } END { print count + 0 }' "${result_directory}"/*.txt)"
if [[ "${accepted_count}" -ne 3 || "${limited_count}" -ne 9 ]]; then
  echo "Cross-instance limiter admitted ${accepted_count} and denied ${limited_count}; expected 3/9." >&2
  exit 1
fi

psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file scripts/ci/waitlist-shared-rate-limit-assertions.sql
