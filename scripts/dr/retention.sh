#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'EOF'
Usage: retention.sh --environment ENV --target-id ID --run-id UUID
       --actor-id ID --evidence-dir DIR [--batch-limit 5000]
       [--database-url-env NAME]
       [--dry-run | --apply --confirm RETENTION:TARGET:RUN_UUID]
       [--allow-production]

Dry-run connects read-only through the retention RPC and returns bounded
candidate counts. Apply deletes at most batch-limit rows from each policy class,
preserves legal holds and financial-ledger usage, and writes content-free
evidence. Repeat with new run IDs until every candidate count is zero.
EOF
}

environment=""; target_id=""; run_id=""; actor_id=""; evidence_dir=""
batch_limit="5000"; database_url_env="COMPLIANCE_DATABASE_URL"
mode="dry-run"; confirmation=""; allow_production="false"
while (($#)); do
  case "$1" in
    --environment) environment="${2-}"; shift 2 ;;
    --target-id) target_id="${2-}"; shift 2 ;;
    --run-id) run_id="${2-}"; shift 2 ;;
    --actor-id) actor_id="${2-}"; shift 2 ;;
    --evidence-dir) evidence_dir="${2-}"; shift 2 ;;
    --batch-limit) batch_limit="${2-}"; shift 2 ;;
    --database-url-env) database_url_env="${2-}"; shift 2 ;;
    --dry-run) mode="dry-run"; shift ;;
    --apply) mode="apply"; shift ;;
    --confirm) confirmation="${2-}"; shift 2 ;;
    --allow-production) allow_production="true"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; dr_die "unknown or incomplete argument" ;;
  esac
done
[[ -n "$environment" && -n "$target_id" && -n "$run_id" \
   && -n "$actor_id" && -n "$evidence_dir" ]] \
  || { usage >&2; dr_die "environment, target, run, actor, and evidence arguments are required"; }
dr_validate_environment "$environment"; dr_validate_identifier "target ID" "$target_id"
dr_validate_uuid "run ID" "$run_id"; dr_validate_identifier "actor ID" "$actor_id"
[[ "$actor_id" =~ ^(system|brevitas_admin):[A-Za-z0-9._:-]{3,96}$ ]] \
  || dr_die "actor ID must be an opaque system or brevitas_admin identity"
[[ "$batch_limit" =~ ^[0-9]+$ && "$batch_limit" -ge 1 && "$batch_limit" -le 10000 ]] \
  || dr_die "batch limit must be between 1 and 10000"
dr_validate_env_name "$database_url_env"; dr_require_production_opt_in "$environment" "$allow_production"
if [[ "$mode" == "apply" ]]; then
  dr_require_confirmation "$confirmation" "RETENTION:$target_id:$run_id"
fi
dr_safe_directory "$evidence_dir"; dr_require_command psql; dr_require_command python3
evidence="$evidence_dir/retention-${run_id}-${mode}.json"
[[ ! -e "$evidence" && ! -L "$evidence" ]] \
  || dr_die "retention evidence already exists; refusing database operation"
database_url="$(dr_secret_from_env "$database_url_env")"
capability="$(PGDATABASE="$database_url" PGCONNECT_TIMEOUT=10 psql -X -v ON_ERROR_STOP=1 -qAt -c \
  "select to_regprocedure('public.compliance_run_retention(uuid,text,integer,boolean)') is not null")"
[[ "$capability" == "t" ]] || dr_die "compliance retention RPC is unavailable"
apply_value="false"; [[ "$mode" == "apply" ]] && apply_value="true"
result="$(PGDATABASE="$database_url" PGCONNECT_TIMEOUT=10 psql -X -v ON_ERROR_STOP=1 -qAt \
  --set=run_id="$run_id" --set=actor_id="$actor_id" --set=batch_limit="$batch_limit" \
  --set=apply_value="$apply_value" -c \
  "select public.compliance_run_retention(:'run_id'::uuid,:'actor_id',:'batch_limit'::integer,:'apply_value'::boolean)")"
python3 - "$result" "$evidence" "$environment" "$target_id" "$actor_id" "$mode" "$run_id" "$batch_limit" <<'PY'
import json,os,sys

raw,path,environment,target,actor,mode,run_id,batch_limit=sys.argv[1:]
try:
    result=json.loads(raw)
except json.JSONDecodeError:
    raise SystemExit("ERROR: retention RPC returned invalid evidence")
count_keys={
    "usage_candidates","audit_candidates","support_candidates","requests_candidates",
    "holds_candidates","prior_run_evidence_candidates","usage_deleted","audit_deleted",
    "support_deleted","requests_deleted","holds_deleted","prior_run_evidence_deleted",
}
expected={"schema","mode","run_id","batch_limit","idempotent_replay",
          "evidence_contains_customer_content"}|count_keys
if not isinstance(result,dict) or set(result)!=expected \
        or result.get("schema")!="brevitas.compliance-retention-result.v1" \
        or result.get("mode") != ("apply" if mode=="apply" else "dry_run") \
        or result.get("run_id")!=run_id or result.get("batch_limit")!=int(batch_limit) \
        or not isinstance(result.get("idempotent_replay"),bool) \
        or result.get("evidence_contains_customer_content") is not False:
    raise SystemExit("ERROR: retention RPC evidence contract mismatch")
for key in count_keys:
    value=result[key]
    if not isinstance(value,int) or isinstance(value,bool) or value<0 or value>result["batch_limit"]:
        raise SystemExit("ERROR: retention RPC count exceeds its bound")
document={
    "schema":"brevitas.compliance-retention-evidence.v1",
    "environment":environment,"target_id":target,"actor_id":actor,
    "policy":{"usage_months":13,"audit_days":400,"support_months":24,
              "completed_request_evidence_days":400,"financial_minimum_years":7},
    "result":result,"evidence_contains_customer_content":False,
}
flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
if hasattr(os,"O_NOFOLLOW"):
    flags|=os.O_NOFOLLOW
try:
    descriptor=os.open(path,flags,0o600)
except OSError:
    raise SystemExit("ERROR: retention evidence could not be created exclusively")
with os.fdopen(descriptor,"w",encoding="utf-8") as stream:
    stream.write(json.dumps(document,indent=2,sort_keys=True)+"\n")
PY
dr_note "Retention $mode completed with bounded content-free evidence: $evidence"
