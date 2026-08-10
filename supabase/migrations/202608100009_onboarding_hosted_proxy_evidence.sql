-- A HOSTED CUSTOMER COULD NEVER FINISH ONBOARDING. The evidence predicate that
-- 202607280004 re-issued and 202607280005 finally made satisfiable describes ONE
-- topology and only one: a laptop that ran `bvx login`, so there is a
-- public.installations row bound to a key with key_type='device' and
-- client_name='bvx'. A hosted customer has neither. They are handed an
-- organization_service key, they point their SDK at the hosted proxy, and their
-- traffic arrives through the in-process bridge that writes authoritative
-- receipts. No device key is ever minted, no installation row is ever written,
-- and both onboarding RPCs therefore return cli_connected=false and
-- proxied_request_observed=false forever -- while the customer's money-producing
-- traffic is already flowing. The dashboard's first screen tells a paying,
-- fully-integrated customer to go install a CLI they do not need.
--
-- THE SECOND LANE, AND WHY authoritative IS THE RIGHT KEY. This migration adds
-- an alternative that stands on its own: any public.usage_log row for the
-- organization with receipt_source='proxy' AND authoritative=true, at or after
-- onboarding started. That flag is not a tenant-supplied field. POST /v1/usage
-- -- the transport a local BVX proxy and any customer with a valid key can call
-- -- is hard-wired to authoritative=false (api/server.py's report_usage), and
-- the ONLY writer of authoritative=true is _hosted_proxy_receipt, the in-process
-- bridge that runs after Brevitas itself proxied the call. So the hosted lane is
-- unforgeable by the tenant in a way the device lane is not, and it needs no
-- installation/audit chain to be trustworthy: the row's existence IS the proof
-- that Brevitas served the request.
--
-- This is a WIDENING and nothing else. Both lanes are OR-ed; the 202607280004
-- device predicate is carried forward character-for-character, so every
-- workspace that could complete onboarding before still completes it by exactly
-- the same evidence. The device lane is evaluated first and, when it produces a
-- row, remains the recorded onboarding_evidence_usage_id -- an existing
-- workspace's stored evidence pointer does not move because this migration ran.
--
-- WHAT THIS DOES NOT TOUCH. Onboarding completion gates the tenant's own
-- dashboard experience and nothing else: not billing, not scopes, not another
-- tenant's data. `authoritative` keeps its billing meaning everywhere it is
-- read for money; this migration only reads it. No fee, no settlement, no
-- ceiling and no catalog is consulted or written here.
--
-- NO NEW TENANT SURFACE, SO NO COMPLIANCE WIRING. This migration creates no
-- table, no column and no tenant-scoped datum: it replaces two SECURITY DEFINER
-- functions and adds one partial index over public.usage_log, whose erasure,
-- export and retention are already owned by the compliance routines that own
-- usage_log itself. There is consequently nothing to add to
-- compliance_delete_tenant, compliance_delete_subject, compliance_export_tenant,
-- compliance_export_subject or compliance_run_retention, and nothing that needs
-- an RLS policy: the index inherits usage_log's own zero-policy RLS posture, and
-- both functions keep 202607280004's revoke-from-everyone /
-- grant-to-service_role posture verbatim.
--
-- Forward-only and idempotent.

-- REVERSE: DDL: drop index if exists public.usage_log_org_authoritative_proxy_idx; re-apply 202607280004_onboarding_local_proxy_evidence.sql's public.organization_onboarding_status(uuid,uuid) and public.complete_organization_onboarding(uuid,uuid,text) verbatim with their revoke/grant. PITR-ONLY: none -- this migration writes no rows, so no data state needs restoring; workspaces whose onboarding_completed_at was set by the hosted lane keep a valid completion (the evidence row still exists) and reverting only stops FUTURE hosted completions.

begin;

do $migration_precondition$
begin
    -- The two functions this migration replaces. Naming them turns "the widened
    -- predicate silently created a function that never existed" into a refusal:
    -- a deployment missing 202607280004 must not receive the OR-ed form as its
    -- first definition, because then the device lane it carries forward would
    -- never have been reviewed against the schema it is running on.
    if to_regprocedure('public.organization_onboarding_status(uuid,uuid)') is null
       or to_regprocedure(
           'public.complete_organization_onboarding(uuid,uuid,text)') is null then
        raise exception using
            errcode = '55000',
            message = '202608100009 requires 202607280004 to be applied';
    end if;
    -- Every column both lanes read, by name. usage_log.authoritative is the
    -- whole basis of the new lane (202607170001) and receipt_source is what
    -- separates a proxied receipt from an SDK report (20260710_cloud_usage); a
    -- deployment without either would install a lane that is silently always
    -- false, which is indistinguishable from the bug this migration fixes.
    if not exists (
        select 1 from pg_catalog.pg_attribute attribute
         where attribute.attrelid = 'public.usage_log'::regclass
           and attribute.attname = 'authoritative'
           and not attribute.attisdropped
    ) or not exists (
        select 1 from pg_catalog.pg_attribute attribute
         where attribute.attrelid = 'public.usage_log'::regclass
           and attribute.attname = 'receipt_source'
           and not attribute.attisdropped
    ) then
        raise exception using
            errcode = '55000',
            message = '202608100009 requires usage_log.authoritative and .receipt_source';
    end if;
    if to_regclass('public.installations') is null
       or to_regclass('public.organizations') is null
       or to_regclass('public.organization_members') is null then
        raise exception using
            errcode = '55000',
            message = '202608100009 requires the enterprise tenancy tables';
    end if;
end;
$migration_precondition$;

-- The hosted lane's only access path. Without it, an organization with millions
-- of client-reported rows and no authoritative ones pays a full org partition
-- scan on every dashboard poll to learn "no". Partial, so it indexes only the
-- rows the lane can ever match, and ascending on (ts, id) because the lane wants
-- the EARLIEST qualifying receipt -- the same ordering both RPCs use.
create index if not exists usage_log_org_authoritative_proxy_idx
    on public.usage_log (organization_id, ts, id)
 where authoritative and receipt_source = 'proxy';

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
    v_hosted_evidence_usage_id bigint;
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

    -- LANE 2, evaluated first because it is one index probe against a partial
    -- index while lane 1 is a four-table join. A hosted organization has no
    -- installation to find, so asking the cheap question first is also asking
    -- the likely one first.
    select usage.id
      into v_hosted_evidence_usage_id
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and usage.authoritative
       and usage.receipt_source = 'proxy'
       and usage.ts >= v_started_at
     order by usage.ts, usage.id
     limit 1;

    -- LANE 1, carried forward verbatim from 202607280004.
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

    -- The device lane's row wins when it exists, so a workspace that already had
    -- evidence keeps naming the same receipt.
    v_evidence_usage_id := coalesce(v_evidence_usage_id, v_hosted_evidence_usage_id);
    v_cli_connected := v_cli_connected or v_hosted_evidence_usage_id is not null;

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
    v_hosted_evidence_usage_id bigint;
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

    select usage.id
      into v_hosted_evidence_usage_id
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and usage.authoritative
       and usage.receipt_source = 'proxy'
       and usage.ts >= v_started_at
     order by usage.ts, usage.id
     limit 1;

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

    v_evidence_usage_id := coalesce(v_evidence_usage_id, v_hosted_evidence_usage_id);
    v_cli_connected := v_cli_connected or v_hosted_evidence_usage_id is not null;

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
