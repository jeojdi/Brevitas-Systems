\set ON_ERROR_STOP on

do $$
declare
    missing text;
begin
    select string_agg(column_name, ', ')
      into missing
      from unnest(array[
        'provider_input_tokens_avoided',
        'native_cache_discount_usd',
        'calls_avoided',
        'transport_bytes_avoided',
        'brevitas_incremental_savings_usd'
      ]) expected(column_name)
     where not exists (
        select 1
          from information_schema.columns actual
         where actual.table_schema = 'public'
           and actual.table_name = 'usage_log'
           and actual.column_name = expected.column_name
     );
    if missing is not null then
        raise exception 'mechanism-separated usage columns are missing: %', missing;
    end if;
    if not exists (
        select 1
          from information_schema.columns
         where table_schema = 'public'
           and table_name = 'organizations'
           and column_name = 'account_type'
    ) then
        raise exception 'workspace account type is missing';
    end if;
end;
$$;

do $$
declare
    missing text;
begin
    select string_agg(index_name, ', ')
      into missing
      from unnest(array[
        'usage_log_org_page_idx',
        'usage_log_owner_page_idx',
        'usage_log_key_page_idx',
        'usage_log_org_customer_page_idx',
        'usage_log_org_pipeline_idx',
        'usage_log_org_agent_idx',
        'usage_log_org_run_idx',
        'usage_log_admin_project_idx',
        'usage_log_admin_client_idx',
        'usage_log_admin_provider_idx',
        'usage_log_admin_model_idx'
      ]) index_name
     where not exists (
        select 1
          from pg_class relation
          join pg_index index_state on index_state.indexrelid = relation.oid
         where relation.relname = index_name
           and index_state.indisvalid
           and index_state.indisready
     );
    if missing is not null then
        raise exception 'indexes are not valid and ready: %', missing;
    end if;
end;
$$;

do $$
declare
    function_name text;
    function_oid oid;
    function_count integer;
begin
    foreach function_name in array array[
        'usage_page', 'usage_stats', 'usage_breakdown', 'usage_grouped',
        'admin_usage_report', 'admin_key_repository_usage',
        'admin_usage_report_page'
    ] loop
        function_count := 0;
        for function_oid in
            select procedure.oid
              from pg_proc procedure
              join pg_namespace namespace on namespace.oid = procedure.pronamespace
             where namespace.nspname = 'public' and procedure.proname = function_name
        loop
            function_count := function_count + 1;
            if not has_function_privilege('service_role', function_oid, 'EXECUTE') then
                raise exception 'service_role cannot execute %', function_name;
            end if;
            if has_function_privilege('anon', function_oid, 'EXECUTE')
               or has_function_privilege('authenticated', function_oid, 'EXECUTE')
               or exists (
                    select 1
                      from aclexplode(coalesce(
                        (select procedure.proacl from pg_proc procedure where procedure.oid = function_oid),
                        acldefault('f', (select procedure.proowner from pg_proc procedure where procedure.oid = function_oid))
                      )) privilege
                     where privilege.grantee = 0 and privilege.privilege_type = 'EXECUTE'
               ) then
                raise exception 'browser/public role can execute %', function_name;
            end if;
        end loop;
        if function_count = 0 then
            raise exception 'required database-scaling function is missing: %', function_name;
        end if;
    end loop;
end;
$$;

insert into auth.users(id, email) values
    ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'release-a@example.invalid'),
    ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'release-b@example.invalid')
on conflict (id) do nothing;

insert into public.organizations(id, name, legacy_owner_id, billing_owner_id) values
    ('10000000-0000-4000-8000-000000000001', 'Release tenant A', 'release-owner-a', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'),
    ('20000000-0000-4000-8000-000000000002', 'Release tenant B', 'release-owner-b', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
on conflict (id) do nothing;

insert into public.organization_members(organization_id, user_id, role) values
    ('10000000-0000-4000-8000-000000000001', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'company_owner'),
    ('20000000-0000-4000-8000-000000000002', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'company_owner')
on conflict (organization_id, user_id) do nothing;

insert into public.api_keys(key_hash, name, owner_id, organization_id, key_type) values
    ('release-key-a', 'Release A', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '10000000-0000-4000-8000-000000000001', 'legacy'),
    ('release-key-b', 'Release B', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', '20000000-0000-4000-8000-000000000002', 'legacy')
on conflict (key_hash) do nothing;

insert into public.billing_accounts(
    organization_id, user_id, stripe_customer_id, subscription_status, billing_started_at,
    current_period_start, current_period_end
) values (
    '10000000-0000-4000-8000-000000000001',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'cus_release_security', 'active',
    '2025-01-01 00:00:00+00', '2026-02-28 00:00:00+00', '2026-03-07 00:00:00+00'
) on conflict (organization_id) do update set
    user_id = excluded.user_id,
    stripe_customer_id = excluded.stripe_customer_id,
    subscription_status = excluded.subscription_status,
    billing_started_at = excluded.billing_started_at,
    current_period_start = excluded.current_period_start,
    current_period_end = excluded.current_period_end;

insert into public.usage_log(
    key_hash, owner_id, organization_id, ts, request_id, project,
    baseline_tokens, optimized_tokens, tokens_saved, pricing_status,
    verified_savings_usd, brevitas_fee_usd, authoritative, receipt_source
) values
    ('release-key-a', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '10000000-0000-4000-8000-000000000001', '2026-02-01 12:00:00+00', 'release-a-1', 'equal-a-1', 10, 5, 5, 'unpriced', 0, 0, true, 'proxy'),
    ('release-key-a', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '10000000-0000-4000-8000-000000000001', '2026-02-01 12:00:00+00', 'release-a-2', 'equal-a-2', 10, 5, 5, 'unpriced', 0, 0, true, 'proxy'),
    ('release-key-a', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '10000000-0000-4000-8000-000000000001', '2026-02-01 12:00:00+00', 'release-a-3', 'equal-a-3', 10, 5, 5, 'unpriced', 0, 0, true, 'proxy'),
    ('release-key-b', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', '20000000-0000-4000-8000-000000000002', '2026-02-01 12:00:00+00', 'release-b-1', 'tenant-b', 10, 5, 5, 'unpriced', 0, 0, true, 'proxy')
on conflict (key_hash, request_id) where request_id <> '' do nothing;

do $$
declare
    first_page bigint[];
    second_page bigint[];
    cursor_timestamp timestamptz;
    cursor_id bigint;
begin
    select array_agg(page.id order by page.ts desc, page.id desc)
      into first_page
      from public.usage_page(
        'release-key-a', '10000000-0000-4000-8000-000000000001', '',
        null, null, 1
      ) page;
    if cardinality(first_page) <> 2 then
        raise exception 'first equal-timestamp cursor page did not include lookahead';
    end if;
    select usage.ts, usage.id into cursor_timestamp, cursor_id
      from public.usage_log usage where usage.id = first_page[1];
    select array_agg(page.id order by page.ts desc, page.id desc)
      into second_page
      from public.usage_page(
        'release-key-a', '10000000-0000-4000-8000-000000000001', '',
        cursor_timestamp, cursor_id, 1
      ) page;
    if cardinality(second_page) <> 2 or first_page[1] = any(second_page) then
        raise exception 'equal-timestamp cursor is unstable or duplicated a row';
    end if;
    if exists (
        select 1 from public.usage_page(
          'release-key-a', '10000000-0000-4000-8000-000000000001', '',
          null, null, 200
        ) page where page.organization_id <> '10000000-0000-4000-8000-000000000001'
    ) then
        raise exception 'usage_page crossed the tenant boundary';
    end if;
end;
$$;

do $$
declare
    first_result jsonb;
    second_result jsonb;
    cursor_value numeric;
    cursor_key text;
begin
    first_result := public.admin_usage_report_page(
        '{"organization_id":"10000000-0000-4000-8000-000000000001"}'::jsonb,
        'tokens_saved', 'desc', null, null, 1
    );
    if jsonb_array_length(first_result->'rows') <> 2 then
        raise exception 'admin cursor page did not include lookahead';
    end if;
    cursor_value := (first_result->'rows'->0->>'_sort_value')::numeric;
    cursor_key := first_result->'rows'->0->>'_row_key';
    second_result := public.admin_usage_report_page(
        '{"organization_id":"10000000-0000-4000-8000-000000000001"}'::jsonb,
        'tokens_saved', 'desc', cursor_value, cursor_key, 1
    );
    if jsonb_array_length(second_result->'rows') <> 2
       or second_result->'rows'->0->>'_row_key' = cursor_key then
        raise exception 'admin equal-sort cursor is unstable or duplicated a group';
    end if;
end;
$$;

do $$
declare
    checked_start timestamptz;
    checked_end timestamptz;
begin
    select period_start, period_end into checked_start, checked_end
      from public.billing_period_for_occurrence(
        '2026-02-28 00:00:00+00', '2026-02-28 00:00:00+00', '2026-03-07 00:00:00+00'
      );
    if checked_start <> '2026-02-28 00:00:00+00'
       or checked_end <> '2026-03-07 00:00:00+00' then
        raise exception 'billing week boundary self-check failed';
    end if;
end;
$$;

insert into public.usage_log(
    key_hash, owner_id, organization_id, ts, request_id, project,
    baseline_tokens, optimized_tokens, tokens_saved, pricing_status,
    verified_savings_usd, brevitas_fee_usd, authoritative, receipt_source
) values (
    'release-key-a', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    '10000000-0000-4000-8000-000000000001', now() - interval '1 day',
    'release-billing-1', 'billing-release', 100, 50, 50, 'priced', 1.0, 0.25, true, 'proxy'
) on conflict (key_hash, request_id) where request_id <> '' do nothing;

do $$
declare
    ledger_id bigint;
    claimed record;
begin
    select ledger.id into ledger_id
      from public.billing_ledger ledger
      join public.usage_log usage on usage.id = ledger.usage_log_id
     where usage.request_id = 'release-billing-1';
    if ledger_id is null then raise exception 'billing trigger did not create a ledger row'; end if;
    -- Keep the upgrade-baseline ledger fixture from consuming the bounded
    -- single-row claim before this test's purpose-built entry. A missing
    -- result must never pass through PL/pgSQL's three-valued NULL comparison.
    update public.billing_ledger
       set next_attempt_at = now() + interval '1 hour'
     where id <> ledger_id
       and status = 'pending';
    update public.billing_ledger
       set next_attempt_at = now() - interval '1 second'
     where id = ledger_id;
    begin
        delete from public.billing_ledger where id = ledger_id;
        raise exception 'billing ledger deletion unexpectedly succeeded';
    exception when others then
        if sqlerrm = 'billing ledger deletion unexpectedly succeeded' then raise; end if;
    end;
    begin
        update public.billing_ledger set fee_microusd = fee_microusd + 1 where id = ledger_id;
        raise exception 'billing ledger identity update unexpectedly succeeded';
    exception when others then
        if sqlerrm = 'billing ledger identity update unexpectedly succeeded' then raise; end if;
    end;
    select * into claimed
      from public.claim_billing_ledger_entries('release-worker', 30, 1, 1000000);
    if claimed.id is distinct from ledger_id
       or claimed.reclaimed is distinct from false then
        raise exception 'fresh billing lease claim failed';
    end if;
    update public.billing_ledger set lease_expires_at = now() - interval '1 second'
     where id = ledger_id;
    select * into claimed
      from public.claim_billing_ledger_entries('release-worker-2', 30, 1, 1000000);
    if claimed.id is distinct from ledger_id
       or claimed.reclaimed is distinct from true then
        raise exception 'stale billing lease recovery failed';
    end if;
end;
$$;

do $$
declare
    created jsonb;
    denied jsonb;
    account_id uuid;
begin
    created := public.company_admin_create_service_account(
        '10000000-0000-4000-8000-000000000001',
        'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        '30000000-0000-4000-8000-000000000003',
        'release-security', 'staging', array['proxy:invoke']::text[],
        repeat('f', 64), 'bvt_release1',
        now() + interval '1 day', 'release-service-create-0001'
    );
    if not coalesce((created->>'ok')::boolean, false) then
        raise exception 'company owner could not create a bounded service account';
    end if;
    account_id := (created->>'id')::uuid;
    denied := public.company_admin_revoke_service_account(
        '20000000-0000-4000-8000-000000000002',
        'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        account_id, 'release-cross-tenant-0002'
    );
    if coalesce((denied->>'ok')::boolean, false) then
        raise exception 'cross-tenant service-account administration succeeded';
    end if;
    if not exists (
        select 1 from public.service_accounts
         where id = account_id
           and organization_id = '10000000-0000-4000-8000-000000000001'
           and status = 'active'
    ) then raise exception 'cross-tenant administration changed the service account'; end if;
    if not exists (
        select 1 from public.api_keys
         where key_hash = repeat('f', 64)
           and organization_id = '10000000-0000-4000-8000-000000000001'
           and service_account_id = account_id
           and key_type = 'organization_service'
           and owner_id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
           and created_by = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
           and key_prefix = 'bvt_release1'
           and revoked_at is null
    ) then raise exception 'service-account creation did not issue its initial billing-owned key'; end if;
end;
$$;

select public.append_company_audit(
    '10000000-0000-4000-8000-000000000001',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    'company_owner', 'release-audit-event-0003',
    'release_security.migration_test', 'release', 'ephemeral', 'committed'
);

do $$
declare
    audit_id bigint;
begin
    select max(id) into audit_id from public.audit_events
     where action = 'release_security.migration_test';
    begin
        update public.audit_events set action = 'tampered' where id = audit_id;
        raise exception 'audit event update unexpectedly succeeded';
    exception when others then
        if sqlerrm = 'audit event update unexpectedly succeeded' then raise; end if;
    end;
    begin
        delete from public.audit_events where id = audit_id;
        raise exception 'audit event deletion unexpectedly succeeded';
    exception when others then
        if sqlerrm = 'audit event deletion unexpectedly succeeded' then raise; end if;
    end;
    begin
        truncate table public.audit_events;
        raise exception 'audit event truncation unexpectedly succeeded';
    exception when others then
        if sqlerrm = 'audit event truncation unexpectedly succeeded' then raise; end if;
    end;
    begin
        insert into public.audit_events(
            organization_id, actor_id, actor_role, request_id, action,
            target_type, target_id, outcome, details
        ) values (
            '10000000-0000-4000-8000-000000000001',
            'system', 'system', 'release-invalid-audit-0004',
            'release_security.invalid', 'release', 'ephemeral', 'committed',
            '{"content":"forbidden"}'::jsonb
        );
        raise exception 'content-bearing audit event unexpectedly succeeded';
    exception when others then
        if sqlerrm = 'content-bearing audit event unexpectedly succeeded' then raise; end if;
    end;
    if not exists (
        select 1 from pg_class relation
         where relation.oid = 'public.audit_events'::regclass and relation.relrowsecurity
    ) then raise exception 'audit events RLS is disabled'; end if;
end;
$$;

do $$
declare
    procedure_row record;
    checked integer := 0;
begin
    for procedure_row in
        select procedure.oid, procedure.proname, procedure.proacl, procedure.proowner
          from pg_proc procedure
          join pg_namespace namespace on namespace.oid = procedure.pronamespace
         where namespace.nspname = 'public'
           and (
             procedure.proname like 'company_admin_%'
             or procedure.proname in (
               'append_company_audit', 'company_role_permissions',
               'lock_company_actor_role', 'lock_company_admin_namespace',
               'service_key_authorization', 'ensure_enterprise_organization'
             )
           )
    loop
        checked := checked + 1;
        if not has_function_privilege('service_role', procedure_row.oid, 'EXECUTE') then
            raise exception 'service_role cannot execute administration RPC %', procedure_row.proname;
        end if;
        if has_function_privilege('anon', procedure_row.oid, 'EXECUTE')
           or has_function_privilege('authenticated', procedure_row.oid, 'EXECUTE')
           or exists (
                select 1
                  from aclexplode(coalesce(
                    procedure_row.proacl,
                    acldefault('f', procedure_row.proowner)
                  )) privilege
                 where privilege.grantee = 0 and privilege.privilege_type = 'EXECUTE'
           ) then
            raise exception 'browser/public role can execute administration RPC %', procedure_row.proname;
        end if;
    end loop;
    if checked < 10 then raise exception 'administration RPC permission surface is incomplete'; end if;
end;
$$;
