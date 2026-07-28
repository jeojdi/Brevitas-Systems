-- Onboarding evidence must be satisfiable by the released BVX topology. The
-- shipped CLI runs a LOCAL proxy that reports receipts over POST /v1/usage,
-- which the API records with authoritative=false by design (only the hosted
-- in-process proxy bridge writes authoritative=true). Migration 202607200016
-- gated proxied_request_observed on `usage.authoritative is true`, so the
-- released client could never complete onboarding. Re-issue both onboarding
-- RPCs accepting any receipt_source='proxy' row from the exact receipt-bound
-- device key. The full binding chain is unchanged: an unrevoked BVX
-- installation registered with a live device key, a committed
-- device_key.activated audit event, and usage from that same key hash at or
-- after installation. `authoritative` keeps its billing meaning everywhere
-- else; onboarding no longer reads it. Accepted tradeoff: /v1/usage callers
-- choose receipt_source, so a tenant holding its own bound device key can
-- self-report the evidence row. Onboarding completion gates only the tenant's
-- own dashboard experience (never billing, scopes, or another tenant), so
-- self-attestation by the key owner is within the threat model.

begin;

create or replace function public.organization_onboarding_status(
    p_actor_user_id uuid,
    p_organization_id uuid
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_role text;
    v_started_at timestamptz;
    v_completed_at timestamptz;
    v_cli_connected boolean := false;
    v_evidence_usage_id bigint;
begin
    select member.role
      into v_role
      from public.organization_members member
     where member.organization_id = p_organization_id
       and member.user_id = p_actor_user_id
       and member.status = 'active'
       and member.role in (
           'company_owner','company_admin','member','billing_admin'
       );
    if v_role is null then
        return jsonb_build_object('ok', false, 'code', 'forbidden');
    end if;

    select organization.onboarding_started_at,
           organization.onboarding_completed_at
      into v_started_at, v_completed_at
      from public.organizations organization
     where organization.id = p_organization_id;
    if v_started_at is null then
        return jsonb_build_object('ok', false, 'code', 'not_found');
    end if;

    select exists (
        select 1
          from public.installations installation
          join public.api_keys credential
            on credential.id = installation.registration_key_id
           and credential.key_hash = installation.registration_key_hash
           and credential.organization_id = installation.organization_id
           and credential.key_type = 'device'
           and credential.revoked_at is null
           and (credential.expires_at is null or credential.expires_at > now())
          join public.audit_events activation
            on activation.organization_id = installation.organization_id
           and activation.action = 'device_key.activated'
           and activation.target_type = 'api_key'
           and activation.target_id = credential.id::text
           and activation.outcome = 'committed'
         where installation.organization_id = p_organization_id
           and installation.revoked_at is null
           and installation.device_auth_receipt_id is not null
           and lower(installation.client_name) = 'bvx'
           and installation.bvx_version <> ''
           and installation.device_id is not null
           and installation.installed_at >= v_started_at
    ) into v_cli_connected;

    select usage.id
      into v_evidence_usage_id
      from public.usage_log usage
      join public.installations installation
        on installation.organization_id = usage.organization_id
       and installation.registration_key_hash = usage.key_hash
       and installation.revoked_at is null
       and installation.device_auth_receipt_id is not null
       and lower(installation.client_name) = 'bvx'
       and installation.bvx_version <> ''
       and installation.device_id is not null
       and installation.installed_at >= v_started_at
       and usage.ts >= installation.installed_at
      join public.api_keys credential
        on credential.id = installation.registration_key_id
       and credential.key_hash = installation.registration_key_hash
       and credential.organization_id = usage.organization_id
       and credential.key_type = 'device'
       and credential.revoked_at is null
       and (credential.expires_at is null or credential.expires_at > now())
      join public.audit_events activation
        on activation.organization_id = usage.organization_id
       and activation.action = 'device_key.activated'
       and activation.target_type = 'api_key'
       and activation.target_id = credential.id::text
       and activation.outcome = 'committed'
     where usage.organization_id = p_organization_id
       and usage.receipt_source = 'proxy'
     order by usage.ts, usage.id
     limit 1;

    return jsonb_build_object(
        'ok', true,
        'company_id', p_organization_id,
        'status', case when v_completed_at is null then 'pending' else 'complete' end,
        'cli_connected', v_cli_connected or v_completed_at is not null,
        'proxied_request_observed',
            v_evidence_usage_id is not null or v_completed_at is not null,
        'completed_at', coalesce(v_completed_at::text, '')
    );
end;
$$;
revoke all on function public.organization_onboarding_status(uuid,uuid)
    from public, anon, authenticated, service_role;
grant execute on function public.organization_onboarding_status(uuid,uuid)
    to service_role;

create or replace function public.complete_organization_onboarding(
    p_actor_user_id uuid,
    p_organization_id uuid,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_role text;
    v_started_at timestamptz;
    v_completed_at timestamptz;
    v_cli_connected boolean := false;
    v_evidence_usage_id bigint;
begin
    if p_request_id !~ '^[A-Za-z0-9._:-]{8,128}$' then
        return jsonb_build_object('ok', false, 'code', 'invalid_request');
    end if;

    perform pg_advisory_xact_lock(hashtextextended(p_organization_id::text, 0));
    select member.role
      into v_role
      from public.organization_members member
     where member.organization_id = p_organization_id
       and member.user_id = p_actor_user_id
       and member.status = 'active'
       and member.role = 'company_owner'
     for update;
    if v_role is null then
        return jsonb_build_object('ok', false, 'code', 'forbidden');
    end if;

    select organization.onboarding_started_at,
           organization.onboarding_completed_at
      into v_started_at, v_completed_at
      from public.organizations organization
     where organization.id = p_organization_id
     for update;
    if v_started_at is null then
        return jsonb_build_object('ok', false, 'code', 'not_found');
    end if;
    if v_completed_at is not null then
        return jsonb_build_object(
            'ok', true, 'company_id', p_organization_id,
            'status', 'complete', 'cli_connected', true,
            'proxied_request_observed', true,
            'completed_at', v_completed_at::text
        );
    end if;

    select exists (
        select 1
          from public.installations installation
          join public.api_keys credential
            on credential.id = installation.registration_key_id
           and credential.key_hash = installation.registration_key_hash
           and credential.organization_id = installation.organization_id
           and credential.key_type = 'device'
           and credential.revoked_at is null
           and (credential.expires_at is null or credential.expires_at > now())
          join public.audit_events activation
            on activation.organization_id = installation.organization_id
           and activation.action = 'device_key.activated'
           and activation.target_type = 'api_key'
           and activation.target_id = credential.id::text
           and activation.outcome = 'committed'
         where installation.organization_id = p_organization_id
           and installation.revoked_at is null
           and installation.device_auth_receipt_id is not null
           and lower(installation.client_name) = 'bvx'
           and installation.bvx_version <> ''
           and installation.device_id is not null
           and installation.installed_at >= v_started_at
    ) into v_cli_connected;

    select usage.id
      into v_evidence_usage_id
      from public.usage_log usage
      join public.installations installation
        on installation.organization_id = usage.organization_id
       and installation.registration_key_hash = usage.key_hash
       and installation.revoked_at is null
       and installation.device_auth_receipt_id is not null
       and lower(installation.client_name) = 'bvx'
       and installation.bvx_version <> ''
       and installation.device_id is not null
       and installation.installed_at >= v_started_at
       and usage.ts >= installation.installed_at
      join public.api_keys credential
        on credential.id = installation.registration_key_id
       and credential.key_hash = installation.registration_key_hash
       and credential.organization_id = usage.organization_id
       and credential.key_type = 'device'
       and credential.revoked_at is null
       and (credential.expires_at is null or credential.expires_at > now())
      join public.audit_events activation
        on activation.organization_id = usage.organization_id
       and activation.action = 'device_key.activated'
       and activation.target_type = 'api_key'
       and activation.target_id = credential.id::text
       and activation.outcome = 'committed'
     where usage.organization_id = p_organization_id
       and usage.receipt_source = 'proxy'
     order by usage.ts, usage.id
     limit 1;

    if v_evidence_usage_id is null then
        return jsonb_build_object(
            'ok', true, 'company_id', p_organization_id,
            'status', 'pending', 'cli_connected', v_cli_connected,
            'proxied_request_observed', false, 'completed_at', ''
        );
    end if;

    v_completed_at := now();
    update public.organizations
       set onboarding_completed_at = v_completed_at,
           onboarding_completed_by = p_actor_user_id,
           onboarding_evidence_usage_id = v_evidence_usage_id
     where id = p_organization_id;
    perform public.append_company_audit(
        p_organization_id, p_actor_user_id::text, v_role, p_request_id,
        'organization.onboarding.completed', 'company',
        p_organization_id::text, 'committed'
    );

    return jsonb_build_object(
        'ok', true, 'company_id', p_organization_id,
        'status', 'complete', 'cli_connected', true,
        'proxied_request_observed', true,
        'completed_at', v_completed_at::text
    );
end;
$$;
revoke all on function public.complete_organization_onboarding(uuid,uuid,text)
    from public, anon, authenticated, service_role;
grant execute on function public.complete_organization_onboarding(uuid,uuid,text)
    to service_role;

commit;

-- Rollback (manual): reapply supabase/migrations/202607200016_durable_onboarding.sql
-- from the `create or replace function public.organization_onboarding_status`
-- statement onward to restore the authoritative-only evidence predicate.
