#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'EOF'
Usage: backup-logical.sh --environment ENV --source-id ID --output-dir DIR
       [--database-url-env NAME] [--age-recipient-env NAME]
       [--dry-run | --apply --confirm BACKUP:ID] [--allow-production]

Default mode is an offline dry-run. The database URL and age recipient are read
only from named environment variables and are never printed.
EOF
}

environment=""
source_id=""
output_dir=""
database_url_env="BACKUP_DATABASE_URL"
age_recipient_env="BREVITAS_BACKUP_AGE_RECIPIENT"
mode="dry-run"
confirmation=""
allow_production="false"

while (($#)); do
  case "$1" in
    --environment) environment="${2-}"; shift 2 ;;
    --source-id) source_id="${2-}"; shift 2 ;;
    --output-dir) output_dir="${2-}"; shift 2 ;;
    --database-url-env) database_url_env="${2-}"; shift 2 ;;
    --age-recipient-env) age_recipient_env="${2-}"; shift 2 ;;
    --dry-run) mode="dry-run"; shift ;;
    --apply) mode="apply"; shift ;;
    --confirm) confirmation="${2-}"; shift 2 ;;
    --allow-production) allow_production="true"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; dr_die "unknown or incomplete argument" ;;
  esac
done

[[ -n "$environment" && -n "$source_id" && -n "$output_dir" ]] || { usage >&2; dr_die "environment, source ID, and output directory are required"; }
dr_validate_environment "$environment"
dr_validate_identifier "source ID" "$source_id"
dr_validate_env_name "$database_url_env"
dr_validate_env_name "$age_recipient_env"
dr_require_production_opt_in "$environment" "$allow_production"

if [[ "$mode" == "dry-run" ]]; then
  dr_note "DRY RUN: would create one encrypted logical backup, table-count manifest, and content-free evidence for source $source_id."
  dr_note "DRY RUN: no database connection was made and no credential was read."
  exit 0
fi

dr_require_confirmation "$confirmation" "BACKUP:$source_id"
dr_safe_directory "$output_dir"
dr_require_postgresql_client_major pg_dump 16
dr_require_command psql
dr_require_command age
dr_require_command python3

database_url="$(dr_secret_from_env "$database_url_env")"
age_recipient="$(dr_secret_from_env "$age_recipient_env")"
stamp="$(dr_timestamp)"
base="brevitas-${source_id}-${stamp}"
encrypted="$output_dir/$base.dump.age"
manifest="$output_dir/$base.manifest.json"
evidence="$output_dir/$base.backup-evidence.json"
[[ ! -e "$encrypted" && ! -e "$manifest" && ! -e "$evidence" ]] || dr_die "backup output set already exists"
counts="$(mktemp "${TMPDIR:-/tmp}/brevitas-backup-counts.XXXXXX")"
snapshot_dir="$(mktemp -d "${TMPDIR:-/tmp}/brevitas-backup-snapshot.XXXXXX")"
snapshot_output="$snapshot_dir/output"
snapshot_errors="$snapshot_dir/errors"
snapshot_fifo="$snapshot_dir/control"
: > "$snapshot_output"
: > "$snapshot_errors"
mkfifo "$snapshot_fifo"
holder_pid=""
backup_complete="false"
cleanup() {
  if [[ -n "$holder_pid" ]] && kill -0 "$holder_pid" 2>/dev/null; then
    printf '%s\n' 'rollback;' '\q' >&9 2>/dev/null || true
    kill "$holder_pid" 2>/dev/null || true
    wait "$holder_pid" 2>/dev/null || true
  fi
  exec 9>&- 2>/dev/null || true
  rm -f -- "$counts" "$snapshot_output" "$snapshot_errors" "$snapshot_fifo"
  rmdir -- "$snapshot_dir" 2>/dev/null || true
  if [[ "$backup_complete" != "true" ]]; then
    rm -f -- "$encrypted" "$manifest" "$evidence"
  fi
}
trap cleanup EXIT INT TERM

# Hold one read-only repeatable-read transaction open. The dump and each table
# count import its exported snapshot, so active writes cannot create a manifest
# that disagrees with the encrypted archive.
exec 9<>"$snapshot_fifo"
dr_database_exec "$database_url" psql -X -qAt <&9 >"$snapshot_output" 2>"$snapshot_errors" &
holder_pid="$!"
printf '%s\n' 'begin isolation level repeatable read read only;' 'select pg_export_snapshot();' >&9
snapshot=""
for _attempt in {1..100}; do
  if [[ -s "$snapshot_output" ]]; then
    IFS= read -r snapshot < "$snapshot_output" || true
    [[ -n "$snapshot" ]] && break
  fi
  kill -0 "$holder_pid" 2>/dev/null || dr_die "failed to open a consistent backup snapshot"
  sleep 0.1
done
[[ "$snapshot" =~ ^[A-Za-z0-9-]{8,128}$ ]] || dr_die "failed to obtain a safe backup snapshot"

# Count only ordinary/partitioned application tables. Names are quoted by
# format(), and only counts—not row content—enter the integrity manifest.
table_names="$(dr_database_exec "$database_url" psql -X -q -v ON_ERROR_STOP=1 -At -F $'\t' -c \
  "begin isolation level repeatable read read only; set transaction snapshot '$snapshot'; select n.nspname, c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind in ('r','p') and n.nspname in ('public','auth') order by 1,2; commit")"
while IFS=$'\t' read -r schema_name table_name; do
  [[ -n "$schema_name" && -n "$table_name" ]] || continue
  [[ "$schema_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && "$table_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || dr_die "database contains an unsupported table identifier"
  row_count="$(dr_database_exec "$database_url" psql -X -q -v ON_ERROR_STOP=1 -At -c \
    "begin isolation level repeatable read read only; set transaction snapshot '$snapshot'; select count(*) from \"$schema_name\".\"$table_name\"; commit")"
  [[ "$row_count" =~ ^[0-9]+$ ]] || dr_die "table verification returned an invalid count"
  printf '%s\t%s\t%s\n' "$schema_name" "$table_name" "$row_count" >> "$counts"
done <<< "$table_names"
[[ -s "$counts" ]] || dr_die "no application tables were available for verification"

# The logical dump never lands on disk in plaintext.
dr_database_exec "$database_url" pg_dump --format=custom --no-owner --no-privileges \
  --schema=public --schema=auth --snapshot="$snapshot" \
  | age --encrypt --recipient "$age_recipient" --output "$encrypted"
chmod 600 "$encrypted"
printf '%s\n' 'rollback;' '\q' >&9
wait "$holder_pid"
holder_pid=""

ciphertext_sha256="$(dr_sha256 "$encrypted")"
ciphertext_bytes="$(dr_file_size "$encrypted")"
created_at="$(dr_now)"

python3 - "$manifest" "$source_id" "$environment" "$created_at" "$encrypted" "$ciphertext_sha256" "$ciphertext_bytes" "$counts" <<'PY'
import json
import pathlib
import sys

manifest, source, environment, created, encrypted, digest, size, counts_path = sys.argv[1:]
tables = []
for line in pathlib.Path(counts_path).read_text().splitlines():
    schema, table, count = line.split("\t")
    tables.append({"schema": schema, "table": table, "rows": int(count)})
document = {
    "schema": "brevitas.logical-backup-manifest.v2",
    "target_contract": "brevitas-ephemeral-postgres-v1",
    "postgresql_major": 16,
    "required_extensions": ["pgcrypto", "vector"],
    "required_roles": ["anon", "authenticated", "service_role"],
    "backup_source_id": source,
    "source_environment": environment,
    "created_at": created,
    "ciphertext_file": pathlib.Path(encrypted).name,
    "ciphertext_sha256": digest,
    "ciphertext_bytes": int(size),
    "encryption": "age-v1",
    "retention_days": 35,
    "tables": tables,
}
pathlib.Path(manifest).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
PY
chmod 600 "$manifest"
manifest_sha256="$(dr_sha256 "$manifest")"

python3 - "$evidence" "$source_id" "$environment" "$created_at" "$manifest" "$manifest_sha256" <<'PY'
import json
import pathlib
import sys

path, source, environment, created, manifest, digest = sys.argv[1:]
document = {
    "schema": "brevitas.backup-evidence.v2",
    "operation": "logical-backup",
    "result": "completed",
    "backup_source_id": source,
    "source_environment": environment,
    "completed_at": created,
    "manifest_file": pathlib.Path(manifest).name,
    "manifest_sha256": digest,
    "evidence_contains_customer_content": False,
}
pathlib.Path(path).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
PY
chmod 600 "$evidence"
backup_complete="true"
dr_note "Backup completed. Evidence: $evidence"
