\set ON_ERROR_STOP on

do $advisor_assertions$
declare
    v_role_map_config text[];
begin
    select proconfig
      into v_role_map_config
      from pg_proc
     where oid = 'public.company_role_permissions(text)'::regprocedure;

    if v_role_map_config is null
       or not ('search_path=pg_catalog, pg_temp' = any(v_role_map_config)) then
        raise exception 'company_role_permissions search path drifted';
    end if;

    if has_function_privilege(
           'anon', 'public.handle_new_user()', 'EXECUTE'
       )
       or has_function_privilege(
           'authenticated', 'public.handle_new_user()', 'EXECUTE'
       )
       or has_function_privilege(
           'service_role', 'public.handle_new_user()', 'EXECUTE'
       )
       or has_function_privilege(
           'anon', 'public.record_legal_acceptance()', 'EXECUTE'
       )
       or has_function_privilege(
           'authenticated', 'public.record_legal_acceptance()', 'EXECUTE'
       )
       or has_function_privilege(
           'service_role', 'public.record_legal_acceptance()', 'EXECUTE'
       ) then
        raise exception 'trigger-only execute access drifted';
    end if;

    if to_regprocedure('public.rls_auto_enable()') is not null then
        if has_function_privilege(
               'anon', 'public.rls_auto_enable()', 'EXECUTE'
           )
           or has_function_privilege(
               'authenticated', 'public.rls_auto_enable()', 'EXECUTE'
           )
           or has_function_privilege(
               'service_role', 'public.rls_auto_enable()', 'EXECUTE'
           ) then
            raise exception 'managed event-trigger execute access drifted';
        end if;
    end if;

    if not exists (
        select 1
          from pg_trigger
         where tgname = 'on_auth_user_created'
           and not tgisinternal
    ) or not exists (
        select 1
          from pg_trigger
         where tgname = 'on_auth_user_legal_acceptance'
           and not tgisinternal
    ) then
        raise exception 'a hardened trigger entry point is missing or disabled';
    end if;

    if to_regprocedure('public.rls_auto_enable()') is not null
       and not exists (
           select 1
             from pg_event_trigger
            where evtname = 'rls_auto_enable'
              and evtenabled <> 'D'
       ) then
        raise exception 'the managed RLS event trigger is missing or disabled';
    end if;
end;
$advisor_assertions$;
