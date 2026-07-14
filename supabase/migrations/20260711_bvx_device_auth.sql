-- One-time browser authorization for `bvx login`.
-- Only Railway's service role can access this table or its RPCs.

create table if not exists public.bvx_device_auth (
    device_hash text primary key,
    expires_at timestamptz not null,
    owner_id text not null default '',
    key_hash text not null default '',
    encrypted_key text not null default '',
    approved_at timestamptz
);

alter table public.bvx_device_auth enable row level security;
alter table public.bvx_device_auth add column if not exists key_hash text not null default '';

create or replace function public.approve_bvx_device(
    p_device_hash text,
    p_owner_id text,
    p_key_hash text,
    p_encrypted_key text
) returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    update public.bvx_device_auth
       set owner_id = p_owner_id,
           key_hash = p_key_hash,
           encrypted_key = p_encrypted_key,
           approved_at = now()
     where device_hash = p_device_hash
       and approved_at is null
       and expires_at > now();
    if not found then
        return false;
    end if;
    return true;
end;
$$;

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
        insert into public.api_keys(key_hash, name, created, owner_id)
        select consumed.key_hash, 'bvx', now(), consumed.owner_id from consumed
    )
    select consumed.owner_id, consumed.encrypted_key from consumed;
$$;

revoke all on function public.approve_bvx_device(text, text, text, text) from public, anon, authenticated;
revoke all on function public.consume_bvx_device(text) from public, anon, authenticated;
grant execute on function public.approve_bvx_device(text, text, text, text) to service_role;
grant execute on function public.consume_bvx_device(text) to service_role;
