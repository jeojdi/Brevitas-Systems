-- Company billing became organization-addressed in 202607200006. The original
-- compliance workflow predates that contract and selected/mutated retained
-- billing evidence through the compatibility billing-owner snapshot. One user
-- may now own multiple companies, so every compliance boundary below uses the
-- immutable organization identity instead.

begin;

do $migration_precondition$
begin
    if to_regprocedure(
        'public.compliance_export_tenant(uuid,uuid,text)'
    ) is null
       or to_regprocedure(
        'public.compliance_export_subject(uuid,uuid,text)'
    ) is null
       or to_regprocedure(
        'public.compliance_delete_tenant(uuid,uuid,text)'
    ) is null
       or to_regprocedure(
        'public.compliance_delete_subject(uuid,uuid,text)'
    ) is null
       or not exists (
            select 1
              from information_schema.columns
             where table_schema = 'public'
               and table_name = 'billing_accounts'
               and column_name = 'organization_id'
               and is_nullable = 'NO'
       )
       or not exists (
            select 1
              from information_schema.columns
             where table_schema = 'public'
               and table_name = 'billing_ledger'
               and column_name = 'organization_id'
               and is_nullable = 'NO'
       ) then
        raise exception using
            errcode = '55000',
            message = '202607200011 requires the company billing identity migration';
    end if;
end;
$migration_precondition$;

-- Legacy billing events have no safe tenant identity. Backfill only rows whose
-- owner has exactly one company billing account; shared-owner rows remain null
-- and therefore cannot appear in a tenant- or subject-scoped export.
alter table public.billing_events
    add column if not exists organization_id uuid
        references public.organizations(id) on delete restrict;

with unambiguous_billing_owner as (
    select account.user_id,
           (array_agg(account.organization_id order by account.organization_id))[1]
               as organization_id
      from public.billing_accounts account
     group by account.user_id
    having count(*) = 1
)
update public.billing_events event
   set organization_id = owner_scope.organization_id
  from unambiguous_billing_owner owner_scope
 where event.organization_id is null
   and event.user_id = owner_scope.user_id;

create index if not exists billing_events_company_user_ts_idx
    on public.billing_events(organization_id, user_id, ts desc)
    where organization_id is not null;

-- The pre-company functions are retained only as private implementation
-- details so their complete export schema stays byte-for-byte compatible.
-- Public entry points below discard their unsafe billing records and append
-- records selected by organization_id.
do $rename_pre_company_functions$
begin
    if to_regprocedure(
        'public.compliance_export_tenant_pre_company_identity(uuid,uuid,text)'
    ) is null then
        alter function public.compliance_export_tenant(uuid,uuid,text)
            rename to compliance_export_tenant_pre_company_identity;
    end if;
    if to_regprocedure(
        'public.compliance_export_subject_pre_company_identity(uuid,uuid,text)'
    ) is null then
        alter function public.compliance_export_subject(uuid,uuid,text)
            rename to compliance_export_subject_pre_company_identity;
    end if;
    if to_regprocedure(
        'public.compliance_delete_tenant_pre_company_identity(uuid,uuid,text)'
    ) is null then
        alter function public.compliance_delete_tenant(uuid,uuid,text)
            rename to compliance_delete_tenant_pre_company_identity;
    end if;
    if to_regprocedure(
        'public.compliance_delete_subject_pre_company_identity(uuid,uuid,text)'
    ) is null then
        alter function public.compliance_delete_subject(uuid,uuid,text)
            rename to compliance_delete_subject_pre_company_identity;
    end if;
end;
$rename_pre_company_functions$;

revoke all on function public.compliance_export_tenant_pre_company_identity(
    uuid,uuid,text
) from public, anon, authenticated, service_role;
revoke all on function public.compliance_export_subject_pre_company_identity(
    uuid,uuid,text
) from public, anon, authenticated, service_role;
revoke all on function public.compliance_delete_tenant_pre_company_identity(
    uuid,uuid,text
) from public, anon, authenticated, service_role;
revoke all on function public.compliance_delete_subject_pre_company_identity(
    uuid,uuid,text
) from public, anon, authenticated, service_role;

create or replace function public.compliance_export_tenant(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns setof jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
begin
    return query
        select exported.record
          from public.compliance_export_tenant_pre_company_identity(
                p_organization_id, p_request_id, p_actor_id
          ) as exported(record)
         where coalesce(exported.record->>'record_type', '')
               not in ('billing_account', 'billing_ledger', 'legacy_billing_event');

    -- A completed request is intentionally replay-empty, matching the original
    -- RPC. The private implementation changes an approved request to processing
    -- before returning its records.
    if not exists (
        select 1
          from public.data_subject_requests request
         where request.id = p_request_id
           and request.organization_id = p_organization_id
           and request.request_type = 'export'
           and request.request_scope = 'tenant'
           and request.status = 'processing'
    ) then
        return;
    end if;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'billing_account',
            'data', pg_catalog.jsonb_build_object(
                'user_id', account.user_id,
                'stripe_customer_id', account.stripe_customer_id,
                'stripe_subscription_id', account.stripe_subscription_id,
                'subscription_status', account.subscription_status,
                'checkout_session_id', account.checkout_session_id,
                'billing_started_at', account.billing_started_at,
                'current_period_start', account.current_period_start,
                'current_period_end', account.current_period_end,
                'last_invoice_id', account.last_invoice_id,
                'last_invoice_status', account.last_invoice_status,
                'stripe_subscription_event_created',
                    account.stripe_subscription_event_created,
                'stripe_invoice_event_created',
                    account.stripe_invoice_event_created,
                'created_at', account.created_at,
                'updated_at', account.updated_at
            )
        )
          from public.billing_accounts account
         where account.organization_id = p_organization_id;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'billing_ledger',
            'data', pg_catalog.jsonb_build_object(
                'id', ledger.id,
                'usage_log_id', ledger.usage_log_id,
                'user_id', ledger.user_id,
                'occurred_at', ledger.occurred_at,
                'fee_microusd', ledger.fee_microusd,
                'status', ledger.status,
                'attempts', ledger.attempts,
                'reported_at', ledger.reported_at,
                'last_error', ledger.last_error,
                'created_at', ledger.created_at
            )
        )
          from public.billing_ledger ledger
         where ledger.organization_id = p_organization_id
         order by ledger.id;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'legacy_billing_event',
            'data', pg_catalog.to_jsonb(event)
        )
          from public.billing_events event
         where event.organization_id = p_organization_id
         order by event.ts, event.id;
end;
$function$;

create or replace function public.compliance_export_subject(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns setof jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
declare
    v_request public.data_subject_requests%rowtype;
begin
    return query
        select exported.record
          from public.compliance_export_subject_pre_company_identity(
                p_organization_id, p_request_id, p_actor_id
          ) as exported(record)
         where coalesce(exported.record->>'record_type', '')
               not in ('billing_account', 'billing_ledger', 'legacy_billing_event');

    select * into v_request
      from public.data_subject_requests request
     where request.id = p_request_id
       and request.organization_id = p_organization_id;
    if not found
       or v_request.request_type <> 'export'
       or v_request.request_scope <> 'member'
       or v_request.status <> 'processing' then
        return;
    end if;

    -- Preserve the original member-subject semantics: billing evidence is a
    -- subject relationship only when that member is the compatibility owner,
    -- while organization_id prevents evidence from another owned company.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'billing_account',
            'data', pg_catalog.jsonb_build_object(
                'user_id', account.user_id,
                'stripe_customer_id', account.stripe_customer_id,
                'stripe_subscription_id', account.stripe_subscription_id,
                'subscription_status', account.subscription_status,
                'checkout_session_id', account.checkout_session_id,
                'billing_started_at', account.billing_started_at,
                'current_period_start', account.current_period_start,
                'current_period_end', account.current_period_end,
                'last_invoice_id', account.last_invoice_id,
                'last_invoice_status', account.last_invoice_status,
                'stripe_subscription_event_created',
                    account.stripe_subscription_event_created,
                'stripe_invoice_event_created',
                    account.stripe_invoice_event_created,
                'created_at', account.created_at,
                'updated_at', account.updated_at
            )
        )
          from public.billing_accounts account
         where account.organization_id = p_organization_id
           and account.user_id = v_request.subject_id;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'billing_ledger',
            'data', pg_catalog.jsonb_build_object(
                'id', ledger.id,
                'usage_log_id', ledger.usage_log_id,
                'user_id', ledger.user_id,
                'occurred_at', ledger.occurred_at,
                'fee_microusd', ledger.fee_microusd,
                'status', ledger.status,
                'attempts', ledger.attempts,
                'reported_at', ledger.reported_at,
                'last_error', ledger.last_error,
                'created_at', ledger.created_at
            )
        )
          from public.billing_ledger ledger
         where ledger.organization_id = p_organization_id
           and ledger.user_id = v_request.subject_id
         order by ledger.id;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'legacy_billing_event',
            'data', pg_catalog.to_jsonb(event)
        )
          from public.billing_events event
         where event.organization_id = p_organization_id
           and event.user_id = v_request.subject_id
         order by event.ts, event.id;
end;
$function$;

-- Identity anonymization is global by design, so it may run only when the user
-- is neither a member nor a billing owner anywhere. Company-specific retained
-- billing cleanup belongs to the deletion wrappers below, not this function.
create or replace function public.compliance_anonymize_unshared_user(
    p_user_id uuid
) returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
declare
    v_column text;
    v_placeholder_email text :=
        'deleted+' || p_user_id::text || '@deleted.invalid';
begin
    if exists (
        select 1 from public.organization_members member
         where member.user_id = p_user_id
    ) or exists (
        select 1 from public.organizations organization
         where organization.billing_owner_id = p_user_id
    ) then
        return false;
    end if;

    -- Optional legal_acceptances rows remain as minimized legal evidence. The
    -- UUID now resolves only to the non-login auth.users placeholder shell.
    if to_regclass('public.legal_acceptances') is not null then
        null;
    end if;
    if to_regclass('public.profiles') is not null
       and exists (
            select 1 from information_schema.columns
             where table_schema = 'public'
               and table_name = 'profiles'
               and column_name = 'id'
       ) then
        execute 'delete from public.profiles where id=$1' using p_user_id;
    end if;
    foreach v_column in array array[
        'confirmation_token','recovery_token','email_change_token_new',
        'email_change_token_current','phone_change_token','reauthentication_token',
        'email_change','phone_change'
    ] loop
        if exists (
            select 1 from information_schema.columns
             where table_schema = 'auth'
               and table_name = 'users'
               and column_name = v_column
        ) then
            execute pg_catalog.format(
                'update auth.users set %I='''' where id=$1', v_column
            ) using p_user_id;
        end if;
    end loop;
    if exists (
        select 1 from information_schema.columns
         where table_schema = 'auth' and table_name = 'users' and column_name = 'phone'
    ) then
        execute 'update auth.users set phone=null where id=$1' using p_user_id;
    end if;
    if exists (
        select 1 from information_schema.columns
         where table_schema = 'auth'
           and table_name = 'users'
           and column_name = 'encrypted_password'
    ) then
        execute 'update auth.users set encrypted_password=null where id=$1'
            using p_user_id;
    end if;
    if exists (
        select 1 from information_schema.columns
         where table_schema = 'auth'
           and table_name = 'users'
           and column_name = 'raw_app_meta_data'
    ) then
        execute 'update auth.users set raw_app_meta_data=''{"provider":"disabled","providers":[]}''::jsonb where id=$1'
            using p_user_id;
    end if;
    if exists (
        select 1 from information_schema.columns
         where table_schema = 'auth'
           and table_name = 'users'
           and column_name = 'banned_until'
    ) then
        execute 'update auth.users set banned_until=''infinity''::timestamptz where id=$1'
            using p_user_id;
    end if;
    if exists (
        select 1 from information_schema.columns
         where table_schema = 'auth'
           and table_name = 'users'
           and column_name = 'deleted_at'
    ) then
        execute 'update auth.users set deleted_at=coalesce(deleted_at,clock_timestamp()) where id=$1'
            using p_user_id;
    end if;
    execute 'update auth.users set email=$2, raw_user_meta_data=''{}''::jsonb where id=$1'
        using p_user_id, v_placeholder_email;
    if to_regclass('auth.sessions') is not null then
        execute 'delete from auth.sessions where user_id=$1' using p_user_id;
    end if;
    if to_regclass('auth.identities') is not null then
        execute 'delete from auth.identities where user_id=$1' using p_user_id;
    end if;
    if to_regclass('auth.mfa_factors') is not null then
        execute 'delete from auth.mfa_factors where user_id=$1' using p_user_id;
    end if;
    if to_regclass('auth.one_time_tokens') is not null then
        execute 'delete from auth.one_time_tokens where user_id=$1' using p_user_id;
    end if;
    return true;
end;
$function$;

revoke all on function public.compliance_anonymize_unshared_user(uuid)
    from public, anon, authenticated, service_role;

create or replace function public.compliance_delete_tenant(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns text
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
declare
    v_result text;
    v_identity_ids uuid[];
    v_identity_id uuid;
    v_billing_before bigint;
    v_billing_after bigint;
begin
    select coalesce(
        pg_catalog.array_agg(distinct identity_id), array[]::uuid[]
    ) into v_identity_ids
      from (
        select member.user_id as identity_id
          from public.organization_members member
         where member.organization_id = p_organization_id
        union all
        select organization.billing_owner_id
          from public.organizations organization
         where organization.id = p_organization_id
           and organization.billing_owner_id is not null
      ) identities;
    select count(*) into v_billing_before
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;

    v_result := public.compliance_delete_tenant_pre_company_identity(
        p_organization_id, p_request_id, p_actor_id
    );

    update public.billing_accounts account
       set checkout_session_id = null,
           updated_at = pg_catalog.clock_timestamp()
     where account.organization_id = p_organization_id;
    update public.billing_events event
       set session_id = ''
     where event.organization_id = p_organization_id;

    foreach v_identity_id in array v_identity_ids loop
        perform public.compliance_anonymize_unshared_user(v_identity_id);
    end loop;

    select count(*) into v_billing_after
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    if v_billing_after <> v_billing_before then
        raise exception 'company financial preservation invariant failed'
            using errcode = '55000';
    end if;
    return v_result;
end;
$function$;

create or replace function public.compliance_delete_subject(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns text
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
declare
    v_result text;
    v_request_scope text;
    v_subject_id uuid;
    v_billing_before bigint;
    v_billing_after bigint;
begin
    select request.request_scope, request.subject_id
      into v_request_scope, v_subject_id
      from public.data_subject_requests request
     where request.id = p_request_id
       and request.organization_id = p_organization_id;
    select count(*) into v_billing_before
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;

    v_result := public.compliance_delete_subject_pre_company_identity(
        p_organization_id, p_request_id, p_actor_id
    );

    if v_request_scope = 'member' then
        update public.billing_accounts account
           set checkout_session_id = null,
               updated_at = pg_catalog.clock_timestamp()
         where account.organization_id = p_organization_id
           and account.user_id = v_subject_id;
        update public.billing_events event
           set session_id = ''
         where event.organization_id = p_organization_id
           and event.user_id = v_subject_id;
        perform public.compliance_anonymize_unshared_user(v_subject_id);
    end if;

    select count(*) into v_billing_after
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    if v_billing_after <> v_billing_before then
        raise exception 'subject company financial preservation invariant failed'
            using errcode = '55000';
    end if;
    return v_result;
end;
$function$;

revoke all on function public.compliance_export_tenant(uuid,uuid,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_export_subject(uuid,uuid,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_delete_tenant(uuid,uuid,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_delete_subject(uuid,uuid,text)
    from public, anon, authenticated, service_role;
grant execute on function public.compliance_export_tenant(uuid,uuid,text)
    to service_role;
grant execute on function public.compliance_export_subject(uuid,uuid,text)
    to service_role;
grant execute on function public.compliance_delete_tenant(uuid,uuid,text)
    to service_role;
grant execute on function public.compliance_delete_subject(uuid,uuid,text)
    to service_role;

comment on column public.billing_events.organization_id is
    'Nullable exact-company identity for safely attributable legacy evidence; null means tenant-ambiguous and is not exportable.';
comment on function public.compliance_export_tenant(uuid,uuid,text) is
    'Tenant export with billing account, ledger, and attributable legacy event evidence scoped by organization_id.';
comment on function public.compliance_export_subject(uuid,uuid,text) is
    'Subject export preserving member relationships without crossing organization billing boundaries.';

commit;
