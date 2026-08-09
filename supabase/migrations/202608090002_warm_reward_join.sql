-- Phase 0 warming instrumentation, part 2: the per-ping reward join.
--
-- THE GAP. A warm ping writes a usage_log row keyed
-- `warm:{prefix_hash[:16]}:{cycle_ts}` (api/worker.py:771) and the customer
-- arrival it was bought for writes an ordinary receipt. Nothing connects them:
-- the arrival row carries no prefix identity, so "what did this ping actually
-- earn" is unanswerable from the database. public.warm_decision_log has carried
-- a realized_net_usd slot since 202608090001 with no producer. This migration
-- supplies the join key and the producer.
--
-- 1. usage_log.warm_prefix_hash -- nullable, the sha256 that
--    brevitas/warming.py:extract_warm_prefix already computes for every
--    observable request. It is NOT written by the receipt INSERT. That insert
--    is a money path: api/store.py:_insert_usage_rows documents that naming a
--    column PostgREST does not know is a 400 that drops the WHOLE receipt, not
--    one field, which for an authoritative row is lost revenue. The hash is
--    stamped afterwards by public.warm_usage_stamp_prefix, an analytics-only
--    UPDATE that fails closed to null and can never cost a receipt.
--
-- 2. warm_decision_log.organic_counterfactual -- the honesty column. If the
--    customer's own two most recent real arrivals on that prefix were closer
--    together than the provider TTL, the entry was self-refreshing and the ping
--    bought nothing that traffic was not already buying. Those rows are credited
--    NOTHING and charged the full ping cost, which is the conservative direction:
--    an attribution error must understate what warming earns, never overstate it.
--
-- 3. public.warm_reward_join -- the producer. Analytics only. It reads
--    usage_log and writes two columns of warm_decision_log; it touches no
--    ledger, no settlement, no fee, and no verified_savings_usd. Warm spend
--    remains never-billable and this changes nothing about that.
--
-- THE ATTRIBUTION HORIZON. A decision is only scored once its TTL window has
-- fully closed (`ts + provider_ttl_seconds < now()`). A ping whose window is
-- still open has an arrival that has not happened yet, and crediting it early
-- would bake a censored observation into the reward as if it were final. This
-- is the "evaluation windows lagged past the attribution horizon" rule from the
-- plan, enforced in SQL rather than trusted to the caller.
--
-- WHICH PINGS ARE SCORED. Only decision='pinged' AND settle_outcome='warmed'.
-- 'warmed' is the only outcome that writes a priced usage row, so it is the
-- only one whose cost is knowable. 'spent_unknown' booked the reservation on
-- purpose (202608080001) precisely because no receipt exists; guessing a cost
-- for it would put a fabricated number in the reward. Those rows keep a null
-- realized_net_usd, which reads as "not scored", not as "earned zero".
--
-- WHICH USAGE ROW IS THE PING. The earliest cache_warm row carrying this
-- prefix hash at or after the decision, bounded by the NEXT logged ping on the
-- same prefix (and by ten minutes, whichever is sooner). Without the next-ping
-- bound, a decision whose own usage row failed to write would silently adopt
-- the following cycle's ping -- the schedule pushes next_due_at only
-- ttl-minus-margin ahead, which on a 5-minute tier is inside any fixed window
-- wide enough to be safe. A decision that finds no ping row is left unscored.
--
-- COMPLIANCE, IN THIS MIGRATION (the 202607280016 lesson, applied to COLUMNS
-- rather than tables -- the enumeration failure mode is identical):
--   * usage_log.warm_prefix_hash is a per-customer pseudonymous identifier
--     derived from that customer's prompt prefix. Every path that minimizes a
--     RETAINED usage row now clears it: compliance_delete_tenant,
--     compliance_delete_subject and compliance_run_retention's minimization
--     class. Left alone it would have survived erasure on exactly the rows the
--     financial-preservation invariant forces us to keep.
--   * warm_decision_log.organic_counterfactual joins realized_net_usd in both
--     export projections. A jsonb_build_object projection is enumerated, so a
--     new column is silently absent from every data-subject export until it is
--     named -- the same failure mode as an unenumerated table.
--   * compliance_delete_subject additionally gains the warm_decision_log delete
--     that 202608090001 gave compliance_delete_tenant but not the subject path.
--     warm_decision_log carries no foreign key by design, so customer deletion
--     cascaded nothing and a deleted customer's scored prefixes survived.
--
-- RETENTION AND CONVERGENCE. warm_prefix_hash joins the minimization SET and
-- therefore also joins the "already minimized" predicate in BOTH the dry-run
-- count and the apply-time delete. compliance_run_retention's contract is that
-- the evidence row's candidate count matches what the apply would really touch;
-- changing one predicate and not the other would break exactly that.
--
-- NO CONTENT, NO NEW TABLE. Two columns, two functions, five compliance
-- routines restated. Hashes, dollars and booleans only.
--
-- Forward-only and idempotent.

-- REVERSE: DDL: drop public.warm_reward_join and public.warm_usage_stamp_prefix, drop index public.usage_log_warm_prefix_idx, drop constraint usage_log_warm_prefix_hash_hex on public.usage_log, drop usage_log.warm_prefix_hash, drop warm_decision_log.organic_counterfactual, and re-apply 202608090001_warm_instrumentation_tables.sql's compliance_delete_tenant, compliance_export_tenant, compliance_export_subject and compliance_run_retention plus 202607280036_settlement_evidence_erasure_fence.sql's compliance_delete_subject verbatim

begin;

-- usage_log is the busiest table in the schema; a stuck apply must fail rather
-- than hold its writes off.
set local lock_timeout = '15s';

do $migration_precondition$
declare
    required_routine text;
begin
    foreach required_routine in array array[
        'public.compliance_delete_tenant(uuid,uuid,text)',
        'public.compliance_delete_subject(uuid,uuid,text)',
        'public.compliance_export_tenant(uuid,uuid,text)',
        'public.compliance_export_subject(uuid,uuid,text)',
        'public.compliance_run_retention(uuid,text,integer,boolean)',
        'public.compliance_preservation_hold(uuid)',
        'public.compliance_delete_tenant_pre_company_identity(uuid,uuid,text)',
        'public.compliance_delete_subject_pre_company_identity(uuid,uuid,text)',
        'public.warm_decision_settle_outcome(uuid,text)'
    ] loop
        if to_regprocedure(required_routine) is null then
            raise exception using
                errcode = '55000',
                message = '202608090002 requires ' || required_routine;
        end if;
    end loop;
    if to_regclass('public.usage_log') is null
       or to_regclass('public.warm_decision_log') is null
       or to_regclass('public.warm_prefixes') is null then
        raise exception using
            errcode = '55000',
            message = '202608090002 requires public.usage_log, public.warm_decision_log and public.warm_prefixes';
    end if;
end;
$migration_precondition$;

-- ---------------------------------------------------------------------------
-- Columns
-- ---------------------------------------------------------------------------

-- The join key. Null on every row that predates this migration, on every row
-- whose request was not warming-observable, and on any row whose stamp lost the
-- race with its own receipt insert -- all of which read identically as "this
-- row is not attributable to a warm prefix", which is the safe reading.
alter table public.usage_log
    add column if not exists warm_prefix_hash text;

do $usage_hash_constraint$
begin
    if not exists (
        select 1 from pg_catalog.pg_constraint
         where conrelid = 'public.usage_log'::regclass
           and conname = 'usage_log_warm_prefix_hash_hex'
    ) then
        -- NOT VALID first, then VALIDATE: adding a validated CHECK holds ACCESS
        -- EXCLUSIVE for the whole scan, and this table serves the receipt write
        -- path. VALIDATE takes only SHARE UPDATE EXCLUSIVE.
        alter table public.usage_log
            add constraint usage_log_warm_prefix_hash_hex
            check (warm_prefix_hash is null
                   or warm_prefix_hash ~ '^[0-9a-f]{64}$') not valid;
        alter table public.usage_log
            validate constraint usage_log_warm_prefix_hash_hex;
    end if;
end;
$usage_hash_constraint$;

-- The reward join's only access path into usage_log. Partial: the overwhelming
-- majority of rows carry no hash and must not enter this index.
create index if not exists usage_log_warm_prefix_idx
    on public.usage_log (organization_id, customer_id, provider,
                         warm_prefix_hash, ts)
    where warm_prefix_hash is not null;

-- True when the customer's own arrivals were already keeping this prefix warm.
-- NOT NULL with a default rather than nullable: the flag is only ever written
-- together with realized_net_usd, and realized_net_usd's own nullness is
-- already the "not scored yet" signal. A second nullable flag would create a
-- state (scored, unknown organicity) that the join can never produce.
alter table public.warm_decision_log
    add column if not exists organic_counterfactual boolean not null default false;

comment on column public.usage_log.warm_prefix_hash is
    'sha256 of the cacheable prefix this request carried, stamped after the receipt insert by public.warm_usage_stamp_prefix. Analytics join key for cache warming; cleared by every erasure and minimization path.';
comment on column public.warm_decision_log.organic_counterfactual is
    'True when the customer''s two most recent real arrivals on this prefix were closer together than the provider TTL, so the entry was self-refreshing and the ping is credited nothing.';

-- ---------------------------------------------------------------------------
-- warm_usage_stamp_prefix -- write the join key onto an already-written receipt
-- ---------------------------------------------------------------------------

-- Strict about its arguments, like public.warm_ttl_observe: both call sites
-- (api/server.py:_hosted_warm_observe and api/worker.py:_warm_one) are inside
-- best-effort guards that validate first, so raising here can never reach a
-- request or a settle.
--
-- Scoped by (key_hash, request_id): that is the leading pair of
-- usage_log_request_authority_unique (202607280026), so the update is an index
-- scan rather than a sequential scan of the receipt table. organization_id is
-- checked as well, so a caller cannot stamp another tenant's row even if it
-- guesses a request id.
--
-- `warm_prefix_hash is null` in the predicate makes the stamp write-once: a
-- retry cannot rewrite a hash, and neither can a second observation of a
-- request that somehow produced two prefixes.
create or replace function public.warm_usage_stamp_prefix(
    p_organization_id uuid,
    p_key_hash text,
    p_request_id text,
    p_prefix_hash text
) returns jsonb as $$
declare
    v_updated integer := 0;
begin
    if p_organization_id is null
       or coalesce(p_key_hash, '') = ''
       or coalesce(p_request_id, '') = ''
       or coalesce(p_prefix_hash, '') !~ '^[0-9a-f]{64}$' then
        raise exception 'warm usage stamp arguments are invalid';
    end if;
    update public.usage_log usage
       set warm_prefix_hash = p_prefix_hash
     where usage.key_hash = p_key_hash
       and usage.request_id = p_request_id
       and usage.organization_id = p_organization_id
       and usage.warm_prefix_hash is null;
    get diagnostics v_updated = row_count;
    return jsonb_build_object(
        'schema', 'brevitas.warm-usage-stamp.v1', 'status', 'recorded',
        'updated', v_updated
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- warm_reward_join -- credit each closed ping with what it actually earned
-- ---------------------------------------------------------------------------

-- Row-at-a-time on purpose. Each decision needs four correlated lookups whose
-- bounds depend on each other (the ping row bounds the arrival search, which
-- bounds nothing else), and the volume is one row per ping per cycle, not per
-- request. A set-based rewrite belongs in the arm that raises ping volume, with
-- the index measurements to justify it.
create or replace function public.warm_reward_join(
    p_lookback_hours integer default 48,
    p_limit integer default 5000
) returns jsonb as $$
declare
    v_lookback integer := least(greatest(coalesce(p_lookback_hours, 48), 1), 720);
    v_limit integer := least(greatest(coalesce(p_limit, 5000), 1), 50000);
    v_scanned integer := 0;
    v_joined integer := 0;
    v_attributed integer := 0;
    v_organic integer := 0;
    v_unpriced integer := 0;
    v_entry record;
    v_ping record;
    v_arrival record;
    v_next_ts timestamptz;
    v_window_end timestamptz;
    v_prev_ts timestamptz;
    v_prev_prior_ts timestamptz;
    v_benefit numeric := 0;
    v_organic_flag boolean := false;
    v_net numeric := 0;
begin
    for v_entry in
        select entry.id,
               entry.organization_id,
               entry.customer_id,
               entry.provider,
               entry.prefix_hash,
               entry.ts,
               -- The prefix row is the TTL of record. A prefix already purged
               -- by public.purge_warm_state leaves the decision behind, and 300
               -- is the provider floor every spec in api/worker.py shares.
               coalesce(prefix.provider_ttl_seconds, 300) as ttl_seconds
          from public.warm_decision_log entry
          left join public.warm_prefixes prefix
            on prefix.organization_id = entry.organization_id
           and prefix.customer_id = entry.customer_id
           and prefix.provider = entry.provider
           and prefix.prefix_hash = entry.prefix_hash
         where entry.decision = 'pinged'
           and entry.settle_outcome = 'warmed'
           and entry.realized_net_usd is null
           and entry.ts >= pg_catalog.now() - make_interval(hours => v_lookback)
           -- The attribution horizon: score nothing whose window is still open.
           and entry.ts + make_interval(
                   secs => coalesce(prefix.provider_ttl_seconds, 300))
               < pg_catalog.now()
         order by entry.ts, entry.id
         limit v_limit
    loop
        v_scanned := v_scanned + 1;

        select min(nxt.ts) into v_next_ts
          from public.warm_decision_log nxt
         where nxt.organization_id = v_entry.organization_id
           and nxt.customer_id = v_entry.customer_id
           and nxt.provider = v_entry.provider
           and nxt.prefix_hash = v_entry.prefix_hash
           and nxt.decision = 'pinged'
           and nxt.ts > v_entry.ts;
        v_window_end := least(
            coalesce(v_next_ts, v_entry.ts + interval '10 minutes'),
            v_entry.ts + interval '10 minutes');

        select usage.ts as ping_ts,
               coalesce(usage.actual_cost_usd, 0)::numeric as cost_usd
          into v_ping
          from public.usage_log usage
         where usage.organization_id = v_entry.organization_id
           and usage.customer_id = v_entry.customer_id
           and usage.provider = v_entry.provider
           and usage.warm_prefix_hash = v_entry.prefix_hash
           and usage.strategy = 'cache_warm'
           and usage.ts >= v_entry.ts
           and usage.ts < v_window_end
         order by usage.ts, usage.id
         limit 1;
        if not found then
            -- No priced ping row: either the receipt write failed or the stamp
            -- did. Leave the decision unscored rather than invent a cost; it
            -- ages out of the lookback on its own.
            v_unpriced := v_unpriced + 1;
            continue;
        end if;

        -- The credit: the FIRST real arrival on this prefix inside the TTL the
        -- ping bought. cache_attributable is the same gate billing uses to
        -- decide a discount came from the provider's native cache rather than
        -- from Brevitas's own replay; a discount that fails it is not something
        -- a keep-alive can have caused.
        select usage.cache_attributable as attributable,
               coalesce(usage.native_cache_discount_usd, 0)::numeric as discount_usd
          into v_arrival
          from public.usage_log usage
         where usage.organization_id = v_entry.organization_id
           and usage.customer_id = v_entry.customer_id
           and usage.provider = v_entry.provider
           and usage.warm_prefix_hash = v_entry.prefix_hash
           and usage.strategy <> 'cache_warm'
           and usage.authoritative
           and usage.ts > v_ping.ping_ts
           and usage.ts <= v_ping.ping_ts
                           + make_interval(secs => v_entry.ttl_seconds)
         order by usage.ts, usage.id
         limit 1;
        if found and v_arrival.attributable then
            v_benefit := v_arrival.discount_usd;
        else
            v_benefit := 0;
        end if;

        -- The counterfactual. Two most recent REAL arrivals as of the ping: if
        -- they were closer together than the TTL, this customer was refreshing
        -- the entry themselves and would have kept it alive with or without us.
        select usage.ts into v_prev_ts
          from public.usage_log usage
         where usage.organization_id = v_entry.organization_id
           and usage.customer_id = v_entry.customer_id
           and usage.provider = v_entry.provider
           and usage.warm_prefix_hash = v_entry.prefix_hash
           and usage.strategy <> 'cache_warm'
           and usage.authoritative
           and usage.ts <= v_ping.ping_ts
         order by usage.ts desc, usage.id desc
         limit 1;
        select usage.ts into v_prev_prior_ts
          from public.usage_log usage
         where usage.organization_id = v_entry.organization_id
           and usage.customer_id = v_entry.customer_id
           and usage.provider = v_entry.provider
           and usage.warm_prefix_hash = v_entry.prefix_hash
           and usage.strategy <> 'cache_warm'
           and usage.authoritative
           and usage.ts <= v_ping.ping_ts
         order by usage.ts desc, usage.id desc
         limit 1 offset 1;
        v_organic_flag := v_prev_ts is not null
            and v_prev_prior_ts is not null
            and extract(epoch from (v_prev_ts - v_prev_prior_ts))
                < v_entry.ttl_seconds;

        if v_organic_flag then
            -- Conservative v0: credit nothing, charge the whole ping.
            v_net := -v_ping.cost_usd;
            v_organic := v_organic + 1;
        else
            v_net := v_benefit - v_ping.cost_usd;
            if v_benefit > 0 then
                v_attributed := v_attributed + 1;
            end if;
        end if;

        update public.warm_decision_log entry
           set realized_net_usd = round(v_net, 10),
               organic_counterfactual = v_organic_flag
         where entry.id = v_entry.id
           and entry.realized_net_usd is null;
        if found then
            v_joined := v_joined + 1;
        end if;
    end loop;

    return jsonb_build_object(
        'schema', 'brevitas.warm-reward-join.v1',
        'status', 'ok',
        'scanned', v_scanned,
        'joined', v_joined,
        'attributed', v_attributed,
        'organic', v_organic,
        'unpriced', v_unpriced
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- compliance_delete_tenant (202608090001:993-1107) + the warm prefix hash on
-- retained usage rows.
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
-- compliance_delete_subject (202607280036:674-745) + the customer's warming
-- decision log and the warm prefix hash on retained usage rows.
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
-- compliance_export_tenant (202608090001:1112-1313) + organic_counterfactual.
-- A jsonb_build_object projection is enumerated: a column nobody names is
-- absent from every export, exactly as an unenumerated table would be.
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
    -- class this migration registers in compliance_run_retention.
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
                'organic_counterfactual', entry.organic_counterfactual
            )
        )
          from public.warm_decision_log entry
         where entry.organization_id = p_organization_id
         order by entry.ts, entry.id;
end;
$function$;

-- ---------------------------------------------------------------------------
-- compliance_export_subject (202608090001:1318-1482) + organic_counterfactual.
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
                    'organic_counterfactual', entry.organic_counterfactual
                )
            )
              from public.warm_decision_log entry
             where entry.organization_id = p_organization_id
               and entry.customer_id = v_request.subject_id
             order by entry.ts, entry.id;
    end if;
end;
$function$;

-- ---------------------------------------------------------------------------
-- compliance_run_retention (202608090001:1487-1823) + warm_prefix_hash in the
-- usage minimization class.
-- ---------------------------------------------------------------------------
create or replace function public.compliance_run_retention(
    p_run_id uuid,
    p_actor_id text,
    p_batch_limit integer,
    p_apply boolean
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_usage_cutoff timestamptz := clock_timestamp()-interval '13 months';
    v_support_cutoff timestamptz := clock_timestamp()-interval '24 months';
    -- Prospect contact data has never had a retention rule. 24 months from
    -- submission, matching the support-record period.
    v_waitlist_cutoff timestamptz := clock_timestamp()-interval '24 months';
    v_evidence_cutoff timestamptz := clock_timestamp()-interval '400 days';
    -- Warming decisions are behavioral telemetry, not financial evidence:
    -- nothing recomputes money from them and no settlement reads them, so they
    -- get the shortest horizon that still spans a full seasonal cycle for the
    -- replay simulator. 90 days.
    v_warm_decision_cutoff timestamptz := clock_timestamp()-interval '90 days';
    v_existing public.compliance_retention_runs%rowtype;
    v_usage_candidates integer := 0;
    v_audit_candidates integer := 0;
    v_support_candidates integer := 0;
    v_request_candidates integer := 0;
    v_hold_candidates integer := 0;
    v_prior_run_candidates integer := 0;
    v_usage_minimize_candidates integer := 0;
    v_waitlist_candidates integer := 0;
    v_warm_decision_candidates integer := 0;
    v_warm_decision_deleted integer := 0;
    v_usage_deleted integer := 0;
    v_audit_deleted integer := 0;
    v_support_deleted integer := 0;
    v_requests_deleted integer := 0;
    v_holds_deleted integer := 0;
    v_prior_run_deleted integer := 0;
    v_usage_minimized integer := 0;
    v_waitlist_deleted integer := 0;
    v_hold_ids uuid[] := array[]::uuid[];
begin
    perform public.compliance_actor_role(p_actor_id);
    if p_run_id is null or p_apply is null or p_batch_limit is null
       or p_batch_limit not between 1 and 10000 then
        raise exception 'retention batch limit must be between 1 and 10000' using errcode='22023';
    end if;
    select * into v_existing from public.compliance_retention_runs where id=p_run_id;
    if found then
        if not p_apply or v_existing.actor_id<>p_actor_id
           or v_existing.batch_limit<>p_batch_limit then
            raise exception 'retention run idempotency conflict' using errcode='23505';
        end if;
        return jsonb_build_object(
            'schema','brevitas.compliance-retention-result.v1','mode','apply',
            'run_id',v_existing.id,'batch_limit',v_existing.batch_limit,
            'usage_candidates',v_existing.usage_candidates,
            'audit_candidates',v_existing.audit_candidates,
            'support_candidates',v_existing.support_candidates,
            'requests_candidates',v_existing.requests_candidates,
            'holds_candidates',v_existing.holds_candidates,
            'prior_run_evidence_candidates',v_existing.prior_run_evidence_candidates,
            'usage_deleted',v_existing.usage_deleted,
            'audit_deleted',v_existing.audit_deleted,
            'support_deleted',v_existing.support_deleted,
            'requests_deleted',v_existing.requests_deleted,
            'holds_deleted',v_existing.holds_deleted,
            'prior_run_evidence_deleted',v_existing.prior_run_evidence_deleted,
            'usage_minimize_candidates',v_existing.usage_minimize_candidates,
            'usage_minimized',v_existing.usage_minimized,
            'waitlist_candidates',v_existing.waitlist_candidates,
            'waitlist_deleted',v_existing.waitlist_deleted,
            'warm_decision_candidates',v_existing.warm_decision_candidates,
            'warm_decision_deleted',v_existing.warm_decision_deleted,
            'idempotent_replay',true,'evidence_contains_customer_content',false
        );
    end if;

    select count(*)::integer into v_usage_candidates from (
        select 1 from public.usage_log usage
         where usage.ts<v_usage_cutoff
           and not exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=usage.id)
           and not exists (select 1 from public.period_settlement_ledger settlement
                            where settlement.organization_id=usage.organization_id
                              and settlement.status<>'void'
                              and settlement.usage_log_watermark_id>=usage.id)
           and not public.compliance_preservation_hold(usage.organization_id)
         order by usage.ts,usage.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_audit_candidates from (
        select 1 from public.audit_events event
         where event.occurred_at<v_evidence_cutoff
           and not public.compliance_preservation_hold(event.organization_id)
         order by event.occurred_at,event.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_request_candidates from (
        select 1 from public.data_subject_requests request
         where request.status='completed' and request.completed_at<v_evidence_cutoff
           and not public.compliance_preservation_hold(request.organization_id)
         order by request.completed_at,request.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_hold_candidates from (
        select 1 from public.legal_holds hold
         where not hold.active and hold.released_at<v_evidence_cutoff
           and not public.compliance_preservation_hold(hold.organization_id)
         order by hold.released_at,hold.id limit p_batch_limit
    ) candidate;
    -- Ledger-referenced usage rows cannot be deleted, and nothing ever
    -- minimized them: compliance_run_retention is delete-only. The deletion path
    -- already classifies these exact columns as needing removal
    -- (202607170007:2417-2423), so past the same 13-month cutoff the rows the
    -- ledger forces us to keep get the same treatment.
    select count(*)::integer into v_usage_minimize_candidates from (
        select 1 from public.usage_log candidate
         where candidate.ts<v_usage_cutoff
           and exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=candidate.id)
           and not public.compliance_preservation_hold(candidate.organization_id)
           -- Already-minimized rows must stop being candidates or the batch
           -- would rewrite the same rows forever and never converge.
           and (candidate.owner_id<>'' or candidate.customer_id is not null
                or candidate.usage_raw<>'' or candidate.session_id<>''
                or candidate.pipeline<>'' or candidate.run_id<>''
                or candidate.repo<>'' or candidate.client<>''
                or candidate.agent<>'' or candidate.call_site_id<>''
                or candidate.framework<>'' or candidate.gateway<>''
                or candidate.provider<>'' or candidate.model<>''
                or candidate.project<>'Deleted' or candidate.environment<>'Deleted'
                or candidate.source<>'Deleted'
                -- 202608090002: warm_prefix_hash joins the SET below, so it
                -- must join the convergence predicate in BOTH the dry-run count
                -- and the apply, or the evidence row would count rows the apply
                -- does not touch (or the batch would never converge).
                or candidate.warm_prefix_hash is not null)
         order by candidate.ts,candidate.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_waitlist_candidates from (
        select 1 from public.waitlist candidate
         where candidate.created_at<v_waitlist_cutoff
         order by candidate.created_at,candidate.id limit p_batch_limit
    ) candidate;
    -- Same preservation-hold fence as every other tenant class: an organization
    -- under legal hold keeps its decision log until the hold lifts.
    select count(*)::integer into v_warm_decision_candidates from (
        select 1 from public.warm_decision_log candidate
         where candidate.ts<v_warm_decision_cutoff
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.ts,candidate.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_prior_run_candidates from (
        select 1 from public.compliance_retention_runs run
         where run.completed_at<v_evidence_cutoff
           and not public.compliance_global_preservation_hold()
         order by run.completed_at,run.id limit p_batch_limit
    ) candidate;

    if to_regclass('public.support_records') is not null then
        if not exists (select 1 from information_schema.columns
                        where table_schema='public' and table_name='support_records'
                          and column_name='organization_id')
           or not exists (select 1 from information_schema.columns
                           where table_schema='public' and table_name='support_records'
                             and column_name='created_at') then
            raise exception 'support_records retention contract is unsupported' using errcode='55000';
        end if;
        execute 'select count(*)::integer from (select 1 from public.support_records support where support.created_at<$1 and not public.compliance_preservation_hold(support.organization_id) order by support.created_at,support.ctid limit $2) candidate'
          into v_support_candidates using v_support_cutoff,p_batch_limit;
    end if;

    if not p_apply then
        return jsonb_build_object(
            'schema','brevitas.compliance-retention-result.v1','mode','dry_run',
            'run_id',p_run_id,'batch_limit',p_batch_limit,
            'usage_candidates',v_usage_candidates,
            'audit_candidates',v_audit_candidates,
            'support_candidates',v_support_candidates,
            'requests_candidates',v_request_candidates,
            'holds_candidates',v_hold_candidates,
            'prior_run_evidence_candidates',v_prior_run_candidates,
            'usage_minimize_candidates',v_usage_minimize_candidates,
            'waitlist_candidates',v_waitlist_candidates,
            'warm_decision_candidates',v_warm_decision_candidates,
            'usage_deleted',0,'audit_deleted',0,'support_deleted',0,
            'requests_deleted',0,'holds_deleted',0,'prior_run_evidence_deleted',0,
            'usage_minimized',0,'waitlist_deleted',0,'warm_decision_deleted',0,
            'idempotent_replay',false,'evidence_contains_customer_content',false
        );
    end if;

    delete from public.usage_log usage
     where usage.id in (
        select candidate.id from public.usage_log candidate
         where candidate.ts<v_usage_cutoff
           and not exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=candidate.id)
           and not exists (select 1 from public.period_settlement_ledger settlement
                            where settlement.organization_id=candidate.organization_id
                              and settlement.status<>'void'
                              and settlement.usage_log_watermark_id>=candidate.id)
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.ts,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics v_usage_deleted = row_count;

    -- Minimize what the ledger forces us to retain. The preserved columns are
    -- exactly the ones the financial evidence needs -- id, organization_id, ts,
    -- authoritative, pricing_status and the token/price/savings columns, verified
    -- against billing_period_settlement_evidence (202607280008:325-363) -- plus
    -- key_hash and request_id, which are NOT cleared here even though the
    -- deletion path clears them: usage_log carries a unique index on
    -- (key_hash, request_id) where request_id<>'', so rewriting key_hash while
    -- leaving request_id in place could collide two retained rows and abort the
    -- retention run, and the seven-year financial evidence is correlated by
    -- request_id in the DR assertions. Clearing both (as tenant erasure does) is
    -- correct only when the whole tenant is going away.
    update public.usage_log usage
       set customer_id = null,
           owner_id = '', project = 'Deleted', environment = 'Deleted',
           source = 'Deleted', repo = '', client = '', agent = '',
           call_site_id = '', framework = '', gateway = '', provider = '',
           model = '', session_id = '', pipeline = '', run_id = '',
           usage_raw = '',
           -- A sha256 of the customer's prompt prefix is a pseudonymous
           -- identifier, not content-free financial evidence.
           warm_prefix_hash = null
     where usage.id in (
        select candidate.id from public.usage_log candidate
         where candidate.ts<v_usage_cutoff
           and exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=candidate.id)
           and not public.compliance_preservation_hold(candidate.organization_id)
           -- Already-minimized rows must stop being candidates or the batch
           -- would rewrite the same rows forever and never converge.
           and (candidate.owner_id<>'' or candidate.customer_id is not null
                or candidate.usage_raw<>'' or candidate.session_id<>''
                or candidate.pipeline<>'' or candidate.run_id<>''
                or candidate.repo<>'' or candidate.client<>''
                or candidate.agent<>'' or candidate.call_site_id<>''
                or candidate.framework<>'' or candidate.gateway<>''
                or candidate.provider<>'' or candidate.model<>''
                or candidate.project<>'Deleted' or candidate.environment<>'Deleted'
                or candidate.source<>'Deleted'
                -- 202608090002: warm_prefix_hash joins the SET below, so it
                -- must join the convergence predicate in BOTH the dry-run count
                -- and the apply, or the evidence row would count rows the apply
                -- does not touch (or the batch would never converge).
                or candidate.warm_prefix_hash is not null)
         order by candidate.ts,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics v_usage_minimized = row_count;

    -- Waitlist prospects never created an account, so no data_subject_requests
    -- scope can reach them: every implemented scope requires an organization_id
    -- or a subject inside one. This is the only retention path they have.
    delete from public.waitlist entry
     where entry.id in (
        select candidate.id from public.waitlist candidate
         where candidate.created_at<v_waitlist_cutoff
         order by candidate.created_at,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics v_waitlist_deleted = row_count;

    delete from public.warm_decision_log entry
     where entry.id in (
        select candidate.id from public.warm_decision_log candidate
         where candidate.ts<v_warm_decision_cutoff
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.ts,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics v_warm_decision_deleted = row_count;

    if to_regclass('public.support_records') is not null then
        execute 'delete from public.support_records support where support.ctid in (select candidate.ctid from public.support_records candidate where candidate.created_at<$1 and not public.compliance_preservation_hold(candidate.organization_id) order by candidate.created_at,candidate.ctid for update skip locked limit $2)'
          using v_support_cutoff,p_batch_limit;
        get diagnostics v_support_deleted = row_count;
    end if;

    select deleted.audit_deleted,deleted.requests_deleted,deleted.prior_run_evidence_deleted
      into v_audit_deleted,v_requests_deleted,v_prior_run_deleted
      from public.compliance_retention_delete_immutable(v_evidence_cutoff,p_batch_limit) deleted;
    select coalesce(array_agg(candidate.id order by candidate.released_at,candidate.id),
                    array[]::uuid[])
      into v_hold_ids
      from (
        select candidate.id,candidate.released_at from public.legal_holds candidate
         where not candidate.active and candidate.released_at<v_evidence_cutoff
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.released_at,candidate.id
         for update skip locked
         limit p_batch_limit
     ) candidate;
    begin
        execute 'alter table public.legal_hold_actions disable trigger legal_hold_actions_enforce_transition';
        delete from public.legal_hold_actions hold_action
         where hold_action.target_hold_id=any(v_hold_ids);
        execute 'alter table public.legal_hold_actions enable trigger legal_hold_actions_enforce_transition';
    exception when others then
        execute 'alter table public.legal_hold_actions enable trigger legal_hold_actions_enforce_transition';
        raise;
    end;
    delete from public.legal_holds hold where hold.id=any(v_hold_ids);
    get diagnostics v_holds_deleted = row_count;

    insert into public.compliance_retention_runs(
        id,actor_id,batch_limit,usage_candidates,audit_candidates,support_candidates,
        requests_candidates,holds_candidates,prior_run_evidence_candidates,
        usage_deleted,audit_deleted,support_deleted,
        requests_deleted,holds_deleted,prior_run_evidence_deleted,
        usage_minimize_candidates,waitlist_candidates,
        usage_minimized,waitlist_deleted,
        warm_decision_candidates,warm_decision_deleted
    ) values (
        p_run_id,p_actor_id,p_batch_limit,v_usage_candidates,v_audit_candidates,v_support_candidates,
        v_request_candidates,v_hold_candidates,v_prior_run_candidates,
        v_usage_deleted,v_audit_deleted,v_support_deleted,
        v_requests_deleted,v_holds_deleted,v_prior_run_deleted,
        v_usage_minimize_candidates,v_waitlist_candidates,
        v_usage_minimized,v_waitlist_deleted,
        v_warm_decision_candidates,v_warm_decision_deleted
    );
    perform public.append_company_audit(
        null,p_actor_id,public.compliance_actor_role(p_actor_id),p_run_id::text,
        'compliance.retention.completed','retention_run',p_run_id::text,'committed'
    );
    return jsonb_build_object(
        'schema','brevitas.compliance-retention-result.v1','mode','apply',
        'run_id',p_run_id,'batch_limit',p_batch_limit,
        'usage_candidates',v_usage_candidates,'audit_candidates',v_audit_candidates,
        'support_candidates',v_support_candidates,'requests_candidates',v_request_candidates,
        'holds_candidates',v_hold_candidates,
        'prior_run_evidence_candidates',v_prior_run_candidates,
        'usage_deleted',v_usage_deleted,'audit_deleted',v_audit_deleted,
        'support_deleted',v_support_deleted,'requests_deleted',v_requests_deleted,
        'holds_deleted',v_holds_deleted,'prior_run_evidence_deleted',v_prior_run_deleted,
        'usage_minimize_candidates',v_usage_minimize_candidates,
        'usage_minimized',v_usage_minimized,
        'waitlist_candidates',v_waitlist_candidates,
        'waitlist_deleted',v_waitlist_deleted,
        'warm_decision_candidates',v_warm_decision_candidates,
        'warm_decision_deleted',v_warm_decision_deleted,
        'idempotent_replay',false,'evidence_contains_customer_content',false
    );
end;
$$;

-- ---------------------------------------------------------------------------
-- Privileges. 202607280032's contract: every migration that defines a routine
-- restates that routine's own posture rather than inheriting one.
-- ---------------------------------------------------------------------------
revoke all on function public.warm_usage_stamp_prefix(uuid, text, text, text)
    from public, anon, authenticated;
grant execute on function public.warm_usage_stamp_prefix(uuid, text, text, text)
    to service_role;
revoke all on function public.warm_reward_join(integer, integer)
    from public, anon, authenticated;
grant execute on function public.warm_reward_join(integer, integer) to service_role;
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
revoke all on function public.compliance_run_retention(uuid, text, integer, boolean)
    from public, anon, authenticated;
grant execute on function public.compliance_run_retention(uuid, text, integer, boolean)
    to service_role;

comment on function public.warm_usage_stamp_prefix(uuid, text, text, text) is
    'Stamp the warm prefix hash onto an already-written receipt. Deliberately not part of the receipt INSERT: an unknown column there is a 400 that drops the whole billable row.';
comment on function public.warm_reward_join(integer, integer) is
    'Credit each closed warm ping with the native cache discount of the first real arrival inside the TTL it bought, minus the ping cost; self-refreshing sessions are credited nothing. Analytics only -- writes no ledger, fee or settlement.';
comment on function public.compliance_delete_subject(uuid, uuid, text) is
    'Subject erasure including the customer''s warming decision log and the warm prefix hashes on retained usage rows; the content-free warm budget ledger and the tenant-free TTL observations are untouched.';

commit;
