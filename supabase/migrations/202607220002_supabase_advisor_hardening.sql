begin;

-- Static permission mapping does not need any caller-controlled schema lookup.
alter function public.company_role_permissions(text)
    set search_path = pg_catalog, pg_temp;

-- Trigger and event-trigger functions run through their owning trigger. They
-- are not public RPCs and must not inherit PostgreSQL's default PUBLIC EXECUTE.
revoke all on function public.handle_new_user()
    from public, anon, authenticated, service_role;
revoke all on function public.record_legal_acceptance()
    from public, anon, authenticated, service_role;

-- Supabase-managed projects install this event-trigger function. Plain
-- PostgreSQL integration fixtures do not, so harden it only when present.
do $managed_event_trigger$
begin
    if to_regprocedure('public.rls_auto_enable()') is not null then
        revoke all on function public.rls_auto_enable()
            from public, anon, authenticated, service_role;
    end if;
end;
$managed_event_trigger$;

do $advisor_contract$
declare
    v_role_map_config text[];
begin
    select proconfig
      into v_role_map_config
      from pg_proc
     where oid = 'public.company_role_permissions(text)'::regprocedure;

    if v_role_map_config is null
       or not ('search_path=pg_catalog, pg_temp' = any(v_role_map_config)) then
        raise exception 'company role permission search path is not fixed';
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
        raise exception 'trigger-only functions retain direct execute access';
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
            raise exception 'managed event-trigger function retains direct execute access';
        end if;
    end if;
end;
$advisor_contract$;

commit;
