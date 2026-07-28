\set ON_ERROR_STOP on

-- Migration 202607280004: onboarding evidence accepts the released BVX
-- topology — a receipt-bound device-key proxy row completes onboarding even
-- when it arrived via POST /v1/usage (authoritative=false). SDK telemetry and
-- unbound keys still never count, and the terminal function definitions must
-- be the relaxed ones (a frozen 016 replay must not survive as the end state).

do $$
declare
    v_company_id constant uuid := '97000000-0000-4000-8000-000000000004';
    v_owner_id constant uuid := 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
    v_key_id constant uuid := '98000000-0000-4000-8000-000000000004';
    v_receipt_id constant uuid := '99000000-0000-4000-8000-000000000004';
    v_installation_id constant uuid := '9a000000-0000-4000-8000-000000000004';
    v_key_hash constant text := lpad('46', 64, '0');
    v_status jsonb;
    v_registration jsonb;
    v_usage_id bigint;
begin
    if position('usage.authoritative is true' in pg_get_functiondef(
           to_regprocedure('public.organization_onboarding_status(uuid,uuid)'))) > 0
       or position('usage.authoritative is true' in pg_get_functiondef(
           to_regprocedure('public.complete_organization_onboarding(uuid,uuid,text)'))) > 0 then
        raise exception 'onboarding functions still gate evidence on authoritative receipts';
    end if;

    insert into auth.users(id, email) values
        (v_owner_id, 'onboarding-local-proxy-owner@example.invalid')
    on conflict (id) do nothing;
    insert into public.organizations(
        id, name, legacy_owner_id, billing_owner_id
    ) values (
        v_company_id, 'Local proxy onboarding fixture',
        'onboarding-local-proxy-fixture', v_owner_id
    ) on conflict (id) do nothing;
    insert into public.organization_members(
        organization_id, user_id, role, status
    ) values (
        v_company_id, v_owner_id, 'company_owner', 'active'
    ) on conflict (organization_id, user_id) do update set
        role = excluded.role, status = excluded.status;

    v_status := public.organization_onboarding_status(v_owner_id, v_company_id);
    if not coalesce((v_status->>'ok')::boolean, false)
       or v_status->>'status' <> 'pending'
       or coalesce((v_status->>'cli_connected')::boolean, true)
       or coalesce((v_status->>'proxied_request_observed')::boolean, true) then
        raise exception 'local-proxy fixture started with unexpected evidence';
    end if;

    insert into public.api_keys(
        id, key_hash, name, owner_id, organization_id, key_type,
        scopes, created_by
    ) values (
        v_key_id, v_key_hash, 'local proxy onboarding device digest',
        v_owner_id::text, v_company_id, 'device',
        array['proxy:invoke','usage:write','installations:register']::text[],
        v_owner_id
    ) on conflict (key_hash) do nothing;
    insert into public.bvx_device_consumption_receipts(
        device_hash, id, key_hash, encrypted_key, owner_id, approver_id,
        organization_id, consumed_at, expires_at, request_id
    ) values (
        repeat('e', 64), v_receipt_id, v_key_hash, 'kms-fixture-ciphertext',
        v_owner_id, v_owner_id, v_company_id, now(),
        now() + interval '10 minutes', 'local-proxy-device-consume-0001'
    );
    perform public.append_company_audit(
        v_company_id, v_owner_id::text, 'company_owner',
        'local-proxy-device-consume-0001', 'device_key.activated', 'api_key',
        v_key_id::text, 'committed'
    );
    v_registration := public.register_bvx_installation(
        v_company_id, v_key_hash, v_installation_id,
        'local-proxy-onboarding-device', 'repo-04', 'owner/repo', 'test',
        'linux', 'x86_64', 'bvx', '1.2.3'
    );
    if not coalesce((v_registration->>'ok')::boolean, false)
       or not coalesce(
           (v_registration->>'device_authorization_bound')::boolean, false
       ) then
        raise exception 'local-proxy fixture BVX registration failed';
    end if;

    -- Caller-authored SDK telemetry remains insufficient with or without the
    -- authoritative flag: receipt_source gates it, not authoritativeness.
    insert into public.usage_log(
        key_hash, owner_id, organization_id, authoritative,
        receipt_source, request_id, baseline_tokens, optimized_tokens
    ) values (
        v_key_hash, v_owner_id::text, v_company_id, false,
        'sdk', 'local-proxy-sdk-0002', 10, 10
    );
    v_status := public.complete_organization_onboarding(
        v_owner_id, v_company_id, 'local-proxy-sdk-check-0003'
    );
    if v_status->>'status' <> 'pending'
       or not coalesce((v_status->>'cli_connected')::boolean, false)
       or coalesce((v_status->>'proxied_request_observed')::boolean, true) then
        raise exception 'non-authoritative SDK telemetry completed onboarding';
    end if;

    -- The released topology: the local BVX proxy reports its provider receipt
    -- over POST /v1/usage, recorded with authoritative=false. From the bound
    -- device key this IS onboarding evidence.
    insert into public.usage_log(
        key_hash, owner_id, organization_id, authoritative,
        receipt_source, request_id, baseline_tokens, optimized_tokens
    ) values (
        v_key_hash, v_owner_id::text, v_company_id, false,
        'proxy', 'local-proxy-receipt-0004', 10, 10
    ) returning id into v_usage_id;
    v_status := public.organization_onboarding_status(v_owner_id, v_company_id);
    if not coalesce((v_status->>'proxied_request_observed')::boolean, false) then
        raise exception 'local-proxy receipt was not observed as evidence';
    end if;
    v_status := public.complete_organization_onboarding(
        v_owner_id, v_company_id, 'local-proxy-complete-0005'
    );
    if not coalesce((v_status->>'ok')::boolean, false)
       or v_status->>'status' <> 'complete'
       or not coalesce((v_status->>'cli_connected')::boolean, false)
       or not coalesce((v_status->>'proxied_request_observed')::boolean, false) then
        raise exception 'local-proxy device receipt did not complete onboarding';
    end if;
    if not exists (
        select 1 from public.organizations
         where id = v_company_id
           and onboarding_completed_by = v_owner_id
           and onboarding_evidence_usage_id = v_usage_id
    ) then
        raise exception 'local-proxy completion/evidence was not durably persisted';
    end if;
end;
$$;
