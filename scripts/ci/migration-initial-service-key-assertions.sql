\set ON_ERROR_STOP on

-- Migration 005 must create the service account, its first usable key, and
-- immutable audit evidence in one transaction. Billing attribution belongs to
-- the company owner while created_by preserves the human administrator.

insert into auth.users(id,email) values
    ('51000000-0000-4000-8000-000000000001','initial-key-owner@example.invalid'),
    ('51000000-0000-4000-8000-000000000002','initial-key-admin@example.invalid')
on conflict (id) do nothing;

insert into public.organizations(id,name,legacy_owner_id,billing_owner_id) values (
    '51100000-0000-4000-8000-000000000001','Initial service key fixture',
    'initial-service-key-fixture','51000000-0000-4000-8000-000000000001'
) on conflict (id) do nothing;

insert into public.organization_members(
    organization_id,user_id,role,status
) values
    ('51100000-0000-4000-8000-000000000001','51000000-0000-4000-8000-000000000001','company_owner','active'),
    ('51100000-0000-4000-8000-000000000001','51000000-0000-4000-8000-000000000002','company_admin','active')
on conflict (organization_id,user_id) do update set
    role=excluded.role,status=excluded.status;

do $$
declare
    v_created jsonb;
    v_duplicate jsonb;
    v_authorization record;
    v_key_id uuid;
begin
    v_created:=public.company_admin_create_service_account(
        '51100000-0000-4000-8000-000000000001',
        '51000000-0000-4000-8000-000000000002',
        '51200000-0000-4000-8000-000000000001',
        'initial-key-fixture','staging',array['proxy:invoke']::text[],
        repeat('5',64),'bvt_initial1',now()+interval '1 day',
        'initial-service-key-create'
    );
    v_key_id:=(v_created->>'key_id')::uuid;
    if coalesce((v_created->>'ok')::boolean,false) is not true
       or v_key_id is null
       or not exists (
            select 1 from public.api_keys credential
             where credential.id=v_key_id
               and credential.key_hash=repeat('5',64)
               and credential.organization_id='51100000-0000-4000-8000-000000000001'
               and credential.service_account_id='51200000-0000-4000-8000-000000000001'
               and credential.key_type='organization_service'
               and credential.owner_id='51000000-0000-4000-8000-000000000001'
               and credential.created_by='51000000-0000-4000-8000-000000000002'
               and credential.key_prefix='bvt_initial1'
               and credential.revoked_at is null
       ) then
        raise exception 'initial service credential attribution is incorrect';
    end if;

    select * into v_authorization
      from public.service_key_authorization(repeat('5',64));
    if v_authorization.key_hash is null
       or v_authorization.owner_id<>'51000000-0000-4000-8000-000000000001'
       or v_authorization.service_account_id<>'51200000-0000-4000-8000-000000000001' then
        raise exception 'initial service credential is not immediately usable';
    end if;

    v_duplicate:=public.company_admin_create_service_account(
        '51100000-0000-4000-8000-000000000001',
        '51000000-0000-4000-8000-000000000002',
        '51200000-0000-4000-8000-000000000002',
        'duplicate-key-fixture','staging',array['proxy:invoke']::text[],
        repeat('5',64),'bvt_initial2',now()+interval '1 day',
        'initial-service-key-duplicate'
    );
    if v_duplicate->>'code'<>'duplicate'
       or exists (
            select 1 from public.service_accounts
             where id='51200000-0000-4000-8000-000000000002'
       ) then
        raise exception 'duplicate initial key left a keyless service account';
    end if;
end;
$$;

do $$
declare
    v_legacy jsonb;
begin
    v_legacy:=public.company_admin_create_service_account(
        '51100000-0000-4000-8000-000000000001',
        '51000000-0000-4000-8000-000000000002',
        '51200000-0000-4000-8000-000000000003',
        'legacy-client-fixture','staging',array['proxy:invoke']::text[],
        now()+interval '1 day','initial-service-key-legacy'
    );
    if coalesce((v_legacy->>'ok')::boolean,true) is not false
       or v_legacy->>'code'<>'client_upgrade_required'
       or exists (
            select 1 from public.service_accounts
             where id='51200000-0000-4000-8000-000000000003'
       ) then
        raise exception 'legacy service-account RPC did not fail closed';
    end if;

    if to_regprocedure(
        'public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text)'
    ) is null
       or to_regprocedure(
            'public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],timestamptz,text)'
       ) is null
       or has_function_privilege(
            'authenticated',
            'public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text)',
            'execute'
       )
       or not has_function_privilege(
            'service_role',
            'public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text)',
            'execute'
       )
       or has_function_privilege(
            'authenticated',
            'public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],timestamptz,text)',
            'execute'
       ) then
        raise exception 'initial service key RPC grants are unsafe';
    end if;
end;
$$;
