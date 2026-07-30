-- Make bvx device credentials revocable through a customer-facing path.
--
-- DELETE /v1/keys/{key_id} is the only key-revocation route a customer has
-- (api/server.py:2546 -> store.revoke_organization_key ->
-- rpc/company_admin_revoke_dashboard_session_key), and that RPC refuses anything
-- that is not a browser session: `or v_key.key_type<>'dashboard_session'`
-- (202607170009_key_listing_security.sql:127) and again in its UPDATE (:150).
-- Its comment says it is "intentionally dashboard-session-specific". Meanwhile
-- the listing RPC in the same file returns device keys to owners and admins with
-- no key_type filter (:68-77), and the dashboard renders a Revoke button for
-- every row it lists. store.revoke_keys_by_type is explicitly disabled
-- (api/store.py:3846), and revoke_installation only stamps
-- installations.revoked_at -- it never touches api_keys. So a lost laptop's
-- device credential, scoped proxy:invoke + usage:write + customers:import +
-- repositories:register + installations:register, cannot be revoked by the
-- customer at all: the only code that revokes one is Brevitas-staff compliance
-- erasure (202607170007:2544) and the internal digest-mismatch quarantine.
--
-- This installs ONE security-definer dispatcher that locks the target row and
-- branches on its key_type, so there is one round trip, one audit event, and no
-- client-side type sniffing:
--
--   * dashboard_session keeps today's semantics EXACTLY -- owner/admin may revoke
--     any session key, member/billing_admin only one they created -- and keeps
--     emitting the pinned dashboard_session.* audit actions.
--   * device requires company_owner or company_admin. It emits distinct
--     device_key.* actions so the pinned dashboard_session.* assertions keep
--     their meaning.
--   * organization_service stays refused, pointing at the service-account
--     lifecycle RPCs that own it, exactly as the strict RPC does today
--     (pinned by scripts/ci/migration-key-audit-assertions.sql:334-341).
--
-- The strict RPC is deliberately left in place and untouched rather than widened:
-- widening it would have required changing BOTH key_type predicates, and changing
-- only the guard would make it return ok/revoked=true while updating zero rows --
-- a silent false success on a revocation path. public.company_admin_revoke_key
-- (uuid,uuid,uuid,text) is NOT re-created: migration 009 removed that callable
-- surface and the assertion suite hard-fails on its reappearance.
--
-- Callers that must follow, and are not changed here:
--   * api/store.py:3831 must point store.revoke_organization_key at this
--     dispatcher (the api/server.py:2548 status mapping still holds).
--   * api/company_admin.py:953-988 (the dev twin) still enforces
--     key_type='dashboard_session' while api/store.py:2412-2430 (SQLite) revokes
--     any type, so dev and prod already disagree; both should adopt these rules.
--   * dashboard/src/components/ApiKeys.jsx:158-165 sends no fingerprint, so the
--     Revoke button is inert in production regardless of the backend.
-- Revocation is eventually consistent within the <=30s auth caches
-- (api/server.py:1093-1104), which cache device contexts.
--
-- Forward-only and idempotent.

-- REVERSE: PITR-ONLY -- un-revoking device keys or restoring the pre-fence RPCs would resurrect revoked credentials

begin;

do $migration_precondition$
begin
    if to_regprocedure(
        'public.company_admin_revoke_dashboard_session_key(uuid,uuid,uuid,text)'
    ) is null
       or to_regprocedure('public.lock_company_admin_namespace(uuid)') is null
       or to_regprocedure('public.lock_company_actor_role(uuid,uuid)') is null then
        raise exception using
            errcode = '55000',
            message = '202607280019 requires the atomic key-audit RPC boundary';
    end if;
    if to_regprocedure('public.company_admin_revoke_key(uuid,uuid,uuid,text)')
       is not null then
        raise exception using
            errcode = '55000',
            message = '202607280019 refuses to run beside the retired generic key RPC';
    end if;
end;
$migration_precondition$;

create or replace function public.company_admin_revoke_tenant_key(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_key_id uuid,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_actor_role text;
    v_key public.api_keys%rowtype;
    v_audit_prefix text;
    v_allowed boolean;
begin
    perform public.lock_company_admin_namespace(p_organization_id);
    v_actor_role := public.lock_company_actor_role(
        p_organization_id,p_actor_user_id);

    select * into v_key
      from public.api_keys
     where organization_id=p_organization_id
       and id=p_key_id
     for update;

    -- The audit action namespace is chosen from the key type BEFORE authorization
    -- so a denial is attributable to the surface that was addressed. An unknown
    -- or missing key is reported under the session namespace, matching the
    -- existing strict RPC's behaviour for a not-found id.
    v_audit_prefix := case v_key.key_type
        when 'device' then 'device_key'
        when 'organization_service' then 'service_key'
        else 'dashboard_session'
    end;

    v_allowed := v_key.id is not null and v_actor_role is not null and case
        -- Unchanged from 202607170009: any company role may revoke a session
        -- key, but member/billing_admin only one they created themselves.
        when v_key.key_type = 'dashboard_session' then
            v_actor_role in (
                'company_owner','company_admin','member','billing_admin'
            ) and (
                v_actor_role in ('company_owner','company_admin')
                or v_key.created_by is not distinct from p_actor_user_id
            )
        -- Deprovisioning a machine credential is an administrative act: a
        -- member cannot revoke the device key another engineer is using.
        when v_key.key_type = 'device' then
            v_actor_role in ('company_owner','company_admin')
        -- Long-lived service credentials remain exclusively under the company
        -- service-account lifecycle RPCs.
        else false
    end;

    if not v_allowed then
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,
            coalesce(v_actor_role,'none'),p_request_id,
            v_audit_prefix||'.revoke.denied','api_key',
            p_key_id::text,'denied');
        return jsonb_build_object(
            'ok',false,
            'code',case
                when v_key.id is not null
                     and v_key.key_type = 'organization_service'
                then 'service_account_lifecycle_required'
                else 'forbidden_or_not_found'
            end
        );
    end if;

    if v_key.revoked_at is not null then
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
            v_audit_prefix||'.revoke.noop','api_key',
            p_key_id::text,'committed');
        return jsonb_build_object(
            'ok',true,'key_id',p_key_id,'key_type',v_key.key_type,
            'revoked',false,'already_revoked',true);
    end if;

    -- The row is already locked and its type authorized above, so the UPDATE
    -- re-states the identity only. It deliberately does not re-filter on
    -- key_type: a predicate that disagreed with the authorization branch is how
    -- a revocation reports success while updating nothing.
    update public.api_keys
       set revoked_at=now()
     where organization_id=p_organization_id
       and id=p_key_id;
    perform public.append_company_audit(
        p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
        v_audit_prefix||'.revoked','api_key',p_key_id::text,'committed');
    return jsonb_build_object(
        'ok',true,'key_id',p_key_id,'key_type',v_key.key_type,
        'revoked',true,'already_revoked',false);
end;
$$;
revoke all on function public.company_admin_revoke_tenant_key(
    uuid,uuid,uuid,text
) from public, anon, authenticated, service_role;
grant execute on function public.company_admin_revoke_tenant_key(
    uuid,uuid,uuid,text
) to service_role;

comment on function public.company_admin_revoke_tenant_key(uuid,uuid,uuid,text) is
    'Tenant key revocation dispatcher: dashboard_session keeps migration 009 semantics, device requires company_owner/company_admin, organization_service stays under the service-account lifecycle.';

commit;
