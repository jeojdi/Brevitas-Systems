-- Tenant erasure and portability must cover the cache-warming tables.
--
-- compliance_delete_tenant enumerates every tenant table it purges
-- (202607170007_compliance_workflows.sql:2385-2445) and then only RENAMES the
-- organization: `update public.organizations set name = 'Deleted organization',
-- ...` (:2444). public.warm_credentials was added afterwards by
-- 202607280001_cache_warming.sql:10 with `organization_id ... references
-- public.organizations(id) on delete cascade`, so its cascade can never fire,
-- and neither the frozen inner body nor the 202607200011 wrapper mentions it.
-- The result is that a terminated customer's KMS-encrypted OpenAI/Anthropic key
-- ciphertext, the UUID of the person who consented, and the consent timestamp
-- survive a deletion that scripts/dr/tenant-data.sh reports as 'completed' and
-- that docs/compliance/DATA_RIGHTS.md:28 describes as removing "credentials".
-- compliance_export_tenant has the mirror-image gap: it hard-codes its record
-- set and never enumerates warm_credentials, warm_prefixes or
-- warm_budget_ledger, so a portability request silently omits all three.
--
-- Both functions live in migrations that are SHA-256 pinned by
-- scripts/ci/migration-frozen-checksums.txt and already applied to a production
-- database with no migration ledger, so they cannot be edited in place. This
-- migration redefines only the OUTER wrappers introduced by
-- 202607200011_compliance_billing_isolation.sql (lines 117-199 and 415-471),
-- transcribed verbatim from that file with the warming statements inserted. A
-- plpgsql SECURITY DEFINER call shares the caller's transaction, so atomicity is
-- unchanged; `create or replace` preserves the argument list and the grants
-- 202607200011 installed. Nothing about the company-identity isolation those
-- wrappers added is altered: the pre-call v_identity_ids snapshot, the
-- billing_accounts/billing_events scrub, the anonymize loop and the
-- billing_ledger before/after invariant are all still here.
--
-- Forward-only and idempotent.

-- REVERSE: PITR-ONLY -- restoring the pre-erasure compliance wrappers would resurrect warm-state data subject evidence already erased

begin;

do $migration_precondition$
declare
    required_table text;
begin
    if to_regprocedure(
        'public.compliance_delete_tenant_pre_company_identity(uuid,uuid,text)'
    ) is null
       or to_regprocedure(
        'public.compliance_export_tenant_pre_company_identity(uuid,uuid,text)'
    ) is null
       or to_regprocedure(
        'public.compliance_export_subject_pre_company_identity(uuid,uuid,text)'
    ) is null then
        raise exception using
            errcode = '55000',
            message = '202607280016 requires the company-scoped compliance wrappers';
    end if;
    foreach required_table in array array[
        'warm_credentials', 'warm_prefixes', 'warm_budget_ledger'
    ] loop
        if to_regclass(format('public.%I', required_table)) is null then
            raise exception using
                errcode = '55000',
                message = '202607280016 requires public.' || required_table;
        end if;
    end loop;
end;
$migration_precondition$;

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

    -- Warming state was introduced by 202607280001, after the frozen inner
    -- body was written, so nothing in the chain deletes it. warm_credentials
    -- holds a KMS-encrypted third-party provider key plus the named consent
    -- actor and consent timestamp; it cascades only from public.organizations,
    -- and tenant deletion deliberately RENAMES that row instead of deleting it,
    -- so the cascade never fires. warm_prefixes cascades from public.customers
    -- (deleted by the inner body) and is repeated here as belt and braces.
    -- Both deletes are inside the caller's transaction, so the whole deletion
    -- still rolls back as one unit, and they re-run for an already-'completed'
    -- request because the inner body short-circuits before them.
    delete from public.warm_credentials credential
     where credential.organization_id = p_organization_id;
    delete from public.warm_prefixes prefix
     where prefix.organization_id = p_organization_id;
    -- public.warm_budget_ledger is intentionally NOT deleted. It is content-free
    -- (organization, provider, day, reserved/spent money) and it is the operand
    -- billing_period_settlement_evidence recomputes the warm deduction from
    -- (202607280008:344-350), so erasing it would silently raise the fee ceiling
    -- for a retained period. It ages out on its own retention horizon in
    -- public.purge_warm_state.

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

    -- Warming state, absent from the original record set because
    -- 202607280001 landed later. Ciphertext columns
    -- (warm_credentials.credential_ciphertext, warm_prefixes.payload_ciphertext)
    -- are reported as present but NOT emitted: the portable-export envelope
    -- decoder allowlists exactly three kind/purpose pairs
    -- (scripts/dr/portable-export.py:45-54) and hard-fails on any other, so
    -- emitting them as 'encrypted_content' today would break every export
    -- artifact. Adding the two purposes there is a prerequisite for exporting
    -- the ciphertext itself; until then the subject learns the record exists,
    -- which is what the omission previously hid.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_credential',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', credential.organization_id,
                'provider', credential.provider,
                'credential_configured', true,
                'credential_ciphertext_exported', false,
                'enabled', credential.enabled,
                'consent_actor_id', credential.consent_actor_id,
                'consent_at', credential.consent_at,
                'daily_budget_usd', credential.daily_budget_usd,
                'max_warm_customers', credential.max_warm_customers,
                'max_pings_per_customer_day', credential.max_pings_per_customer_day,
                'credential_state', credential.credential_state,
                'created_at', credential.created_at,
                'updated_at', credential.updated_at
            )
        )
          from public.warm_credentials credential
         where credential.organization_id = p_organization_id
         order by credential.provider;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_prefix',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', prefix.organization_id,
                'customer_id', prefix.customer_id,
                'provider', prefix.provider,
                'prefix_hash', prefix.prefix_hash,
                'payload_ciphertext_exported', false,
                'prefix_tokens', prefix.prefix_tokens,
                'provider_ttl_seconds', prefix.provider_ttl_seconds,
                'ping_reserve_usd', prefix.ping_reserve_usd,
                'arrival_count', prefix.arrival_count,
                'ewma_interarrival_s', prefix.ewma_interarrival_s,
                'hour_histogram', prefix.hour_histogram,
                'warm_pings', prefix.warm_pings,
                'warm_hits', prefix.warm_hits,
                'warm_misses', prefix.warm_misses,
                'consecutive_misses', prefix.consecutive_misses,
                'pings_today', prefix.pings_today,
                'pings_today_date', prefix.pings_today_date,
                'state', prefix.state,
                'created_at', prefix.created_at,
                'last_seen_at', prefix.last_seen_at,
                'next_due_at', prefix.next_due_at,
                'expires_at', prefix.expires_at
            )
        )
          from public.warm_prefixes prefix
         where prefix.organization_id = p_organization_id
         order by prefix.customer_id, prefix.provider, prefix.prefix_hash;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_budget_ledger',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', ledger.organization_id,
                'provider', ledger.provider,
                'day', ledger.day,
                'reserved_usd', ledger.reserved_usd,
                'spent_usd', ledger.spent_usd,
                'updated_at', ledger.updated_at
            )
        )
          from public.warm_budget_ledger ledger
         where ledger.organization_id = p_organization_id
         order by ledger.day, ledger.provider;
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
       or v_request.request_scope not in ('member', 'customer')
       or v_request.status <> 'processing' then
        return;
    end if;

    -- Preserve the original member-subject semantics: billing evidence is a
    -- subject relationship only when that member is the compatibility owner,
    -- while organization_id prevents evidence from another owned company.
    -- Billing evidence stays a member-only relationship, exactly as
    -- 202607200011 defined it; the guard above now also admits a
    -- customer-scoped request, which must not reach it.
    if v_request.request_scope = 'member' then
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
    end if;

    -- A customer-scoped subject request now also carries that customer's
    -- warming observations. warm_prefixes is keyed by (organization, customer),
    -- so the customer IS the subject here.
    if v_request.request_scope = 'customer' then
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_prefix',
                'data', pg_catalog.jsonb_build_object(
                    'organization_id', prefix.organization_id,
                    'customer_id', prefix.customer_id,
                    'provider', prefix.provider,
                    'prefix_hash', prefix.prefix_hash,
                    'payload_ciphertext_exported', false,
                    'prefix_tokens', prefix.prefix_tokens,
                    'provider_ttl_seconds', prefix.provider_ttl_seconds,
                    'arrival_count', prefix.arrival_count,
                    'ewma_interarrival_s', prefix.ewma_interarrival_s,
                    'hour_histogram', prefix.hour_histogram,
                    'warm_pings', prefix.warm_pings,
                    'warm_hits', prefix.warm_hits,
                    'warm_misses', prefix.warm_misses,
                    'state', prefix.state,
                    'created_at', prefix.created_at,
                    'last_seen_at', prefix.last_seen_at,
                    'next_due_at', prefix.next_due_at,
                    'expires_at', prefix.expires_at
                )
            )
              from public.warm_prefixes prefix
             where prefix.organization_id = p_organization_id
               and prefix.customer_id = v_request.subject_id
             order by prefix.provider, prefix.prefix_hash;
    end if;
end;
$function$;

-- create or replace preserves the privileges 202607200011 installed; restate
-- them so a hand-assembled project cannot end up with a wider surface.
revoke all on function public.compliance_export_tenant(uuid,uuid,text)
    from public, anon, authenticated;
revoke all on function public.compliance_export_subject(uuid,uuid,text)
    from public, anon, authenticated;
revoke all on function public.compliance_delete_tenant(uuid,uuid,text)
    from public, anon, authenticated;
grant execute on function public.compliance_export_tenant(uuid,uuid,text)
    to service_role;
grant execute on function public.compliance_export_subject(uuid,uuid,text)
    to service_role;
grant execute on function public.compliance_delete_tenant(uuid,uuid,text)
    to service_role;

comment on function public.compliance_delete_tenant(uuid,uuid,text) is
    'Tenant erasure including cache-warming credentials and prefixes; the content-free warm budget ledger is retained as settlement evidence and ages out on its own retention horizon.';
comment on function public.compliance_export_tenant(uuid,uuid,text) is
    'Tenant export including cache-warming credential, prefix, and budget-ledger records; ciphertext columns are reported as present and excluded until the portable-export envelope decoder supports their purposes.';

commit;
