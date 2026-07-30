-- 202607280026: the usage dedupe key now carries authority.
--
-- Two properties, and the file is worthless without both:
--   1. A same-authority repeat of (key_hash, request_id) is STILL rejected.
--      That is the property that prevents a billable receipt being counted
--      twice; widening the key must not have relaxed it.
--   2. A cross-authority pair with the same (key_hash, request_id) is now
--      ALLOWED. That is the change: an analytics observation and a billable
--      settlement receipt are different facts about the same request, and the
--      old index made the second one lose a race it should never have entered.
--
-- Opens its own transaction and ends in ROLLBACK, so it is rerunnable and
-- leaves no usage rows behind. usage_log carries no BEFORE INSERT trigger
-- (202607280006 retired queue_brevitas_fee_after_usage) and its tenant columns
-- are nullable, so a bare (key_hash, request_id, authoritative) row is a legal
-- fixture that touches no billing path.
\set ON_ERROR_STOP on

begin;

do $$
declare
    v_key text := 'dedupe-authority-assertions-key-hash';
    v_request text := 'proxy:dedupe-authority-assertions';
    v_rows bigint;
begin
    -- The widened index exists, is UNIQUE, is partial on the same predicate the
    -- 20260710 index used, and keys on exactly the three intended columns in
    -- the intended order (key_hash, request_id first, so the has_request probe
    -- still gets an index scan).
    if to_regclass('public.usage_log_request_authority_unique') is null then
        raise exception '202607280026 did not install the authority-scoped dedupe index';
    end if;
    if not exists (
        select 1
          from pg_index index_state
         where index_state.indexrelid = 'public.usage_log_request_authority_unique'::regclass
           and index_state.indisunique
           and index_state.indpred is not null
    ) then
        raise exception 'the authority-scoped dedupe index is not a partial UNIQUE index';
    end if;
    if pg_get_indexdef('public.usage_log_request_authority_unique'::regclass)
       !~ 'USING btree \(key_hash, request_id, authoritative\)'
       or pg_get_indexdef('public.usage_log_request_authority_unique'::regclass)
          !~ 'WHERE \(request_id <> ''''::text\)' then
        raise exception 'unexpected authority dedupe index definition: %',
            pg_get_indexdef('public.usage_log_request_authority_unique'::regclass);
    end if;

    -- The superseded narrow index is gone. While it exists the cross-authority
    -- pair below is still rejected, so leaving it behind would make the whole
    -- migration a no-op.
    if to_regclass('public.usage_log_request_unique') is not null then
        raise exception 'the superseded (key_hash, request_id) dedupe index survived';
    end if;

    -- (2) Cross-authority pair is admitted.
    insert into public.usage_log (key_hash, request_id, authoritative)
        values (v_key, v_request, false);
    insert into public.usage_log (key_hash, request_id, authoritative)
        values (v_key, v_request, true);
    select count(*) into v_rows
      from public.usage_log
     where key_hash = v_key and request_id = v_request;
    if v_rows <> 2 then
        raise exception
            'the authoritative receipt was still suppressed by the analytics row (% rows)',
            v_rows;
    end if;

    -- (1) Same-authority repeat is still rejected, in BOTH authorities.
    begin
        insert into public.usage_log (key_hash, request_id, authoritative)
            values (v_key, v_request, true);
        raise exception 'a duplicate authoritative receipt was accepted';
    exception
        when unique_violation then null;
    end;
    begin
        insert into public.usage_log (key_hash, request_id, authoritative)
            values (v_key, v_request, false);
        raise exception 'a duplicate non-authoritative receipt was accepted';
    exception
        when unique_violation then null;
    end;

    -- The partial predicate still exempts the empty request id, which is the
    -- default for receipts that carry no provider-visible identifier at all.
    insert into public.usage_log (key_hash, request_id, authoritative)
        values (v_key, '', true);
    insert into public.usage_log (key_hash, request_id, authoritative)
        values (v_key, '', true);
    select count(*) into v_rows
      from public.usage_log
     where key_hash = v_key and request_id = '';
    if v_rows <> 2 then
        raise exception 'unidentified receipts are no longer exempt from dedupe';
    end if;

    -- A different key_hash never collides, whatever the authority.
    insert into public.usage_log (key_hash, request_id, authoritative)
        values (v_key || '-other', v_request, true);
end;
$$;

rollback;
