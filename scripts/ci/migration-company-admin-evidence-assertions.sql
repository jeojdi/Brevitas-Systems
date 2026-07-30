\set ON_ERROR_STOP on

-- Behavioural contract for 202607280019_tenant_device_key_revocation.sql and
-- 202607280022_audit_read_and_transition_evidence.sql. All fixture state is
-- discarded by the trailing rollback, so this file is re-runnable and
-- order-independent.
begin;

insert into auth.users (id, email) values
    ('7d000000-0000-4000-8000-000000000001', 'tenant-key-owner@example.invalid'),
    ('7d000000-0000-4000-8000-000000000002', 'tenant-key-member@example.invalid');
insert into public.organizations (id, name, billing_owner_id) values (
    '7d100000-0000-4000-8000-000000000001', 'Tenant key revocation fixture',
    '7d000000-0000-4000-8000-000000000001');
insert into public.organization_members (organization_id, user_id, role, status)
values
    ('7d100000-0000-4000-8000-000000000001',
     '7d000000-0000-4000-8000-000000000001', 'company_owner', 'active'),
    ('7d100000-0000-4000-8000-000000000001',
     '7d000000-0000-4000-8000-000000000002', 'member', 'active');
insert into public.service_accounts (
    id, organization_id, name, environment, scopes, created_by, expires_at
) values (
    '7d200000-0000-4000-8000-000000000001',
    '7d100000-0000-4000-8000-000000000001', 'fixture service account',
    'production', array['proxy:invoke']::text[],
    '7d000000-0000-4000-8000-000000000001', now() + interval '30 days');
insert into public.api_keys (
    id, key_hash, name, owner_id, organization_id, service_account_id,
    key_type, scopes, environment, key_prefix, created_by
) values
    ('7d300000-0000-4000-8000-000000000001', repeat('7d', 32), 'device key',
     '7d000000-0000-4000-8000-000000000001',
     '7d100000-0000-4000-8000-000000000001', null, 'device',
     array['proxy:invoke','usage:write']::text[], 'production', 'bvt_dev0001',
     '7d000000-0000-4000-8000-000000000002'),
    ('7d300000-0000-4000-8000-000000000002', repeat('7e', 32), 'session key',
     '7d000000-0000-4000-8000-000000000001',
     '7d100000-0000-4000-8000-000000000001', null, 'dashboard_session',
     array['usage:read_own']::text[], 'production', 'bvt_ses0001',
     '7d000000-0000-4000-8000-000000000001'),
    ('7d300000-0000-4000-8000-000000000003', repeat('7f', 32), 'service key',
     '7d000000-0000-4000-8000-000000000001',
     '7d100000-0000-4000-8000-000000000001',
     '7d200000-0000-4000-8000-000000000001', 'organization_service',
     array['proxy:invoke']::text[], 'production', 'bvt_svc0001',
     '7d000000-0000-4000-8000-000000000001');

do $$
declare
    v_org uuid := '7d100000-0000-4000-8000-000000000001';
    v_owner uuid := '7d000000-0000-4000-8000-000000000001';
    v_member uuid := '7d000000-0000-4000-8000-000000000002';
    v_device_key uuid := '7d300000-0000-4000-8000-000000000001';
    v_session_key uuid := '7d300000-0000-4000-8000-000000000002';
    v_service_key uuid := '7d300000-0000-4000-8000-000000000003';
    v_result jsonb;
begin
    if to_regprocedure(
        'public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)'
    ) is null then
        raise exception 'the tenant key revocation dispatcher is missing';
    end if;
    -- Migration 009 removed the generic revocation surface and the suite
    -- hard-fails on its return; the dispatcher must not have re-created it.
    if to_regprocedure('public.company_admin_revoke_key(uuid,uuid,uuid,text)')
       is not null then
        raise exception 'the retired generic key revocation RPC reappeared';
    end if;
    if has_function_privilege(
        'anon', 'public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)',
        'EXECUTE'
    ) or has_function_privilege(
        'authenticated',
        'public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)', 'EXECUTE'
    ) or not has_function_privilege(
        'service_role',
        'public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text)', 'EXECUTE'
    ) then
        raise exception 'the tenant key revocation dispatcher is not service-only';
    end if;

    -- A member cannot deprovision another engineer's machine credential.
    v_result := public.company_admin_revoke_tenant_key(
        v_org, v_member, v_device_key, 'assert-device-member-denied');
    if coalesce((v_result->>'ok')::boolean, false)
       or v_result->>'code' <> 'forbidden_or_not_found'
       or (select credential.revoked_at from public.api_keys credential
            where credential.id = v_device_key) is not null then
        raise exception 'a member revoked a device key: %', v_result;
    end if;

    -- THE FIX: an owner can. Before 202607280019 there was no path at all.
    v_result := public.company_admin_revoke_tenant_key(
        v_org, v_owner, v_device_key, 'assert-device-owner-revoked');
    if not coalesce((v_result->>'ok')::boolean, false)
       or not coalesce((v_result->>'revoked')::boolean, false)
       or v_result->>'key_type' <> 'device'
       or (select credential.revoked_at from public.api_keys credential
            where credential.id = v_device_key) is null then
        raise exception 'an owner could not revoke a device key: %', v_result;
    end if;
    if not exists (
        select 1 from public.audit_events event
         where event.organization_id = v_org
           and event.action = 'device_key.revoked'
           and event.target_id = v_device_key::text
           and event.outcome = 'committed'
    ) or not exists (
        select 1 from public.audit_events event
         where event.organization_id = v_org
           and event.action = 'device_key.revoke.denied'
           and event.outcome = 'denied'
    ) then
        raise exception 'device key revocation left no distinct audit evidence';
    end if;

    -- Retrying is an audited no-op, not a second revocation.
    v_result := public.company_admin_revoke_tenant_key(
        v_org, v_owner, v_device_key, 'assert-device-owner-retry');
    if not coalesce((v_result->>'ok')::boolean, false)
       or coalesce((v_result->>'revoked')::boolean, true)
       or not coalesce((v_result->>'already_revoked')::boolean, false)
       or not exists (
            select 1 from public.audit_events event
             where event.organization_id = v_org
               and event.action = 'device_key.revoke.noop'
       ) then
        raise exception 'repeating a device key revocation is not an audited no-op';
    end if;

    -- Session keys keep migration 009's exact semantics through the dispatcher.
    v_result := public.company_admin_revoke_tenant_key(
        v_org, v_member, v_session_key, 'assert-session-member-denied');
    if coalesce((v_result->>'ok')::boolean, false)
       or (select credential.revoked_at from public.api_keys credential
            where credential.id = v_session_key) is not null then
        raise exception
            'a member revoked a session key it did not create: %', v_result;
    end if;
    v_result := public.company_admin_revoke_tenant_key(
        v_org, v_owner, v_session_key, 'assert-session-owner-revoked');
    if not coalesce((v_result->>'revoked')::boolean, false)
       or v_result->>'key_type' <> 'dashboard_session'
       or not exists (
            select 1 from public.audit_events event
             where event.organization_id = v_org
               and event.action = 'dashboard_session.revoked'
               and event.target_id = v_session_key::text
       ) then
        raise exception 'the dispatcher changed session key semantics: %', v_result;
    end if;

    -- Service credentials stay under the service-account lifecycle.
    v_result := public.company_admin_revoke_tenant_key(
        v_org, v_owner, v_service_key, 'assert-service-key-denied');
    if coalesce((v_result->>'ok')::boolean, false)
       or v_result->>'code' <> 'service_account_lifecycle_required'
       or (select credential.revoked_at from public.api_keys credential
            where credential.id = v_service_key) is not null then
        raise exception
            'the dispatcher addressed an organization_service key: %', v_result;
    end if;
    -- The strict RPC must still refuse it too.
    v_result := public.company_admin_revoke_dashboard_session_key(
        v_org, v_owner, v_service_key, 'assert-strict-service-key-denied');
    if coalesce((v_result->>'ok')::boolean, false) then
        raise exception 'the strict revoke RPC lost its service-key refusal';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Audit evidence: successful reads are recorded once, transitions are legible.
-- ---------------------------------------------------------------------------
do $$
declare
    v_org uuid := '7d100000-0000-4000-8000-000000000001';
    v_owner uuid := '7d000000-0000-4000-8000-000000000001';
    v_member uuid := '7d000000-0000-4000-8000-000000000002';
    v_page jsonb;
    v_result jsonb;
begin
    -- One request, three pages: exactly one read event, and the page itself must
    -- never contain a '%.read' row or a scripted walk would inflate an
    -- append-only 400-day table with evidence of itself.
    v_page := public.company_admin_audit_page(
        v_org, v_owner, null, null, 100, 'assert-audit-read-request');
    if not coalesce((v_page->>'ok')::boolean, false) then
        raise exception 'an owner could not read the audit log: %', v_page;
    end if;
    perform public.company_admin_audit_page(
        v_org, v_owner, null, null, 100, 'assert-audit-read-request');
    perform public.company_admin_audit_page(
        v_org, v_owner, null, null, 100, 'assert-audit-read-request');
    if (select count(*) from public.audit_events event
         where event.organization_id = v_org
           and event.action = 'audit.read'
           and event.request_id = 'assert-audit-read-request') <> 1 then
        raise exception 'successful audit reads are not recorded exactly once';
    end if;
    v_page := public.company_admin_audit_page(
        v_org, v_owner, null, null, 100, 'assert-audit-read-second-request');
    if (select count(*) from public.audit_events event
         where event.organization_id = v_org
           and event.action = 'audit.read') <> 2 then
        raise exception 'a second request did not record its own read evidence';
    end if;
    if exists (
        select 1 from jsonb_array_elements(v_page->'items') item
         where item->>'action' like '%.read'
    ) then
        raise exception 'the audit page returns its own read events';
    end if;
    -- Denials were always recorded and must stay visible in the page.
    v_page := public.company_admin_audit_page(
        v_org, v_member, null, null, 100, 'assert-audit-read-denied');
    if coalesce((v_page->>'ok')::boolean, true)
       or not exists (
            select 1 from public.audit_events event
             where event.organization_id = v_org
               and event.action = 'audit.read.denied'
       ) then
        raise exception 'audit read denial evidence regressed: %', v_page;
    end if;
    v_page := public.company_admin_audit_page(
        v_org, v_owner, null, null, 100, 'assert-audit-read-denial-visible');
    if not exists (
        select 1 from jsonb_array_elements(v_page->'items') item
         where item->>'action' = 'audit.read.denied'
    ) then
        raise exception 'read denials were filtered out of the audit page';
    end if;

    -- A permission change must record what it changed, not just that it changed.
    v_result := public.company_admin_set_member(
        v_org, v_owner, v_member, 'company_admin', 'active',
        'assert-member-transition');
    if not coalesce((v_result->>'ok')::boolean, false)
       or not exists (
            select 1 from public.audit_events event
             where event.organization_id = v_org
               and event.action = 'member.changed'
               and event.request_id = 'assert-member-transition'
       )
       or not exists (
            select 1 from public.audit_events event
             where event.organization_id = v_org
               and event.action = 'member.role_changed.member_to_company_admin'
               and event.target_id = v_member::text
               and event.outcome = 'committed'
       ) then
        raise exception 'a role change recorded no before/after evidence: %',
            v_result;
    end if;
    v_result := public.company_admin_set_member(
        v_org, v_owner, v_member, 'company_admin', 'disabled',
        'assert-member-status-transition');
    if not exists (
        select 1 from public.audit_events event
         where event.organization_id = v_org
           and event.action = 'member.status_changed.active_to_disabled'
           and event.target_id = v_member::text
    ) or exists (
        select 1 from public.audit_events event
         where event.organization_id = v_org
           and event.request_id = 'assert-member-status-transition'
           and event.action like 'member.role_changed.%'
    ) then
        raise exception
            'status transitions are not recorded, or an unchanged role was: %',
            v_result;
    end if;

    -- The granted authority is the one fact service_account.created omitted.
    v_result := public.company_admin_create_service_account(
        v_org, v_owner, '7d200000-0000-4000-8000-000000000002',
        'scope evidence account', 'production',
        array['proxy:invoke','jobs:read']::text[], repeat('1b', 32),
        'bvt_scope01', now() + interval '30 days',
        'assert-service-account-scopes');
    if not coalesce((v_result->>'ok')::boolean, false) then
        raise exception 'the service account fixture was refused: %', v_result;
    end if;
    if (select count(*) from public.audit_events event
         where event.organization_id = v_org
           and event.action = 'service_account.scope_granted'
           and event.request_id = 'assert-service-account-scopes') <> 2
       or not exists (
            select 1 from public.audit_events event
             where event.action = 'service_account.scope_granted'
               and event.target_id
                   = '7d200000-0000-4000-8000-000000000002:proxy:invoke'
       ) then
        raise exception 'granted service-account scopes are still unrecorded';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Membership-ending transitions revoke the target's own credentials
-- (202607280022): a departed engineer's `bvx login` device key authenticates
-- with no live-membership read, so company_admin_set_member must revoke it in
-- the same transaction — including hosted device rows whose created_by is NULL,
-- which are matched through their 'device_key.activated' activation event.
-- ---------------------------------------------------------------------------
do $$
declare
    v_org uuid := '7d100000-0000-4000-8000-000000000001';
    v_owner uuid := '7d000000-0000-4000-8000-000000000001';
    v_member uuid := '7d000000-0000-4000-8000-000000000002';
    v_departed_device uuid := '7d300000-0000-4000-8000-000000000011';
    v_departed_session uuid := '7d300000-0000-4000-8000-000000000012';
    v_survivor_device uuid := '7d300000-0000-4000-8000-000000000013';
    v_result jsonb;
begin
    -- The member is currently company_admin/disabled from the transition
    -- assertions above; re-activate so this block owns its whole scenario.
    v_result := public.company_admin_set_member(
        v_org, v_owner, v_member, 'member', 'active',
        'assert-member-reactivate-for-revoke');
    if not coalesce((v_result->>'ok')::boolean, false) then
        raise exception 'could not re-activate the fixture member: %', v_result;
    end if;
    -- Hosted-shaped device row: created_by NULL, holder recorded only by the
    -- append-only activation event (consume_bvx_device_idempotent inserts no
    -- created_by), plus the member's own dashboard session and an unrelated
    -- admin device credential that must survive.
    insert into public.api_keys (
        id, key_hash, name, owner_id, organization_id, service_account_id,
        key_type, scopes, environment, key_prefix, created_by
    ) values
        (v_departed_device, repeat('8a', 32), 'departed device key',
         v_owner::text, v_org, null, 'device',
         array['proxy:invoke','usage:write']::text[], 'production',
         'bvt_dev0011', null),
        (v_departed_session, repeat('8b', 32), 'departed session key',
         v_member::text, v_org, null, 'dashboard_session',
         array['usage:read_own']::text[], 'production', 'bvt_ses0012',
         v_member),
        (v_survivor_device, repeat('8c', 32), 'surviving device key',
         v_owner::text, v_org, null, 'device',
         array['proxy:invoke','usage:write']::text[], 'production',
         'bvt_dev0013', v_owner);
    perform public.append_company_audit(
        v_org, v_member::text, 'member', 'assert-departed-device-activation',
        'device_key.activated', 'api_key', v_departed_device::text,
        'committed');

    v_result := public.company_admin_set_member(
        v_org, v_owner, v_member, 'member', 'removed',
        'assert-member-removal-revokes');
    if not coalesce((v_result->>'ok')::boolean, false) then
        raise exception 'the removal transition was refused: %', v_result;
    end if;
    if (select credential.revoked_at from public.api_keys credential
         where credential.id = v_departed_device) is null then
        raise exception
            'a removed member''s created_by-less device key survived removal';
    end if;
    if (select credential.revoked_at from public.api_keys credential
         where credential.id = v_departed_session) is null then
        raise exception 'a removed member''s dashboard session survived removal';
    end if;
    if (select credential.revoked_at from public.api_keys credential
         where credential.id = v_survivor_device) is not null then
        raise exception 'removal revoked an unrelated member''s device key';
    end if;
    if not exists (
        select 1 from public.audit_events event
         where event.organization_id = v_org
           and event.action = 'member.credentials_revoked'
           and event.target_id = v_member::text
           and event.request_id = 'assert-member-removal-revokes'
           and event.outcome = 'committed'
    ) then
        raise exception 'credential revocation on removal left no audit evidence';
    end if;
    -- Role-only changes must NOT revoke: the earlier pure role transition in
    -- this suite ('assert-member-transition') may not have shed credentials.
    if exists (
        select 1 from public.audit_events event
         where event.organization_id = v_org
           and event.action = 'member.credentials_revoked'
           and event.request_id = 'assert-member-transition'
    ) then
        raise exception 'a role-only change revoked the member''s credentials';
    end if;
end;
$$;

-- The read-evidence dedupe must be enforced by the database, not by the RPC.
do $$
declare
    v_raised boolean := false;
begin
    if not exists (
        select 1 from pg_class relation
         where relation.relname = 'audit_events_read_request_idx'
           and relation.relkind = 'i'
    ) then
        raise exception 'the audit read dedupe index is missing';
    end if;
    begin
        insert into public.audit_events(
            organization_id, actor_id, actor_role, request_id,
            action, target_type, target_id, outcome, details
        ) values (
            '7d100000-0000-4000-8000-000000000001', 'system', 'system',
            'assert-audit-read-request', 'audit.read', 'company',
            '7d100000-0000-4000-8000-000000000001', 'committed', '{}'::jsonb
        );
    exception when unique_violation then v_raised := true;
    end;
    if not v_raised then
        raise exception 'a duplicate read event was accepted';
    end if;
end;
$$;

rollback;
