-- Onboarding could never complete for the released local-proxy BVX. The
-- cli_connected gate (organization_onboarding_status, 202607280004) requires a
-- receipt-bound, device-key-bound public.installations row with
-- client_name='bvx', a non-empty bvx_version, and a non-null device_id. But the
-- shipped BVX CLI (0.1.27) only performs device authorization — `bvx login`
-- exchanges the device code for the API key and stores it, and no BVX command
-- (login, install, repair, doctor, start) ever calls POST /v1/installations.
-- So the installations row that the gate joins against was never created, and
-- every new workspace stalled at "connect the CLI" forever.
--
-- Fix (Option A): make the device-key activation itself register the server-side
-- installation, inside the same atomic consume transaction that writes the
-- device_key.activated audit event and the consumption receipt. The row is bound
-- to the exact activated device key (registration_key_id/hash) and its
-- consumption receipt (device_auth_receipt_id), and the device_id references a
-- real public.devices row derived from the device-authorization exchange — so the
-- evidence chain the gate verifies is genuine, not synthesized. The 202607280004
-- predicate is unchanged; this only supplies the row it always expected.
--
-- device_id: the device-auth flow (start/approve/token) carries no client device
-- metadata, so we mint a stable devices row keyed on the device-auth secret hash.
-- bvx_version: the exact CLI build is not reported over device-auth, so the row
-- records the provenance sentinel 'device-auth' (non-empty, satisfies the gate).
-- When a future BVX build calls /v1/installations with real metadata, that
-- register path upserts its own installation id independently of this row.
--
-- Rollback: restore the 202607170010 body of consume_bvx_device_idempotent (no
-- installation/devices inserts). The backfilled rows are inert and can be left
-- or revoked (set revoked_at) without affecting billing or scopes.

begin;

create or replace function public.consume_bvx_device_idempotent(
    p_device_hash text,
    p_expected_key_hash text,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_exchange public.bvx_device_auth%rowtype;
    v_receipt public.bvx_device_consumption_receipts%rowtype;
    v_member public.organization_members%rowtype;
    v_approver_member public.organization_members%rowtype;
    v_existing_key public.api_keys%rowtype;
    v_organization_id uuid;
    v_owner_id uuid;
    v_key_owner_id uuid;
    v_key_id uuid;
    v_quarantine_id uuid := gen_random_uuid();
    v_key_valid boolean := false;
    v_member_valid boolean := false;
    v_approver_valid boolean := false;
    v_audit_valid boolean := false;
    v_installation_device_id uuid;
    v_installation_fingerprint text;
begin
    if p_device_hash !~ '^[0-9a-f]{64}$'
       or p_expected_key_hash !~ '^[0-9a-f]{64}$'
       or p_request_id !~ '^[A-Za-z0-9._:-]{8,128}$' then
        raise exception 'invalid device consumption fields' using errcode='22023';
    end if;

    -- Serialize missing exchange, live exchange, and retained-receipt states.
    -- The row lock below then protects the approved record and its original TTL.
    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(p_device_hash,0));
    delete from public.bvx_device_consumption_receipts receipt
     where receipt.expires_at<=now();

    select * into v_receipt
      from public.bvx_device_consumption_receipts receipt
     where receipt.device_hash=p_device_hash
     for update;
    if found then
        if v_receipt.quarantined_at is not null then
            update public.bvx_device_consumption_receipts receipt
               set encrypted_key=''
             where receipt.device_hash=p_device_hash;
            perform public.append_company_audit(
                v_receipt.organization_id,'system','system',p_request_id,
                'device_key.consume.denied','device_receipt',
                v_receipt.id::text,'denied');
            return jsonb_build_object('ok',false,'code','receipt_quarantined');
        end if;
        select * into v_existing_key
          from public.api_keys credential
         where credential.key_hash=v_receipt.key_hash
         for update;
        if v_receipt.key_hash <> p_expected_key_hash then
            update public.api_keys credential
               set revoked_at=coalesce(credential.revoked_at,now())
             where credential.key_hash=v_receipt.key_hash
               and credential.organization_id=v_receipt.organization_id;
            update public.bvx_device_consumption_receipts receipt
               set encrypted_key='',quarantined_at=now()
             where receipt.device_hash=p_device_hash;
            perform public.append_company_audit(
                v_receipt.organization_id,v_receipt.owner_id::text,
                'system',p_request_id,'device_key.consume.denied',
                case when v_existing_key.id is null then 'device_receipt' else 'api_key' end,
                coalesce(v_existing_key.id,v_receipt.id)::text,'denied');
            return jsonb_build_object('ok',false,'code','digest_mismatch');
        end if;
        v_key_valid := found
            and v_existing_key.key_hash=v_receipt.key_hash
            and v_existing_key.organization_id=v_receipt.organization_id
            and v_existing_key.owner_id=v_receipt.owner_id::text
            and v_existing_key.key_type='device'
            and v_existing_key.revoked_at is null
            and (v_existing_key.expires_at is null
                 or v_existing_key.expires_at>now());
        select * into v_member
          from public.organization_members member
         where member.organization_id=v_receipt.organization_id
           and member.user_id=v_receipt.owner_id
           and member.status='active'
           and member.role in (
               'company_owner','company_admin','member','billing_admin')
         for share;
        v_member_valid := found;
        select * into v_approver_member
          from public.organization_members member
         where member.organization_id=v_receipt.organization_id
           and member.user_id=v_receipt.approver_id
           and member.status='active'
           and member.role in (
               'company_owner','company_admin','member','billing_admin')
         for share;
        v_approver_valid := found;
        select exists(
            select 1
              from public.audit_events event
             where event.organization_id=v_receipt.organization_id
               and event.actor_id=v_receipt.approver_id::text
               and event.actor_role in (
                   'company_owner','company_admin','member','billing_admin')
               and event.request_id=v_receipt.request_id
               and event.action='device_key.activated'
               and event.target_type='api_key'
               and event.target_id=v_existing_key.id::text
               and event.outcome='committed'
               and event.details='{}'::jsonb
        ) into v_audit_valid;
        if (not v_key_valid or not v_member_valid
                or not v_approver_valid or not v_audit_valid) then
            update public.bvx_device_consumption_receipts receipt
               set encrypted_key='',quarantined_at=now()
             where receipt.device_hash=p_device_hash;
            perform public.append_company_audit(
                v_receipt.organization_id,'system','system',p_request_id,
                'device_key.consume.denied',
                case when v_existing_key.id is null then 'device_receipt' else 'api_key' end,
                coalesce(v_existing_key.id,v_receipt.id)::text,'denied');
            return jsonb_build_object('ok',false,'code','receipt_invalid');
        end if;
        -- p_request_id binds the activation and immutable audit event. A later
        -- HTTP retry receives a new middleware ID, so a matching device/key
        -- digest may retrieve only the exact retained receipt; it cannot mint.
        return jsonb_build_object(
            'ok',true,'status','consumed','already_consumed',true,
            'device_hash',v_receipt.device_hash,
            'key_hash',v_receipt.key_hash,
            'encrypted_key',v_receipt.encrypted_key,
            'owner_id',v_receipt.owner_id::text,
            'organization_id',v_receipt.organization_id::text,
            'consumed_at',v_receipt.consumed_at
        );
    end if;

    select * into v_exchange
      from public.bvx_device_auth exchange
     where exchange.device_hash=p_device_hash
       and exchange.approved_at is not null
       and exchange.quarantined_at is null
       and exchange.expires_at>now()
     for update;
    if not found then
        return jsonb_build_object('ok',false,'code','expired_or_missing');
    end if;

    begin
        v_owner_id := v_exchange.owner_id::uuid;
    exception when invalid_text_representation then
        update public.bvx_device_auth exchange
           set quarantined_at=now(),encrypted_key=''
         where exchange.device_hash=p_device_hash;
        perform public.append_company_audit(
            v_exchange.organization_id,'system','system',p_request_id,
            'device_key.consume.denied','device_receipt',
            v_quarantine_id::text,'denied');
        return jsonb_build_object('ok',false,'code','tenant_binding_missing');
    end;
    v_organization_id := v_exchange.organization_id;
    select * into v_member
      from public.organization_members member
     where member.organization_id=v_organization_id
       and member.user_id=v_owner_id
       and member.status='active'
       and member.role in (
           'company_owner','company_admin','member','billing_admin')
     for share;
    if not found or v_organization_id is null then
        update public.bvx_device_auth exchange
           set quarantined_at=now(),encrypted_key=''
         where exchange.device_hash=p_device_hash;
        perform public.append_company_audit(
            v_organization_id,'system','system',p_request_id,
            'device_key.consume.denied','device_receipt',
            v_quarantine_id::text,'denied');
        return jsonb_build_object('ok',false,'code','tenant_binding_missing');
    end if;

    if v_exchange.key_hash <> p_expected_key_hash then
        update public.bvx_device_auth exchange
           set quarantined_at=now(),encrypted_key=''
         where exchange.device_hash=p_device_hash;
        update public.api_keys credential
           set revoked_at=coalesce(credential.revoked_at,now())
         where credential.key_hash=v_exchange.key_hash
           and credential.organization_id=v_organization_id;
        perform public.append_company_audit(
            v_organization_id,v_owner_id::text,v_member.role,p_request_id,
            'device_key.consume.denied','device_receipt',
            v_quarantine_id::text,'denied');
        return jsonb_build_object('ok',false,'code','digest_mismatch');
    end if;

    select * into v_existing_key
      from public.api_keys credential
     where credential.key_hash=v_exchange.key_hash
     for update;
    if found then
        update public.api_keys credential
           set revoked_at=coalesce(credential.revoked_at,now())
         where credential.key_hash=v_exchange.key_hash;
        update public.bvx_device_auth exchange
           set quarantined_at=now(),encrypted_key=''
         where exchange.device_hash=p_device_hash;
        perform public.append_company_audit(
            v_organization_id,v_owner_id::text,v_member.role,p_request_id,
            'device_key.consume.denied','api_key',v_existing_key.id::text,'denied');
        return jsonb_build_object('ok',false,'code','activation_conflict');
    end if;

    select billing_member.user_id into v_key_owner_id
      from public.organizations organization
      join public.organization_members billing_member
        on billing_member.organization_id=organization.id
       and billing_member.user_id=organization.billing_owner_id
       and billing_member.status='active'
       and billing_member.role in (
           'company_owner','company_admin','member','billing_admin')
     where organization.id=v_organization_id
     for share of billing_member;
    if not found then
        v_key_owner_id := v_owner_id;
    end if;
    insert into public.api_keys(
        key_hash,name,created,owner_id,organization_id,key_type,scopes
    ) values (
        v_exchange.key_hash,'bvx device',now(),
        v_key_owner_id::text,v_organization_id,'device',
        array['proxy:invoke','usage:write','repositories:register',
              'installations:register','customers:import']::text[]
    ) returning id into v_key_id;

    insert into public.bvx_device_consumption_receipts(
        device_hash,key_hash,encrypted_key,owner_id,approver_id,organization_id,
        consumed_at,expires_at,request_id
    ) values (
        v_exchange.device_hash,v_exchange.key_hash,v_exchange.encrypted_key,
        v_key_owner_id,v_owner_id,v_organization_id,now(),v_exchange.expires_at,p_request_id
    ) returning * into v_receipt;
    perform public.append_company_audit(
        v_organization_id,v_owner_id::text,v_member.role,p_request_id,
        'device_key.activated','api_key',v_key_id::text,'committed');

    -- Register the server-side BVX installation for this activation so the
    -- onboarding cli_connected gate is satisfiable (202607280005). The shipped
    -- local-proxy BVX never calls /v1/installations, so bind the row here to the
    -- just-activated device key and its consumption receipt. The device_id
    -- references a real devices row minted from the device-authorization hash;
    -- bvx_version records the 'device-auth' provenance sentinel (the exact CLI
    -- build is not reported over device authorization).
    v_installation_fingerprint := 'deviceauth:' || left(p_device_hash, 40);
    insert into public.devices(organization_id, device_fingerprint, last_seen_at)
    values (v_organization_id, v_installation_fingerprint, now())
    on conflict (organization_id, device_fingerprint)
        do update set last_seen_at = now()
    returning id into v_installation_device_id;

    insert into public.installations(
        organization_id, device_id, client_name, bvx_version,
        installed_at, last_seen_at, registration_key_hash,
        registration_key_id, device_auth_receipt_id
    ) values (
        v_organization_id, v_installation_device_id, 'bvx', 'device-auth',
        now(), now(), v_exchange.key_hash, v_key_id, v_receipt.id
    );

    delete from public.bvx_device_auth exchange
     where exchange.device_hash=p_device_hash;

    return jsonb_build_object(
        'ok',true,'status','consumed','already_consumed',false,
        'device_hash',v_receipt.device_hash,
        'key_hash',v_receipt.key_hash,
        'encrypted_key',v_receipt.encrypted_key,
        'owner_id',v_receipt.owner_id::text,
        'organization_id',v_receipt.organization_id::text,
        'consumed_at',v_receipt.consumed_at
    );
end;
$$;
revoke all on function public.consume_bvx_device_idempotent(text,text,text)
    from public, anon, authenticated, service_role;
grant execute on function public.consume_bvx_device_idempotent(text,text,text)
    to service_role;

-- Backfill: device keys activated before this migration have a committed
-- device_key.activated audit event and a consumption receipt but no installation
-- row, so their workspaces are permanently stuck at "connect the CLI". Register
-- the missing installation for each, using the genuine activation identities.
-- installed_at is the real consume time so any prior proxied usage still counts.
do $$
declare
    r record;
    v_device_id uuid;
    v_fingerprint text;
begin
    for r in
        select distinct on (credential.id)
               credential.id as key_id,
               credential.key_hash as key_hash,
               credential.organization_id as organization_id,
               receipt.id as receipt_id,
               receipt.device_hash as device_hash,
               receipt.consumed_at as consumed_at
          from public.api_keys credential
          join public.bvx_device_consumption_receipts receipt
            on receipt.key_hash = credential.key_hash
           and receipt.organization_id = credential.organization_id
           and receipt.quarantined_at is null
          join public.audit_events activation
            on activation.organization_id = credential.organization_id
           and activation.action = 'device_key.activated'
           and activation.target_type = 'api_key'
           and activation.target_id = credential.id::text
           and activation.outcome = 'committed'
         where credential.key_type = 'device'
           and credential.revoked_at is null
           and (credential.expires_at is null or credential.expires_at > now())
           and not exists (
               select 1 from public.installations existing
                where existing.organization_id = credential.organization_id
                  and existing.registration_key_id = credential.id
                  and existing.revoked_at is null
           )
         order by credential.id, receipt.consumed_at desc
    loop
        v_fingerprint := 'deviceauth:' || left(r.device_hash, 40);
        insert into public.devices(organization_id, device_fingerprint, last_seen_at)
        values (r.organization_id, v_fingerprint, now())
        on conflict (organization_id, device_fingerprint)
            do update set last_seen_at = now()
        returning id into v_device_id;

        insert into public.installations(
            organization_id, device_id, client_name, bvx_version,
            installed_at, last_seen_at, registration_key_hash,
            registration_key_id, device_auth_receipt_id
        ) values (
            r.organization_id, v_device_id, 'bvx', 'device-auth',
            r.consumed_at, now(), r.key_hash, r.key_id, r.receipt_id
        );
    end loop;
end;
$$;

commit;
