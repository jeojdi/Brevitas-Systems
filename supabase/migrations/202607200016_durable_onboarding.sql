-- Onboarding is a durable server decision, never a browser checkbox. Completion
-- requires a registered BVX installation and a later authoritative proxy receipt
-- from a tenant-bound device key. Raw credentials never enter this state.

begin;

alter table public.installations
    add column if not exists registration_key_hash text,
    add column if not exists registration_key_id uuid,
    add column if not exists device_auth_receipt_id uuid;
alter table public.installations
    drop constraint if exists installations_registration_identity_check;
alter table public.installations
    add constraint installations_registration_identity_check check (
        registration_key_hash is null
        and registration_key_id is null
        and device_auth_receipt_id is null
        or registration_key_hash ~ '^[0-9a-f]{64}$'
        and registration_key_id is not null
    ) not valid;
alter table public.installations
    validate constraint installations_registration_identity_check;

alter table public.organizations
    add column if not exists onboarding_started_at timestamptz,
    add column if not exists onboarding_completed_at timestamptz,
    add column if not exists onboarding_completed_by uuid,
    add column if not exists onboarding_evidence_usage_id bigint;

update public.organizations
   set onboarding_started_at = created_at
 where onboarding_started_at is null;

alter table public.organizations
    alter column onboarding_started_at set default now(),
    alter column onboarding_started_at set not null;

do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conname = 'organizations_onboarding_completed_by_fk'
           and conrelid = 'public.organizations'::regclass
    ) then
        alter table public.organizations
            add constraint organizations_onboarding_completed_by_fk
            foreign key (onboarding_completed_by)
            references auth.users(id) on delete set null;
    end if;
end;
$$;

-- Preserve only historical completion that already has the same proof required
-- for new workspaces. Merely having a workspace, key, or healthy process is not
-- enough to pass this backfill.
with historical_evidence as (
    select distinct on (organization.id)
           organization.id as organization_id,
           usage.id as usage_id,
           usage.ts as completed_at
      from public.organizations organization
      join public.installations installation
        on installation.organization_id = organization.id
       and installation.revoked_at is null
       and lower(installation.client_name) = 'bvx'
       and installation.bvx_version <> ''
       and installation.device_id is not null
       and installation.device_auth_receipt_id is not null
       and installation.installed_at >= organization.onboarding_started_at
      join public.api_keys credential
        on credential.id = installation.registration_key_id
       and credential.key_hash = installation.registration_key_hash
       and credential.organization_id = organization.id
       and credential.key_type = 'device'
       and credential.revoked_at is null
       and (credential.expires_at is null or credential.expires_at > now())
      join public.audit_events activation
        on activation.organization_id = organization.id
       and activation.action = 'device_key.activated'
       and activation.target_type = 'api_key'
       and activation.target_id = credential.id::text
       and activation.outcome = 'committed'
      join public.usage_log usage
        on usage.organization_id = organization.id
       and usage.key_hash = installation.registration_key_hash
       and usage.authoritative is true
       and usage.receipt_source = 'proxy'
       and usage.ts >= installation.installed_at
     order by organization.id, usage.ts, usage.id
)
update public.organizations organization
   set onboarding_completed_at = evidence.completed_at,
       onboarding_evidence_usage_id = evidence.usage_id
  from historical_evidence evidence
 where organization.id = evidence.organization_id
   and organization.onboarding_completed_at is null;

alter table public.organizations
    drop constraint if exists organizations_onboarding_evidence_check;
alter table public.organizations
    add constraint organizations_onboarding_evidence_check check (
        onboarding_completed_at is null
        and onboarding_evidence_usage_id is null
        and onboarding_completed_by is null
        or onboarding_completed_at is not null
        and onboarding_completed_at >= onboarding_started_at
        and onboarding_evidence_usage_id is not null
        and onboarding_evidence_usage_id > 0
    ) not valid;
alter table public.organizations
    validate constraint organizations_onboarding_evidence_check;

create index if not exists organizations_onboarding_pending_idx
    on public.organizations(onboarding_started_at, id)
    where onboarding_completed_at is null;

-- Registration binds server-derived credential and device-authorization
-- identities in one transaction. The browser/CLI supplies neither database ID.
create or replace function public.register_bvx_installation(
    p_organization_id uuid,
    p_registration_key_hash text,
    p_installation_id uuid,
    p_device_fingerprint text,
    p_repository_id text,
    p_repository text,
    p_environment text,
    p_device_platform text,
    p_device_arch text,
    p_client_name text,
    p_bvx_version text
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_credential public.api_keys%rowtype;
    v_receipt_id uuid;
    v_device_id uuid;
    v_device_revoked_at timestamptz;
    v_existing public.installations%rowtype;
    v_existing_found boolean := false;
    v_now timestamptz := now();
begin
    if p_organization_id is null
       or p_installation_id is null
       or p_registration_key_hash is null
       or p_device_fingerprint is null
       or p_repository_id is null
       or p_repository is null
       or p_environment is null
       or p_device_platform is null
       or p_device_arch is null
       or p_client_name is null
       or p_bvx_version is null
       or p_registration_key_hash !~ '^[0-9a-f]{64}$'
       or char_length(p_device_fingerprint) not between 1 and 128
       or p_device_fingerprint !~ '^[A-Za-z0-9._:-]+$'
       or char_length(p_repository_id) > 128
       or char_length(p_repository) > 128
       or char_length(p_environment) > 32
       or char_length(p_device_platform) > 64
       or char_length(p_device_arch) > 64
       or char_length(p_client_name) > 64
       or char_length(p_bvx_version) > 64
       or concat_ws('', p_repository_id, p_repository, p_environment,
                    p_device_platform, p_device_arch, p_client_name,
                    p_bvx_version) ~ '[[:cntrl:]]' then
        return jsonb_build_object('ok', false, 'code', 'invalid_request');
    end if;

    perform pg_advisory_xact_lock(hashtextextended(p_installation_id::text, 0));
    -- Different installation IDs can register the same fingerprint concurrently.
    -- Serialize that unique-key decision before the select/insert pair.
    perform pg_advisory_xact_lock(hashtextextended(
        p_organization_id::text || ':' || p_device_fingerprint, 1
    ));
    select credential.* into v_credential
      from public.api_keys credential
     where credential.key_hash = p_registration_key_hash
       and credential.organization_id = p_organization_id
       and credential.revoked_at is null
       and (credential.expires_at is null or credential.expires_at > v_now)
       and credential.scopes @> array['installations:register']::text[]
     for share;
    if not found then
        return jsonb_build_object('ok', false, 'code', 'forbidden');
    end if;

    if v_credential.key_type = 'device' then
        select receipt.id into v_receipt_id
          from public.bvx_device_consumption_receipts receipt
         where receipt.key_hash = p_registration_key_hash
           and receipt.organization_id = p_organization_id
           and receipt.quarantined_at is null
           and exists (
               select 1 from public.audit_events activation
                where activation.organization_id = p_organization_id
                  and activation.action = 'device_key.activated'
                  and activation.target_type = 'api_key'
                  and activation.target_id = v_credential.id::text
                  and activation.outcome = 'committed'
           )
         order by receipt.consumed_at desc
         limit 1;
    end if;

    select installation.* into v_existing
      from public.installations installation
     where installation.id = p_installation_id
     for update;
    v_existing_found := found;
    if v_existing_found then
        if v_existing.organization_id <> p_organization_id then
            return jsonb_build_object('ok', false, 'code', 'foreign_installation');
        end if;
        if v_existing.revoked_at is not null then
            return jsonb_build_object('ok', false, 'code', 'installation_revoked');
        end if;
        if (v_existing.registration_key_hash is not null
                and v_existing.registration_key_hash <> p_registration_key_hash)
           or (v_existing.registration_key_id is not null
                and v_existing.registration_key_id <> v_credential.id)
           or (v_existing.device_auth_receipt_id is not null
                and v_receipt_id is not null
                and v_existing.device_auth_receipt_id <> v_receipt_id) then
            return jsonb_build_object('ok', false, 'code', 'credential_mismatch');
        end if;
    end if;

    select device.id, device.revoked_at
      into v_device_id, v_device_revoked_at
      from public.devices device
     where device.organization_id = p_organization_id
       and device.device_fingerprint = p_device_fingerprint
     for update;
    if v_device_id is null then
        insert into public.devices(
            organization_id, device_fingerprint, last_seen_at
        ) values (
            p_organization_id, p_device_fingerprint, v_now
        ) returning id into v_device_id;
    elsif v_device_revoked_at is not null then
        return jsonb_build_object('ok', false, 'code', 'device_revoked');
    else
        update public.devices set last_seen_at = v_now where id = v_device_id;
    end if;

    if v_existing_found then
        update public.installations
           set device_id = v_device_id,
               service_account_id = v_credential.service_account_id,
               repository_id = p_repository_id,
               repository = p_repository,
               environment = p_environment,
               device_platform = p_device_platform,
               device_arch = p_device_arch,
               client_name = p_client_name,
               bvx_version = p_bvx_version,
               last_seen_at = v_now,
               registration_key_hash = coalesce(
                   v_existing.registration_key_hash, p_registration_key_hash),
               registration_key_id = coalesce(
                   v_existing.registration_key_id, v_credential.id),
               device_auth_receipt_id = coalesce(
                   v_existing.device_auth_receipt_id, v_receipt_id)
         where id = p_installation_id;
    else
        insert into public.installations(
            id, organization_id, device_id, service_account_id,
            repository_id, repository, environment, device_platform,
            device_arch, client_name, bvx_version, installed_at, last_seen_at,
            registration_key_hash, registration_key_id, device_auth_receipt_id
        ) values (
            p_installation_id, p_organization_id, v_device_id,
            v_credential.service_account_id, p_repository_id, p_repository,
            p_environment, p_device_platform, p_device_arch, p_client_name,
            p_bvx_version, v_now, v_now, p_registration_key_hash,
            v_credential.id, v_receipt_id
        );
    end if;

    return jsonb_build_object(
        'ok', true, 'id', p_installation_id, 'last_seen_at', v_now::text,
        'device_authorization_bound', v_receipt_id is not null
            or v_existing.device_auth_receipt_id is not null
    );
end;
$$;
revoke all on function public.register_bvx_installation(
    uuid,text,uuid,text,text,text,text,text,text,text,text
) from public, anon, authenticated, service_role;
grant execute on function public.register_bvx_installation(
    uuid,text,uuid,text,text,text,text,text,text,text,text
) to service_role;

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
       and usage.authoritative is true
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
       and usage.authoritative is true
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

-- Rollback (manual and destructive):
-- drop function if exists public.complete_organization_onboarding(uuid,uuid,text);
-- drop function if exists public.organization_onboarding_status(uuid,uuid);
-- drop function if exists public.register_bvx_installation(uuid,text,uuid,text,text,text,text,text,text,text,text);
-- drop index if exists public.organizations_onboarding_pending_idx;
-- alter table public.organizations drop constraint if exists organizations_onboarding_evidence_check;
-- alter table public.organizations drop constraint if exists organizations_onboarding_completed_by_fk;
-- alter table public.organizations drop column if exists onboarding_evidence_usage_id;
-- alter table public.organizations drop column if exists onboarding_completed_by;
-- alter table public.organizations drop column if exists onboarding_completed_at;
-- alter table public.organizations drop column if exists onboarding_started_at;
-- alter table public.installations drop constraint if exists installations_registration_identity_check;
-- alter table public.installations drop column if exists device_auth_receipt_id;
-- alter table public.installations drop column if exists registration_key_id;
-- alter table public.installations drop column if exists registration_key_hash;
