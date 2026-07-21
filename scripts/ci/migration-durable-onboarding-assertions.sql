\set ON_ERROR_STOP on

do $$
declare
    v_company_id constant uuid := '91000000-0000-4000-8000-000000000016';
    v_other_company_id constant uuid := '92000000-0000-4000-8000-000000000016';
    v_owner_id constant uuid := 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
    v_other_owner_id constant uuid := 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
    v_forged_device_id constant uuid := '93000000-0000-4000-8000-000000000016';
    v_forged_installation_id constant uuid := '94000000-0000-4000-8000-000000000016';
    v_valid_installation_id constant uuid := '94000000-0000-4000-8000-000000000026';
    v_key_id constant uuid := '95000000-0000-4000-8000-000000000016';
    v_non_device_key_id constant uuid := '95000000-0000-4000-8000-000000000026';
    v_mismatch_key_id constant uuid := '95000000-0000-4000-8000-000000000036';
    v_receipt_id constant uuid := '96000000-0000-4000-8000-000000000016';
    -- Keep these fixture digests distinct from the earlier device-membership
    -- assertions, which intentionally retain repeat('6'/'7'/'8', 64) keys.
    v_key_hash constant text := lpad('16', 64, '0');
    v_non_device_key_hash constant text := lpad('26', 64, '0');
    v_mismatch_key_hash constant text := lpad('36', 64, '0');
    v_status jsonb;
    v_registration jsonb;
    v_usage_id bigint;
    v_completed_at timestamptz;
begin
    insert into auth.users(id, email) values
        (v_owner_id, 'durable-onboarding-owner@example.invalid'),
        (v_other_owner_id, 'durable-onboarding-other@example.invalid')
    on conflict (id) do nothing;

    insert into public.organizations(
        id, name, legacy_owner_id, billing_owner_id
    ) values (
        v_company_id, 'Durable onboarding fixture',
        'durable-onboarding-fixture', v_owner_id
    ) on conflict (id) do nothing;
    insert into public.organization_members(
        organization_id, user_id, role, status
    ) values (
        v_company_id, v_owner_id, 'company_owner', 'active'
    ) on conflict (organization_id, user_id) do update set
        role = excluded.role, status = excluded.status;

    insert into public.organizations(
        id, name, legacy_owner_id, billing_owner_id
    ) values (
        v_other_company_id, 'Other onboarding fixture',
        'durable-onboarding-other-fixture', v_other_owner_id
    ) on conflict (id) do nothing;
    insert into public.organization_members(
        organization_id, user_id, role, status
    ) values (
        v_other_company_id, v_other_owner_id, 'company_owner', 'active'
    ) on conflict (organization_id, user_id) do update set
        role = excluded.role, status = excluded.status;

    v_status := public.organization_onboarding_status(v_owner_id, v_company_id);
    if not coalesce((v_status->>'ok')::boolean, false)
       or v_status->>'status' <> 'pending'
       or coalesce((v_status->>'cli_connected')::boolean, true)
       or coalesce((v_status->>'proxied_request_observed')::boolean, true) then
        raise exception 'workspace existence incorrectly completed onboarding';
    end if;

    v_status := public.complete_organization_onboarding(
        v_owner_id, v_company_id, 'durable-onboarding-empty-0001'
    );
    if v_status->>'status' <> 'pending' then
        raise exception 'self-attestation completed onboarding without evidence';
    end if;

    insert into public.api_keys(
        id, key_hash, name, owner_id, organization_id, key_type,
        scopes, created_by
    ) values
    (
        v_key_id, v_key_hash, 'durable onboarding device digest',
        v_owner_id::text, v_company_id, 'device',
        array['proxy:invoke','installations:register']::text[], v_owner_id
    ),
    (
        v_non_device_key_id, v_non_device_key_hash,
        'durable onboarding non-device digest', v_owner_id::text,
        v_company_id, 'legacy',
        array['proxy:invoke','installations:register']::text[], v_owner_id
    ),
    (
        v_mismatch_key_id, v_mismatch_key_hash,
        'durable onboarding mismatched device digest', v_owner_id::text,
        v_company_id, 'device',
        array['proxy:invoke','installations:register']::text[], v_owner_id
    ) on conflict (key_hash) do nothing;

    insert into public.bvx_device_consumption_receipts(
        device_hash, id, key_hash, encrypted_key, owner_id, approver_id,
        organization_id, consumed_at, expires_at, request_id
    ) values (
        repeat('d', 64), v_receipt_id, v_key_hash, 'kms-fixture-ciphertext',
        v_owner_id, v_owner_id, v_company_id, now(),
        now() + interval '10 minutes', 'durable-device-consume-0002'
    );
    perform public.append_company_audit(
        v_company_id, v_owner_id::text, 'company_owner',
        'durable-device-consume-0002', 'device_key.activated', 'api_key',
        v_key_id::text, 'committed'
    );

    -- A hand-written BVX-looking installation is not evidence. Even real proxy
    -- traffic from the company device key cannot bind this forged row.
    insert into public.devices(
        id, organization_id, device_fingerprint, display_name
    ) values (
        v_forged_device_id, v_company_id,
        'durable-onboarding-forged-device', 'Forged BVX fixture'
    );
    insert into public.installations(
        id, organization_id, device_id, environment,
        client_name, bvx_version, installed_at, last_seen_at
    ) values (
        v_forged_installation_id, v_company_id, v_forged_device_id, 'test',
        'bvx', '9.9.9', now(), now()
    );
    insert into public.usage_log(
        key_hash, owner_id, organization_id, ts, authoritative,
        receipt_source, request_id, baseline_tokens, optimized_tokens
    ) values (
        v_key_hash, v_owner_id::text, v_company_id, now() - interval '2 seconds',
        true, 'proxy', 'durable-onboarding-forged-0003', 10, 10
    );
    v_status := public.complete_organization_onboarding(
        v_owner_id, v_company_id, 'durable-onboarding-forged-check-0004'
    );
    if v_status->>'status' <> 'pending'
       or coalesce((v_status->>'cli_connected')::boolean, true)
       or coalesce((v_status->>'proxied_request_observed')::boolean, true) then
        raise exception 'forged unbound installation became onboarding evidence';
    end if;

    v_registration := public.register_bvx_installation(
        v_company_id, v_key_hash, v_valid_installation_id,
        'durable-onboarding-valid-device', 'repo-16', 'owner/repo', 'test',
        'linux', 'x86_64', 'bvx', '1.2.3'
    );
    if not coalesce((v_registration->>'ok')::boolean, false)
       or v_registration->>'id' <> v_valid_installation_id::text
       or not coalesce(
           (v_registration->>'device_authorization_bound')::boolean, false
       ) then
        raise exception 'receipt-bound BVX registration failed';
    end if;
    if not exists (
        select 1 from public.installations installation
         where installation.id = v_valid_installation_id
           and installation.organization_id = v_company_id
           and installation.registration_key_hash = v_key_hash
           and installation.registration_key_id = v_key_id
           and installation.device_auth_receipt_id = v_receipt_id
           and installation.client_name = 'bvx'
    ) then
        raise exception 'registration did not persist server-derived identities';
    end if;

    -- Caller-authored, non-authoritative SDK telemetry is never a proxy receipt
    -- and cannot complete onboarding.
    insert into public.usage_log(
        key_hash, owner_id, organization_id, authoritative,
        receipt_source, request_id, baseline_tokens, optimized_tokens
    ) values (
        v_key_hash, v_owner_id::text, v_company_id, false,
        'sdk', 'durable-onboarding-sdk-0005', 10, 10
    );
    v_status := public.complete_organization_onboarding(
        v_owner_id, v_company_id, 'durable-onboarding-sdk-check-0006'
    );
    if v_status->>'status' <> 'pending'
       or not coalesce((v_status->>'cli_connected')::boolean, false)
       or coalesce((v_status->>'proxied_request_observed')::boolean, true) then
        raise exception 'SDK telemetry completed onboarding';
    end if;

    -- A server-authoritative proxy row from a non-device key remains
    -- insufficient even within the same company.
    insert into public.usage_log(
        key_hash, owner_id, organization_id, authoritative,
        receipt_source, request_id, baseline_tokens, optimized_tokens
    ) values (
        v_non_device_key_hash, v_owner_id::text, v_company_id, true,
        'proxy', 'durable-onboarding-non-device-0007', 10, 10
    );
    v_status := public.complete_organization_onboarding(
        v_owner_id, v_company_id, 'durable-onboarding-non-device-check-0008'
    );
    if v_status->>'status' <> 'pending' then
        raise exception 'non-device authoritative receipt completed onboarding';
    end if;

    -- A device-key proxy row is still insufficient when it is not the exact key
    -- bound by the installation registration transaction.
    insert into public.usage_log(
        key_hash, owner_id, organization_id, authoritative,
        receipt_source, request_id, baseline_tokens, optimized_tokens
    ) values (
        v_mismatch_key_hash, v_owner_id::text, v_company_id, true,
        'proxy', 'durable-onboarding-key-mismatch-0009', 10, 10
    );
    v_status := public.complete_organization_onboarding(
        v_owner_id, v_company_id, 'durable-onboarding-mismatch-check-0010'
    );
    if v_status->>'status' <> 'pending' then
        raise exception 'registration-key/usage-key mismatch completed onboarding';
    end if;

    v_status := public.complete_organization_onboarding(
        v_other_owner_id, v_company_id, 'durable-onboarding-cross-0011'
    );
    if coalesce((v_status->>'ok')::boolean, false) then
        raise exception 'cross-tenant actor completed onboarding';
    end if;

    insert into public.usage_log(
        key_hash, owner_id, organization_id, authoritative,
        receipt_source, request_id, baseline_tokens, optimized_tokens
    ) values (
        v_key_hash, v_owner_id::text, v_company_id, true,
        'proxy', 'durable-onboarding-valid-proxy-0012', 10, 10
    ) returning id into v_usage_id;
    v_status := public.complete_organization_onboarding(
        v_owner_id, v_company_id, 'durable-onboarding-complete-0013'
    );
    if not coalesce((v_status->>'ok')::boolean, false)
       or v_status->>'status' <> 'complete'
       or not coalesce((v_status->>'cli_connected')::boolean, false)
       or not coalesce((v_status->>'proxied_request_observed')::boolean, false) then
        raise exception 'valid receipt-bound BVX proxy request did not complete onboarding';
    end if;

    select onboarding_completed_at
      into v_completed_at
      from public.organizations
     where id = v_company_id
       and onboarding_completed_by = v_owner_id
       and onboarding_evidence_usage_id = v_usage_id;
    if v_completed_at is null then
        raise exception 'onboarding completion/evidence was not durably persisted';
    end if;
    if (select count(*) from public.audit_events
         where organization_id = v_company_id
           and action = 'organization.onboarding.completed'
           and request_id = 'durable-onboarding-complete-0013'
           and details = '{}'::jsonb) <> 1 then
        raise exception 'onboarding completion lacks content-free company audit';
    end if;

    v_status := public.complete_organization_onboarding(
        v_owner_id, v_company_id, 'durable-onboarding-retry-0014'
    );
    if v_status->>'status' <> 'complete'
       or (select count(*) from public.audit_events
            where organization_id = v_company_id
              and action = 'organization.onboarding.completed') <> 1 then
        raise exception 'onboarding completion retry was not idempotent';
    end if;

    if has_function_privilege(
        'anon', 'public.organization_onboarding_status(uuid,uuid)', 'EXECUTE'
    ) or has_function_privilege(
        'authenticated',
        'public.complete_organization_onboarding(uuid,uuid,text)', 'EXECUTE'
    ) or has_function_privilege(
        'authenticated',
        'public.register_bvx_installation(uuid,text,uuid,text,text,text,text,text,text,text,text)',
        'EXECUTE'
    ) or not has_function_privilege(
        'service_role', 'public.organization_onboarding_status(uuid,uuid)', 'EXECUTE'
    ) or not has_function_privilege(
        'service_role',
        'public.complete_organization_onboarding(uuid,uuid,text)', 'EXECUTE'
    ) or not has_function_privilege(
        'service_role',
        'public.register_bvx_installation(uuid,text,uuid,text,text,text,text,text,text,text,text)',
        'EXECUTE'
    ) then
        raise exception 'onboarding RPC grants are not service-role-only';
    end if;
end;
$$;
