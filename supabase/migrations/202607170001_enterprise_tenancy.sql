-- Enterprise tenant boundary. End customers remain identities owned by an
-- organization; they never receive Brevitas credentials or Supabase accounts.

create table if not exists public.organizations (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    legacy_owner_id text unique,
    billing_owner_id uuid references auth.users(id) on delete restrict,
    cache_enabled boolean not null default false,
    created_at timestamptz not null default now()
);

create table if not exists public.organization_members (
    organization_id uuid not null references public.organizations(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    role text not null default 'admin' check (role in ('owner', 'admin', 'member', 'billing')),
    created_at timestamptz not null default now(),
    primary key (organization_id, user_id)
);
alter table public.organizations add column if not exists cache_enabled boolean not null default false;
alter table public.organizations add column if not exists billing_owner_id uuid references auth.users(id) on delete restrict;

create table if not exists public.customers (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    external_id text not null check (char_length(external_id) between 1 and 200),
    display_name text not null default '',
    status text not null default 'active' check (status in ('active', 'suspended', 'deleted')),
    cache_enabled boolean not null default false,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (organization_id, external_id),
    unique (organization_id, id)
);
alter table public.customers add column if not exists cache_enabled boolean not null default false;

-- Import a complete API batch in one database round trip. The API derives the
-- organization from its authenticated key/session; callers cannot select a
-- tenant through the public endpoint. Empty labels preserve an existing label.
create or replace function public.import_enterprise_customers(
    p_organization_id uuid,
    p_customers jsonb
)
returns table(id uuid, external_id text, display_name text, status text)
language sql
security definer
set search_path = public
as $$
    with raw_input as (
        select item.ordinality,
               item.value->>'external_id' as external_id,
               coalesce(item.value->>'display_name', '') as display_name
          from jsonb_array_elements(p_customers) with ordinality as item(value, ordinality)
    ), deduplicated as (
        select distinct on (raw_input.external_id)
               raw_input.external_id, raw_input.display_name
          from raw_input
         order by raw_input.external_id, raw_input.ordinality desc
    ), upserted as (
        insert into public.customers as customer(
            organization_id, external_id, display_name
        )
        select p_organization_id, deduplicated.external_id, deduplicated.display_name
          from deduplicated
        on conflict (organization_id, external_id) do update set
            display_name = case
                when excluded.display_name <> '' then excluded.display_name
                else customer.display_name
            end,
            updated_at = case
                when excluded.display_name <> ''
                 and excluded.display_name <> customer.display_name then now()
                else customer.updated_at
            end
        returning customer.id, customer.external_id,
                  customer.display_name, customer.status
    )
    select upserted.id, upserted.external_id,
           upserted.display_name, upserted.status
      from raw_input
      join upserted using (external_id)
     order by raw_input.ordinality;
$$;
revoke all on function public.import_enterprise_customers(uuid, jsonb)
    from public, anon, authenticated;
grant execute on function public.import_enterprise_customers(uuid, jsonb)
    to service_role;

create table if not exists public.service_accounts (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    name text not null,
    environment text not null default 'production',
    created_by uuid references auth.users(id) on delete set null,
    created_at timestamptz not null default now(),
    unique (organization_id, name, environment),
    unique (organization_id, id)
);

alter table public.api_keys add column if not exists organization_id uuid references public.organizations(id) on delete cascade;
alter table public.api_keys add column if not exists id uuid default gen_random_uuid();
update public.api_keys set id = gen_random_uuid() where id is null;
alter table public.api_keys alter column id set not null;
create unique index if not exists api_keys_id_idx on public.api_keys(id);
alter table public.api_keys add column if not exists service_account_id uuid references public.service_accounts(id) on delete cascade;
alter table public.api_keys add column if not exists key_type text not null default 'legacy';
alter table public.api_keys drop constraint if exists api_keys_key_type_check;
alter table public.api_keys add constraint api_keys_key_type_check
    check (key_type in ('legacy', 'organization_service', 'device', 'dashboard_session'));
alter table public.api_keys add column if not exists scopes text[] not null default array[
    'proxy:invoke', 'usage:write', 'usage:read_own', 'repositories:register'
]::text[];
alter table public.api_keys add column if not exists environment text not null default '';
alter table public.api_keys add column if not exists key_prefix text not null default '';
alter table public.api_keys add column if not exists expires_at timestamptz;
alter table public.api_keys add column if not exists last_used_at timestamptz;
alter table public.api_keys add column if not exists revoked_at timestamptz;
alter table public.api_keys add column if not exists created_by uuid references auth.users(id) on delete set null;
alter table public.api_keys drop constraint if exists api_keys_service_account_tenant_fk;
alter table public.api_keys add constraint api_keys_service_account_tenant_fk
    foreign key (organization_id, service_account_id)
    references public.service_accounts(organization_id, id) on delete cascade;

-- Existing owner-scoped traffic is mapped to a private legacy organization.
-- Empty owner IDs remain deliberately unattributed.
insert into public.organizations (name, legacy_owner_id)
select 'Legacy account ' || left(owner_id, 12), owner_id
from public.api_keys
where owner_id <> ''
group by owner_id
on conflict (legacy_owner_id) do nothing;

update public.api_keys k
set organization_id = o.id
from public.organizations o
where k.organization_id is null and k.owner_id <> '' and o.legacy_owner_id = k.owner_id;

insert into public.organization_members (organization_id, user_id, role)
select o.id, u.id, 'owner'
from public.organizations o
join auth.users u on u.id::text = o.legacy_owner_id
on conflict (organization_id, user_id) do nothing;

update public.organizations organization
set billing_owner_id = member.user_id
from public.organization_members member
where member.organization_id = organization.id
  and member.role = 'owner'
  and organization.billing_owner_id is null;

create index if not exists api_keys_organization_idx on public.api_keys(organization_id, created desc);
create index if not exists api_keys_service_account_idx on public.api_keys(service_account_id);

alter table public.usage_log add column if not exists organization_id uuid references public.organizations(id) on delete restrict;
alter table public.usage_log add column if not exists customer_id uuid;
alter table public.usage_log add column if not exists authoritative boolean not null default false;
update public.usage_log u
set organization_id = o.id
from public.organizations o
where u.organization_id is null and u.owner_id <> '' and o.legacy_owner_id = u.owner_id;
alter table public.usage_log drop constraint if exists usage_log_customer_tenant_fk;
alter table public.usage_log add constraint usage_log_customer_tenant_fk
    foreign key (organization_id, customer_id)
    references public.customers(organization_id, id) on delete restrict;
create index if not exists usage_log_tenant_ts_idx
    on public.usage_log(organization_id, customer_id, ts desc);

create table if not exists public.devices (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    device_fingerprint text not null,
    display_name text not null default '',
    created_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    revoked_at timestamptz,
    unique (organization_id, device_fingerprint),
    unique (organization_id, id)
);

create table if not exists public.installations (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    device_id uuid,
    service_account_id uuid,
    repository_id text not null default '',
    repository text not null default '',
    environment text not null default '',
    device_platform text not null default '',
    device_arch text not null default '',
    client_name text not null default '',
    bvx_version text not null default '',
    installed_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    revoked_at timestamptz,
    foreign key (organization_id, device_id) references public.devices(organization_id, id) on delete cascade,
    foreign key (organization_id, service_account_id) references public.service_accounts(organization_id, id) on delete set null
);
alter table public.installations add column if not exists repository_id text not null default '';
alter table public.installations add column if not exists device_platform text not null default '';
alter table public.installations add column if not exists device_arch text not null default '';
alter table public.installations add column if not exists client_name text not null default '';
create index if not exists installations_org_seen_idx on public.installations(organization_id, last_seen_at desc);

create table if not exists public.audit_events (
    id bigint generated always as identity primary key,
    organization_id uuid references public.organizations(id) on delete restrict,
    actor_user_id uuid references auth.users(id) on delete set null,
    actor_key_hash text,
    action text not null,
    target_type text not null default '',
    target_id text not null default '',
    details jsonb not null default '{}'::jsonb,
    occurred_at timestamptz not null default now()
);
create index if not exists audit_events_org_time_idx on public.audit_events(organization_id, occurred_at desc);

-- Browser clients do not retain raw API credentials in Postgres. Existing rows
-- are destroyed before removing the legacy plaintext column/table.
drop table if exists public.user_keys;

alter table public.organizations enable row level security;
alter table public.organization_members enable row level security;
alter table public.customers enable row level security;
alter table public.service_accounts enable row level security;
alter table public.devices enable row level security;
alter table public.installations enable row level security;
alter table public.audit_events enable row level security;

-- Management goes through the backend using a validated Supabase bearer token.
-- No browser policies are intentionally granted on service-owned tenant tables.

create or replace function public.ensure_enterprise_organization(
    p_user_id uuid,
    p_name text default 'My organization'
) returns table(id uuid, name text, role text)
language plpgsql
security definer
set search_path = public
as $$
declare organization public.organizations%rowtype;
begin
    insert into public.organizations(name, legacy_owner_id, billing_owner_id)
    values (coalesce(nullif(p_name, ''), 'My organization'), p_user_id::text, p_user_id)
    on conflict (legacy_owner_id) do update set legacy_owner_id = excluded.legacy_owner_id
    returning * into organization;
    insert into public.organization_members(organization_id, user_id, role)
    values (organization.id, p_user_id, 'owner')
    on conflict (organization_id, user_id) do nothing;
    return query select organization.id, organization.name, member.role
    from public.organization_members member
    where member.organization_id = organization.id and member.user_id = p_user_id;
end;
$$;
revoke all on function public.ensure_enterprise_organization(uuid, text) from public, anon, authenticated;
grant execute on function public.ensure_enterprise_organization(uuid, text) to service_role;

-- Only server-observed rows are eligible for billing. SDK/customer telemetry may
-- remain useful for analytics but can never create a billing ledger entry.
create or replace function public.queue_brevitas_fee()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare safe_fee numeric;
begin
    if not new.authoritative or new.owner_id = '' or new.pricing_status <> 'priced' then
        return new;
    end if;
    if not exists (
        select 1 from public.billing_accounts account
        where account.user_id::text = new.owner_id
          and account.subscription_status in ('active', 'trialing')
          and account.billing_started_at is not null
          and new.ts >= account.billing_started_at
    ) then return new; end if;
    safe_fee := least(greatest(coalesce(new.brevitas_fee_usd, 0), 0),
                      greatest(coalesce(new.verified_savings_usd, 0), 0) * 0.25);
    insert into public.billing_ledger (usage_log_id, user_id, occurred_at, fee_microusd)
    values (new.id, new.owner_id::uuid, new.ts, floor(safe_fee * 1000000)::bigint)
    on conflict (usage_log_id) do nothing;
    return new;
exception when invalid_text_representation then return new;
end;
$$;
revoke all on function public.queue_brevitas_fee() from public, anon, authenticated;

-- Device login is for Company A's own BVX installations. It inherits the
-- approving human's organization and receives no customer-routing permission.
create or replace function public.consume_bvx_device(p_device_hash text)
returns table(owner_id text, encrypted_key text)
language sql
security definer
set search_path = public
as $$
    with consumed as (
        delete from public.bvx_device_auth as request
         where request.device_hash = p_device_hash
           and request.approved_at is not null
           and request.expires_at > now()
        returning request.owner_id, request.key_hash, request.encrypted_key
    ), activated as (
        insert into public.api_keys(
            key_hash, name, created, owner_id, organization_id, key_type, scopes
        )
        select consumed.key_hash, 'bvx device', now(),
               coalesce(organization.billing_owner_id::text, consumed.owner_id),
               member.organization_id, 'device',
               array['proxy:invoke','usage:write',
                     'repositories:register','installations:register',
                     'customers:import']::text[]
        from consumed
        left join lateral (
            select organization_id from public.organization_members
            where user_id::text = consumed.owner_id order by created_at limit 1
        ) member on true
        left join public.organizations organization on organization.id = member.organization_id
    )
    select consumed.owner_id, consumed.encrypted_key from consumed;
$$;
revoke all on function public.consume_bvx_device(text) from public, anon, authenticated;
grant execute on function public.consume_bvx_device(text) to service_role;
