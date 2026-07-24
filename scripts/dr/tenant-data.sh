#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'EOF'
Usage: tenant-data.sh --action export|delete --request-id UUID --tenant-id UUID
       --scope tenant|member|customer [--subject-id UUID]
       --environment ENV --target-id ID --evidence-dir DIR
       [--database-url-env NAME] [--age-recipient-env NAME]
       [--age-identity-env NAME] [--evidence-hmac-key-env NAME]
       [--decrypt-command-env NAME] [--actor-id ID]
       [--dry-run | --apply --confirm ACTION:ID:REQUEST_UUID]
       [--allow-production]

Apply mode requires the separately migrated compliance schema/RPC contract.
It fails closed on legal holds, missing capabilities, unapproved requests,
tenant mismatch, or missing deletion tombstones. Overdue approved requests are
processed urgently with content-free deadline-breach evidence. Export content
is streamed directly to age encryption and never enters general telemetry.
EOF
}

action=""; scope="tenant"; subject_id=""; request_id=""; tenant_id=""
environment=""; target_id=""; evidence_dir=""
database_url_env="COMPLIANCE_DATABASE_URL"; age_recipient_env="BREVITAS_EXPORT_AGE_RECIPIENT"
age_identity_env="BREVITAS_EXPORT_AGE_IDENTITY"
evidence_hmac_key_env="BREVITAS_EXPORT_EVIDENCE_HMAC_KEY"
decrypt_command_env="BREVITAS_COMPLIANCE_DECRYPT_COMMAND"
actor_id=""; mode="dry-run"; confirmation=""; allow_production="false"
while (($#)); do
  case "$1" in
    --action) action="${2-}"; shift 2 ;;
    --scope) scope="${2-}"; shift 2 ;;
    --subject-id) subject_id="${2-}"; shift 2 ;;
    --request-id) request_id="${2-}"; shift 2 ;;
    --tenant-id) tenant_id="${2-}"; shift 2 ;;
    --environment) environment="${2-}"; shift 2 ;;
    --target-id) target_id="${2-}"; shift 2 ;;
    --evidence-dir) evidence_dir="${2-}"; shift 2 ;;
    --database-url-env) database_url_env="${2-}"; shift 2 ;;
    --age-recipient-env) age_recipient_env="${2-}"; shift 2 ;;
    --age-identity-env) age_identity_env="${2-}"; shift 2 ;;
    --evidence-hmac-key-env) evidence_hmac_key_env="${2-}"; shift 2 ;;
    --decrypt-command-env) decrypt_command_env="${2-}"; shift 2 ;;
    --actor-id) actor_id="${2-}"; shift 2 ;;
    --dry-run) mode="dry-run"; shift ;;
    --apply) mode="apply"; shift ;;
    --confirm) confirmation="${2-}"; shift 2 ;;
    --allow-production) allow_production="true"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; dr_die "unknown or incomplete argument" ;;
  esac
done
[[ "$action" == "export" || "$action" == "delete" ]] || dr_die "--action must be export or delete"
[[ "$scope" == "tenant" || "$scope" == "member" || "$scope" == "customer" ]] \
  || dr_die "--scope must be tenant, member, or customer"
if [[ "$scope" == "tenant" ]]; then
  [[ -z "$subject_id" ]] || dr_die "tenant scope must not include --subject-id"
else
  [[ -n "$subject_id" ]] || dr_die "member/customer scope requires --subject-id"
  dr_validate_uuid "subject ID" "$subject_id"
fi
[[ -n "$request_id" && -n "$tenant_id" && -n "$environment" && -n "$target_id" && -n "$evidence_dir" ]] || { usage >&2; dr_die "request, tenant, environment, target, and evidence arguments are required"; }
dr_validate_uuid "request ID" "$request_id"
dr_validate_uuid "tenant ID" "$tenant_id"
dr_validate_environment "$environment"
dr_validate_identifier "target ID" "$target_id"
dr_validate_env_name "$database_url_env"
dr_validate_env_name "$age_recipient_env"
dr_validate_env_name "$age_identity_env"
dr_validate_env_name "$evidence_hmac_key_env"
dr_validate_env_name "$decrypt_command_env"
dr_require_production_opt_in "$environment" "$allow_production"

if [[ "$mode" == "dry-run" ]]; then
  dr_note "DRY RUN: would preflight the compliance schema, tenant scope, 30-day due date, legal hold, immutable audit, and ${action} RPC."
  if [[ "$action" == "delete" ]]; then
    dr_note "DRY RUN: would require billing/tax exceptions and a backup deletion tombstone expiring within 35 days."
  else
    dr_note "DRY RUN: would application-decrypt every encrypted row with exact KMS context, age-encrypt the portable export, decrypt-verify it, and sign request-bound evidence before database finalization."
  fi
  dr_note "DRY RUN: no database connection was made and no credential was read."
  exit 0
fi

[[ -n "$actor_id" ]] || dr_die "--actor-id is required in apply mode"
dr_validate_identifier "actor ID" "$actor_id"
[[ "$actor_id" =~ ^(system|brevitas_admin):[A-Za-z0-9._:-]{3,96}$ ]] \
  || dr_die "actor ID must be an opaque system or brevitas_admin identity"
if [[ "$action" == "export" ]]; then
  confirmation_operation="EXPORT"
else
  confirmation_operation="DELETE"
fi
dr_require_confirmation "$confirmation" "$confirmation_operation:$target_id:$request_id"
dr_safe_directory "$evidence_dir"
dr_require_command psql
dr_require_command python3
database_url="$(dr_secret_from_env "$database_url_env")"

# Capability preflight. to_reg* returns null rather than executing partial work.
capabilities="$(dr_database_exec "$database_url" psql -X -v ON_ERROR_STOP=1 -At -F '|' -c \
  "select to_regclass('public.data_subject_requests') is not null, to_regclass('public.legal_holds') is not null, to_regclass('public.backup_deletion_tombstones') is not null, to_regprocedure('public.compliance_export_tenant(uuid,uuid,text)') is not null, to_regprocedure('public.compliance_export_subject(uuid,uuid,text)') is not null, to_regprocedure('public.compliance_complete_export(uuid,uuid,text,text,text,integer,text)') is not null, to_regprocedure('public.compliance_delete_tenant(uuid,uuid,text)') is not null, to_regprocedure('public.compliance_delete_subject(uuid,uuid,text)') is not null")"
[[ "$capabilities" == "t|t|t|t|t|t|t|t" ]] || dr_die "required compliance migration/RPC capabilities are absent; refusing partial processing"

subject_query_id="${subject_id:-00000000-0000-4000-8000-000000000000}"
request_state="$(
  dr_database_exec "$database_url" psql -X -v ON_ERROR_STOP=1 -At -F '|' \
    --set=request_id="$request_id" --set=tenant_id="$tenant_id" --set=action="$action" \
    --set=scope="$scope" --set=subject_id="$subject_query_id" <<'SQL'
select status, (deadline_breached or (status<>'completed' and due_at<now())), (due_at <= requested_at + interval '30 days'), not exists (select 1 from public.legal_holds h where h.organization_id=r.organization_id and h.active and (h.expires_at is null or h.expires_at > now()) and h.scope in ('all', r.request_type)), coalesce(export_artifact_sha256,''), request_scope, coalesce(subject_id::text,''), to_char(requested_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'), to_char(due_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"') from public.data_subject_requests r where r.id=:'request_id'::uuid and r.organization_id=:'tenant_id'::uuid and r.request_type=:'action' and r.request_scope=:'scope' and (r.request_scope='tenant' or r.subject_id=:'subject_id'::uuid);
SQL
)"
IFS='|' read -r request_status deadline_breached valid_due_bound hold_free stored_artifact_sha256 request_scope stored_subject_id requested_at due_at <<< "$request_state"
[[ "$request_scope" == "$scope" && "$stored_subject_id" == "$subject_id" ]] \
  || dr_die "request is absent, tenant-mismatched, or subject-mismatched"
[[ "$request_status" == "approved" || "$request_status" == "processing" || "$request_status" == "completed" ]] \
  || dr_die "request is absent, tenant-mismatched, or unapproved"
[[ "$valid_due_bound" == "t" ]] || dr_die "request violates the 30-day timing constraint"
if [[ "$request_status" != "completed" ]]; then
  [[ "$hold_free" == "t" ]] || dr_die "request is blocked by an active legal hold"
fi
if [[ "$deadline_breached" == "t" ]]; then
  dr_note "ALERT compliance_data_request_deadline_breached request_id=$request_id tenant_id=$tenant_id"
fi

evidence="$evidence_dir/${action}-${scope}-${request_id}.evidence.json"
if [[ -e "$evidence" ]]; then
  [[ -f "$evidence" && ! -L "$evidence" ]] \
    || dr_die "existing data-rights evidence must be a regular non-symlink file"
fi
if [[ "$action" == "export" ]]; then
  export_path="$evidence_dir/export-${request_id}.jsonl.age"
  attestation="$evidence_dir/export-${request_id}.attestation.json"
  export_partial="$evidence_dir/.export-${request_id}.partial.$$"
  [[ ! -e "$export_partial" ]] || dr_die "stale partial export requires restricted operator review"
  dr_require_command age
  dr_secret_from_env "$evidence_hmac_key_env" >/dev/null
  age_identity="$(dr_secret_from_env "$age_identity_env")"
  identity_file="$(mktemp "${TMPDIR:-/tmp}/brevitas-export-age-identity.XXXXXX")"
  summary_file="$(mktemp "${TMPDIR:-/tmp}/brevitas-export-summary.XXXXXX")"
  rm -f -- "$summary_file"
  printf '%s\n' "$age_identity" > "$identity_file"; chmod 600 "$identity_file"
  trap 'rm -f -- "$export_partial" "$identity_file" "$summary_file"' EXIT INT TERM
  if [[ -e "$export_path" ]]; then
    [[ -f "$export_path" && ! -L "$export_path" ]] \
      || dr_die "existing export artifact must be a regular non-symlink file"
    [[ -f "$attestation" && ! -L "$attestation" ]] \
      || dr_die "existing export requires its signed request-bound attestation sidecar"
    dr_note "RESUME: verifying signed request binding and decrypting the existing portable export before finalize/evidence recovery."
  else
    [[ "$request_status" != "completed" ]] \
      || dr_die "completed export is missing its encrypted artifact; create a new approved request"
    age_recipient="$(dr_secret_from_env "$age_recipient_env")"
    if [[ "$scope" == "tenant" ]]; then
      export_rpc="compliance_export_tenant"
    else
      export_rpc="compliance_export_subject"
    fi
    dr_database_exec "$database_url" psql -X -v ON_ERROR_STOP=1 -qAt \
      --set=export_rpc="$export_rpc" --set=request_id="$request_id" \
      --set=tenant_id="$tenant_id" --set=actor_id="$actor_id" <<'SQL' \
      | "$SCRIPT_DIR/portable-export.py" --decrypt-command-env "$decrypt_command_env" \
      | age --encrypt --recipient "$age_recipient" --output "$export_partial"
select public.:"export_rpc"(:'tenant_id'::uuid, :'request_id'::uuid, :'actor_id') as record;
SQL
    chmod 600 "$export_partial"
    python3 - "$export_partial" "$export_path" <<'PY'
import os,sys
source,target=sys.argv[1:]
try:
    os.link(source,target,follow_symlinks=False)
except OSError:
    raise SystemExit("ERROR: portable export could not be published atomically without replacement")
os.unlink(source)
PY
  fi
  artifact_name="$(basename -- "$export_path")"
  verification_args=(
    --artifact "$export_path" --sidecar "$attestation" --summary "$summary_file"
    --identity-file "$identity_file"
    --request-id "$request_id" --tenant-id "$tenant_id" --scope "$scope"
    --subject-id "$subject_id" --actor-id "$actor_id" --target-id "$target_id"
    --environment "$environment" --requested-at "$requested_at" --due-at "$due_at"
    --deadline-breached "$([[ "$deadline_breached" == "t" ]] && printf true || printf false)"
    --hmac-key-env "$evidence_hmac_key_env"
  )
  portable_proof="$($SCRIPT_DIR/verify-and-attest-export.py "${verification_args[@]}")"
  proof_values="$(python3 - "$portable_proof" <<'PY'
import json,sys
document=json.loads(sys.argv[1])
print(f"{document['artifact_sha256']}|{document['attestation_sha256']}|{document['portable_record_count']}|{document['portable_records_sha256']}")
PY
)"
  IFS='|' read -r artifact_sha256 attestation_sha256 portable_record_count portable_records_sha256 <<< "$proof_values"
  if [[ -n "$stored_artifact_sha256" && "$stored_artifact_sha256" != "$artifact_sha256" ]]; then
    dr_die "existing export artifact digest does not match finalized database evidence"
  fi
  finalized="$(
    dr_database_exec "$database_url" psql -X -v ON_ERROR_STOP=1 -qAt \
      --set=request_id="$request_id" --set=tenant_id="$tenant_id" --set=actor_id="$actor_id" \
      --set=artifact_sha256="$artifact_sha256" --set=attestation_sha256="$attestation_sha256" \
      --set=portable_record_count="$portable_record_count" \
      --set=portable_records_sha256="$portable_records_sha256" <<'SQL'
select public.compliance_complete_export(:'tenant_id'::uuid,:'request_id'::uuid,:'actor_id',:'artifact_sha256',:'attestation_sha256',:'portable_record_count'::integer,:'portable_records_sha256');
SQL
  )"
  [[ "$finalized" == "completed" ]] || dr_die "encrypted export was not transactionally finalized"
else
  if [[ "$scope" == "tenant" ]]; then
    delete_rpc="compliance_delete_tenant"
  else
    delete_rpc="compliance_delete_subject"
  fi
  result="$(
    dr_database_exec "$database_url" psql -X -v ON_ERROR_STOP=1 -qAt \
      --set=delete_rpc="$delete_rpc" --set=request_id="$request_id" \
      --set=tenant_id="$tenant_id" --set=actor_id="$actor_id" <<'SQL'
select public.:"delete_rpc"(:'tenant_id'::uuid, :'request_id'::uuid, :'actor_id');
SQL
  )"
  [[ "$result" == "completed" ]] || dr_die "transactional scoped deletion did not complete"
  tombstone="$(
    dr_database_exec "$database_url" psql -X -v ON_ERROR_STOP=1 -At \
      --set=request_id="$request_id" --set=tenant_id="$tenant_id" <<'SQL'
select exists(select 1 from public.backup_deletion_tombstones t join public.data_subject_requests r on r.id=t.request_id where t.request_id=:'request_id'::uuid and t.organization_id=:'tenant_id'::uuid and t.expires_at <= r.requested_at + interval '35 days');
SQL
  )"
  [[ "$tombstone" == "t" ]] || dr_die "deletion completed without a valid <=35-day backup tombstone"
  artifact_name=""
  artifact_sha256=""
fi

completion="$(
  dr_database_exec "$database_url" psql -X -v ON_ERROR_STOP=1 -At -F '|' \
    --set=request_id="$request_id" --set=tenant_id="$tenant_id" <<'SQL'
select status,deadline_breached::text,coalesce(export_artifact_sha256,''),coalesce(export_attestation_sha256,''),coalesce(portable_record_count::text,''),coalesce(portable_records_sha256,''),to_char(completed_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"') from public.data_subject_requests where id=:'request_id'::uuid and organization_id=:'tenant_id'::uuid;
SQL
)"
IFS='|' read -r completion_status recorded_deadline_breach completed_artifact_sha256 completed_attestation_sha256 completed_portable_record_count completed_portable_records_sha256 completed_at <<< "$completion"
[[ "$completion_status" == "completed" ]] || dr_die "request completion evidence is missing"
if [[ "$deadline_breached" == "t" && "$recorded_deadline_breach" != "true" ]]; then
  dr_die "overdue request completed without deadline-breach evidence"
fi
if [[ "$action" == "export" && "$completed_artifact_sha256" != "$artifact_sha256" ]]; then
  dr_die "finalized export digest does not match the encrypted artifact"
fi
if [[ "$action" == "export" && ( "$completed_attestation_sha256" != "$attestation_sha256" \
   || "$completed_portable_record_count" != "$portable_record_count" \
   || "$completed_portable_records_sha256" != "$portable_records_sha256" ) ]]; then
  dr_die "finalized portable export proof does not match the signed attestation"
fi

attestation_path="${attestation-}"
python3 - "$evidence" "$request_id" "$tenant_id" "$scope" "$subject_id" "$action" "$target_id" "$environment" "$actor_id" "$artifact_name" "$artifact_sha256" "$recorded_deadline_breach" "$requested_at" "$due_at" "$completed_at" "$attestation_path" "${attestation_sha256-}" <<'PY'
import json
import os
import pathlib
import sys

path,request_id,tenant_id,scope,subject_id,action,target,environment,actor,artifact,digest,deadline_breached,requested_at,due_at,completed_at,attestation_path,attestation_digest=sys.argv[1:]
attestation=None
if action=="export":
    attestation=json.loads(pathlib.Path(attestation_path).read_text())
    if attestation.get("artifact_sha256")!=digest or attestation.get("status")!="portable_export_verified":
        raise SystemExit("ERROR: completed export attestation does not match final artifact")
document = {
    "schema": "brevitas.data-rights-evidence.v3",
    "request_id": request_id,
    "tenant_id": tenant_id,
    "request_scope": scope,
    "subject_id": subject_id or None,
    "action": action,
    "target_id": target,
    "environment": environment,
    "actor_id": actor,
    "status": "completed",
    "requested_at": requested_at,
    "due_at": due_at,
    "completed_at": completed_at,
    "primary_deadline_days": 30,
    "deadline_breached": deadline_breached == "true",
    "backup_expiry_days": 35 if action == "delete" else None,
    "encrypted_artifact": artifact or None,
    "artifact_sha256": digest or None,
    "artifact_retention_hours": attestation.get("artifact_retention_hours") if attestation else None,
    "artifact_expires_at": attestation.get("artifact_expires_at") if attestation else None,
    "attestation_file": pathlib.Path(attestation_path).name if attestation else None,
    "attestation_sha256": attestation_digest or None,
    "attestation_signature": attestation.get("signature") if attestation else None,
    "portable_record_count": attestation.get("portable_record_count") if attestation else None,
    "portable_records_sha256": attestation.get("portable_records_sha256") if attestation else None,
    "ciphertext_only_records": attestation.get("ciphertext_only_records") if attestation else None,
    "general_telemetry_content_exported": False,
}
evidence_path = pathlib.Path(path)
if evidence_path.exists():
    flags=os.O_RDONLY|(os.O_NOFOLLOW if hasattr(os,"O_NOFOLLOW") else 0)
    descriptor=os.open(evidence_path,flags)
    with os.fdopen(descriptor,encoding="utf-8") as stream:
        existing=json.load(stream)
    if existing != document:
        raise SystemExit("ERROR: existing data-rights evidence conflicts with completed request")
else:
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
    if hasattr(os,"O_NOFOLLOW"):
        flags|=os.O_NOFOLLOW
    descriptor=os.open(evidence_path,flags,0o600)
    with os.fdopen(descriptor,"w",encoding="utf-8") as stream:
        stream.write(json.dumps(document,indent=2,sort_keys=True)+"\n")
PY
dr_note "Data-rights request completed. Restricted evidence: $evidence"
