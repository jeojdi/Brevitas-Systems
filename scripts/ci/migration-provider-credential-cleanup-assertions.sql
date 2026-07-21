\set ON_ERROR_STOP on

-- Migration 008 must remove encrypted provider configuration when its API key
-- is revoked, deleted, or expires, and must reject new configuration for an
-- already inactive key.

insert into public.api_keys(
    key_hash,name,owner_id,organization_id,key_type,expires_at
) values
    ('provider-cleanup-revoke','Provider cleanup revoke fixture',
     'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
     '10000000-0000-4000-8000-000000000001','legacy',now()+interval '1 day'),
    ('provider-cleanup-delete','Provider cleanup delete fixture',
     'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
     '10000000-0000-4000-8000-000000000001','legacy',now()+interval '1 day'),
    ('provider-cleanup-expire','Provider cleanup expiry fixture',
     'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
     '10000000-0000-4000-8000-000000000001','legacy',now()+interval '1 day')
on conflict (key_hash) do nothing;

insert into public.provider_config(key_hash,provider,provider_api_key,model) values
    ('provider-cleanup-revoke','openai','release-ciphertext-revoke','fixture-model'),
    ('provider-cleanup-delete','openai','release-ciphertext-delete','fixture-model'),
    ('provider-cleanup-expire','openai','release-ciphertext-expire','fixture-model')
on conflict (key_hash) do update set provider_api_key=excluded.provider_api_key;

update public.api_keys set revoked_at=now()
 where key_hash='provider-cleanup-revoke';
delete from public.api_keys
 where key_hash='provider-cleanup-delete';
update public.api_keys set expires_at=now()-interval '1 second'
 where key_hash='provider-cleanup-expire';

do $$
declare
    v_removed bigint;
begin
    if exists (
        select 1 from public.provider_config
         where key_hash in ('provider-cleanup-revoke','provider-cleanup-delete')
    ) then
        raise exception 'provider configuration survived key revocation or deletion';
    end if;

    v_removed:=public.purge_expired_provider_configs(1);
    if v_removed<>1 or exists (
        select 1 from public.provider_config
         where key_hash='provider-cleanup-expire'
    ) then
        raise exception 'bounded provider configuration expiry purge failed';
    end if;

    begin
        insert into public.provider_config(
            key_hash,provider,provider_api_key,model
        ) values (
            'provider-cleanup-revoke','openai',
            'release-ciphertext-denied','fixture-model'
        );
        raise exception 'inactive key accepted a provider configuration';
    exception when check_violation then
        null;
    end;

    if has_function_privilege(
        'authenticated','public.purge_expired_provider_configs(integer)','execute'
    ) or has_function_privilege(
        'anon','public.purge_expired_provider_configs(integer)','execute'
    ) or not has_function_privilege(
        'service_role','public.purge_expired_provider_configs(integer)','execute'
    ) then
        raise exception 'provider configuration cleanup grants are unsafe';
    end if;
end;
$$;
