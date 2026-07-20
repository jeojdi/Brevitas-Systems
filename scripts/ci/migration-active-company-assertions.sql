\set ON_ERROR_STOP on

-- Migration 013 stores no browser-readable tenant selector and exposes only
-- service-role RPCs. The actor UUID must always come from the verified session.
do $$
declare
    checked_role text;
    checked_privilege text;
    function_signature text;
    function_oid oid;
begin
    if not (select relation.relrowsecurity from pg_class relation
             where relation.oid='public.active_company_selections'::regclass) then
        raise exception 'active company selections do not have RLS enabled';
    end if;
    foreach checked_role in array array['anon','authenticated','service_role'] loop
        foreach checked_privilege in array array[
            'SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'
        ] loop
            if has_table_privilege(
                checked_role,'public.active_company_selections',checked_privilege
            ) then
                raise exception 'unsafe active company table privilege: % %',
                    checked_role,checked_privilege;
            end if;
        end loop;
    end loop;
    foreach function_signature in array array[
        'public.company_admin_resolve_active_membership(uuid)',
        'public.company_admin_select_active_membership(uuid,uuid,text)'
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
            raise exception 'unsafe active company RPC privilege: %',function_signature;
        end if;
    end loop;
end;
$$;

-- A foreign target is rejected, an active membership can be selected, and a
-- later disable repairs the saved preference to another live membership.
do $$
declare
    selected jsonb;
    denied jsonb;
    resolved jsonb;
begin
    denied := public.company_admin_select_active_membership(
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        '20000000-0000-4000-8000-000000000002',
        'release-active-foreign'
    );
    if coalesce((denied->>'ok')::boolean,false)
       or denied->>'code'<>'forbidden' then
        raise exception 'foreign active company selection was accepted';
    end if;

    selected := public.company_admin_select_active_membership(
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        '10000000-0000-4000-8000-000000000001',
        'release-active-select'
    );
    resolved := public.company_admin_resolve_active_membership(
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    );
    if selected->>'company_id'<>'10000000-0000-4000-8000-000000000001'
       or selected->>'role'<>'company_owner'
       or resolved->>'company_id'<>'10000000-0000-4000-8000-000000000001'
       or not exists (
            select 1 from public.active_company_selections selection
             where selection.user_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
               and selection.organization_id='10000000-0000-4000-8000-000000000001'
       )
       or (select count(*) from public.audit_events event
            where event.request_id='release-active-select'
              and event.action='company.active_selected'
              and event.organization_id='10000000-0000-4000-8000-000000000001')<>1
    then raise exception 'active company selection was not persisted safely'; end if;

    update public.organization_members
       set status='disabled',disabled_at=now(),updated_at=now()
     where organization_id='10000000-0000-4000-8000-000000000001'
       and user_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
    resolved := public.company_admin_resolve_active_membership(
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    );
    if not coalesce((resolved->>'ok')::boolean,false)
       or resolved->>'company_id'='10000000-0000-4000-8000-000000000001'
       or not exists (
            select 1 from public.organization_members member
             where member.user_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
               and member.organization_id=(resolved->>'company_id')::uuid
               and member.status='active'
               and member.role=resolved->>'role'
       ) then
        raise exception 'stale active company selection was not repaired';
    end if;

    update public.organization_members
       set status='active',disabled_at=null,updated_at=now()
     where organization_id='10000000-0000-4000-8000-000000000001'
       and user_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
    perform public.company_admin_select_active_membership(
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        '10000000-0000-4000-8000-000000000001',
        'release-active-restore'
    );
end;
$$;
