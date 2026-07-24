#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [[ -z "${DATABASE_URL:-}" || -z "${RESTORE_DATABASE_URL:-}" ]]; then
  echo 'DATABASE_URL and RESTORE_DATABASE_URL are required.' >&2
  exit 2
fi
node scripts/ci/validate-migration-dsn.mjs
DATABASE_URL="${RESTORE_DATABASE_URL}" node scripts/ci/validate-migration-dsn.mjs

for client in psql pg_dump pg_restore; do
  version="$(${client} --version 2>/dev/null)" \
    || { echo "Unable to identify PostgreSQL client: ${client}" >&2; exit 2; }
  if [[ "${version}" =~ PostgreSQL\)[[:space:]]+([0-9]+) ]] \
      && [[ "${BASH_REMATCH[1]}" == "16" ]]; then
    continue
  fi
  echo "${client} major version must be 16 for the PostgreSQL 16 restore contract." >&2
  exit 2
done

assert_loopback_server() {
  local dsn="$1"
  local address
  address="$({ PGOPTIONS='-c default_transaction_read_only=on' \
    psql "${dsn}" --no-psqlrc --set ON_ERROR_STOP=1 \
      --tuples-only --no-align \
      --command 'select pg_catalog.host(pg_catalog.inet_server_addr())'; } | tr -d '[:space:]')"
  case "${address}" in
    127.0.0.1|::1) ;;
    *) echo 'Restore integration refuses a server not reached over loopback.' >&2; exit 2 ;;
  esac
}
assert_loopback_server "${DATABASE_URL}"
assert_loopback_server "${RESTORE_DATABASE_URL}"

source_database="$(psql "${DATABASE_URL}" --no-psqlrc -At --command 'select current_database()')"
target_database="$(psql "${RESTORE_DATABASE_URL}" --no-psqlrc -At --command 'select current_database()')"
if [[ -z "${source_database}" || -z "${target_database}" || "${source_database}" == "${target_database}" ]]; then
  echo 'Restore integration requires distinct named source and target databases.' >&2
  exit 2
fi

workdir="$(mktemp -d "${TMPDIR:-/tmp}/brevitas-restore-ci.XXXXXX")"
workdir="$(cd "${workdir}" && pwd -P)"
cleanup() { rm -rf -- "${workdir}"; }
trap cleanup EXIT INT TERM
dump="${workdir}/migration-source.dump"
counts="${workdir}/raw-counts.tsv"
manifest="${workdir}/migration-source.manifest.json"
artifact="${workdir}/migration-source.deletions.json"
evidence_dir="${workdir}/evidence"
mkdir -p -m 700 "${evidence_dir}"

pg_dump "${DATABASE_URL}" --format=custom --no-owner --no-privileges \
  --schema=public --schema=auth --file="${dump}"

table_names="$(psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 -At -F $'\t' --command \
  "select namespace.nspname, relation.relname from pg_class relation join pg_namespace namespace on namespace.oid=relation.relnamespace where relation.relkind in ('r','p') and namespace.nspname in ('public','auth') order by 1,2")"
while IFS=$'\t' read -r schema_name table_name; do
  [[ -n "${schema_name}" && -n "${table_name}" ]] || continue
  [[ "${schema_name}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && "${table_name}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
    || { echo 'Unsafe table identifier in restore inventory.' >&2; exit 1; }
  row_count="$(psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 -At --command \
    "select count(*) from \"${schema_name}\".\"${table_name}\"")"
  [[ "${row_count}" =~ ^[0-9]+$ ]] || { echo 'Invalid restore inventory row count.' >&2; exit 1; }
  printf '%s\t%s\t%s\n' "${schema_name}" "${table_name}" "${row_count}" >> "${counts}"
done <<< "${table_names}"
[[ -s "${counts}" ]] || { echo 'Restore inventory is empty.' >&2; exit 1; }

dump_sha256="$(sha256sum "${dump}" | awk '{print $1}')"
dump_bytes="$(wc -c < "${dump}" | tr -d '[:space:]')"
created_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
python3 - "${manifest}" "${dump}" "${dump_sha256}" "${dump_bytes}" "${created_at}" "${counts}" <<'PY'
import json, pathlib, sys
manifest, dump, digest, size, created, counts = sys.argv[1:]
tables=[]
for line in pathlib.Path(counts).read_text().splitlines():
    schema, table, rows = line.split("\t")
    tables.append({"schema": schema, "table": table, "rows": int(rows)})
document={
    "schema":"brevitas.logical-backup-manifest.v2",
    "target_contract":"brevitas-ephemeral-postgres-v1",
    "postgresql_major":16,
    "required_extensions":["pgcrypto","vector"],
    "required_roles":["anon","authenticated","service_role"],
    "backup_source_id":"migration-source",
    "source_environment":"test",
    "created_at":created,
    "ciphertext_file":pathlib.Path(dump).name,
    "ciphertext_sha256":digest,
    "ciphertext_bytes":int(size),
    "encryption":"ci-raw-custom-dump",
    "retention_days":0,
    "tables":tables,
}
pathlib.Path(manifest).write_text(json.dumps(document,indent=2,sort_keys=True)+"\n")
PY
manifest_sha256="$(sha256sum "${manifest}" | awk '{print $1}')"
python3 - "${artifact}" "${manifest_sha256}" "${created_at}" <<'PY'
import json, pathlib, sys
from datetime import datetime, timedelta, timezone
path, manifest_hash, created = sys.argv[1:]
backup_time=datetime.strptime(created,"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
document={
    "schema":"brevitas.deletion-artifact.v1",
    "backup_source_id":"migration-source",
    "source_environment":"test",
    "backup_manifest_sha256":manifest_hash,
    "backup_created_at":created,
    "issued_at":(backup_time+timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "evidence_reference":"deletion:ci:zero:001",
    "tombstones":[],
}
pathlib.Path(path).write_text(json.dumps(document,indent=2,sort_keys=True)+"\n")
PY
artifact_sha256="$(sha256sum "${artifact}" | awk '{print $1}')"

replay_base=(
  --environment test --target-id migration-restore
  --target-mode ephemeral-postgres --expected-database-name "${target_database}"
  --source-environment test --source-id migration-source
  --backup-manifest "${manifest}" --expected-manifest-sha256 "${manifest_sha256}"
  --deletion-artifact "${artifact}"
  --expected-deletion-artifact-sha256 "${artifact_sha256}"
  --deletion-evidence-reference deletion:ci:zero:001
  --evidence-dir "${evidence_dir}" --actor-id system:restore:ci-test
  --database-url-env RESTORE_DATABASE_URL --apply
)

expect_failure() {
  local label="$1" expected="$2"
  shift 2
  local log="${workdir}/${label}.log"
  if "$@" >"${log}" 2>&1; then
    echo "Restore negative control unexpectedly succeeded: ${label}" >&2
    exit 1
  fi
  if ! grep -q "${expected}" "${log}"; then
    echo "Restore negative control failed for the wrong reason: ${label}" >&2
    sed -n '1,80p' "${log}" >&2
    exit 1
  fi
}

expect_failure missing-control 'restore control/evidence preflight mismatch' env \
  RESTORE_DATABASE_URL="${RESTORE_DATABASE_URL}" \
  scripts/dr/replay-deletion-artifact.sh "${replay_base[@]}" \
  --confirm REPLAY:migration-source:migration-restore

RESTORE_DATABASE_URL="${RESTORE_DATABASE_URL}" scripts/dr/bootstrap-restore-target.sh \
  --environment test --target-id migration-restore --target-mode ephemeral-postgres \
  --expected-database-name "${target_database}" --source-environment test \
  --source-id migration-source --expected-manifest-sha256 "${manifest_sha256}" \
  --expected-deletion-artifact-sha256 "${artifact_sha256}" \
  --deletion-evidence-reference deletion:ci:zero:001 \
  --database-url-env RESTORE_DATABASE_URL --apply \
  --confirm BOOTSTRAP:migration-source:migration-restore

expect_failure wrong-control 'preflight mismatch' env \
  RESTORE_DATABASE_URL="${RESTORE_DATABASE_URL}" \
  scripts/dr/replay-deletion-artifact.sh \
  --environment test --target-id migration-wrong --target-mode ephemeral-postgres \
  --expected-database-name "${target_database}" --source-environment test --source-id migration-source \
  --backup-manifest "${manifest}" --expected-manifest-sha256 "${manifest_sha256}" \
  --deletion-artifact "${artifact}" --expected-deletion-artifact-sha256 "${artifact_sha256}" \
  --deletion-evidence-reference deletion:ci:zero:001 --evidence-dir "${evidence_dir}" \
  --actor-id system:restore:ci-test --database-url-env RESTORE_DATABASE_URL --apply \
  --confirm REPLAY:migration-source:migration-wrong
expect_failure wrong-hash 'hash mismatch' env \
  RESTORE_DATABASE_URL="${RESTORE_DATABASE_URL}" \
  scripts/dr/replay-deletion-artifact.sh \
  --environment test --target-id migration-restore --target-mode ephemeral-postgres \
  --expected-database-name "${target_database}" --source-environment test --source-id migration-source \
  --backup-manifest "${manifest}" --expected-manifest-sha256 "${manifest_sha256}" \
  --deletion-artifact "${artifact}" --expected-deletion-artifact-sha256 "$(printf '0%.0s' {1..64})" \
  --deletion-evidence-reference deletion:ci:zero:001 --evidence-dir "${evidence_dir}" \
  --actor-id system:restore:ci-test --database-url-env RESTORE_DATABASE_URL --apply \
  --confirm REPLAY:migration-source:migration-restore
expect_failure wrong-reference 'evidence binding mismatch' env \
  RESTORE_DATABASE_URL="${RESTORE_DATABASE_URL}" \
  scripts/dr/replay-deletion-artifact.sh \
  --environment test --target-id migration-restore --target-mode ephemeral-postgres \
  --expected-database-name "${target_database}" --source-environment test --source-id migration-source \
  --backup-manifest "${manifest}" --expected-manifest-sha256 "${manifest_sha256}" \
  --deletion-artifact "${artifact}" --expected-deletion-artifact-sha256 "${artifact_sha256}" \
  --deletion-evidence-reference deletion:ci:wrong:001 --evidence-dir "${evidence_dir}" \
  --actor-id system:restore:ci-test --database-url-env RESTORE_DATABASE_URL --apply \
  --confirm REPLAY:migration-source:migration-restore

restore_list="${workdir}/migration-source.restore-list"
pg_restore --list "${dump}" | awk '
  /^[0-9]+; [0-9]+ [0-9]+ SCHEMA - public / {
    print ";" $0
    public_schema_entries++
    next
  }
  { print }
  END { if (public_schema_entries != 1) exit 1 }
' > "${restore_list}"
# The isolated target already has the otherwise-empty public schema because
# pgcrypto and vector are bootstrapped there. Restore every public object but
# skip the dump's single duplicate CREATE SCHEMA public entry.
pg_restore --dbname="${RESTORE_DATABASE_URL}" --exit-on-error --single-transaction \
  --no-owner --no-privileges --use-list="${restore_list}" "${dump}"

RESTORE_DATABASE_URL="${RESTORE_DATABASE_URL}" scripts/dr/verify-logical.sh \
  --environment test --target-id migration-restore --target-mode ephemeral-postgres \
  --expected-database-name "${target_database}" --source-environment test \
  --source-id migration-source --manifest "${manifest}" --encrypted-backup "${dump}" \
  --expected-manifest-sha256 "${manifest_sha256}" \
  --backup-evidence-reference backup:ci:restore:001 --deletion-artifact "${artifact}" \
  --expected-deletion-artifact-sha256 "${artifact_sha256}" \
  --deletion-evidence-reference deletion:ci:zero:001 --evidence-dir "${evidence_dir}" \
  --database-url-env RESTORE_DATABASE_URL --apply \
  --confirm VERIFY:migration-source:migration-restore

state="$(psql "${RESTORE_DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 -At -F '|' --command \
  "select (raw_verified_at < replay_verified_at),(replay_verified_at <= ready_at),(select count(*) from brevitas_restore.replay_evidence),raw_verified_at is not null,replay_verified_at is not null,ready_at is not null from brevitas_restore.control where singleton")"
if [[ "${state}" != 't|t|0|t|t|t' ]]; then
  echo "Restore verification timestamps or zero-tombstone evidence are invalid: ${state}" >&2
  exit 1
fi

echo 'Second-database restore, raw counts, and zero-tombstone deletion replay passed.'
