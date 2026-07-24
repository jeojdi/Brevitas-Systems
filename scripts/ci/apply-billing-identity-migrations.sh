#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

node scripts/ci/billing-migration-maintenance-gate.mjs
node scripts/ci/verify-billing-maintenance-deployment.mjs

billing_identity_migrations=()
while IFS= read -r migration; do
  billing_identity_migrations+=("${migration}")
done < <(
  node --input-type=module -e \
    "import { BILLING_IDENTITY_MIGRATIONS as migrations } from './scripts/ci/billing-migration-maintenance-gate.mjs'; for (const migration of migrations) console.log(migration)"
)
if [[ "${#billing_identity_migrations[@]}" -ne 3 ]]; then
  echo 'Billing identity migration inventory is incomplete.' >&2
  exit 2
fi

actual_database="$({ PGOPTIONS='-c default_transaction_read_only=on' \
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --tuples-only --no-align --command 'select current_database()'; } | tr -d '[:space:]')"
if [[ "${actual_database}" != "${BREVITAS_BILLING_MIGRATION_EXPECTED_DATABASE}" ]]; then
  echo 'Connected PostgreSQL database does not match the approved billing migration target.' >&2
  exit 2
fi

prerequisites="$({ PGOPTIONS='-c default_transaction_read_only=on' \
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --tuples-only --no-align --command \
    "select to_regprocedure('public.claim_stripe_webhook_event(text,text,uuid,integer)') is not null and to_regprocedure('public.submit_waitlist_signup(text,text,text,text,text,text,text,text,boolean)') is not null and to_regprocedure('public.enforce_service_key_billing_owner()') is not null"; } | tr -d '[:space:]')"
if [[ "${prerequisites}" != 't' ]]; then
  echo 'Billing identity rollout requires completed migrations 200001-200003.' >&2
  exit 2
fi

billing_identity_state_sql="
select case
when
    to_regprocedure('public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],timestamptz,text)') is not null
    and to_regprocedure('public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text)') is not null
    and to_regprocedure('public.company_billing_authorize_actor(uuid)') is not null
    and coalesce((
        select procedure_state.proargnames[1]
          from pg_catalog.pg_proc procedure_state
         where procedure_state.oid=to_regprocedure('public.compare_and_set_stripe_subscription_snapshot(uuid,bigint,bigint,text,text,text,text,timestamptz,timestamptz,timestamptz)')
    ),'')='p_organization_id'
    and coalesce((
        select procedure_state.proargnames[1]
          from pg_catalog.pg_proc procedure_state
         where procedure_state.oid=to_regprocedure('public.compare_and_set_stripe_invoice_snapshot(uuid,bigint,bigint,text,text,text,text)')
    ),'')='p_organization_id'
    and exists (
        select 1 from pg_catalog.pg_constraint constraint_state
         where constraint_state.conrelid='public.billing_accounts'::regclass
           and constraint_state.contype='p'
           and pg_get_constraintdef(constraint_state.oid)='PRIMARY KEY (organization_id)'
    )
then 'complete'
when
    to_regprocedure('public.company_billing_authorize_actor(uuid)') is not null
    or coalesce((
        select procedure_state.proargnames[1]
          from pg_catalog.pg_proc procedure_state
         where procedure_state.oid=to_regprocedure('public.compare_and_set_stripe_subscription_snapshot(uuid,bigint,bigint,text,text,text,text,timestamptz,timestamptz,timestamptz)')
    ),'')='p_organization_id'
    or coalesce((
        select procedure_state.proargnames[1]
          from pg_catalog.pg_proc procedure_state
         where procedure_state.oid=to_regprocedure('public.compare_and_set_stripe_invoice_snapshot(uuid,bigint,bigint,text,text,text,text)')
    ),'')='p_organization_id'
    or exists (
        select 1 from pg_catalog.pg_constraint constraint_state
         where constraint_state.conrelid='public.billing_accounts'::regclass
           and constraint_state.contype='p'
           and pg_get_constraintdef(constraint_state.oid)='PRIMARY KEY (organization_id)'
    )
then 'inconsistent-company-scoped'
else 'pending'
end"

read_billing_identity_state() {
  { PGOPTIONS='-c default_transaction_read_only=on' \
    psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
      --tuples-only --no-align --command "${billing_identity_state_sql}"; } \
    | tr -d '[:space:]'
}

initial_state="$(read_billing_identity_state)"
case "${initial_state}" in
  complete)
    echo 'Billing identity migrations 200004-200006 are already complete; validated the company-scoped postcondition and skipped reapplication.'
    echo 'Billing remains disabled until the new API and worker pass staging validation.'
    exit 0
    ;;
  pending) ;;
  inconsistent-company-scoped)
    echo 'Billing identity state is company-scoped but incomplete; refusing to replay earlier identity migrations. Keep billing disabled and investigate schema drift.' >&2
    exit 1
    ;;
  *)
    echo 'Billing identity state probe returned an invalid result; keep billing disabled.' >&2
    exit 1
    ;;
esac

trap 'echo "Billing identity rollout failed; keep API/webhook and worker billing disabled, then rerun the idempotent procedure." >&2' ERR
for migration in "${billing_identity_migrations[@]}"; do
  echo "Applying atomic billing maintenance migration ${migration}"
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --file "${migration}"
done

rollout_state="$(read_billing_identity_state)"
if [[ "${rollout_state}" != 'complete' ]]; then
  echo 'Billing identity rollout postcondition failed; keep billing disabled.' >&2
  exit 1
fi
trap - ERR

echo 'Billing identity migrations 200004-200006 passed. Billing remains disabled until the new API and worker pass staging validation.'
