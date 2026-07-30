# Apply checklist — audit migrations → wyfz (production)

Generated 2026-07-30. Apply **DB-first, before** the Railway/Vercel deploy of
branch `security/enterprise-audit-2026-07-30`.

Why DB-first: every fix in that branch was written to *degrade* when its
migration is missing (billing status renders "Unavailable", device-key revocation
falls back to session-only, the waitlist network bucket fails open to the
existing global bucket). DB-ahead-of-code is therefore harmless; code-ahead-of-DB
is not.

wyfz has **no migration ledger**, so do not replay the chain blind. Run the
PRECHECK for each file first: if it already returns `t`, that migration is
already applied — skip it.

All 21 files below are verified green on live PostgreSQL 17.10 via
`scripts/ci/run-migration-tests.sh`, on both fresh-install and upgrade paths.

## Per file

Apply with:

```
supabase db query --linked -f supabase/migrations/<FILE>
```

| # | File | PRECHECK (skip if `t`) |
|---|---|---|
| 1 | `202607280006_retire_per_row_fee_trigger.sql` | `select not exists (select 1 from pg_trigger where tgname='usage_log_brevitas_fee');` |
| 2 | `202607280007_period_settlement_ledger.sql` | `select to_regclass('public.period_settlement_ledger') is not null;` |
| 3 | `202607280008_billing_halting_conditions.sql` | `select to_regprocedure('public.assert_billing_period_halting_conditions(uuid,timestamptz)') is not null;` |
| 4 | `202607280009_billing_arrangement_attestation.sql` | `select to_regclass('public.organization_billing_arrangement') is not null;` |
| 5 | `202607280010_period_settlement_send_latches.sql` | *(in-flight; owner to confirm intent)* |
| 6 | `202607280011_billing_ledger_unsent_release.sql` | *(in-flight; owner to confirm intent)* |
| 7 | `202607280012_settlement_evidence_warm_days.sql` | *(in-flight; owner to confirm intent)* |
| 8 | `202607280013_period_settlement_writer.sql` | `select to_regprocedure('public.billing_period_settlement_summary(uuid,timestamptz)') is not null;` |
| 9 | `202607280014_drop_billing_monthly_view.sql` | `select to_regclass('public.billing_monthly') is null;` |
| 10 | `202607280015_browser_role_privilege_contract.sql` | `select to_regprocedure('public.assert_browser_role_table_privileges()') is not null;` |
| 11 | `202607280016_compliance_warm_state_erasure.sql` | `select pg_get_functiondef(to_regprocedure('public.compliance_delete_tenant(uuid,uuid,text)')) like '%warm_credentials%';` |
| 12 | `202607280017_warm_evidence_retention_floor.sql` | `select to_regprocedure('public.purge_warm_state(integer)') is not null;` |
| 13 | `202607280018_warm_claim_lease_fence.sql` | `select pg_get_functiondef(to_regprocedure('public.warm_prefix_observe(uuid,text,text,boolean)')) not like '%next_due_at = %';` |
| 14 | `202607280019_tenant_device_key_revocation.sql` | `select to_regprocedure('public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)') is not null;` |
| 15 | `202607280020_expired_job_reclaim_fence.sql` | `select pg_get_functiondef(to_regprocedure('public.claim_ai_job(text,integer)')) like '%retention%';` |
| 16 | `202607280021_server_authoritative_legal_acceptance.sql` | `select to_regprocedure('public.record_legal_acceptance(uuid,text,text)') is not null;` |
| 17 | `202607280022_audit_read_and_transition_evidence.sql` | `select to_regprocedure('public.append_company_audit_read(uuid,uuid,text,jsonb)') is not null;` |
| 18 | `202607280023_retention_minimization_and_waitlist.sql` | `select to_regprocedure('public.erase_waitlist_signup(text)') is not null;` |
| 19 | `202607280024_browser_role_privilege_completion.sql` | `select has_table_privilege('anon','public.audit_events','select') = false;` |
| 20 | `202607280025_waitlist_network_budget.sql` | `select to_regprocedure('public.consume_waitlist_network_budget(text,integer,integer)') is not null;` |
| 21 | `202607280026_usage_log_authority_dedupe.sql` | `select exists (select 1 from pg_indexes where indexname='usage_log_request_authority_unique');` |

Signatures are from the local files; if a PRECHECK errors on an argument
mismatch, check the actual signature with:
`select oid::regprocedure from pg_proc where proname = '<name>';`

## Before you start

`202607280026` swaps the `usage_log` dedupe unique index. Confirm nothing would
block it:

```sql
select key_hash, request_id, count(*)
from public.usage_log
where request_id <> ''
group by 1,2 having count(*) > 1 limit 5;
```

Zero rows expected. If not, resolve those before applying #21.

## After all 21

```sql
-- no reserved-namespace squatters left by any legacy import
select count(*) from public.usage_log
where request_id like 'proxy:%' and authoritative is not true;   -- expect 0

-- browser roles hold nothing on tenant tables
select public.assert_browser_role_table_privileges();            -- expect no error
```

Then deploy the branch (Railway + Vercel). Confirm the dashboard is live before
starting the `amount_owed_usd` deprecation clock, and remove
`SupabaseUsageStore._revoke_key_via_predispatcher_rpc` once #14 is confirmed
applied.
