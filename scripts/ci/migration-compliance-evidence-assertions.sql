\set ON_ERROR_STOP on

-- Behavioural contract for 202607280016_compliance_warm_state_erasure.sql,
-- 202607280021_server_authoritative_legal_acceptance.sql and
-- 202607280023_retention_minimization_and_waitlist.sql. All fixture state is
-- discarded by the trailing rollback, so this file is re-runnable and
-- order-independent with respect to every other assertion file.
begin;

insert into auth.users (id, email) values (
    '7f000000-0000-4000-8000-000000000001',
    'warm-erasure-owner@example.invalid');
insert into public.organizations (id, name, billing_owner_id) values (
    '7f100000-0000-4000-8000-000000000001', 'Warm erasure tenant',
    '7f000000-0000-4000-8000-000000000001');
insert into public.organization_members (organization_id, user_id, role, status)
values (
    '7f100000-0000-4000-8000-000000000001',
    '7f000000-0000-4000-8000-000000000001', 'company_owner', 'active');
insert into public.customers (id, organization_id, external_id) values (
    '7f200000-0000-4000-8000-000000000001',
    '7f100000-0000-4000-8000-000000000001', 'warm-erasure-customer');

select public.warm_credentials_upsert(
    '7f100000-0000-4000-8000-000000000001', 'anthropic',
    'warm-erasure-provider-key-ciphertext', true,
    '7f000000-0000-4000-8000-000000000001', 5.0, 100, 288);
select public.warm_prefix_observe(
    '7f100000-0000-4000-8000-000000000001',
    '7f200000-0000-4000-8000-000000000001', 'anthropic', repeat('4f', 32),
    'warm-erasure-prefix-payload', 100000, 300, 60, false, 0.5);
insert into public.warm_budget_ledger (
    organization_id, provider, day, reserved_usd, spent_usd
) values (
    '7f100000-0000-4000-8000-000000000001', 'anthropic',
    (now() at time zone 'utc')::date, 0, 3.5
) on conflict (organization_id, provider, day) do update
    set spent_usd = excluded.spent_usd;

insert into public.data_subject_requests (
    id, organization_id, request_type, request_scope, subject_id, status,
    evidence_reference, requested_at, due_at, approved_at, approved_by, created_by
) values
    ('7f300000-0000-4000-8000-000000000001',
     '7f100000-0000-4000-8000-000000000001', 'export', 'tenant', null,
     'approved', 'evidence:warm:export', now() - interval '1 day',
     now() + interval '29 days', now(), 'system:warm-erasure',
     'system:warm-erasure'),
    ('7f300000-0000-4000-8000-000000000002',
     '7f100000-0000-4000-8000-000000000001', 'delete', 'tenant', null,
     'approved', 'evidence:warm:delete', now() - interval '1 day',
     now() + interval '29 days', now(), 'system:warm-erasure',
     'system:warm-erasure');

-- Portability first: the export must no longer omit the warming tables.
create temporary table warm_tenant_export(record jsonb) on commit drop;
insert into warm_tenant_export(record)
select value from public.compliance_export_tenant(
    '7f100000-0000-4000-8000-000000000001',
    '7f300000-0000-4000-8000-000000000001',
    'system:warm-erasure'
) value;

do $$
begin
    if not exists (
        select 1 from warm_tenant_export
         where record->>'record_type' = 'warming_credential'
           and record->'data'->>'provider' = 'anthropic'
           and (record->'data'->>'consent_actor_id')::uuid
               = '7f000000-0000-4000-8000-000000000001'
    ) or not exists (
        select 1 from warm_tenant_export
         where record->>'record_type' = 'warming_prefix'
           and record->'data'->>'prefix_hash' = repeat('4f', 32)
    ) or not exists (
        select 1 from warm_tenant_export
         where record->>'record_type' = 'warming_budget_ledger'
           and (record->'data'->>'spent_usd')::numeric = 3.5
    ) then
        raise exception 'the tenant export still omits cache-warming records';
    end if;
    -- Ciphertext must not leak into an artifact whose envelope decoder cannot
    -- verify its context (scripts/dr/portable-export.py:45-54 allowlists three
    -- kind/purpose pairs and fails closed on anything else).
    if exists (
        select 1 from warm_tenant_export
         where record::text like '%warm-erasure-provider-key-ciphertext%'
            or record::text like '%warm-erasure-prefix-payload%'
    ) then
        raise exception
            'warming ciphertext was exported outside a supported envelope';
    end if;
end;
$$;

-- THE ERASURE ASSERTION.
select public.compliance_delete_tenant(
    '7f100000-0000-4000-8000-000000000001',
    '7f300000-0000-4000-8000-000000000002',
    'system:warm-erasure');

do $$
begin
    if exists (
        select 1 from public.warm_credentials credential
         where credential.organization_id = '7f100000-0000-4000-8000-000000000001'
    ) then
        raise exception
            'tenant erasure left the provider-key ciphertext in warm_credentials';
    end if;
    if exists (
        select 1 from public.warm_prefixes prefix
         where prefix.organization_id = '7f100000-0000-4000-8000-000000000001'
    ) then
        raise exception 'tenant erasure left encrypted prefix payloads behind';
    end if;
    -- warm_budget_ledger is content-free and is the operand settlement
    -- recomputes the warm deduction from, so erasure must NOT touch it.
    if not exists (
        select 1 from public.warm_budget_ledger ledger
         where ledger.organization_id = '7f100000-0000-4000-8000-000000000001'
           and ledger.spent_usd = 3.5
    ) then
        raise exception
            'tenant erasure destroyed the warm spend evidence a retained period settles against';
    end if;
    if not exists (
        select 1 from public.backup_deletion_tombstones tombstone
         where tombstone.request_id = '7f300000-0000-4000-8000-000000000002'
    ) or not exists (
        select 1 from public.data_subject_requests request
         where request.id = '7f300000-0000-4000-8000-000000000002'
           and request.status = 'completed'
    ) then
        raise exception 'the warming erasure broke the deletion state machine';
    end if;
end;
$$;

-- Reverse coverage. The gap this migration closes was not a typo, it was the
-- absence of any check that an organization_id-bearing table is either erased or
-- deliberately retained. Every new tenant table must now be classified.
do $$
declare
    v_chain text;
    v_table text;
    v_retained text[] := array[
        -- Immutable or financial evidence, retained by policy:
        'audit_events', 'legal_holds', 'legal_hold_actions',
        'billing_ledger', 'billing_accounts', 'billing_events',
        'billing_recovery_audit', 'period_settlement_ledger',
        'organization_billing_arrangement', 'usage_log',
        'data_subject_requests', 'backup_deletion_tombstones',
        -- Content-free and short-lived operational state:
        'warm_budget_ledger', 'billing_checkout_reservations',
        'active_company_selections'
    ];
begin
    select string_agg(pg_get_functiondef(procedure.oid), ' ')
      into v_chain
      from pg_proc procedure
      join pg_namespace namespace on namespace.oid = procedure.pronamespace
     where namespace.nspname = 'public'
       and procedure.proname in (
            'compliance_delete_tenant',
            'compliance_delete_tenant_pre_company_identity',
            'compliance_anonymize_unshared_user',
            'compliance_erase_support_records');
    for v_table in
        select column_definition.table_name
          from information_schema.columns column_definition
         where column_definition.table_schema = 'public'
           and column_definition.column_name = 'organization_id'
           and exists (
                select 1 from information_schema.tables table_definition
                 where table_definition.table_schema = 'public'
                   and table_definition.table_name = column_definition.table_name
                   and table_definition.table_type = 'BASE TABLE')
         order by 1
    loop
        if position('public.' || v_table in v_chain) = 0
           and not v_table = any(v_retained) then
            raise exception
                'public.% is tenant-scoped but neither erased by compliance_delete_tenant nor listed as retained',
                v_table;
        end if;
    end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- Retention: minimize what the ledger forces us to keep, and age out waitlist.
-- ---------------------------------------------------------------------------
insert into auth.users (id, email) values (
    '7f000000-0000-4000-8000-000000000002',
    'retention-minimize-owner@example.invalid');
insert into public.organizations (id, name, billing_owner_id) values (
    '7f100000-0000-4000-8000-000000000002', 'Retention minimize tenant',
    '7f000000-0000-4000-8000-000000000002');
insert into public.billing_accounts (
    organization_id, user_id, stripe_customer_id, subscription_status,
    billing_started_at, current_period_start, current_period_end
) values (
    '7f100000-0000-4000-8000-000000000002',
    '7f000000-0000-4000-8000-000000000002', 'cus_retention_minimize', 'active',
    now() - interval '3 years', now() - interval '1 day',
    now() + interval '6 days'
) on conflict (organization_id) do nothing;
insert into public.usage_log (
    key_hash, owner_id, organization_id, request_id, ts, project, environment,
    source, session_id, pipeline, run_id, usage_raw, authoritative,
    pricing_status, verified_savings_usd, brevitas_fee_usd, receipt_source
) values (
    'retention-minimize-key', '7f000000-0000-4000-8000-000000000002',
    '7f100000-0000-4000-8000-000000000002', 'retention-minimize-receipt',
    now() - interval '20 months', 'Billing Pipeline', 'production', 'sdk',
    'session-abc', 'nightly-batch', 'run-42', '{"prompt":"customer content"}',
    true, 'priced', 4, 1, 'proxy');
insert into public.billing_ledger (
    usage_log_id, organization_id, user_id, occurred_at, fee_microusd
)
select usage.id, usage.organization_id,
       '7f000000-0000-4000-8000-000000000002', usage.ts, 1000000
  from public.usage_log usage
 where usage.request_id = 'retention-minimize-receipt';

insert into public.waitlist (email, name, company, notes, created_at) values
    ('stale-prospect@example.invalid', 'Stale Prospect', 'Old Co',
     'free-text notes that are personal data', now() - interval '30 months'),
    ('fresh-prospect@example.invalid', 'Fresh Prospect', 'New Co',
     'recent enquiry', now() - interval '1 month');

do $$
declare
    v_applied jsonb;
    v_row public.usage_log%rowtype;
begin
    v_applied := public.compliance_run_retention(
        '7f400000-0000-4000-8000-000000000001', 'system:retention-minimize',
        100, true);
    select * into v_row from public.usage_log usage
     where usage.request_id = 'retention-minimize-receipt';
    if v_row.id is null then
        raise exception
            'retention deleted a usage row the billing ledger references';
    end if;
    -- Financial columns and the correlation identity survive; everything the
    -- deletion path classifies as removable is gone.
    if v_row.verified_savings_usd <> 4 or v_row.pricing_status <> 'priced'
       or not v_row.authoritative or v_row.key_hash <> 'retention-minimize-key'
       or v_row.request_id <> 'retention-minimize-receipt' then
        raise exception 'retention minimization destroyed financial evidence';
    end if;
    if v_row.owner_id <> '' or v_row.usage_raw <> '' or v_row.session_id <> ''
       or v_row.pipeline <> '' or v_row.run_id <> ''
       or v_row.project <> 'Deleted' or v_row.environment <> 'Deleted'
       or v_row.source <> 'Deleted' then
        raise exception
            'ledger-preserved usage rows are still not minimized past the retention cutoff';
    end if;
    if (v_applied->>'usage_minimized')::integer < 1
       or (v_applied->>'usage_minimize_candidates')::integer < 1
       or (v_applied->>'waitlist_deleted')::integer < 1
       or (v_applied->>'waitlist_candidates')::integer < 1 then
        raise exception 'the retention evidence row omits the new classes: %',
            v_applied;
    end if;
    if not exists (
        select 1 from public.compliance_retention_runs run
         where run.id = '7f400000-0000-4000-8000-000000000001'
           and run.usage_minimized >= 1
           and run.waitlist_deleted >= 1
    ) then
        raise exception
            'the immutable retention evidence row does not record the new classes';
    end if;

    -- A second apply must find nothing left to minimize: an unconverged
    -- predicate would rewrite the same rows on every cycle forever.
    v_applied := public.compliance_run_retention(
        '7f400000-0000-4000-8000-000000000002', 'system:retention-minimize',
        100, true);
    if (v_applied->>'usage_minimized')::integer <> 0 then
        raise exception 'retention minimization does not converge: %', v_applied;
    end if;

    if exists (
        select 1 from public.waitlist entry
         where entry.email = 'stale-prospect@example.invalid'
    ) then
        raise exception 'waitlist PII has no retention rule';
    end if;
    if not exists (
        select 1 from public.waitlist entry
         where entry.email = 'fresh-prospect@example.invalid'
    ) then
        raise exception 'waitlist retention deleted a live prospect';
    end if;
end;
$$;

-- Erasure for an account-less prospect: there is no data_subject_requests scope
-- that can reach one, so the dedicated RPC is the only path.
do $$
declare
    v_result jsonb;
begin
    v_result := public.erase_waitlist_signup(
        'Fresh-Prospect@example.invalid', 'brevitas_admin:dsr-desk',
        'assert-waitlist-erasure');
    if v_result->>'status' <> 'erased'
       or (v_result->>'rows_deleted')::integer <> 1
       or exists (
            select 1 from public.waitlist entry
             where entry.email = 'fresh-prospect@example.invalid'
       ) then
        raise exception 'waitlist erasure did not erase the prospect: %', v_result;
    end if;
    if not exists (
        select 1 from public.audit_events event
         where event.action = 'waitlist.erased'
           and event.request_id = 'assert-waitlist-erasure'
           and event.target_id = v_result->>'subject_digest'
    ) then
        raise exception 'waitlist erasure left no audit evidence';
    end if;
    if v_result->>'subject_digest' like '%@%'
       or v_result::text like '%fresh-prospect%' then
        raise exception 'waitlist erasure evidence contains the email address';
    end if;
    v_result := public.erase_waitlist_signup(
        'fresh-prospect@example.invalid', 'brevitas_admin:dsr-desk',
        'assert-waitlist-erasure-repeat');
    if v_result->>'status' <> 'not_found'
       or not exists (
            select 1 from public.audit_events event
             where event.action = 'waitlist.erase.noop'
               and event.request_id = 'assert-waitlist-erasure-repeat'
       ) then
        raise exception 'repeating a waitlist erasure is not an audited no-op';
    end if;
    if has_function_privilege(
        'anon', 'public.erase_waitlist_signup(text,text,text)', 'EXECUTE'
    ) or has_function_privilege(
        'authenticated', 'public.erase_waitlist_signup(text,text,text)', 'EXECUTE'
    ) or not has_function_privilege(
        'service_role', 'public.erase_waitlist_signup(text,text,text)', 'EXECUTE'
    ) then
        raise exception 'the waitlist erasure RPC is not service-only';
    end if;
end;
$$;

rollback;
