\set ON_ERROR_STOP on

do $$
declare
    checked_role text;
    checked_privilege text;
    submission_result jsonb;
    function_oid oid := to_regprocedure(
        'public.submit_waitlist_signup(text,text,text,text,text,text,text,text,boolean)'
    );
begin
    if not (select relation.relrowsecurity from pg_class relation
             where relation.oid = 'public.waitlist'::regclass) then
        raise exception 'waitlist RLS is disabled';
    end if;

    foreach checked_role in array array['anon','authenticated','service_role'] loop
        foreach checked_privilege in array array[
            'SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'
        ] loop
            if has_table_privilege(
                checked_role,'public.waitlist',checked_privilege
            ) then
                raise exception 'unsafe waitlist table privilege: % %',
                    checked_role,checked_privilege;
            end if;
        end loop;
    end loop;

    if function_oid is null
       or not has_function_privilege('service_role',function_oid,'EXECUTE')
       or has_function_privilege('anon',function_oid,'EXECUTE')
       or has_function_privilege('authenticated',function_oid,'EXECUTE')
       or exists (
            select 1 from aclexplode(coalesce(
                (select procedure.proacl from pg_proc procedure
                  where procedure.oid = function_oid),
                acldefault('f',(select procedure.proowner from pg_proc procedure
                                 where procedure.oid = function_oid))
            )) privilege
             where privilege.grantee = 0 and privilege.privilege_type = 'EXECUTE'
       ) then
        raise exception 'waitlist RPC privilege boundary is unsafe';
    end if;
    if exists (
        select 1
        from pg_proc procedure
        join pg_namespace namespace on namespace.oid = procedure.pronamespace
        where namespace.nspname = 'public'
          and procedure.proname = 'submit_waitlist_signup'
          and procedure.oid <> function_oid
          and (
              has_function_privilege('anon',procedure.oid,'EXECUTE')
              or has_function_privilege('authenticated',procedure.oid,'EXECUTE')
              or exists (
                  select 1 from aclexplode(coalesce(
                      procedure.proacl,
                      acldefault('f',procedure.proowner)
                  )) privilege
                  where privilege.grantee = 0
                    and privilege.privilege_type = 'EXECUTE'
              )
          )
    ) then
        raise exception 'a legacy waitlist RPC overload remains browser-callable';
    end if;

    if not exists (
        select 1 from pg_proc procedure
        where procedure.oid = function_oid
          and procedure.prosecdef
          and 'search_path=pg_catalog, public, extensions, pg_temp' = any(procedure.proconfig)
    ) then
        raise exception 'waitlist RPC is not a hardened security-definer function';
    end if;

    if not exists (
        select 1 from pg_constraint constraint_state
        where constraint_state.conrelid = 'public.waitlist'::regclass
          and constraint_state.conname = 'waitlist_email_canonical_check'
    ) or not exists (
        select 1 from pg_constraint constraint_state
        where constraint_state.conrelid = 'public.waitlist'::regclass
          and constraint_state.conname = 'waitlist_field_lengths_check'
    ) then
        raise exception 'waitlist database validation constraints are missing';
    end if;

    begin
        execute 'set local role anon';
        execute $anon_insert$
            insert into public.waitlist(email) values ('anon-bypass@example.com')
        $anon_insert$;
        raise exception 'anon direct waitlist insert unexpectedly succeeded';
    exception when insufficient_privilege then null;
    end;

    begin
        execute 'set local role anon';
        perform public.submit_waitlist_signup(
            'anon-rpc@example.com',null,null,null,null,null,null,null,false
        );
        raise exception 'anon waitlist RPC unexpectedly succeeded';
    exception when insufficient_privilege then null;
    end;

    submission_result:=public.submit_waitlist_signup(
        'Release-Waitlist@Example.com',
        ' Release User ',
        ' Release Company ',
        ' Engineer ',
        ' Three agents ',
        ' 1000 ',
        ' Custom ',
        ' Release validation ',
        true
    );
    if submission_result->>'code'<>'accepted'
       or coalesce((submission_result->>'created')::boolean,false) is not true then
        raise exception 'server-authorized waitlist signup was not inserted';
    end if;
    submission_result:=public.submit_waitlist_signup(
        'release-waitlist@example.com',null,null,null,null,null,null,null,false
    );
    if submission_result->>'code'<>'accepted'
       or coalesce((submission_result->>'created')::boolean,true) is not false then
        raise exception 'duplicate waitlist signup was not idempotent';
    end if;
    if not exists (
        select 1 from public.waitlist
        where email = 'release-waitlist@example.com'
          and name = 'Release User'
          and company = 'Release Company'
          and design_partner
    ) then
        raise exception 'waitlist RPC did not normalize and persist bounded fields';
    end if;

    begin
        perform public.submit_waitlist_signup(
            'invalid-email',null,null,null,null,null,null,null,false
        );
        raise exception 'invalid waitlist email unexpectedly persisted';
    exception when check_violation then null;
    end;
    begin
        perform public.submit_waitlist_signup(
            'oversized@example.com',null,null,null,repeat('x',2001),null,null,null,false
        );
        raise exception 'oversized waitlist field unexpectedly persisted';
    exception when check_violation then null;
    end;
end;
$$;
