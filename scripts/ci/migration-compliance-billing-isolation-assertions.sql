\set ON_ERROR_STOP on

-- Real PostgreSQL isolation fixture: one identity owns two companies with
-- independent Stripe state, retained ledger entries, and legacy event rows.
-- All fixture state is rolled back after the assertions.
begin;

insert into auth.users(id,email) values (
    'fb000000-0000-4000-8000-000000000001',
    'shared-compliance-owner@example.invalid'
) on conflict (id) do nothing;

insert into public.organizations(id,name,billing_owner_id) values
    ('fb100000-0000-4000-8000-000000000001','Compliance isolation company A',
     'fb000000-0000-4000-8000-000000000001'),
    ('fb200000-0000-4000-8000-000000000002','Compliance isolation company B',
     'fb000000-0000-4000-8000-000000000001')
on conflict (id) do nothing;

insert into public.organization_members(
    organization_id,user_id,role,status
) values
    ('fb100000-0000-4000-8000-000000000001',
     'fb000000-0000-4000-8000-000000000001','company_owner','active'),
    ('fb200000-0000-4000-8000-000000000002',
     'fb000000-0000-4000-8000-000000000001','company_owner','active')
on conflict (organization_id,user_id) do update set
    role=excluded.role,status=excluded.status;

insert into public.billing_accounts(
    organization_id,user_id,stripe_customer_id,stripe_subscription_id,
    subscription_status,checkout_session_id,billing_started_at,
    current_period_start,current_period_end,last_invoice_id,last_invoice_status
) values
    ('fb100000-0000-4000-8000-000000000001',
     'fb000000-0000-4000-8000-000000000001',
     'cus_compliance_isolation_a','sub_compliance_isolation_a','active',
     'cs_compliance_isolation_a',now()-interval '1 day',
     date_trunc('day',now()),date_trunc('day',now())+interval '7 days',
     'in_compliance_isolation_a','paid'),
    ('fb200000-0000-4000-8000-000000000002',
     'fb000000-0000-4000-8000-000000000001',
     'cus_compliance_isolation_b','sub_compliance_isolation_b','active',
     'cs_compliance_isolation_b',now()-interval '1 day',
     date_trunc('day',now()),date_trunc('day',now())+interval '7 days',
     'in_compliance_isolation_b','paid')
on conflict (organization_id) do update set
    user_id=excluded.user_id,
    stripe_customer_id=excluded.stripe_customer_id,
    stripe_subscription_id=excluded.stripe_subscription_id,
    subscription_status=excluded.subscription_status,
    checkout_session_id=excluded.checkout_session_id,
    billing_started_at=excluded.billing_started_at,
    current_period_start=excluded.current_period_start,
    current_period_end=excluded.current_period_end,
    last_invoice_id=excluded.last_invoice_id,
    last_invoice_status=excluded.last_invoice_status;

insert into public.billing_events(
    user_id,organization_id,session_id,provider,model
) values
    ('fb000000-0000-4000-8000-000000000001',
     'fb100000-0000-4000-8000-000000000001',
     'legacy_session_compliance_a','test','test'),
    ('fb000000-0000-4000-8000-000000000001',
     'fb200000-0000-4000-8000-000000000002',
     'legacy_session_compliance_b','test','test'),
    ('fb000000-0000-4000-8000-000000000001',null,
     'legacy_session_tenant_ambiguous','test','test');

insert into public.usage_log(
    key_hash,owner_id,organization_id,request_id,authoritative,
    pricing_status,verified_savings_usd,brevitas_fee_usd,receipt_source
) values
    ('compliance-isolation-key-a','fb000000-0000-4000-8000-000000000001',
     'fb100000-0000-4000-8000-000000000001','compliance-isolation-usage-a',
     true,'priced',4,1,'proxy'),
    ('compliance-isolation-key-b','fb000000-0000-4000-8000-000000000001',
     'fb200000-0000-4000-8000-000000000002','compliance-isolation-usage-b',
     true,'priced',4,1,'proxy')
on conflict (key_hash,request_id) where request_id<>'' do nothing;

do $assert_ledger_fixture$
begin
    if (select count(*) from public.billing_ledger ledger
         where ledger.organization_id in (
            'fb100000-0000-4000-8000-000000000001',
            'fb200000-0000-4000-8000-000000000002'
         )) <> 2 then
        raise exception 'compliance isolation fixture did not create both company ledgers';
    end if;
end;
$assert_ledger_fixture$;

insert into public.data_subject_requests(
    id,organization_id,request_type,request_scope,subject_id,status,
    evidence_reference,requested_at,due_at,approved_at,approved_by,created_by
) values
    ('fb300000-0000-4000-8000-000000000001',
     'fb100000-0000-4000-8000-000000000001','export','tenant',null,
     'approved','evidence:billing:isolation:tenant',
     now()-interval '1 day',now()+interval '29 days',now(),
     'system:compliance-isolation','system:compliance-isolation'),
    ('fb300000-0000-4000-8000-000000000002',
     'fb100000-0000-4000-8000-000000000001','export','member',
     'fb000000-0000-4000-8000-000000000001','approved',
     'evidence:billing:isolation:subject',
     now()-interval '1 day',now()+interval '29 days',now(),
     'system:compliance-isolation','system:compliance-isolation');

create temporary table compliance_tenant_export(record jsonb) on commit drop;
insert into compliance_tenant_export(record)
select value from public.compliance_export_tenant(
    'fb100000-0000-4000-8000-000000000001',
    'fb300000-0000-4000-8000-000000000001',
    'system:compliance-isolation'
) value;

create temporary table compliance_subject_export(record jsonb) on commit drop;
insert into compliance_subject_export(record)
select value from public.compliance_export_subject(
    'fb100000-0000-4000-8000-000000000001',
    'fb300000-0000-4000-8000-000000000002',
    'system:compliance-isolation'
) value;

do $assert_export_isolation$
declare
    v_tenant_text text;
    v_subject_text text;
begin
    select string_agg(record::text,'') into v_tenant_text
      from compliance_tenant_export;
    select string_agg(record::text,'') into v_subject_text
      from compliance_subject_export;

    if (select count(*) from compliance_tenant_export
         where record->>'record_type'='billing_account') <> 1
       or not exists (
            select 1 from compliance_tenant_export
             where record->>'record_type'='billing_account'
               and record->'data'->>'stripe_customer_id'='cus_compliance_isolation_a'
               and record->'data'->>'stripe_subscription_id'='sub_compliance_isolation_a'
               and record->'data'->>'checkout_session_id'='cs_compliance_isolation_a'
       )
       or (select count(*) from compliance_tenant_export
            where record->>'record_type'='billing_ledger') <> 1
       or exists (
            select 1
              from compliance_tenant_export exported
              join public.billing_ledger ledger
                on ledger.id=(exported.record->'data'->>'id')::bigint
             where exported.record->>'record_type'='billing_ledger'
               and ledger.organization_id<>'fb100000-0000-4000-8000-000000000001'
       )
       or (select count(*) from compliance_tenant_export
            where record->>'record_type'='legacy_billing_event') <> 1
       or not exists (
            select 1 from compliance_tenant_export
             where record->>'record_type'='legacy_billing_event'
               and record->'data'->>'organization_id'=
                   'fb100000-0000-4000-8000-000000000001'
               and record->'data'->>'session_id'='legacy_session_compliance_a'
       ) then
        raise exception 'tenant export did not emit exactly company A billing evidence';
    end if;

    if v_tenant_text like '%cus_compliance_isolation_b%'
       or v_tenant_text like '%sub_compliance_isolation_b%'
       or v_tenant_text like '%cs_compliance_isolation_b%'
       or v_tenant_text like '%in_compliance_isolation_b%'
       or v_tenant_text like '%legacy_session_compliance_b%'
       or v_tenant_text like '%legacy_session_tenant_ambiguous%' then
        raise exception 'tenant A export contained company B or ambiguous billing evidence';
    end if;

    if (select count(*) from compliance_subject_export
         where record->>'record_type'='billing_account') <> 1
       or (select count(*) from compliance_subject_export
            where record->>'record_type'='billing_ledger') <> 1
       or (select count(*) from compliance_subject_export
            where record->>'record_type'='legacy_billing_event') <> 1
       or v_subject_text like '%cus_compliance_isolation_b%'
       or v_subject_text like '%sub_compliance_isolation_b%'
       or v_subject_text like '%cs_compliance_isolation_b%'
       or v_subject_text like '%in_compliance_isolation_b%'
       or v_subject_text like '%legacy_session_compliance_b%'
       or v_subject_text like '%legacy_session_tenant_ambiguous%'
       or not exists (
            select 1
              from compliance_subject_export exported
              join public.billing_ledger ledger
                on ledger.id=(exported.record->'data'->>'id')::bigint
             where exported.record->>'record_type'='billing_ledger'
               and ledger.organization_id='fb100000-0000-4000-8000-000000000001'
       ) then
        raise exception 'member subject export crossed the requested company boundary';
    end if;
end;
$assert_export_isolation$;

-- Deleting this shared member from company A must minimize only A's ephemeral
-- Stripe/session evidence. Company B and the still-shared identity remain live.
insert into public.data_subject_requests(
    id,organization_id,request_type,request_scope,subject_id,status,
    evidence_reference,requested_at,due_at,approved_at,approved_by,created_by
) values (
    'fb300000-0000-4000-8000-000000000003',
    'fb100000-0000-4000-8000-000000000001','delete','member',
    'fb000000-0000-4000-8000-000000000001','approved',
    'evidence:billing:isolation:delete',
    now()-interval '1 day',now()+interval '29 days',now(),
    'system:compliance-isolation','system:compliance-isolation'
);

select public.compliance_delete_subject(
    'fb100000-0000-4000-8000-000000000001',
    'fb300000-0000-4000-8000-000000000003',
    'system:compliance-isolation'
);

do $assert_delete_isolation$
begin
    if not exists (
        select 1 from public.billing_accounts account
         where account.organization_id='fb100000-0000-4000-8000-000000000001'
           and account.checkout_session_id is null
    ) or not exists (
        select 1 from public.billing_accounts account
         where account.organization_id='fb200000-0000-4000-8000-000000000002'
           and account.stripe_customer_id='cus_compliance_isolation_b'
           and account.stripe_subscription_id='sub_compliance_isolation_b'
           and account.checkout_session_id='cs_compliance_isolation_b'
    ) or not exists (
        select 1 from public.billing_events event
         where event.organization_id='fb100000-0000-4000-8000-000000000001'
           and event.session_id=''
    ) or not exists (
        select 1 from public.billing_events event
         where event.organization_id='fb200000-0000-4000-8000-000000000002'
           and event.session_id='legacy_session_compliance_b'
    ) or not exists (
        select 1 from auth.users user_account
         where user_account.id='fb000000-0000-4000-8000-000000000001'
           and user_account.email='shared-compliance-owner@example.invalid'
    ) or (select count(*) from public.billing_ledger ledger
           where ledger.organization_id in (
              'fb100000-0000-4000-8000-000000000001',
              'fb200000-0000-4000-8000-000000000002'
           )) <> 2 then
        raise exception 'subject deletion mutated another company or lost financial evidence';
    end if;
end;
$assert_delete_isolation$;

do $assert_function_privileges$
declare
    v_anonymizer_definition text;
begin
    select pg_get_functiondef(
        to_regprocedure('public.compliance_anonymize_unshared_user(uuid)')
    ) into v_anonymizer_definition;
    if not has_function_privilege(
        'service_role','public.compliance_export_tenant(uuid,uuid,text)','EXECUTE'
    ) or not has_function_privilege(
        'service_role','public.compliance_export_subject(uuid,uuid,text)','EXECUTE'
    ) or has_function_privilege(
        'authenticated','public.compliance_export_tenant(uuid,uuid,text)','EXECUTE'
    ) or has_function_privilege(
        'service_role',
        'public.compliance_export_tenant_pre_company_identity(uuid,uuid,text)',
        'EXECUTE'
    ) then
        raise exception 'compliance export function privileges are unsafe';
    end if;
    if v_anonymizer_definition ilike
           '%update public.billing_accounts set checkout_session_id=null where user_id=$1%'
       or v_anonymizer_definition ilike
           '%update public.billing_events set session_id='''' where user_id=$1%' then
        raise exception 'installed anonymizer retains owner-wide billing mutation';
    end if;
end;
$assert_function_privileges$;

rollback;
