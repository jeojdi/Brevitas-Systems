\set ON_ERROR_STOP on

-- Migration 010 tables are RLS-protected with no direct application-role ACL.
do $$
declare
    checked_role text;
    checked_privilege text;
    function_signature text;
    function_oid oid;
begin
    if not (select relation.relrowsecurity from pg_class relation
             where relation.oid='public.bvx_device_consumption_receipts'::regclass) then
        raise exception 'device receipts do not have RLS enabled';
    end if;
    foreach checked_role in array array['anon','authenticated','service_role'] loop
        foreach checked_privilege in array array[
            'SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'
        ] loop
            if has_table_privilege(
                checked_role,'public.bvx_device_consumption_receipts',checked_privilege
            ) then
                raise exception 'unsafe device receipt privilege: % %',
                    checked_role,checked_privilege;
            end if;
        end loop;
    end loop;
    if exists (
        select 1 from aclexplode(coalesce(
            (select relation.relacl from pg_class relation
              where relation.oid='public.bvx_device_consumption_receipts'::regclass),
            acldefault('r',(select relation.relowner from pg_class relation
                            where relation.oid='public.bvx_device_consumption_receipts'::regclass))
        )) privilege
         where privilege.grantee=0
    ) then raise exception 'PUBLIC has a direct device receipt privilege'; end if;
    foreach function_signature in array array[
        'public.resolve_bvx_device_approval_organization(text,uuid)',
        'public.approve_bvx_device(text,text,text,text,uuid)',
        'public.get_bvx_device_exchange(text)',
        'public.consume_bvx_device_idempotent(text,text,text)'
    ] loop
        function_oid := to_regprocedure(function_signature);
        if function_oid is null
           or not has_function_privilege('service_role',function_oid,'EXECUTE')
           or has_function_privilege('anon',function_oid,'EXECUTE')
           or has_function_privilege('authenticated',function_oid,'EXECUTE')
           or exists (
                select 1 from aclexplode(coalesce(
                    (select procedure.proacl from pg_proc procedure
                      where procedure.oid=function_oid),
                    acldefault('f',(select procedure.proowner from pg_proc procedure
                                     where procedure.oid=function_oid))
                )) privilege
                 where privilege.grantee=0 and privilege.privilege_type='EXECUTE'
           ) then
            raise exception 'unsafe migration 010 RPC privilege: %',function_signature;
        end if;
    end loop;
    if has_function_privilege(
        'service_role','public.consume_bvx_device(text)'::regprocedure,'EXECUTE'
    ) or has_function_privilege(
        'service_role','public.approve_bvx_device(text,text,text,text)'::regprocedure,'EXECUTE'
    ) then
        raise exception 'legacy device RPC remains callable by service_role';
    end if;
end;
$$;

insert into auth.users(id,email) values (
    'cccccccc-cccc-4ccc-8ccc-cccccccccccc','device-approver@example.invalid'
) on conflict(id) do nothing;
insert into public.organization_members(organization_id,user_id,role,status) values (
    '10000000-0000-4000-8000-000000000001',
    'cccccccc-cccc-4ccc-8ccc-cccccccccccc','member','active'
) on conflict(organization_id,user_id) do update set role='member',status='active';

-- Cross-tenant approval cannot bind an exchange.
insert into public.bvx_device_auth(
    device_hash,expires_at,owner_id,key_hash,encrypted_key,approved_at,
    organization_id,quarantined_at
) values (repeat('3',64),now()+interval '10 minutes','','','',null,null,null)
on conflict(device_hash) do update set
    expires_at=excluded.expires_at,owner_id='',key_hash='',encrypted_key='',
    approved_at=null,organization_id=null,quarantined_at=null;
do $$
begin
    if public.approve_bvx_device(
        repeat('3',64),'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        repeat('4',64),'cross-tenant-ciphertext',
        '10000000-0000-4000-8000-000000000001'
    ) or exists (
        select 1 from public.bvx_device_auth exchange
         where exchange.device_hash=repeat('3',64) and exchange.approved_at is not null
    ) then
        raise exception 'cross-tenant device approval was accepted';
    end if;
end;
$$;

-- First consumption and a retry under a different request ID return the same
-- bounded receipt and create only one key/audit activation.
insert into public.bvx_device_auth(
    device_hash,expires_at,owner_id,key_hash,encrypted_key,approved_at,
    organization_id,quarantined_at
) values (repeat('5',64),now()+interval '10 minutes','','','',null,null,null)
on conflict(device_hash) do update set
    expires_at=excluded.expires_at,owner_id='',key_hash='',encrypted_key='',
    approved_at=null,organization_id=null,quarantined_at=null;
do $$
declare
    first_result jsonb;
    retry_result jsonb;
    activated_key_id uuid;
    returned_keys text[];
begin
    if not public.approve_bvx_device(
        repeat('5',64),'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        repeat('6',64),'main-kms-ciphertext',
        '10000000-0000-4000-8000-000000000001'
    ) then raise exception 'valid device approval was denied'; end if;
    first_result := public.consume_bvx_device_idempotent(
        repeat('5',64),repeat('6',64),'release-device-first');
    retry_result := public.consume_bvx_device_idempotent(
        repeat('5',64),repeat('6',64),'release-device-retry');
    select array_agg(key order by key) into returned_keys
      from jsonb_object_keys(first_result) key;
    if not coalesce((first_result->>'ok')::boolean,false)
       or coalesce((first_result->>'already_consumed')::boolean,true)
       or not coalesce((retry_result->>'already_consumed')::boolean,false)
       or first_result->>'encrypted_key'<>'main-kms-ciphertext'
       or retry_result->>'encrypted_key'<>first_result->>'encrypted_key'
       or retry_result->>'consumed_at'<>first_result->>'consumed_at'
       or returned_keys<>array[
            'already_consumed','consumed_at','device_hash','encrypted_key',
            'key_hash','ok','organization_id','owner_id','status'
       ]::text[] then
        raise exception 'device consume/retry result is not exact or idempotent';
    end if;
    select credential.id into strict activated_key_id
      from public.api_keys credential
     where credential.key_hash=repeat('6',64)
       and credential.organization_id='10000000-0000-4000-8000-000000000001'
       and credential.key_type='device';
    if (select count(*) from public.api_keys where key_hash=repeat('6',64))<>1
       or (select count(*) from public.bvx_device_consumption_receipts
            where device_hash=repeat('5',64))<>1
       or (select count(*) from public.audit_events event
            where event.action='device_key.activated'
              and event.target_id=activated_key_id::text
              and event.request_id='release-device-first'
              and event.details='{}'::jsonb)<>1
       or exists (
            select 1 from public.audit_events event
             where event.request_id in ('release-device-first','release-device-retry')
               and event::text like '%'||repeat('5',64)||'%'
       ) then
        raise exception 'device activation was duplicated or leaked a digest into audit';
    end if;

    update public.api_keys set revoked_at=now() where id=activated_key_id;
    retry_result := public.consume_bvx_device_idempotent(
        repeat('5',64),repeat('6',64),'release-device-key-drift');
    if retry_result->>'code'<>'receipt_invalid' or not exists (
        select 1 from public.bvx_device_consumption_receipts receipt
         where receipt.device_hash=repeat('5',64)
           and receipt.encrypted_key='' and receipt.quarantined_at is not null
    ) then
        raise exception 'revoked-key replay did not quarantine its receipt';
    end if;
end;
$$;

-- The approving human is independent of the activated billing owner. Removing
-- only that approver must still fail closed on replay.
insert into public.bvx_device_auth(
    device_hash,expires_at,owner_id,key_hash,encrypted_key,approved_at,
    organization_id,quarantined_at
) values (repeat('7',64),now()+interval '10 minutes','','','',null,null,null)
on conflict(device_hash) do update set
    expires_at=excluded.expires_at,owner_id='',key_hash='',encrypted_key='',
    approved_at=null,organization_id=null,quarantined_at=null;
do $$
declare result jsonb;
begin
    if not public.approve_bvx_device(
        repeat('7',64),'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
        repeat('8',64),'approver-kms-ciphertext',
        '10000000-0000-4000-8000-000000000001'
    ) then raise exception 'member device approval was denied'; end if;
    result := public.consume_bvx_device_idempotent(
        repeat('7',64),repeat('8',64),'release-device-approver');
    if not coalesce((result->>'ok')::boolean,false) or not exists (
        select 1 from public.bvx_device_consumption_receipts receipt
         where receipt.device_hash=repeat('7',64)
           and receipt.owner_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
           and receipt.approver_id='cccccccc-cccc-4ccc-8ccc-cccccccccccc'
    ) then raise exception 'approver and billing owner were not preserved separately'; end if;
    update public.organization_members set status='disabled',disabled_at=now()
     where organization_id='10000000-0000-4000-8000-000000000001'
       and user_id='cccccccc-cccc-4ccc-8ccc-cccccccccccc';
    result := public.consume_bvx_device_idempotent(
        repeat('7',64),repeat('8',64),'release-device-approver-drift');
    if result->>'code'<>'receipt_invalid' or not exists (
        select 1 from public.bvx_device_consumption_receipts receipt
         where receipt.device_hash=repeat('7',64)
           and receipt.encrypted_key='' and receipt.quarantined_at is not null
    ) then raise exception 'removed approver replay did not quarantine its receipt'; end if;
end;
$$;

-- A digest mismatch destroys exchange recovery material and never mints.
insert into public.bvx_device_auth(
    device_hash,expires_at,owner_id,key_hash,encrypted_key,approved_at,
    organization_id,quarantined_at
) values (repeat('9',64),now()+interval '10 minutes','','','',null,null,null)
on conflict(device_hash) do update set
    expires_at=excluded.expires_at,owner_id='',key_hash='',encrypted_key='',
    approved_at=null,organization_id=null,quarantined_at=null;
do $$
declare result jsonb;
begin
    perform public.approve_bvx_device(
        repeat('9',64),'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        repeat('e',64),'mismatch-kms-ciphertext',
        '10000000-0000-4000-8000-000000000001');
    result := public.consume_bvx_device_idempotent(
        repeat('9',64),repeat('f',64),'release-device-mismatch');
    if result->>'code'<>'digest_mismatch'
       or exists(select 1 from public.api_keys where key_hash=repeat('e',64))
       or not exists (
            select 1 from public.bvx_device_auth exchange
             where exchange.device_hash=repeat('9',64)
               and exchange.encrypted_key='' and exchange.quarantined_at is not null
       ) then raise exception 'digest mismatch did not quarantine without minting'; end if;
end;
$$;

-- An elapsed receipt is purged before any recovery result is returned.
insert into public.bvx_device_consumption_receipts(
    device_hash,key_hash,encrypted_key,owner_id,approver_id,organization_id,
    consumed_at,expires_at,request_id
) values (
    repeat('c',64),repeat('d',64),'expired-kms-ciphertext',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    '10000000-0000-4000-8000-000000000001',
    now()-interval '11 minutes',now()-interval '1 minute','release-device-expired'
) on conflict(device_hash) do nothing;
do $$
declare result jsonb;
begin
    result := public.consume_bvx_device_idempotent(
        repeat('c',64),repeat('d',64),'release-device-expired-retry');
    if result->>'code'<>'expired_or_missing' or exists (
        select 1 from public.bvx_device_consumption_receipts
         where device_hash=repeat('c',64)
    ) then raise exception 'expired device receipt was not purged'; end if;
end;
$$;

-- Migration 011 is service-only, actor-bound, active-role filtered and capped.
do $$
declare
    function_oid oid := to_regprocedure(
        'public.company_admin_active_memberships(uuid,uuid)');
begin
    if function_oid is null
       or not has_function_privilege('service_role',function_oid,'EXECUTE')
       or has_function_privilege('anon',function_oid,'EXECUTE')
       or has_function_privilege('authenticated',function_oid,'EXECUTE')
       or exists (
            select 1 from aclexplode(coalesce(
                (select procedure.proacl from pg_proc procedure
                  where procedure.oid=function_oid),
                acldefault('f',(select procedure.proowner from pg_proc procedure
                                 where procedure.oid=function_oid))
            )) privilege
             where privilege.grantee=0 and privilege.privilege_type='EXECUTE'
       ) then raise exception 'unsafe active-membership RPC privilege'; end if;
    if not exists (
        select 1 from pg_class relation join pg_index state
          on state.indexrelid=relation.oid
         where relation.relname='organization_members_actor_active_idx'
           and state.indisvalid and state.indisready
    ) then raise exception 'active-membership index is not ready'; end if;
end;
$$;

do $$
declare
    item_index integer;
    company_id uuid;
    result jsonb;
    denied jsonb;
begin
    for item_index in 1..105 loop
        insert into public.organizations(name,legacy_owner_id,billing_owner_id)
        values (
            'Release bounded company '||lpad(item_index::text,3,'0'),
            'release-cap-owner-'||item_index,
            'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        ) on conflict(legacy_owner_id) do update set
            name=excluded.name
        returning id into company_id;
        insert into public.organization_members(
            organization_id,user_id,role,status
        ) values (
            company_id,'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            case item_index%4 when 0 then 'company_owner'
                 when 1 then 'company_admin' when 2 then 'member'
                 else 'billing_admin' end,'active'
        ) on conflict(organization_id,user_id) do update set
            role=excluded.role,status='active';
    end loop;
    insert into public.organizations(name,legacy_owner_id,billing_owner_id)
    values ('Release disabled company','release-cap-disabled',
            'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
    on conflict(legacy_owner_id) do update set name=excluded.name
    returning id into company_id;
    insert into public.organization_members(organization_id,user_id,role,status)
    values(company_id,'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa','member','disabled')
    on conflict(organization_id,user_id) do update set status='disabled';

    result := public.company_admin_active_memberships(
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        '10000000-0000-4000-8000-000000000001');
    denied := public.company_admin_active_memberships(
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        '20000000-0000-4000-8000-000000000002');
    if not coalesce((result->>'ok')::boolean,false)
       or jsonb_array_length(result->'items')<>100
       or result->'items'->0->>'company_id'<>'10000000-0000-4000-8000-000000000001'
       or coalesce((denied->>'ok')::boolean,false)
       or denied->>'code'<>'forbidden'
       or exists (
            select 1
              from jsonb_to_recordset(result->'items')
                   as item(company_id uuid,company_name text,role text)
              left join public.organization_members member
                on member.organization_id=item.company_id
               and member.user_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
             where member.user_id is null or member.status<>'active'
                or item.role not in (
                    'company_owner','company_admin','member','billing_admin')
                or item.role<>member.role
                or char_length(item.company_name)>200
       ) then
        raise exception 'active membership actor/role/cap contract failed';
    end if;
end;
$$;
