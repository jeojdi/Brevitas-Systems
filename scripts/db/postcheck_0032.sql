-- Postconditions for 202607280032 on wyfz. Read-only.
select 'anon_executable_secdef' as metric, count(*)::text as value,
       '0 expected' as expectation
  from pg_proc p join pg_namespace n on n.oid=p.pronamespace
 where n.nspname='public' and p.prosecdef
   and not exists (select 1 from pg_depend d where d.objid=p.oid and d.classid='pg_proc'::regclass and d.deptype='e')
   and has_function_privilege('anon', p.oid, 'execute')
union all
select 'authenticated_executable_secdef', count(*)::text, '0 expected'
  from pg_proc p join pg_namespace n on n.oid=p.pronamespace
 where n.nspname='public' and p.prosecdef
   and not exists (select 1 from pg_depend d where d.objid=p.oid and d.classid='pg_proc'::regclass and d.deptype='e')
   and has_function_privilege('authenticated', p.oid, 'execute')
union all
select 'service_role_executable', count(*)::text, 'must stay ~130 (money paths intact)'
  from pg_proc p join pg_namespace n on n.oid=p.pronamespace
 where n.nspname='public'
   and not exists (select 1 from pg_depend d where d.objid=p.oid and d.classid='pg_proc'::regclass and d.deptype='e')
   and has_function_privilege('service_role', p.oid, 'execute')
union all
select 'guard_installed', (to_regprocedure('public.assert_browser_role_function_privileges()') is not null)::text, 'true expected'
union all
select 'attestation_still_locked',
       (not has_function_privilege('service_role','public.attest_billing_arrangement(uuid,text,text,text,timestamptz,text)','execute'))::text,
       'true expected (no role may self-attest)';
