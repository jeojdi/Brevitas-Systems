\set ON_ERROR_STOP on

-- Run only against the ephemeral migration-test database after the complete
-- forward chain. These assertions freeze migration 009's strict key boundary.
-- The fixtures below use opaque hashes and never connect to staging or production.

do $$
declare
    function_oid oid;
    function_signature text;
begin
    foreach function_signature in array array[
        'public.company_admin_create_dashboard_session_key(uuid,uuid,text,text,timestamptz,text)',
        'public.company_admin_dashboard_keys_page(uuid,uuid,timestamptz,uuid,integer,text)',
        'public.company_admin_revoke_dashboard_session_key(uuid,uuid,uuid,text)'
    ] loop
        function_oid := to_regprocedure(function_signature);
        if function_oid is null
           or not has_function_privilege('service_role', function_oid, 'EXECUTE')
           or has_function_privilege('anon', function_oid, 'EXECUTE')
           or has_function_privilege('authenticated', function_oid, 'EXECUTE')
           or exists (
                select 1
                  from aclexplode(coalesce(
                    (select procedure.proacl from pg_proc procedure
                      where procedure.oid = function_oid),
                    acldefault('f', (select procedure.proowner from pg_proc procedure
                      where procedure.oid = function_oid))
                  )) privilege
                 where privilege.grantee = 0 and privilege.privilege_type = 'EXECUTE'
           ) then
            raise exception 'unsafe atomic key RPC privilege: %', function_signature;
        end if;
    end loop;
    if to_regprocedure('public.company_admin_revoke_key(uuid,uuid,uuid,text)') is not null then
        raise exception 'migration 008 generic key revocation RPC survived migration 009';
    end if;
end;
$$;

do $$
declare
    created jsonb;
    first_key_id uuid;
begin
    created := public.company_admin_create_dashboard_session_key(
        '10000000-0000-4000-8000-000000000001',
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        repeat('a', 64), 'bvt_atomic01', now() + interval '1 hour',
        'release-key-create-first'
    );
    if not coalesce((created->>'ok')::boolean, false) then
        raise exception 'atomic dashboard key creation was denied';
    end if;
    first_key_id := (created->>'key_id')::uuid;
    if not exists (
        select 1 from public.api_keys key
         where key.id = first_key_id
           and key.organization_id = '10000000-0000-4000-8000-000000000001'
           and key.created_by = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
           and key.key_hash = repeat('a', 64)
           and key.key_type = 'dashboard_session'
           and key.revoked_at is null
    ) or not exists (
        select 1 from public.audit_events event
         where event.organization_id = '10000000-0000-4000-8000-000000000001'
           and event.request_id = 'release-key-create-first'
           and event.action = 'dashboard_session.created'
           and event.target_type = 'api_key'
           and event.target_id = first_key_id::text
           and event.outcome = 'committed'
    ) then
        raise exception 'key row and immutable creation evidence did not commit together';
    end if;
    if exists (
        select 1 from public.audit_events event
         where event.request_id = 'release-key-create-first'
           and to_jsonb(event)::text like '%' || repeat('a', 64) || '%'
    ) then
        raise exception 'key digest entered immutable audit evidence';
    end if;
end;
$$;

create or replace function public.release_security_reject_key_audit()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
    if new.request_id in ('release-key-create-failure', 'release-key-revoke-failure') then
        raise exception 'release security forced audit failure' using errcode = '55000';
    end if;
    return new;
end;
$$;
drop trigger if exists release_security_reject_key_audit on public.audit_events;
create trigger release_security_reject_key_audit
    before insert on public.audit_events
    for each row execute function public.release_security_reject_key_audit();

do $$
declare
    first_key_id uuid;
begin
    select key.id into strict first_key_id
      from public.api_keys key
     where key.key_hash = repeat('a', 64);
    begin
        perform public.company_admin_create_dashboard_session_key(
            '10000000-0000-4000-8000-000000000001',
            'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            repeat('b', 64), 'bvt_atomic02', now() + interval '1 hour',
            'release-key-create-failure'
        );
        raise exception 'key creation unexpectedly survived audit failure';
    exception when object_not_in_prerequisite_state then
        null;
    end;
    if exists (select 1 from public.api_keys where key_hash = repeat('b', 64))
       or (select revoked_at from public.api_keys where id = first_key_id) is not null
       or exists (select 1 from public.audit_events
                   where request_id = 'release-key-create-failure') then
        raise exception 'failed creation did not roll back key replacement and audit';
    end if;
end;
$$;

drop trigger release_security_reject_key_audit on public.audit_events;

do $$
declare
    created jsonb;
    denied jsonb;
    listed jsonb;
    revoked jsonb;
    first_key_id uuid;
    second_key_id uuid;
begin
    select key.id into strict first_key_id
      from public.api_keys key where key.key_hash = repeat('a', 64);
    created := public.company_admin_create_dashboard_session_key(
        '10000000-0000-4000-8000-000000000001',
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        repeat('c', 64), 'bvt_atomic03', now() + interval '1 hour',
        'release-key-create-second'
    );
    second_key_id := (created->>'key_id')::uuid;
    if not coalesce((created->>'ok')::boolean, false)
       or (select revoked_at from public.api_keys where id = first_key_id) is null
       or (select revoked_at from public.api_keys where id = second_key_id) is not null then
        raise exception 'atomic dashboard key replacement failed';
    end if;

    listed := public.company_admin_dashboard_keys_page(
        '10000000-0000-4000-8000-000000000001',
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        null, null, 100, 'release-key-list-success'
    );
    if not coalesce((listed->>'ok')::boolean, false)
       or not (listed->'items' @> jsonb_build_array(jsonb_build_object('id', second_key_id)))
       or listed::text like '%' || repeat('c', 64) || '%'
       or listed::text like '%key_hash%'
       or listed::text like '%fingerprint%' then
        raise exception 'migration 009 key listing leaked credentials or omitted the tenant key';
    end if;

    denied := public.company_admin_revoke_dashboard_session_key(
        '20000000-0000-4000-8000-000000000002',
        'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        second_key_id, 'release-key-cross-tenant-revoke'
    );
    if coalesce((denied->>'ok')::boolean, false)
       or (select revoked_at from public.api_keys where id = second_key_id) is not null then
        raise exception 'cross-tenant key revocation was not denied atomically';
    end if;

    revoked := public.company_admin_revoke_dashboard_session_key(
        '10000000-0000-4000-8000-000000000001',
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        second_key_id, 'release-key-revoke-success'
    );
    if not coalesce((revoked->>'ok')::boolean, false)
       or not coalesce((revoked->>'revoked')::boolean, false)
       or (select revoked_at from public.api_keys where id = second_key_id) is null
       or not exists (
            select 1 from public.audit_events event
             where event.request_id = 'release-key-revoke-success'
               and event.action = 'dashboard_session.revoked'
               and event.target_id = second_key_id::text
               and event.outcome = 'committed'
       ) then
        raise exception 'authorized revocation and immutable audit did not commit together';
    end if;
end;
$$;

-- Prove revocation is also rolled back if its evidence cannot be appended.
create trigger release_security_reject_key_audit
    before insert on public.audit_events
    for each row execute function public.release_security_reject_key_audit();

do $$
declare
    created jsonb;
    key_id uuid;
begin
    created := public.company_admin_create_dashboard_session_key(
        '10000000-0000-4000-8000-000000000001',
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        repeat('d', 64), 'bvt_atomic04', now() + interval '1 hour',
        'release-key-revoke-fixture'
    );
    key_id := (created->>'key_id')::uuid;
    begin
        perform public.company_admin_revoke_dashboard_session_key(
            '10000000-0000-4000-8000-000000000001',
            'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            key_id, 'release-key-revoke-failure'
        );
        raise exception 'key revocation unexpectedly survived audit failure';
    exception when object_not_in_prerequisite_state then
        null;
    end;
    if (select revoked_at from public.api_keys where id = key_id) is not null
       or exists (select 1 from public.audit_events
                   where request_id = 'release-key-revoke-failure') then
        raise exception 'failed revocation did not roll back key state and audit';
    end if;
end;
$$;

drop trigger release_security_reject_key_audit on public.audit_events;
drop function public.release_security_reject_key_audit();

-- Members may list and revoke only their own dashboard session. The strict
-- migration-009 RPC also rejects long-lived service-account credentials, and a
-- retry of an already-revoked session is an audited no-op.
insert into auth.users(id,email) values (
    'dddddddd-dddd-4ddd-8ddd-dddddddddddd','release-member@example.invalid'
) on conflict(id) do nothing;
insert into public.organization_members(organization_id,user_id,role,status) values (
    '10000000-0000-4000-8000-000000000001',
    'dddddddd-dddd-4ddd-8ddd-dddddddddddd','member','active'
) on conflict(organization_id,user_id) do update set role='member',status='active';

do $$
declare
    created jsonb;
    denied jsonb;
    listed jsonb;
    revoked jsonb;
    retried jsonb;
    service_rotated jsonb;
    member_key_id uuid;
    owner_key_id uuid;
    service_key_id uuid;
begin
    created := public.company_admin_create_dashboard_session_key(
        '10000000-0000-4000-8000-000000000001',
        'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
        repeat('0',64),'bvt_member01',now()+interval '1 hour',
        'release-member-key-create'
    );
    member_key_id := (created->>'key_id')::uuid;
    select credential.id into strict owner_key_id
      from public.api_keys credential
     where credential.key_hash=repeat('d',64);
    denied := public.company_admin_revoke_dashboard_session_key(
        '10000000-0000-4000-8000-000000000001',
        'dddddddd-dddd-4ddd-8ddd-dddddddddddd',owner_key_id,
        'release-member-owner-denied'
    );
    listed := public.company_admin_dashboard_keys_page(
        '10000000-0000-4000-8000-000000000001',
        'dddddddd-dddd-4ddd-8ddd-dddddddddddd',null,null,100,
        'release-member-key-list'
    );
    if not coalesce((created->>'ok')::boolean,false)
       or coalesce((denied->>'ok')::boolean,false)
       or not coalesce((listed->>'ok')::boolean,false)
       or not (listed->'items' @> jsonb_build_array(
            jsonb_build_object('id',member_key_id)))
       or exists (
            select 1
              from jsonb_array_elements(listed->'items') item
              left join public.api_keys credential
                on credential.id=(item->>'id')::uuid
             where credential.id is null
                or credential.organization_id<>'10000000-0000-4000-8000-000000000001'
                or credential.key_type<>'dashboard_session'
                or credential.created_by is distinct from
                   'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
       ) then
        raise exception 'member dashboard listing/revoke ownership contract failed';
    end if;

    revoked := public.company_admin_revoke_dashboard_session_key(
        '10000000-0000-4000-8000-000000000001',
        'dddddddd-dddd-4ddd-8ddd-dddddddddddd',member_key_id,
        'release-member-key-revoke'
    );
    retried := public.company_admin_revoke_dashboard_session_key(
        '10000000-0000-4000-8000-000000000001',
        'dddddddd-dddd-4ddd-8ddd-dddddddddddd',member_key_id,
        'release-member-key-retry'
    );
    if not coalesce((revoked->>'revoked')::boolean,false)
       or not coalesce((retried->>'already_revoked')::boolean,false)
       or coalesce((retried->>'revoked')::boolean,true)
       or not exists (
            select 1 from public.audit_events event
             where event.request_id='release-member-key-retry'
               and event.action='dashboard_session.revoke.noop'
               and event.target_id=member_key_id::text
       ) then
        raise exception 'dashboard revoke retry was not an idempotent audited no-op';
    end if;

    service_rotated := public.company_admin_rotate_service_key(
        '10000000-0000-4000-8000-000000000001',
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        '30000000-0000-4000-8000-000000000003',repeat('1',64),
        'bvt_service01',now()+interval '12 hours','release-service-key-rotate'
    );
    service_key_id := (service_rotated->>'key_id')::uuid;
    if not coalesce((service_rotated->>'ok')::boolean,false)
       or not exists (
            select 1 from public.api_keys credential
             where credential.id=service_key_id
               and credential.key_type='organization_service'
               and credential.revoked_at is null
       ) then raise exception 'service-account key fixture is missing'; end if;
    denied := public.company_admin_revoke_dashboard_session_key(
        '10000000-0000-4000-8000-000000000001',
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',service_key_id,
        'release-service-key-strict-denied'
    );
    if coalesce((denied->>'ok')::boolean,false)
       or (select revoked_at from public.api_keys where id=service_key_id) is not null then
        raise exception 'strict dashboard revoke addressed a service-account key';
    end if;
end;
$$;
