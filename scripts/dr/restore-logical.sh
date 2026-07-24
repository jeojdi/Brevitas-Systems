#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'EOF'
Usage: restore-logical.sh --environment ENV --target-id ID
       --target-mode ephemeral-postgres --expected-database-name NAME
       --source-environment ENV --source-id ID --manifest FILE
       --encrypted-backup FILE --expected-manifest-sha256 SHA256
       --backup-evidence-reference ID --deletion-artifact FILE
       --expected-deletion-artifact-sha256 SHA256
       --deletion-evidence-reference ID --evidence-dir DIR
       [--database-url-env NAME] [--age-identity-env NAME]
       [--dry-run | --apply --confirm RESTORE:SOURCE:TARGET]
       [--allow-production]

The target must first be bootstrapped by bootstrap-restore-target.sh. Only an
isolated PostgreSQL 16 database using the explicit ephemeral-postgres contract
is accepted. Raw table verification and deletion replay must both succeed
before the target is marked ready. This is not a managed Supabase restore.
EOF
}

environment=""; target_id=""; target_mode=""; expected_database_name=""
source_environment=""; source_id=""; manifest=""; encrypted=""
expected_manifest_sha256=""; backup_evidence_reference=""
deletion_artifact=""; expected_deletion_artifact_sha256=""
deletion_evidence_reference=""; evidence_dir=""
database_url_env="RESTORE_DATABASE_URL"; age_identity_env="BREVITAS_BACKUP_AGE_IDENTITY"
mode="dry-run"; confirmation=""; allow_production="false"
while (($#)); do
  case "$1" in
    --environment) environment="${2-}"; shift 2 ;;
    --target-id) target_id="${2-}"; shift 2 ;;
    --target-mode) target_mode="${2-}"; shift 2 ;;
    --expected-database-name) expected_database_name="${2-}"; shift 2 ;;
    --source-environment) source_environment="${2-}"; shift 2 ;;
    --source-id) source_id="${2-}"; shift 2 ;;
    --manifest) manifest="${2-}"; shift 2 ;;
    --encrypted-backup) encrypted="${2-}"; shift 2 ;;
    --expected-manifest-sha256) expected_manifest_sha256="${2-}"; shift 2 ;;
    --backup-evidence-reference) backup_evidence_reference="${2-}"; shift 2 ;;
    --deletion-artifact) deletion_artifact="${2-}"; shift 2 ;;
    --expected-deletion-artifact-sha256) expected_deletion_artifact_sha256="${2-}"; shift 2 ;;
    --deletion-evidence-reference) deletion_evidence_reference="${2-}"; shift 2 ;;
    --evidence-dir) evidence_dir="${2-}"; shift 2 ;;
    --database-url-env) database_url_env="${2-}"; shift 2 ;;
    --age-identity-env) age_identity_env="${2-}"; shift 2 ;;
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
   && -n "$manifest" && -n "$encrypted" && -n "$expected_manifest_sha256" \
   && -n "$backup_evidence_reference" && -n "$deletion_artifact" \
   && -n "$expected_deletion_artifact_sha256" && -n "$deletion_evidence_reference" \
   && -n "$evidence_dir" ]] \
  || { usage >&2; dr_die "all source, target, artifact, hash, and evidence arguments are required"; }
dr_validate_environment "$environment"; dr_validate_environment "$source_environment"
dr_validate_identifier "target ID" "$target_id"; dr_validate_identifier "source ID" "$source_id"
dr_validate_identifier "database name" "$expected_database_name"
[[ "$target_mode" == "ephemeral-postgres" ]] || dr_die "target mode must be ephemeral-postgres"
[[ "$source_id" != "$target_id" ]] || dr_die "source ID and destination target ID must differ"
[[ "$expected_manifest_sha256" =~ ^[0-9a-f]{64}$ ]] || dr_die "expected manifest SHA-256 is invalid"
[[ "$expected_deletion_artifact_sha256" =~ ^[0-9a-f]{64}$ ]] || dr_die "expected deletion artifact SHA-256 is invalid"
dr_validate_identifier "backup evidence reference" "$backup_evidence_reference"
dr_validate_identifier "deletion evidence reference" "$deletion_evidence_reference"
[[ ${#backup_evidence_reference} -ge 8 && ${#deletion_evidence_reference} -ge 8 ]] \
  || dr_die "evidence references must be at least eight characters"
dr_validate_env_name "$database_url_env"; dr_validate_env_name "$age_identity_env"
dr_require_production_opt_in "$environment" "$allow_production"

if [[ "$mode" == "dry-run" ]]; then
  dr_note "DRY RUN: would bind source $source_id ($source_environment) to bootstrapped PostgreSQL 16 destination $target_id ($environment), restore raw tables, replay the independently protected deletion artifact, and mark readiness only after both verifications."
  dr_note "DRY RUN: no database connection was made and no credential was read."
  exit 0
fi

dr_require_confirmation "$confirmation" "RESTORE:$source_id:$target_id"
for path in "$manifest" "$encrypted" "$deletion_artifact"; do
  [[ -f "$path" && ! -L "$path" ]] || dr_die "backup, manifest, and deletion artifact must be regular non-symlink files"
done
[[ "$(dr_file_size "$manifest")" -le 10485760 ]] || dr_die "manifest exceeds its 10 MiB safety bound"
[[ "$(dr_file_size "$deletion_artifact")" -le 52428800 ]] || dr_die "deletion artifact exceeds its 50 MiB safety bound"
dr_safe_directory "$evidence_dir"; dr_require_command python3
actual_manifest_sha256="$(dr_sha256 "$manifest")"
[[ "$actual_manifest_sha256" == "$expected_manifest_sha256" ]] || dr_die "manifest does not match the independent expected SHA-256"
actual_deletion_sha256="$(dr_sha256 "$deletion_artifact")"
[[ "$actual_deletion_sha256" == "$expected_deletion_artifact_sha256" ]] || dr_die "deletion artifact does not match the independent expected SHA-256"
actual_ciphertext_sha256="$(dr_sha256 "$encrypted")"
python3 - "$manifest" "$encrypted" "$actual_ciphertext_sha256" "$source_id" \
  "$source_environment" "$deletion_artifact" "$expected_manifest_sha256" \
  "$deletion_evidence_reference" <<'PY'
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

(manifest_path, encrypted_path, digest, source_id, source_environment,
 artifact_path, manifest_hash, deletion_reference) = sys.argv[1:]
manifest = json.loads(pathlib.Path(manifest_path).read_text())
if manifest.get("schema") != "brevitas.logical-backup-manifest.v2":
    raise SystemExit("ERROR: unsupported manifest schema")
if manifest.get("target_contract") != "brevitas-ephemeral-postgres-v1" \
        or manifest.get("postgresql_major") != 16 \
        or manifest.get("required_extensions") != ["pgcrypto", "vector"] \
        or manifest.get("required_roles") != ["anon", "authenticated", "service_role"]:
    raise SystemExit("ERROR: manifest restore target contract is unsupported")
if manifest.get("backup_source_id") != source_id or manifest.get("source_environment") != source_environment:
    raise SystemExit("ERROR: manifest source identity does not match operator intent")
if manifest.get("ciphertext_file") != pathlib.Path(encrypted_path).name \
        or manifest.get("ciphertext_sha256") != digest \
        or os.path.getsize(encrypted_path) != manifest.get("ciphertext_bytes"):
    raise SystemExit("ERROR: ciphertext integrity mismatch")
tables = manifest.get("tables")
if not isinstance(tables, list) or not tables:
    raise SystemExit("ERROR: manifest table inventory is empty")
seen = set()
for item in tables:
    schema, table, rows = item.get("schema"), item.get("table"), item.get("rows")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or "") \
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table or ""):
        raise SystemExit("ERROR: unsafe table identifier in manifest")
    if (schema, table) in seen or not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
        raise SystemExit("ERROR: invalid or duplicate table inventory")
    seen.add((schema, table))
artifact = json.loads(pathlib.Path(artifact_path).read_text())
if artifact.get("schema") != "brevitas.deletion-artifact.v1" \
        or artifact.get("backup_source_id") != source_id \
        or artifact.get("source_environment") != source_environment:
    raise SystemExit("ERROR: deletion artifact source binding mismatch")
if artifact.get("backup_manifest_sha256") != manifest_hash \
        or artifact.get("evidence_reference") != deletion_reference:
    raise SystemExit("ERROR: deletion artifact evidence binding mismatch")
if artifact.get("backup_created_at") != manifest.get("created_at"):
    raise SystemExit("ERROR: deletion artifact backup timestamp binding mismatch")
try:
    backup_time = datetime.strptime(manifest["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    issued_time = datetime.strptime(artifact["issued_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
except (KeyError, TypeError, ValueError):
    raise SystemExit("ERROR: restore artifact timestamp is invalid")
if issued_time <= backup_time:
    raise SystemExit("ERROR: deletion artifact must be newer than the backup")
if not isinstance(artifact.get("tombstones"), list):
    raise SystemExit("ERROR: deletion artifact tombstones are invalid")
PY

dr_require_command age; dr_require_postgresql_client_major pg_restore 16; dr_require_command psql
database_url="$(dr_secret_from_env "$database_url_env")"
age_identity="$(dr_secret_from_env "$age_identity_env")"
preflight="$(dr_database_exec "$database_url" psql -X -v ON_ERROR_STOP=1 -At -F '|' -c \
  "select current_database(),current_setting('server_version_num')::integer,(select count(*) from pg_extension where extname in ('pgcrypto','vector')),(select count(*) from pg_roles where rolname in ('anon','authenticated','service_role')),(select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind in ('r','p') and n.nspname in ('public','auth')),target_mode,target_id,target_environment,expected_database_name,backup_source_id,source_environment,backup_manifest_sha256,deletion_artifact_sha256,deletion_evidence_reference,(raw_verified_at is null),(replay_verified_at is null),(ready_at is null) from brevitas_restore.control where singleton")"
IFS='|' read -r actual_database version_num extension_count role_count application_tables \
  control_mode control_target control_environment control_database control_source control_source_environment \
  control_manifest control_deletion control_reference raw_unset replay_unset ready_unset <<< "$preflight"
[[ "$actual_database" == "$expected_database_name" && "$control_database" == "$expected_database_name" ]] || dr_die "restore target database name/control mismatch"
[[ "$version_num" =~ ^16[0-9]{4}$ ]] || dr_die "restore target requires PostgreSQL major version 16"
[[ "$extension_count" == "2" && "$role_count" == "3" ]] || dr_die "restore target is missing required extensions or roles"
[[ "$application_tables" == "0" ]] || dr_die "restore target is not empty"
[[ "$control_mode" == "$target_mode" && "$control_target" == "$target_id" \
   && "$control_environment" == "$environment" \
   && "$control_source" == "$source_id" && "$control_source_environment" == "$source_environment" \
   && "$control_manifest" == "$expected_manifest_sha256" \
   && "$control_deletion" == "$expected_deletion_artifact_sha256" \
   && "$control_reference" == "$deletion_evidence_reference" \
   && "$raw_unset" == "t" && "$replay_unset" == "t" && "$ready_unset" == "t" ]] \
  || dr_die "restore bootstrap control/evidence preflight mismatch"

identity_file="$(mktemp "${TMPDIR:-/tmp}/brevitas-age-identity.XXXXXX")"
trap 'rm -f -- "$identity_file"' EXIT
printf '%s\n' "$age_identity" > "$identity_file"
chmod 600 "$identity_file"
age --decrypt --identity "$identity_file" "$encrypted" \
  | dr_database_exec "$database_url" pg_restore --dbname="" \
      --exit-on-error --no-owner --no-privileges

verify_args=(
  --environment "$environment" --target-id "$target_id"
  --target-mode "$target_mode" --expected-database-name "$expected_database_name"
  --source-environment "$source_environment" --source-id "$source_id"
  --manifest "$manifest" --encrypted-backup "$encrypted" --evidence-dir "$evidence_dir"
  --expected-manifest-sha256 "$expected_manifest_sha256"
  --backup-evidence-reference "$backup_evidence_reference"
  --deletion-artifact "$deletion_artifact"
  --expected-deletion-artifact-sha256 "$expected_deletion_artifact_sha256"
  --deletion-evidence-reference "$deletion_evidence_reference"
  --database-url-env "$database_url_env" --apply --confirm "VERIFY:$source_id:$target_id"
)
if [[ "$allow_production" == "true" ]]; then verify_args+=(--allow-production); fi
"$SCRIPT_DIR/verify-logical.sh" "${verify_args[@]}"
dr_note "Restore, raw verification, deletion replay, and readiness verification completed from source $source_id to destination $target_id."
