-- Minimize the usage rows retention cannot delete, and give waitlist PII a
-- retention rule and an erasure path.
--
-- TWO GAPS IN THE SAME FUNCTION. compliance_run_retention is delete-only: it
-- exempts every ledger-referenced usage row (202607170007:723-729) and contains
-- no `update public.usage_log` anywhere. The deletion path, by contrast, DOES
-- minimize the rows it preserves (:2417-2423) -- customer_id, owner_id, project,
-- environment, source, repo, client, agent, call_site_id, framework, gateway,
-- provider, model, session_id, pipeline, run_id, usage_raw. So the exact field
-- set the code itself classifies as needing removal is retained indefinitely for
-- any still-active tenant whose rows the billing ledger references. Past the same
-- 13-month cutoff those rows are now minimized in place, bounded by the same
-- p_batch_limit and skip-locked pattern as the delete, and counted in the
-- immutable compliance_retention_runs evidence row.
--
-- Two columns are deliberately NOT cleared here, unlike in tenant erasure:
-- key_hash and request_id. public.usage_log carries a unique index on
-- (key_hash, request_id) where request_id <> '', so rewriting key_hash to a
-- per-organization placeholder while leaving request_id populated can collide two
-- retained rows and abort the whole retention run; tenant erasure gets away with
-- it because it clears request_id to '' at the same time, which drops both rows
-- out of the partial index. Financial evidence is also correlated by request_id.
--
-- Second, public.waitlist stores up to 4,000 characters of free-text notes plus
-- name, company, role, pipeline shape, spend band and IP address
-- (202607200002_waitlist_security.sql:6-20) and appears in NO retention or
-- compliance path: compliance_run_retention enumerates exactly six classes, none
-- of them waitlist, and all three data_subject_requests scopes (tenant, member,
-- customer) require an organization_id or a subject inside one -- so a prospect
-- who never created an account is unreachable by every implemented scope. This
-- adds a bounded waitlist class on created_at, mirroring support_records, plus a
-- dedicated erasure RPC keyed on the canonicalized email rather than widening the
-- DSR scopes, whose tenant/subject invariants and state machine all assume an
-- organization_id.
--
-- compliance_retention_worker_cycle is redefined too, so the new classes count
-- toward the apply decision and the backlog gauge; otherwise a waitlist-only or
-- minimization-only backlog would never trigger an apply.
--
-- Forward-only and idempotent.

-- REVERSE: PITR-ONLY -- undoing retention minimization would re-widen personal-data retention beyond the documented floor

begin;

do $migration_precondition$
begin
    if to_regprocedure('public.compliance_run_retention(uuid,text,integer,boolean)') is null
       or to_regprocedure(
        'public.compliance_retention_worker_cycle(uuid,uuid,uuid,uuid,text,text,integer)'
    ) is null
       or to_regclass('public.waitlist') is null then
        raise exception using
            errcode = '55000',
            message = '202607280023 requires the retention authority and public.waitlist';
    end if;
end;
$migration_precondition$;

-- compliance_retention_runs is immutable per-cycle evidence (UPDATE/DELETE are
-- rejected by trigger), so the new counters are added as columns with the same
-- batch-bounded CHECK the existing counters use. Existing rows default to 0,
-- which is the truthful value for runs that predate these classes.
alter table public.compliance_retention_runs
    add column if not exists usage_minimize_candidates integer not null default 0,
    add column if not exists waitlist_candidates integer not null default 0,
    add column if not exists usage_minimized integer not null default 0,
    add column if not exists waitlist_deleted integer not null default 0;

do $retention_run_bounds$
declare
    bounded_column text;
begin
    foreach bounded_column in array array[
        'usage_minimize_candidates', 'waitlist_candidates',
        'usage_minimized', 'waitlist_deleted'
    ] loop
        if not exists (
            select 1 from pg_catalog.pg_constraint
             where conrelid = 'public.compliance_retention_runs'::regclass
               and conname = 'compliance_retention_runs_' || bounded_column || '_check'
        ) then
            execute format(
                'alter table public.compliance_retention_runs'
                || ' add constraint %I check (%I between 0 and batch_limit)',
                'compliance_retention_runs_' || bounded_column || '_check',
                bounded_column
            );
        end if;
    end loop;
end;
$retention_run_bounds$;

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
    v_existing public.compliance_retention_runs%rowtype;
    v_usage_candidates integer := 0;
    v_audit_candidates integer := 0;
    v_support_candidates integer := 0;
    v_request_candidates integer := 0;
    v_hold_candidates integer := 0;
    v_prior_run_candidates integer := 0;
    v_usage_minimize_candidates integer := 0;
    v_waitlist_candidates integer := 0;
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
            'idempotent_replay',true,'evidence_contains_customer_content',false
        );
    end if;

    select count(*)::integer into v_usage_candidates from (
        select 1 from public.usage_log usage
         where usage.ts<v_usage_cutoff
           and not exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=usage.id)
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
                or candidate.source<>'Deleted')
         order by candidate.ts,candidate.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_waitlist_candidates from (
        select 1 from public.waitlist candidate
         where candidate.created_at<v_waitlist_cutoff
         order by candidate.created_at,candidate.id limit p_batch_limit
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
            'usage_deleted',0,'audit_deleted',0,'support_deleted',0,
            'requests_deleted',0,'holds_deleted',0,'prior_run_evidence_deleted',0,
            'usage_minimized',0,'waitlist_deleted',0,
            'idempotent_replay',false,'evidence_contains_customer_content',false
        );
    end if;

    delete from public.usage_log usage
     where usage.id in (
        select candidate.id from public.usage_log candidate
         where candidate.ts<v_usage_cutoff
           and not exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=candidate.id)
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
           usage_raw = ''
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
                or candidate.source<>'Deleted')
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
        usage_minimized,waitlist_deleted
    ) values (
        p_run_id,p_actor_id,p_batch_limit,v_usage_candidates,v_audit_candidates,v_support_candidates,
        v_request_candidates,v_hold_candidates,v_prior_run_candidates,
        v_usage_deleted,v_audit_deleted,v_support_deleted,
        v_requests_deleted,v_holds_deleted,v_prior_run_deleted,
        v_usage_minimize_candidates,v_waitlist_candidates,
        v_usage_minimized,v_waitlist_deleted
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
        'idempotent_replay',false,'evidence_contains_customer_content',false
    );
end;
$$;

create or replace function public.compliance_retention_worker_cycle(
    p_cycle_id uuid,
    p_dry_run_id uuid,
    p_apply_run_id uuid,
    p_post_run_id uuid,
    p_worker_owner text,
    p_actor_id text,
    p_batch_limit integer
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_started_at timestamptz := clock_timestamp();
    v_dry jsonb;
    v_apply jsonb;
    v_post jsonb;
    v_initial_candidates integer;
    v_remaining_candidates integer;
    v_backlog boolean;
begin
    perform public.compliance_actor_role(p_actor_id);
    perform public.compliance_assert_usage_export_schema();
    if p_cycle_id is null or p_dry_run_id is null or p_apply_run_id is null
       or p_post_run_id is null or p_batch_limit is null
       or p_batch_limit not between 1 and 10000
       or p_worker_owner is null
       or p_worker_owner !~ '^[A-Za-z0-9._:-]{3,128}$'
       or p_worker_owner ~ '@'
       or p_worker_owner ~* '(^|[._:-])(secret|password|token|api[_-]?key)([._:-]|$)'
       or p_cycle_id=any(array[p_dry_run_id,p_apply_run_id,p_post_run_id])
       or p_dry_run_id=any(array[p_apply_run_id,p_post_run_id])
       or p_apply_run_id=p_post_run_id then
        raise exception 'invalid retention worker cycle' using errcode='22023';
    end if;
    if not pg_try_advisory_xact_lock(
        hashtextextended('brevitas.compliance.retention.worker.v1',0)
    ) then
        return jsonb_build_object(
            'schema','brevitas.compliance-retention-cycle.v1',
            'status','lease_unavailable','cycle_id',p_cycle_id,
            'worker_owner',p_worker_owner,'evidence_contains_customer_content',false
        );
    end if;

    v_dry:=public.compliance_run_retention(
        p_dry_run_id,p_actor_id,p_batch_limit,false);
    v_initial_candidates:=
        (v_dry->>'usage_candidates')::integer+
        (v_dry->>'audit_candidates')::integer+
        (v_dry->>'support_candidates')::integer+
        (v_dry->>'requests_candidates')::integer+
        (v_dry->>'holds_candidates')::integer+
        (v_dry->>'prior_run_evidence_candidates')::integer+
        (v_dry->>'usage_minimize_candidates')::integer+
        (v_dry->>'waitlist_candidates')::integer;
    if v_initial_candidates>0 then
        v_apply:=public.compliance_run_retention(
            p_apply_run_id,p_actor_id,p_batch_limit,true);
        v_post:=public.compliance_run_retention(
            p_post_run_id,p_actor_id,p_batch_limit,false);
    else
        v_post:=v_dry;
    end if;
    v_remaining_candidates:=
        (v_post->>'usage_candidates')::integer+
        (v_post->>'audit_candidates')::integer+
        (v_post->>'support_candidates')::integer+
        (v_post->>'requests_candidates')::integer+
        (v_post->>'holds_candidates')::integer+
        (v_post->>'prior_run_evidence_candidates')::integer+
        (v_post->>'usage_minimize_candidates')::integer+
        (v_post->>'waitlist_candidates')::integer;
    v_backlog:=v_remaining_candidates>0;

    insert into public.compliance_retention_worker_state as state(
        singleton,last_cycle_id,worker_owner,last_started_at,last_success_at,
        last_batch_limit,backlog_remaining,backlog_since,remaining_candidates,
        schema_contract_ok,legal_holds_evaluated,financial_ledger_preserved,
        evidence_contains_customer_content
    ) values (
        true,p_cycle_id,p_worker_owner,v_started_at,clock_timestamp(),p_batch_limit,
        v_backlog,case when v_backlog then v_started_at else null end,
        v_remaining_candidates,true,true,true,false
    ) on conflict(singleton) do update set
        last_cycle_id=excluded.last_cycle_id,
        worker_owner=excluded.worker_owner,
        last_started_at=excluded.last_started_at,
        last_success_at=excluded.last_success_at,
        last_batch_limit=excluded.last_batch_limit,
        backlog_remaining=excluded.backlog_remaining,
        backlog_since=case when excluded.backlog_remaining
            then coalesce(state.backlog_since,excluded.last_started_at) else null end,
        remaining_candidates=excluded.remaining_candidates,
        schema_contract_ok=true,
        legal_holds_evaluated=true,
        financial_ledger_preserved=true,
        evidence_contains_customer_content=false;

    return jsonb_build_object(
        'schema','brevitas.compliance-retention-cycle.v1','status','completed',
        'cycle_id',p_cycle_id,'worker_owner',p_worker_owner,
        'started_at',v_started_at,'completed_at',clock_timestamp(),
        'batch_limit',p_batch_limit,'initial_candidates',v_initial_candidates,
        'remaining_candidates',v_remaining_candidates,'backlog_remaining',v_backlog,
        'dry_run',v_dry,'apply',v_apply,'post_apply_dry_run',v_post,
        'schema_contract_ok',true,'legal_holds_evaluated',true,
        'financial_ledger_preserved',true,
        'evidence_contains_customer_content',false
    );
end;
$$;

-- Erasure for an account-less prospect. Keyed on the canonicalized email because
-- that is the only identifier a waitlist row has; the audit evidence records a
-- truncated digest, never the address, so it satisfies both the erasure request
-- and the content-free audit schema (an '@' or a bare 64-hex target_id is
-- rejected outright by validate_audit_event_insert, 202607170005:176-190).
create or replace function public.erase_waitlist_signup(
    p_email text,
    p_actor_id text,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_email text := lower(trim(coalesce(p_email, '')));
    v_deleted integer := 0;
    v_target text;
begin
    perform public.compliance_actor_role(p_actor_id);
    if v_email = '' or char_length(v_email) > 255 or v_email !~ '^[^@[:space:]]+@[^@[:space:]]+$'
       or p_request_id !~ '^[A-Za-z0-9._:-]{8,128}$' then
        raise exception 'waitlist erasure arguments are invalid' using errcode = '22023';
    end if;
    v_target := 'waitlist:' || substr(
        encode(digest(v_email, 'sha256'), 'hex'), 1, 32);
    delete from public.waitlist entry where lower(entry.email) = v_email;
    get diagnostics v_deleted = row_count;
    perform public.append_company_audit(
        null, p_actor_id, public.compliance_actor_role(p_actor_id), p_request_id,
        case when v_deleted > 0 then 'waitlist.erased' else 'waitlist.erase.noop' end,
        'waitlist', v_target, 'committed'
    );
    return jsonb_build_object(
        'schema', 'brevitas.waitlist-erasure.v1',
        'status', case when v_deleted > 0 then 'erased' else 'not_found' end,
        'rows_deleted', v_deleted,
        'subject_digest', v_target,
        'evidence_contains_customer_content', false
    );
end;
$function$;
revoke all on function public.erase_waitlist_signup(text,text,text)
    from public, anon, authenticated;
grant execute on function public.erase_waitlist_signup(text,text,text)
    to service_role;

comment on function public.erase_waitlist_signup(text,text,text) is
    'Erasure path for an account-less waitlist prospect, keyed on the canonicalized email and audited with a truncated digest only.';
comment on function public.compliance_run_retention(uuid,text,integer,boolean) is
    'Authoritative retention: deletes expired usage/evidence, minimizes the ledger-preserved usage rows it cannot delete, and ages out waitlist prospects.';

commit;
