#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'EOF'
Usage: bootstrap-restore-target.sh --environment ENV --target-id ID
       --target-mode ephemeral-postgres --expected-database-name NAME
       --source-environment ENV --source-id ID
       --expected-manifest-sha256 SHA256
       --expected-deletion-artifact-sha256 SHA256
       --deletion-evidence-reference ID [--database-url-env NAME]
       [--dry-run | --apply --confirm BOOTSTRAP:SOURCE:TARGET]
       [--allow-production]

This command bootstraps only a new isolated PostgreSQL 16 database. It is not a
fresh Supabase project and never provisions a database or cloud resource.
EOF
}

environment=""; target_id=""; target_mode=""; expected_database_name=""
source_environment=""; source_id=""; expected_manifest_sha256=""
expected_deletion_artifact_sha256=""; deletion_evidence_reference=""
database_url_env="RESTORE_DATABASE_URL"; mode="dry-run"; confirmation=""
allow_production="false"
while (($#)); do
  case "$1" in
    --environment) environment="${2-}"; shift 2 ;;
    --target-id) target_id="${2-}"; shift 2 ;;
    --target-mode) target_mode="${2-}"; shift 2 ;;
    --expected-database-name) expected_database_name="${2-}"; shift 2 ;;
    --source-environment) source_environment="${2-}"; shift 2 ;;
    --source-id) source_id="${2-}"; shift 2 ;;
    --expected-manifest-sha256) expected_manifest_sha256="${2-}"; shift 2 ;;
    --expected-deletion-artifact-sha256) expected_deletion_artifact_sha256="${2-}"; shift 2 ;;
    --deletion-evidence-reference) deletion_evidence_reference="${2-}"; shift 2 ;;
    --database-url-env) database_url_env="${2-}"; shift 2 ;;
    --dry-run) mode="dry-run"; shift ;;
    --apply) mode="apply"; shift ;;
    --confirm) confirmation="${2-}"; shift 2 ;;
    --allow-production) allow_production="true"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; dr_die "unknown or incomplete argument" ;;
  esac
done
[[ -n "$environment" && -n "$target_id" && -n "$target_mode" \
   && -n "$expected_database_name" && -n "$source_environment" && -n "$source_id" \
   && -n "$expected_manifest_sha256" && -n "$expected_deletion_artifact_sha256" \
   && -n "$deletion_evidence_reference" ]] || { usage >&2; dr_die "all identity and evidence arguments are required"; }
dr_validate_environment "$environment"; dr_validate_environment "$source_environment"
dr_validate_identifier "target ID" "$target_id"; dr_validate_identifier "source ID" "$source_id"
dr_validate_identifier "database name" "$expected_database_name"
[[ "$target_mode" == "ephemeral-postgres" ]] || dr_die "target mode must be ephemeral-postgres"
[[ "$target_id" != "$source_id" ]] || dr_die "source and target IDs must differ"
[[ "$expected_manifest_sha256" =~ ^[0-9a-f]{64}$ ]] || dr_die "expected manifest SHA-256 is invalid"
[[ "$expected_deletion_artifact_sha256" =~ ^[0-9a-f]{64}$ ]] || dr_die "expected deletion artifact SHA-256 is invalid"
dr_validate_identifier "deletion evidence reference" "$deletion_evidence_reference"
[[ ${#deletion_evidence_reference} -ge 8 ]] || dr_die "deletion evidence reference is too short"
dr_validate_env_name "$database_url_env"
dr_require_production_opt_in "$environment" "$allow_production"
if [[ "$mode" == "dry-run" ]]; then
  dr_note "DRY RUN: would bootstrap isolated PostgreSQL 16 target $target_id with required roles/extensions and source-bound restore control."
  dr_note "DRY RUN: no database connection was made and no credential was read."
  exit 0
fi
dr_require_confirmation "$confirmation" "BOOTSTRAP:$source_id:$target_id"
dr_require_command psql
database_url="$(dr_secret_from_env "$database_url_env")"
preflight="$(dr_database_exec "$database_url" psql -X -v ON_ERROR_STOP=1 -At -F '|' -c \
  "select current_database(),current_setting('server_version_num')::integer,(select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind in ('r','p') and n.nspname in ('public','auth'))")"
IFS='|' read -r actual_database version_num application_tables <<< "$preflight"
[[ "$actual_database" == "$expected_database_name" ]] || dr_die "restore target database name mismatch"
[[ "$version_num" =~ ^16[0-9]{4}$ ]] || dr_die "restore target requires PostgreSQL major version 16"
[[ "$application_tables" == "0" ]] || dr_die "restore bootstrap target contains application tables"
dr_database_exec "$database_url" psql -X -v ON_ERROR_STOP=1 \
  --set=target_mode="$target_mode" --set=target_id="$target_id" \
  --set=target_environment="$environment" \
  --set=expected_database_name="$expected_database_name" --set=backup_source_id="$source_id" \
  --set=source_environment="$source_environment" --set=backup_manifest_sha256="$expected_manifest_sha256" \
  --set=deletion_artifact_sha256="$expected_deletion_artifact_sha256" \
  --set=deletion_evidence_reference="$deletion_evidence_reference" \
  --file "$SCRIPT_DIR/restore-target-bootstrap.sql" >/dev/null
postflight="$(dr_database_exec "$database_url" psql -X -v ON_ERROR_STOP=1 -At -F '|' -c \
  "select (select count(*) from pg_extension where extname in ('pgcrypto','vector')),(select count(*) from pg_roles where rolname in ('anon','authenticated','service_role')),target_mode,target_id,target_environment,backup_source_id from brevitas_restore.control where singleton")"
[[ "$postflight" == "2|3|ephemeral-postgres|$target_id|$environment|$source_id" ]] || dr_die "restore bootstrap verification failed"
dr_note "Isolated restore target bootstrap completed for $target_id."
