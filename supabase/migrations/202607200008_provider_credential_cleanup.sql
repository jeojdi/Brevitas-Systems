-- Provider credentials are scoped to one API-key digest. Remove the encrypted
-- configuration as soon as that key is revoked or deleted, and clean up expiry
-- races in bounded service-role maintenance batches.

begin;

-- A legacy deployment may already contain configurations whose key row was
-- deleted. Clear those rows before adding the referential-integrity guard.
delete from public.provider_config as config
 where not exists (
    select 1 from public.api_keys as credential
     where credential.key_hash = config.key_hash
 );

do $$
begin
    if not exists (
        select 1
          from pg_constraint
         where conname = 'provider_config_key_hash_fkey'
           and conrelid = 'public.provider_config'::regclass
    ) then
        alter table public.provider_config
            add constraint provider_config_key_hash_fkey
            foreign key (key_hash) references public.api_keys(key_hash)
            on delete cascade;
    end if;
end;
$$;

create or replace function public.require_active_provider_config_key()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    -- Lock the credential before writing its configuration. A concurrent
    -- revocation either waits and deletes this row, or commits first and makes
    -- this write fail closed.
    perform 1
      from public.api_keys as credential
     where credential.key_hash = new.key_hash
       and credential.revoked_at is null
       and (
           credential.expires_at is null
           or credential.expires_at > clock_timestamp()
       )
       and (
           credential.key_type <> 'organization_service'
           or exists (
               select 1
                 from public.service_accounts as account
                where account.organization_id = credential.organization_id
                  and account.id = credential.service_account_id
                  and account.status = 'active'
                  and account.revoked_at is null
                  and (
                      account.expires_at is null
                      or account.expires_at > clock_timestamp()
                  )
           )
       )
     for update of credential;

    if not found then
        raise exception 'provider configuration requires an active credential'
            using errcode = '23514';
    end if;
    return new;
end;
$$;

revoke all on function public.require_active_provider_config_key()
    from public, anon, authenticated;
drop trigger if exists provider_config_active_key_guard
    on public.provider_config;
create trigger provider_config_active_key_guard
before insert or update on public.provider_config
for each row execute function public.require_active_provider_config_key();

create or replace function public.delete_revoked_provider_config()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if tg_op = 'DELETE' then
        delete from public.provider_config where key_hash = old.key_hash;
        return old;
    end if;

    if new.revoked_at is not null then
        delete from public.provider_config where key_hash = new.key_hash;
    end if;
    return new;
end;
$$;

revoke all on function public.delete_revoked_provider_config()
    from public, anon, authenticated;

drop trigger if exists api_keys_provider_config_cleanup on public.api_keys;
create trigger api_keys_provider_config_cleanup
after update of revoked_at or delete on public.api_keys
for each row execute function public.delete_revoked_provider_config();

create or replace function public.purge_expired_provider_configs(p_limit integer default 500)
returns bigint
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_limit integer := greatest(1, least(coalesce(p_limit, 500), 1000));
    v_removed bigint;
begin
    with candidates as (
        select config.key_hash
          from public.provider_config as config
         where not exists (
                   select 1
                     from public.api_keys as credential
                    where credential.key_hash = config.key_hash
               )
            or exists (
                   select 1
                     from public.api_keys as credential
                    where credential.key_hash = config.key_hash
                      and (
                          credential.revoked_at is not null
                          or credential.expires_at <= clock_timestamp()
                          or (
                              credential.key_type = 'organization_service'
                              and not exists (
                                  select 1
                                    from public.service_accounts as account
                                   where account.organization_id = credential.organization_id
                                     and account.id = credential.service_account_id
                                     and account.status = 'active'
                                     and account.revoked_at is null
                                     and (
                                         account.expires_at is null
                                         or account.expires_at > clock_timestamp()
                                     )
                              )
                          )
                      )
               )
         order by config.key_hash
         for update of config skip locked
         limit v_limit
    ), removed as (
        delete from public.provider_config as config
         using candidates
         where config.key_hash = candidates.key_hash
         returning 1
    )
    select count(*) into v_removed from removed;

    return v_removed;
end;
$$;

revoke all on function public.purge_expired_provider_configs(integer)
    from public, anon, authenticated;
grant execute on function public.purge_expired_provider_configs(integer)
    to service_role;

-- Remove a first bounded batch during rollout. Worker maintenance continues
-- until all pre-existing expired/revoked configurations have been removed.
select public.purge_expired_provider_configs(1000);

commit;
