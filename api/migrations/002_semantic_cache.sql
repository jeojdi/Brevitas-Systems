-- DEPRECATED compatibility guard; this is not the canonical cache migration.
--
-- The only supported hosted-cache schema path is the ordered Supabase migration:
--   supabase/migrations/202607170002_cache_security.sql
-- after:
--   supabase/migrations/202607170001_enterprise_tenancy.sql
--
-- Older versions of this file created response_json as required plaintext and
-- granted service_role direct writes. This guard is safe before or after the
-- canonical migration: it never creates semantic_cache, purges only unsafe legacy
-- rows, removes the plaintext lookup signature, and denies direct content writes.

create extension if not exists vector;

do $$
begin
    if to_regclass('public.semantic_cache') is null then
        raise notice 'semantic_cache not created: apply 202607170002_cache_security.sql';
        return;
    end if;

    alter table public.semantic_cache
        add column if not exists response_ciphertext text not null default '';
    alter table public.semantic_cache
        add column if not exists tenant_namespace text not null default '';
    alter table public.semantic_cache alter column response_json drop not null;

    -- SQL migrations do not possess the application encryption key. Purge rather
    -- than copy or retain any row that is plaintext or lacks valid ciphertext.
    delete from public.semantic_cache
     where response_json is not null
        or response_ciphertext is null
        or response_ciphertext = '';
    update public.semantic_cache set response_json = null where response_json is not null;

    if not exists (
        select 1 from pg_constraint
         where conname = 'semantic_cache_no_plaintext'
           and conrelid = 'public.semantic_cache'::regclass
    ) then
        alter table public.semantic_cache add constraint semantic_cache_no_plaintext
            check (response_json is null);
    end if;

    alter table public.semantic_cache enable row level security;
    revoke all on table public.semantic_cache from public, anon, authenticated;
    revoke insert, update on table public.semantic_cache from service_role;
    grant select, delete on table public.semantic_cache to service_role;
end;
$$;

-- The retired RPC exposed response_json. It must never coexist with the encrypted
-- five-argument lookup installed by the timestamped canonical migration.
drop function if exists public.semantic_cache_lookup(vector, text, float);

-- Do not define a store/lookup RPC here. Operators must continue with
-- 202607170002_cache_security.sql, which installs bounded encrypted writes,
-- tenant/model-scoped ciphertext lookup, TTL/size constraints, and purge controls.
