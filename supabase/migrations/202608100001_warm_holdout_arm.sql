-- Phase 0 warming instrumentation, part 4: the (org, prefix) control arm.
--
-- THE GAP. Every number the warming loop reports is an attribution estimate.
-- warm_prefixes.warm_hits credits a cache read that followed a ping, and
-- 202608090002's reward join prices it -- but a customer who would have
-- returned inside the TTL window anyway produces exactly the same row. The
-- organic-return share is unobservable from warmed traffic alone, so
-- "incremental savings" cannot be computed, only asserted. That is the number
-- Brevitas bills on.
--
-- A fixed-probability holdout is the one estimator that does not depend on any
-- attribution model: withhold a random share of the pings that were about to
-- happen, and the difference in what those prefixes cost is the causal effect,
-- whatever the organic rate turns out to be.
--
-- THE UNIT IS (org, prefix, UTC day). Not (org, customer, prefix): provider
-- prompt caches are scoped to the credential, i.e. to the organization, so a
-- prefix held out for one customer is kept warm for free by any sibling
-- customer that shares it. A per-customer holdout would measure near-zero lift
-- even where warming works perfectly (docs/RL_PREDICTIVE_WARMING_PLAN.md:196).
-- The day is in the key so assignment is redrawn every day against the same
-- eligibility test, which keeps the arms exchangeable as prefixes age; and the
-- assignment is a pure function of the key, so it is stable within a day across
-- ticks, replicas and restarts, with no state to store and nothing to erase
-- (the input is an org id and a hash the row already carries).
--
-- WHERE THE COIN IS FLIPPED. At the last possible moment: after the ROI floor,
-- the per-customer ping cap and the daily budget have all passed, so the row
-- was certain to be pinged and the two arms differ by nothing but the draw.
-- A held-out row reserves nothing, spends nothing, consumes no ping cap and
-- takes no claim token -- only next_due_at moves, by the same TTL horizon
-- warm_ping_settle would have applied, so the recorded counterfactual is "the
-- ping did not happen" rather than "the ping was rescheduled". Counters stay
-- put: consecutive_misses is the stop-loss's evidence that pings are not
-- converting, and a day with no ping is no evidence either way.
--
-- OFF BY DEFAULT. p_holdout_fraction defaults to 0 and null reads as 0. At 0 no
-- digest is computed, no branch is taken and no propensity is recorded, so the
-- claim loop is byte-identical to 202608090001's. The knob is
-- BREVITAS_WARM_HOLDOUT_PCT (a percent) on the worker; api/store.py parses it
-- once for both the worker and warm_status, which discloses it to the org.
--
-- THE MONEY DIRECTION. Holding a row out can only reduce spend: it is the
-- single point in this function that skips a reservation the org would have
-- paid. It cannot double-book, cannot strand a reservation (none is taken) and
-- cannot release one (none exists yet). The ledger, its advisory lock and the
-- claim-token fence are untouched.
--
-- NO NEW TABLES, NO NEW COLUMNS, NO CONTENT. The arm writes one
-- warm_decision_log row per held-out candidate, using the 'holdout' value that
-- 202608090001 already declared in that table's CHECK and the propensity column
-- it already declared for exactly this purpose. Nothing new to wire into
-- compliance: warm_decision_log is already enumerated by compliance_delete_tenant,
-- compliance_delete_subject, both exports and the 90-day retention class.
--
-- SIGNATURE CHANGE. warm_due_claim gains a trailing
-- `p_holdout_fraction double precision default 0`. The 10-argument signature is
-- dropped and replaced rather than overloaded -- exactly as 202607280003:214
-- did for this same function and 202608090001:418 did for warm_prefix_observe --
-- because two candidate signatures make every existing 10-argument call
-- ambiguous. PostgREST callers sending the old ten named arguments keep
-- resolving, because the new argument has a default.
--
-- Forward-only and idempotent.

-- REVERSE: DDL: re-apply 202608090001_warm_instrumentation_tables.sql's public.warm_due_claim verbatim after dropping public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision), then re-apply that migration's revoke/grant/comment for the ten-argument signature; no table, column or data change to undo

begin;

do $migration_precondition$
declare
    required_routine text;
begin
    -- Either arity satisfies this: the 10-argument form on a first apply, the
    -- 11-argument form on a re-apply after this migration already replaced it.
    if to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb)') is null
       and to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision)') is null then
        raise exception using
            errcode = '55000',
            message = '202608100001 requires public.warm_due_claim';
    end if;
    foreach required_routine in array array[
        -- The decision log's single writer, at the 15-argument arity that
        -- carries p_rng_seed and p_propensity; the holdout row needs both slots.
        'public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric)',
        'public.warm_ping_settle(uuid,uuid,text,text,date,numeric,numeric,text,integer,integer,uuid)'
    ] loop
        if to_regprocedure(required_routine) is null then
            raise exception using
                errcode = '55000',
                message = '202608100001 requires ' || required_routine;
        end if;
    end loop;
    if to_regclass('public.warm_prefixes') is null
       or to_regclass('public.warm_decision_log') is null then
        raise exception using
            errcode = '55000',
            message = '202608100001 requires public.warm_prefixes and public.warm_decision_log';
    end if;
    -- The arm is unrepresentable without this value in the CHECK; 202608090001
    -- declared it ahead of time precisely so this migration would not have to
    -- rewrite a constraint on a hot append-only table.
    if not exists (
        select 1
          from pg_catalog.pg_constraint constraint_row
         where constraint_row.conrelid = 'public.warm_decision_log'::regclass
           and constraint_row.contype = 'c'
           and pg_catalog.pg_get_constraintdef(constraint_row.oid) like '%holdout%'
    ) then
        raise exception using
            errcode = '55000',
            message = '202608100001 requires warm_decision_log to admit decision = holdout';
    end if;
end;
$migration_precondition$;

-- ---------------------------------------------------------------------------
-- warm_due_claim (202608090001:614-819) + the control arm. Every existing gate,
-- reservation and claim is carried forward unchanged; the only new exit is the
-- draw between the budget check and the reservation. The 10-argument signature
-- is replaced, not overloaded.
-- ---------------------------------------------------------------------------
drop function if exists public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer,
    integer, jsonb
);

create or replace function public.warm_due_claim(
    p_claim_limit integer,
    p_reserve_usd_per_mtok numeric,
    p_roi_min_arrivals integer,
    p_roi_min_p numeric,
    p_roi_break_even_p numeric,
    p_stop_loss integer,
    p_max_gap_seconds integer,
    p_safety_margin_seconds integer,
    p_claim_lease_seconds integer default 900,
    -- null means the caller predates per-provider ROI: every provider uses
    -- the flat p_roi_break_even_p, exactly as 202607280001 behaved.
    p_roi_break_even_by_provider jsonb default null,
    -- Control-arm share as a fraction in [0, 1]; 0 (and null, from a caller
    -- that predates the arm) means no randomization happens at all and this
    -- function behaves exactly as 202608090001's did.
    p_holdout_fraction double precision default 0
) returns setof jsonb as $$
declare
    v_now timestamptz := clock_timestamp();
    v_day date := (clock_timestamp() at time zone 'utc')::date;
    v_bucket_key text;
    v_row record;
    v_claimed integer := 0;
    v_claimed_counts jsonb := '{}'::jsonb;
    v_customer_key text;
    v_pings_today integer;
    v_p_return numeric;
    v_break_even numeric;
    -- The floor actually applied, materialized so the decision log can record
    -- the bar a denied candidate failed instead of leaving it to be re-derived.
    v_floor numeric;
    v_reserve numeric;
    v_reserved numeric;
    v_spent numeric;
    v_token uuid;
    -- Deterministic assignment for one (org, prefix, UTC day) unit. Mirrored
    -- by api/store.py:warm_holdout_bucket, which reads the same four bytes.
    v_holdout_digest bytea;
    v_holdout_bucket bigint;
begin
    if coalesce(p_claim_limit, 0) not between 1 and 500
       or coalesce(p_reserve_usd_per_mtok, -1) not between 0 and 1000
       or coalesce(p_roi_min_arrivals, 0) not between 1 and 1000
       or coalesce(p_roi_min_p, -1) not between 0 and 1
       or coalesce(p_roi_break_even_p, -1) not between 0 and 1
       or coalesce(p_stop_loss, 0) not between 1 and 100
       or coalesce(p_max_gap_seconds, 0) not between 1 and 604800
       or coalesce(p_safety_margin_seconds, -1) not between 0 and 3600
       or coalesce(p_claim_lease_seconds, 0) not between 60 and 7200
       or coalesce(p_holdout_fraction, 0) not between 0 and 1 then
        raise exception 'warm claim bounds are invalid';
    end if;
    if p_roi_break_even_by_provider is not null then
        if jsonb_typeof(p_roi_break_even_by_provider) <> 'object' or exists (
            select 1 from jsonb_each(p_roi_break_even_by_provider) entry
             where entry.key not in ('anthropic', 'openai', 'deepseek')
                or case when jsonb_typeof(entry.value) = 'number'
                       then (entry.value)::text::numeric not between 0 and 1
                       else true end
        ) then
            raise exception 'warm claim bounds are invalid';
        end if;
    end if;
    if not pg_try_advisory_xact_lock(
        hashtextextended('brevitas.warming.due_claim.v1', 0)
    ) then
        return next jsonb_build_object(
            'schema', 'brevitas.warm-claim.v1', 'status', 'lease_unavailable'
        );
        return;
    end if;

    v_bucket_key := ((extract(isodow from (v_now at time zone 'utc'))::integer - 1) * 24
                     + extract(hour from (v_now at time zone 'utc'))::integer)::text;

    for v_row in
        select prefix.*, cred.credential_ciphertext, cred.daily_budget_usd,
               cred.max_pings_per_customer_day
          from public.warm_prefixes prefix
          join public.warm_credentials cred
            on cred.organization_id = prefix.organization_id
           and cred.provider = prefix.provider
         where prefix.state = 'active'
           and prefix.next_due_at <= v_now
           and prefix.expires_at > v_now
           and prefix.consecutive_misses < p_stop_loss
           and coalesce(prefix.ewma_interarrival_s <= p_max_gap_seconds, true)
           and cred.enabled
           and cred.credential_state = 'active'
         order by prefix.next_due_at
         limit p_claim_limit * 4
    loop
        exit when v_claimed >= p_claim_limit;
        -- plpgsql locals survive the loop iteration, so a candidate denied
        -- before the cap query would otherwise log the previous candidate's
        -- count. Null means "not read yet", which is what happened.
        v_pings_today := null;
        v_token := null;

        -- ROI gate: warm only when the observed hour-of-week return frequency
        -- clears the break-even probability, with a stricter cold-start floor
        -- until enough arrivals make the histogram trustworthy.
        v_p_return := least(1, coalesce(
            (v_row.hour_histogram ->> v_bucket_key)::numeric, 0)
            / greatest(v_row.arrival_count, 1));
        v_break_even := coalesce(
            (p_roi_break_even_by_provider ->> v_row.provider)::numeric,
            p_roi_break_even_p);
        v_floor := case when v_row.arrival_count < p_roi_min_arrivals
            then p_roi_min_p else v_break_even end;
        -- Pure arithmetic on the candidate row, hoisted above the gates so a
        -- denied candidate can record what warming it would have cost. The
        -- value and its use below are unchanged: the reservation must
        -- upper-bound actual spend for the daily ceiling to hold, so reserve the
        -- larger of the observer-priced worst case and the flat caller floor.
        v_reserve := greatest(
            v_row.ping_reserve_usd,
            round(p_reserve_usd_per_mtok * v_row.prefix_tokens / 1000000.0, 10));
        if v_p_return < v_floor then
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, 'skipped_roi', v_p_return, v_floor, v_reserve,
                v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token);
            continue;
        end if;

        v_customer_key := v_row.organization_id || ':' || v_row.customer_id;
        select coalesce(sum(peer.pings_today), 0) into v_pings_today
          from public.warm_prefixes peer
         where peer.organization_id = v_row.organization_id
           and peer.customer_id = v_row.customer_id
           and peer.provider = v_row.provider
           and peer.pings_today_date = v_day;
        if v_pings_today + coalesce((v_claimed_counts ->> v_customer_key)::integer, 0)
           >= v_row.max_pings_per_customer_day then
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, 'cap_denied', v_p_return, v_floor, v_reserve,
                v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token);
            continue;
        end if;

        insert into public.warm_budget_ledger (organization_id, provider, day)
        values (v_row.organization_id, v_row.provider, v_day)
        on conflict (organization_id, provider, day) do nothing;
        select ledger.reserved_usd, ledger.spent_usd into v_reserved, v_spent
          from public.warm_budget_ledger ledger
         where ledger.organization_id = v_row.organization_id
           and ledger.provider = v_row.provider
           and ledger.day = v_day;
        if v_reserved + v_spent + v_reserve > v_row.daily_budget_usd then
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, 'budget_denied', v_p_return, v_floor,
                v_reserve, v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token);
            continue;
        end if;
        -- CONTROL ARM. Every gate has passed, so this row was certain to be
        -- pinged: the coin is the only thing separating the two arms, which is
        -- what makes the comparison causal. Randomizing earlier would fill the
        -- control arm with rows that were never going to be warmed.
        --
        -- A held-out row reserves nothing, spends nothing, consumes no ping cap
        -- and takes no claim token. Only next_due_at moves, by exactly the TTL
        -- horizon warm_ping_settle would have set (202608080001:110), so the
        -- counterfactual is "this ping did not happen" and not "this ping
        -- happened one cycle later". warm_pings, consecutive_misses and
        -- pings_today deliberately do NOT move: consecutive_misses is the
        -- stop-loss's evidence that pings are failing to convert, a holdout day
        -- produces no such evidence, and incrementing it would retire control
        -- prefixes for a ping nobody sent.
        --
        -- The unit is (org, prefix) and NOT (org, customer, prefix): provider
        -- caches are keyed by the org's credential, so a prefix held out for
        -- one customer is kept warm by any sibling sharing it and the arm would
        -- measure nothing (the SUTVA finding in
        -- docs/RL_PREDICTIVE_WARMING_PLAN.md:196). Redrawing daily keeps the
        -- assignment independent of how a prefix aged.
        if coalesce(p_holdout_fraction, 0) > 0 then
            -- to_char, not v_day::text: a non-ISO DateStyle would silently
            -- change every bucket. lower() on the uuid text is redundant in
            -- Postgres and load-bearing in the SQLite mirror, so both spell it.
            v_holdout_digest := sha256(convert_to(
                lower(v_row.organization_id::text)
                || lower(v_row.prefix_hash)
                || to_char(v_day, 'YYYY-MM-DD'), 'UTF8'));
            -- First four digest bytes, big-endian, as a uint32. Compared in
            -- IEEE-754 double space rather than by dividing down, so this and
            -- api/store.py:warm_is_held_out cannot disagree on a boundary unit.
            v_holdout_bucket := get_byte(v_holdout_digest, 0)::bigint * 16777216
                              + get_byte(v_holdout_digest, 1) * 65536
                              + get_byte(v_holdout_digest, 2) * 256
                              + get_byte(v_holdout_digest, 3);
            if v_holdout_bucket::double precision
               < p_holdout_fraction * 4294967296::double precision then
                update public.warm_prefixes prefix
                   set next_due_at = v_now + make_interval(secs => greatest(
                           1, prefix.provider_ttl_seconds - p_safety_margin_seconds))
                 where prefix.organization_id = v_row.organization_id
                   and prefix.customer_id = v_row.customer_id
                   and prefix.provider = v_row.provider
                   and prefix.prefix_hash = v_row.prefix_hash;
                perform public.warm_decision_record(
                    v_row.organization_id, v_row.customer_id, v_row.provider,
                    v_row.prefix_hash, 'holdout', v_p_return, v_floor, v_reserve,
                    v_row.prefix_tokens, v_row.ewma_interarrival_s,
                    v_row.arrival_count, v_pings_today, v_token, null,
                    p_holdout_fraction::numeric);
                continue;
            end if;
        end if;

        update public.warm_budget_ledger ledger
           set reserved_usd = ledger.reserved_usd + v_reserve,
               updated_at = v_now
         where ledger.organization_id = v_row.organization_id
           and ledger.provider = v_row.provider
           and ledger.day = v_day;

        -- Claim lease: must outlive a full sequential worker batch — not one
        -- tick — or an unsynchronized replica re-claims the tail of a batch
        -- mid-flight. The rotated token fences warm_ping_settle so a lapsed
        -- claimant cannot double-apply counters; warm_ping_settle assigns the
        -- real next_due_at.
        v_token := gen_random_uuid();
        update public.warm_prefixes prefix
           set next_due_at = v_now + make_interval(secs => greatest(
                   p_claim_lease_seconds, p_safety_margin_seconds, 60)),
               claim_token = v_token
         where prefix.organization_id = v_row.organization_id
           and prefix.customer_id = v_row.customer_id
           and prefix.provider = v_row.provider
           and prefix.prefix_hash = v_row.prefix_hash;

        v_claimed := v_claimed + 1;
        v_claimed_counts := jsonb_set(
            v_claimed_counts, array[v_customer_key],
            to_jsonb(coalesce((v_claimed_counts ->> v_customer_key)::integer, 0) + 1));
        perform public.warm_decision_record(
            v_row.organization_id, v_row.customer_id, v_row.provider,
            v_row.prefix_hash, 'pinged', v_p_return, v_floor, v_reserve,
            v_row.prefix_tokens, v_row.ewma_interarrival_s,
            v_row.arrival_count, v_pings_today, v_token, null,
            case when coalesce(p_holdout_fraction, 0) > 0
                then (1 - p_holdout_fraction)::numeric else null end);
        return next jsonb_build_object(
            'schema', 'brevitas.warm-claim.v1', 'status', 'claimed',
            'organization_id', v_row.organization_id,
            'customer_id', v_row.customer_id,
            'provider', v_row.provider,
            'prefix_hash', v_row.prefix_hash,
            'prefix_tokens', v_row.prefix_tokens,
            'provider_ttl_seconds', v_row.provider_ttl_seconds,
            'payload_ciphertext', v_row.payload_ciphertext,
            'credential_ciphertext', v_row.credential_ciphertext,
            'reserved_usd', v_reserve,
            'budget_day', v_day,
            'claim_token', v_token,
            -- The worker needs the pre-claim touch to turn its ping receipt
            -- into a TTL observation. Rows written before 202608090001 have no
            -- touch clock; last_seen_at is the best available lower bound and
            -- is exactly what the clock was seeded from.
            'last_touch_at', coalesce(v_row.last_touch_at, v_row.last_seen_at)
        );
    end loop;
    return;
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision
) from public, anon, authenticated;
grant execute on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision
) to service_role;

comment on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision
) is 'Claim due warm prefixes under the daily budget and per-customer caps, logging every candidate it scores -- claimed, denied and held out -- to public.warm_decision_log. A deterministic (organization, prefix, UTC day) share given by p_holdout_fraction is withheld as an unbiased control arm: it spends nothing and only advances next_due_at by the TTL horizon the ping would have set.';

commit;
