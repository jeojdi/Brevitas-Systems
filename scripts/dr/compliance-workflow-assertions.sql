\set ON_ERROR_STOP on

-- Run only against the ephemeral migration-test database after migration 007.
-- No statement in this file connects to staging or production.

do $$
declare function_name text; function_oid oid;
begin
    foreach function_name in array array[
        'compliance_submit_data_request', 'compliance_submit_subject_request',
        'compliance_approve_data_request',
        'compliance_request_legal_hold_action',
        'compliance_approve_legal_hold_action',
        'compliance_export_tenant', 'compliance_export_subject',
        'compliance_complete_export', 'compliance_delete_tenant',
        'compliance_delete_subject', 'compliance_replay_deletion_tombstone',
        'compliance_run_retention', 'compliance_retention_worker_cycle',
        'compliance_retention_worker_health'
    ] loop
        select procedure.oid into function_oid
          from pg_proc procedure join pg_namespace namespace on namespace.oid=procedure.pronamespace
         where namespace.nspname='public' and procedure.proname=function_name;
        if function_oid is null
           or not has_function_privilege('service_role', function_oid, 'EXECUTE')
           or has_function_privilege('anon', function_oid, 'EXECUTE')
           or has_function_privilege('authenticated', function_oid, 'EXECUTE') then
            raise exception 'unsafe compliance RPC privilege: %', function_name;
        end if;
    end loop;
    if has_table_privilege('service_role','public.legal_holds','INSERT')
       or has_table_privilege('service_role','public.legal_holds','UPDATE')
       or not has_table_privilege('service_role','public.legal_holds','SELECT') then
        raise exception 'legal hold table is not compliance-RPC-only';
    end if;
    if has_table_privilege('service_role','public.legal_hold_actions','INSERT')
       or has_table_privilege('service_role','public.legal_hold_actions','UPDATE')
       or has_table_privilege('service_role','public.legal_hold_actions','DELETE')
       or not has_table_privilege('service_role','public.legal_hold_actions','SELECT') then
        raise exception 'legal hold action table is not compliance-RPC-only';
    end if;
    if to_regprocedure('public.compliance_create_legal_hold(uuid,uuid,text,text,text,text,timestamptz)')
          is not null
       or to_regprocedure('public.compliance_release_legal_hold(uuid,uuid,text,text)')
          is not null then
        raise exception 'legacy single-actor legal hold RPC remains exposed';
    end if;
    if has_function_privilege(
        'service_role','public.compliance_retention_delete_immutable(timestamptz,integer)','EXECUTE'
    ) then
        raise exception 'immutable retention maintenance helper is caller-accessible';
    end if;
    if has_function_privilege(
        'service_role','public.compliance_erase_support_records(uuid,text,uuid)','EXECUTE'
    ) or has_function_privilege(
        'service_role','public.compliance_assert_usage_export_schema()','EXECUTE'
    ) or has_function_privilege(
        'service_role','public.compliance_preservation_hold(uuid)','EXECUTE'
    ) or has_function_privilege(
        'service_role','public.compliance_global_preservation_hold()','EXECUTE'
    ) or has_function_privilege(
        'service_role','public.enforce_legal_hold_action_transition()','EXECUTE'
    ) then
        raise exception 'internal compliance validation helper is caller-accessible';
    end if;
end;
$$;

-- Optional support data is never guessed. Missing adapters roll back the
-- request state; exact tenant and subject adapters must prove zero remaining.
create table public.support_records(
    id uuid primary key,
    organization_id uuid not null,
    subject_id uuid,
    created_at timestamptz not null default now(),
    body text not null
);
insert into auth.users(id,email) values
    ('a0000000-0000-4000-8000-000000000001','support-owner@example.invalid')
on conflict(id) do nothing;
insert into public.organizations(id,name,billing_owner_id) values
    ('a1000000-0000-4000-8000-000000000001','Support adapter tenant',
     'a0000000-0000-4000-8000-000000000001');
insert into public.organization_members(organization_id,user_id,role,status) values
    ('a1000000-0000-4000-8000-000000000001','a0000000-0000-4000-8000-000000000001',
     'company_owner','active');
insert into public.customers(id,organization_id,external_id,display_name) values
    ('aa000000-0000-4000-8000-000000000001','a1000000-0000-4000-8000-000000000001',
     'support-subject','Support Subject');
insert into public.support_records(id,organization_id,subject_id,body) values
    ('ab000000-0000-4000-8000-000000000001','a1000000-0000-4000-8000-000000000001',
     'aa000000-0000-4000-8000-000000000001','private subject support body'),
    ('ab000000-0000-4000-8000-000000000002','a1000000-0000-4000-8000-000000000001',
     null,'private tenant support body');
select public.compliance_submit_subject_request(
    'a1000000-0000-4000-8000-000000000001','ac000000-0000-4000-8000-000000000001',
    'delete','customer','aa000000-0000-4000-8000-000000000001',
    'system:support-submit','evidence:support:subject'
);
select public.compliance_approve_data_request(
    'a1000000-0000-4000-8000-000000000001','ac000000-0000-4000-8000-000000000001',
    'system:support-approve'
);
do $$
begin
    begin
        perform public.compliance_delete_subject(
            'a1000000-0000-4000-8000-000000000001',
            'ac000000-0000-4000-8000-000000000001','system:support-delete');
        raise exception 'subject support deletion succeeded without adapter';
    exception when object_not_in_prerequisite_state then null;
    end;
    if (select status from public.data_subject_requests
         where id='ac000000-0000-4000-8000-000000000001')<>'approved' then
        raise exception 'missing subject support adapter changed request state';
    end if;
end;
$$;
create function public.compliance_delete_support_subject(uuid,text,uuid)
returns jsonb language plpgsql as $$
declare deleted integer; remaining integer;
begin
    delete from public.support_records
     where organization_id=$1 and subject_id=$3;
    get diagnostics deleted=row_count;
    select count(*) into remaining from public.support_records
     where organization_id=$1 and subject_id=$3;
    return jsonb_build_object(
        'schema','brevitas.support-erasure.v1','organization_id',$1,
        'request_scope',$2,'subject_id',$3,'deleted_count',deleted,
        'anonymized_count',0,'remaining_count',remaining);
end;
$$;
select public.compliance_delete_subject(
    'a1000000-0000-4000-8000-000000000001','ac000000-0000-4000-8000-000000000001',
    'system:support-delete'
);
select public.compliance_submit_data_request(
    'a1000000-0000-4000-8000-000000000001','ad000000-0000-4000-8000-000000000001',
    'delete','system:support-submit','evidence:support:tenant'
);
select public.compliance_approve_data_request(
    'a1000000-0000-4000-8000-000000000001','ad000000-0000-4000-8000-000000000001',
    'system:support-approve'
);
do $$
begin
    begin
        perform public.compliance_delete_tenant(
            'a1000000-0000-4000-8000-000000000001',
            'ad000000-0000-4000-8000-000000000001','system:support-delete');
        raise exception 'tenant support deletion succeeded without adapter';
    exception when object_not_in_prerequisite_state then null;
    end;
    if (select status from public.data_subject_requests
         where id='ad000000-0000-4000-8000-000000000001')<>'approved' then
        raise exception 'missing tenant support adapter changed request state';
    end if;
end;
$$;
create function public.compliance_delete_support_records(uuid)
returns jsonb language plpgsql as $$
declare deleted integer; remaining integer;
begin
    delete from public.support_records where organization_id=$1;
    get diagnostics deleted=row_count;
    select count(*) into remaining from public.support_records where organization_id=$1;
    return jsonb_build_object(
        'schema','brevitas.support-erasure.v1','organization_id',$1,
        'request_scope','tenant','subject_id',null,'deleted_count',deleted,
        'anonymized_count',0,'remaining_count',remaining);
end;
$$;
select public.compliance_delete_tenant(
    'a1000000-0000-4000-8000-000000000001','ad000000-0000-4000-8000-000000000001',
    'system:support-delete'
);
do $$
begin
    if exists (select 1 from public.support_records)
       or exists (select 1 from public.data_subject_requests
                   where id in ('ac000000-0000-4000-8000-000000000001',
                                'ad000000-0000-4000-8000-000000000001')
                     and status<>'completed') then
        raise exception 'support adapter deletion did not complete exactly';
    end if;
end;
$$;
drop function public.compliance_delete_support_records(uuid);
drop function public.compliance_delete_support_subject(uuid,text,uuid);
drop table public.support_records;

-- Tenant offboarding anonymizes only identities whose final membership is in
-- that tenant. A user shared with another tenant remains fully usable there.
insert into auth.users(id,email,raw_user_meta_data) values
    ('d0000000-0000-4000-8000-000000000001','sole-tenant@example.invalid','{"display_name":"Sole User"}'::jsonb),
    ('d0000000-0000-4000-8000-000000000002','shared-user@example.invalid','{"display_name":"Shared User"}'::jsonb),
    ('d0000000-0000-4000-8000-000000000003','other-owner@example.invalid','{}'::jsonb)
on conflict (id) do nothing;
insert into public.organizations(id,name,billing_owner_id) values
    ('d1000000-0000-4000-8000-000000000001','Sole/shared tenant','d0000000-0000-4000-8000-000000000001'),
    ('d2000000-0000-4000-8000-000000000002','Shared survivor tenant','d0000000-0000-4000-8000-000000000003')
on conflict (id) do nothing;
insert into public.organization_members(organization_id,user_id,role,status) values
    ('d1000000-0000-4000-8000-000000000001','d0000000-0000-4000-8000-000000000001','company_owner','active'),
    ('d1000000-0000-4000-8000-000000000001','d0000000-0000-4000-8000-000000000002','member','active'),
    ('d2000000-0000-4000-8000-000000000002','d0000000-0000-4000-8000-000000000002','company_admin','active'),
    ('d2000000-0000-4000-8000-000000000002','d0000000-0000-4000-8000-000000000003','company_owner','active')
on conflict (organization_id,user_id) do nothing;
insert into public.customers(id,organization_id,external_id,display_name) values
    ('da000000-0000-4000-8000-000000000001','d1000000-0000-4000-8000-000000000001','cache-customer','Cache Customer')
on conflict(id) do nothing;
insert into public.billing_accounts(
    user_id,stripe_customer_id,subscription_status,checkout_session_id,
    billing_started_at,current_period_start,current_period_end
) values (
    'd0000000-0000-4000-8000-000000000001','cus_compliance_sole','active',
    'checkout-must-be-minimized',now()-interval '1 year',now()-interval '1 day',now()+interval '29 days'
) on conflict(user_id) do update set checkout_session_id=excluded.checkout_session_id;
insert into public.billing_events(user_id,session_id,provider,model) values
    ('d0000000-0000-4000-8000-000000000001','session-must-be-minimized','test','test');
insert into public.legal_acceptances(user_id,terms_version,accepted_at) values
    ('d0000000-0000-4000-8000-000000000001','compliance-test',now()-interval '1 year')
on conflict(user_id) do nothing;
insert into public.semantic_cache(
    exact_hash,context_hash,model_id,response_ciphertext,tenant_namespace,
    created_at,expires_at
) values
    (repeat('d',63)||'1',repeat('e',64),'test','ciphertext-one',
     encode(digest('d1000000-0000-4000-8000-000000000001:unattributed','sha256'),'hex'),
     now(),now()+interval '1 hour'),
    (repeat('d',63)||'2',repeat('e',63)||'2','test','ciphertext-two',
     encode(digest('d1000000-0000-4000-8000-000000000001:da000000-0000-4000-8000-000000000001','sha256'),'hex'),
     now(),now()+interval '1 hour')
on conflict(exact_hash) do nothing;
insert into public.data_subject_requests(
    id,organization_id,request_type,status,evidence_reference,requested_at,due_at,
    approved_at,approved_by,created_by
) values (
    'dd000000-0000-4000-8000-000000000001','d1000000-0000-4000-8000-000000000001',
    'delete','approved','evidence:tenant:identity',now()-interval '1 day',now()+interval '29 days',
    now(),'system:test','system:test'
);
select public.compliance_delete_tenant(
    'd1000000-0000-4000-8000-000000000001',
    'dd000000-0000-4000-8000-000000000001','system:test'
);
do $$
begin
    if not exists (
        select 1 from auth.users
         where id='d0000000-0000-4000-8000-000000000001'
           and email='deleted+d0000000-0000-4000-8000-000000000001@deleted.invalid'
           and raw_user_meta_data='{}'::jsonb
           and banned_until='infinity'::timestamptz
    ) or exists (select 1 from public.profiles where id='d0000000-0000-4000-8000-000000000001') then
        raise exception 'sole-tenant auth/profile PII was not converted to a non-login placeholder';
    end if;
    if not exists (
        select 1 from auth.users user_account
        join public.profiles profile on profile.id=user_account.id
        join public.organization_members member on member.user_id=user_account.id
         where user_account.id='d0000000-0000-4000-8000-000000000002'
           and user_account.email='shared-user@example.invalid'
           and member.organization_id='d2000000-0000-4000-8000-000000000002'
    ) then
        raise exception 'multi-organization user identity/profile was not preserved';
    end if;
    if not exists (
        select 1 from public.billing_accounts
         where user_id='d0000000-0000-4000-8000-000000000001'
           and stripe_customer_id='cus_compliance_sole' and checkout_session_id is null
    ) or not exists (
        select 1 from public.billing_events
         where user_id='d0000000-0000-4000-8000-000000000001' and session_id=''
    ) or not exists (
        select 1 from public.legal_acceptances
         where user_id='d0000000-0000-4000-8000-000000000001'
    ) then
        raise exception 'minimized financial/legal evidence was not preserved';
    end if;
    if exists (
        select 1 from public.semantic_cache where tenant_namespace in (
            encode(digest('d1000000-0000-4000-8000-000000000001:unattributed','sha256'),'hex'),
            encode(digest('d1000000-0000-4000-8000-000000000001:da000000-0000-4000-8000-000000000001','sha256'),'hex')
        )
    ) then
        raise exception 'tenant offboarding retained an unattributed/customer semantic-cache namespace';
    end if;
end;
$$;

-- Authoritative retention is bounded, dry-runnable, idempotent, hold-aware,
-- and never removes usage referenced by the seven-year financial ledger.
insert into auth.users(id,email) values
    ('f0000000-0000-4000-8000-000000000001','retention-owner@example.invalid'),
    ('f0000000-0000-4000-8000-000000000002','retention-held@example.invalid'),
    ('f0000000-0000-4000-8000-000000000003','retention-pending@example.invalid')
on conflict(id) do nothing;
insert into public.organizations(id,name,billing_owner_id) values
    ('f1000000-0000-4000-8000-000000000001','Retention tenant','f0000000-0000-4000-8000-000000000001'),
    ('f2000000-0000-4000-8000-000000000002','Retention held tenant','f0000000-0000-4000-8000-000000000002'),
    ('f2500000-0000-4000-8000-000000000005','Retention pending-hold tenant','f0000000-0000-4000-8000-000000000003')
on conflict(id) do nothing;
insert into public.organization_members(organization_id,user_id,role,status) values
    ('f1000000-0000-4000-8000-000000000001','f0000000-0000-4000-8000-000000000001','company_owner','active'),
    ('f2000000-0000-4000-8000-000000000002','f0000000-0000-4000-8000-000000000002','company_owner','active'),
    ('f2500000-0000-4000-8000-000000000005','f0000000-0000-4000-8000-000000000003','company_owner','active')
on conflict(organization_id,user_id) do nothing;
insert into public.billing_accounts(
    user_id,stripe_customer_id,subscription_status,billing_started_at,current_period_start,current_period_end
) values (
    'f0000000-0000-4000-8000-000000000001','cus_retention_financial','active',
    now()-interval '7 years',now()-interval '1 day',now()+interval '29 days'
) on conflict(user_id) do update set subscription_status='active';
insert into public.usage_log(
    key_hash,owner_id,organization_id,request_id,ts,baseline_tokens,
    optimized_tokens,tokens_saved,pricing_status,verified_savings_usd,
    brevitas_fee_usd,authoritative
) values
    ('retention-expired','f0000000-0000-4000-8000-000000000001',
     'f1000000-0000-4000-8000-000000000001','retention-expired',now()-interval '14 months',
     10,5,5,'unpriced',0,0,false),
    ('retention-financial','f0000000-0000-4000-8000-000000000001',
     'f1000000-0000-4000-8000-000000000001','retention-financial',now()-interval '6 years',
     100,50,50,'priced',1,0.25,true),
    ('retention-held','f0000000-0000-4000-8000-000000000002',
     'f2000000-0000-4000-8000-000000000002','retention-held',now()-interval '14 months',
     10,5,5,'unpriced',0,0,false),
    ('retention-pending','f0000000-0000-4000-8000-000000000003',
     'f2500000-0000-4000-8000-000000000005','retention-pending',now()-interval '14 months',
     10,5,5,'unpriced',0,0,false)
on conflict(key_hash,request_id) where request_id<>'' do nothing;
insert into public.legal_holds(id,organization_id,scope,reason_code,created_by) values (
    'f3000000-0000-4000-8000-000000000003','f2000000-0000-4000-8000-000000000002',
    'all','retention_test','system:test'
);
insert into public.legal_holds(
    id,organization_id,scope,reason_code,active,created_by,created_at,released_by,released_at
) values (
    'f4000000-0000-4000-8000-000000000004','f2000000-0000-4000-8000-000000000002',
    'delete','released_retention_test',false,'system:test',now()-interval '500 days',
    'system:approver',now()-interval '401 days'
);
select public.compliance_request_legal_hold_action(
    'f2500000-0000-4000-8000-000000000005',
    'f7000000-0000-4000-8000-000000000007','create',
    'f6000000-0000-4000-8000-000000000006','all','retention_pending',
    'brevitas_admin:retention-requester','audit:retention:pending:hold',null
);
insert into public.compliance_retention_runs(
    id,actor_id,batch_limit,usage_candidates,audit_candidates,support_candidates,
    requests_candidates,holds_candidates,prior_run_evidence_candidates,
    usage_deleted,audit_deleted,support_deleted,requests_deleted,holds_deleted,
    prior_run_evidence_deleted,completed_at
) values (
    'fe000000-0000-4000-8000-000000000001','system:test',1,
    0,0,0,0,0,0,0,0,0,0,0,0,now()-interval '401 days'
);
insert into public.audit_events(
    organization_id,actor_user_id,actor_key_hash,action,target_type,target_id,details,
    occurred_at,request_id,actor_id,actor_role,outcome
) values
    ('f1000000-0000-4000-8000-000000000001',null,null,'retention.fixture','company',
     'f1000000-0000-4000-8000-000000000001','{}',now()-interval '401 days',
     'retention:audit:expired','system:test','system','committed'),
    ('f2000000-0000-4000-8000-000000000002',null,null,'retention.fixture','company',
     'f2000000-0000-4000-8000-000000000002','{}',now()-interval '401 days',
     'retention:audit:held','system:test','system','committed');
insert into public.data_subject_requests(
    id,organization_id,request_type,status,evidence_reference,requested_at,due_at,
    approved_at,approved_by,completed_at,export_artifact_sha256,
    export_attestation_sha256,portable_record_count,portable_records_sha256,created_by
) values (
    'fd000000-0000-4000-8000-000000000001','f1000000-0000-4000-8000-000000000001',
    'export','completed','evidence:retention:request',now()-interval '500 days',
    now()-interval '470 days',now()-interval '499 days','system:test',
    now()-interval '401 days',repeat('a',64),repeat('b',64),1,repeat('c',64),'system:test'
);
do $$
declare dry jsonb; applied jsonb; replayed jsonb;
begin
    dry:=public.compliance_run_retention(
        'ff000000-0000-4000-8000-000000000001','system:test',1,false);
    if dry->>'mode'<>'dry_run' or (dry->>'usage_candidates')::integer<>1
       or (dry->>'audit_candidates')::integer<>1
       or (dry->>'requests_candidates')::integer<>1
       or (dry->>'usage_deleted')::integer<>0
       or not exists (select 1 from public.usage_log where request_id='retention-expired') then
        raise exception 'retention dry-run counts or no-mutation guarantee failed';
    end if;
    applied:=public.compliance_run_retention(
        'ff000000-0000-4000-8000-000000000001','system:test',1,true);
    replayed:=public.compliance_run_retention(
        'ff000000-0000-4000-8000-000000000001','system:test',1,true);
    if applied->>'mode'<>'apply' or replayed->>'idempotent_replay'<>'true'
       or replayed->>'usage_candidates'<>applied->>'usage_candidates'
       or replayed->>'audit_candidates'<>applied->>'audit_candidates'
       or replayed->>'requests_candidates'<>applied->>'requests_candidates'
       or exists (select 1 from public.usage_log where request_id='retention-expired')
       or exists (select 1 from public.audit_events where request_id='retention:audit:expired')
       or exists (select 1 from public.data_subject_requests where id='fd000000-0000-4000-8000-000000000001')
       or not exists (select 1 from public.usage_log where request_id='retention-financial')
       or not exists (select 1 from public.billing_ledger ledger join public.usage_log usage on usage.id=ledger.usage_log_id where usage.request_id='retention-financial')
       or not exists (select 1 from public.usage_log where request_id='retention-held')
       or not exists (select 1 from public.usage_log where request_id='retention-pending')
       or not exists (select 1 from public.audit_events where request_id='retention:audit:held')
       or not exists (select 1 from public.legal_hold_actions
                       where id='f7000000-0000-4000-8000-000000000007'
                         and status='pending')
       or not exists (select 1 from public.legal_holds
                       where id='f4000000-0000-4000-8000-000000000004')
       or not exists (select 1 from public.compliance_retention_runs
                       where id='fe000000-0000-4000-8000-000000000001')
       or not exists (select 1 from public.compliance_retention_runs where id='ff000000-0000-4000-8000-000000000001') then
        raise exception 'retention apply/idempotency/hold/financial preservation failed';
    end if;
    begin
        delete from public.compliance_retention_runs
         where id='ff000000-0000-4000-8000-000000000001';
        raise exception 'retention evidence mutation unexpectedly succeeded';
    exception when object_not_in_prerequisite_state then null;
    end;
end;
$$;

do $$
declare cycle jsonb; health jsonb;
begin
    cycle:=public.compliance_retention_worker_cycle(
        'fb000000-0000-4000-8000-000000000001',
        'fb000000-0000-4000-8000-000000000002',
        'fb000000-0000-4000-8000-000000000003',
        'fb000000-0000-4000-8000-000000000004',
        'retention-worker:test','system:retention-worker',100
    );
    health:=public.compliance_retention_worker_health();
    if cycle->>'status'<>'completed'
       or cycle->>'evidence_contains_customer_content'<>'false'
       or health->>'initialized'<>'true'
       or health->>'schema_contract_ok'<>'true'
       or health->>'legal_holds_evaluated'<>'true'
       or health->>'financial_ledger_preserved'<>'true'
       or health->>'evidence_contains_customer_content'<>'false'
       or not exists (select 1 from public.compliance_retention_worker_state
                        where singleton and last_cycle_id=
                            'fb000000-0000-4000-8000-000000000001') then
        raise exception 'retention worker cycle/health evidence contract failed';
    end if;
end;
$$;

-- Member and end-customer subject requests are scoped operations, not aliases
-- for tenant offboarding. Cross-tenant intake fails and legal holds block work.
insert into auth.users(id,email) values
    ('e0000000-0000-4000-8000-000000000001','subject-owner@example.invalid'),
    ('e0000000-0000-4000-8000-000000000002','member-subject@example.invalid')
on conflict(id) do nothing;
insert into public.organizations(id,name,billing_owner_id) values
    ('e1000000-0000-4000-8000-000000000001','Subject tenant','e0000000-0000-4000-8000-000000000001'),
    ('e2000000-0000-4000-8000-000000000002','Member survivor tenant','e0000000-0000-4000-8000-000000000002')
on conflict(id) do nothing;
insert into public.organization_members(organization_id,user_id,role,status) values
    ('e1000000-0000-4000-8000-000000000001','e0000000-0000-4000-8000-000000000001','company_owner','active'),
    ('e1000000-0000-4000-8000-000000000001','e0000000-0000-4000-8000-000000000002','member','active'),
    ('e2000000-0000-4000-8000-000000000002','e0000000-0000-4000-8000-000000000002','company_owner','active')
on conflict(organization_id,user_id) do nothing;
insert into public.customers(id,organization_id,external_id,display_name) values
    ('ea000000-0000-4000-8000-000000000001','e1000000-0000-4000-8000-000000000001','subject-customer','Subject Customer')
on conflict(id) do nothing;
insert into public.billing_accounts(
    user_id,stripe_customer_id,subscription_status,billing_started_at,current_period_start,current_period_end
) values
    ('e0000000-0000-4000-8000-000000000001','cus_subject_owner','active',now()-interval '1 year',now()-interval '1 day',now()+interval '29 days'),
    ('e0000000-0000-4000-8000-000000000002','cus_member_subject','active',now()-interval '1 year',now()-interval '1 day',now()+interval '29 days')
on conflict(user_id) do update set subscription_status='active';
insert into public.organization_invitations(
    id,organization_id,email_lookup_hash,token_hash,role,status,invited_by,
    created_at,expires_at,accepted_at,accepted_by
) values (
    'e4000000-0000-4000-8000-000000000004','e1000000-0000-4000-8000-000000000001',
    repeat('4',64),repeat('5',64),'member','accepted',
    'e0000000-0000-4000-8000-000000000001',now()-interval '2 days',
    now()+interval '1 day',now()-interval '1 day','e0000000-0000-4000-8000-000000000002'
);
do $$
begin
    perform public.compliance_submit_data_request(
        'e1000000-0000-4000-8000-000000000001',
        'ee000000-0000-4000-8000-000000000097','export',
        'system:test','evidence:tenant:scope'
    );
    begin
        perform public.compliance_submit_subject_request(
            'e1000000-0000-4000-8000-000000000001',
            'ee000000-0000-4000-8000-000000000097','export','member',
            'e0000000-0000-4000-8000-000000000002',
            'system:test','evidence:tenant:scope'
        );
        raise exception 'tenant request UUID was reused across subject scope';
    exception when unique_violation then null;
    end;
    perform public.compliance_submit_subject_request(
        'e1000000-0000-4000-8000-000000000001',
        'ee000000-0000-4000-8000-000000000098','export','customer',
        'ea000000-0000-4000-8000-000000000001',
        'system:test','evidence:subject:scope'
    );
    begin
        perform public.compliance_submit_data_request(
            'e1000000-0000-4000-8000-000000000001',
            'ee000000-0000-4000-8000-000000000098','export',
            'system:test','evidence:subject:scope'
        );
        raise exception 'subject request UUID was reused as tenant scope';
    exception when unique_violation then null;
    end;
end;
$$;
do $$
begin
    begin
        perform public.compliance_submit_subject_request(
            'e2000000-0000-4000-8000-000000000002','ee000000-0000-4000-8000-000000000099',
            'delete','customer','ea000000-0000-4000-8000-000000000001',
            'system:test','evidence:cross:tenant'
        );
        raise exception 'cross-tenant subject request unexpectedly succeeded';
    exception when no_data_found then null;
    end;
end;
$$;

select public.compliance_submit_subject_request(
    'e1000000-0000-4000-8000-000000000001','ee000000-0000-4000-8000-000000000001',
    'export','member','e0000000-0000-4000-8000-000000000002',
    'system:test','evidence:member:export'
);
do $$
begin
    begin
        perform public.compliance_approve_data_request(
            'e1000000-0000-4000-8000-000000000001',
            'ee000000-0000-4000-8000-000000000001','system:test');
        raise exception 'submitter approved their own compliance request';
    exception when insufficient_privilege then null;
    end;
end;
$$;
select public.compliance_approve_data_request(
    'e1000000-0000-4000-8000-000000000001',
    'ee000000-0000-4000-8000-000000000001','system:approver'
);
do $$
declare exported jsonb;
begin
    select value into exported from public.compliance_export_subject(
        'e1000000-0000-4000-8000-000000000001',
        'ee000000-0000-4000-8000-000000000001','system:test'
    ) value limit 1;
    if exported is null then raise exception 'member subject export was empty'; end if;
    perform public.compliance_complete_export(
        'e1000000-0000-4000-8000-000000000001',
        'ee000000-0000-4000-8000-000000000001','system:test',repeat('e',64),
        repeat('d',64),1,repeat('c',64)
    );
end;
$$;

insert into public.usage_log(
    key_hash,owner_id,organization_id,request_id,ts,baseline_tokens,
    optimized_tokens,tokens_saved,pricing_status,verified_savings_usd,
    brevitas_fee_usd,authoritative
) values (
    'member-subject-usage','e0000000-0000-4000-8000-000000000002',
    'e1000000-0000-4000-8000-000000000001','member-subject-billing',now(),
    100,50,50,'priced',1,0.25,true
) on conflict(key_hash,request_id) where request_id<>'' do nothing;
select public.compliance_submit_subject_request(
    'e1000000-0000-4000-8000-000000000001','ed000000-0000-4000-8000-000000000001',
    'delete','member','e0000000-0000-4000-8000-000000000002',
    'system:test','evidence:member:delete'
);
select public.compliance_approve_data_request(
    'e1000000-0000-4000-8000-000000000001',
    'ed000000-0000-4000-8000-000000000001','system:approver'
);
do $$
declare before_count bigint; after_count bigint;
begin
    select count(*) into before_count from public.billing_ledger ledger
      join public.usage_log usage on usage.id=ledger.usage_log_id
     where usage.organization_id='e1000000-0000-4000-8000-000000000001';
    perform public.compliance_delete_subject(
        'e1000000-0000-4000-8000-000000000001',
        'ed000000-0000-4000-8000-000000000001','system:test'
    );
    select count(*) into after_count from public.billing_ledger ledger
      join public.usage_log usage on usage.id=ledger.usage_log_id
     where usage.organization_id='e1000000-0000-4000-8000-000000000001';
    if before_count=0 or before_count<>after_count
       or exists (select 1 from public.organization_members
                   where organization_id='e1000000-0000-4000-8000-000000000001'
                     and user_id='e0000000-0000-4000-8000-000000000002')
       or not exists (select 1 from public.organization_members
                       where organization_id='e2000000-0000-4000-8000-000000000002'
                         and user_id='e0000000-0000-4000-8000-000000000002')
       or not exists (select 1 from auth.users
                       where id='e0000000-0000-4000-8000-000000000002'
                         and email='member-subject@example.invalid')
       or not exists (select 1 from public.organization_invitations
                       where id='e4000000-0000-4000-8000-000000000004'
                         and accepted_by is null) then
        raise exception 'member deletion crossed tenant scope or lost financial evidence';
    end if;
end;
$$;

insert into public.usage_log(
    key_hash,owner_id,organization_id,customer_id,request_id,ts,baseline_tokens,
    optimized_tokens,tokens_saved,pricing_status,verified_savings_usd,
    brevitas_fee_usd,authoritative
) values (
    'customer-subject-usage','e0000000-0000-4000-8000-000000000001',
    'e1000000-0000-4000-8000-000000000001','ea000000-0000-4000-8000-000000000001',
    'customer-subject-billing',now(),100,50,50,'priced',1,0.25,true
) on conflict(key_hash,request_id) where request_id<>'' do nothing;
insert into public.semantic_cache(
    exact_hash,context_hash,model_id,response_ciphertext,tenant_namespace,created_at,expires_at
) values (
    repeat('f',64),repeat('a',64),'test','customer-ciphertext',
    encode(digest('e1000000-0000-4000-8000-000000000001:ea000000-0000-4000-8000-000000000001','sha256'),'hex'),
    now(),now()+interval '1 hour'
) on conflict(exact_hash) do nothing;
select public.compliance_submit_subject_request(
    'e1000000-0000-4000-8000-000000000001','ed000000-0000-4000-8000-000000000002',
    'delete','customer','ea000000-0000-4000-8000-000000000001',
    'system:test','evidence:customer:delete'
);
select public.compliance_approve_data_request(
    'e1000000-0000-4000-8000-000000000001',
    'ed000000-0000-4000-8000-000000000002','system:approver'
);
select public.compliance_request_legal_hold_action(
    'e1000000-0000-4000-8000-000000000001',
    'e3100000-0000-4000-8000-000000000003','create',
    'e3000000-0000-4000-8000-000000000003','delete','subject_test',
    'brevitas_admin:hold-requester','audit:subject:hold:create:request',null
);
-- Exact requester replay is a read of the same immutable action, not a second
-- audit or a direct hold mutation.
select public.compliance_request_legal_hold_action(
    'e1000000-0000-4000-8000-000000000001',
    'e3100000-0000-4000-8000-000000000003','create',
    'e3000000-0000-4000-8000-000000000003','delete','subject_test',
    'brevitas_admin:hold-requester','audit:subject:hold:create:replay',null
);
do $$
begin
    if exists (select 1 from public.legal_holds
                where id='e3000000-0000-4000-8000-000000000003') then
        raise exception 'pending create committed a legal hold without approval';
    end if;
    begin
        perform public.compliance_approve_legal_hold_action(
            'e1000000-0000-4000-8000-000000000001',
            'e3100000-0000-4000-8000-000000000003',
            'brevitas_admin:hold-requester','audit:subject:hold:create:self');
        raise exception 'legal hold requester approved their own create action';
    exception when insufficient_privilege then null;
    end;
    begin
        perform public.compliance_request_legal_hold_action(
            'e2000000-0000-4000-8000-000000000002',
            'e3100000-0000-4000-8000-000000000003','create',
            'e3000000-0000-4000-8000-000000000003','delete','subject_test',
            'brevitas_admin:hold-requester','audit:subject:hold:create:cross',null);
        raise exception 'cross-tenant legal hold action replay succeeded';
    exception when unique_violation then null;
    end;
    begin
        perform public.compliance_delete_subject(
            'e1000000-0000-4000-8000-000000000001',
            'ed000000-0000-4000-8000-000000000002','system:test'
        );
        raise exception 'pending legal hold create did not block deletion';
    exception when object_not_in_prerequisite_state then null;
    end;
end;
$$;
select public.compliance_approve_legal_hold_action(
    'e1000000-0000-4000-8000-000000000001',
    'e3100000-0000-4000-8000-000000000003',
    'brevitas_admin:hold-approver','audit:subject:hold:create:approve'
);
select public.compliance_approve_legal_hold_action(
    'e1000000-0000-4000-8000-000000000001',
    'e3100000-0000-4000-8000-000000000003',
    'brevitas_admin:hold-approver','audit:subject:hold:create:approve-replay'
);
do $$
begin
    begin
        update public.legal_hold_actions
           set requested_by='brevitas_admin:hold-competitor'
         where id='e3100000-0000-4000-8000-000000000003';
        raise exception 'legal hold action requester mutation succeeded';
    exception when object_not_in_prerequisite_state then null;
    end;
    begin
        delete from public.legal_hold_actions
         where id='e3100000-0000-4000-8000-000000000003';
        raise exception 'legal hold action evidence deletion succeeded';
    exception when object_not_in_prerequisite_state then null;
    end;
    begin
        perform public.compliance_approve_legal_hold_action(
            'e1000000-0000-4000-8000-000000000001',
            'e3100000-0000-4000-8000-000000000003',
            'brevitas_admin:hold-competitor','audit:subject:hold:create:compete');
        raise exception 'competing legal hold create approval succeeded';
    exception when insufficient_privilege then null;
    end;
    if not exists (select 1 from public.legal_holds
                    where id='e3000000-0000-4000-8000-000000000003'
                      and organization_id='e1000000-0000-4000-8000-000000000001'
                      and active and created_by='brevitas_admin:hold-requester')
       or not exists (select 1 from public.legal_hold_actions
                       where id='e3100000-0000-4000-8000-000000000003'
                         and status='approved'
                         and requested_by='brevitas_admin:hold-requester'
                         and approved_by='brevitas_admin:hold-approver')
       or (select count(*) from public.audit_events
            where action='compliance.legal_hold.create_requested'
              and target_id='e3100000-0000-4000-8000-000000000003')<>1
       or (select count(*) from public.audit_events
            where action='compliance.legal_hold.created'
              and target_id='e3000000-0000-4000-8000-000000000003')<>1 then
        raise exception 'legal hold create approval/replay/audit contract failed';
    end if;
end;
$$;
select public.compliance_request_legal_hold_action(
    'e1000000-0000-4000-8000-000000000001',
    'e3200000-0000-4000-8000-000000000003','release',
    'e3000000-0000-4000-8000-000000000003',null,null,
    'brevitas_admin:hold-approver','audit:subject:hold:release:request',null
);
select public.compliance_request_legal_hold_action(
    'e1000000-0000-4000-8000-000000000001',
    'e3200000-0000-4000-8000-000000000003','release',
    'e3000000-0000-4000-8000-000000000003',null,null,
    'brevitas_admin:hold-approver','audit:subject:hold:release:replay',null
);
do $$
begin
    begin
        perform public.compliance_approve_legal_hold_action(
            'e1000000-0000-4000-8000-000000000001',
            'e3200000-0000-4000-8000-000000000003',
            'brevitas_admin:hold-approver','audit:subject:hold:release:self');
        raise exception 'legal hold requester approved their own release action';
    exception when insufficient_privilege then null;
    end;
    begin
        perform public.compliance_request_legal_hold_action(
            'e2000000-0000-4000-8000-000000000002',
            'e3300000-0000-4000-8000-000000000003','release',
            'e3000000-0000-4000-8000-000000000003',null,null,
            'brevitas_admin:hold-requester','audit:subject:hold:release:cross',null);
        raise exception 'cross-tenant legal hold release request succeeded';
    exception when no_data_found then null;
    end;
    begin
        perform public.compliance_delete_subject(
            'e1000000-0000-4000-8000-000000000001',
            'ed000000-0000-4000-8000-000000000002','system:test');
        raise exception 'pending legal hold release weakened active hold';
    exception when object_not_in_prerequisite_state then null;
    end;
end;
$$;
select public.compliance_approve_legal_hold_action(
    'e1000000-0000-4000-8000-000000000001',
    'e3200000-0000-4000-8000-000000000003',
    'brevitas_admin:hold-requester','audit:subject:hold:release:approve'
);
select public.compliance_approve_legal_hold_action(
    'e1000000-0000-4000-8000-000000000001',
    'e3200000-0000-4000-8000-000000000003',
    'brevitas_admin:hold-requester','audit:subject:hold:release:approve-replay'
);
do $$
begin
    begin
        perform public.compliance_approve_legal_hold_action(
            'e1000000-0000-4000-8000-000000000001',
            'e3200000-0000-4000-8000-000000000003',
            'brevitas_admin:hold-competitor','audit:subject:hold:release:compete');
        raise exception 'competing legal hold release approval succeeded';
    exception when insufficient_privilege then null;
    end;
    if not exists (select 1 from public.legal_holds
                    where id='e3000000-0000-4000-8000-000000000003'
                      and not active
                      and released_by='brevitas_admin:hold-requester')
       or not exists (select 1 from public.legal_hold_actions
                       where id='e3200000-0000-4000-8000-000000000003'
                         and status='approved'
                         and requested_by='brevitas_admin:hold-approver'
                         and approved_by='brevitas_admin:hold-requester')
       or (select count(*) from public.audit_events
            where action='compliance.legal_hold.release_requested'
              and target_id='e3200000-0000-4000-8000-000000000003')<>1
       or (select count(*) from public.audit_events
            where action='compliance.legal_hold.released'
              and target_id='e3000000-0000-4000-8000-000000000003')<>1 then
        raise exception 'legal hold release approval/replay/audit contract failed';
    end if;
end;
$$;
do $$
declare before_count bigint; after_count bigint;
begin
    select count(*) into before_count from public.billing_ledger ledger
      join public.usage_log usage on usage.id=ledger.usage_log_id
     where usage.organization_id='e1000000-0000-4000-8000-000000000001';
    perform public.compliance_delete_subject(
        'e1000000-0000-4000-8000-000000000001',
        'ed000000-0000-4000-8000-000000000002','system:test'
    );
    select count(*) into after_count from public.billing_ledger ledger
      join public.usage_log usage on usage.id=ledger.usage_log_id
     where usage.organization_id='e1000000-0000-4000-8000-000000000001';
    if before_count=0 or before_count<>after_count
       or exists (select 1 from public.customers
                   where organization_id='e1000000-0000-4000-8000-000000000001'
                     and id='ea000000-0000-4000-8000-000000000001')
       or exists (select 1 from public.semantic_cache
                   where tenant_namespace=encode(digest('e1000000-0000-4000-8000-000000000001:ea000000-0000-4000-8000-000000000001','sha256'),'hex'))
       or not exists (select 1 from public.audit_events
                       where request_id='ed000000-0000-4000-8000-000000000002'
                         and action='compliance.subject_delete.completed') then
        raise exception 'customer deletion failed scope, cache, billing, or audit preservation';
    end if;
end;
$$;

-- Production/ordinary databases do not contain the restore control schema;
-- direct replay must fail closed before it can mutate authoritative data.
do $$
begin
    begin
        perform public.compliance_replay_deletion_tombstone(
            'production-us','e1000000-0000-4000-8000-000000000001',
            'ef000000-0000-4000-8000-000000000001',now()-interval '36 days',
            now()-interval '1 day','tenant',null,'system:restore:test',
            'evidence:replay:closed',repeat('f',64)
        );
        raise exception 'production restore replay unexpectedly succeeded without control schema';
    exception when object_not_in_prerequisite_state then null;
    end;
end;
$$;

insert into auth.users(id,email) values
    ('c0000000-0000-4000-8000-000000000001','compliance-a@example.invalid'),
    ('c0000000-0000-4000-8000-000000000002','compliance-b@example.invalid')
on conflict (id) do nothing;
insert into public.organizations(id,name,billing_owner_id) values
    ('c1000000-0000-4000-8000-000000000001','Compliance tenant A','c0000000-0000-4000-8000-000000000001'),
    ('c2000000-0000-4000-8000-000000000002','Compliance tenant B','c0000000-0000-4000-8000-000000000002')
on conflict (id) do nothing;
insert into public.organization_members(organization_id,user_id,role,status) values
    ('c1000000-0000-4000-8000-000000000001','c0000000-0000-4000-8000-000000000001','company_owner','active'),
    ('c2000000-0000-4000-8000-000000000002','c0000000-0000-4000-8000-000000000002','company_owner','active')
on conflict (organization_id,user_id) do nothing;
insert into public.api_keys(key_hash,name,owner_id,organization_id,key_type) values
    ('compliance-key-a','Key A','c0000000-0000-4000-8000-000000000001','c1000000-0000-4000-8000-000000000001','organization_service'),
    ('compliance-key-b','Key B','c0000000-0000-4000-8000-000000000002','c2000000-0000-4000-8000-000000000002','organization_service')
on conflict (key_hash) do nothing;
insert into public.provider_config(key_hash,provider,provider_api_key,model) values
    ('compliance-key-a','openai','provider-private-must-not-export','test-model')
on conflict (key_hash) do update set provider_api_key=excluded.provider_api_key;
insert into public.customers(id,organization_id,external_id,display_name) values
    ('ca000000-0000-4000-8000-000000000001','c1000000-0000-4000-8000-000000000001','customer-a','Customer A')
on conflict (id) do nothing;
insert into public.billing_accounts(
    user_id,stripe_customer_id,subscription_status,billing_started_at,
    current_period_start,current_period_end
) values (
    'c0000000-0000-4000-8000-000000000001','cus_compliance_a','active',
    now()-interval '1 year',now()-interval '1 day',now()+interval '29 days'
) on conflict (user_id) do update set subscription_status='active';
insert into public.usage_log(
    key_hash,owner_id,organization_id,customer_id,request_id,ts,
    baseline_tokens,optimized_tokens,tokens_saved,pricing_status,
    verified_savings_usd,brevitas_fee_usd,authoritative
) values (
    'compliance-key-a','c0000000-0000-4000-8000-000000000001',
    'c1000000-0000-4000-8000-000000000001','ca000000-0000-4000-8000-000000000001',
    'compliance-billing-usage',now(),100,50,50,'priced',1,0.25,true
) on conflict (key_hash,request_id) where request_id<>'' do nothing;

-- Valid export is tenant scoped, excludes security authenticators, emits an
-- encrypted provider-credential envelope for managed decryption, and finalizes
-- only against the encrypted artifact digest.
insert into public.data_subject_requests(
    id,organization_id,request_type,status,evidence_reference,requested_at,due_at,
    approved_at,approved_by,created_by
) values (
    'ce000000-0000-4000-8000-000000000001','c1000000-0000-4000-8000-000000000001',
    'export','approved','evidence:export:001',now()-interval '1 day',now()+interval '29 days',
    now(),'system:test','system:test'
);
do $$
declare exported text;
begin
    select string_agg(value::text,'') into exported
      from public.compliance_export_tenant(
        'c1000000-0000-4000-8000-000000000001',
        'ce000000-0000-4000-8000-000000000001','system:test'
      ) value;
    if exported is null or exported not like '%encrypted_content%'
       or exported not like '%provider_configuration%'
       or exported not like '%api_key_metadata%' then
        raise exception 'tenant export omitted encrypted/persisted configuration records';
    end if;
    if public.compliance_complete_export(
        'c1000000-0000-4000-8000-000000000001',
        'ce000000-0000-4000-8000-000000000001','system:test',repeat('a',64),
        repeat('b',64),3,repeat('c',64)
       ) <> 'completed'
       or public.compliance_complete_export(
        'c1000000-0000-4000-8000-000000000001',
        'ce000000-0000-4000-8000-000000000001','system:test',repeat('a',64),
        repeat('b',64),3,repeat('c',64)
       ) <> 'completed' then
        raise exception 'export finalize is not idempotent';
    end if;
    begin
        perform * from public.compliance_export_tenant(
            'c2000000-0000-4000-8000-000000000002',
            'ce000000-0000-4000-8000-000000000001','system:test');
        raise exception 'cross-tenant export unexpectedly succeeded';
    exception when no_data_found then null;
    end;
end;
$$;

-- Holds block approved work without mutating its state.
insert into public.legal_holds(
    id,organization_id,scope,reason_code,created_by
) values (
    'c3000000-0000-4000-8000-000000000003','c2000000-0000-4000-8000-000000000002',
    'delete','litigation','system:test'
);
insert into public.data_subject_requests(
    id,organization_id,request_type,status,evidence_reference,requested_at,due_at,
    approved_at,approved_by,created_by
) values (
    'cd000000-0000-4000-8000-000000000002','c2000000-0000-4000-8000-000000000002',
    'delete','approved','evidence:delete:held',now()-interval '1 day',now()+interval '29 days',
    now(),'system:test','system:test'
);
do $$
begin
    begin
        perform public.compliance_delete_tenant(
            'c2000000-0000-4000-8000-000000000002',
            'cd000000-0000-4000-8000-000000000002','system:test');
        raise exception 'held deletion unexpectedly succeeded';
    exception when object_not_in_prerequisite_state then null;
    end;
    if (select status from public.data_subject_requests
         where id='cd000000-0000-4000-8000-000000000002') <> 'approved' then
        raise exception 'held request changed state';
    end if;
end;
$$;

-- Timing constraints reject requests beyond 30 days, but an already-approved
-- overdue request is processed urgently and records immutable breach evidence.
do $$
begin
    begin
        insert into public.data_subject_requests(
            id,organization_id,request_type,status,evidence_reference,
            requested_at,due_at,created_by
        ) values (
            'cf000000-0000-4000-8000-000000000003','c1000000-0000-4000-8000-000000000001',
            'export','pending','evidence:invalid:due',now(),now()+interval '31 days','system:test'
        );
        raise exception 'invalid due date unexpectedly succeeded';
    exception when check_violation then null;
    end;
end;
$$;
insert into public.data_subject_requests(
    id,organization_id,request_type,status,evidence_reference,requested_at,due_at,
    approved_at,approved_by,created_by
) values (
    'cd000000-0000-4000-8000-000000000001','c1000000-0000-4000-8000-000000000001',
    'delete','approved','evidence:delete:overdue',now()-interval '31 days',now()-interval '1 day',
    now(),'system:test','system:test'
);
select public.append_company_audit(
    'c1000000-0000-4000-8000-000000000001','system:test','system','audit:preserve:001',
    'compliance.fixture','company','c1000000-0000-4000-8000-000000000001','committed'
);
do $$
declare billing_before bigint; billing_after bigint; result text;
begin
    select count(*) into billing_before from public.billing_ledger ledger
      join public.usage_log usage on usage.id=ledger.usage_log_id
     where usage.organization_id='c1000000-0000-4000-8000-000000000001';
    result := public.compliance_delete_tenant(
        'c1000000-0000-4000-8000-000000000001',
        'cd000000-0000-4000-8000-000000000001','system:test');
    select count(*) into billing_after from public.billing_ledger ledger
      join public.usage_log usage on usage.id=ledger.usage_log_id
     where usage.organization_id='c1000000-0000-4000-8000-000000000001';
    if result <> 'completed' or billing_before = 0 or billing_after <> billing_before then
        raise exception 'delete did not preserve billing evidence';
    end if;
    if public.compliance_delete_tenant(
        'c1000000-0000-4000-8000-000000000001',
        'cd000000-0000-4000-8000-000000000001','system:test') <> 'completed'
       or (select count(*) from public.backup_deletion_tombstones
            where request_id='cd000000-0000-4000-8000-000000000001') <> 1 then
        raise exception 'delete/tombstone is not idempotent';
    end if;
    if not (select deadline_breached from public.data_subject_requests
             where id='cd000000-0000-4000-8000-000000000001')
       or not exists (select 1 from public.audit_events
                       where request_id='cd000000-0000-4000-8000-000000000001'
                         and action='compliance.deadline_breached')
       or not exists (select 1 from public.audit_events
                       where request_id='audit:preserve:001') then
        raise exception 'deadline or immutable audit evidence was not preserved';
    end if;
    if exists (select 1 from public.api_keys where organization_id='c1000000-0000-4000-8000-000000000001')
       or exists (select 1 from public.provider_config where provider_api_key='provider-private-must-not-export')
       or not exists (select 1 from public.api_keys where key_hash='compliance-key-b') then
        raise exception 'tenant erasure crossed scope or retained credentials';
    end if;
    if not exists (
        select 1 from public.backup_deletion_tombstones tombstone
        join public.data_subject_requests request on request.id=tombstone.request_id
        where tombstone.request_id='cd000000-0000-4000-8000-000000000001'
          and tombstone.expires_at=request.requested_at+interval '35 days'
    ) then raise exception 'backup tombstone deadline is invalid'; end if;
end;
$$;

do $$
begin
    begin
        delete from public.backup_deletion_tombstones
         where request_id='cd000000-0000-4000-8000-000000000001';
        raise exception 'immutable tombstone deletion unexpectedly succeeded';
    exception when object_not_in_prerequisite_state then null;
    end;
end;
$$;
