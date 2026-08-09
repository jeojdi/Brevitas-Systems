-- A warm ping that the provider may have accepted must never settle as $0 spend.
--
-- warm_ping_settle books ledger spend only for 'warmed'. Every other outcome
-- releases the reservation and books nothing, which is correct exactly when the
-- provider cannot have charged. api/worker.py:643-651 routed *all* httpx
-- transport failures to that path, including read timeouts, resets and protocol
-- errors that arrive only after the POST body was written -- i.e. after the
-- provider may already have accepted, cached and billed the ping. Those pings
-- cost the org real money and were recorded as zero.
--
-- Understated warm spend is not a cosmetic accounting gap: the settlement fee is
-- 25% x max(verified_savings - warm_spend, 0), and
-- billing_period_settlement_evidence.warm_spend_usd reads this ledger. Every
-- unbooked dollar of warm spend therefore raises the fee ceiling and OVERCHARGES
-- the organization. It ships before anything is allowed to raise ping volume.
--
-- This migration adds the settle outcome 'spent_unknown':
--   * ledger: releases the reservation and books the FULL reservation as spent,
--     ignoring p_spent_usd. There is no receipt to price the ping, and the
--     reservation is warm_due_claim's observer-priced upper bound which
--     daily_budget_usd already admitted, so it is the only defensible number --
--     and deriving it here means a buggy caller cannot book less than it
--     reserved. Conservative by construction: warm spend may be overstated,
--     never understated. (Overstating warm spend only ever lowers our own fee.)
--   * prefix: identical to the 'warmed' arm, under the same claim-token fence --
--     the ping counts toward warm_pings/pings_today (it may have cost money, so
--     it must count against max_pings_per_customer_day), consecutive_misses
--     follows the existing pre-charge convention (a ping is a miss until an
--     arrival clears it; no new hit/miss accounting is introduced), and
--     next_due_at moves to the TTL horizon so a flapping transport cannot
--     re-ping -- and re-charge -- on the next tick.
--
-- Pre-send failures (connect refused, DNS, connect/pool timeout) keep 'release':
-- no request bytes reached the provider, so no spend exists to book. The worker
-- classification mirrors brevitas/provider_reliability.py:590-594, which is also
-- the retry policy's own definition of "before request bytes can be accepted".
-- Because KNOWN_IDEMPOTENT_OPERATIONS is empty, ambiguous transport failures are
-- never retried, so the terminal exception type classifies the whole call.
--
-- api/store.py:73 (_WARM_SETTLE_OUTCOMES) and api/store.py:3478-3545 (the SQLite
-- mirror of this RPC) change in the same commit, with mirrored semantics.
--
-- No new tables, columns or stored values: compliance_delete_tenant, the
-- tenant/subject exports and compliance_run_retention are unaffected (the
-- warm_prefixes / warm_budget_ledger rows this touches are already enumerated by
-- 202607280016). No prompt or response material is read or written. Signature,
-- privileges and result schema are unchanged; forward-only and idempotent.

-- REVERSE: DDL: re-apply 202607280018_warm_claim_lease_fence.sql's warm_ping_settle body verbatim to drop the spent_unknown arm; callers passing 'spent_unknown' would then raise, so revert api/worker.py first

begin;

do $migration_precondition$
begin
    if to_regprocedure(
        'public.warm_ping_settle(uuid,uuid,text,text,date,numeric,numeric,text,integer,integer,uuid)'
    ) is null then
        raise exception using
            errcode = '55000',
            message = '202608080001 requires the multi-provider warming RPCs';
    end if;
    -- Anti-downgrade: 202608090001 extends this same function body with the
    -- last_touch_at provable-touch clock the TTL physics depend on. Re-applying
    -- this migration after that one would silently revert it — fail closed
    -- instead of overwriting a newer body.
    if position('last_touch_at' in coalesce(pg_get_functiondef(to_regprocedure(
        'public.warm_ping_settle(uuid,uuid,text,text,date,numeric,numeric,text,integer,integer,uuid)'
    )), '')) > 0 then
        raise exception using
            errcode = '55000',
            message = '202608080001 must not be applied after 202608090001: '
                      'the installed warm_ping_settle already carries the '
                      'last_touch_at clock this older body would drop';
    end if;
end;
$migration_precondition$;

create or replace function public.warm_ping_settle(
    p_organization_id uuid,
    p_customer_id uuid,
    p_provider text,
    p_prefix_hash text,
    p_budget_day date,
    p_reserved_usd numeric,
    p_spent_usd numeric,
    p_outcome text,
    p_provider_ttl_seconds integer,
    p_safety_margin_seconds integer,
    p_claim_token uuid default null
) returns jsonb as $$
declare
    v_now timestamptz := clock_timestamp();
    v_day date := (clock_timestamp() at time zone 'utc')::date;
    v_booked numeric := 0;
begin
    if p_provider not in ('anthropic', 'openai', 'deepseek')
       or p_prefix_hash !~ '^[0-9a-f]{64}$'
       or p_budget_day is null
       or coalesce(p_reserved_usd, -1) not between 0 and 99999999
       or coalesce(p_spent_usd, -1) not between 0 and 99999999
       or p_outcome not in ('warmed', 'spent_unknown', 'release',
                            'prefix_invalid', 'auth_failed')
       or coalesce(p_provider_ttl_seconds, 0) not between 60 and 86400
       or coalesce(p_safety_margin_seconds, -1) not between 0 and 3600 then
        raise exception 'warm settle arguments are invalid';
    end if;

    -- 'spent_unknown' books the reservation, not the caller's spend: the ping
    -- may have been charged and nothing priced it, so the admitted upper bound
    -- is the conservative booking.
    v_booked := case
        when p_outcome = 'warmed' then p_spent_usd
        when p_outcome = 'spent_unknown' then p_reserved_usd
        else 0 end;

    update public.warm_budget_ledger ledger
       set reserved_usd = greatest(0, ledger.reserved_usd - p_reserved_usd),
           spent_usd = ledger.spent_usd + v_booked,
           updated_at = v_now
     where ledger.organization_id = p_organization_id
       and ledger.provider = p_provider
       and ledger.day = p_budget_day;

    if p_outcome in ('warmed', 'spent_unknown') then
        update public.warm_prefixes prefix
           set warm_pings = prefix.warm_pings + 1,
               consecutive_misses = prefix.consecutive_misses + 1,
               pings_today = case when prefix.pings_today_date = v_day
                   then prefix.pings_today + 1 else 1 end,
               pings_today_date = v_day,
               next_due_at = v_now + make_interval(secs => greatest(
                   1, p_provider_ttl_seconds - p_safety_margin_seconds)),
               claim_token = null
         where prefix.organization_id = p_organization_id
           and prefix.customer_id = p_customer_id
           and prefix.provider = p_provider
           and prefix.prefix_hash = p_prefix_hash
           and (p_claim_token is null or prefix.claim_token = p_claim_token);
    elsif p_outcome = 'release' then
        -- A released claim is over, so drop the token: the schedule fence in
        -- warm_prefix_observe hands the row back to live traffic instead of
        -- pinning it at the lease horizon until the next claim settles as
        -- 'warmed'. Same token fence as the other arms, so a lapsed claimant
        -- cannot release someone else's claim.
        update public.warm_prefixes prefix
           set claim_token = null
         where prefix.organization_id = p_organization_id
           and prefix.customer_id = p_customer_id
           and prefix.provider = p_provider
           and prefix.prefix_hash = p_prefix_hash
           and (p_claim_token is null or prefix.claim_token = p_claim_token);
    elsif p_outcome = 'prefix_invalid' then
        update public.warm_prefixes prefix
           set state = 'stopped',
               claim_token = null
         where prefix.organization_id = p_organization_id
           and prefix.customer_id = p_customer_id
           and prefix.provider = p_provider
           and prefix.prefix_hash = p_prefix_hash
           and (p_claim_token is null or prefix.claim_token = p_claim_token);
    elsif p_outcome = 'auth_failed' then
        update public.warm_credentials cred
           set credential_state = 'auth_failed',
               updated_at = v_now
         where cred.organization_id = p_organization_id
           and cred.provider = p_provider;
    end if;

    return jsonb_build_object(
        'schema', 'brevitas.warm-settle.v1', 'status', 'settled',
        'outcome', p_outcome
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.warm_ping_settle(
    uuid, uuid, text, text, date, numeric, numeric, text, integer, integer, uuid
) from public, anon, authenticated;
grant execute on function public.warm_ping_settle(
    uuid, uuid, text, text, date, numeric, numeric, text, integer, integer, uuid
) to service_role;

comment on function public.warm_ping_settle(
    uuid, uuid, text, text, date, numeric, numeric, text, integer, integer, uuid
) is 'Settles one warm ping against the daily ledger; post-acceptance transport failures settle spent_unknown and book the full reservation so warm spend is never understated.';

commit;
