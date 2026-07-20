-- Atomic, replay-safe BVX device credential delivery.
-- Requires timestamped migrations through 202607170009. Postgres remains the
-- activation authority; only short-lived KMS ciphertext is recoverable after
-- an ambiguous HTTP commit. Raw credentials never cross this boundary.

alter table public.bvx_device_auth
    add column if not exists organization_id uuid
        references public.organizations(id) on delete cascade;
alter table public.bvx_device_auth
    add column if not exists quarantined_at timestamptz;

create table if not exists public.bvx_device_consumption_receipts (
    device_hash text primary key check (device_hash ~ '^[0-9a-f]{64}$'),
    id uuid not null default gen_random_uuid(),
    key_hash text not null check (key_hash ~ '^[0-9a-f]{64}$'),
    encrypted_key text not null,
    owner_id uuid not null references auth.users(id) on delete cascade,
    approver_id uuid references auth.users(id) on delete cascade,
    organization_id uuid not null
        references public.organizations(id) on delete cascade,
    consumed_at timestamptz not null default now(),
    expires_at timestamptz not null,
    request_id text not null
        check (request_id ~ '^[A-Za-z0-9._:-]{8,128}$'),
    quarantined_at timestamptz,
    check (expires_at > consumed_at),
    check (expires_at <= consumed_at + interval '15 minutes'),
    constraint bvx_device_receipt_ciphertext_check check (
        (quarantined_at is null and approver_id is not null
         and char_length(encrypted_key) between 1 and 16384)
        or (quarantined_at is not null and encrypted_key='')
    )
);
alter table public.bvx_device_consumption_receipts
    add column if not exists quarantined_at timestamptz;
alter table public.bvx_device_consumption_receipts
    add column if not exists approver_id uuid references auth.users(id) on delete cascade;
alter table public.bvx_device_consumption_receipts
    add column if not exists id uuid default gen_random_uuid();
update public.bvx_device_consumption_receipts receipt
   set id=gen_random_uuid() where receipt.id is null;
alter table public.bvx_device_consumption_receipts alter column id set not null;
create unique index if not exists bvx_device_receipt_id_idx
    on public.bvx_device_consumption_receipts(id);
-- Reinstall the named invariant after remediating any pre-constraint fixture or
-- earlier draft. NOT VALID permits the cleanup to run before the explicit full
-- validation; the final state is enforced for both existing and future rows.
alter table public.bvx_device_consumption_receipts
    drop constraint if exists bvx_device_receipt_ciphertext_check;
-- A receipt created by an earlier draft cannot prove which member approved it
-- when billing ownership differed. Destroy its recovery material rather than
-- guessing an approver during an idempotent migration reapply/upgrade.
update public.bvx_device_consumption_receipts receipt
   set encrypted_key='',quarantined_at=coalesce(receipt.quarantined_at,now())
 where receipt.approver_id is null and receipt.quarantined_at is null;
update public.bvx_device_consumption_receipts receipt
   set encrypted_key=''
 where receipt.quarantined_at is not null
   and receipt.encrypted_key is distinct from '';
alter table public.bvx_device_consumption_receipts
    add constraint bvx_device_receipt_ciphertext_check check (
        encrypted_key is not null and (
            (quarantined_at is null and approver_id is not null
             and char_length(encrypted_key) between 1 and 16384)
            or (quarantined_at is not null and encrypted_key='')
        )
    ) not valid;
alter table public.bvx_device_consumption_receipts
    validate constraint bvx_device_receipt_ciphertext_check;
create index if not exists bvx_device_receipts_expiry_idx
    on public.bvx_device_consumption_receipts(expires_at);
alter table public.bvx_device_consumption_receipts enable row level security;
revoke all on table public.bvx_device_consumption_receipts
    from public, anon, authenticated, service_role;

-- W1 supplies only its authenticated company selector. An omitted selector is
-- safe solely for a user with exactly one active membership.
create or replace function public.resolve_bvx_device_approval_organization(
    p_owner_id text,
    p_selected_organization_id uuid default null
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_owner_id uuid;
    v_memberships integer;
    v_member public.organization_members%rowtype;
begin
    begin
        v_owner_id := p_owner_id::uuid;
    exception when invalid_text_representation then
        return jsonb_build_object('ok',false,'code','company_access_denied');
    end;

    if p_selected_organization_id is not null then
        select * into v_member
          from public.organization_members member
         where member.organization_id=p_selected_organization_id
           and member.user_id=v_owner_id
           and member.status='active'
           and member.role in (
               'company_owner','company_admin','member','billing_admin')
         for share;
        if not found then
            return jsonb_build_object('ok',false,'code','company_access_denied');
        end if;
    else
        select count(*) into v_memberships
          from public.organization_members member
         where member.user_id=v_owner_id
           and member.status='active'
           and member.role in (
               'company_owner','company_admin','member','billing_admin');
        if v_memberships>1 then
            return jsonb_build_object(
                'ok',false,'code','company_selection_required');
        elsif v_memberships<>1 then
            return jsonb_build_object('ok',false,'code','company_access_denied');
        end if;
        select * into v_member
          from public.organization_members member
         where member.user_id=v_owner_id
           and member.status='active'
           and member.role in (
               'company_owner','company_admin','member','billing_admin')
         for share;
    end if;

    return jsonb_build_object(
        'ok',true,'id',v_member.organization_id,'role',v_member.role);
end;
$$;
revoke all on function public.resolve_bvx_device_approval_organization(text,uuid)
    from public, anon, authenticated, service_role;
grant execute on function public.resolve_bvx_device_approval_organization(text,uuid)
    to service_role;

-- Approval independently re-locks and revalidates the exact selected tenant;
-- a resolver response cannot race a membership disable/removal.
create or replace function public.approve_bvx_device(
    p_device_hash text,
    p_owner_id text,
    p_key_hash text,
    p_encrypted_key text,
    p_organization_id uuid
) returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_owner_id uuid;
    v_member public.organization_members%rowtype;
begin
    if p_device_hash !~ '^[0-9a-f]{64}$'
       or p_key_hash !~ '^[0-9a-f]{64}$'
       or char_length(p_encrypted_key) not between 1 and 16384 then
        raise exception 'invalid device approval fields' using errcode='22023';
    end if;
    begin
        v_owner_id := p_owner_id::uuid;
    exception when invalid_text_representation then
        return false;
    end;
    select * into v_member
      from public.organization_members member
     where member.organization_id=p_organization_id
       and member.user_id=v_owner_id
       and member.status='active'
       and member.role in (
           'company_owner','company_admin','member','billing_admin')
     for share;
    if not found then
        return false;
    end if;

    update public.bvx_device_auth exchange
       set owner_id=v_owner_id::text,
           organization_id=p_organization_id,
           key_hash=p_key_hash,
           encrypted_key=p_encrypted_key,
           approved_at=now()
     where exchange.device_hash=p_device_hash
       and exchange.approved_at is null
       and exchange.quarantined_at is null
       and exchange.expires_at>now();
    return found;
end;
$$;
revoke all on function public.approve_bvx_device(text,text,text,text,uuid)
    from public, anon, authenticated, service_role;
grant execute on function public.approve_bvx_device(text,text,text,text,uuid)
    to service_role;
-- The four-argument predecessor cannot bind W1's selected company.
revoke all on function public.approve_bvx_device(text,text,text,text)
    from public, anon, authenticated, service_role;

-- The API reads through this service-only boundary so a process/client retry can
-- decrypt and verify the same bounded receipt after commit acknowledgement was
-- lost. request_id and other idempotency controls are intentionally not returned.
create or replace function public.get_bvx_device_exchange(p_device_hash text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_exchange public.bvx_device_auth%rowtype;
    v_receipt public.bvx_device_consumption_receipts%rowtype;
begin
    if p_device_hash !~ '^[0-9a-f]{64}$' then
        raise exception 'invalid device digest' using errcode='22023';
    end if;

    select * into v_exchange
      from public.bvx_device_auth exchange
     where exchange.device_hash=p_device_hash
       and exchange.quarantined_at is null
       and exchange.expires_at>now();
    if found then
        return jsonb_build_object(
            'device_hash',v_exchange.device_hash,
            'expires_at',v_exchange.expires_at,
            'owner_id',v_exchange.owner_id,
            'organization_id',v_exchange.organization_id,
            'key_hash',v_exchange.key_hash,
            'encrypted_key',v_exchange.encrypted_key,
            'approved_at',v_exchange.approved_at
        );
    end if;

    select * into v_receipt
      from public.bvx_device_consumption_receipts receipt
     where receipt.device_hash=p_device_hash
       and receipt.quarantined_at is null
       and receipt.expires_at>now();
    if not found then
        return null;
    end if;
    return jsonb_build_object(
        'device_hash',v_receipt.device_hash,
        'expires_at',v_receipt.expires_at,
        'owner_id',v_receipt.approver_id::text,
        'organization_id',v_receipt.organization_id::text,
        'key_hash',v_receipt.key_hash,
        'encrypted_key',v_receipt.encrypted_key,
        'approved_at',v_receipt.consumed_at
    );
end;
$$;
revoke all on function public.get_bvx_device_exchange(text)
    from public, anon, authenticated, service_role;
grant execute on function public.get_bvx_device_exchange(text) to service_role;

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

-- Retire the destructive legacy path. It cannot recover after a lost commit.
revoke all on function public.consume_bvx_device(text)
    from public, anon, authenticated, service_role;
