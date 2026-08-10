-- Phase 1.5 learned warming, part 1: chain-hash prefix keying and the prefix
-- tree.
--
-- THE BUG THIS FIXES IS A HASHING BUG, NOT A DATABASE GAP. warm_prefixes is
-- keyed on prefix_hash: a sha256 of the WHOLE canonical payload. Two customers
-- sharing forty thousand tokens of system prompt therefore produce two
-- unrelated 64-hex strings, and every containment question -- is this prefix an
-- extension of that one, is this node already warm because a sibling is --
-- becomes unanswerable in SQL. Warming pays for the same shared prefix once per
-- arm because it cannot see that the arms share anything.
--
-- The fix is vLLM-style CHAIN HASHING, and it is ADDITIVE: prefix_hash is
-- untouched, byte for byte, because every stored row, every decision and the
-- reward join are keyed on it. The chain is a second artifact computed beside
-- it -- fixed-size blocks of framed, canonically-serialized elements, each
-- block's digest chaining the previous one -- so a shared leading prefix
-- produces a shared leading DIGEST SEQUENCE. Containment becomes a prefix test
-- on a path, with no content stored anywhere.
--
-- WHAT IS STORED, AND WHAT CANNOT BE RECOVERED FROM IT. public.warm_prefix_node
-- holds one row per block: a salted digest, its parent, its depth, its label,
-- and the token weight of that block. public.warm_prefix_edge holds the same
-- adjacency as an explicit relation, because the ltree path is an ACCELERATOR
-- and the parent link is the semantics -- every read in Phase 1.5 must be
-- expressible as a plain recursive walk, or the day a path is missing (past 253
-- blocks) the read silently means something different.
--
-- Node digests arrive already salted, and the salt is derived per organization
-- from a master that lives only on the hosted server: the customer-installed
-- package computes the UNSALTED chain and never holds the key. So a node digest
-- is a pseudonymous identifier of a block of a prefix, not of its content, and
-- two organizations with byte-identical system prompts share no rows at all.
-- Rotation is (new master, new version) together -- salt_version namespaces
-- every row -- and ERASURE IS NEVER IMPLEMENTED AS ROTATION: rotating leaves
-- the old rows exactly where they were, which is why tenant deletion below
-- deletes them outright.
--
-- MEASURED-ONLY. Nothing in this migration reads the tree. No claim decision,
-- no reserve, no settle, no fee basis and no savings number changes. This is
-- the substrate the dedup pre-pass and the attribution job are built on, and it
-- ships inert on purpose: the day the tree starts being read is not the day it
-- starts being written, because a structure with no history is a structure with
-- no answers.
--
-- WHAT THIS MIGRATION DOES NOT TOUCH. usage_log.verified_savings_usd, the
-- settlement sweep, warm_budget_ledger's reserve-then-settle arithmetic, the
-- claim-token fence, billing_period_settlement_evidence. Warm spend stays
-- never-billable.
--
-- Forward-only and idempotent.

-- REVERSE: DDL: drop public.warm_prefix_node and public.warm_prefix_edge; drop the three warm_prefixes chain_* columns (chain_leaf_digest, chain_path, chain_salt_version); drop the fourteen-argument public.warm_prefix_observe and re-apply 202608100003_warm_customer_state_hazard.sql's eleven-argument public.warm_prefix_observe verbatim with its revoke/grant and 202608090001_warm_instrumentation_tables.sql's comment; re-apply 202608100005_warm_beta_cap_guardrail.sql's public.purge_warm_state, public.compliance_delete_tenant and public.compliance_export_tenant verbatim; re-apply 202608100004_warm_customer_budget_envelopes.sql's public.compliance_delete_subject and public.compliance_export_subject verbatim; the ltree extension is left installed because dropping it would drop the type out from under any other user of it

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
        'public.warm_ttl_observe(text,text,text,numeric,text,text)',
        'public.warm_ttl_tier(integer)',
        'public.warm_customer_state_touch(uuid,uuid,text,timestamptz)',
        'public.compliance_delete_tenant_pre_company_identity(uuid,uuid,text)',
        'public.compliance_delete_subject_pre_company_identity(uuid,uuid,text)',
        'public.compliance_export_tenant_pre_company_identity(uuid,uuid,text)',
        'public.compliance_export_subject_pre_company_identity(uuid,uuid,text)',
        'public.compliance_anonymize_unshared_user(uuid)',
        'public.compliance_actor_role(text)'
    ] loop
        if to_regprocedure(required_routine) is null then
            raise exception using
                errcode = '55000',
                message = '202608100006 requires ' || required_routine;
        end if;
    end loop;
    -- Either shape satisfies this, and deliberately: the eleven-argument form
    -- is what this migration carries forward, and the fourteen-argument form is
    -- what it installs, so a re-application of an already-applied migration
    -- must not fail its own precondition. Same rule as 202608100005:97-107.
    if to_regprocedure('public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric,text)') is null
       and to_regprocedure('public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric,text,jsonb,text,integer)') is null then
        raise exception using
            errcode = '55000',
            message = '202608100006 requires public.warm_prefix_observe';
    end if;
    if to_regclass('public.warm_prefixes') is null
       or to_regclass('public.warm_credentials') is null
       or to_regclass('public.warm_customer_state') is null
       or to_regclass('public.warm_modeling_suppression') is null
       or to_regclass('public.warm_customer_budget') is null
       or to_regclass('public.warm_control_savings_daily') is null
       or to_regclass('public.warm_org_mode') is null
       or to_regclass('public.usage_log') is null then
        raise exception using
            errcode = '55000',
            message = '202608100006 requires the warming tables through 202608100005';
    end if;
end;
$migration_precondition$;

-- ---------------------------------------------------------------------------
-- THE PATH TYPE. ltree is a core contrib extension and lives in the
-- `extensions` schema, which is where Supabase puts every extension and out of
-- which nothing is implicitly searchable: the type is referenced schema
-- qualified everywhere below, because every function here pins
-- search_path = pg_catalog, public.
--
-- The extension is created here rather than assumed. An installation that
-- already has it somewhere else is moved, not duplicated -- two ltree types
-- would make `extensions.ltree` mean nothing.
-- ---------------------------------------------------------------------------
create schema if not exists extensions;
grant usage on schema extensions to public;

do $ltree_extension$
declare
    v_schema text;
begin
    select namespace.nspname into v_schema
      from pg_catalog.pg_extension extension
      join pg_catalog.pg_namespace namespace
        on namespace.oid = extension.extnamespace
     where extension.extname = 'ltree';
    if v_schema is null then
        execute 'create extension ltree with schema extensions';
    elsif v_schema <> 'extensions' then
        execute 'alter extension ltree set schema extensions';
    end if;
end;
$ltree_extension$;

-- ---------------------------------------------------------------------------
-- THE TREE. One row per block of one chain, per (organization, provider, salt
-- version). node_digest is the primary key because the chain already binds
-- every ancestor and the block index into it: two arms that reach the same
-- node_digest have provably identical prefixes up to that block, which is the
-- whole containment claim.
--
-- path is the ACCELERATOR and parent_digest is the SEMANTICS. Past 253 blocks
-- the ltree label budget is exhausted and path is null; those nodes are still
-- complete rows, still chained by parent_digest, and every read must be written
-- so that it still answers correctly for them.
-- ---------------------------------------------------------------------------
create table if not exists public.warm_prefix_node (
    organization_id uuid not null,
    provider text not null check (provider in ('anthropic','openai','deepseek')),
    salt_version smallint not null check (salt_version between 1 and 9999),
    node_digest text not null check (node_digest ~ '^[0-9a-f]{64}$'),
    parent_digest text check (parent_digest is null or parent_digest ~ '^[0-9a-f]{64}$'),
    depth integer not null check (depth between 0 and 300),
    label text not null check (label ~ '^[0-9a-f]{12}(_[0-9a-f]{4})?$'),
    block_tokens integer not null check (block_tokens between 0 and 2000000000),
    token_cum integer not null check (token_cum between 0 and 2000000000),
    block_elements integer not null default 0 check (block_elements >= 0),
    path extensions.ltree,              -- null for depth > 253 (adjacency-only tail)
    path_truncated boolean not null default false,
    created_at timestamptz not null default now(),
    last_touch_at timestamptz not null default now(),
    primary key (organization_id, provider, salt_version, node_digest)
);

-- gist_ltree_ops, never btree on path: a btree index tuple cannot hold a deep
-- path, and ancestor/descendant containment is a gist operator anyway.
create index if not exists warm_prefix_node_path_gist on public.warm_prefix_node
    using gist (path);
create unique index if not exists warm_prefix_node_path_uq on public.warm_prefix_node
    (organization_id, provider, salt_version, path) where path is not null;
create index if not exists warm_prefix_node_parent_idx on public.warm_prefix_node
    (organization_id, provider, salt_version, parent_digest);
create index if not exists warm_prefix_node_touch_idx on public.warm_prefix_node
    (last_touch_at);

-- The adjacency relation, stored explicitly rather than derived from
-- parent_digest, because the recursive read is the definition of ancestry here
-- and it must not depend on a materialized path that deep chains do not have.
create table if not exists public.warm_prefix_edge (
    organization_id uuid not null,
    provider text not null check (provider in ('anthropic','openai','deepseek')),
    salt_version smallint not null,
    parent_digest text not null check (parent_digest ~ '^[0-9a-f]{64}$'),
    child_digest text not null check (child_digest ~ '^[0-9a-f]{64}$'),
    primary key (organization_id, provider, salt_version, parent_digest, child_digest)
);

-- RLS on, ZERO policies: reachable only through the SECURITY DEFINER writer
-- below, exactly like every other warming table. A policy here would be a
-- second, weaker way in.
alter table public.warm_prefix_node enable row level security;
alter table public.warm_prefix_edge enable row level security;
revoke all on table public.warm_prefix_node
    from public, anon, authenticated, service_role;
revoke all on table public.warm_prefix_edge
    from public, anon, authenticated, service_role;

-- The arm's own stamp: which leaf this arm's prefix currently reaches, the path
-- that reached it, and the salt version both are expressed in. Nullable and
-- null on every existing row, because an arm observed before this migration has
-- no chain and inventing one would be a lie about what was measured.
alter table public.warm_prefixes
    add column if not exists chain_leaf_digest text,
    add column if not exists chain_path text,
    add column if not exists chain_salt_version smallint;

do $chain_leaf_check$
begin
    if not exists (
        select 1 from pg_catalog.pg_constraint
         where conrelid = 'public.warm_prefixes'::regclass
           and conname = 'warm_prefixes_chain_leaf_digest_check'
    ) then
        alter table public.warm_prefixes
            add constraint warm_prefixes_chain_leaf_digest_check
            check (chain_leaf_digest is null
                   or chain_leaf_digest ~ '^[0-9a-f]{64}$');
    end if;
end;
$chain_leaf_check$;

-- ---------------------------------------------------------------------------
-- warm_prefix_observe (202608100003:501-715) + the chain writes. The
-- eleven-argument form is REPLACED, not overloaded: three defaulted arguments
-- on top of it would make an eleven-argument call ambiguous.
-- ---------------------------------------------------------------------------
drop function if exists public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric, text
);
create or replace function public.warm_prefix_observe(
    p_organization_id uuid,
    p_customer_id uuid,
    p_provider text,
    p_prefix_hash text,
    p_payload_ciphertext text,
    p_prefix_tokens integer,
    p_provider_ttl_seconds integer,
    p_safety_margin_seconds integer,
    p_cache_read boolean,
    -- null means the caller predates observer pricing: keep the stored value.
    p_ping_reserve_usd numeric default null,
    -- Provider catalog model family for the TTL sensor. The model itself lives
    -- inside payload_ciphertext, which SQL cannot read, so the caller supplies
    -- the coarsened class. '' means "unknown", and the observation is still
    -- recorded: the provider and TTL tier alone are useful physics.
    p_model_class text default '',
    -- 202608100006. The SALTED prefix chain for this arrival: an ordered array
    -- of node objects (digest, label, parent, depth, block_tokens, token_cum,
    -- block_elements, path_truncated), the dotted ltree path they materialize,
    -- and the salt version that namespaces both. All three null -- the ordinary
    -- case for a deployment with no chain salt configured -- writes no tree and
    -- changes nothing else about the observation.
    p_chain jsonb default null,
    p_chain_path text default null,
    p_chain_salt_version integer default null
) returns jsonb as $$
declare
    v_now timestamptz := clock_timestamp();
    v_bucket_key text;
    v_customer_count integer;
    -- Pre-upsert snapshot of the provable-touch clock, read before the row is
    -- rewritten. Null on a first observation and on rows predating 202608090001.
    v_prior_touch timestamptz;
    v_gap_seconds numeric;
    -- 202608100006 chain locals.
    v_chain_ok boolean;
    v_chain_count integer;
    v_chain_blocks integer;
    v_chain_truncated boolean := false;
    v_chain_labels text[];
    v_chain_node jsonb;
    v_chain_depth integer;
    v_chain_upper integer;
    v_chain_digest text;
    v_chain_label text;
    v_chain_parent text;
    v_chain_attempt integer;
    -- Resolved BEFORE anything is written, so the whole tree lands in one
    -- statement. Index d+1 holds depth d.
    v_chain_node_labels text[];
    v_chain_paths text[];
    v_chain_stored text;
begin
    if p_organization_id is null or p_customer_id is null then
        raise exception 'warm observation requires an organization and customer';
    end if;
    if p_provider not in ('anthropic', 'openai', 'deepseek') then
        raise exception 'warm observation provider is invalid';
    end if;
    if p_prefix_hash !~ '^[0-9a-f]{64}$' then
        raise exception 'warm observation prefix hash is invalid';
    end if;
    if octet_length(p_payload_ciphertext) < 1
       or octet_length(p_payload_ciphertext) > 16777216 then
        raise exception 'warm observation payload exceeds its absolute bound';
    end if;
    if p_prefix_tokens not between 0 and 2000000000
       or coalesce(p_provider_ttl_seconds, 0) not between 60 and 86400
       or coalesce(p_safety_margin_seconds, -1) not between 0 and 3600
       or coalesce(p_ping_reserve_usd, 0) not between 0 and 99999999 then
        raise exception 'warm observation bounds are invalid';
    end if;

    -- Serialized per organization+provider so concurrent replicas cannot each
    -- observe a below-cap snapshot of this org's customer cap. The key is
    -- deliberately not fleet-global: one busy org's observations must never
    -- queue every other org's behind a single mutex.
    perform pg_advisory_xact_lock(
        hashtextextended('brevitas.warm_prefixes.write_bound.v1:'
                         || p_organization_id::text || ':' || p_provider, 0)
    );

    if not exists (
        select 1 from public.warm_credentials cred
         where cred.organization_id = p_organization_id
           and cred.provider = p_provider
           and cred.enabled
           and cred.credential_state = 'active'
    ) then
        return jsonb_build_object(
            'schema', 'brevitas.warm-observe.v1', 'status', 'not_enabled'
        );
    end if;

    delete from public.warm_prefixes prefix
     where prefix.organization_id = p_organization_id
       and prefix.expires_at <= v_now;

    if not exists (
        select 1 from public.warm_prefixes prefix
         where prefix.organization_id = p_organization_id
           and prefix.customer_id = p_customer_id
           and prefix.provider = p_provider
    ) then
        select count(distinct prefix.customer_id) into v_customer_count
          from public.warm_prefixes prefix
         where prefix.organization_id = p_organization_id
           and prefix.provider = p_provider;
        if v_customer_count >= (
            select cred.max_warm_customers from public.warm_credentials cred
             where cred.organization_id = p_organization_id
               and cred.provider = p_provider
        ) then
            return jsonb_build_object(
                'schema', 'brevitas.warm-observe.v1', 'status', 'customer_cap'
            );
        end if;
    end if;

    v_bucket_key := ((extract(isodow from (v_now at time zone 'utc'))::integer - 1) * 24
                     + extract(hour from (v_now at time zone 'utc'))::integer)::text;

    -- Read the touch clock before the upsert overwrites it. The advisory lock
    -- taken above already serializes this organization+provider, so no other
    -- observation can move it between this read and the write.
    select prefix.last_touch_at into v_prior_touch
      from public.warm_prefixes prefix
     where prefix.organization_id = p_organization_id
       and prefix.customer_id = p_customer_id
       and prefix.provider = p_provider
       and prefix.prefix_hash = p_prefix_hash;

    insert into public.warm_prefixes as prefix (
        organization_id, customer_id, provider, prefix_hash,
        payload_ciphertext, prefix_tokens, provider_ttl_seconds,
        ping_reserve_usd, arrival_count, ewma_interarrival_s, hour_histogram,
        created_at, last_seen_at, last_touch_at, next_due_at, expires_at
    ) values (
        p_organization_id, p_customer_id, p_provider, p_prefix_hash,
        p_payload_ciphertext, p_prefix_tokens, p_provider_ttl_seconds,
        coalesce(p_ping_reserve_usd, 0),
        1, null, jsonb_build_object(v_bucket_key, 1),
        v_now, v_now, v_now,
        v_now + make_interval(secs => greatest(
            1, p_provider_ttl_seconds - p_safety_margin_seconds)),
        v_now + interval '7 days'
    )
    on conflict (organization_id, customer_id, provider, prefix_hash) do update set
        payload_ciphertext = excluded.payload_ciphertext,
        prefix_tokens = excluded.prefix_tokens,
        provider_ttl_seconds = excluded.provider_ttl_seconds,
        ping_reserve_usd = coalesce(p_ping_reserve_usd, prefix.ping_reserve_usd),
        arrival_count = prefix.arrival_count + 1,
        ewma_interarrival_s = round(coalesce(
            0.3 * extract(epoch from (v_now - prefix.last_seen_at))
            + 0.7 * prefix.ewma_interarrival_s,
            extract(epoch from (v_now - prefix.last_seen_at))), 3),
        hour_histogram = jsonb_set(
            prefix.hour_histogram, array[v_bucket_key],
            to_jsonb(coalesce((prefix.hour_histogram ->> v_bucket_key)::integer, 0) + 1)),
        warm_hits = prefix.warm_hits
            + case when p_cache_read and prefix.warm_pings > 0 then 1 else 0 end,
        warm_misses = prefix.warm_misses
            + case when not p_cache_read and prefix.warm_pings > 0 then 1 else 0 end,
        consecutive_misses = case when p_cache_read then 0
            else prefix.consecutive_misses end,
        state = 'active',
        last_seen_at = v_now,
        -- An arrival provably wrote or refreshed the provider-side entry --
        -- that is what "cache read" and "cache write" both mean -- so it is a
        -- touch regardless of p_cache_read.
        last_touch_at = v_now,
        -- Fence the schedule on the claim lease. warm_due_claim pushes
        -- next_due_at out past a full sequential worker batch and rotates
        -- claim_token (202607280003:352-358) precisely so an unsynchronized
        -- replica cannot re-claim the tail of a batch mid-flight. Warming
        -- targets HOT prefixes, so an arrival inside that lease window is the
        -- normal case -- and recomputing next_due_at here erased the lease
        -- almost immediately for exactly the rows it protects (anthropic:
        -- 300 - 60 = 240s against a 1500s default lease). While a claim token
        -- is held, the claimant owns the schedule; warm_ping_settle assigns the
        -- real next_due_at and clears the token.
        next_due_at = case
            when prefix.claim_token is null then excluded.next_due_at
            else prefix.next_due_at
        end,
        expires_at = v_now + interval '7 days';

    -- The free sensor. A real arrival against a prefix we have already seen is
    -- a censored TTL observation at zero cost: p_cache_read is the provider's
    -- own verdict on whether the entry survived the gap. Guarded rather than
    -- validated-and-raised, because this runs inside a live request: a clock
    -- jump or a prefix idle past the 30-day bound skips the observation instead
    -- of failing the observation call that the response path depends on.
    if v_prior_touch is not null then
        v_gap_seconds := extract(epoch from (v_now - v_prior_touch));
        if v_gap_seconds between 0 and 2592000 then
            perform public.warm_ttl_observe(
                p_provider,
                left(coalesce(p_model_class, ''), 128),
                public.warm_ttl_tier(p_provider_ttl_seconds),
                v_gap_seconds,
                case when p_cache_read then 'warm' else 'expired' end,
                'arrival'
            );
        end if;
    end if;

    -- PLANE B STATE (202608100003), written UNCONDITIONALLY -- there is no flag
    -- on this block and that is deliberate. It changes no admission decision:
    -- nothing reads warm_customer_state unless p_hazard_v2 is passed to the
    -- claim, so these writes are pure instrumentation in exactly the sense
    -- 202608090001's decision-log writes are. The flag day is not the day the
    -- model starts learning; it is the day the model starts being read, and a
    -- posterior with a 14-day half-life needs the history to already exist.
    --
    -- The suppression check fences BOTH touches on the REAL customer: an erased
    -- subject must not contribute to the organization aggregate either, or the
    -- aggregate becomes the place their behaviour survives erasure. The
    -- aggregate's existing mass from an erased subject is not retroactively
    -- removed -- it is not attributable to anyone and fades on the 14-day
    -- half-life -- which is the same accounting the plan's two-plane rule
    -- applies to any organization-level aggregate.
    if not exists (
        select 1 from public.warm_modeling_suppression suppression
         where suppression.organization_id = p_organization_id
           and suppression.customer_id = p_customer_id
    ) then
        perform public.warm_customer_state_touch(
            p_organization_id, p_customer_id, p_provider, v_now);
        perform public.warm_customer_state_touch(
            p_organization_id,
            '00000000-0000-0000-0000-000000000000'::uuid, p_provider, v_now);
    end if;

    -- THE STRUCTURAL KEY (202608100006). prefix_hash is a sha256 of the whole
    -- payload, so two arms sharing 40k tokens of system prompt hash to two
    -- unrelated strings and containment is invisible. The chain is a parallel
    -- artifact: a shared leading prefix produces a shared leading DIGEST
    -- SEQUENCE, which is what makes the tree below a tree at all.
    --
    -- Node digests arrive ALREADY SALTED. The unsalted chain is computed in the
    -- customer-installed package, which must never hold the key; the hosted
    -- observation boundary HMACs each value once, per organization. Nothing
    -- here can be inverted to content, and nothing here is comparable across
    -- organizations.
    --
    -- Every rejection is silent and TOTAL. A malformed chain, a second path
    -- collision, or any error at all leaves no node, no edge and no stamp --
    -- never a partial ancestry that a later read would treat as real -- and the
    -- observation itself still succeeds, because this runs inside a live
    -- request and structure is worth exactly zero receipts.
    if p_chain is not null and jsonb_typeof(p_chain) = 'array' then
      begin
        v_chain_count := jsonb_array_length(p_chain);
        v_chain_blocks := v_chain_count - 1;
        v_chain_ok := v_chain_count between 1 and 301
            and coalesce(p_chain_salt_version, 0) between 1 and 9999
            and coalesce(p_chain_path, '') <> '';
        if v_chain_ok then
            v_chain_labels := string_to_array(p_chain_path, '.');
            -- Two header labels (scheme, salt version), the root, then one
            -- label per MATERIALIZED block: past 253 blocks a node is
            -- adjacency-only and carries no path at all.
            if coalesce(array_length(v_chain_labels, 1), 0)
               <> 3 + least(v_chain_blocks, 253) then
                v_chain_ok := false;
            end if;
            if v_chain_blocks > 253 then
                v_chain_truncated := true;
            end if;
        end if;
        if v_chain_ok then
            for v_chain_depth in 0 .. v_chain_count - 1 loop
                v_chain_node := p_chain -> v_chain_depth;
                v_chain_digest := coalesce(v_chain_node ->> 'digest', '');
                v_chain_label := coalesce(v_chain_node ->> 'label', '');
                v_chain_parent := coalesce(v_chain_node ->> 'parent', '');
                if coalesce((v_chain_node ->> 'depth')::integer, -1) <> v_chain_depth
                   or v_chain_digest !~ '^[0-9a-f]{64}$'
                   or v_chain_label !~ '^[0-9a-f]{12}(_[0-9a-f]{4})?$'
                   or (v_chain_depth = 0 and v_chain_parent <> '')
                   or (v_chain_depth > 0 and v_chain_parent
                       <> coalesce((p_chain -> (v_chain_depth - 1)) ->> 'digest', '')) then
                    v_chain_ok := false;
                    exit;
                end if;
                if coalesce((v_chain_node ->> 'path_truncated')::boolean, false) then
                    v_chain_truncated := true;
                end if;
            end loop;
        end if;
        -- Distinct digests, so the single insert below can never be asked to
        -- affect one row twice. The parent-linkage check above admits a chain
        -- whose node is its own parent; a repeated digest is malformed, and
        -- malformed means the whole chain is dropped.
        if v_chain_ok and v_chain_count <> (
                select count(distinct entry.node ->> 'digest')
                  from jsonb_array_elements(p_chain) as entry(node)) then
            v_chain_ok := false;
        end if;
        -- LABEL RESOLUTION, a pure read pass. Every depth's final label and
        -- path is settled here and the whole tree is then written by ONE
        -- statement.
        --
        -- Why not insert-and-catch per block, which is the obvious shape: a
        -- PL/pgSQL block with an EXCEPTION clause always opens a SUBTRANSACTION
        -- and every iteration writes, so it always takes an XID. A 300-block
        -- chain would assign 300+ subxids inside one transaction, overflowing
        -- PGPROC's 64-slot subxid cache and forcing pg_subtrans lookups for
        -- every concurrent backend on the instance -- on the live request path.
        if v_chain_ok then
            v_chain_node_labels := array_fill(null::text, array[v_chain_count]);
            v_chain_paths := array_fill(null::text, array[v_chain_count]);
            for v_chain_depth in 0 .. v_chain_count - 1 loop
                v_chain_node := p_chain -> v_chain_depth;
                v_chain_digest := v_chain_node ->> 'digest';
                -- A node this organization already holds keeps the label it
                -- was STORED with. Recomputing it would rebuild a descendant's
                -- path under an ancestor label that no row actually carries,
                -- and a block appended at the next depth in a later
                -- observation would land in the colliding sibling's subtree.
                select node.label into v_chain_stored
                  from public.warm_prefix_node node
                 where node.organization_id = p_organization_id
                   and node.provider = p_provider
                   and node.salt_version = p_chain_salt_version
                   and node.node_digest = v_chain_digest;
                v_chain_label := coalesce(v_chain_stored,
                                          v_chain_node ->> 'label');
                v_chain_upper := 3 + v_chain_depth;
                -- The collision escape. Two distinct chains can land on the
                -- same 48-bit label at the same depth; the second one extends
                -- its own label with four more hex digits and rebuilds its path
                -- from it, so the subtrees separate instead of one silently
                -- becoming the other. A SECOND collision is not escaped -- at
                -- that point the structure is not trustworthy and the whole
                -- chain is skipped.
                for v_chain_attempt in 0 .. 1 loop
                    if v_chain_attempt = 1 then
                        v_chain_label := substr(v_chain_digest, 1, 12) || '_'
                                         || substr(v_chain_digest, 13, 4);
                    end if;
                    v_chain_labels[v_chain_upper] := case
                        when v_chain_depth = 0 then 'r' || v_chain_label
                        else v_chain_label end;
                    v_chain_paths[v_chain_depth + 1] := case
                        when v_chain_depth <= 253
                        then array_to_string(v_chain_labels[1:v_chain_upper], '.')
                        else null end;
                    -- A stored label is by definition already the one that row
                    -- holds, so it cannot collide with itself.
                    exit when v_chain_stored is not null
                           or v_chain_paths[v_chain_depth + 1] is null
                           or not exists (
                        -- TEXT comparison, never the ltree `=` operator: this
                        -- function pins search_path to (pg_catalog, public)
                        -- and every ltree operator lives in `extensions`, so
                        -- the operator form does not resolve here. The cast
                        -- does, because a cast is looked up by type name.
                        select 1 from public.warm_prefix_node node
                         where node.organization_id = p_organization_id
                           and node.provider = p_provider
                           and node.salt_version = p_chain_salt_version
                           and node.path is not null
                           and node.path::text
                               = v_chain_paths[v_chain_depth + 1]
                           and node.node_digest <> v_chain_digest);
                    if v_chain_attempt = 1 then
                        v_chain_ok := false;
                    end if;
                end loop;
                v_chain_node_labels[v_chain_depth + 1] := v_chain_label;
                exit when not v_chain_ok;
            end loop;
        end if;
        if v_chain_ok then
            -- ONE statement for the whole tree. A racer that took one of these
            -- paths between the read pass and here raises a unique_violation
            -- that the block-level handler below turns into "no structure",
            -- which is the contract: structure is best effort, always.
            insert into public.warm_prefix_node (
                organization_id, provider, salt_version, node_digest,
                parent_digest, depth, label, block_tokens, token_cum,
                block_elements, path, path_truncated, created_at, last_touch_at
            )
            select p_organization_id, p_provider, p_chain_salt_version,
                   entry.node ->> 'digest',
                   nullif(coalesce(entry.node ->> 'parent', ''), ''),
                   (entry.ordinality - 1)::integer,
                   v_chain_node_labels[entry.ordinality],
                   greatest(coalesce((entry.node ->> 'block_tokens')::integer, 0), 0),
                   greatest(coalesce((entry.node ->> 'token_cum')::integer, 0), 0),
                   greatest(coalesce((entry.node ->> 'block_elements')::integer, 0), 0),
                   v_chain_paths[entry.ordinality]::extensions.ltree,
                   v_chain_truncated, v_now, v_now
              from jsonb_array_elements(p_chain)
                   with ordinality as entry(node, ordinality)
            on conflict (organization_id, provider, salt_version, node_digest)
            do update set last_touch_at = v_now;
            insert into public.warm_prefix_edge (
                organization_id, provider, salt_version, parent_digest, child_digest
            )
            select p_organization_id, p_provider, p_chain_salt_version,
                   (p_chain -> (edge_depth - 1)) ->> 'digest',
                   (p_chain -> edge_depth) ->> 'digest'
              from generate_series(1, v_chain_count - 1) as edge_depth
            on conflict do nothing;
            update public.warm_prefixes prefix
               set chain_leaf_digest = (p_chain -> (v_chain_count - 1)) ->> 'digest',
                   chain_path = p_chain_path,
                   chain_salt_version = p_chain_salt_version
             where prefix.organization_id = p_organization_id
               and prefix.customer_id = p_customer_id
               and prefix.provider = p_provider
               and prefix.prefix_hash = p_prefix_hash;
        end if;
      exception when others then
        -- Structure is best effort by contract. The arrival, the TTL sensor and
        -- the Plane B state have all already been recorded and must stand.
        null;
      end;
    end if;

    return jsonb_build_object(
        'schema', 'brevitas.warm-observe.v1', 'status', 'observed',
        'cache_read', p_cache_read
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;
revoke all on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric,
    text, jsonb, text, integer
) from public, anon, authenticated;
grant execute on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric,
    text, jsonb, text, integer
) to service_role;

comment on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric,
    text, jsonb, text, integer
) is 'Proxy-side warm prefix observation; the warming schedule stays fenced on the claim lease, and each arrival also records a free TTL observation, stamps the provable-touch clock and -- when the caller supplies a salted prefix chain -- upserts that chain into public.warm_prefix_node/public.warm_prefix_edge and stamps the arm''s leaf. The chain writes are best effort by contract: anything malformed, colliding or unexpected skips the tree entirely and still records the arrival.';

-- ---------------------------------------------------------------------------
-- purge_warm_state (202608100005:1655-1741) + the tree horizon. The tree is raw
-- warm state and ages on the same sweeper, keyed on the touch clock: a node
-- still being observed is still in use, however old it is.
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
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;
-- ---------------------------------------------------------------------------
-- compliance_delete_tenant (202608100005:1747-1914) + the tree. An organization
-- leaving takes its own structure with it; the salt is per organization, so
-- these rows describe nobody else.
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
-- compliance_delete_subject (202608100004:1516-1665), restated VERBATIM.
--
-- The classification this migration is required to record: public
-- .warm_prefix_node and public.warm_prefix_edge carry NO customer key at all.
-- A node is shared, content-derived structure -- the same block reached by two
-- customers of the same organization is ONE row -- so there is nothing in
-- either table that belongs to one data subject and could be deleted for them
-- without deleting another subject's structure at the same time. What the
-- subject does own is their warm_prefixes arms, which the inner body already
-- deletes; the chain_leaf_digest, chain_path and chain_salt_version stamps go
-- with those rows, so nothing links the subject to the tree once erasure runs.
-- The nodes themselves then age out on the 90-day horizon in
-- public.purge_warm_state like the rest of the raw warm state.
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
-- compliance_export_tenant (202608100005:1920-2256) + the tree COUNTS.
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
-- compliance_export_subject (202608100004:1966-2213), restated VERBATIM and
-- unchanged: the tree has no subject key to project, so a subject export has
-- nothing new to say about it.
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
    end if;
end;
$function$;
commit;
