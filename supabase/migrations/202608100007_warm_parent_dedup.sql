-- Phase 1.5 prefix graph, part 2: shared-parent warming dedup.
--
-- THE OBSERVATION. A provider cache entry is keyed on CONTENT. Two customers
-- of one organization whose requests carry the same long system prompt are not
-- warming two entries -- they are warming ONE, twice, and paying for it twice.
-- Until 202608100006 there was no way to know that from the data: prefix_hash
-- is a whole-prefix digest, so two arms that share nine tenths of their bytes
-- and differ in the last turn hash to two unrelated values. The chain tree
-- gives the shared span a NAME (a node digest) and a cheap containment test (a
-- path prefix), and this migration spends that name exactly once: to stop
-- buying the same write twice.
--
-- WHAT IT DOES. Under p_parent_dedup (env BREVITAS_WARM_PARENT_DEDUP, default
-- OFF) the claim runs a pre-pass over its own candidate window. Members whose
-- chain paths descend from a common node that is (a) shared by at least two
-- window members, (b) large enough for the provider to cache at all, and (c)
-- believed warm right now, are grouped under the DEEPEST such node. The
-- cheapest member leads; it is claimed once, scored on the group's combined
-- return probability, and it reserves once. Every other member is deferred --
-- decision ''dedup_deferred'' -- to the horizon the leader's ping just bought,
-- and touches no ledger, no envelope and no ping counter.
--
-- WHAT IT DOES NOT DO. It denies nothing. Deferral requires that the leader
-- was ACTUALLY CLAIMED in the same invocation; a leader stopped by the ROI
-- floor, the pacing dual, the per-customer cap, the daily budget, the
-- envelope, the beta cap or the holdout coin authorizes no deferral, and its
-- members run every one of those gates themselves. The gates keep their order
-- and their meaning; nothing is inserted between the envelope reservation and
-- the ledger reservation; the holdout coin stays the last thing before a
-- reservation.
--
-- WHAT IT DOES NOT TOUCH. usage_log.verified_savings_usd, the settlement
-- sweep, warm_budget_ledger's reserve-then-settle arithmetic, the fee basis,
-- the claim-token fence. Warm spend stays never-billable. The single charge
-- this migration makes is the leader''s, and it is the SAME charge the leader
-- would have made alone; what changes is that the redundant second charge is
-- not made. dedup_node_digest on the log row is what lets 202608100008
-- reallocate that one charge across the beneficiaries -- MEASURED-ONLY, and
-- out of scope here.
--
-- Forward-only and idempotent.

-- REVERSE: DDL: drop the seventeen-argument public.warm_due_claim (integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric,boolean) and re-apply 202608100005_warm_beta_cap_guardrail.sql's sixteen-argument public.warm_due_claim verbatim with its revoke/grant/comment; drop the twenty-seven-argument public.warm_decision_record (uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,uuid,text,integer,text) and re-apply 202608100002_warm_index_claim_ordering.sql's twenty-three-argument public.warm_decision_record verbatim with its revoke/grant/comment; delete from public.warm_decision_log where decision = 'dedup_deferred'; alter table public.warm_decision_log drop constraint warm_decision_log_dedup_pairing_check, drop column dedup_node_digest, drop column dedup_group_size, drop column dedup_role, drop column dedup_group; drop constraint warm_decision_log_decision_check and re-add 202608100002_warm_index_claim_ordering.sql's vocabulary check verbatim

begin;

do $migration_precondition$
declare
    required_routine text;
begin
    foreach required_routine in array array[
        -- warm_prefix_observe is 202608100006's fourteen-argument form: this
        -- migration groups on the chain columns that form writes, and a
        -- deployment still on the eleven-argument observe has no chain to
        -- group on. Naming it here turns "the groups are always empty" into a
        -- refusal to apply.
        'public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric,text,jsonb,text,integer)'
    ] loop
        if to_regprocedure(required_routine) is null then
            raise exception using
                errcode = '55000',
                message = '202608100007 requires ' || required_routine;
        end if;
    end loop;
    -- Either shape satisfies this, and for the same reason the claim check
    -- below does: the twenty-three-argument form is what this migration
    -- carries forward and the twenty-seven-argument form is what it installs,
    -- so a second application must not fail on the artefact of the first.
    if to_regprocedure('public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric)') is null
       and to_regprocedure('public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,uuid,text,integer,text)') is null then
        raise exception using
            errcode = '55000',
            message = '202608100007 requires public.warm_decision_record';
    end if;
    -- Either shape satisfies this, and deliberately: the sixteen-argument form
    -- is what this migration carries forward, the seventeen-argument form is
    -- what it installs, so re-applying an applied migration must not fail its
    -- own precondition. Same rule as 202608100005:99-107.
    if to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric)') is null
       and to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric,boolean)') is null then
        raise exception using
            errcode = '55000',
            message = '202608100007 requires public.warm_due_claim';
    end if;
    -- The claim text carried forward below reads every one of these by name.
    -- Without them the copy would install a claim path that silently gates on
    -- a different set of ceilings than the one in force.
    if to_regclass('public.warm_prefixes') is null
       or to_regclass('public.warm_credentials') is null
       or to_regclass('public.warm_decision_log') is null
       or to_regclass('public.warm_budget_ledger') is null
       or to_regclass('public.warm_customer_state') is null
       or to_regclass('public.warm_modeling_suppression') is null
       or to_regclass('public.warm_customer_budget') is null
       or to_regclass('public.warm_control_savings_daily') is null
       or to_regclass('public.warm_org_mode') is null then
        raise exception using
            errcode = '55000',
            message = '202608100007 requires the Phase 1 warming tables';
    end if;
    -- 202608100006. The tree IS the grouping key; without it every candidate
    -- is ungroupable and the flag would be a silent no-op rather than a
    -- feature.
    if to_regclass('public.warm_prefix_node') is null
       or to_regclass('public.warm_prefix_edge') is null then
        raise exception using
            errcode = '55000',
            message = '202608100007 requires 202608100006 to be applied';
    end if;
    if not exists (
        select 1 from pg_catalog.pg_attribute attribute
         where attribute.attrelid = 'public.warm_prefixes'::regclass
           and attribute.attname = 'chain_path'
           and not attribute.attisdropped
    ) then
        raise exception using
            errcode = '55000',
            message = '202608100007 requires public.warm_prefixes.chain_path';
    end if;
end;
$migration_precondition$;

-- ---------------------------------------------------------------------------
-- THE GROUP STAMP ON THE DECISION LOG. Four nullable columns, null on every
-- row the pre-pass did not group, which is every row at p_parent_dedup =
-- false. dedup_node_digest is the load-bearing one: it names the SHARED NODE
-- the single charge was made against, which is what 202608100008 needs to
-- reallocate that charge across the arms that benefited from it. It is a
-- salted, organization-scoped HMAC of content -- it names bytes, never a
-- customer -- and it rides tenant erasure with the row it sits on.
-- ---------------------------------------------------------------------------
alter table public.warm_decision_log
    add column if not exists dedup_group uuid,
    add column if not exists dedup_role text,
    add column if not exists dedup_group_size integer,
    add column if not exists dedup_node_digest text;

do $dedup_constraints$
begin
    if not exists (
        select 1 from pg_catalog.pg_constraint
         where conrelid = 'public.warm_decision_log'::regclass
           and conname = 'warm_decision_log_dedup_role_check'
    ) then
        alter table public.warm_decision_log
            add constraint warm_decision_log_dedup_role_check
            check (dedup_role is null or dedup_role in ('leader', 'deferred'))
            not valid;
        alter table public.warm_decision_log
            validate constraint warm_decision_log_dedup_role_check;
    end if;
    if not exists (
        select 1 from pg_catalog.pg_constraint
         where conrelid = 'public.warm_decision_log'::regclass
           and conname = 'warm_decision_log_dedup_group_size_check'
    ) then
        -- >= 2 rather than >= 1: a group of one is not a group, and a stamped
        -- size of 1 would mean the pre-pass logged a group it did not form.
        alter table public.warm_decision_log
            add constraint warm_decision_log_dedup_group_size_check
            check (dedup_group_size is null or dedup_group_size >= 2)
            not valid;
        alter table public.warm_decision_log
            validate constraint warm_decision_log_dedup_group_size_check;
    end if;
    if not exists (
        select 1 from pg_catalog.pg_constraint
         where conrelid = 'public.warm_decision_log'::regclass
           and conname = 'warm_decision_log_dedup_node_digest_check'
    ) then
        alter table public.warm_decision_log
            add constraint warm_decision_log_dedup_node_digest_check
            check (dedup_node_digest is null
                   or dedup_node_digest ~ '^[0-9a-f]{64}$')
            not valid;
        alter table public.warm_decision_log
            validate constraint warm_decision_log_dedup_node_digest_check;
    end if;
    if not exists (
        select 1 from pg_catalog.pg_constraint
         where conrelid = 'public.warm_decision_log'::regclass
           and conname = 'warm_decision_log_dedup_pairing_check'
    ) then
        -- A group id without a role, or a role without a group id, is a half
        -- written stamp -- and 202608100008 would read it as one or the other.
        alter table public.warm_decision_log
            add constraint warm_decision_log_dedup_pairing_check
            check ((dedup_group is null) = (dedup_role is null))
            not valid;
        alter table public.warm_decision_log
            validate constraint warm_decision_log_dedup_pairing_check;
    end if;
end;
$dedup_constraints$;

comment on column public.warm_decision_log.dedup_group is
    'Per-invocation identifier of the shared-parent group this candidate belonged to, or null when 202608100007''s pre-pass did not group it (which is every row at p_parent_dedup = false). Stamped only on the leader''s ''pinged'' row and on its members'' ''dedup_deferred'' rows.';
comment on column public.warm_decision_log.dedup_role is
    '''leader'' on the one member of a shared-parent group that was actually claimed and reserved, ''deferred'' on a member whose ping the leader''s made redundant. Null off the dedup path.';
comment on column public.warm_decision_log.dedup_group_size is
    'Number of window members in the shared-parent group, always at least 2. The deferred members are size - 1 pings the organization did not pay for.';
comment on column public.warm_decision_log.dedup_node_digest is
    'public.warm_prefix_node.node_digest of the shared ancestor the group formed under: the node the single keep-alive was bought against. Salted, organization-scoped and content-derived -- it names bytes, never a customer. The attribution job (202608100008) reallocates the leader''s charge across the arms beneath this node; nothing in the billing path reads it.';

-- ---------------------------------------------------------------------------
-- THE VOCABULARY. 'dedup_deferred' joins the closed set. The existing
-- constraint is DISCOVERED rather than assumed (202608100002 named it, but a
-- database that predates that migration carries the auto-named one), dropped,
-- and re-added widened. A widening is satisfied by every existing row by
-- construction, so NOT VALID + VALIDATE is a formality that costs one scan and
-- buys the guarantee that it really was a widening.
-- ---------------------------------------------------------------------------
do $decision_vocabulary$
declare
    v_constraint_name text;
begin
    for v_constraint_name in
        select constraint_row.conname
          from pg_catalog.pg_constraint constraint_row
         where constraint_row.conrelid = 'public.warm_decision_log'::regclass
           and constraint_row.contype = 'c'
           and pg_catalog.pg_get_constraintdef(constraint_row.oid) like '%skipped_roi%'
    loop
        execute format(
            'alter table public.warm_decision_log drop constraint %I',
            v_constraint_name);
    end loop;
    alter table public.warm_decision_log
        add constraint warm_decision_log_decision_check
        check (decision in (
            'pinged', 'skipped_roi', 'budget_denied', 'cap_denied',
            'stopped', 'holdout', 'skipped_lambda', 'envelope_denied',
            'beta_denied', 'dedup_deferred'))
        not valid;
    alter table public.warm_decision_log
        validate constraint warm_decision_log_decision_check;
end;
$decision_vocabulary$;

-- ---------------------------------------------------------------------------
-- warm_decision_record (202608100002:258-345) + the four group columns.
--
-- The twenty-three-argument form is DROPPED, not overloaded: two candidates
-- for one name is how half the callers keep writing rows without the stamp.
-- All four new arguments DEFAULT NULL, so every existing twenty-three-argument
-- call site -- every gate in the claim loop -- resolves unchanged and writes
-- exactly the row it wrote before.
--
-- The validator is the TABLE'S OWN constraints and nothing more. It runs
-- inside warm_due_claim's advisory-locked transaction, so a stricter check
-- here aborts a claim the table would have accepted.
-- ---------------------------------------------------------------------------
drop function if exists public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric, numeric, numeric, numeric, numeric,
    numeric, numeric, numeric, numeric
);

create or replace function public.warm_decision_record(
    p_organization_id uuid,
    p_customer_id uuid,
    p_provider text,
    p_prefix_hash text,
    p_decision text,
    p_p_return numeric,
    p_roi_floor numeric,
    p_reserve_usd numeric,
    p_prefix_tokens integer,
    p_ewma_interarrival_s numeric,
    p_arrival_count integer,
    p_pings_today integer default null,
    p_claim_token uuid default null,
    p_rng_seed bigint default null,
    p_propensity numeric default null,
    -- Null on every one of these means "the index machinery was off for this
    -- row", which is the state a caller that predates Phase 1 produces.
    p_index_score numeric default null,
    p_index_density numeric default null,
    p_v_hit_usd numeric default null,
    p_chain_cost_usd numeric default null,
    p_c_belief_usd numeric default null,
    p_p_alive numeric default null,
    p_organic_multiplier numeric default null,
    p_lambda_index numeric default null,
    -- Null on every one of these means "this candidate was not grouped", which
    -- is the state p_parent_dedup = false produces for every candidate.
    p_dedup_group uuid default null,
    p_dedup_role text default null,
    p_dedup_group_size integer default null,
    p_dedup_node_digest text default null
) returns jsonb as $$
begin
    if p_organization_id is null or p_customer_id is null
       or p_provider not in ('anthropic', 'openai', 'deepseek')
       or coalesce(p_prefix_hash, '') !~ '^[0-9a-f]{64}$'
       or coalesce(p_decision, '') not in (
            'pinged', 'skipped_roi', 'budget_denied', 'cap_denied',
            'stopped', 'holdout', 'skipped_lambda', 'envelope_denied',
            'beta_denied', 'dedup_deferred')
       or coalesce(p_p_return, -1) not between 0 and 1
       or coalesce(p_roi_floor, -1) not between 0 and 1
       or coalesce(p_reserve_usd, -1) not between 0 and 99999999
       or coalesce(p_prefix_tokens, -1) not between 0 and 2000000000
       or coalesce(p_arrival_count, -1) < 0
       or coalesce(p_ewma_interarrival_s, 0) < 0
       or coalesce(p_pings_today, 0) < 0
       or coalesce(p_propensity, 0) not between 0 and 1
       or coalesce(p_v_hit_usd, 0) not between 0 and 99999999
       or coalesce(p_chain_cost_usd, 0) < 0
       or coalesce(p_c_belief_usd, 0) < 0
       or coalesce(p_p_alive, 0) not between 0 and 1
       or coalesce(p_organic_multiplier, 0) < 0
       or coalesce(p_lambda_index, 0) < 0
       or (p_claim_token is not null and p_decision <> 'pinged')
       or (p_dedup_role is not null
           and p_dedup_role not in ('leader', 'deferred'))
       or (p_dedup_group is null) <> (p_dedup_role is null)
       or coalesce(p_dedup_group_size, 2) < 2
       or (p_dedup_node_digest is not null
           and p_dedup_node_digest !~ '^[0-9a-f]{64}$') then
        raise exception 'warm decision arguments are invalid';
    end if;
    insert into public.warm_decision_log (
        organization_id, customer_id, provider, prefix_hash, decision,
        p_return, roi_floor, reserve_usd, prefix_tokens, ewma_interarrival_s,
        arrival_count, pings_today, claim_token, rng_seed, propensity,
        index_score, index_density, v_hit_usd, chain_cost_usd, c_belief_usd,
        p_alive, organic_multiplier, lambda_index,
        dedup_group, dedup_role, dedup_group_size, dedup_node_digest
    ) values (
        p_organization_id, p_customer_id, p_provider, p_prefix_hash, p_decision,
        p_p_return, p_roi_floor, p_reserve_usd, p_prefix_tokens,
        p_ewma_interarrival_s, p_arrival_count, p_pings_today, p_claim_token,
        p_rng_seed, p_propensity,
        p_index_score, p_index_density, p_v_hit_usd, p_chain_cost_usd,
        p_c_belief_usd, p_p_alive, p_organic_multiplier, p_lambda_index,
        p_dedup_group, p_dedup_role, p_dedup_group_size, p_dedup_node_digest
    );
    return jsonb_build_object(
        'schema', 'brevitas.warm-decision.v1', 'status', 'recorded',
        'decision', p_decision
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric, numeric, numeric, numeric, numeric,
    numeric, numeric, numeric, numeric, uuid, text, integer, text
) from public, anon, authenticated;
grant execute on function public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric, numeric, numeric, numeric, numeric,
    numeric, numeric, numeric, numeric, uuid, text, integer, text
) to service_role;

comment on function public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric, numeric, numeric, numeric, numeric,
    numeric, numeric, numeric, numeric, uuid, text, integer, text
) is 'Append one scored warming candidate to public.warm_decision_log. Validates exactly the table''s own constraints and nothing more, because it runs inside warm_due_claim''s advisory-locked transaction and a stricter check here would abort a claim the table would have accepted. The four trailing group arguments are 202608100007''s shared-parent stamp and are null on every candidate the dedup pre-pass did not group, which is every candidate at p_parent_dedup = false.';

-- --------------------------------------------------------------------------
-- warm_due_claim (202608100005:786-1648) + the shared-parent pre-pass, the
-- group''s combined return probability and the deferral. Every existing gate,
-- the lambda dual, the hazard read side, the envelope gate, the beta cap, the
-- reservation, the claim token and the holdout draw are carried forward
-- unchanged and in the same order; the deferral is inserted ABOVE the first
-- gate and nothing else moves.
--
-- The signature grows, so the sixteen-argument form is DROPPED first: two
-- candidate signatures for one name is how a caller silently keeps talking to
-- the old policy.
-- --------------------------------------------------------------------------
drop function if exists public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean, numeric
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
    p_roi_break_even_by_provider jsonb default null,
    p_holdout_fraction double precision default 0,
    -- false (and null, from a caller that predates Phase 1) means no index is
    -- computed, no lambda row is touched, the ordering key is all-NULL and this
    -- function behaves exactly as 202608100001's did.
    p_index_enabled boolean default false,
    -- Pacing gain and ceiling. Only read when p_index_enabled.
    p_lambda_eta numeric default 0.2,
    p_lambda_max numeric default 1000,
    -- false (and null, from a caller that predates 202608100003) means the
    -- hazard model is not read at all: p_return is the lifetime histogram
    -- ratio, the stop-loss predicate applies, and this function behaves exactly
    -- as 202608100002's did.
    p_hazard_v2 boolean default false,
    -- The trailing-28-day spend cap, as a multiple of control-verified
    -- savings. 0 (and null, from a caller that predates 202608100005) means
    -- the cap does not run at all -- not that the cap is zero. A zero cap
    -- would deny every candidate, which is what an operator who armed the cap
    -- with no control data should get and what an operator who never armed it
    -- must never get.
    p_beta numeric default 0,
    -- SHARED-PARENT DEDUP (202608100007), default OFF and null-safe for a
    -- caller that predates it. At false the pre-pass does not run, no temp
    -- table is created, every group local stays null and this function is
    -- BEHAVIOURALLY IDENTICAL to 202608100005's -- which is the only claim
    -- a flag on a claim path is allowed to make.
    p_parent_dedup boolean default false
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
    v_floor numeric;
    v_reserve numeric;
    v_reserved numeric;
    v_spent numeric;
    v_token uuid;
    v_holdout_digest bytea;
    v_holdout_bucket bigint;
    -- ENVELOPE (202608100004). The period is derived from the SAME UTC day
    -- v_day is, so a claim can only ever move money on the month its budget_day
    -- belongs to and warm_ping_settle -- which derives it from p_budget_day --
    -- lands on the same row even when the settle happens the next month.
    v_period date := date_trunc(
        'month', ((clock_timestamp() at time zone 'utc')::date)::timestamp)::date;
    v_env_found boolean;
    v_env numeric;
    v_env_reserved numeric;
    v_env_day date;
    v_env_spent numeric;
    -- Nulls coalesced once, so the formula below never has to repeat the
    -- default and the two spellings cannot drift apart.
    v_eta numeric := coalesce(p_lambda_eta, 0.2);
    v_lambda_ceiling numeric := coalesce(p_lambda_max, 1000);
    -- Index belief snapshot for the candidate in hand. All null when the
    -- machinery is off; reset every iteration because plpgsql locals survive it.
    v_index numeric;
    v_index_density numeric;
    -- Index economics (202608100002). Derived per candidate from
    -- warm_prefixes.ping_reserve_usd, never from the floored
    -- ledger reservation.
    v_write_mult numeric;
    v_read_fraction numeric;
    v_price_base numeric;
    v_v_hit numeric;
    v_chain_cost numeric;
    v_c_belief numeric;
    v_p_alive numeric;
    v_organic numeric;
    v_p_eff numeric;
    v_n_chain integer;
    -- The pacing dual, per (org, provider), updated at most once per
    -- invocation. v_lambda is the value in force for the candidate in hand.
    v_lambda numeric;
    v_lambda_by_pair jsonb := '{}'::jsonb;
    v_pair_key text;
    v_lambda_old numeric;
    v_lambda_spend numeric;
    v_lambda_u numeric;
    v_lambda_exponent numeric;
    -- HAZARD READ SIDE (202608100003). All null/zero unless p_hazard_v2 found a
    -- state row for this candidate's customer, which is what makes the
    -- no-state fallback identical to 202608100002's flat priors.
    v_state public.warm_customer_state%rowtype;
    v_org_state public.warm_customer_state%rowtype;
    v_hazard_found boolean;
    v_h_org numeric;
    v_h numeric;
    v_hazard_p_alive numeric;
    v_hazard_chain integer;
    v_bg_x numeric;
    v_bg_tx numeric;
    v_bg_t numeric;
    v_bg_z numeric;
    v_chain_age numeric;
    v_chain_gap numeric;
    v_chain_tau numeric;
    v_chain_f numeric;
    v_chain_imax numeric;
    -- THE GUARDRAIL (202608100005). Per candidate, from the LEFT JOIN below:
    -- true when the worker froze this (organization, provider) pair. Frozen
    -- means FLAT PRIORS, not "warming off" -- see the hazard block.
    v_frozen boolean;
    -- THE BETA CAP (202608100005). Cap and running trailing-window spend, per
    -- (organization, provider), resolved once per invocation. v_beta_w28 rises
    -- locally with every reservation this invocation makes, so a single batch
    -- cannot walk past the cap by reading a stale ledger sum once.
    v_beta_key text;
    v_beta_cap numeric;
    v_beta_w28 numeric;
    v_beta_cap_by_pair jsonb := '{}'::jsonb;
    v_beta_w28_by_pair jsonb := '{}'::jsonb;
    v_beta_c28 numeric;
    -- SHARED-PARENT DEDUP (202608100007). All null on every candidate the
    -- pre-pass did not group, which is every candidate at p_parent_dedup =
    -- false, so the reset block below is what keeps a flag-off row from
    -- inheriting the previous candidate's group.
    v_parent_dedup boolean := coalesce(p_parent_dedup, false);
    v_dedup_group uuid;
    v_dedup_role text;
    v_dedup_group_size integer;
    v_dedup_node_digest text;
    v_dedup_node_token_cum integer;
    v_dedup_peer_h numeric[];
    v_dedup_peer numeric;
    v_dedup_peer_product numeric;
    -- DIAGNOSTIC ONLY. The group-combined return probability. It is reported
    -- on the leader's claim payload and gates NOTHING; see the block that
    -- computes it for why.
    v_p_group numeric;
    -- Group ids whose LEADER was actually claimed in this invocation.
    -- Deferral reads only this: a leader denied by any gate, or not yet
    -- visited, leaves its members to proceed exactly as they would have
    -- without the flag. Dedup removes provably redundant pings; it never
    -- denies a ping the old policy would have made.
    v_dedup_leaders jsonb := '{}'::jsonb;
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
       or coalesce(p_holdout_fraction, 0) not between 0 and 1
       or v_eta not between 0.01 and 2
       or v_lambda_ceiling not between 0 and 1000000
       or coalesce(p_beta, 0) not between 0 and 100 then
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

    -- CANDIDATE WINDOW. The predicate is 202608090001's, unchanged; only the
    -- order within the window moves. The key is THE INDEX ITSELF -- the same
    -- arithmetic the loop performs below (with n_chain = 0, p_alive = 1 and
    -- organic = 1, which is all this migration can produce), evaluated inline
    -- so the window is index-selected rather than FIFO-selected -- a window
    -- ordered by next_due_at and then re-sorted would still be the soonest-due
    -- claim_limit*4 rows, which is the behaviour this replaces.
    --
    -- NOT index per reserved dollar. The index is ALREADY value per
    -- belief-dollar (it divides by c_belief by construction), so dividing it a
    -- second time by the reservation makes the key value-per-dollar-squared:
    -- under a binding budget that systematically prefers small cheap arms over
    -- the large valuable ones the budget exists to allocate. The reservation
    -- enters the claim exactly once, at the budget gate, where it belongs.
    --
    -- The CASE short-circuits: at p_index_enabled = false nothing is computed,
    -- the key is NULL for every row, and the effective order is
    -- (next_due_at, prefix_hash). prefix_hash is the documented tiebreak -- ties
    -- on next_due_at were previously broken by whatever the plan produced.
    --
    -- 202608100003: this expression stays the LIFETIME-HISTOGRAM index even
    -- under p_hazard_v2, and deliberately. The window is a recall heuristic
    -- over claim_limit*4 rows; the loop below rescores every row it visits with
    -- the hazard model and that rescore is what gates. Pulling the hazard maps
    -- into the ORDER BY would mean joining warm_customer_state twice and
    -- evaluating two jsonb lookups per candidate row inside a sort key, to
    -- reorder a window whose membership the flag already widened. Phase 2 can
    -- revisit it with the state row joined once.

    -- ----------------------------------------------------------------------
    -- THE SHARED-PARENT PRE-PASS (202608100007). Runs ONCE per invocation,
    -- before the loop, and only under the flag.
    --
    -- WHAT IT IS. Two customers of the same organization whose requests share
    -- a long system prompt share a PROVIDER CACHE ENTRY for that shared span:
    -- the provider keys on content, not on who sent it. 202608100006's chain
    -- tree is what makes that shareable span nameable -- a chain node is a
    -- content-derived block boundary, and two arms under the same node have
    -- provably identical bytes up to it. One keep-alive on the shared node
    -- therefore keeps the entry warm for EVERY arm beneath it, and the second
    -- ping is not a second unit of warmth, it is a duplicate purchase.
    --
    -- WHAT IT IS NOT. It does not deny anything. A group's leader is claimed
    -- exactly as it would have been; the members are DEFERRED to the horizon
    -- the leader's ping just bought them, and only after the leader was
    -- actually claimed in this same invocation. Every existing gate keeps its
    -- position and its meaning.
    --
    -- WINDOW IDENTITY. The temp table is materialized from the SAME subquery,
    -- in the SAME order, under the SAME limit and inside the SAME transaction
    -- snapshot as the loop below, so a member of one is a member of the other.
    -- Anything the two could still disagree on (a tie at the limit boundary
    -- between rows sharing sort key, next_due_at and prefix_hash) can only
    -- leave a row UNGROUPED, which is the no-op.
    -- ----------------------------------------------------------------------
    if v_parent_dedup then
        -- to_regclass rather than DROP ... IF EXISTS: the latter raises a
        -- NOTICE for a temp schema that does not exist yet, and this runs on
        -- every invocation. The drop is needed at all because two claims can
        -- share one transaction, and ON COMMIT DROP only fires at commit.
        if to_regclass('pg_temp.warm_dedup_window') is not null then
            drop table pg_temp.warm_dedup_window;
        end if;
        create temp table warm_dedup_window on commit drop as
        select candidate.organization_id, candidate.customer_id,
               candidate.provider, candidate.prefix_hash,
               candidate.prefix_tokens, candidate.provider_ttl_seconds,
               candidate.chain_path, candidate.chain_salt_version,
               candidate.hour_histogram, candidate.arrival_count
          from (
            select prefix.*, cred.credential_ciphertext, cred.daily_budget_usd,
                   cred.max_pings_per_customer_day,
                   -- Absent row means 'learned', so an empty warm_org_mode
                   -- makes this join and everything downstream of it a no-op.
                   coalesce(mode.mode, 'learned') = 'frozen' as warm_frozen,
                   case when p_index_enabled then
                       (case
                            when coalesce(
                                (p_roi_break_even_by_provider ->> prefix.provider)::numeric,
                                p_roi_break_even_p) <= 1e-9
                            then 1000000::numeric
                            else least(1000000, greatest(-1000000,
                                least(1, coalesce(
                                    (prefix.hour_histogram ->> v_bucket_key)::numeric, 0)
                                    / greatest(prefix.arrival_count, 1))
                                / coalesce(
                                    (p_roi_break_even_by_provider ->> prefix.provider)::numeric,
                                    p_roi_break_even_p)
                                - 1))
                        end)
                   end as sort_index
              from public.warm_prefixes prefix
              join public.warm_credentials cred
                on cred.organization_id = prefix.organization_id
               and cred.provider = prefix.provider
              left join public.warm_org_mode mode
                on mode.organization_id = prefix.organization_id
               and mode.provider = prefix.provider
             where prefix.state = 'active'
               and prefix.next_due_at <= v_now
               and prefix.expires_at > v_now
               -- STOP-LOSS RETIREMENT (202608100003). The counter and every
               -- writer of it (warm_ping_settle, warm_prefix_observe) are
               -- untouched; only this gate is bypassed under the flag, because
               -- P(alive) answers the same question continuously and recovers
               -- when the customer returns, which a monotone counter cannot.
               --
               -- p_index_enabled IS PART OF THE PREDICATE, not decoration.
               -- P(alive) -- the thing that replaces the counter -- only ever
               -- reaches a decision through the index block, which runs under
               -- p_index_enabled. With the hazard flag on and the index flag
               -- off, retiring the counter here would remove the churn
               -- stop-loss and put NOTHING in its place: a dead prefix would be
               -- re-claimed forever. hazard_v2 is an EXTENSION of the index
               -- policy and is inert without it. At p_hazard_v2 = false, or at
               -- p_index_enabled = false, the predicate is 202608100002's,
               -- exactly.
               -- 202608100005 SUPERSEDES 202608100003's form: the
               -- stop-loss is retired in favour of P(alive) only while the
               -- pair is LEARNED. A frozen pair is scored at flat priors, and
               -- flat priors have no P(alive) to retire it with, so the
               -- counter has to come back with them.
               and ((p_hazard_v2 and p_index_enabled
                     and coalesce(mode.mode, 'learned') = 'learned')
                    or prefix.consecutive_misses < p_stop_loss)
               and coalesce(prefix.ewma_interarrival_s <= p_max_gap_seconds, true)
               and cred.enabled
               and cred.credential_state = 'active'
          ) candidate
         order by candidate.sort_index desc nulls last,
                  candidate.next_due_at asc,
                  candidate.prefix_hash asc
         limit p_claim_limit * 4;

        if to_regclass('pg_temp.warm_dedup_groups') is not null then
            drop table pg_temp.warm_dedup_groups;
        end if;
        create temp table warm_dedup_groups on commit drop as
        with member as (
            -- Only arms 202608100006 actually stamped. A null chain_path is a
            -- request whose chain was absent (no exact tokenizer, no salt,
            -- kill switch, unserialisable element) and it is UNGROUPABLE by
            -- construction rather than by policy.
            select window_row.*,
                   least(1, coalesce(
                       (window_row.hour_histogram ->> v_bucket_key)::numeric, 0)
                       / greatest(window_row.arrival_count, 1)) as h
              from pg_temp.warm_dedup_window window_row
             where window_row.chain_path is not null
               and window_row.chain_salt_version is not null
        ),
        ancestor as (
            -- Every ancestor prefix of the member's path at label-depth >= 4:
            -- two header labels (scheme, salt version), the root, then at
            -- least one BLOCK label. Depth 3 is the seed alone -- shared by
            -- every arm with the same model/ttl/vary and carrying no content
            -- -- so grouping there would group strangers. The member's own
            -- full path is included: two arms at the identical leaf are the
            -- most shareable case there is.
            select member_row.organization_id, member_row.customer_id,
                   member_row.provider, member_row.prefix_hash,
                   member_row.prefix_tokens, member_row.provider_ttl_seconds,
                   member_row.chain_salt_version, member_row.h,
                   label_depth.d,
                   array_to_string(
                       (string_to_array(member_row.chain_path, '.'))[1:label_depth.d],
                       '.') as anc_path
              from member member_row,
                   lateral generate_series(4, coalesce(array_length(
                       string_to_array(member_row.chain_path, '.'), 1), 0))
                       as label_depth(d)
        ),
        qualified as (
            select ancestor_row.*, node.node_digest, node.token_cum
              from ancestor ancestor_row
              join public.warm_prefix_node node
                on node.organization_id = ancestor_row.organization_id
               and node.provider = ancestor_row.provider
               and node.salt_version = ancestor_row.chain_salt_version
               and node.path is not null
               and node.path::text = ancestor_row.anc_path
             where
               -- (b) THE PROVIDER FLOOR, ON A DEFENSIVE BASIS. Below the
               -- provider's minimum the provider caches nothing, so a
               -- keep-alive on this node buys no shared warmth and the group
               -- would be an accounting fiction. Hard-coded on purpose: a
               -- caller-supplied floor is a caller-supplied way to manufacture
               -- groups.
               --
               -- token_cum is NOT counted on the provider's basis. It is
               -- count_tokens() over canonical JSON, which on measured real
               -- prefixes runs 4-12% above the text-only count the provider
               -- actually sees (and far higher on many short elements).
               -- Comparing it to the raw minimum admits nodes whose true
               -- cacheable span is ~915-985 tokens -- under the minimum, so
               -- the provider caches nothing and the group is precisely the
               -- fiction this floor exists to prevent. The 1.25 headroom
               -- covers the measured band with margin; erring high costs a few
               -- marginal groups, erring low costs the whole premise.
               node.token_cum >= ceil(1.25 * case ancestor_row.provider
                   when 'anthropic' then 1024
                   when 'openai' then 1024
                   when 'deepseek' then 64
                   else 1024 end)
               -- (a) AT LEAST TWO window members at or below this node, in the
               -- same (organization, provider, salt version) key space. The
               -- seed binds provider, model, ttl tier, vary headers and the
               -- cache-identity extras, so a shared block label past the root
               -- already implies all of them -- there is nothing left to
               -- re-check, and re-checking it would only invite drift.
               --
               -- The descendant test is a LEFT() prefix comparison, not LIKE:
               -- a collision-escaped label carries an underscore, and '_' is a
               -- LIKE wildcard that would silently over-match sibling paths.
               and (select count(*) from member peer
                     where peer.organization_id = ancestor_row.organization_id
                       and peer.provider = ancestor_row.provider
                       and peer.chain_salt_version = ancestor_row.chain_salt_version
                       and (peer.chain_path = ancestor_row.anc_path
                            or left(peer.chain_path,
                                    length(ancestor_row.anc_path) + 1)
                               = ancestor_row.anc_path || '.')) >= 2
               -- (c) WARMED. Some arm under this node was touched inside this
               -- member's own TTL, so the shared entry the group would ride is
               -- believed to exist. Without this a "group" is two cold arms
               -- agreeing to send one ping between them, which is exactly the
               -- ping the old policy would have made and this one would not.
               and exists (
                    select 1 from public.warm_prefixes warm
                     where warm.organization_id = ancestor_row.organization_id
                       and warm.provider = ancestor_row.provider
                       and warm.chain_path is not null
                       and (warm.chain_path = ancestor_row.anc_path
                            or left(warm.chain_path,
                                    length(ancestor_row.anc_path) + 1)
                               = ancestor_row.anc_path || '.')
                       and warm.last_touch_at >= v_now - make_interval(
                               secs => ancestor_row.provider_ttl_seconds))
        ),
        deepest as (
            -- V(m): the DEEPEST qualifying ancestor. Deeper means more shared
            -- bytes, so the group that shares the most is the group that
            -- forms.
            select distinct on (organization_id, customer_id, provider, prefix_hash)
                   organization_id, customer_id, provider, prefix_hash,
                   chain_salt_version, prefix_tokens, h, anc_path,
                   node_digest, token_cum
              from qualified
             order by organization_id, customer_id, provider, prefix_hash, d desc
        ),
        grouped as (
            select deepest_row.*,
                   count(*) over group_window as group_size,
                   -- Leader = the CHEAPEST member. The whole group rides one
                   -- write, so buy the smallest one; prefix_hash breaks ties
                   -- deterministically.
                   row_number() over (partition by deepest_row.organization_id,
                                      deepest_row.provider,
                                      deepest_row.chain_salt_version,
                                      deepest_row.anc_path
                                      order by deepest_row.prefix_tokens asc,
                                               deepest_row.prefix_hash asc)
                       as leader_rank,
                   -- Hazard membership is capped at 64 members ordered by
                   -- prefix_hash. Members past the cap still DEFER (deferral
                   -- is a correctness property of the shared entry) but
                   -- contribute no probability mass, which can only understate
                   -- the group's return probability. Conservative in the
                   -- direction that spends less.
                   row_number() over (partition by deepest_row.organization_id,
                                      deepest_row.provider,
                                      deepest_row.chain_salt_version,
                                      deepest_row.anc_path
                                      order by deepest_row.prefix_hash asc)
                       as hazard_rank
              from deepest deepest_row
            window group_window as (partition by deepest_row.organization_id,
                                    deepest_row.provider,
                                    deepest_row.chain_salt_version,
                                    deepest_row.anc_path)
        ),
        sized as (
            -- A group of one is not a group. Condition (a) counts members at
            -- or below a node; a member can still be alone at its own DEEPEST
            -- node, and a size-1 group must reduce exactly to current
            -- behaviour rather than log a group of one.
            select * from grouped where group_size >= 2
        ),
        identified as (
            select sized_row.*, group_id.dedup_group
              from sized sized_row
              join (select organization_id, provider, chain_salt_version,
                           anc_path, gen_random_uuid() as dedup_group
                      from sized
                     group by organization_id, provider, chain_salt_version,
                              anc_path) group_id
                on group_id.organization_id = sized_row.organization_id
               and group_id.provider = sized_row.provider
               and group_id.chain_salt_version = sized_row.chain_salt_version
               and group_id.anc_path = sized_row.anc_path
        )
        select identified_row.organization_id, identified_row.customer_id,
               identified_row.provider, identified_row.prefix_hash,
               identified_row.dedup_group,
               case when identified_row.leader_rank = 1
                    then 'leader' else 'deferred' end as dedup_role,
               identified_row.group_size::integer as dedup_group_size,
               identified_row.node_digest as dedup_node_digest,
               identified_row.token_cum as dedup_node_token_cum,
               -- The peers' survival factors (1 - h_i), carried as an ARRAY
               -- rather than a product: the leader's combined hazard is
               -- multiplied out in numeric inside the loop, so the value the
               -- ROI gate sees is exact rather than exp(sum(ln)))-approximate.
               case when identified_row.leader_rank = 1 then (
                    select array_agg(1 - peer.h order by peer.prefix_hash)
                      from identified peer
                     where peer.organization_id = identified_row.organization_id
                       and peer.provider = identified_row.provider
                       and peer.chain_salt_version = identified_row.chain_salt_version
                       and peer.anc_path = identified_row.anc_path
                       and peer.leader_rank > 1
                       and peer.hazard_rank <= 64) end as dedup_peer_h
          from identified identified_row;
    end if;

    for v_row in
        select candidate.*
          from (
            select prefix.*, cred.credential_ciphertext, cred.daily_budget_usd,
                   cred.max_pings_per_customer_day,
                   -- Absent row means 'learned', so an empty warm_org_mode
                   -- makes this join and everything downstream of it a no-op.
                   coalesce(mode.mode, 'learned') = 'frozen' as warm_frozen,
                   case when p_index_enabled then
                       (case
                            when coalesce(
                                (p_roi_break_even_by_provider ->> prefix.provider)::numeric,
                                p_roi_break_even_p) <= 1e-9
                            then 1000000::numeric
                            else least(1000000, greatest(-1000000,
                                least(1, coalesce(
                                    (prefix.hour_histogram ->> v_bucket_key)::numeric, 0)
                                    / greatest(prefix.arrival_count, 1))
                                / coalesce(
                                    (p_roi_break_even_by_provider ->> prefix.provider)::numeric,
                                    p_roi_break_even_p)
                                - 1))
                        end)
                   end as sort_index
              from public.warm_prefixes prefix
              join public.warm_credentials cred
                on cred.organization_id = prefix.organization_id
               and cred.provider = prefix.provider
              left join public.warm_org_mode mode
                on mode.organization_id = prefix.organization_id
               and mode.provider = prefix.provider
             where prefix.state = 'active'
               and prefix.next_due_at <= v_now
               and prefix.expires_at > v_now
               -- STOP-LOSS RETIREMENT (202608100003). The counter and every
               -- writer of it (warm_ping_settle, warm_prefix_observe) are
               -- untouched; only this gate is bypassed under the flag, because
               -- P(alive) answers the same question continuously and recovers
               -- when the customer returns, which a monotone counter cannot.
               --
               -- p_index_enabled IS PART OF THE PREDICATE, not decoration.
               -- P(alive) -- the thing that replaces the counter -- only ever
               -- reaches a decision through the index block, which runs under
               -- p_index_enabled. With the hazard flag on and the index flag
               -- off, retiring the counter here would remove the churn
               -- stop-loss and put NOTHING in its place: a dead prefix would be
               -- re-claimed forever. hazard_v2 is an EXTENSION of the index
               -- policy and is inert without it. At p_hazard_v2 = false, or at
               -- p_index_enabled = false, the predicate is 202608100002's,
               -- exactly.
               -- 202608100005 SUPERSEDES 202608100003's form: the
               -- stop-loss is retired in favour of P(alive) only while the
               -- pair is LEARNED. A frozen pair is scored at flat priors, and
               -- flat priors have no P(alive) to retire it with, so the
               -- counter has to come back with them.
               and ((p_hazard_v2 and p_index_enabled
                     and coalesce(mode.mode, 'learned') = 'learned')
                    or prefix.consecutive_misses < p_stop_loss)
               and coalesce(prefix.ewma_interarrival_s <= p_max_gap_seconds, true)
               and cred.enabled
               and cred.credential_state = 'active'
          ) candidate
         order by candidate.sort_index desc nulls last,
                  candidate.next_due_at asc,
                  candidate.prefix_hash asc
         limit p_claim_limit * 4
    loop
        exit when v_claimed >= p_claim_limit;
        v_pings_today := null;
        v_token := null;
        v_dedup_group := null;
        v_dedup_role := null;
        v_dedup_group_size := null;
        v_dedup_node_digest := null;
        v_dedup_node_token_cum := null;
        v_dedup_peer_h := null;
        v_p_group := null;
        if v_parent_dedup then
            select groups.dedup_group, groups.dedup_role,
                   groups.dedup_group_size, groups.dedup_node_digest,
                   groups.dedup_node_token_cum, groups.dedup_peer_h
              into v_dedup_group, v_dedup_role, v_dedup_group_size,
                   v_dedup_node_digest, v_dedup_node_token_cum,
                   v_dedup_peer_h
              from pg_temp.warm_dedup_groups groups
             where groups.organization_id = v_row.organization_id
               and groups.customer_id = v_row.customer_id
               and groups.provider = v_row.provider
               and groups.prefix_hash = v_row.prefix_hash;
        end if;
        -- Every index local is reset, not carried: a candidate that exits
        -- before the lambda block would otherwise log the previous candidate's
        -- dual.
        v_index := null;
        v_index_density := null;
        v_v_hit := null;
        v_chain_cost := null;
        v_c_belief := null;
        v_p_alive := null;
        v_organic := null;
        v_lambda := null;

        v_p_return := least(1, coalesce(
            (v_row.hour_histogram ->> v_bucket_key)::numeric, 0)
            / greatest(v_row.arrival_count, 1));
        v_break_even := coalesce(
            (p_roi_break_even_by_provider ->> v_row.provider)::numeric,
            p_roi_break_even_p);

        -- THE HAZARD MODEL (202608100003). Read only under the flag, and only
        -- when this customer HAS a state row that is not suppressed. With no
        -- row -- a new customer, or one erased and suppressed -- every value
        -- below keeps its 202608100002 default: the lifetime histogram
        -- p_return, p_alive = 1 and n_chain = 0, which is the flat-prior form
        -- Task A proved equal to v1. That fallback IS the cold-start story, and
        -- it is why turning this flag on cannot change a decision for a
        -- customer the model has never seen.
        v_hazard_found := false;
        v_hazard_p_alive := null;
        v_hazard_chain := 0;
        -- FROZEN IS FLAT PRIORS, NOT A SECOND SYSTEM (202608100005). The state
        -- row is not read, so p_return stays the lifetime histogram, p_alive
        -- stays 1 and the chain stays 0 -- exactly the fallback a customer the
        -- model has never seen gets, which 202608100002 proved equal to v1.
        -- The index and the pacing dual keep running on top of it.
        v_frozen := coalesce(v_row.warm_frozen, false);
        if p_hazard_v2 and not v_frozen then
            select * into v_state
              from public.warm_customer_state state
             where state.organization_id = v_row.organization_id
               and state.customer_id = v_row.customer_id
               and state.provider = v_row.provider;
            v_hazard_found := found;
            if v_hazard_found and exists (
                select 1 from public.warm_modeling_suppression suppression
                 where suppression.organization_id = v_row.organization_id
                   and suppression.customer_id = v_row.customer_id
            ) then
                -- An erased subject is scored as if the model had never seen
                -- them, which is the same treatment a brand-new customer gets.
                v_hazard_found := false;
            end if;
        end if;
        if v_hazard_found then
            -- Absent aggregate row: SELECT INTO leaves every field null and the
            -- coalesces below read it as zero mass, so the org term degenerates
            -- to the uniform prior rather than failing.
            select * into v_org_state
              from public.warm_customer_state state
             where state.organization_id = v_row.organization_id
               and state.customer_id = '00000000-0000-0000-0000-000000000000'::uuid
               and state.provider = v_row.provider;
            -- 0.25 / 42.0 = 1/168: one arrival per week per bucket-hour. This
            -- is the "global" level of the customer -> org -> global shrinkage
            -- and it is a CONSTANT on purpose -- content-free, identical for
            -- every tenant, so no cross-organization behaviour is ever pooled.
            v_h_org := (coalesce((v_org_state.hazard_n ->> v_bucket_key)::numeric, 0) + 0.25)
                     / (coalesce((v_org_state.hazard_e ->> v_bucket_key)::numeric, 0) + 42.0);
            -- 8 pseudo-exposure-hours of organization-prior weight: a customer
            -- with a full week of exposure in this bucket dominates its own
            -- estimate, one with an hour of it does not.
            v_h := (coalesce((v_state.hazard_n ->> v_bucket_key)::numeric, 0) + 8.0 * v_h_org)
                 / (coalesce((v_state.hazard_e ->> v_bucket_key)::numeric, 0) + 8.0);
            -- Single-bucket approximation: a TTL window spanning more than one
            -- hour-of-week bucket is priced entirely at the bucket it starts
            -- in. Phase-2 integrates the rate across the window. The exponent
            -- clamp only keeps exp() inside numeric's domain -- at -50 the
            -- value is 1 to twenty digits.
            --
            -- This value REPLACES p_return everywhere below: in the v1 ROI gate
            -- and in the index alike, and warm_decision_log.p_return records
            -- what was actually used, so a row is never scored on one number
            -- and logged with another.
            v_p_return := least(1, greatest(0, 1 - exp(greatest(-50,
                -v_h * v_row.provider_ttl_seconds / 3600.0))));

            -- P(ALIVE), BG/NBD with fixed hyperparameters r = 0.5,
            -- alpha = 7 days, a = 1, b = 2.5. x is the raw undecayed arrival
            -- count, t_x the age of the last arrival relative to the first, T
            -- the age of the record. The ratio ((alpha + T)/(alpha + t_x))
            -- raised to (r + x) is the likelihood that a customer this active
            -- would have gone this quiet by chance; the more arrivals, the less
            -- forgiving the silence.
            v_bg_x := coalesce(v_state.events_total, 0);
            v_bg_tx := greatest(0, extract(epoch from
                (v_state.last_seen_at - v_state.first_seen_at)) / 86400.0);
            v_bg_t := greatest(v_bg_tx, extract(epoch from
                (v_now - v_state.first_seen_at)) / 86400.0);
            v_bg_z := ln(1.0 / (2.5 + greatest(v_bg_x - 1, 0)))
                + least(50, 0.5 + v_bg_x) * ln((7.0 + v_bg_t) / (7.0 + v_bg_tx));
            v_hazard_p_alive := greatest(0.01, least(1.0,
                1.0 / (1.0 + exp(least(50, greatest(-50, v_bg_z))))));

            -- THE CHAIN. A keep-alive now commits the organization to every
            -- further keep-alive before the expected next arrival, so the index
            -- must price the whole chain, not this one ping.
            --
            --   E_gap = expected seconds to the next arrival, net of how long
            --           this session has already been idle
            --   tau   = the keep-alive period (TTL less the safety margin)
            --   f     = the provider read-cost fraction, recovered from the
            --           break-even b = f/(1-f) as f = b/(1+b)
            --   I_max = tau * (1/f - 1): the longest gap a chain can bridge
            --           before its cost exceeds everything a hit could save
            --
            -- The truncation at I_max is what makes "never warm a dead session"
            -- ARITHMETIC rather than policy: at E_gap >= I_max the chain alone
            -- drives index = p_eff/b - 1 - n_chain below zero for every p <= 1.
            v_chain_age := greatest(0, extract(epoch from
                (v_now - v_state.last_seen_at)));
            v_chain_gap := least(2592000, greatest(0,
                3600.0 / greatest(v_h, 0.000001) - v_chain_age));
            v_chain_tau := greatest(1,
                v_row.provider_ttl_seconds - p_safety_margin_seconds);
            v_chain_f := v_break_even / (1 + v_break_even);
            v_chain_imax := v_chain_tau
                * greatest(0, 1.0 / greatest(v_chain_f, 0.000000001) - 1);
            v_hazard_chain := greatest(0,
                ceil(least(v_chain_gap, v_chain_imax) / v_chain_tau)::integer - 1);
        end if;

        -- THE GROUP'S RETURN PROBABILITY (202608100007), DIAGNOSTIC ONLY.
        -- The leader's ping is arguably worth a return by ANY member of
        -- the group, because any member's arrival reads the shared entry
        -- the ping refreshed:
        --
        --   p_group = 1 - (1 - p_own) * prod_{i != leader} (1 - h_i)
        --
        -- h_i is member i's own lifetime-histogram return probability for
        -- this hour bucket, read off the window row -- no extra query and
        -- no model.
        --
        -- IT DOES NOT GATE. Substituting p_group for p_own before the ROI
        -- floor lets the flag BUY a ping: two arms at p_own = 0.06 under a
        -- 0.11 floor are both skipped_roi with the flag off, and combine to
        -- p_group = 0.1164 -- over the floor -- with it on. That strictly
        -- INCREASES warm spend, contradicting this migration's own promise
        -- that the single charge it makes is the SAME charge the leader
        -- would have made alone. Independence across members is an
        -- approximation and a generous one; it is not a licence to spend.
        -- Every gate below therefore runs on p_own, exactly as it does with
        -- the flag off, and dedup can only ever REMOVE a redundant ping.
        -- p_group rides out on the leader's claim payload for the analysis
        -- that would justify promoting it to a gate; nothing reads it yet.
        --
        -- At group size 1 the product is empty and p_group = p_own exactly.
        if v_dedup_role = 'leader' and v_dedup_peer_h is not null then
            v_dedup_peer_product := 1;
            foreach v_dedup_peer in array v_dedup_peer_h loop
                v_dedup_peer_product := v_dedup_peer_product * v_dedup_peer;
            end loop;
            v_p_group := least(1, greatest(0,
                1 - (1 - v_p_return) * v_dedup_peer_product));
        end if;

        v_floor := case when v_row.arrival_count < p_roi_min_arrivals
            then p_roi_min_p else v_break_even end;
        v_reserve := greatest(
            v_row.ping_reserve_usd,
            round(p_reserve_usd_per_mtok * v_row.prefix_tokens / 1000000.0, 10));

        -- THE INDEX, computed above the first gate so that even a candidate the
        -- ROI floor rejects records what the index thought of it.
        --
        -- p_alive: P(the customer session is still alive). No producer here.
        --
        -- organic_multiplier: the incremental-probability netting multiplier
        -- P(warm|act) - P(warm|skip) normalized against P(return). Until the
        -- (org, prefix) control arm produces per-(provider, hour) organic
        -- baselines there is no unbiased estimator for it, so it is pinned to
        -- 1.0 (no netting) and logged, so the day it becomes real is visible in
        -- the data. Pinning it at 1.0 can only OVERSTATE the index, and the
        -- index gates spending, not billing -- billing continues to flow
        -- exclusively through verified_savings/settlement, untouched.
        --
        -- n_chain: the count of future keep-alives this ping commits the org to
        -- before the session's expected next arrival. 0 here; its producer
        -- arrives with the hazard model, under its own flag.
        if p_index_enabled then
            -- 202608100003: the hazard model fills the p_alive and n_chain
            -- slots this block declared. Coalesced, not branched: with the
            -- hazard flag off, or on with no state row, they are exactly the
            -- 1.0 and 0 Task A pinned, so the index is unchanged and a non-null
            -- p_alive < 1 or a non-zero chain_cost_usd in the log is the mark
            -- of a v2-scored row.
            v_p_alive := coalesce(v_hazard_p_alive, 1.0);
            v_organic := 1.0;
            v_n_chain := coalesce(v_hazard_chain, 0);
            v_p_eff := v_p_return * v_p_alive * v_organic;
            -- THE DOLLARS. ONE definition, and it is not the ledger's.
            -- warm_prefixes.ping_reserve_usd is the observer-priced TRUE worst
            -- case for one keep-alive: api/server.py prices it per row against
            -- the provider catalog as a full cache write at the model+TTL
            -- premium. Dividing it by that same write multiplier recovers the
            -- prefix's base input dollars, and every index component is priced
            -- from there:
            --
            --   price_base = ping_reserve_usd / w
            --   v_hit      = price_base * (1 - f)   -- what a return saves
            --   c_belief   = price_base * f         -- what the keep-alive costs
            --   chain      = n_chain * c_belief
            --   index      = (p_eff*v_hit - chain - c_belief) / c_belief
            --              = p_eff/b - 1 - n_chain
            --
            -- w is the provider write multiplier, mirroring the ping_rate CASE
            -- in api/server.py that produced ping_reserve_usd: anthropic on the
            -- 1h tier 2.0, anthropic on 5m 1.25, automatic-cache providers
            -- (deepseek) 1.0. f is api/worker.py WARM_PROVIDER_SPECS'
            -- read_cost_fraction, recovered from the SAME per-provider
            -- break-even this claim already gates on -- the worker derives
            -- b = f/(1-f), which inverts to f = b/(1+b) exactly -- so the
            -- dollars and the gate cannot drift, and v_hit/c_belief =
            -- (1-f)/f = 1/b keeps the index above algebraically identical to
            -- p_eff/b - 1 - n_chain.
            --
            -- v_reserve (the LEDGER reservation, floored by the
            -- anthropic-calibrated p_reserve_usd_per_mtok) deliberately does
            -- NOT appear here. That floor is money safety for the daily
            -- ceiling, not economics: folding it into the index over-prices
            -- DeepSeek roughly 14x and would rank providers by how badly the
            -- flat floor missed them. It is still logged on every row as
            -- warm_decision_log.reserve_usd, so the pessimistic full-write
            -- dollars and the expected keep-alive dollars are BOTH auditable
            -- and neither is mislabelled as the other.
            --
            -- b <= 0 would mean a provider whose reads are free: every ping
            -- pays for itself and the ratio is unbounded. Clamped rather than
            -- divided, and no dollar component is derivable, so all three stay
            -- NULL rather than being invented.
            if v_break_even <= 1e-9 then
                v_index := 1000000;
                v_v_hit := null;
                v_c_belief := null;
                v_chain_cost := null;
            else
                v_read_fraction := v_break_even / (1 + v_break_even);
                v_write_mult := case
                    when v_row.provider = 'anthropic'
                         and v_row.provider_ttl_seconds > 300 then 2.0
                    when v_row.provider = 'anthropic' then 1.25
                    else 1.0 end;
                v_price_base := coalesce(v_row.ping_reserve_usd, 0)
                                / v_write_mult;
                v_v_hit := least(99999999,
                    round(v_price_base * (1 - v_read_fraction), 10));
                -- GROUPED: the value a return realizes is the value of the
                -- SHARED span, which is the group node's token_cum, not the
                -- leader's whole prefix. Scaled down; v_reserve stays the
                -- leader's full-prefix worst-case write. Conservative in
                -- both directions: less claimed value, unchanged claimed
                -- cost. v_index keeps its ratio form (p_eff/b - 1 -
                -- n_chain), in which the prefix's dollar value cancels, so
                -- this is a LOGGED quantity and not a second ranking.
                if v_dedup_node_token_cum is not null then
                    v_v_hit := round(v_v_hit * least(1,
                        v_dedup_node_token_cum::numeric
                        / greatest(v_row.prefix_tokens, 1)), 10);
                end if;
                v_c_belief := round(v_price_base * v_read_fraction, 10);
                v_chain_cost := round(v_n_chain * v_c_belief, 10);
                -- Written in ratio form, which is the identical number: an
                -- unpriced model carries ping_reserve_usd = 0, and a row with
                -- no dollars must still rank by its beliefs rather than divide
                -- zero by zero.
                v_index := least(1000000, greatest(-1000000,
                    v_p_eff / v_break_even - 1 - v_n_chain));
            end if;
            -- DIAGNOSTIC ONLY, and NOT the ordering key. See the candidate
            -- window above: index_score is already value per belief-dollar, so
            -- ordering by index/reserve prefers cheap arms over valuable ones
            -- under a binding budget. Kept in the log so the two rankings stay
            -- comparable in the data.
            v_index_density := v_index / greatest(v_reserve, 1e-10);
        end if;

        -- DEFERRAL (202608100007). Placed ABOVE every gate, because a
        -- deferred member must move no ledger, no envelope, no cap
        -- accounting and no ping counter -- the leader already bought the
        -- warmth, and charging twice for one write is the whole thing this
        -- removes. next_due_at advances by the horizon that write bought,
        -- the same horizon warm_ping_settle would have set, so the member
        -- comes back when the SHARED entry is due rather than immediately.
        --
        -- Gated on the leader having been CLAIMED in this invocation. A
        -- leader denied by any gate, or simply not yet visited (the loop
        -- order is the index order, not the group order), leaves its
        -- members to run every gate exactly as they would have without the
        -- flag. Dedup only ever removes a provably redundant ping.
        --
        -- Also gated on COVERAGE. The leader's ping warms the leader's own
        -- prefix and nothing past the fork, so a deferred member keeps warmth
        -- over token_cum tokens and loses everything beyond it. The leader is
        -- chosen by prefix_tokens ASC -- the cheapest ping available -- which
        -- maximizes exactly that loss: an arm sharing 4k of a 200k-token
        -- prefix would otherwise be deferred to save the cheapest ping in the
        -- group. Requiring the shared span to be at least a QUARTER of the
        -- member's prefix keeps the deferral a substitution rather than a
        -- downgrade.
        -- A member under the ratio simply runs every gate as it would with
        -- the flag off, which still cannot spend more than the flag-off
        -- policy.
        if v_dedup_role = 'deferred'
           and v_dedup_leaders ? (v_dedup_group::text)
           and coalesce(v_dedup_node_token_cum, 0)
               >= 0.25 * greatest(v_row.prefix_tokens, 1) then
            update public.warm_prefixes prefix
               set next_due_at = v_now + make_interval(secs => greatest(
                       60, prefix.provider_ttl_seconds - p_safety_margin_seconds))
             where prefix.organization_id = v_row.organization_id
               and prefix.customer_id = v_row.customer_id
               and prefix.provider = v_row.provider
               and prefix.prefix_hash = v_row.prefix_hash;
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, 'dedup_deferred', v_p_return, v_floor,
                v_reserve, v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token, null, null,
                v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                v_p_alive, v_organic, null,
                v_dedup_group, 'deferred', v_dedup_group_size,
                v_dedup_node_digest);
            continue;
        end if;

        -- ROI gate, verbatim from 202608090001 and evaluated FIRST. At the
        -- default configuration roi_min_p (0.35) >= b for every spec'd
        -- provider, so at flat priors and lambda = 0 the lambda gate can never
        -- deny a candidate this gate admitted: p >= floor >= b implies
        -- index >= 0 = lambda. An operator who configures roi_min_p < b makes
        -- the lambda gate the stricter -- and economically correct -- one, and
        -- the flag-on/flag-off equivalence intentionally does not cover that
        -- misconfiguration.
        if v_p_return < v_floor then
            -- lambda is logged NULL here on purpose: the pacing block below has
            -- not run for this (org, provider) yet, so no dual was in force.
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, 'skipped_roi', v_p_return, v_floor, v_reserve,
                v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token, null, null,
                v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                v_p_alive, v_organic, null);
            continue;
        end if;

        -- THE PACING DUAL. lambda lives in index units, per
        -- (org, provider, UTC day), and is updated at most once per pair per
        -- invocation -- at the first candidate of that pair to reach this gate.
        -- Recomputing it per candidate would compound the same intraday reading
        -- once per row and turn a pacing signal into a step function.
        --
        --   lambda' = min(cap, max(0, (1 + lambda) * exp(eta*(S - B*u)/max(B,0.01)) - 1))
        --
        -- with S the day's reserved+spent, B the daily budget, u the fraction
        -- of the UTC day elapsed. Multiplicative in (1 + lambda) space so
        -- lambda can rise from exactly 0; on pace or under pace (S <= B*u) the
        -- factor is <= 1 and the max(0, .) floor holds it at 0, which IS the
        -- provider break-even floor (index = 0 <=> p_eff = f/(1-f)). Over pace
        -- raises it until projected end-of-day spend is about the budget.
        --
        -- B*u is a LINEAR intraday target: the Phase-1 instantiation. Pacing
        -- against the remaining forecast hazard mass is plan section 4.4 and is
        -- deliberately out of scope here.
        if p_index_enabled then
            v_pair_key := v_row.organization_id || ':' || v_row.provider;
            if v_lambda_by_pair ? v_pair_key then
                v_lambda := (v_lambda_by_pair ->> v_pair_key)::numeric;
            else
                -- The ledger row must exist to carry lambda. This is the same
                -- upsert the budget gate performs below; at p_index_enabled =
                -- false it does NOT run early, which is what keeps a flag-off
                -- claim from creating a ledger row one gate sooner than v1 did.
                insert into public.warm_budget_ledger (organization_id, provider, day)
                values (v_row.organization_id, v_row.provider, v_day)
                on conflict (organization_id, provider, day) do nothing;
                select ledger.reserved_usd + ledger.spent_usd, ledger.lambda_index
                  into v_lambda_spend, v_lambda_old
                  from public.warm_budget_ledger ledger
                 where ledger.organization_id = v_row.organization_id
                   and ledger.provider = v_row.provider
                   and ledger.day = v_day;
                v_lambda_u := extract(epoch from
                        (v_now at time zone 'utc')
                        - date_trunc('day', v_now at time zone 'utc')) / 86400.0;
                -- The exponent is clamped only to keep exp() inside numeric's
                -- domain. It is outcome-identical: at +50 the result exceeds
                -- any admissible p_lambda_max (<= 1e6) and is clamped by it
                -- anyway, and at -50 the factor drives (1 + lambda) * f - 1
                -- below 0 for any lambda <= 1e6, which the floor takes to 0.
                v_lambda_exponent := least(50, greatest(-50,
                    v_eta * (v_lambda_spend
                             - coalesce(v_row.daily_budget_usd, 0) * v_lambda_u)
                    / greatest(coalesce(v_row.daily_budget_usd, 0), 0.01)));
                v_lambda := least(v_lambda_ceiling, greatest(0,
                    (1 + coalesce(v_lambda_old, 0)) * exp(v_lambda_exponent) - 1));
                update public.warm_budget_ledger ledger
                   set lambda_index = v_lambda,
                       lambda_updated_at = v_now
                 where ledger.organization_id = v_row.organization_id
                   and ledger.provider = v_row.provider
                   and ledger.day = v_day;
                v_lambda_by_pair := jsonb_set(
                    v_lambda_by_pair, array[v_pair_key], to_jsonb(v_lambda));
            end if;
            if v_index < v_lambda then
                perform public.warm_decision_record(
                    v_row.organization_id, v_row.customer_id, v_row.provider,
                    v_row.prefix_hash, 'skipped_lambda', v_p_return, v_floor,
                    v_reserve, v_row.prefix_tokens, v_row.ewma_interarrival_s,
                    v_row.arrival_count, v_pings_today, v_token, null, null,
                    v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                    v_p_alive, v_organic, v_lambda);
                continue;
            end if;
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
                v_row.arrival_count, v_pings_today, v_token, null, null,
                v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                v_p_alive, v_organic, v_lambda);
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
                v_row.arrival_count, v_pings_today, v_token, null, null,
                v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                v_p_alive, v_organic, v_lambda);
            continue;
        end if;
        -- PER-CUSTOMER ENVELOPE (202608100004). Positioned after the
        -- organization budget and before the control arm: it is a hard spend
        -- gate of the same kind as the budget above it, and the holdout coin
        -- must remain the LAST thing between a candidate and its reservation.
        --
        -- NO ROW MEANS NO CONSTRAINT, and that is the default-off story rather
        -- than a loophole. A spend ceiling cannot take a boolean argument
        -- without giving a caller a way to switch it off, so the check is
        -- unconditional and simply vacuous until a row exists -- and rows exist
        -- only because an operator PUT one or because the allocator coroutine
        -- (BREVITAS_WARM_ENVELOPE_ALLOCATOR, default off) ran. A deployment
        -- that has done neither behaves exactly as 202608100003 did.
        v_env_found := null;
        v_env := null;
        v_env_reserved := null;
        v_env_spent := null;
        v_env_day := null;
        select true, budget.envelope_usd, budget.reserved_usd, budget.spent_usd,
               budget.reserved_day
          into v_env_found, v_env, v_env_reserved, v_env_spent, v_env_day
          from public.warm_customer_budget budget
         where budget.organization_id = v_row.organization_id
           and budget.provider = v_row.provider
           and budget.period_start = v_period
           and budget.customer_ref = v_row.customer_id::text;
        -- STALE-RESERVATION SELF-HEAL. A reservation written before today was
        -- never released -- the worker holding it died, or its customer was
        -- erased and settle can no longer name the row. On the DAY-keyed
        -- organization ledger that heals itself, because tomorrow is a
        -- different row; on this MONTH-keyed table it would hold the
        -- customer's envelope hostage until the 1st. So a reservation from a
        -- previous UTC day is zeroed BEFORE the check reads it, exactly as if
        -- the day had rolled the row over. SPENT is untouched: spent dollars
        -- are settled money and the envelope is a monthly ceiling on them.
        if coalesce(v_env_found, false) and v_env_day < v_day::date then
            update public.warm_customer_budget budget
               set reserved_usd = 0,
                   reserved_day = v_day::date,
                   updated_at = v_now
             where budget.organization_id = v_row.organization_id
               and budget.provider = v_row.provider
               and budget.period_start = v_period
               and budget.customer_ref = v_row.customer_id::text;
            v_env_reserved := 0;
        end if;
        if coalesce(v_env_found, false)
           and v_env_reserved + v_env_spent + v_reserve > v_env then
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, 'envelope_denied', v_p_return, v_floor,
                v_reserve, v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token, null, null,
                v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                v_p_alive, v_organic, v_lambda);
            continue;
        end if;
        -- THE BETA CAP (202608100005). Trailing 28-day warm spend may not
        -- exceed beta x the control-verified savings measured over the same
        -- window. Positioned after every per-organization and per-customer
        -- ceiling and before the control arm, because the holdout coin must
        -- stay the LAST thing between a candidate and its reservation.
        --
        --   C28 = sum of control_lift_usd over [day-28, day-2]
        --         (the closed days warm_control_savings_refresh has scored)
        --   W28 = sum of reserved+spent over [day-27, day]
        --         (the open window that spend actually landed in)
        --   cap = beta * C28
        --
        -- The two windows deliberately do not line up. Savings can only be
        -- measured on days whose usage rows have settled, and spend has to be
        -- counted the moment it is reserved; asking them to share endpoints
        -- would either count spend nobody has measured the benefit of yet or
        -- credit savings against a day whose spend has not been counted.
        --
        -- p_beta = 0 skips the block ENTIRELY rather than computing a zero cap.
        if coalesce(p_beta, 0) > 0 then
            v_beta_key := v_row.organization_id || ':' || v_row.provider;
            if not (v_beta_cap_by_pair ? v_beta_key) then
                select coalesce(sum(savings.control_lift_usd), 0)
                  into v_beta_c28
                  from public.warm_control_savings_daily savings
                 where savings.organization_id = v_row.organization_id
                   and savings.provider = v_row.provider
                   and savings.day between v_day - 28 and v_day - 2;
                select coalesce(sum(ledger.reserved_usd + ledger.spent_usd), 0)
                  into v_beta_w28
                  from public.warm_budget_ledger ledger
                 where ledger.organization_id = v_row.organization_id
                   and ledger.provider = v_row.provider
                   and ledger.day between v_day - 27 and v_day;
                v_beta_cap_by_pair := jsonb_set(
                    v_beta_cap_by_pair, array[v_beta_key],
                    to_jsonb(p_beta * v_beta_c28));
                v_beta_w28_by_pair := jsonb_set(
                    v_beta_w28_by_pair, array[v_beta_key], to_jsonb(v_beta_w28));
            end if;
            v_beta_cap := (v_beta_cap_by_pair ->> v_beta_key)::numeric;
            v_beta_w28 := (v_beta_w28_by_pair ->> v_beta_key)::numeric;
            if v_beta_w28 + v_reserve > v_beta_cap then
                perform public.warm_decision_record(
                    v_row.organization_id, v_row.customer_id, v_row.provider,
                    v_row.prefix_hash, 'beta_denied', v_p_return, v_floor,
                    v_reserve, v_row.prefix_tokens, v_row.ewma_interarrival_s,
                    v_row.arrival_count, v_pings_today, v_token, null, null,
                    v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                    v_p_alive, v_organic, v_lambda);
                continue;
            end if;
        end if;
        -- CONTROL ARM (202608100001). Still the LAST gate before the
        -- reservation: its causal validity is exactly "every other gate has
        -- already passed", so the lambda gate had to go above it, not below.
        if coalesce(p_holdout_fraction, 0) > 0 then
            v_holdout_digest := sha256(convert_to(
                lower(v_row.organization_id::text)
                || lower(v_row.prefix_hash)
                || to_char(v_day, 'YYYY-MM-DD'), 'UTF8'));
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
                    p_holdout_fraction::numeric,
                    v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                    v_p_alive, v_organic, v_lambda);
                continue;
            end if;
        end if;

        update public.warm_budget_ledger ledger
           set reserved_usd = ledger.reserved_usd + v_reserve,
               updated_at = v_now
         where ledger.organization_id = v_row.organization_id
           and ledger.provider = v_row.provider
           and ledger.day = v_day;
        -- The envelope reservation is written IMMEDIATELY ADJACENT to the
        -- organization ledger's, deliberately: they are the same dollars seen
        -- at two granularities, and any code between them would be code that
        -- could reserve one without the other.
        if coalesce(v_env_found, false) then
            update public.warm_customer_budget budget
               set reserved_usd = budget.reserved_usd + v_reserve,
                   reserved_day = v_day::date,
                   updated_at = v_now
             where budget.organization_id = v_row.organization_id
               and budget.provider = v_row.provider
               and budget.period_start = v_period
               and budget.customer_ref = v_row.customer_id::text;
        end if;

        -- The cap's in-invocation accounting. The ledger sum above was read
        -- once for this pair; without this line a batch of candidates would
        -- each be measured against the same pre-batch W28 and the batch as a
        -- whole could walk straight past the cap.
        if coalesce(p_beta, 0) > 0 then
            v_beta_w28_by_pair := jsonb_set(
                v_beta_w28_by_pair, array[v_beta_key],
                to_jsonb(v_beta_w28 + v_reserve));
        end if;

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
        -- The leader is claimed: from here its members may defer. Written
        -- AFTER the reservation, so a leader that never reserved never
        -- authorizes a deferral.
        if v_dedup_role = 'leader' then
            v_dedup_leaders := jsonb_set(
                v_dedup_leaders, array[v_dedup_group::text], to_jsonb(true));
        end if;
        v_claimed_counts := jsonb_set(
            v_claimed_counts, array[v_customer_key],
            to_jsonb(coalesce((v_claimed_counts ->> v_customer_key)::integer, 0) + 1));
        perform public.warm_decision_record(
            v_row.organization_id, v_row.customer_id, v_row.provider,
            v_row.prefix_hash, 'pinged', v_p_return, v_floor, v_reserve,
            v_row.prefix_tokens, v_row.ewma_interarrival_s,
            v_row.arrival_count, v_pings_today, v_token, null,
            case when coalesce(p_holdout_fraction, 0) > 0
                then (1 - p_holdout_fraction)::numeric else null end,
            v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
            v_p_alive, v_organic, v_lambda,
            case when v_dedup_role = 'leader' then v_dedup_group end,
            case when v_dedup_role = 'leader' then 'leader' end,
            case when v_dedup_role = 'leader' then v_dedup_group_size end,
            case when v_dedup_role = 'leader' then v_dedup_node_digest end);
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
            'last_touch_at', coalesce(v_row.last_touch_at, v_row.last_seen_at)
        )
        -- Additive and ONLY on a leader, so a flag-off claimed row is
        -- byte-identical to 202608100005's. The worker ignores unknown
        -- keys; nothing downstream reads these yet.
        || case when v_dedup_role = 'leader'
                then jsonb_build_object(
                    'dedup_group', v_dedup_group,
                    'dedup_group_size', v_dedup_group_size,
                    -- Reported, never gated. See the p_group block above.
                    'dedup_p_group', v_p_group)
                else '{}'::jsonb end;
    end loop;
    return;
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean, numeric, boolean
) from public, anon, authenticated;
grant execute on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean, numeric, boolean
) to service_role;

comment on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean, numeric, boolean
) is 'Claim due warm prefixes under the daily budget and per-customer caps, logging every candidate it scores to public.warm_decision_log. Under p_index_enabled candidates are ranked by the dimensionless dollar index itself and paced by the lambda dual. Under p_hazard_v2 p_return is the decayed hierarchical hazard from public.warm_customer_state rather than the lifetime histogram, P(alive) and the keep-alive chain enter the index, and (only when p_index_enabled, which is what carries P(alive) into a decision) the stop-loss predicate is bypassed in favour of P(alive); a customer with no state row (or a suppressed one) falls back to the v1 histogram at flat priors, which is provably the v1 policy. A candidate whose reservation would carry its customer past that customer''s public.warm_customer_budget envelope for the month is denied as ''envelope_denied''; a customer with no envelope row is unconstrained, which is what keeps a deployment with no envelopes identical to 202608100003. Under p_beta > 0 a candidate is denied as ''beta_denied'' when the organization''s trailing 28-day warm spend plus this reservation would exceed p_beta times the control-verified savings in public.warm_control_savings_daily; at the default p_beta = 0 the cap does not run. A pair frozen in public.warm_org_mode is scored at FLAT PRIORS -- no hazard read, P(alive) 1, no chain, stop-loss predicate re-applied -- which is provably the v1 policy, not a second policy. Under p_parent_dedup, candidates whose 202608100006 chain paths share a warmed ancestor node above the provider''s cache floor are grouped: the cheapest member is claimed once, scored on its OWN return probability exactly as it would be with the flag off (the group''s combined return probability is reported on the claim payload as ''dedup_p_group'' and gates nothing, so the flag can never buy a ping the flag-off policy would have skipped), and every other member of the group is deferred as ''dedup_deferred'' to the horizon that one ping bought -- reserving nothing, spending nothing and consuming no ping cap. A member whose leader was denied or not yet reached proceeds through every gate unchanged, so the flag can only ever remove a redundant ping. At the default p_parent_dedup = false the pre-pass does not run at all.';


commit;
