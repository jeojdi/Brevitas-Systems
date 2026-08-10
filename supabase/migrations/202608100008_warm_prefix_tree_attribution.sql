-- Phase 1.5 learned warming, part 3: airport-game attribution.
--
-- THE QUESTION THIS ANSWERS. 202608100006 made a shared prefix nameable and
-- 202608100007 made Brevitas stop paying for it twice. Neither says WHO the
-- money was spent on. A single keep-alive ping keeps a single provider cache
-- entry warm, and every customer whose request lands inside that entry's TTL
-- flew in on a runway somebody else's ping paid for. Splitting that cost is
-- the airport game -- the textbook cooperative-game problem whose Shapley value
-- for a shared runway is the EQUAL SPLIT among the arrivals it actually served
-- -- and this migration computes it nightly, per node of the prefix tree.
--
-- THE DUMMY AXIOM IS THE WHOLE ETHICAL CONTENT. A player who contributes
-- nothing to a coalition is paid nothing by it, and its dual here is: a ping
-- NOBODY READ ALLOCATES NOTHING. It was a bet Brevitas made, it lost, and the
-- loss stays on Brevitas. It is recorded in public.warm_attribution_residual as
-- unallocated_speculative_usd and itemized in public.warm_prefix_cost_miss
-- beside the beneficiary the policy predicted, because the honest thing to do
-- with a failed prediction is to keep it where the model can be scored against
-- it -- not to charge somebody for a guess about them.
--
-- WHAT IS MEASURED-ONLY, WHICH IS EVERYTHING. warming_cost_share_usd and
-- warm_attributed_savings_usd are analytics. Nothing in this migration writes
-- usage_log.verified_savings_usd, the settlement sweep, warm_budget_ledger's
-- reserve-then-settle arithmetic, or any fee basis. warm spend is never
-- billable and stays never billable; the verified_savings_usd column on the
-- daily statement is a READ-ONLY COPY of usage_log's own number, carried so an
-- operator can read one row instead of joining two tables. Promoting any of
-- these numbers into a billed quantity is a future decision that this migration
-- deliberately does not make and does not prepare for.
--
-- THE EPOCH MODEL, WRITTEN OUT BECAUSE IT IS THE WHOLE ARITHMETIC. Every event
-- on a node -- a priced warming ping, or a real customer arrival -- opens a
-- window during which that node is warm: [t, min(t + TTL, end of day)). TTL is
-- the arm's own stored provider_ttl_seconds, which is already the pessimistic
-- floor the physics table calibrated. Windows opened by pings are 'brevitas'
-- windows and carry dollars; windows opened by arrivals are 'organic' and carry
-- none, because the traffic paid for those itself. Overlapping brevitas windows
-- on one node are COLLAPSED to a single window priced at the CHEAPEST of them,
-- and the excess is redundancy_usd -- Brevitas's own waste, and precisely the
-- number 202608100007's dedup exists to drive to zero.
--
-- WHAT A CUSTOMER IS CREDITED WITH. Cost flows down: a ping's price is split
-- along its ancestry by block token weight, with the tail beyond the last
-- complete block riding on the leaf so the shares sum to the ping EXACTLY.
-- Benefit does not: a customer's native cache discount is credited ONCE PER
-- ARRIVAL against the leaf window it landed in, never fanned to ancestors,
-- because an arrival saved money once. And it is credited only when
-- cache_attributable is true -- which since 202607170012 means, and only means,
-- that Brevitas placed the marker. A provider that caches on its own earns
-- Brevitas nothing here, by construction rather than by policy.
--
-- CONSERVATION IS AN ASSERTION, NOT AN ASPIRATION. Every dollar of priced
-- warming spend on a day lands in exactly one of three places: a customer's
-- cost share, the speculative residual, or the redundancy residual. The job
-- raises rather than write a statement that does not add up. The two other
-- comparisons it can make -- against warm_budget_ledger, and against the reward
-- join's own realized_net_usd -- are DIAGNOSTICS and are stored as such, because
-- a ledger row and an attribution row are allowed to disagree (they are
-- different clocks) and a gate built on that disagreement would fire on
-- bookkeeping lag rather than on error.
--
-- CLOSED DAYS ARE NEVER RECOMPUTED. Days older than p_close_days are skipped
-- outright. A statement an operator has already read is history; a job that
-- silently restated it would make every number in it provisional forever.
--
-- Forward-only and idempotent.

-- REVERSE: DDL: drop public.warm_attribution_run(integer,integer), public.warm_attribution_list(uuid,text,date,date) and public.warm_attribution_residual_get(uuid,text,date,date); drop public.warm_attribution_daily, public.warm_attribution_residual and public.warm_prefix_cost_miss; re-apply 202608100006_warm_prefix_chain_tree.sql's public.purge_warm_state, public.compliance_delete_tenant, public.compliance_delete_subject, public.compliance_export_tenant and public.compliance_export_subject verbatim with their revoke/grant

begin;

do $migration_precondition$
declare
    required_routine text;
begin
    foreach required_routine in array array[
        -- Re-created below carrying their own text forward; carrying text
        -- forward is only meaningful if that text is what is installed.
        'public.purge_warm_state(integer)',
        'public.compliance_delete_tenant(uuid,uuid,text)',
        'public.compliance_delete_subject(uuid,uuid,text)',
        'public.compliance_export_tenant(uuid,uuid,text)',
        'public.compliance_export_subject(uuid,uuid,text)',
        'public.compliance_delete_tenant_pre_company_identity(uuid,uuid,text)',
        'public.compliance_delete_subject_pre_company_identity(uuid,uuid,text)',
        'public.compliance_export_tenant_pre_company_identity(uuid,uuid,text)',
        'public.compliance_export_subject_pre_company_identity(uuid,uuid,text)',
        'public.compliance_anonymize_unshared_user(uuid)',
        'public.compliance_actor_role(text)',
        -- 202608100006. The tree IS the attribution substrate: without the
        -- fourteen-argument observe nothing ever writes a chain, and this job
        -- would be a no-op that looks like a zero.
        'public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric,text,jsonb,text,integer)'
    ] loop
        if to_regprocedure(required_routine) is null then
            raise exception using
                errcode = '55000',
                message = '202608100008 requires ' || required_routine;
        end if;
    end loop;
    if to_regclass('public.warm_prefix_node') is null
       or to_regclass('public.warm_prefix_edge') is null
       or to_regclass('public.warm_prefixes') is null
       or to_regclass('public.warm_decision_log') is null
       or to_regclass('public.warm_budget_ledger') is null
       or to_regclass('public.usage_log') is null then
        raise exception using
            errcode = '55000',
            message = '202608100008 requires the warming tables through 202608100006';
    end if;
    -- The chain stamps on the arm are what join a receipt to the tree. Their
    -- absence would make every ancestry walk start from null.
    if not exists (
        select 1 from pg_catalog.pg_attribute attribute
         where attribute.attrelid = 'public.warm_prefixes'::regclass
           and attribute.attname = 'chain_leaf_digest'
           and not attribute.attisdropped
    ) then
        raise exception using
            errcode = '55000',
            message = '202608100008 requires public.warm_prefixes.chain_leaf_digest';
    end if;
end;
$migration_precondition$;

-- ---------------------------------------------------------------------------
-- THE STATEMENT. One row per (organization, provider, UTC day, customer): what
-- the customer read, what share of warming spend their reads pulled toward
-- them, and what warming demonstrably saved them.
--
-- customer_ref is text rather than uuid for exactly the reason
-- warm_customer_budget's is: erasure tombstones the KEY and keeps the DOLLARS,
-- and a tombstone is not a uuid. A row whose ref starts 'erased:' is an
-- unlinkable sum belonging to the organization's own accounting.
--
-- avg_split_denominator is the average number of distinct customers this
-- customer shared a warm window with. It is the honest summary of "how much of
-- this bill is yours alone" -- and it is also why this table is admin-only:
-- a denominator above one is proof that siblings exist.
-- ---------------------------------------------------------------------------
create table if not exists public.warm_attribution_daily (
    organization_id uuid not null,
    provider text not null check (provider in ('anthropic', 'openai', 'deepseek')),
    day date not null,
    customer_ref text not null,
    nodes_read integer not null default 0 check (nodes_read >= 0),
    nodes_shared integer not null default 0 check (nodes_shared >= 0),
    avg_split_denominator numeric
        check (avg_split_denominator is null or avg_split_denominator >= 1),
    warming_cost_share_usd numeric(18,10) not null default 0,
    warm_attributed_savings_usd numeric(18,10) not null default 0,
    -- DISPLAY COPY ONLY. Read from usage_log, written by nobody else, and read
    -- back by nothing that prices, bills or settles.
    verified_savings_usd numeric(18,10) not null default 0,
    net_usd numeric(18,10) not null default 0,
    computed_at timestamptz not null default now(),
    primary key (organization_id, provider, day, customer_ref)
);
create index if not exists warm_attribution_daily_window_idx
    on public.warm_attribution_daily (organization_id, provider, day);
create index if not exists warm_attribution_daily_retention_idx
    on public.warm_attribution_daily (day);

-- ---------------------------------------------------------------------------
-- THE FOOTER. What the statement above does NOT allocate, and why. An operator
-- reading a day's attribution is entitled to see the whole dollar, including
-- the part of it that went nowhere.
-- ---------------------------------------------------------------------------
create table if not exists public.warm_attribution_residual (
    organization_id uuid not null,
    provider text not null check (provider in ('anthropic', 'openai', 'deepseek')),
    day date not null,
    total_warm_spend_usd numeric(18,10) not null default 0,
    allocated_usd numeric(18,10) not null default 0,
    unallocated_speculative_usd numeric(18,10) not null default 0,
    redundancy_usd numeric(18,10) not null default 0,
    unpriced_usd numeric(18,10) not null default 0,
    -- DIAGNOSTIC. The gap between what this job credits and what the reward
    -- join credited. It is the live measurement of the sibling-organic
    -- over-credit and it must not gate anything.
    reward_join_delta_usd numeric,
    computed_at timestamptz not null default now(),
    primary key (organization_id, provider, day)
);
create index if not exists warm_attribution_residual_retention_idx
    on public.warm_attribution_residual (day);

-- ---------------------------------------------------------------------------
-- THE FAILED BETS. One row per warm window nobody arrived in, with the
-- beneficiary the claim decision predicted and the belief it held. This is the
-- only place the policy's own predictions are scored against what happened,
-- and it exists so that a wrong prediction has a price paid by Brevitas and a
-- record kept by Brevitas.
-- ---------------------------------------------------------------------------
create table if not exists public.warm_prefix_cost_miss (
    id bigint generated always as identity primary key,
    organization_id uuid not null,
    provider text not null check (provider in ('anthropic', 'openai', 'deepseek')),
    day date not null,
    node_digest text not null check (node_digest ~ '^[0-9a-f]{64}$'),
    epoch_start timestamptz not null,
    usd numeric(18,10) not null,
    predicted_customer_ref text,
    p_return numeric check (p_return is null or p_return between 0 and 1)
);
create index if not exists warm_prefix_cost_miss_window_idx
    on public.warm_prefix_cost_miss (organization_id, provider, day);
create index if not exists warm_prefix_cost_miss_retention_idx
    on public.warm_prefix_cost_miss (day);

-- RLS on, zero policies, and no table privilege for any role: every read and
-- write goes through the SECURITY DEFINER routines below. Same posture as
-- 202608100006's tree tables.
alter table public.warm_attribution_daily enable row level security;
alter table public.warm_attribution_residual enable row level security;
alter table public.warm_prefix_cost_miss enable row level security;
revoke all on table public.warm_attribution_daily
    from public, anon, authenticated, service_role;
revoke all on table public.warm_attribution_residual
    from public, anon, authenticated, service_role;
revoke all on table public.warm_prefix_cost_miss
    from public, anon, authenticated, service_role;
-- Identity columns hand out sequence privileges separately from the table
-- (202608090001:229). A revoke on the table alone leaves the browser roles
-- holding USAGE on the sequence, which is the exposure 202607280024 was written
-- to close.
revoke all on sequence public.warm_prefix_cost_miss_id_seq
    from public, anon, authenticated, service_role;

comment on table public.warm_attribution_daily is
    'MEASURED-ONLY airport-game attribution statement: per (organization, provider, UTC day, customer), the share of warming spend that customer''s reads pulled toward them and the native cache discount warming demonstrably earned them. Never a billed quantity; verified_savings_usd is a read-only display copy of usage_log''s own number. Erasure tombstones customer_ref and keeps the dollars.';
comment on table public.warm_attribution_residual is
    'Per (organization, provider, UTC day) footer for public.warm_attribution_daily: total priced warming spend and the parts of it that were allocated, spent speculatively on windows nobody read, wasted on redundant overlapping pings, or unpriceable. Conservation of the first four is asserted by public.warm_attribution_run.';
comment on table public.warm_prefix_cost_miss is
    'One row per warm window that cost money and served no arrival, with the beneficiary the claim decision predicted and the belief it held. The dummy axiom made auditable: a ping nobody read allocates nothing and stays on Brevitas.';

-- ---------------------------------------------------------------------------
-- warm_attribution_run -- the nightly job.
--
-- The ancestry walk is a plain WITH RECURSIVE on parent_digest and NOT an
-- ltree containment query, deliberately. The path column is null past 253
-- blocks and its operators are not on this function's search_path anyway;
-- writing the read against the adjacency means a deep chain and a shallow one
-- mean the same thing. The hop bound is 300 -- the extraction cap -- and a leaf
-- that has not reached its root by then is attributed FLAT: the whole ping on
-- the leaf, which is the conservative reading, because a partial ancestry would
-- spread a cost across a span nobody can prove was shared.
-- ---------------------------------------------------------------------------
create or replace function public.warm_attribution_run(
    p_lookback_days integer default 2,
    p_close_days integer default 7
) returns jsonb as $$
declare
    v_now timestamptz := now();
    v_today date := (v_now at time zone 'utc')::date;
    v_day date;
    v_offset integer;
    v_day_start timestamptz;
    v_day_end timestamptz;
    v_pair record;
    v_days integer := 0;
    v_rows integer := 0;
    v_row_count integer;
    v_misses integer := 0;
    v_miss_count integer;
    v_priced numeric;
    v_unpriced numeric;
    v_allocated numeric;
    v_speculative numeric;
    v_redundancy numeric;
    v_attributed numeric;
    v_reward numeric;
    v_reward_rows integer;
begin
    if coalesce(p_lookback_days, 0) not between 1 and 31
       or coalesce(p_close_days, 0) not between 1 and 400 then
        raise exception 'warm attribution bounds are invalid';
    end if;
    -- Temp tables are dropped by hand rather than left to ON COMMIT DROP: two
    -- calls can share one transaction, and the second would otherwise inherit
    -- the first's rows. to_regclass rather than DROP IF EXISTS so the first
    -- call in a session does not emit a NOTICE about a schema that does not
    -- exist yet (202608100007's precedent).
    if to_regclass('pg_temp.warm_attr_event') is not null then
        drop table pg_temp.warm_attr_event;
    end if;
    if to_regclass('pg_temp.warm_attr_path') is not null then
        drop table pg_temp.warm_attr_path;
    end if;
    if to_regclass('pg_temp.warm_attr_leaf') is not null then
        drop table pg_temp.warm_attr_leaf;
    end if;
    if to_regclass('pg_temp.warm_attr_cost') is not null then
        drop table pg_temp.warm_attr_cost;
    end if;
    if to_regclass('pg_temp.warm_attr_read') is not null then
        drop table pg_temp.warm_attr_read;
    end if;
    if to_regclass('pg_temp.warm_attr_epoch') is not null then
        drop table pg_temp.warm_attr_epoch;
    end if;
    if to_regclass('pg_temp.warm_attr_alloc') is not null then
        drop table pg_temp.warm_attr_alloc;
    end if;
    create temp table warm_attr_event (
        event_id bigint generated always as identity,
        kind text not null,
        customer_id uuid,
        prefix_hash text,
        ts timestamptz not null,
        usd numeric,
        attributable boolean not null default false,
        discount numeric not null default 0,
        verified numeric not null default 0,
        leaf text not null,
        salt smallint not null,
        prefix_tokens integer not null default 0,
        ttl_seconds integer not null default 300
    ) on commit drop;
    create temp table warm_attr_path (
        leaf text not null,
        salt smallint not null,
        node_digest text not null,
        depth integer not null,
        block_tokens integer not null,
        token_cum integer not null,
        is_root boolean not null
    ) on commit drop;
    create temp table warm_attr_leaf (
        leaf text not null,
        salt smallint not null,
        resolved boolean not null,
        -- The chain's OWN token basis: warm_prefix_node.token_cum at the leaf.
        -- Deliberately not warm_prefixes.prefix_tokens -- that count is taken
        -- on a different basis (text-only on the anthropic path) and runs
        -- 4-12% below token_cum, which would hand the ancestors more than the
        -- whole ping and leave the leaf a NEGATIVE share.
        leaf_tokens integer not null default 0
    ) on commit drop;
    create temp table warm_attr_cost (
        event_id bigint not null,
        node_digest text not null,
        ts timestamptz not null,
        usd numeric not null,
        ttl_seconds integer not null,
        prefix_hash text
    ) on commit drop;
    create temp table warm_attr_read (
        node_digest text not null,
        ts timestamptz not null,
        customer_id uuid not null
    ) on commit drop;
    create temp table warm_attr_epoch (
        node_digest text not null,
        epoch_start timestamptz not null,
        epoch_end timestamptz not null,
        usd numeric not null,
        usd_total numeric not null,
        prefix_hash text
    ) on commit drop;
    create temp table warm_attr_alloc (
        node_digest text not null,
        epoch_start timestamptz not null,
        customer_id uuid not null,
        usd numeric not null,
        denominator integer not null
    ) on commit drop;

    for v_offset in reverse p_lookback_days .. 1 loop
        v_day := v_today - v_offset;
        -- CLOSED. Older than the close horizon is history and is not touched.
        continue when v_day <= v_today - p_close_days;
        v_days := v_days + 1;
        v_day_start := (v_day::timestamp at time zone 'utc');
        v_day_end := ((v_day + 1)::timestamp at time zone 'utc');
        for v_pair in
            select distinct usage.organization_id, usage.provider
              from public.usage_log usage
             where usage.ts >= v_day_start
               and usage.ts < v_day_end
               and usage.warm_prefix_hash is not null
               and usage.organization_id is not null
               and usage.provider in ('anthropic', 'openai', 'deepseek')
        loop
            truncate pg_temp.warm_attr_event;
            truncate pg_temp.warm_attr_path;
            truncate pg_temp.warm_attr_leaf;
            truncate pg_temp.warm_attr_cost;
            truncate pg_temp.warm_attr_read;
            truncate pg_temp.warm_attr_epoch;
            truncate pg_temp.warm_attr_alloc;

            -- Events. An arm with no chain stamp contributes nothing: there is
            -- no tree to attribute along, and inventing one is exactly the
            -- guess this job refuses to make.
            insert into pg_temp.warm_attr_event (
                kind, customer_id, prefix_hash, ts, usd, attributable,
                discount, verified, leaf, salt, prefix_tokens, ttl_seconds)
            select case when usage.strategy = 'cache_warm'
                        then 'cost' else 'read' end,
                   usage.customer_id, usage.warm_prefix_hash, usage.ts,
                   usage.actual_cost_usd,
                   coalesce(usage.cache_attributable, false),
                   coalesce(usage.native_cache_discount_usd, 0),
                   coalesce(usage.verified_savings_usd, 0),
                   prefix.chain_leaf_digest, prefix.chain_salt_version,
                   greatest(0, coalesce(prefix.prefix_tokens, 0)),
                   greatest(1, coalesce(prefix.provider_ttl_seconds, 300))
              from public.usage_log usage
              join public.warm_prefixes prefix
                on prefix.organization_id = usage.organization_id
               and prefix.customer_id = usage.customer_id
               and prefix.provider = usage.provider
               and prefix.prefix_hash = usage.warm_prefix_hash
             where usage.organization_id = v_pair.organization_id
               and usage.provider = v_pair.provider
               and usage.ts >= v_day_start
               and usage.ts < v_day_end
               and usage.warm_prefix_hash is not null
               and prefix.chain_leaf_digest is not null
               and prefix.chain_salt_version is not null
               -- A read that is not authoritative is not evidence of anything;
               -- a cost row is a cost row regardless.
               and (usage.strategy = 'cache_warm' or usage.authoritative);

            if not exists (select 1 from pg_temp.warm_attr_event) then
                -- Nothing chained on this day. Clear any stale statement and
                -- move on: a day that produced no attributable events must not
                -- keep yesterday's answer.
                delete from public.warm_attribution_daily statement
                 where statement.organization_id = v_pair.organization_id
                   and statement.provider = v_pair.provider and statement.day = v_day;
                delete from public.warm_attribution_residual statement
                 where statement.organization_id = v_pair.organization_id
                   and statement.provider = v_pair.provider and statement.day = v_day;
                delete from public.warm_prefix_cost_miss statement
                 where statement.organization_id = v_pair.organization_id
                   and statement.provider = v_pair.provider and statement.day = v_day;
                continue;
            end if;

            -- Ancestry, on the adjacency, bounded at the extraction cap.
            insert into pg_temp.warm_attr_path (
                leaf, salt, node_digest, depth, block_tokens, token_cum, is_root)
            with recursive leaves as (
                select distinct event.leaf, event.salt from pg_temp.warm_attr_event event
            ), walk as (
                select leaves.leaf, leaves.salt, node.node_digest,
                       node.parent_digest, node.depth, node.block_tokens,
                       node.token_cum, 0 as hops
                  from leaves
                  join public.warm_prefix_node node
                    on node.organization_id = v_pair.organization_id
                   and node.provider = v_pair.provider
                   and node.salt_version = leaves.salt
                   and node.node_digest = leaves.leaf
                union all
                select walk.leaf, walk.salt, node.node_digest,
                       node.parent_digest, node.depth, node.block_tokens,
                       node.token_cum, walk.hops + 1
                  from walk
                  join public.warm_prefix_node node
                    on node.organization_id = v_pair.organization_id
                   and node.provider = v_pair.provider
                   and node.salt_version = walk.salt
                   and node.node_digest = walk.parent_digest
                 where walk.parent_digest is not null
                   and walk.hops < 300
            )
            select distinct walk.leaf, walk.salt, walk.node_digest, walk.depth,
                   greatest(0, coalesce(walk.block_tokens, 0)),
                   greatest(0, coalesce(walk.token_cum, 0)),
                   walk.parent_digest is null
              from walk;

            -- A leaf is RESOLVED when its walk reached a root. Anything else --
            -- a pruned ancestor, a chain deeper than the bound -- is flat.
            insert into pg_temp.warm_attr_leaf (leaf, salt, resolved, leaf_tokens)
            select event.leaf, event.salt,
                   coalesce(bool_or(path.is_root), false),
                   coalesce(max(path.token_cum)
                            filter (where path.node_digest = event.leaf), 0)
              from (select distinct leaf, salt from pg_temp.warm_attr_event) event
              left join pg_temp.warm_attr_path path
                on path.leaf = event.leaf and path.salt = event.salt
             group by event.leaf, event.salt;

            -- Cost, split along the ancestry by block token weight, normalized
            -- by the LEAF's token_cum -- the same basis block_tokens is
            -- measured on, so the ancestor shares can never exceed the ping.
            -- Non-leaf nodes first; the leaf then absorbs whatever is left,
            -- which is its own block plus every rounding crumb, so the shares
            -- sum to the ping EXACTLY and the leaf share stays >= 0.
            insert into pg_temp.warm_attr_cost (
                event_id, node_digest, ts, usd, ttl_seconds, prefix_hash)
            select event.event_id, path.node_digest, event.ts,
                   round(event.usd * path.block_tokens / leaf.leaf_tokens, 10),
                   event.ttl_seconds, event.prefix_hash
              from pg_temp.warm_attr_event event
              join pg_temp.warm_attr_leaf leaf
                on leaf.leaf = event.leaf and leaf.salt = event.salt
              join pg_temp.warm_attr_path path
                on path.leaf = event.leaf and path.salt = event.salt
             where event.kind = 'cost'
               and event.usd is not null
               and leaf.resolved
               and leaf.leaf_tokens > 0
               and path.node_digest <> event.leaf
               and round(event.usd * path.block_tokens / leaf.leaf_tokens, 10) <> 0;
            insert into pg_temp.warm_attr_cost (
                event_id, node_digest, ts, usd, ttl_seconds, prefix_hash)
            select event.event_id, event.leaf, event.ts,
                   event.usd - coalesce((
                       select sum(cost.usd) from pg_temp.warm_attr_cost cost
                        where cost.event_id = event.event_id), 0),
                   event.ttl_seconds, event.prefix_hash
              from pg_temp.warm_attr_event event
             where event.kind = 'cost' and event.usd is not null;

            -- The leaf share is a REMAINDER, so a basis mismatch between the
            -- ancestor weights and the denominator shows up here as a negative
            -- number -- a signed redistribution the conservation check at the
            -- end cannot see, because it still sums to the ping. Fail closed.
            if exists (
                select 1 from pg_temp.warm_attr_cost cost
                  join pg_temp.warm_attr_event event
                    on event.event_id = cost.event_id
                 where cost.node_digest = event.leaf
                   and event.usd >= 0 and cost.usd < 0) then
                raise exception
                    'warm attribution leaf share is negative for % % %',
                    v_pair.organization_id, v_pair.provider, v_day;
            end if;

            -- Reads, fanned to every ancestor. A flat leaf fans to itself.
            insert into pg_temp.warm_attr_read (node_digest, ts, customer_id)
            select path.node_digest, event.ts, event.customer_id
              from pg_temp.warm_attr_event event
              join pg_temp.warm_attr_leaf leaf
                on leaf.leaf = event.leaf and leaf.salt = event.salt
              join pg_temp.warm_attr_path path
                on path.leaf = event.leaf and path.salt = event.salt
             where event.kind = 'read' and event.customer_id is not null
               and leaf.resolved
            union all
            select event.leaf, event.ts, event.customer_id
              from pg_temp.warm_attr_event event
              join pg_temp.warm_attr_leaf leaf
                on leaf.leaf = event.leaf and leaf.salt = event.salt
             where event.kind = 'read' and event.customer_id is not null
               and not leaf.resolved;

            -- Windows, then the redundancy collapse. Gaps and islands: a window
            -- that starts at or before the running maximum end of the windows
            -- before it belongs to the same run.
            insert into pg_temp.warm_attr_epoch (
                node_digest, epoch_start, epoch_end, usd, usd_total, prefix_hash)
            with raw as (
                select cost.node_digest, cost.ts as epoch_start,
                       least(cost.ts + make_interval(secs => cost.ttl_seconds),
                             v_day_end) as epoch_end,
                       cost.usd, cost.prefix_hash
                  from pg_temp.warm_attr_cost cost
            ), ordered as (
                select raw.*,
                       max(raw.epoch_end) over (
                           partition by raw.node_digest
                           order by raw.epoch_start, raw.usd, raw.prefix_hash
                           rows between unbounded preceding and 1 preceding
                       ) as prior_end
                  from raw
            ), marked as (
                select ordered.*,
                       case when ordered.prior_end is null
                                 or ordered.epoch_start > ordered.prior_end
                            then 1 else 0 end as new_run
                  from ordered
            ), grouped as (
                select marked.*,
                       sum(marked.new_run) over (
                           partition by marked.node_digest
                           order by marked.epoch_start, marked.usd, marked.prefix_hash
                           rows between unbounded preceding and current row
                       ) as run_id
                  from marked
            )
            select grouped.node_digest, min(grouped.epoch_start),
                   max(grouped.epoch_end), min(grouped.usd), sum(grouped.usd),
                   (array_agg(grouped.prefix_hash
                              order by grouped.epoch_start))[1]
              from grouped
             group by grouped.node_digest, grouped.run_id;

            -- The airport rule. Equal split among the DISTINCT customers who
            -- arrived inside the window, left-inclusive so an arrival at
            -- exactly the ping's own instant counts. Largest remainder on the
            -- last beneficiary keeps the split exact at ten decimal places.
            insert into pg_temp.warm_attr_alloc (
                node_digest, epoch_start, customer_id, usd, denominator)
            with beneficiary as (
                select epoch.node_digest, epoch.epoch_start, epoch.usd,
                       arrival.customer_id
                  from pg_temp.warm_attr_epoch epoch
                  join pg_temp.warm_attr_read arrival
                    on arrival.node_digest = epoch.node_digest
                   and arrival.ts >= epoch.epoch_start
                   and arrival.ts < epoch.epoch_end
                 where epoch.usd > 0
                 group by epoch.node_digest, epoch.epoch_start, epoch.usd,
                          arrival.customer_id
            ), counted as (
                select beneficiary.*,
                       count(*) over (partition by beneficiary.node_digest,
                                                   beneficiary.epoch_start)
                           as denominator,
                       row_number() over (partition by beneficiary.node_digest,
                                                       beneficiary.epoch_start
                                          order by beneficiary.customer_id)
                           as slot
                  from beneficiary
            )
            select counted.node_digest, counted.epoch_start, counted.customer_id,
                   case when counted.slot < counted.denominator
                        then round(counted.usd / counted.denominator, 10)
                        else counted.usd
                             - round(counted.usd / counted.denominator, 10)
                               * (counted.denominator - 1)
                   end,
                   counted.denominator
              from counted;

            -- The failed bets, itemized beside what the policy predicted.
            delete from public.warm_prefix_cost_miss statement
             where statement.organization_id = v_pair.organization_id
               and statement.provider = v_pair.provider and statement.day = v_day;
            insert into public.warm_prefix_cost_miss (
                organization_id, provider, day, node_digest, epoch_start, usd,
                predicted_customer_ref, p_return)
            select v_pair.organization_id, v_pair.provider, v_day,
                   epoch.node_digest, epoch.epoch_start, epoch.usd,
                   predicted.customer_id::text, predicted.p_return
              from pg_temp.warm_attr_epoch epoch
              left join lateral (
                    select decision.customer_id, decision.p_return
                      from public.warm_decision_log decision
                     where decision.organization_id = v_pair.organization_id
                       and decision.provider = v_pair.provider
                       and decision.prefix_hash = epoch.prefix_hash
                       and decision.decision = 'pinged'
                       and decision.ts <= epoch.epoch_start
                     order by decision.ts desc
                     limit 1
              ) predicted on true
             where epoch.usd > 0
               and not exists (
                    select 1 from pg_temp.warm_attr_alloc allocation
                     where allocation.node_digest = epoch.node_digest
                       and allocation.epoch_start = epoch.epoch_start);
            get diagnostics v_miss_count = row_count;
            v_misses := v_misses + v_miss_count;

            -- Conservation. Every priced dollar is a cost share, a speculative
            -- loss, or redundancy -- and an epoch priced at or below zero (a
            -- free ping, a rounding crumb) counts as speculative so the
            -- identity closes with no residue term.
            select coalesce(sum(event.usd), 0)
              into v_priced
              from pg_temp.warm_attr_event event
             where event.kind = 'cost' and event.usd is not null;
            select coalesce(sum(prefix.ping_reserve_usd), 0)
              into v_unpriced
              from pg_temp.warm_attr_event event
              join public.warm_prefixes prefix
                on prefix.organization_id = v_pair.organization_id
               and prefix.customer_id = event.customer_id
               and prefix.provider = v_pair.provider
               and prefix.prefix_hash = event.prefix_hash
             where event.kind = 'cost' and event.usd is null;
            select coalesce(sum(allocation.usd), 0) into v_allocated
              from pg_temp.warm_attr_alloc allocation;
            select coalesce(sum(epoch.usd_total - epoch.usd), 0)
              into v_redundancy from pg_temp.warm_attr_epoch epoch;
            select coalesce(sum(epoch.usd), 0) into v_speculative
              from pg_temp.warm_attr_epoch epoch
             where epoch.usd <= 0
                or not exists (
                    select 1 from pg_temp.warm_attr_alloc allocation
                     where allocation.node_digest = epoch.node_digest
                       and allocation.epoch_start = epoch.epoch_start);
            if abs(v_allocated + v_speculative + v_redundancy - v_priced)
               > 0.000000001 then
                raise exception
                    'warm attribution conservation invariant failed for % % %',
                    v_pair.organization_id, v_pair.provider, v_day;
            end if;

            -- Benefit: once per arrival, on the leaf window only.
            select coalesce(sum(event.discount), 0) into v_attributed
              from pg_temp.warm_attr_event event
             where event.kind = 'read' and event.attributable
               and exists (
                    select 1 from pg_temp.warm_attr_epoch epoch
                     where epoch.node_digest = event.leaf
                       and event.ts >= epoch.epoch_start
                       and event.ts < epoch.epoch_end);

            delete from public.warm_attribution_daily statement
             where statement.organization_id = v_pair.organization_id
               and statement.provider = v_pair.provider and statement.day = v_day;
            insert into public.warm_attribution_daily (
                organization_id, provider, day, customer_ref, nodes_read,
                nodes_shared, avg_split_denominator, warming_cost_share_usd,
                warm_attributed_savings_usd, verified_savings_usd, net_usd,
                computed_at)
            select v_pair.organization_id, v_pair.provider, v_day,
                   reader.customer_id::text,
                   coalesce(touched.nodes_read, 0),
                   coalesce(share.nodes_shared, 0),
                   share.avg_denominator,
                   coalesce(share.cost_share, 0),
                   coalesce(benefit.savings, 0),
                   coalesce(reader.verified, 0),
                   coalesce(benefit.savings, 0) - coalesce(share.cost_share, 0),
                   v_now
              from (
                    select event.customer_id, sum(event.verified) as verified
                      from pg_temp.warm_attr_event event
                     where event.kind = 'read' and event.customer_id is not null
                     group by event.customer_id
              ) reader
              left join (
                    select arrival.customer_id,
                           count(distinct arrival.node_digest) as nodes_read
                      from pg_temp.warm_attr_read arrival
                     group by arrival.customer_id
              ) touched on touched.customer_id = reader.customer_id
              left join (
                    select allocation.customer_id,
                           sum(allocation.usd) as cost_share,
                           avg(allocation.denominator) as avg_denominator,
                           count(distinct allocation.node_digest)
                               filter (where allocation.denominator >= 2)
                               as nodes_shared
                      from pg_temp.warm_attr_alloc allocation
                     group by allocation.customer_id
              ) share on share.customer_id = reader.customer_id
              left join (
                    select event.customer_id, sum(event.discount) as savings
                      from pg_temp.warm_attr_event event
                     where event.kind = 'read' and event.attributable
                       and exists (
                            select 1 from pg_temp.warm_attr_epoch epoch
                             where epoch.node_digest = event.leaf
                               and event.ts >= epoch.epoch_start
                               and event.ts < epoch.epoch_end)
                     group by event.customer_id
              ) benefit on benefit.customer_id = reader.customer_id;
            get diagnostics v_row_count = row_count;
            v_rows := v_rows + v_row_count;

            select count(*)::integer,
                   coalesce(sum(decision.realized_net_usd), 0)
              into v_reward_rows, v_reward
              from public.warm_decision_log decision
             where decision.organization_id = v_pair.organization_id
               and decision.provider = v_pair.provider
               and decision.decision = 'pinged'
               and decision.realized_net_usd is not null
               and decision.ts >= v_day_start
               and decision.ts < v_day_end;
            delete from public.warm_attribution_residual statement
             where statement.organization_id = v_pair.organization_id
               and statement.provider = v_pair.provider and statement.day = v_day;
            insert into public.warm_attribution_residual (
                organization_id, provider, day, total_warm_spend_usd,
                allocated_usd, unallocated_speculative_usd, redundancy_usd,
                unpriced_usd, reward_join_delta_usd, computed_at)
            values (v_pair.organization_id, v_pair.provider, v_day,
                    round(v_priced, 10), round(v_allocated, 10),
                    round(v_speculative, 10), round(v_redundancy, 10),
                    round(v_unpriced, 10),
                    case when v_reward_rows > 0
                         then round(v_attributed - v_reward, 10) end,
                    v_now);
        end loop;
    end loop;
    return jsonb_build_object(
        'schema', 'brevitas.warm-attribution.v1', 'status', 'computed',
        'days_scanned', v_days, 'rows_written', v_rows,
        'cost_misses', v_misses);
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- The two read paths. Org-scoped arguments, service_role only, and neither one
-- ever returns a node digest, a path or anything else derived from prompt
-- content. Shape copied from 202608100004's warm_customer_budget_list.
-- ---------------------------------------------------------------------------
create or replace function public.warm_attribution_list(
    p_organization_id uuid,
    p_provider text default null,
    p_day_from date default null,
    p_day_to date default null
) returns setof jsonb as $$
declare
    v_to date := coalesce(p_day_to, (clock_timestamp() at time zone 'utc')::date);
    v_from date := coalesce(p_day_from, v_to - 30);
begin
    if p_organization_id is null then
        raise exception 'warm attribution arguments are invalid';
    end if;
    if p_provider is not null
       and p_provider not in ('anthropic', 'openai', 'deepseek') then
        raise exception 'warm attribution arguments are invalid';
    end if;
    if v_from > v_to then
        raise exception 'warm attribution arguments are invalid';
    end if;
    return query
        select jsonb_build_object(
            'organization_id', statement.organization_id,
            'provider', statement.provider,
            'day', statement.day,
            'customer_ref', statement.customer_ref,
            'erased', statement.customer_ref like 'erased:%',
            'nodes_read', statement.nodes_read,
            'nodes_shared', statement.nodes_shared,
            'avg_split_denominator', statement.avg_split_denominator,
            'warming_cost_share_usd', statement.warming_cost_share_usd,
            'warm_attributed_savings_usd', statement.warm_attributed_savings_usd,
            'verified_savings_usd', statement.verified_savings_usd,
            'net_usd', statement.net_usd,
            'computed_at', statement.computed_at
        )
          from public.warm_attribution_daily statement
         where statement.organization_id = p_organization_id
           and (p_provider is null or statement.provider = p_provider)
           and statement.day between v_from and v_to
         order by statement.day, statement.provider, statement.customer_ref;
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

create or replace function public.warm_attribution_residual_get(
    p_organization_id uuid,
    p_provider text default null,
    p_day_from date default null,
    p_day_to date default null
) returns setof jsonb as $$
declare
    v_to date := coalesce(p_day_to, (clock_timestamp() at time zone 'utc')::date);
    v_from date := coalesce(p_day_from, v_to - 30);
begin
    if p_organization_id is null then
        raise exception 'warm attribution arguments are invalid';
    end if;
    if p_provider is not null
       and p_provider not in ('anthropic', 'openai', 'deepseek') then
        raise exception 'warm attribution arguments are invalid';
    end if;
    if v_from > v_to then
        raise exception 'warm attribution arguments are invalid';
    end if;
    return query
        select jsonb_build_object(
            'organization_id', statement.organization_id,
            'provider', statement.provider,
            'day', statement.day,
            'total_warm_spend_usd', statement.total_warm_spend_usd,
            'allocated_usd', statement.allocated_usd,
            'unallocated_speculative_usd', statement.unallocated_speculative_usd,
            'redundancy_usd', statement.redundancy_usd,
            'unpriced_usd', statement.unpriced_usd,
            'reward_join_delta_usd', statement.reward_join_delta_usd,
            'computed_at', statement.computed_at
        )
          from public.warm_attribution_residual statement
         where statement.organization_id = p_organization_id
           and (p_provider is null or statement.provider = p_provider)
           and statement.day between v_from and v_to
         order by statement.day, statement.provider;
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- Privileges. 202607280032's contract: every migration that defines a routine
-- restates that routine's own posture rather than inheriting one.
-- ---------------------------------------------------------------------------
revoke all on function public.warm_attribution_run(integer, integer)
    from public, anon, authenticated;
grant execute on function public.warm_attribution_run(integer, integer)
    to service_role;
revoke all on function public.warm_attribution_list(uuid, text, date, date)
    from public, anon, authenticated;
grant execute on function public.warm_attribution_list(uuid, text, date, date)
    to service_role;
revoke all on function public.warm_attribution_residual_get(uuid, text, date, date)
    from public, anon, authenticated;
grant execute on function public.warm_attribution_residual_get(uuid, text, date, date)
    to service_role;
revoke all on function public.purge_warm_state(integer)
    from public, anon, authenticated;
grant execute on function public.purge_warm_state(integer)
    to service_role;
revoke all on function public.compliance_delete_tenant(uuid, uuid, text)
    from public, anon, authenticated, service_role;
grant execute on function public.compliance_delete_tenant(uuid, uuid, text)
    to service_role;
revoke all on function public.compliance_delete_subject(uuid, uuid, text)
    from public, anon, authenticated, service_role;
grant execute on function public.compliance_delete_subject(uuid, uuid, text)
    to service_role;
revoke all on function public.compliance_export_tenant(uuid, uuid, text)
    from public, anon, authenticated;
grant execute on function public.compliance_export_tenant(uuid, uuid, text)
    to service_role;
revoke all on function public.compliance_export_subject(uuid, uuid, text)
    from public, anon, authenticated;
grant execute on function public.compliance_export_subject(uuid, uuid, text)
    to service_role;

comment on function public.warm_attribution_run(integer, integer) is
    'MEASURED-ONLY nightly airport-game attribution. Splits each priced warming ping along its prefix ancestry by block token weight, collapses overlapping warm windows on a node to the cheapest of them, and divides each window''s cost equally among the DISTINCT customers who arrived inside it. A window nobody arrived in allocates nothing and is itemized in public.warm_prefix_cost_miss beside the beneficiary the policy predicted. Raises rather than write a statement whose cost shares, speculative loss and redundancy do not sum to the priced spend. Days older than p_close_days are never recomputed. Writes no usage_log row, no ledger row, no settlement and no fee basis.';

-- ---------------------------------------------------------------------------
-- purge_warm_state (202608100006:628-739) + the attribution horizons. None of
-- the three tables added above carries a data subject who can hold, export or
-- erase it on their own -- customer_ref is the organization's own accounting
-- key and is tombstoned rather than deleted by subject erasure -- so they age
-- on this sweeper beside the rest of the warming state, exactly as
-- 202608100006's tree tables do.
-- ---------------------------------------------------------------------------

create or replace function public.purge_warm_state(
    p_retention_days integer
) returns jsonb as $$
declare
    -- The financial-evidence floor. warm_budget_ledger is the only record of
    -- what a warming ping cost, and every settlement and correction revision
    -- recomputes the deduction from it, so it outlives the maintenance window by
    -- construction rather than by configuration.
    v_ledger_retention_days integer := greatest(coalesce(p_retention_days, 0), 365);
    -- Absolute ceiling on a warm prefix payload, independent of how often the
    -- prefix is re-observed. Must stay above the rolling
    -- `last_seen_at + interval '7 days'` TTL so the two do not fight.
    v_prefix_absolute_days integer := 30;
    -- Aggregate physics horizon. warm_ttl_observations has no tenant keys, so
    -- no data subject can hold, export or erase it and it has no business in
    -- compliance_run_retention's class list; it ages here, on the sweeper that
    -- already owns warming maintenance. A year is chosen to span provider TTL
    -- and pricing regime changes, which is the timescale the table exists to
    -- detect. It is deliberately NOT tied to p_retention_days: that argument is
    -- the operator's prefix-payload window, and letting a 7-day default erase
    -- the physics evidence is the same mistake 202607280017 fixed for the
    -- budget ledger.
    v_observation_retention_days integer := 365;
    -- 202608100005. Three more aggregate horizons on the same sweeper and for
    -- the same reason: none of these tables carries a tenant key, so no data
    -- subject can hold, export or erase them and they have no business in
    -- compliance_run_retention's class list.
    --
    -- Canary probes are operational state -- a pending row is an experiment in
    -- flight -- and 30 days is well past the longest gap the ladder can ask
    -- for. The canary dollar ledger and the control-savings comparison are
    -- both financial evidence: the ledger is what the probes cost Brevitas,
    -- and the comparison is the measurement a spend ceiling was set from.
    -- Both retain on the 400-day evidence horizon the rest of the accounting
    -- record uses, and neither is tied to p_retention_days for the reason
    -- 202607280017 established for the budget ledger.
    -- 202608100006. The prefix tree is RAW warm state -- structure derived
    -- from prompt prefixes -- so it ages on the same 90-day horizon the rest
    -- of the raw warm state uses, keyed on the touch clock rather than on
    -- creation: a node that is still being observed is still in use.
    v_chain_node_retention_days integer := 90;
    v_canary_probe_retention_days integer := 30;
    v_canary_evidence_retention_days integer := 400;
    v_prefixes_deleted integer;
    v_prefixes_absolute_deleted integer;
    v_ledger_deleted integer;
    v_observations_deleted integer;
    v_canary_probes_deleted integer;
    v_canary_ledger_deleted integer;
    v_control_savings_deleted integer;
    v_chain_nodes_deleted integer;
    v_chain_edges_deleted integer;
    -- 202608100008. The daily attribution statement and its residual footer
    -- are DOLLAR ACCOUNTING -- they are the arithmetic an operator was shown
    -- when they asked who warming spent money on -- so they retain on the
    -- 400-day evidence horizon the rest of the accounting record uses, and
    -- like every other horizon here they are deliberately NOT tied to
    -- p_retention_days, for the reason 202607280017 established for the budget
    -- ledger. The cost-miss log is a node-keyed diagnostic about the tree and
    -- ages with the tree: a miss row naming a node that has been pruned names
    -- nothing.
    v_attribution_retention_days integer := 400;
    v_attribution_miss_retention_days integer := 90;
    v_attribution_daily_deleted integer;
    v_attribution_residual_deleted integer;
    v_attribution_misses_deleted integer;
begin
    if coalesce(p_retention_days, 0) not between 1 and 365 then
        raise exception 'warm retention bounds are invalid';
    end if;
    delete from public.warm_prefixes prefix where prefix.expires_at <= now();
    get diagnostics v_prefixes_deleted = row_count;
    delete from public.warm_prefixes prefix
     where prefix.created_at < now() - make_interval(days => v_prefix_absolute_days);
    get diagnostics v_prefixes_absolute_deleted = row_count;
    delete from public.warm_budget_ledger ledger
     where ledger.day < (now() at time zone 'utc')::date - v_ledger_retention_days;
    get diagnostics v_ledger_deleted = row_count;
    delete from public.warm_ttl_observations observation
     where observation.observed_at
           < now() - make_interval(days => v_observation_retention_days);
    get diagnostics v_observations_deleted = row_count;
    delete from public.warm_canary_probes probe
     where probe.created_at
           < now() - make_interval(days => v_canary_probe_retention_days);
    get diagnostics v_canary_probes_deleted = row_count;
    delete from public.warm_canary_ledger ledger
     where ledger.day < (now() at time zone 'utc')::date
                        - v_canary_evidence_retention_days;
    get diagnostics v_canary_ledger_deleted = row_count;
    delete from public.warm_control_savings_daily savings
     where savings.day < (now() at time zone 'utc')::date
                         - v_canary_evidence_retention_days;
    get diagnostics v_control_savings_deleted = row_count;
    delete from public.warm_prefix_node node
     where node.last_touch_at
           < now() - make_interval(days => v_chain_node_retention_days);
    get diagnostics v_chain_nodes_deleted = row_count;
    -- An edge whose child node is gone points at an ancestry that no longer
    -- exists. Deleting by the child is what keeps the adjacency table from
    -- outliving the tree it describes.
    delete from public.warm_prefix_edge edge
     where not exists (
        select 1 from public.warm_prefix_node node
         where node.organization_id = edge.organization_id
           and node.provider = edge.provider
           and node.salt_version = edge.salt_version
           and node.node_digest = edge.child_digest
     );
    get diagnostics v_chain_edges_deleted = row_count;
    delete from public.warm_attribution_daily statement
     where statement.day < (now() at time zone 'utc')::date
                           - v_attribution_retention_days;
    get diagnostics v_attribution_daily_deleted = row_count;
    delete from public.warm_attribution_residual statement
     where statement.day < (now() at time zone 'utc')::date
                           - v_attribution_retention_days;
    get diagnostics v_attribution_residual_deleted = row_count;
    delete from public.warm_prefix_cost_miss miss
     where miss.day < (now() at time zone 'utc')::date
                      - v_attribution_miss_retention_days;
    get diagnostics v_attribution_misses_deleted = row_count;
    return jsonb_build_object(
        'schema', 'brevitas.warm-purge.v1', 'status', 'purged',
        'canary_probes_deleted', v_canary_probes_deleted,
        'canary_ledger_deleted', v_canary_ledger_deleted,
        'control_savings_deleted', v_control_savings_deleted,
        'prefixes_deleted', v_prefixes_deleted,
        'prefixes_absolute_deleted', v_prefixes_absolute_deleted,
        'ledger_retention_days', v_ledger_retention_days,
        'ledger_deleted', v_ledger_deleted,
        'observation_retention_days', v_observation_retention_days,
        'observations_deleted', v_observations_deleted,
        'prefix_nodes_deleted', v_chain_nodes_deleted,
        'prefix_edges_deleted', v_chain_edges_deleted
        , 'attribution_daily_deleted', v_attribution_daily_deleted
        , 'attribution_residual_deleted', v_attribution_residual_deleted
        , 'attribution_misses_deleted', v_attribution_misses_deleted
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- compliance_delete_tenant (202608100006:745-925) + the attribution rows. An
-- organization leaving takes its own accounting with it.
-- ---------------------------------------------------------------------------

create or replace function public.compliance_delete_tenant(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns text
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
declare
    v_result text;
    v_identity_ids uuid[];
    v_identity_id uuid;
    v_billing_before bigint;
    v_billing_after bigint;
    v_settlement_before bigint;
    v_settlement_after bigint;
begin
    select coalesce(
        pg_catalog.array_agg(distinct identity_id), array[]::uuid[]
    ) into v_identity_ids
      from (
        select member.user_id as identity_id
          from public.organization_members member
         where member.organization_id = p_organization_id
        union all
        select organization.billing_owner_id
          from public.organizations organization
         where organization.id = p_organization_id
           and organization.billing_owner_id is not null
      ) identities;
    select count(*) into v_billing_before
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    select count(*) into v_settlement_before
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id = usage.organization_id
                      and settlement.status <> 'void'
                      and settlement.usage_log_watermark_id >= usage.id);

    v_result := public.compliance_delete_tenant_pre_company_identity(
        p_organization_id, p_request_id, p_actor_id
    );

    -- Warming state was introduced by 202607280001, after the frozen inner
    -- body was written, so nothing in the chain deletes it. warm_credentials
    -- holds a KMS-encrypted third-party provider key plus the named consent
    -- actor and consent timestamp; it cascades only from public.organizations,
    -- and tenant deletion deliberately RENAMES that row instead of deleting it,
    -- so the cascade never fires. warm_prefixes cascades from public.customers
    -- (deleted by the inner body) and is repeated here as belt and braces.
    -- Both deletes are inside the caller's transaction, so the whole deletion
    -- still rolls back as one unit, and they re-run for an already-'completed'
    -- request because the inner body short-circuits before them.
    delete from public.warm_credentials credential
     where credential.organization_id = p_organization_id;
    delete from public.warm_prefixes prefix
     where prefix.organization_id = p_organization_id;
    -- public.warm_decision_log is per-customer behavioral evidence -- which
    -- prefixes were scored, how often they were expected to return, what was
    -- warmed and what was denied. It carries no foreign key (an FK to
    -- public.customers would let a tenant deletion block or abort a warming
    -- claim, a money path, to protect an analytics row), so nothing cascades it
    -- away and this explicit delete is the only thing that erases it.
    delete from public.warm_decision_log entry
     where entry.organization_id = p_organization_id;
    -- public.warm_customer_state (202608100003) is the learned behavioural
    -- profile itself -- decayed hour-of-week arrival mass, churn counters and a
    -- periodicity label, per customer. It carries no foreign key, for the two
    -- reasons stated at the table, so nothing cascades it away and this is the
    -- only thing that erases it. The organization-aggregate row (nil customer
    -- uuid) goes with the rest: the whole tenant is leaving.
    delete from public.warm_customer_state state
     where state.organization_id = p_organization_id;
    -- The suppression list is deleted HERE and only here. It exists to keep an
    -- erased subject out of the model for the lifetime of the tenant; once the
    -- tenant itself is gone there is no model left for it to fence, and keeping
    -- a list of customer ids belonging to a deleted organization would be the
    -- exact residue tenant erasure is for.
    delete from public.warm_modeling_suppression suppression
     where suppression.organization_id = p_organization_id;
    -- public.warm_customer_budget (202608100004) is this organization's own
    -- per-customer money: envelope ceilings and the reserved/spent booked
    -- against them. A SUBJECT erasure tombstones the key and keeps the dollars,
    -- because the organization's accounting must survive one of its customers
    -- leaving. A TENANT deletion is the opposite case -- there is no
    -- organization left to account to -- so the rows go outright.
    delete from public.warm_customer_budget budget
     where budget.organization_id = p_organization_id;
    -- public.warm_control_savings_daily (202608100005) is this organization's
    -- own treated-versus-control comparison: counts and dollar means over
    -- prefix-hash units, with no customer key on it. It is org-level analytics
    -- about this tenant, so it leaves with the tenant.
    -- public.warm_prefix_node and public.warm_prefix_edge (202608100006) are
    -- this organization's own prefix STRUCTURE: salted block digests, their
    -- parent links and the token weights on them. The salt is per
    -- organization, so no row here is comparable to any other tenant's -- and
    -- once this tenant is gone the rows describe the shape of prompts nobody
    -- is entitled to reason about any more. They carry no foreign key (an FK
    -- to public.customers would let an erasure abort a live observation), so
    -- nothing cascades them away and these deletes are the only thing that
    -- erases them.
    delete from public.warm_prefix_edge edge
     where edge.organization_id = p_organization_id;
    delete from public.warm_prefix_node node
     where node.organization_id = p_organization_id;
    -- public.warm_attribution_daily, public.warm_attribution_residual and
    -- public.warm_prefix_cost_miss (202608100008) are this organization's own
    -- attribution accounting: what warming spent on it, which of its customers
    -- the spend reached, and which windows it bought that nobody used. A
    -- SUBJECT erasure tombstones the customer key and keeps the dollars,
    -- because the organization's accounting must survive one of its customers
    -- leaving. A TENANT deletion is the opposite case -- there is no
    -- organization left to account to -- so the rows go outright. None of the
    -- three carries a foreign key, for the same reason the tree does not, so
    -- these deletes are the only thing that erases them.
    delete from public.warm_prefix_cost_miss miss
     where miss.organization_id = p_organization_id;
    delete from public.warm_attribution_daily statement
     where statement.organization_id = p_organization_id;
    delete from public.warm_attribution_residual statement
     where statement.organization_id = p_organization_id;
    delete from public.warm_control_savings_daily savings
     where savings.organization_id = p_organization_id;
    -- public.warm_org_mode (202608100005) is the guardrail's per-provider
    -- freeze state for this organization. Leaving it behind would freeze -- or
    -- silently un-freeze -- a future organization that happened to reuse the
    -- id, and it names a tenant that no longer exists.
    delete from public.warm_org_mode mode
     where mode.organization_id = p_organization_id;
    -- public.warm_canary_probes and public.warm_canary_ledger (202608100005)
    -- are deliberately NOT touched, exactly as public.warm_ttl_observations is
    -- not: Plane G. A canary probe is a synthetic prefix Brevitas generated
    -- from a seed and sent with Brevitas's own credential against Brevitas's
    -- own account. There is no organization, customer or prefix key anywhere
    -- in either table, so there is nothing here belonging to this tenant to
    -- erase, and both age on public.purge_warm_state.
    -- usage_log.warm_prefix_hash (202608090002) is a sha256 of this customer's
    -- own prompt prefix, so it is a pseudonymous identifier and not content-free
    -- financial evidence. The frozen inner body enumerates the columns it
    -- minimizes and was written before this column existed, so it clears every
    -- other identifier on the rows the settlement invariant forces us to keep
    -- and leaves this one behind. Nulling a column changes no row count, so both
    -- preservation invariants below still hold.
    update public.usage_log usage
       set warm_prefix_hash = null
     where usage.organization_id = p_organization_id
       and usage.warm_prefix_hash is not null;
    -- public.warm_ttl_observations is intentionally NOT deleted, and this is a
    -- deliberate two-plane decision rather than an oversight. Its rows are
    -- (provider, model class, TTL tier, gap, warm/expired, ping/arrival,
    -- timestamp): provider physics with no organization, no customer and no
    -- prefix hash, so there is nothing in it belonging to this data subject to
    -- erase. It ages out on the 365-day aggregate horizon in
    -- public.purge_warm_state.
    -- public.warm_budget_ledger is intentionally NOT deleted. It is content-free
    -- (organization, provider, day, reserved/spent money) and it is the operand
    -- billing_period_settlement_evidence recomputes the warm deduction from
    -- (202607280008:344-350), so erasing it would silently raise the fee ceiling
    -- for a retained period. It ages out on its own retention horizon in
    -- public.purge_warm_state.

    update public.billing_accounts account
       set checkout_session_id = null,
           updated_at = pg_catalog.clock_timestamp()
     where account.organization_id = p_organization_id;
    update public.billing_events event
       set session_id = ''
     where event.organization_id = p_organization_id;

    foreach v_identity_id in array v_identity_ids loop
        perform public.compliance_anonymize_unshared_user(v_identity_id);
    end loop;

    select count(*) into v_billing_after
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    if v_billing_after <> v_billing_before then
        raise exception 'company financial preservation invariant failed'
            using errcode = '55000';
    end if;
    select count(*) into v_settlement_after
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id = usage.organization_id
                      and settlement.status <> 'void'
                      and settlement.usage_log_watermark_id >= usage.id);
    if v_settlement_after <> v_settlement_before then
        raise exception 'company settlement evidence preservation invariant failed'
            using errcode = '55000';
    end if;
    return v_result;
end;
$function$;

-- ---------------------------------------------------------------------------
-- compliance_delete_subject (202608100006:941-1090) + the attribution
-- tombstones. Same posture as public.warm_customer_budget: the key dies, the
-- dollars live.
-- ---------------------------------------------------------------------------

create or replace function public.compliance_delete_subject(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns text
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
declare
    v_result text;
    v_request_scope text;
    v_subject_id uuid;
    v_billing_before bigint;
    v_billing_after bigint;
    v_settlement_before bigint;
    v_settlement_after bigint;
begin
    select request.request_scope, request.subject_id
      into v_request_scope, v_subject_id
      from public.data_subject_requests request
     where request.id = p_request_id
       and request.organization_id = p_organization_id;
    select count(*) into v_billing_before
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    select count(*) into v_settlement_before
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id = usage.organization_id
                      and settlement.status <> 'void'
                      and settlement.usage_log_watermark_id >= usage.id);

    v_result := public.compliance_delete_subject_pre_company_identity(
        p_organization_id, p_request_id, p_actor_id
    );

    -- public.warm_decision_log (202608090001) is per-customer behavioral
    -- evidence and carries NO foreign key on purpose -- an FK to
    -- public.customers would let an erasure block or abort a warming claim,
    -- which is a money path. 202608090001 wired the tenant path and not this
    -- one, so a deleted customer's scored prefixes survived their customer row.
    -- Scoped to the customer path: a 'member' request erases an operator of the
    -- organization, not one of its end customers, and warm_decision_log has no
    -- member dimension to erase.
    if v_request_scope <> 'member' and v_subject_id is not null then
        delete from public.warm_decision_log entry
         where entry.organization_id = p_organization_id
           and entry.customer_id = v_subject_id;
        -- public.warm_customer_state (202608100003) is the learned profile for
        -- this subject, across every provider. Deleting it is necessary and NOT
        -- sufficient: the very next arrival would rebuild it from scratch,
        -- because warm_prefix_observe writes state unconditionally. The
        -- suppression row below is what makes the deletion stick.
        delete from public.warm_customer_state state
         where state.organization_id = p_organization_id
           and state.customer_id = v_subject_id;
        -- THE SUPPRESSION ROW IS DELIBERATELY NOT DELETED BY THIS FUNCTION, NOW
        -- OR ON ANY LATER RUN. It is the memory of the deletion request, and a
        -- deletion path that erased its own memory would re-admit the subject
        -- to the model on their next request. Twilio Segment's
        -- deletion-and-suppression contract, the closest B2B2C processor
        -- precedent (plan Part 0). It leaves only with the tenant.
        insert into public.warm_modeling_suppression (
            organization_id, customer_id)
        values (p_organization_id, v_subject_id)
        on conflict (organization_id, customer_id) do nothing;
        -- public.warm_customer_budget (202608100004) is deliberately NOT
        -- deleted. Its rows are dollars the organization's daily budget was
        -- measured against, and deleting them would silently restate org-level
        -- accounting for a period that may already be settled. The customer KEY
        -- is destroyed instead, so the row survives as an unlinkable sum.
        --
        -- DELIBERATE DEVIATION FROM THE PLAN. The plan (Part 0) says
        -- "HMAC-tombstone"; this is a RANDOM tombstone, which is strictly
        -- stronger. An HMAC is reversible by whoever holds the key, so Brevitas
        -- would retain the ability to re-link an erased subject to their spend
        -- and would acquire a key to hold, rotate and lose. This token comes
        -- from gen_random_uuid() and clock_timestamp() and is unlinkable even
        -- to us. It meets the plan's actual requirement -- customer key
        -- tombstoned, dollar sums preserved -- and gives up only the ability to
        -- link one erased subject's rows to each other across periods, which is
        -- precisely what erasure is meant to destroy. Per-row randomness is
        -- also what keeps the primary key unique when the same subject holds
        -- rows in several periods or providers.
        update public.warm_customer_budget budget
           set customer_ref = 'erased:' || encode(sha256(convert_to(
                   gen_random_uuid()::text || gen_random_uuid()::text
                   || clock_timestamp()::text, 'UTF8')), 'hex'),
               updated_at = pg_catalog.clock_timestamp()
         where budget.organization_id = p_organization_id
           and budget.customer_ref = v_subject_id::text;
        -- public.warm_attribution_daily (202608100008) is the same case and
        -- takes the same posture: the row is dollars -- a share of warming
        -- spend and the savings measured against it -- and deleting it would
        -- silently restate a day's attribution that an operator may already
        -- have read. The customer KEY is destroyed and the sums survive as an
        -- unlinkable row, with the same RANDOM tombstone
        -- public.warm_customer_budget uses above and for the same reason: an
        -- HMAC would leave Brevitas able to re-link an erased subject to their
        -- spend. public.warm_attribution_residual holds no customer key at all
        -- and is untouched.
        update public.warm_attribution_daily statement
           set customer_ref = 'erased:' || encode(sha256(convert_to(
                   gen_random_uuid()::text || gen_random_uuid()::text
                   || clock_timestamp()::text, 'UTF8')), 'hex')
         where statement.organization_id = p_organization_id
           and statement.customer_ref = v_subject_id::text;
        -- public.warm_prefix_cost_miss names the beneficiary the POLICY
        -- PREDICTED for a window nobody arrived in -- a guess about this
        -- subject, attached to a dollar amount that was never charged to
        -- anyone. The dollar stays as Brevitas's own speculative loss; the
        -- guess about a named person does not survive their erasure.
        update public.warm_prefix_cost_miss miss
           set predicted_customer_ref = 'erased:' || encode(sha256(convert_to(
                   gen_random_uuid()::text || gen_random_uuid()::text
                   || clock_timestamp()::text, 'UTF8')), 'hex')
         where miss.organization_id = p_organization_id
           and miss.predicted_customer_ref = v_subject_id::text;
        -- The organization-aggregate row (nil customer uuid) is intentionally
        -- untouched. It is an aggregate over the whole tenant and carries no
        -- subject key; the erased subject's decayed contribution to it is not
        -- attributable to anyone and fades on the 14-day half-life. Subtracting
        -- it would require retaining exactly the per-subject mass this request
        -- deletes.
    end if;
    -- usage_log.warm_prefix_hash (202608090002) survives on the rows the
    -- financial-preservation invariant forces us to retain, and it is derived
    -- from this subject's own prompt prefix. The frozen inner body enumerates
    -- the columns it minimizes and predates the column. Nulling it changes no
    -- row count, so both invariants below still hold. It is cleared for both
    -- scopes: the member path minimizes rows by owner_id, and those rows can
    -- carry a hash too.
    update public.usage_log usage
       set warm_prefix_hash = null
     where usage.organization_id = p_organization_id
       and usage.warm_prefix_hash is not null
       and ((v_request_scope = 'member'
             and usage.owner_id = v_subject_id::text)
            or (v_request_scope <> 'member'
                and usage.customer_id = v_subject_id));

    if v_request_scope = 'member' then
        update public.billing_accounts account
           set checkout_session_id = null,
               updated_at = pg_catalog.clock_timestamp()
         where account.organization_id = p_organization_id
           and account.user_id = v_subject_id;
        update public.billing_events event
           set session_id = ''
         where event.organization_id = p_organization_id
           and event.user_id = v_subject_id;
        perform public.compliance_anonymize_unshared_user(v_subject_id);
    end if;

    select count(*) into v_billing_after
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    if v_billing_after <> v_billing_before then
        raise exception 'subject company financial preservation invariant failed'
            using errcode = '55000';
    end if;
    select count(*) into v_settlement_after
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id = usage.organization_id
                      and settlement.status <> 'void'
                      and settlement.usage_log_watermark_id >= usage.id);
    if v_settlement_after <> v_settlement_before then
        raise exception 'subject company settlement evidence preservation invariant failed'
            using errcode = '55000';
    end if;
    return v_result;
end;
$function$;

-- ---------------------------------------------------------------------------
-- compliance_export_tenant (202608100006:1094-1464) + the attribution
-- statement and its footer.
-- ---------------------------------------------------------------------------

create or replace function public.compliance_export_tenant(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns setof jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
begin
    return query
        select exported.record
          from public.compliance_export_tenant_pre_company_identity(
                p_organization_id, p_request_id, p_actor_id
          ) as exported(record)
         where coalesce(exported.record->>'record_type', '')
               not in ('billing_account', 'billing_ledger', 'legacy_billing_event');

    -- A completed request is intentionally replay-empty, matching the original
    -- RPC. The private implementation changes an approved request to processing
    -- before returning its records.
    if not exists (
        select 1
          from public.data_subject_requests request
         where request.id = p_request_id
           and request.organization_id = p_organization_id
           and request.request_type = 'export'
           and request.request_scope = 'tenant'
           and request.status = 'processing'
    ) then
        return;
    end if;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'billing_account',
            'data', pg_catalog.jsonb_build_object(
                'user_id', account.user_id,
                'stripe_customer_id', account.stripe_customer_id,
                'stripe_subscription_id', account.stripe_subscription_id,
                'subscription_status', account.subscription_status,
                'checkout_session_id', account.checkout_session_id,
                'billing_started_at', account.billing_started_at,
                'current_period_start', account.current_period_start,
                'current_period_end', account.current_period_end,
                'last_invoice_id', account.last_invoice_id,
                'last_invoice_status', account.last_invoice_status,
                'stripe_subscription_event_created',
                    account.stripe_subscription_event_created,
                'stripe_invoice_event_created',
                    account.stripe_invoice_event_created,
                'created_at', account.created_at,
                'updated_at', account.updated_at
            )
        )
          from public.billing_accounts account
         where account.organization_id = p_organization_id;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'billing_ledger',
            'data', pg_catalog.jsonb_build_object(
                'id', ledger.id,
                'usage_log_id', ledger.usage_log_id,
                'user_id', ledger.user_id,
                'occurred_at', ledger.occurred_at,
                'fee_microusd', ledger.fee_microusd,
                'status', ledger.status,
                'attempts', ledger.attempts,
                'reported_at', ledger.reported_at,
                'last_error', ledger.last_error,
                'created_at', ledger.created_at
            )
        )
          from public.billing_ledger ledger
         where ledger.organization_id = p_organization_id
         order by ledger.id;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'legacy_billing_event',
            'data', pg_catalog.to_jsonb(event)
        )
          from public.billing_events event
         where event.organization_id = p_organization_id
         order by event.ts, event.id;

    -- Warming state, absent from the original record set because
    -- 202607280001 landed later. Ciphertext columns
    -- (warm_credentials.credential_ciphertext, warm_prefixes.payload_ciphertext)
    -- are reported as present but NOT emitted: the portable-export envelope
    -- decoder allowlists exactly three kind/purpose pairs
    -- (scripts/dr/portable-export.py:45-54) and hard-fails on any other, so
    -- emitting them as 'encrypted_content' today would break every export
    -- artifact. Adding the two purposes there is a prerequisite for exporting
    -- the ciphertext itself; until then the subject learns the record exists,
    -- which is what the omission previously hid.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_credential',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', credential.organization_id,
                'provider', credential.provider,
                'credential_configured', true,
                'credential_ciphertext_exported', false,
                'enabled', credential.enabled,
                'consent_actor_id', credential.consent_actor_id,
                'consent_at', credential.consent_at,
                'daily_budget_usd', credential.daily_budget_usd,
                'max_warm_customers', credential.max_warm_customers,
                'max_pings_per_customer_day', credential.max_pings_per_customer_day,
                'credential_state', credential.credential_state,
                'created_at', credential.created_at,
                'updated_at', credential.updated_at
            )
        )
          from public.warm_credentials credential
         where credential.organization_id = p_organization_id
         order by credential.provider;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_prefix',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', prefix.organization_id,
                'customer_id', prefix.customer_id,
                'provider', prefix.provider,
                'prefix_hash', prefix.prefix_hash,
                'payload_ciphertext_exported', false,
                'prefix_tokens', prefix.prefix_tokens,
                'provider_ttl_seconds', prefix.provider_ttl_seconds,
                'ping_reserve_usd', prefix.ping_reserve_usd,
                'arrival_count', prefix.arrival_count,
                'ewma_interarrival_s', prefix.ewma_interarrival_s,
                'hour_histogram', prefix.hour_histogram,
                'warm_pings', prefix.warm_pings,
                'warm_hits', prefix.warm_hits,
                'warm_misses', prefix.warm_misses,
                'consecutive_misses', prefix.consecutive_misses,
                'pings_today', prefix.pings_today,
                'pings_today_date', prefix.pings_today_date,
                'state', prefix.state,
                'created_at', prefix.created_at,
                'last_seen_at', prefix.last_seen_at,
                'next_due_at', prefix.next_due_at,
                'expires_at', prefix.expires_at
            )
        )
          from public.warm_prefixes prefix
         where prefix.organization_id = p_organization_id
         order by prefix.customer_id, prefix.provider, prefix.prefix_hash;

    -- The ledger projection is 202608090002's, unchanged. The two new ledger
    -- columns are deliberately NOT added here: the pacing dual in force for a
    -- candidate is already carried, per decision, by warm_decision_log.lambda_index
    -- below, which is the row the org audits a decision from. Widening this
    -- projection is a separate change with its own review.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_budget_ledger',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', ledger.organization_id,
                'provider', ledger.provider,
                'day', ledger.day,
                'reserved_usd', ledger.reserved_usd,
                'spent_usd', ledger.spent_usd,
                'updated_at', ledger.updated_at
            )
        )
          from public.warm_budget_ledger ledger
         where ledger.organization_id = p_organization_id
         order by ledger.day, ledger.provider;

    -- Warming decisions. Portable in full: every column is a hash, a count, a
    -- probability, a dollar amount or an enum -- there is no prompt or response
    -- material to withhold, so unlike the ciphertext columns above nothing here
    -- is reported-but-omitted. The volume is bounded by the 90-day retention
    -- class 202608090002 registered in compliance_run_retention. The eight
    -- index fields are the belief the scorer acted on, and an export that drops
    -- them is an export the org cannot audit the policy from.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_decision',
            'data', pg_catalog.jsonb_build_object(
                'id', entry.id,
                'organization_id', entry.organization_id,
                'customer_id', entry.customer_id,
                'provider', entry.provider,
                'prefix_hash', entry.prefix_hash,
                'ts', entry.ts,
                'decision', entry.decision,
                'p_return', entry.p_return,
                'roi_floor', entry.roi_floor,
                'reserve_usd', entry.reserve_usd,
                'prefix_tokens', entry.prefix_tokens,
                'ewma_interarrival_s', entry.ewma_interarrival_s,
                'arrival_count', entry.arrival_count,
                'pings_today', entry.pings_today,
                'rng_seed', entry.rng_seed,
                'propensity', entry.propensity,
                'settle_outcome', entry.settle_outcome,
                'realized_net_usd', entry.realized_net_usd,
                'organic_counterfactual', entry.organic_counterfactual,
                'index_score', entry.index_score,
                'index_density', entry.index_density,
                'v_hit_usd', entry.v_hit_usd,
                'chain_cost_usd', entry.chain_cost_usd,
                'c_belief_usd', entry.c_belief_usd,
                'p_alive', entry.p_alive,
                'organic_multiplier', entry.organic_multiplier,
                'lambda_index', entry.lambda_index
            )
        )
          from public.warm_decision_log entry
         where entry.organization_id = p_organization_id
         order by entry.ts, entry.id;

    -- The learned behavioural state (202608100003). Portable in full: decayed
    -- counts, exposure hours, timestamps and a regime label -- no prompt or
    -- response material, so nothing here is reported-but-omitted. The
    -- organization-aggregate row IS included at tenant scope: it is the
    -- organization's own aggregate and the organization is the subject here.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_customer_state',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', state.organization_id,
                'customer_id', state.customer_id,
                'provider', state.provider,
                'customer_key_hmac', state.customer_key_hmac,
                'hazard_n', state.hazard_n,
                'hazard_e', state.hazard_e,
                'events_total', state.events_total,
                'first_seen_at', state.first_seen_at,
                'last_seen_at', state.last_seen_at,
                'last_update_at', state.last_update_at,
                'regime', state.regime,
                'regime_score', state.regime_score,
                'regime_updated_at', state.regime_updated_at,
                'created_at', state.created_at,
                'updated_at', state.updated_at
            )
        )
          from public.warm_customer_state state
         where state.organization_id = p_organization_id
         order by state.customer_id, state.provider;

    -- The suppression list. An organization is entitled to see which of its own
    -- customers it has told us never to model again -- that is the record of
    -- deletion requests it processed, and withholding it would make the
    -- deletions unauditable.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_modeling_suppression',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', suppression.organization_id,
                'customer_id', suppression.customer_id,
                'suppressed_at', suppression.suppressed_at
            )
        )
          from public.warm_modeling_suppression suppression
         where suppression.organization_id = p_organization_id
         order by suppression.customer_id;

    -- The spend envelopes (202608100004). Ceilings, the money booked against
    -- them and where each ceiling came from, for every period. Tombstoned rows
    -- are INCLUDED at tenant scope: the dollars are the organization's own
    -- accounting and the key on them is already unlinkable to anybody, so
    -- withholding them would only make the organization's spend unauditable.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_customer_budget',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', budget.organization_id,
                'provider', budget.provider,
                'period_start', budget.period_start,
                'customer_ref', budget.customer_ref,
                'erased', budget.customer_ref like 'erased:%',
                'envelope_usd', budget.envelope_usd,
                'reserved_usd', budget.reserved_usd,
                'spent_usd', budget.spent_usd,
                'source', budget.source,
                'created_at', budget.created_at,
                'updated_at', budget.updated_at
            )
        )
          from public.warm_customer_budget budget
         where budget.organization_id = p_organization_id
         order by budget.period_start, budget.provider, budget.customer_ref;


    -- The attribution statement (202608100008). This is the answer to "who did
    -- warming actually spend money on", and an organization is entitled to its
    -- own copy of it. Dollars and counts only: no node digest, no path and
    -- nothing else derived from a prompt appears here or anywhere else in an
    -- export. MEASURED-ONLY -- none of these numbers was ever billed.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_attribution_daily',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', statement.organization_id,
                'provider', statement.provider,
                'day', statement.day,
                'customer_ref', statement.customer_ref,
                'erased', statement.customer_ref like 'erased:%',
                'nodes_read', statement.nodes_read,
                'nodes_shared', statement.nodes_shared,
                'avg_split_denominator', statement.avg_split_denominator,
                'warming_cost_share_usd', statement.warming_cost_share_usd,
                'warm_attributed_savings_usd', statement.warm_attributed_savings_usd,
                'verified_savings_usd', statement.verified_savings_usd,
                'net_usd', statement.net_usd,
                'computed_at', statement.computed_at
            )
        )
          from public.warm_attribution_daily statement
         where statement.organization_id = p_organization_id
         order by statement.day, statement.provider, statement.customer_ref;

    -- The footer that says what the statement above did NOT allocate. An
    -- organization reading its attribution is entitled to the whole dollar,
    -- including the part of it Brevitas spent on windows nobody used.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_attribution_residual',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', statement.organization_id,
                'provider', statement.provider,
                'day', statement.day,
                'total_warm_spend_usd', statement.total_warm_spend_usd,
                'allocated_usd', statement.allocated_usd,
                'unallocated_speculative_usd', statement.unallocated_speculative_usd,
                'redundancy_usd', statement.redundancy_usd,
                'unpriced_usd', statement.unpriced_usd,
                'reward_join_delta_usd', statement.reward_join_delta_usd,
                'computed_at', statement.computed_at
            )
        )
          from public.warm_attribution_residual statement
         where statement.organization_id = p_organization_id
         order by statement.day, statement.provider;

    -- public.warm_prefix_cost_miss is deliberately NOT exported. Every row in
    -- it is keyed on a node digest -- a pseudonymous identifier of a block of
    -- somebody's prefix -- and the dollars on it were never charged to this
    -- organization or to anybody in it. Exporting it would hand out tree
    -- structure in exchange for no accounting the organization is owed.

    -- The control comparison (202608100005). This is the measurement the
    -- organization's warming spend ceiling is derived from, so an organization
    -- that wants to know why its warming was throttled -- or was not -- is
    -- entitled to the arithmetic. Counts and means only; the per-unit costs
    -- behind them were never stored.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_control_savings',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', savings.organization_id,
                'provider', savings.provider,
                'day', savings.day,
                'treated_units', savings.treated_units,
                'control_units', savings.control_units,
                'treated_mean_cost_usd', savings.treated_mean_cost_usd,
                'control_mean_cost_usd', savings.control_mean_cost_usd,
                'control_lift_usd', savings.control_lift_usd,
                'computed_at', savings.computed_at
            )
        )
          from public.warm_control_savings_daily savings
         where savings.organization_id = p_organization_id
         order by savings.day, savings.provider;

    -- The guardrail's verdict (202608100005), with the reason string the
    -- worker wrote. An organization frozen back to flat priors should be able
    -- to see that it was, and why.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_org_mode',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', mode.organization_id,
                'provider', mode.provider,
                'mode', mode.mode,
                'reason', mode.reason,
                'updated_at', mode.updated_at
            )
        )
          from public.warm_org_mode mode
         where mode.organization_id = p_organization_id
         order by mode.provider;

    -- The prefix tree (202608100006), as COUNTS AND NOTHING ELSE. A node digest
    -- is a salted, content-derived identifier of one block of one customer's
    -- prompt prefix; exporting the digests or the paths would hand back a
    -- structural fingerprint of the tenant's own traffic in a form that can be
    -- correlated across an export boundary. The counts answer the question an
    -- export is for -- how much structure is held about us -- without being
    -- that fingerprint.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_prefix_tree',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', p_organization_id,
                'provider', counts.provider,
                'salt_version', counts.salt_version,
                'node_count', counts.node_count,
                'edge_count', counts.edge_count,
                'max_depth', counts.max_depth
            )
        )
          from (
            select node.provider,
                   node.salt_version,
                   count(*) as node_count,
                   (select count(*) from public.warm_prefix_edge edge
                     where edge.organization_id = node.organization_id
                       and edge.provider = node.provider
                       and edge.salt_version = node.salt_version) as edge_count,
                   max(node.depth) as max_depth
              from public.warm_prefix_node node
             where node.organization_id = p_organization_id
             group by node.organization_id, node.provider, node.salt_version
          ) counts
         order by counts.provider, counts.salt_version;
    -- public.warm_canary_probes and public.warm_canary_ledger are absent for
    -- the same reason public.warm_ttl_observations is: Plane G carries no
    -- tenant key, so there is no row in either table that belongs to this
    -- organization to export.
end;
$function$;

-- ---------------------------------------------------------------------------
-- compliance_export_subject (202608100006:1470-1717) + the subject's own
-- attribution rows. Nothing else about this function changes, and it is
-- restated in full because a migration that carries text forward must carry
-- the text it is carrying.
-- ---------------------------------------------------------------------------

create or replace function public.compliance_export_subject(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns setof jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
declare
    v_request public.data_subject_requests%rowtype;
begin
    return query
        select exported.record
          from public.compliance_export_subject_pre_company_identity(
                p_organization_id, p_request_id, p_actor_id
          ) as exported(record)
         where coalesce(exported.record->>'record_type', '')
               not in ('billing_account', 'billing_ledger', 'legacy_billing_event');

    select * into v_request
      from public.data_subject_requests request
     where request.id = p_request_id
       and request.organization_id = p_organization_id;
    if not found
       or v_request.request_type <> 'export'
       or v_request.request_scope not in ('member', 'customer')
       or v_request.status <> 'processing' then
        return;
    end if;

    -- Preserve the original member-subject semantics: billing evidence is a
    -- subject relationship only when that member is the compatibility owner,
    -- while organization_id prevents evidence from another owned company.
    -- Billing evidence stays a member-only relationship, exactly as
    -- 202607200011 defined it; the guard above now also admits a
    -- customer-scoped request, which must not reach it.
    if v_request.request_scope = 'member' then
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'billing_account',
            'data', pg_catalog.jsonb_build_object(
                'user_id', account.user_id,
                'stripe_customer_id', account.stripe_customer_id,
                'stripe_subscription_id', account.stripe_subscription_id,
                'subscription_status', account.subscription_status,
                'checkout_session_id', account.checkout_session_id,
                'billing_started_at', account.billing_started_at,
                'current_period_start', account.current_period_start,
                'current_period_end', account.current_period_end,
                'last_invoice_id', account.last_invoice_id,
                'last_invoice_status', account.last_invoice_status,
                'stripe_subscription_event_created',
                    account.stripe_subscription_event_created,
                'stripe_invoice_event_created',
                    account.stripe_invoice_event_created,
                'created_at', account.created_at,
                'updated_at', account.updated_at
            )
        )
          from public.billing_accounts account
         where account.organization_id = p_organization_id
           and account.user_id = v_request.subject_id;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'billing_ledger',
            'data', pg_catalog.jsonb_build_object(
                'id', ledger.id,
                'usage_log_id', ledger.usage_log_id,
                'user_id', ledger.user_id,
                'occurred_at', ledger.occurred_at,
                'fee_microusd', ledger.fee_microusd,
                'status', ledger.status,
                'attempts', ledger.attempts,
                'reported_at', ledger.reported_at,
                'last_error', ledger.last_error,
                'created_at', ledger.created_at
            )
        )
          from public.billing_ledger ledger
         where ledger.organization_id = p_organization_id
           and ledger.user_id = v_request.subject_id
         order by ledger.id;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'legacy_billing_event',
            'data', pg_catalog.to_jsonb(event)
        )
          from public.billing_events event
         where event.organization_id = p_organization_id
           and event.user_id = v_request.subject_id
         order by event.ts, event.id;
    end if;

    -- A customer-scoped subject request now also carries that customer's
    -- warming observations. warm_prefixes is keyed by (organization, customer),
    -- so the customer IS the subject here.
    if v_request.request_scope = 'customer' then
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_prefix',
                'data', pg_catalog.jsonb_build_object(
                    'organization_id', prefix.organization_id,
                    'customer_id', prefix.customer_id,
                    'provider', prefix.provider,
                    'prefix_hash', prefix.prefix_hash,
                    'payload_ciphertext_exported', false,
                    'prefix_tokens', prefix.prefix_tokens,
                    'provider_ttl_seconds', prefix.provider_ttl_seconds,
                    'arrival_count', prefix.arrival_count,
                    'ewma_interarrival_s', prefix.ewma_interarrival_s,
                    'hour_histogram', prefix.hour_histogram,
                    'warm_pings', prefix.warm_pings,
                    'warm_hits', prefix.warm_hits,
                    'warm_misses', prefix.warm_misses,
                    'state', prefix.state,
                    'created_at', prefix.created_at,
                    'last_seen_at', prefix.last_seen_at,
                    'next_due_at', prefix.next_due_at,
                    'expires_at', prefix.expires_at
                )
            )
              from public.warm_prefixes prefix
             where prefix.organization_id = p_organization_id
               and prefix.customer_id = v_request.subject_id
             order by prefix.provider, prefix.prefix_hash;

        -- warm_decision_log is keyed by (organization, customer) exactly as
        -- warm_prefixes is, so the customer is the subject here too. A prefix
        -- that has since expired leaves no warm_prefixes row but does leave
        -- decisions, which is the whole point of logging them; omitting these
        -- would repeat the 202607280016 gap on a newer table.
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_decision',
                'data', pg_catalog.jsonb_build_object(
                    'id', entry.id,
                    'organization_id', entry.organization_id,
                    'customer_id', entry.customer_id,
                    'provider', entry.provider,
                    'prefix_hash', entry.prefix_hash,
                    'ts', entry.ts,
                    'decision', entry.decision,
                    'p_return', entry.p_return,
                    'roi_floor', entry.roi_floor,
                    'reserve_usd', entry.reserve_usd,
                    'prefix_tokens', entry.prefix_tokens,
                    'ewma_interarrival_s', entry.ewma_interarrival_s,
                    'arrival_count', entry.arrival_count,
                    'pings_today', entry.pings_today,
                    'rng_seed', entry.rng_seed,
                    'propensity', entry.propensity,
                    'settle_outcome', entry.settle_outcome,
                    'realized_net_usd', entry.realized_net_usd,
                    'organic_counterfactual', entry.organic_counterfactual,
                    'index_score', entry.index_score,
                    'index_density', entry.index_density,
                    'v_hit_usd', entry.v_hit_usd,
                    'chain_cost_usd', entry.chain_cost_usd,
                    'c_belief_usd', entry.c_belief_usd,
                    'p_alive', entry.p_alive,
                    'organic_multiplier', entry.organic_multiplier,
                    'lambda_index', entry.lambda_index
                )
            )
              from public.warm_decision_log entry
             where entry.organization_id = p_organization_id
               and entry.customer_id = v_request.subject_id
             order by entry.ts, entry.id;

        -- The subject's own learned state (202608100003), keyed by
        -- (organization, customer) exactly as warm_prefixes is. The
        -- organization-aggregate row is deliberately NOT reachable here: its
        -- customer_id is the nil uuid, which can never equal a subject id, so
        -- the predicate excludes it by construction rather than by a filter
        -- somebody could drop.
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_customer_state',
                'data', pg_catalog.jsonb_build_object(
                    'organization_id', state.organization_id,
                    'customer_id', state.customer_id,
                    'provider', state.provider,
                    'customer_key_hmac', state.customer_key_hmac,
                    'hazard_n', state.hazard_n,
                    'hazard_e', state.hazard_e,
                    'events_total', state.events_total,
                    'first_seen_at', state.first_seen_at,
                    'last_seen_at', state.last_seen_at,
                    'last_update_at', state.last_update_at,
                    'regime', state.regime,
                    'regime_score', state.regime_score,
                    'regime_updated_at', state.regime_updated_at,
                    'created_at', state.created_at,
                    'updated_at', state.updated_at
                )
            )
              from public.warm_customer_state state
             where state.organization_id = p_organization_id
               and state.customer_id = v_request.subject_id
             order by state.provider;

        -- Whether this subject is suppressed is a fact about them, and an
        -- export that omitted it would leave them unable to confirm that an
        -- earlier deletion request is still being honoured.
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_modeling_suppression',
                'data', pg_catalog.jsonb_build_object(
                    'organization_id', suppression.organization_id,
                    'customer_id', suppression.customer_id,
                    'suppressed_at', suppression.suppressed_at
                )
            )
              from public.warm_modeling_suppression suppression
             where suppression.organization_id = p_organization_id
               and suppression.customer_id = v_request.subject_id;

        -- The subject's own spend envelopes (202608100004). LIVE rows only: a
        -- tombstoned row's customer_ref is a random token that can never equal
        -- a subject id, so it is excluded by construction rather than by a
        -- filter somebody could drop -- and an erased subject has no claim on
        -- the organization's residual accounting anyway.
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_customer_budget',
                'data', pg_catalog.jsonb_build_object(
                    'organization_id', budget.organization_id,
                    'provider', budget.provider,
                    'period_start', budget.period_start,
                    'customer_ref', budget.customer_ref,
                    'envelope_usd', budget.envelope_usd,
                    'reserved_usd', budget.reserved_usd,
                    'spent_usd', budget.spent_usd,
                    'source', budget.source,
                    'created_at', budget.created_at,
                    'updated_at', budget.updated_at
                )
            )
              from public.warm_customer_budget budget
             where budget.organization_id = p_organization_id
               and budget.customer_ref = v_request.subject_id::text
             order by budget.period_start, budget.provider;

        -- The subject's own attribution rows (202608100008): the share of
        -- warming spend their arrivals pulled toward them and the savings
        -- measured against it. LIVE rows only, and by construction rather than
        -- by a filter somebody could drop: a tombstoned customer_ref is a
        -- random token that can never equal a subject id. Counts and dollars;
        -- no node digest and no path.
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_attribution_daily',
                'data', pg_catalog.jsonb_build_object(
                    'organization_id', statement.organization_id,
                    'provider', statement.provider,
                    'day', statement.day,
                    'customer_ref', statement.customer_ref,
                    'nodes_read', statement.nodes_read,
                    'nodes_shared', statement.nodes_shared,
                    'avg_split_denominator', statement.avg_split_denominator,
                    'warming_cost_share_usd', statement.warming_cost_share_usd,
                    'warm_attributed_savings_usd', statement.warm_attributed_savings_usd,
                    'verified_savings_usd', statement.verified_savings_usd,
                    'net_usd', statement.net_usd,
                    'computed_at', statement.computed_at
                )
            )
              from public.warm_attribution_daily statement
             where statement.organization_id = p_organization_id
               and statement.customer_ref = v_request.subject_id::text
             order by statement.day, statement.provider;
    end if;
end;
$function$;
commit;
