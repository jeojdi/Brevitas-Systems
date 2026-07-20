\set ON_ERROR_STOP on

drop trigger if exists semantic_cache_absolute_bound on public.semantic_cache;
drop trigger if exists semantic_cache_normalize_write on public.semantic_cache;
drop function if exists public.enforce_semantic_cache_absolute_bound();
drop function if exists public.normalize_semantic_cache_write();
drop function if exists public.semantic_cache_store_bounded(
    text,text,text,vector,text,text,integer,integer,integer,integer
);
drop function if exists public.semantic_cache_lookup(vector,text,float,text,text);
alter table public.semantic_cache drop constraint if exists semantic_cache_positive_bounded_ttl;
alter table public.semantic_cache drop constraint if exists semantic_cache_ciphertext_size;
alter table public.semantic_cache drop constraint if exists semantic_cache_metadata_size;
alter table public.semantic_cache drop constraint if exists semantic_cache_no_plaintext;
