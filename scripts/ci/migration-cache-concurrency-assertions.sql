\set ON_ERROR_STOP on

do $$
begin
    if (select count(*) from public.semantic_cache) <> 3 then
        raise exception 'concurrent bounded cache writes did not converge to exactly three rows';
    end if;
    if exists (
        select 1 from public.semantic_cache
         where response_json is not null
            or response_ciphertext = ''
            or expires_at <= created_at
            or expires_at > created_at + interval '24 hours'
    ) then raise exception 'concurrent cache writes violated content or TTL constraints'; end if;
end;
$$;
