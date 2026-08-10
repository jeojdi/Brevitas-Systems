\set ON_ERROR_STOP on

-- Migration 202608100009: a HOSTED organization can finish onboarding.
--
-- The topology under test is the one 202607280004/202607280005 cannot describe:
-- an organization whose only credential is an organization_service key, with no
-- device key, no bvx installation and no device_key.activated audit event. The
-- whole point of the second lane is that it stands alone, so this fixture
-- deliberately creates none of the device-lane furniture -- if any assertion
-- below passes because of a device binding, the fixture is wrong.
--
-- Three things are proved, in order: that a hosted org starts with no evidence;
-- that CLIENT-REPORTED proxy traffic (authoritative=false, what POST /v1/usage
-- writes and what any tenant holding a valid key can send) is still NOT
-- evidence; and that an AUTHORITATIVE proxy receipt -- which only the in-process
-- hosted bridge can write -- both lights up cli_connected and completes
-- onboarding against its own usage id.
--
-- Runs in its own transaction and ends in ROLLBACK, so it is rerunnable, leaves
-- no organization or usage row behind, and is order-independent with respect to
-- every other suite.

begin;

do $$
declare
    v_company_id constant uuid := '97000000-0000-4000-8000-000000000009';
    v_owner_id constant uuid := 'edededed-eded-4ded-8ded-edededededed';
    v_key_id constant uuid := '98000000-0000-4000-8000-000000000009';
    v_service_account_id constant uuid := '9b000000-0000-4000-8000-000000000009';
    v_key_hash constant text := lpad('49', 64, '0');
    v_status jsonb;
    v_usage_id bigint;
    v_client_usage_id bigint;
begin
    -- Guard against a frozen replay of 202607280004 surviving as the end state:
    -- both terminal definitions must carry the hosted lane.
    if position('usage.authoritative' in pg_get_functiondef(
           to_regprocedure('public.organization_onboarding_status(uuid,uuid)'))) = 0
       or position('usage.authoritative' in pg_get_functiondef(
           to_regprocedure('public.complete_organization_onboarding(uuid,uuid,text)'))) = 0 then
        raise exception 'onboarding functions are missing the hosted-proxy evidence lane';
    end if;
    -- The lane's only access path. Without it the lane is correct and slow,
    -- which on a dashboard poll over a large organization is its own defect.
    if to_regclass('public.usage_log_org_authoritative_proxy_idx') is null then
        raise exception 'hosted-proxy evidence index is missing';
    end if;

    insert into auth.users(id, email) values
        (v_owner_id, 'onboarding-hosted-proxy-owner@example.invalid')
    on conflict (id) do nothing;
    insert into public.organizations(
        id, name, legacy_owner_id, billing_owner_id
    ) values (
        v_company_id, 'Hosted onboarding fixture',
        'onboarding-hosted-proxy-fixture', v_owner_id
    );
    insert into public.organization_members(
        organization_id, user_id, role, status
    ) values (
        v_company_id, v_owner_id, 'company_owner', 'active'
    );
    -- An organization_service key: exactly what a hosted customer is handed, and
    -- exactly what the device lane can never match. The service account is not
    -- decoration -- enforce_service_key_billing_owner (202607200003) refuses a
    -- service key that cannot be traced to a billing owner through one.
    insert into public.service_accounts(
        id, organization_id, name, environment, created_by
    ) values (
        v_service_account_id, v_company_id, 'Company backend (production)',
        'production', v_owner_id
    );
    insert into public.api_keys(
        id, key_hash, name, owner_id, organization_id, service_account_id,
        key_type, scopes, created_by
    ) values (
        v_key_id, v_key_hash, 'hosted onboarding service key',
        v_owner_id::text, v_company_id, v_service_account_id,
        'organization_service',
        array['proxy:invoke','usage:write','usage:read_own']::text[],
        v_owner_id
    );

    v_status := public.organization_onboarding_status(v_owner_id, v_company_id);
    if not coalesce((v_status->>'ok')::boolean, false)
       or v_status->>'status' <> 'pending'
       or coalesce((v_status->>'cli_connected')::boolean, true)
       or coalesce((v_status->>'proxied_request_observed')::boolean, true) then
        raise exception 'hosted fixture started with unexpected evidence';
    end if;

    -- CLIENT-REPORTED. receipt_source='proxy' but authoritative=false: this is
    -- what POST /v1/usage writes, and a tenant holding its own key can send it.
    -- It must not be able to buy itself an onboarding completion.
    insert into public.usage_log(
        key_hash, owner_id, organization_id, authoritative,
        receipt_source, request_id, baseline_tokens, optimized_tokens
    ) values (
        v_key_hash, v_owner_id::text, v_company_id, false,
        'proxy', 'hosted-client-reported-0001', 10, 10
    ) returning id into v_client_usage_id;
    v_status := public.organization_onboarding_status(v_owner_id, v_company_id);
    if coalesce((v_status->>'cli_connected')::boolean, true)
       or coalesce((v_status->>'proxied_request_observed')::boolean, true) then
        raise exception 'client-reported traffic satisfied hosted onboarding evidence';
    end if;
    v_status := public.complete_organization_onboarding(
        v_owner_id, v_company_id, 'hosted-client-reported-check-0002'
    );
    if v_status->>'status' <> 'pending'
       or coalesce((v_status->>'proxied_request_observed')::boolean, true) then
        raise exception 'client-reported traffic completed hosted onboarding';
    end if;

    -- AUTHORITATIVE. Written only by the in-process hosted bridge, after
    -- Brevitas itself served the request.
    insert into public.usage_log(
        key_hash, owner_id, organization_id, authoritative,
        receipt_source, request_id, baseline_tokens, optimized_tokens
    ) values (
        v_key_hash, v_owner_id::text, v_company_id, true,
        'proxy', 'hosted-authoritative-0003', 10, 8
    ) returning id into v_usage_id;
    v_status := public.organization_onboarding_status(v_owner_id, v_company_id);
    if not coalesce((v_status->>'cli_connected')::boolean, false)
       or not coalesce((v_status->>'proxied_request_observed')::boolean, false) then
        raise exception 'authoritative hosted receipt was not observed as evidence';
    end if;

    v_status := public.complete_organization_onboarding(
        v_owner_id, v_company_id, 'hosted-complete-0004'
    );
    if not coalesce((v_status->>'ok')::boolean, false)
       or v_status->>'status' <> 'complete'
       or not coalesce((v_status->>'cli_connected')::boolean, false)
       or not coalesce((v_status->>'proxied_request_observed')::boolean, false) then
        raise exception 'authoritative hosted receipt did not complete onboarding';
    end if;
    -- The recorded evidence must be the AUTHORITATIVE row, never the earlier
    -- client-reported one that shares its organization and receipt_source.
    if not exists (
        select 1 from public.organizations
         where id = v_company_id
           and onboarding_completed_by = v_owner_id
           and onboarding_evidence_usage_id = v_usage_id
    ) then
        raise exception 'hosted completion recorded the wrong evidence row';
    end if;
    if v_client_usage_id = v_usage_id then
        raise exception 'fixture did not distinguish the two usage rows';
    end if;

    -- A non-member must still be refused, unchanged by the widening.
    v_status := public.organization_onboarding_status(
        'ffffffff-ffff-4fff-8fff-ffffffffffff'::uuid, v_company_id);
    if coalesce((v_status->>'ok')::boolean, true)
       or v_status->>'code' <> 'forbidden' then
        raise exception 'hosted onboarding status leaked to a non-member';
    end if;
end;
$$;

rollback;
