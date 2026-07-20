#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'EOF'
Usage: export-deletion-artifact.sh --environment ENV --source-id ID
       --backup-manifest FILE --expected-manifest-sha256 SHA256
       --evidence-reference ID --output-dir DIR [--database-url-env NAME]
       [--dry-run | --apply --confirm TOMBSTONES:SOURCE]
       [--allow-production]

The output is content-free but restricted. Store it immutably and obtain its
SHA-256 through an independent evidence channel before any restore.
EOF
}

environment=""; source_id=""; backup_manifest=""; expected_manifest_sha256=""
evidence_reference=""; output_dir=""; database_url_env="COMPLIANCE_DATABASE_URL"
mode="dry-run"; confirmation=""; allow_production="false"
while (($#)); do
  case "$1" in
    --environment) environment="${2-}"; shift 2 ;;
    --source-id) source_id="${2-}"; shift 2 ;;
    --backup-manifest) backup_manifest="${2-}"; shift 2 ;;
    --expected-manifest-sha256) expected_manifest_sha256="${2-}"; shift 2 ;;
    --evidence-reference) evidence_reference="${2-}"; shift 2 ;;
    --output-dir) output_dir="${2-}"; shift 2 ;;
    --database-url-env) database_url_env="${2-}"; shift 2 ;;
    --dry-run) mode="dry-run"; shift ;;
    --apply) mode="apply"; shift ;;
    --confirm) confirmation="${2-}"; shift 2 ;;
    --allow-production) allow_production="true"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; dr_die "unknown or incomplete argument" ;;
  esac
done
[[ -n "$environment" && -n "$source_id" && -n "$backup_manifest" \
   && -n "$expected_manifest_sha256" && -n "$evidence_reference" && -n "$output_dir" ]] \
  || { usage >&2; dr_die "all source, manifest, evidence, and output arguments are required"; }
dr_validate_environment "$environment"; dr_validate_identifier "source ID" "$source_id"
[[ "$expected_manifest_sha256" =~ ^[0-9a-f]{64}$ ]] || dr_die "expected manifest SHA-256 is invalid"
dr_validate_identifier "evidence reference" "$evidence_reference"
[[ ${#evidence_reference} -ge 8 ]] || dr_die "evidence reference is too short"
dr_validate_env_name "$database_url_env"; dr_require_production_opt_in "$environment" "$allow_production"
if [[ "$mode" == "dry-run" ]]; then
  dr_note "DRY RUN: would export completed deletion tombstones newer than the source-bound backup into an independently protected artifact."
  dr_note "DRY RUN: no database connection was made and no credential was read."
  exit 0
fi
dr_require_confirmation "$confirmation" "TOMBSTONES:$source_id"
[[ -f "$backup_manifest" && ! -L "$backup_manifest" ]] || dr_die "backup manifest must be a regular non-symlink file"
[[ "$(dr_file_size "$backup_manifest")" -le 10485760 ]] || dr_die "backup manifest exceeds its safety bound"
actual_manifest_sha256="$(dr_sha256 "$backup_manifest")"
[[ "$actual_manifest_sha256" == "$expected_manifest_sha256" ]] || dr_die "backup manifest does not match independent evidence"
dr_safe_directory "$output_dir"; dr_require_command python3; dr_require_command psql
backup_created_at="$(python3 - "$backup_manifest" "$source_id" "$environment" <<'PY'
import json, pathlib, sys
path, source, environment = sys.argv[1:]
document = json.loads(pathlib.Path(path).read_text())
if document.get("schema") != "brevitas.logical-backup-manifest.v2":
    raise SystemExit("ERROR: unsupported backup manifest")
if document.get("target_contract") != "brevitas-ephemeral-postgres-v1" \
        or document.get("postgresql_major") != 16 \
        or document.get("required_extensions") != ["pgcrypto", "vector"] \
        or document.get("required_roles") != ["anon", "authenticated", "service_role"]:
    raise SystemExit("ERROR: unsupported backup restore target contract")
if document.get("backup_source_id") != source or document.get("source_environment") != environment:
    raise SystemExit("ERROR: backup manifest source mismatch")
created = document.get("created_at", "")
if not isinstance(created, str):
    raise SystemExit("ERROR: backup manifest timestamp is invalid")
print(created)
PY
)"
database_url="$(dr_secret_from_env "$database_url_env")"
tombstones_tmp="$(mktemp "${TMPDIR:-/tmp}/brevitas-tombstones.XXXXXX")"
trap 'rm -f -- "$tombstones_tmp"' EXIT INT TERM
PGDATABASE="$database_url" PGCONNECT_TIMEOUT=10 psql -X -v ON_ERROR_STOP=1 -qAt -c \
  "select coalesce(jsonb_agg(jsonb_build_object('request_id',t.request_id,'organization_id',t.organization_id,'requested_at',t.request_received_at,'expires_at',t.expires_at,'request_scope',r.request_scope,'subject_id',r.subject_id) order by t.request_id),'[]'::jsonb) from public.backup_deletion_tombstones t join public.data_subject_requests r on r.id=t.request_id and r.organization_id=t.organization_id where r.status='completed'" \
  > "$tombstones_tmp"
stamp="$(dr_timestamp)"; base="deletions-${source_id}-${stamp}"
artifact="$output_dir/$base.json"; evidence="$output_dir/$base.evidence.json"
[[ ! -e "$artifact" && ! -e "$evidence" ]] || dr_die "deletion artifact output already exists"
python3 - "$artifact" "$tombstones_tmp" "$source_id" "$environment" \
  "$expected_manifest_sha256" "$backup_created_at" "$evidence_reference" <<'PY'
import json, pathlib, sys
from datetime import datetime, timezone
path, rows_path, source, environment, manifest_hash, backup_created, evidence_reference = sys.argv[1:]
try:
    backup_time = datetime.strptime(backup_created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
except ValueError:
    raise SystemExit("ERROR: backup timestamp is invalid")
issued = datetime.now(timezone.utc).replace(microsecond=0)
if issued <= backup_time:
    raise SystemExit("ERROR: deletion artifact must be newer than the backup")
rows = json.loads(pathlib.Path(rows_path).read_text())
if not isinstance(rows, list):
    raise SystemExit("ERROR: tombstone query returned invalid data")
document = {
    "schema": "brevitas.deletion-artifact.v1",
    "backup_source_id": source,
    "source_environment": environment,
    "backup_manifest_sha256": manifest_hash,
    "backup_created_at": backup_created,
    "issued_at": issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "evidence_reference": evidence_reference,
    "tombstones": rows,
}
pathlib.Path(path).write_text(json.dumps(document,indent=2,sort_keys=True)+"\n")
PY
artifact_sha256="$(dr_sha256 "$artifact")"
python3 - "$evidence" "$artifact" "$artifact_sha256" "$evidence_reference" <<'PY'
import json,pathlib,sys
path,artifact,digest,reference=sys.argv[1:]
document={"schema":"brevitas.deletion-artifact-evidence.v1","artifact_file":pathlib.Path(artifact).name,"artifact_sha256":digest,"evidence_reference":reference,"immutable_storage_required":True}
pathlib.Path(path).write_text(json.dumps(document,indent=2,sort_keys=True)+"\n")
PY
chmod 600 "$artifact" "$evidence"
dr_note "Deletion artifact completed. Restricted evidence: $evidence"
