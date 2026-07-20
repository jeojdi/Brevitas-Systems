\set ON_ERROR_STOP on

do $$
declare organization_id uuid;
begin
    select organization.id into organization_id
      from public.organizations organization
     where organization.legacy_owner_id = 'e0000000-0000-4000-8000-000000000001';
    if organization_id is null then
        raise exception 'upgrade did not create the legacy owner organization';
    end if;
    if not exists (
        select 1 from public.api_keys key
         where key.key_hash = 'upgrade-baseline-key'
           and key.organization_id = organization_id
           and key.id is not null
           and key.key_type = 'legacy'
    ) then raise exception 'upgrade did not preserve and tenant-scope the legacy key'; end if;
    if not exists (
        select 1 from public.usage_log usage
         where usage.request_id = 'upgrade-baseline-usage'
           and usage.organization_id = organization_id
           and not usage.authoritative
           and usage.cache_write_5m_tokens = 0
           and usage.cache_write_1h_tokens = 0
           and not usage.cache_attributable
    ) then raise exception 'upgrade did not preserve and tenant-scope legacy usage'; end if;
    if (select count(*) from public.billing_ledger ledger
         join public.usage_log usage on usage.id = ledger.usage_log_id
         where usage.request_id = 'upgrade-baseline-usage') <> 1 then
        raise exception 'upgrade changed immutable financial evidence';
    end if;
    if to_regclass('public.user_keys') is not null then
        raise exception 'upgrade retained the browser raw-key table';
    end if;
    if exists (select 1 from public.semantic_cache where response_json is not null) then
        raise exception 'upgrade retained plaintext semantic cache content';
    end if;
end;
$$;
