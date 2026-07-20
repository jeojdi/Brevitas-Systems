-- Compliance data-rights workflow. Release order: after
-- 202607170006_database_scaling.sql. This migration is repository-controlled;
-- applying it to staging or production requires a separately approved change.

do $$
begin
    if to_regprocedure('public.append_company_audit(uuid,text,text,text,text,text,text,text)') is null
       or to_regprocedure('public.usage_page(text,uuid,text,timestamptz,bigint,integer)') is null then
        raise exception '202607170007 requires migrations through 202607170006';
    end if;
end;
$$;

create table if not exists public.data_subject_requests (
    id uuid primary key,
    organization_id uuid not null references public.organizations(id) on delete restrict,
    request_type text not null check (request_type in ('export', 'delete')),
    request_scope text not null default 'tenant'
        check (request_scope in ('tenant', 'member', 'customer')),
    subject_id uuid,
    status text not null default 'pending'
        check (status in ('pending', 'approved', 'processing', 'completed', 'denied', 'failed')),
    evidence_reference text not null
        check (evidence_reference ~ '^[A-Za-z0-9._:-]{8,128}$'
               and evidence_reference !~ '@'),
    requested_at timestamptz not null default clock_timestamp(),
    due_at timestamptz not null,
    approved_at timestamptz,
    approved_by text,
    started_at timestamptz,
    completed_at timestamptz,
    deadline_breached boolean not null default false,
    deadline_breached_at timestamptz,
    export_artifact_sha256 text,
    export_attestation_sha256 text,
    portable_record_count integer,
    portable_records_sha256 text,
    created_by text not null
        check (created_by ~ '^(system|brevitas_admin):[A-Za-z0-9._:-]{3,96}$'),
    unique (organization_id, id),
    check ((request_scope = 'tenant' and subject_id is null)
           or (request_scope in ('member', 'customer') and subject_id is not null)),
    check (due_at > requested_at
           and due_at <= requested_at + interval '30 days'),
    check (approved_by is null
           or approved_by ~ '^(system|brevitas_admin):[A-Za-z0-9._:-]{3,96}$'),
    check (status = 'pending' or (approved_at is not null and approved_by is not null)),
    check (status <> 'processing' or started_at is not null),
    check (status <> 'completed' or completed_at is not null),
    check ((not deadline_breached and deadline_breached_at is null)
           or (deadline_breached and deadline_breached_at is not null)),
    check (export_artifact_sha256 is null
           or (request_type = 'export' and export_artifact_sha256 ~ '^[0-9a-f]{64}$')),
    check (export_attestation_sha256 is null
           or (request_type='export' and export_attestation_sha256 ~ '^[0-9a-f]{64}$')),
    check (portable_records_sha256 is null
           or (request_type='export' and portable_records_sha256 ~ '^[0-9a-f]{64}$')),
    check (portable_record_count is null
           or (request_type='export' and portable_record_count between 0 and 10000000)),
    check (request_type <> 'export' or status <> 'completed'
           or (export_artifact_sha256 is not null
               and export_attestation_sha256 is not null
               and portable_record_count is not null
               and portable_records_sha256 is not null))
);
create index if not exists data_subject_requests_tenant_status_idx
    on public.data_subject_requests(organization_id, status, due_at, id);
create index if not exists data_subject_requests_deadline_idx
    on public.data_subject_requests(due_at, id)
    where status in ('pending', 'approved', 'processing');
alter table public.data_subject_requests enable row level security;

-- Holds are service-readable for fail-closed preflight but mutation is possible
-- only through the compliance RPCs below. The administrative API must verify a
-- Brevitas compliance administrator before calling those RPCs.
create table if not exists public.legal_holds (
    id uuid primary key,
    organization_id uuid not null references public.organizations(id) on delete restrict,
    scope text not null check (scope in ('all', 'export', 'delete')),
    reason_code text not null check (reason_code ~ '^[a-z0-9_.-]{3,64}$'),
    active boolean not null default true,
    created_by text not null
        check (created_by ~ '^(system|brevitas_admin):[A-Za-z0-9._:-]{3,96}$'),
    created_at timestamptz not null default clock_timestamp(),
    expires_at timestamptz,
    released_by text,
    released_at timestamptz,
    unique (organization_id, id),
    check (expires_at is null or expires_at > created_at),
    check ((active and released_by is null and released_at is null)
           or (not active and released_by is not null and released_at is not null)),
    check (released_by is null
           or released_by ~ '^(system|brevitas_admin):[A-Za-z0-9._:-]{3,96}$')
);
create index if not exists legal_holds_active_tenant_idx
    on public.legal_holds(organization_id, scope, expires_at, id)
    where active;
alter table public.legal_holds enable row level security;

-- Every administrative hold mutation is first recorded as an immutable
-- two-person action request. A pending create is already preservation intent:
-- matching exports/deletions and tenant retention fail closed until it expires
-- or a distinct Brevitas administrator approves it. A pending release never
-- weakens the active hold; only approval changes public.legal_holds.
create table if not exists public.legal_hold_actions (
    id uuid primary key,
    organization_id uuid not null references public.organizations(id) on delete restrict,
    action text not null check (action in ('create', 'release')),
    target_hold_id uuid not null,
    scope text not null check (scope in ('all', 'export', 'delete')),
    reason_code text not null check (reason_code ~ '^[a-z0-9_.-]{3,64}$'),
    expires_at timestamptz,
    status text not null default 'pending' check (status in ('pending', 'approved')),
    requested_by text not null
        check (requested_by ~ '^brevitas_admin:[A-Za-z0-9._:-]{3,96}$'),
    requested_at timestamptz not null default clock_timestamp(),
    approved_by text,
    approved_at timestamptz,
    unique (organization_id, id),
    unique (action, target_hold_id),
    check (approved_by is null
           or approved_by ~ '^brevitas_admin:[A-Za-z0-9._:-]{3,96}$'),
    check ((status='pending' and approved_by is null and approved_at is null)
           or (status='approved' and approved_by is not null and approved_at is not null
               and approved_by<>requested_by))
);
create index if not exists legal_hold_actions_tenant_status_idx
    on public.legal_hold_actions(organization_id,status,requested_at,id);
create index if not exists legal_hold_actions_pending_create_idx
    on public.legal_hold_actions(organization_id,scope,expires_at,target_hold_id)
    where status='pending' and action='create';
alter table public.legal_hold_actions enable row level security;

-- Tombstones are immutable evidence/instructions for every PITR or logical
-- restore. `expires_at` is the deletion deadline and may already be in the past
-- for an overdue request; such a request remains processable and is urgent.
create table if not exists public.backup_deletion_tombstones (
    request_id uuid primary key,
    organization_id uuid not null,
    request_received_at timestamptz not null,
    created_at timestamptz not null default clock_timestamp(),
    expires_at timestamptz not null,
    foreign key (organization_id, request_id)
        references public.data_subject_requests(organization_id, id) on delete restrict,
    check (expires_at = request_received_at + interval '35 days')
);
create index if not exists backup_deletion_tombstones_expiry_idx
    on public.backup_deletion_tombstones(expires_at, request_id);
alter table public.backup_deletion_tombstones enable row level security;

-- Content-free execution evidence for the bounded authoritative retention job.
-- Dry-run counts are returned to the operator without inserting a row; applied
-- batches insert exactly one immutable row keyed by the operator-supplied UUID.
create table if not exists public.compliance_retention_runs (
    id uuid primary key,
    actor_id text not null
        check (actor_id ~ '^(system|brevitas_admin):[A-Za-z0-9._:-]{3,96}$'),
    batch_limit integer not null check (batch_limit between 1 and 10000),
    usage_candidates integer not null check (usage_candidates between 0 and batch_limit),
    audit_candidates integer not null check (audit_candidates between 0 and batch_limit),
    support_candidates integer not null check (support_candidates between 0 and batch_limit),
    requests_candidates integer not null check (requests_candidates between 0 and batch_limit),
    holds_candidates integer not null check (holds_candidates between 0 and batch_limit),
    prior_run_evidence_candidates integer not null
        check (prior_run_evidence_candidates between 0 and batch_limit),
    usage_deleted integer not null check (usage_deleted between 0 and batch_limit),
    audit_deleted integer not null check (audit_deleted between 0 and batch_limit),
    support_deleted integer not null check (support_deleted between 0 and batch_limit),
    requests_deleted integer not null check (requests_deleted between 0 and batch_limit),
    holds_deleted integer not null check (holds_deleted between 0 and batch_limit),
    prior_run_evidence_deleted integer not null
        check (prior_run_evidence_deleted between 0 and batch_limit),
    result text not null default 'completed' check (result='completed'),
    completed_at timestamptz not null default clock_timestamp()
);
create index if not exists compliance_retention_runs_completed_idx
    on public.compliance_retention_runs(completed_at,id);
alter table public.compliance_retention_runs enable row level security;

-- Mutable singleton health for the dedicated retention authority. It contains
-- only opaque IDs, timestamps, bounded counts, and fixed booleans; immutable
-- per-cycle evidence remains in compliance_retention_runs/audit_events.
create table if not exists public.compliance_retention_worker_state (
    singleton boolean primary key default true check (singleton),
    last_cycle_id uuid not null,
    worker_owner text not null
        check (worker_owner ~ '^[A-Za-z0-9._:-]{3,128}$' and worker_owner !~ '@'),
    last_started_at timestamptz not null,
    last_success_at timestamptz not null,
    last_batch_limit integer not null check (last_batch_limit between 1 and 10000),
    backlog_remaining boolean not null,
    backlog_since timestamptz,
    remaining_candidates integer not null check (remaining_candidates between 0 and 60000),
    schema_contract_ok boolean not null default true check (schema_contract_ok),
    legal_holds_evaluated boolean not null default true check (legal_holds_evaluated),
    financial_ledger_preserved boolean not null default true check (financial_ledger_preserved),
    evidence_contains_customer_content boolean not null default false
        check (not evidence_contains_customer_content),
    check ((backlog_remaining and backlog_since is not null)
           or (not backlog_remaining and backlog_since is null))
);
alter table public.compliance_retention_worker_state enable row level security;

revoke all on table public.data_subject_requests from public, anon, authenticated, service_role;
revoke all on table public.legal_holds from public, anon, authenticated, service_role;
revoke all on table public.legal_hold_actions from public, anon, authenticated, service_role;
revoke all on table public.backup_deletion_tombstones from public, anon, authenticated, service_role;
revoke all on table public.compliance_retention_runs from public, anon, authenticated, service_role;
revoke all on table public.compliance_retention_worker_state from public, anon, authenticated, service_role;
grant select on table public.data_subject_requests to service_role;
grant select on table public.legal_holds to service_role;
grant select on table public.legal_hold_actions to service_role;
grant select on table public.backup_deletion_tombstones to service_role;
grant select on table public.compliance_retention_runs to service_role;

create or replace function public.reject_backup_tombstone_mutation()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    raise exception 'backup deletion tombstones are immutable' using errcode = '55000';
end;
$$;
revoke all on function public.reject_backup_tombstone_mutation()
    from public, anon, authenticated, service_role;
drop trigger if exists backup_tombstones_reject_update_delete
    on public.backup_deletion_tombstones;
create trigger backup_tombstones_reject_update_delete
    before update or delete on public.backup_deletion_tombstones
    for each row execute function public.reject_backup_tombstone_mutation();
drop trigger if exists backup_tombstones_reject_truncate
    on public.backup_deletion_tombstones;
create trigger backup_tombstones_reject_truncate
    before truncate on public.backup_deletion_tombstones
    for each statement execute function public.reject_backup_tombstone_mutation();

create or replace function public.reject_compliance_retention_run_mutation()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    raise exception 'compliance retention run evidence is immutable' using errcode='55000';
end;
$$;
revoke all on function public.reject_compliance_retention_run_mutation()
    from public, anon, authenticated, service_role;
drop trigger if exists compliance_retention_runs_reject_update_delete
    on public.compliance_retention_runs;
create trigger compliance_retention_runs_reject_update_delete
    before update or delete on public.compliance_retention_runs
    for each row execute function public.reject_compliance_retention_run_mutation();
drop trigger if exists compliance_retention_runs_reject_truncate
    on public.compliance_retention_runs;
create trigger compliance_retention_runs_reject_truncate
    before truncate on public.compliance_retention_runs
    for each statement execute function public.reject_compliance_retention_run_mutation();

create or replace function public.enforce_legal_hold_action_transition()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if tg_op<>'UPDATE' then
        raise exception 'legal hold action request evidence is immutable'
            using errcode='55000';
    end if;
    if old.status<>'pending' or new.status<>'approved'
       or new.id<>old.id
       or new.organization_id<>old.organization_id
       or new.action<>old.action
       or new.target_hold_id<>old.target_hold_id
       or new.scope<>old.scope
       or new.reason_code<>old.reason_code
       or new.expires_at is distinct from old.expires_at
       or new.requested_by<>old.requested_by
       or new.requested_at<>old.requested_at
       or new.approved_by is null or new.approved_by=new.requested_by
       or new.approved_at is null then
        raise exception 'legal hold action request evidence is immutable'
            using errcode='55000';
    end if;
    return new;
end;
$$;
revoke all on function public.enforce_legal_hold_action_transition()
    from public, anon, authenticated, service_role;
drop trigger if exists legal_hold_actions_enforce_transition
    on public.legal_hold_actions;
create trigger legal_hold_actions_enforce_transition
    before update or delete on public.legal_hold_actions
    for each row execute function public.enforce_legal_hold_action_transition();
drop trigger if exists legal_hold_actions_reject_truncate
    on public.legal_hold_actions;
create trigger legal_hold_actions_reject_truncate
    before truncate on public.legal_hold_actions
    for each statement execute function public.enforce_legal_hold_action_transition();

create or replace function public.compliance_actor_role(p_actor_id text)
returns text
language plpgsql
immutable
security definer
set search_path = public, pg_temp
as $$
begin
    if p_actor_id is null
       or p_actor_id !~ '^(system|brevitas_admin):[A-Za-z0-9._:-]{3,96}$'
       or p_actor_id ~ '@'
       or p_actor_id ~* '(^|[._:-])(secret|password|token|api[_-]?key)([._:-]|$)' then
        raise exception 'invalid compliance actor' using errcode = '22023';
    end if;
    if p_actor_id like 'system:%' then
        return 'system';
    end if;
    return 'brevitas_admin';
end;
$$;
revoke all on function public.compliance_actor_role(text)
    from public, anon, authenticated, service_role;

-- Optional support storage is erased only through an application-owned exact
-- adapter because this migration cannot safely guess a future support schema.
-- The adapter runs in the caller transaction and must prove zero remaining
-- scoped rows; absence, exception, widening, or a malformed result aborts the
-- entire deletion before completion can be committed.
create or replace function public.compliance_erase_support_records(
    p_organization_id uuid,
    p_request_scope text,
    p_subject_id uuid
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_result jsonb;
    v_expected_keys text[] := array[
        'schema','organization_id','request_scope','subject_id',
        'deleted_count','anonymized_count','remaining_count'
    ];
begin
    if p_organization_id is null
       or p_request_scope not in ('tenant','member','customer')
       or (p_request_scope='tenant' and p_subject_id is not null)
       or (p_request_scope<>'tenant' and p_subject_id is null) then
        raise exception 'invalid support erasure scope' using errcode='22023';
    end if;
    if to_regclass('public.support_records') is null then
        return jsonb_build_object(
            'schema','brevitas.support-erasure.v1','organization_id',p_organization_id,
            'request_scope',p_request_scope,'subject_id',p_subject_id,
            'deleted_count',0,'anonymized_count',0,'remaining_count',0
        );
    end if;
    if p_request_scope='tenant' then
        if to_regprocedure('public.compliance_delete_support_records(uuid)') is null then
            raise exception 'support_records exists without its tenant erasure adapter'
                using errcode='55000';
        end if;
        execute 'select public.compliance_delete_support_records($1)'
           into v_result using p_organization_id;
    else
        if to_regprocedure('public.compliance_delete_support_subject(uuid,text,uuid)') is null then
            raise exception 'support_records exists without its subject erasure adapter'
                using errcode='55000';
        end if;
        execute 'select public.compliance_delete_support_subject($1,$2,$3)'
           into v_result using p_organization_id,p_request_scope,p_subject_id;
    end if;
    if v_result is null
       or jsonb_typeof(v_result)<>'object'
       or array(select jsonb_object_keys(v_result) order by 1)
          is distinct from array(select unnest(v_expected_keys) order by 1)
       or v_result->>'schema'<>'brevitas.support-erasure.v1'
       or v_result->>'organization_id'<>p_organization_id::text
       or v_result->>'request_scope'<>p_request_scope
       or v_result->'subject_id' is distinct from
          coalesce(to_jsonb(p_subject_id),'null'::jsonb)
       or (v_result->>'deleted_count')::integer<0
       or (v_result->>'anonymized_count')::integer<0
       or (v_result->>'remaining_count')::integer<>0 then
        raise exception 'support erasure adapter result is invalid' using errcode='55000';
    end if;
    return v_result;
exception
    when invalid_text_representation or numeric_value_out_of_range then
        raise exception 'support erasure adapter result is invalid' using errcode='55000';
end;
$$;
revoke all on function public.compliance_erase_support_records(uuid,text,uuid)
    from public, anon, authenticated, service_role;

create or replace function public.compliance_assert_usage_export_schema()
returns void
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
begin
    if (select count(*) from information_schema.columns
         where table_schema='public' and table_name='usage_log'
           and ((column_name in ('cache_write_5m_tokens','cache_write_1h_tokens')
                 and data_type='bigint')
                or (column_name='cache_attributable' and data_type='boolean')))<>3 then
        raise exception 'usage receipt export schema is incomplete; migration 012 is required'
            using errcode='55000';
    end if;
end;
$$;
revoke all on function public.compliance_assert_usage_export_schema()
    from public, anon, authenticated, service_role;

create or replace function public.compliance_request_has_hold(
    p_organization_id uuid, p_request_type text
) returns boolean
language sql
security definer
set search_path = public, pg_temp
as $$
    select exists (
        select 1
          from public.legal_holds hold
         where hold.organization_id = p_organization_id
           and hold.active
           and (hold.expires_at is null or hold.expires_at > clock_timestamp())
           and hold.scope in ('all', p_request_type)
    ) or exists (
        select 1
          from public.legal_hold_actions hold_action
         where hold_action.organization_id=p_organization_id
           and hold_action.action='create'
           and hold_action.status='pending'
           and (hold_action.expires_at is null
                or hold_action.expires_at>clock_timestamp())
           and hold_action.scope in ('all',p_request_type)
    );
$$;
revoke all on function public.compliance_request_has_hold(uuid,text)
    from public, anon, authenticated, service_role;

create or replace function public.compliance_preservation_hold(
    p_organization_id uuid
) returns boolean
language sql
security definer
set search_path = public, pg_temp
as $$
    select exists (
        select 1 from public.legal_holds hold
         where hold.organization_id=p_organization_id
           and hold.active
           and (hold.expires_at is null or hold.expires_at>clock_timestamp())
    ) or exists (
        select 1 from public.legal_hold_actions hold_action
         where hold_action.organization_id=p_organization_id
           and hold_action.action='create'
           and hold_action.status='pending'
           and (hold_action.expires_at is null
                or hold_action.expires_at>clock_timestamp())
    );
$$;
revoke all on function public.compliance_preservation_hold(uuid)
    from public, anon, authenticated, service_role;

create or replace function public.compliance_global_preservation_hold()
returns boolean
language sql
security definer
set search_path = public, pg_temp
as $$
    select exists (
        select 1 from public.legal_holds hold
         where hold.active
           and (hold.expires_at is null or hold.expires_at>clock_timestamp())
    ) or exists (
        select 1 from public.legal_hold_actions hold_action
         where hold_action.action='create'
           and hold_action.status='pending'
           and (hold_action.expires_at is null
                or hold_action.expires_at>clock_timestamp())
    );
$$;
revoke all on function public.compliance_global_preservation_hold()
    from public, anon, authenticated, service_role;

create or replace function public.compliance_record_deadline_breach(
    p_request public.data_subject_requests,
    p_actor_id text
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if clock_timestamp() <= p_request.due_at or p_request.deadline_breached then
        return p_request.deadline_breached;
    end if;
    update public.data_subject_requests
       set deadline_breached = true,
           deadline_breached_at = clock_timestamp()
     where id = p_request.id and organization_id = p_request.organization_id;
    perform public.append_company_audit(
        p_request.organization_id, p_actor_id,
        public.compliance_actor_role(p_actor_id), p_request.id::text,
        'compliance.deadline_breached', 'data_subject_request',
        p_request.id::text, 'committed'
    );
    return true;
end;
$$;
revoke all on function public.compliance_record_deadline_breach(public.data_subject_requests,text)
    from public, anon, authenticated, service_role;

-- This helper is deliberately non-callable, including by service_role. It is
-- reached only from the bounded retention RPC owned by the same migration role.
-- ALTER TRIGGER is transactional and takes an exclusive table lock; callers
-- cannot manufacture a session flag or caller-controlled bypass for immutable
-- evidence. Any exception rolls back both deletion and trigger state.
create or replace function public.compliance_retention_delete_immutable(
    p_evidence_cutoff timestamptz,
    p_batch_limit integer
) returns table(audit_deleted integer, requests_deleted integer,
                prior_run_evidence_deleted integer)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_request_ids uuid[] := array[]::uuid[];
begin
    if p_batch_limit is null
       or p_evidence_cutoff is null
       or p_batch_limit not between 1 and 10000
       or p_evidence_cutoff > clock_timestamp()-interval '400 days' then
        raise exception 'invalid immutable retention boundary' using errcode='22023';
    end if;

    execute 'alter table public.audit_events disable trigger audit_events_reject_update_delete';
    delete from public.audit_events event
     where event.id in (
        select candidate.id from public.audit_events candidate
         where candidate.occurred_at < p_evidence_cutoff
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.occurred_at,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics audit_deleted = row_count;
    execute 'alter table public.audit_events enable trigger audit_events_reject_update_delete';

    select coalesce(array_agg(candidate.id order by candidate.completed_at,candidate.id),array[]::uuid[])
      into v_request_ids
      from (
        select request.id,request.completed_at
          from public.data_subject_requests request
         where request.status='completed'
           and request.completed_at < p_evidence_cutoff
           and not public.compliance_preservation_hold(request.organization_id)
         order by request.completed_at,request.id
         for update skip locked
         limit p_batch_limit
      ) candidate;
    execute 'alter table public.backup_deletion_tombstones disable trigger backup_tombstones_reject_update_delete';
    delete from public.backup_deletion_tombstones tombstone
     where tombstone.request_id=any(v_request_ids)
       and tombstone.expires_at<clock_timestamp();
    execute 'alter table public.backup_deletion_tombstones enable trigger backup_tombstones_reject_update_delete';
    delete from public.data_subject_requests request where request.id=any(v_request_ids);
    get diagnostics requests_deleted = row_count;

    execute 'alter table public.compliance_retention_runs disable trigger compliance_retention_runs_reject_update_delete';
    delete from public.compliance_retention_runs run
     where run.id in (
        select candidate.id from public.compliance_retention_runs candidate
         where candidate.completed_at < p_evidence_cutoff
           and not public.compliance_global_preservation_hold()
         order by candidate.completed_at,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics prior_run_evidence_deleted = row_count;
    execute 'alter table public.compliance_retention_runs enable trigger compliance_retention_runs_reject_update_delete';
    return next;
exception when others then
    -- The exception subtransaction has already restored trigger state. These
    -- idempotent enables also fail closed if a future trigger contract drifts.
    execute 'alter table public.audit_events enable trigger audit_events_reject_update_delete';
    execute 'alter table public.backup_deletion_tombstones enable trigger backup_tombstones_reject_update_delete';
    execute 'alter table public.compliance_retention_runs enable trigger compliance_retention_runs_reject_update_delete';
    raise;
end;
$$;
revoke all on function public.compliance_retention_delete_immutable(timestamptz,integer)
    from public, anon, authenticated, service_role;

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
    v_evidence_cutoff timestamptz := clock_timestamp()-interval '400 days';
    v_existing public.compliance_retention_runs%rowtype;
    v_usage_candidates integer := 0;
    v_audit_candidates integer := 0;
    v_support_candidates integer := 0;
    v_request_candidates integer := 0;
    v_hold_candidates integer := 0;
    v_prior_run_candidates integer := 0;
    v_usage_deleted integer := 0;
    v_audit_deleted integer := 0;
    v_support_deleted integer := 0;
    v_requests_deleted integer := 0;
    v_holds_deleted integer := 0;
    v_prior_run_deleted integer := 0;
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
            'usage_deleted',0,'audit_deleted',0,'support_deleted',0,
            'requests_deleted',0,'holds_deleted',0,'prior_run_evidence_deleted',0,
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
        requests_deleted,holds_deleted,prior_run_evidence_deleted
    ) values (
        p_run_id,p_actor_id,p_batch_limit,v_usage_candidates,v_audit_candidates,v_support_candidates,
        v_request_candidates,v_hold_candidates,v_prior_run_candidates,
        v_usage_deleted,v_audit_deleted,v_support_deleted,
        v_requests_deleted,v_holds_deleted,v_prior_run_deleted
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
        'idempotent_replay',false,'evidence_contains_customer_content',false
    );
end;
$$;

-- One bounded transaction is the advisory lease. PostgREST/Supavisor cannot
-- safely hold a session lock across requests, so dry-run, optional apply, and
-- post-apply backlog verification all execute under this transaction-scoped
-- database authority. Competing Railway replicas receive lease_unavailable.
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
        (v_dry->>'prior_run_evidence_candidates')::integer;
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
        (v_post->>'prior_run_evidence_candidates')::integer;
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

create or replace function public.compliance_retention_worker_health()
returns jsonb
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select case when state.singleton is null then jsonb_build_object(
        'schema','brevitas.compliance-retention-health.v1',
        'initialized',false,'missed_run_24h',true,'backlog_over_24h',false,
        'evidence_contains_customer_content',false
    ) else jsonb_build_object(
        'schema','brevitas.compliance-retention-health.v1','initialized',true,
        'last_cycle_id',state.last_cycle_id,'last_success_at',state.last_success_at,
        'backlog_remaining',state.backlog_remaining,
        'backlog_since',state.backlog_since,
        'remaining_candidates',state.remaining_candidates,
        'missed_run_24h',state.last_success_at<now()-interval '24 hours',
        'backlog_over_24h',state.backlog_remaining
            and state.backlog_since<now()-interval '24 hours',
        'schema_contract_ok',state.schema_contract_ok,
        'legal_holds_evaluated',state.legal_holds_evaluated,
        'financial_ledger_preserved',state.financial_ledger_preserved,
        'evidence_contains_customer_content',false
    ) end
      from (select true singleton) singleton
      left join public.compliance_retention_worker_state state using(singleton);
$$;

-- Remove login/identity PII only after the user has no remaining organization
-- membership. The auth.users shell and content-free legal/financial rows remain
-- so immutable billing/audit foreign keys stay valid. Optional legacy/platform
-- tables are addressed only after catalog checks.
create or replace function public.compliance_anonymize_unshared_user(p_user_id uuid)
returns boolean
language plpgsql
security definer
set search_path = public, auth, pg_temp
as $$
declare
    v_column text;
    v_placeholder_email text := 'deleted+' || p_user_id::text || '@deleted.invalid';
begin
    if exists (select 1 from public.organization_members where user_id = p_user_id) then
        return false;
    end if;
    if to_regclass('public.billing_accounts') is not null then
        -- Preserve the account and provider/invoice identifiers required for
        -- minimized financial evidence, but remove ephemeral checkout state.
        if exists (select 1 from information_schema.columns
                    where table_schema='public' and table_name='billing_accounts'
                      and column_name='checkout_session_id') then
            execute 'update public.billing_accounts set checkout_session_id=null where user_id=$1'
              using p_user_id;
        end if;
        if exists (select 1 from information_schema.columns
                    where table_schema='public' and table_name='billing_accounts'
                      and column_name='updated_at') then
            execute 'update public.billing_accounts set updated_at=clock_timestamp() where user_id=$1'
              using p_user_id;
        end if;
    end if;
    if to_regclass('public.billing_events') is not null
       and exists (select 1 from information_schema.columns
                    where table_schema='public' and table_name='billing_events'
                      and column_name='session_id')
       and exists (select 1 from information_schema.columns
                    where table_schema='public' and table_name='billing_events'
                      and column_name='user_id') then
        execute 'update public.billing_events set session_id='''' where user_id=$1'
          using p_user_id;
    end if;
    -- Optional legal_acceptances rows remain as minimized legal evidence. The
    -- UUID now resolves only to the non-login auth.users placeholder shell.
    if to_regclass('public.legal_acceptances') is not null then
        null; -- intentionally retained; never copy its values into telemetry
    end if;
    if to_regclass('public.profiles') is not null
       and exists (select 1 from information_schema.columns
                    where table_schema='public' and table_name='profiles'
                      and column_name='id') then
        execute 'delete from public.profiles where id=$1' using p_user_id;
    end if;
    foreach v_column in array array[
        'confirmation_token','recovery_token','email_change_token_new',
        'email_change_token_current','phone_change_token','reauthentication_token',
        'email_change','phone_change'
    ] loop
        if exists (
            select 1 from information_schema.columns
             where table_schema='auth' and table_name='users' and column_name=v_column
        ) then
            execute format('update auth.users set %I='''' where id=$1', v_column)
              using p_user_id;
        end if;
    end loop;
    if exists (select 1 from information_schema.columns
                where table_schema='auth' and table_name='users' and column_name='phone') then
        execute 'update auth.users set phone=null where id=$1' using p_user_id;
    end if;
    if exists (select 1 from information_schema.columns
                where table_schema='auth' and table_name='users' and column_name='encrypted_password') then
        execute 'update auth.users set encrypted_password=null where id=$1' using p_user_id;
    end if;
    if exists (select 1 from information_schema.columns
                where table_schema='auth' and table_name='users' and column_name='raw_app_meta_data') then
        execute 'update auth.users set raw_app_meta_data=''{"provider":"disabled","providers":[]}''::jsonb where id=$1'
          using p_user_id;
    end if;
    if exists (select 1 from information_schema.columns
                where table_schema='auth' and table_name='users' and column_name='banned_until') then
        execute 'update auth.users set banned_until=''infinity''::timestamptz where id=$1'
          using p_user_id;
    end if;
    if exists (select 1 from information_schema.columns
                where table_schema='auth' and table_name='users' and column_name='deleted_at') then
        execute 'update auth.users set deleted_at=coalesce(deleted_at,clock_timestamp()) where id=$1'
          using p_user_id;
    end if;
    execute 'update auth.users set email=$2, raw_user_meta_data=''{}''::jsonb where id=$1'
      using p_user_id, v_placeholder_email;
    if to_regclass('auth.sessions') is not null then
        execute 'delete from auth.sessions where user_id=$1' using p_user_id;
    end if;
    if to_regclass('auth.identities') is not null then
        execute 'delete from auth.identities where user_id=$1' using p_user_id;
    end if;
    if to_regclass('auth.mfa_factors') is not null then
        execute 'delete from auth.mfa_factors where user_id=$1' using p_user_id;
    end if;
    if to_regclass('auth.one_time_tokens') is not null then
        execute 'delete from auth.one_time_tokens where user_id=$1' using p_user_id;
    end if;
    return true;
end;
$$;
revoke all on function public.compliance_anonymize_unshared_user(uuid)
    from public, anon, authenticated, service_role;

create or replace function public.compliance_submit_data_request(
    p_organization_id uuid,
    p_request_id uuid,
    p_request_type text,
    p_actor_id text,
    p_evidence_reference text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_request public.data_subject_requests%rowtype;
    v_now timestamptz := clock_timestamp();
    v_inserted bigint;
begin
    perform public.compliance_actor_role(p_actor_id);
    if p_request_type not in ('export', 'delete')
       or p_evidence_reference !~ '^[A-Za-z0-9._:-]{8,128}$'
       or p_evidence_reference ~ '@' then
        raise exception 'invalid data-subject request' using errcode = '22023';
    end if;
    perform 1 from public.organizations where id = p_organization_id for update;
    if not found then
        raise exception 'organization not found' using errcode = 'P0002';
    end if;
    insert into public.data_subject_requests(
        id, organization_id, request_type, status, evidence_reference,
        requested_at, due_at, created_by
    ) values (
        p_request_id, p_organization_id, p_request_type, 'pending',
        p_evidence_reference, v_now, v_now + interval '30 days', p_actor_id
    ) on conflict (id) do nothing;
    get diagnostics v_inserted = row_count;
    select * into v_request from public.data_subject_requests
     where id = p_request_id for update;
    if v_request.organization_id <> p_organization_id
       or v_request.request_type <> p_request_type
       or v_request.request_scope <> 'tenant'
       or v_request.subject_id is not null
       or v_request.evidence_reference <> p_evidence_reference then
        raise exception 'request idempotency conflict' using errcode = '23505';
    end if;
    if v_inserted = 1 then
        perform public.append_company_audit(
            p_organization_id, p_actor_id, public.compliance_actor_role(p_actor_id),
            p_request_id::text, 'compliance.request.submitted',
            'data_subject_request', p_request_id::text, 'committed'
        );
    end if;
    return jsonb_build_object('id', v_request.id, 'status', v_request.status,
                              'due_at', v_request.due_at);
end;
$$;

create or replace function public.compliance_approve_data_request(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_request public.data_subject_requests%rowtype;
begin
    perform public.compliance_actor_role(p_actor_id);
    perform 1 from public.organizations where id = p_organization_id for update;
    if not found then raise exception 'organization not found' using errcode = 'P0002'; end if;
    select * into v_request from public.data_subject_requests
     where id = p_request_id and organization_id = p_organization_id for update;
    if not found then
        raise exception 'data-subject request not found' using errcode = 'P0002';
    end if;
    if v_request.created_by=p_actor_id then
        raise exception 'two-person approval requires a distinct compliance administrator'
            using errcode='42501';
    end if;
    if v_request.status in ('approved', 'processing', 'completed') then
        if v_request.approved_by is distinct from p_actor_id then
            raise exception 'request was approved by a different compliance administrator'
                using errcode='42501';
        end if;
        return v_request.status;
    end if;
    if v_request.status <> 'pending' then
        raise exception 'data-subject request is not approvable' using errcode = '55000';
    end if;
    update public.data_subject_requests
       set status = 'approved', approved_at = clock_timestamp(), approved_by = p_actor_id
     where id = p_request_id and organization_id = p_organization_id;
    perform public.append_company_audit(
        p_organization_id, p_actor_id, public.compliance_actor_role(p_actor_id),
        p_request_id::text, 'compliance.request.approved',
        'data_subject_request', p_request_id::text, 'committed'
    );
    return 'approved';
end;
$$;

create or replace function public.compliance_submit_subject_request(
    p_organization_id uuid,
    p_request_id uuid,
    p_request_type text,
    p_request_scope text,
    p_subject_id uuid,
    p_actor_id text,
    p_evidence_reference text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_request public.data_subject_requests%rowtype;
    v_now timestamptz := clock_timestamp();
    v_inserted bigint;
begin
    perform public.compliance_actor_role(p_actor_id);
    if p_request_type not in ('export', 'delete')
       or p_request_scope not in ('member', 'customer')
       or p_evidence_reference !~ '^[A-Za-z0-9._:-]{8,128}$'
       or p_evidence_reference ~ '@' then
        raise exception 'invalid subject request' using errcode = '22023';
    end if;
    perform 1 from public.organizations where id = p_organization_id for update;
    if not found then raise exception 'organization not found' using errcode = 'P0002'; end if;
    if p_request_scope = 'member' then
        perform 1 from public.organization_members
         where organization_id = p_organization_id and user_id = p_subject_id;
    else
        perform 1 from public.customers
         where organization_id = p_organization_id and id = p_subject_id;
    end if;
    if not found then raise exception 'subject not found in organization' using errcode = 'P0002'; end if;
    insert into public.data_subject_requests(
        id, organization_id, request_type, request_scope, subject_id,
        status, evidence_reference, requested_at, due_at, created_by
    ) values (
        p_request_id, p_organization_id, p_request_type, p_request_scope,
        p_subject_id, 'pending', p_evidence_reference, v_now,
        v_now + interval '30 days', p_actor_id
    ) on conflict (id) do nothing;
    get diagnostics v_inserted = row_count;
    select * into v_request from public.data_subject_requests
     where id = p_request_id for update;
    if v_request.organization_id <> p_organization_id
       or v_request.request_type <> p_request_type
       or v_request.request_scope <> p_request_scope
       or v_request.subject_id is distinct from p_subject_id
       or v_request.evidence_reference <> p_evidence_reference then
        raise exception 'subject request idempotency conflict' using errcode = '23505';
    end if;
    if v_inserted = 1 then
        perform public.append_company_audit(
            p_organization_id, p_actor_id, public.compliance_actor_role(p_actor_id),
            p_request_id::text, 'compliance.subject_request.submitted',
            p_request_scope, p_subject_id::text, 'committed'
        );
    end if;
    return jsonb_build_object('id', v_request.id, 'status', v_request.status,
        'scope', v_request.request_scope, 'subject_id', v_request.subject_id,
        'due_at', v_request.due_at);
end;
$$;

-- Remove the former single-actor commit surface when this migration is used
-- to upgrade an ephemeral/staging database built from an earlier draft.
drop function if exists public.compliance_create_legal_hold(
    uuid,uuid,text,text,text,text,timestamptz
);
drop function if exists public.compliance_release_legal_hold(uuid,uuid,text,text);

create or replace function public.compliance_request_legal_hold_action(
    p_organization_id uuid,
    p_action_id uuid,
    p_action text,
    p_hold_id uuid,
    p_scope text,
    p_reason_code text,
    p_actor_id text,
    p_audit_request_id text,
    p_expires_at timestamptz default null
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_action public.legal_hold_actions%rowtype;
    v_hold public.legal_holds%rowtype;
    v_inserted bigint;
    v_scope text;
    v_reason_code text;
    v_expires_at timestamptz;
begin
    if public.compliance_actor_role(p_actor_id)<>'brevitas_admin' then
        raise exception 'legal hold actions require a verified Brevitas administrator'
            using errcode='42501';
    end if;
    if p_action_id is null or p_hold_id is null
       or p_action not in ('create','release') then
        raise exception 'invalid legal hold action request' using errcode='22023';
    end if;
    if p_action='release'
       and (p_scope is not null or p_reason_code is not null
            or p_expires_at is not null) then
        raise exception 'release request derives immutable hold fields from its target'
            using errcode='22023';
    end if;
    perform 1 from public.organizations
     where id=p_organization_id for update;
    if not found then raise exception 'organization not found' using errcode='P0002'; end if;

    select * into v_action from public.legal_hold_actions
     where id=p_action_id for update;
    if found then
        if v_action.organization_id<>p_organization_id
           or v_action.action<>p_action
           or v_action.target_hold_id<>p_hold_id
           or v_action.requested_by<>p_actor_id
           or (p_action='create' and (
                v_action.scope is distinct from p_scope
                or v_action.reason_code is distinct from p_reason_code
                or v_action.expires_at is distinct from p_expires_at
           )) then
            raise exception 'legal hold action idempotency conflict' using errcode='23505';
        end if;
        return to_jsonb(v_action)-'organization_id';
    end if;

    if p_action='create' then
        if p_scope not in ('all','export','delete')
           or p_reason_code !~ '^[a-z0-9_.-]{3,64}$'
           or (p_expires_at is not null and p_expires_at<=clock_timestamp()) then
            raise exception 'invalid legal hold create request' using errcode='22023';
        end if;
        if exists (select 1 from public.legal_holds where id=p_hold_id) then
            raise exception 'legal hold target already exists' using errcode='23505';
        end if;
        v_scope:=p_scope;
        v_reason_code:=p_reason_code;
        v_expires_at:=p_expires_at;
    else
        select * into v_hold from public.legal_holds
         where id=p_hold_id and organization_id=p_organization_id for update;
        if not found then raise exception 'legal hold not found' using errcode='P0002'; end if;
        if not v_hold.active then
            raise exception 'legal hold is already released' using errcode='55000';
        end if;
        v_scope:=v_hold.scope;
        v_reason_code:=v_hold.reason_code;
        v_expires_at:=v_hold.expires_at;
    end if;

    insert into public.legal_hold_actions(
        id,organization_id,action,target_hold_id,scope,reason_code,expires_at,
        status,requested_by
    ) values (
        p_action_id,p_organization_id,p_action,p_hold_id,v_scope,v_reason_code,
        v_expires_at,'pending',p_actor_id
    ) on conflict(id) do nothing;
    get diagnostics v_inserted=row_count;
    select * into v_action from public.legal_hold_actions
     where id=p_action_id for update;
    if v_action.organization_id<>p_organization_id
       or v_action.action<>p_action
       or v_action.target_hold_id<>p_hold_id
       or v_action.scope<>v_scope
       or v_action.reason_code<>v_reason_code
       or v_action.expires_at is distinct from v_expires_at
       or v_action.requested_by<>p_actor_id then
        raise exception 'legal hold action idempotency conflict' using errcode='23505';
    end if;
    if v_inserted=1 then
        perform public.append_company_audit(
            p_organization_id,p_actor_id,'brevitas_admin',p_audit_request_id,
            'compliance.legal_hold.'||p_action||'_requested',
            'legal_hold_action',p_action_id::text,'committed'
        );
    end if;
    return to_jsonb(v_action)-'organization_id';
end;
$$;

create or replace function public.compliance_approve_legal_hold_action(
    p_organization_id uuid,
    p_action_id uuid,
    p_actor_id text,
    p_audit_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_action public.legal_hold_actions%rowtype;
    v_hold public.legal_holds%rowtype;
begin
    if public.compliance_actor_role(p_actor_id)<>'brevitas_admin' then
        raise exception 'legal hold approval requires a verified Brevitas administrator'
            using errcode='42501';
    end if;
    perform 1 from public.organizations
     where id=p_organization_id for update;
    if not found then raise exception 'organization not found' using errcode='P0002'; end if;
    select * into v_action from public.legal_hold_actions
     where id=p_action_id and organization_id=p_organization_id for update;
    if not found then raise exception 'legal hold action not found' using errcode='P0002'; end if;
    if v_action.requested_by=p_actor_id then
        raise exception 'two-person legal hold approval requires a distinct administrator'
            using errcode='42501';
    end if;
    if v_action.status='approved' then
        if v_action.approved_by is distinct from p_actor_id then
            raise exception 'legal hold action was approved by a different administrator'
                using errcode='42501';
        end if;
        if v_action.action='create' then
            perform 1 from public.legal_holds
             where id=v_action.target_hold_id
               and organization_id=v_action.organization_id
               and scope=v_action.scope and reason_code=v_action.reason_code
               and expires_at is not distinct from v_action.expires_at
               and created_by=v_action.requested_by;
        else
            perform 1 from public.legal_holds
             where id=v_action.target_hold_id
               and organization_id=v_action.organization_id
               and not active and released_by=v_action.approved_by;
        end if;
        if not found then
            raise exception 'approved legal hold action state mismatch' using errcode='55000';
        end if;
        return to_jsonb(v_action)-'organization_id';
    end if;
    if v_action.status<>'pending' then
        raise exception 'legal hold action is not approvable' using errcode='55000';
    end if;

    if v_action.action='create' then
        if v_action.expires_at is not null
           and v_action.expires_at<=clock_timestamp() then
            raise exception 'pending legal hold create request has expired' using errcode='55000';
        end if;
        insert into public.legal_holds(
            id,organization_id,scope,reason_code,created_by,expires_at
        ) values (
            v_action.target_hold_id,v_action.organization_id,v_action.scope,
            v_action.reason_code,v_action.requested_by,v_action.expires_at
        );
    else
        select * into v_hold from public.legal_holds
         where id=v_action.target_hold_id
           and organization_id=v_action.organization_id for update;
        if not found then raise exception 'legal hold not found' using errcode='P0002'; end if;
        if not v_hold.active
           or v_hold.scope<>v_action.scope
           or v_hold.reason_code<>v_action.reason_code
           or v_hold.expires_at is distinct from v_action.expires_at then
            raise exception 'legal hold release target changed after request'
                using errcode='55000';
        end if;
        update public.legal_holds
           set active=false,released_by=p_actor_id,released_at=clock_timestamp()
         where id=v_action.target_hold_id
           and organization_id=v_action.organization_id;
    end if;

    update public.legal_hold_actions
       set status='approved',approved_by=p_actor_id,approved_at=clock_timestamp()
     where id=p_action_id and organization_id=p_organization_id
     returning * into v_action;
    perform public.append_company_audit(
        p_organization_id,p_actor_id,'brevitas_admin',p_audit_request_id,
        case when v_action.action='create' then 'compliance.legal_hold.created'
             else 'compliance.legal_hold.released' end,
        'legal_hold',v_action.target_hold_id::text,'committed'
    );
    return to_jsonb(v_action)-'organization_id';
end;
$$;

create or replace function public.compliance_export_tenant(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns setof jsonb
language plpgsql
security definer
set search_path = public, auth, pg_temp
as $$
declare
    v_request public.data_subject_requests%rowtype;
    v_was_approved boolean;
begin
    perform public.compliance_actor_role(p_actor_id);
    perform public.compliance_assert_usage_export_schema();
    perform 1 from public.organizations where id = p_organization_id for update;
    if not found then raise exception 'organization not found' using errcode = 'P0002'; end if;
    select * into v_request from public.data_subject_requests
     where id = p_request_id and organization_id = p_organization_id for update;
    if not found or v_request.request_type <> 'export'
       or v_request.request_scope <> 'tenant' then
        raise exception 'approved tenant export request not found' using errcode = 'P0002';
    end if;
    if v_request.status = 'completed' then return; end if;
    if v_request.status not in ('approved', 'processing') then
        raise exception 'tenant export request is not approved' using errcode = '55000';
    end if;
    if public.compliance_request_has_hold(p_organization_id, 'export') then
        raise exception 'tenant export is blocked by legal hold' using errcode = '55000';
    end if;
    v_was_approved := v_request.status = 'approved';
    update public.data_subject_requests
       set status = 'processing', started_at = coalesce(started_at, clock_timestamp())
     where id = p_request_id and organization_id = p_organization_id;
    perform public.compliance_record_deadline_breach(v_request, p_actor_id);
    if v_was_approved then
        perform public.append_company_audit(
            p_organization_id, p_actor_id, public.compliance_actor_role(p_actor_id),
            p_request_id::text, 'compliance.export.started',
            'data_subject_request', p_request_id::text, 'committed'
        );
    end if;

    return query
        select jsonb_build_object('record_type', 'organization', 'data', jsonb_build_object(
            'id', organization.id, 'name', organization.name,
            'legacy_owner_id',organization.legacy_owner_id,
            'billing_owner_id',organization.billing_owner_id,
            'created_at', organization.created_at, 'cache_enabled', organization.cache_enabled
        )) from public.organizations organization where organization.id = p_organization_id;
    return query
        select jsonb_build_object('record_type', 'member', 'data', jsonb_build_object(
            'user_id', member.user_id, 'email', auth_user.email,
            'role', member.role, 'status', member.status, 'created_at', member.created_at,
            'updated_at',member.updated_at,'disabled_at',member.disabled_at,
            'removed_at',member.removed_at
        ))
          from public.organization_members member
          left join auth.users auth_user on auth_user.id = member.user_id
         where member.organization_id = p_organization_id
         order by member.user_id;
    return query
        select jsonb_build_object('record_type','member_identity_profile','data',jsonb_build_object(
            'user_id',auth_user.id,'email',auth_user.email,
            'phone',to_jsonb(auth_user)->'phone',
            'user_metadata',auth_user.raw_user_meta_data,
            'app_metadata',to_jsonb(auth_user)->'raw_app_meta_data',
            'email_change',to_jsonb(auth_user)->'email_change',
            'phone_change',to_jsonb(auth_user)->'phone_change',
            'created_at',auth_user.created_at,
            'updated_at',to_jsonb(auth_user)->'updated_at',
            'last_sign_in_at',to_jsonb(auth_user)->'last_sign_in_at',
            'email_confirmed_at',to_jsonb(auth_user)->'email_confirmed_at',
            'phone_confirmed_at',to_jsonb(auth_user)->'phone_confirmed_at',
            'confirmed_at',to_jsonb(auth_user)->'confirmed_at',
            'invited_at',to_jsonb(auth_user)->'invited_at'
        )) from auth.users auth_user join (
            select member.user_id from public.organization_members member
             where member.organization_id=p_organization_id
            union
            select organization.billing_owner_id from public.organizations organization
             where organization.id=p_organization_id and organization.billing_owner_id is not null
        ) tenant_identity on tenant_identity.user_id=auth_user.id order by auth_user.id;
    if to_regclass('public.profiles') is not null then
        return query execute
            'select jsonb_build_object(''record_type'',''member_application_profile'',''data'',to_jsonb(profile)) from public.profiles profile join (select member.user_id from public.organization_members member where member.organization_id=$1 union select organization.billing_owner_id from public.organizations organization where organization.id=$1 and organization.billing_owner_id is not null) tenant_identity on tenant_identity.user_id=profile.id order by profile.id'
            using p_organization_id;
    end if;
    if to_regclass('public.billing_events') is not null then
        return query execute
            'select jsonb_build_object(''record_type'',''legacy_billing_event'',''data'',to_jsonb(event)) from public.billing_events event join (select member.user_id from public.organization_members member where member.organization_id=$1 union select organization.billing_owner_id from public.organizations organization where organization.id=$1 and organization.billing_owner_id is not null) tenant_identity on tenant_identity.user_id=event.user_id order by event.ts,event.id'
            using p_organization_id;
    end if;
    if to_regclass('public.legal_acceptances') is not null then
        return query execute
            'select jsonb_build_object(''record_type'',''legal_acceptance'',''data'',to_jsonb(acceptance)) from public.legal_acceptances acceptance join (select member.user_id from public.organization_members member where member.organization_id=$1 union select organization.billing_owner_id from public.organizations organization where organization.id=$1 and organization.billing_owner_id is not null) tenant_identity on tenant_identity.user_id=acceptance.user_id order by acceptance.accepted_at,acceptance.user_id'
            using p_organization_id;
    end if;
    if to_regclass('auth.identities') is not null then
        return query execute
            'select jsonb_build_object(''record_type'',''authentication_identity'',''data'',jsonb_strip_nulls(jsonb_build_object(''id'',auth_identity.id,''user_id'',auth_identity.user_id,''provider'',to_jsonb(auth_identity)->''provider'',''identity_data'',to_jsonb(auth_identity)->''identity_data'',''last_sign_in_at'',to_jsonb(auth_identity)->''last_sign_in_at'',''created_at'',to_jsonb(auth_identity)->''created_at'',''updated_at'',to_jsonb(auth_identity)->''updated_at''))) from auth.identities auth_identity join (select member.user_id from public.organization_members member where member.organization_id=$1 union select organization.billing_owner_id from public.organizations organization where organization.id=$1 and organization.billing_owner_id is not null) tenant_identity on tenant_identity.user_id=auth_identity.user_id order by auth_identity.user_id,auth_identity.id'
            using p_organization_id;
    end if;
    if to_regclass('auth.sessions') is not null then
        return query execute
            'select jsonb_build_object(''record_type'',''authentication_session_metadata'',''data'',jsonb_strip_nulls(jsonb_build_object(''user_id'',auth_session.user_id,''factor_id'',to_jsonb(auth_session)->''factor_id'',''created_at'',to_jsonb(auth_session)->''created_at'',''updated_at'',to_jsonb(auth_session)->''updated_at'',''aal'',to_jsonb(auth_session)->''aal'',''not_after'',to_jsonb(auth_session)->''not_after'',''refreshed_at'',to_jsonb(auth_session)->''refreshed_at'',''user_agent'',to_jsonb(auth_session)->''user_agent'',''ip'',to_jsonb(auth_session)->''ip'',''tag'',to_jsonb(auth_session)->''tag''))) from auth.sessions auth_session join (select member.user_id from public.organization_members member where member.organization_id=$1 union select organization.billing_owner_id from public.organizations organization where organization.id=$1 and organization.billing_owner_id is not null) tenant_identity on tenant_identity.user_id=auth_session.user_id order by auth_session.user_id,to_jsonb(auth_session)->>''created_at'''
            using p_organization_id;
    end if;
    if to_regclass('auth.mfa_factors') is not null then
        return query execute
            'select jsonb_build_object(''record_type'',''authentication_factor_metadata'',''data'',jsonb_strip_nulls(jsonb_build_object(''id'',factor.id,''user_id'',factor.user_id,''factor_type'',to_jsonb(factor)->''factor_type'',''friendly_name'',to_jsonb(factor)->''friendly_name'',''status'',to_jsonb(factor)->''status'',''phone'',to_jsonb(factor)->''phone'',''created_at'',to_jsonb(factor)->''created_at'',''updated_at'',to_jsonb(factor)->''updated_at'',''last_challenged_at'',to_jsonb(factor)->''last_challenged_at''))) from auth.mfa_factors factor join (select member.user_id from public.organization_members member where member.organization_id=$1 union select organization.billing_owner_id from public.organizations organization where organization.id=$1 and organization.billing_owner_id is not null) tenant_identity on tenant_identity.user_id=factor.user_id order by factor.user_id,factor.id'
            using p_organization_id;
    end if;
    if to_regclass('auth.one_time_tokens') is not null then
        return query execute
            'select jsonb_build_object(''record_type'',''authentication_one_time_token_metadata'',''data'',jsonb_strip_nulls(jsonb_build_object(''user_id'',token.user_id,''token_type'',to_jsonb(token)->''token_type'',''relates_to'',to_jsonb(token)->''relates_to'',''created_at'',to_jsonb(token)->''created_at'',''updated_at'',to_jsonb(token)->''updated_at''))) from auth.one_time_tokens token join (select member.user_id from public.organization_members member where member.organization_id=$1 union select organization.billing_owner_id from public.organizations organization where organization.id=$1 and organization.billing_owner_id is not null) tenant_identity on tenant_identity.user_id=token.user_id order by token.user_id,to_jsonb(token)->>''created_at'''
            using p_organization_id;
    end if;
    return query
        select jsonb_build_object('record_type', 'customer', 'data', jsonb_build_object(
            'id', customer.id, 'external_id', customer.external_id,
            'display_name', customer.display_name, 'status', customer.status,
            'cache_enabled',customer.cache_enabled,'metadata', customer.metadata,
            'created_at', customer.created_at,
            'updated_at', customer.updated_at
        )) from public.customers customer
         where customer.organization_id = p_organization_id order by customer.id;
    return query
        select jsonb_build_object('record_type', 'service_account', 'data', jsonb_build_object(
            'id', account.id, 'name', account.name, 'environment', account.environment,
            'scopes', account.scopes, 'status', account.status,
            'created_by',account.created_by,
            'created_at', account.created_at, 'expires_at', account.expires_at,
            'revoked_at', account.revoked_at,'updated_at',account.updated_at
        )) from public.service_accounts account
         where account.organization_id = p_organization_id order by account.id;
    return query
        select jsonb_build_object('record_type', 'device', 'data', jsonb_build_object(
            'id', device.id, 'display_name', device.display_name,
            'device_fingerprint',device.device_fingerprint,
            'created_at', device.created_at, 'last_seen_at', device.last_seen_at,
            'revoked_at', device.revoked_at
        )) from public.devices device
         where device.organization_id = p_organization_id order by device.id;
    return query
        select jsonb_build_object('record_type', 'installation', 'data', jsonb_build_object(
            'id', installation.id, 'repository', installation.repository,
            'device_id',installation.device_id,
            'service_account_id',installation.service_account_id,
            'repository_id',installation.repository_id,
            'environment', installation.environment, 'device_platform', installation.device_platform,
            'device_arch', installation.device_arch, 'client_name', installation.client_name,
            'bvx_version', installation.bvx_version, 'installed_at', installation.installed_at,
            'last_seen_at', installation.last_seen_at, 'revoked_at', installation.revoked_at
        )) from public.installations installation
         where installation.organization_id = p_organization_id order by installation.id;
    return query
        select jsonb_build_object('record_type', 'usage_metadata', 'data', jsonb_build_object(
            'id', usage.id,'organization_id',usage.organization_id,
            'ts',usage.ts,'timestamp', usage.ts, 'project', usage.project,
            'environment', usage.environment, 'source', usage.source,
            'repo',usage.repo,'repository', usage.repo,
            'client', usage.client, 'agent', usage.agent, 'framework', usage.framework,
            'gateway', usage.gateway, 'operation', usage.operation, 'provider', usage.provider,
            'model', usage.model,'customer_id',usage.customer_id,'owner_id',usage.owner_id,
            'call_site_id',usage.call_site_id,'baseline_tokens', usage.baseline_tokens,
            'optimized_tokens', usage.optimized_tokens, 'tokens_saved', usage.tokens_saved,
            'savings_pct',usage.savings_pct,'fresh_input_tokens',usage.fresh_input_tokens,
            'cached_input_tokens',usage.cached_input_tokens,'cache_write_tokens',usage.cache_write_tokens,
            'cache_write_5m_tokens',to_jsonb(usage)->'cache_write_5m_tokens',
            'cache_write_1h_tokens',to_jsonb(usage)->'cache_write_1h_tokens',
            'cache_attributable',to_jsonb(usage)->'cache_attributable',
            'output_tokens',usage.output_tokens,'baseline_cost_usd',usage.baseline_cost_usd,
            'actual_cost_usd', usage.actual_cost_usd,
            'measured_savings_usd',usage.measured_savings_usd,
            'verified_savings_usd', usage.verified_savings_usd,
            'cost_saved_usd',usage.cost_saved_usd,'brevitas_fee_usd', usage.brevitas_fee_usd,
            'quality_proxy',usage.quality_proxy,'quality_status',usage.quality_status,
            'pricing_status',usage.pricing_status,'pricing_version',usage.pricing_version,
            'strategy',usage.strategy,'receipt_source',usage.receipt_source,
            'is_stream',usage.is_stream,'session_id',usage.session_id,
            'pipeline',usage.pipeline,'run_id',usage.run_id,'request_id',usage.request_id,
            'authoritative',usage.authoritative
        )) from public.usage_log usage
         where usage.organization_id = p_organization_id order by usage.id;
    return query
        select jsonb_build_object('record_type', 'billing_account', 'data', jsonb_build_object(
            'user_id',account.user_id,'stripe_customer_id',account.stripe_customer_id,
            'stripe_subscription_id',account.stripe_subscription_id,
            'subscription_status', account.subscription_status,
            'checkout_session_id',account.checkout_session_id,
            'billing_started_at', account.billing_started_at,
            'current_period_start', account.current_period_start,
            'current_period_end', account.current_period_end,
            'last_invoice_id',account.last_invoice_id,
            'last_invoice_status', account.last_invoice_status,
            'stripe_subscription_event_created',account.stripe_subscription_event_created,
            'stripe_invoice_event_created',account.stripe_invoice_event_created,
            'created_at', account.created_at, 'updated_at', account.updated_at
        ))
          from public.billing_accounts account
          join public.organizations organization on organization.billing_owner_id = account.user_id
         where organization.id = p_organization_id;
    return query
        select jsonb_build_object('record_type', 'billing_ledger', 'data', jsonb_build_object(
            'id', ledger.id,'usage_log_id',ledger.usage_log_id,'user_id',ledger.user_id,
            'occurred_at', ledger.occurred_at,
            'fee_microusd', ledger.fee_microusd, 'status', ledger.status,
            'attempts',ledger.attempts,'reported_at', ledger.reported_at,
            'last_error',ledger.last_error,'created_at', ledger.created_at
        ))
          from public.billing_ledger ledger
          join public.usage_log usage on usage.id = ledger.usage_log_id
         where usage.organization_id = p_organization_id order by ledger.id;
    return query
        select jsonb_build_object('record_type', 'administrative_audit', 'data', jsonb_build_object(
            'id', event.id, 'request_id', event.request_id, 'actor_id', event.actor_id,
            'actor_user_id',event.actor_user_id,'actor_role', event.actor_role, 'action', event.action,
            'target_type', event.target_type, 'target_id', event.target_id,
            'details',event.details,'outcome', event.outcome, 'occurred_at', event.occurred_at
        )) from public.audit_events event
         where event.organization_id = p_organization_id order by event.id;
    return query
        select jsonb_build_object('record_type','api_key_metadata','data',jsonb_build_object(
            'id',credential.id,'name',credential.name,'key_type',credential.key_type,
            'scopes',credential.scopes,'environment',credential.environment,
            'prefix',credential.key_prefix,'service_account_id',credential.service_account_id,
            'owner_id',credential.owner_id,'created_by',credential.created_by,
            'created_at',credential.created,
            'expires_at',credential.expires_at,'last_used_at',credential.last_used_at,
            'revoked_at',credential.revoked_at
        )) from public.api_keys credential
         where credential.organization_id=p_organization_id order by credential.created,credential.id;
    return query
        select jsonb_build_object('record_type','invitation_relationship','data',jsonb_build_object(
            'id',invitation.id,'role',invitation.role,'status',invitation.status,
            'invited_by',invitation.invited_by,'accepted_by',invitation.accepted_by,
            'created_at',invitation.created_at,'expires_at',invitation.expires_at,
            'accepted_at',invitation.accepted_at,'cancelled_at',invitation.cancelled_at
        )) from public.organization_invitations invitation
         where invitation.organization_id=p_organization_id order by invitation.created_at,invitation.id;
    return query
        select jsonb_build_object('record_type','data_rights_request','data',jsonb_build_object(
            'id',request.id,'request_type',request.request_type,'request_scope',request.request_scope,
            'subject_id',request.subject_id,'status',request.status,
            'evidence_reference',request.evidence_reference,
            'requested_at',request.requested_at,'due_at',request.due_at,
            'approved_at',request.approved_at,'approved_by',request.approved_by,
            'started_at',request.started_at,'completed_at',request.completed_at,
            'deadline_breached',request.deadline_breached,
            'deadline_breached_at',request.deadline_breached_at,
            'export_artifact_sha256',request.export_artifact_sha256,
            'export_attestation_sha256',request.export_attestation_sha256,
            'portable_record_count',request.portable_record_count,
            'portable_records_sha256',request.portable_records_sha256,
            'created_by',request.created_by
        )) from public.data_subject_requests request
         where request.organization_id=p_organization_id order by request.requested_at,request.id;
    if to_regclass('public.support_records') is not null then
        if to_regprocedure('public.compliance_export_support_records(uuid)') is null then
            raise exception 'support_records exists without its portable export adapter' using errcode='55000';
        end if;
        return query execute
            'select jsonb_build_object(''record_type'',''support_record'',''data'',record) from public.compliance_export_support_records($1) record'
            using p_organization_id;
    end if;
    return query
        select jsonb_build_object('record_type','device_authorization_metadata','data',jsonb_strip_nulls(jsonb_build_object(
            'device_hash',exchange.device_hash,'expires_at',exchange.expires_at,
            'approved_at',exchange.approved_at,'owner_id',nullif(exchange.owner_id,''),
            'organization_id',to_jsonb(exchange)->'organization_id',
            'quarantined_at',to_jsonb(exchange)->'quarantined_at'
        ))) from public.bvx_device_auth exchange
         where exchange.key_hash in (
            select credential.key_hash from public.api_keys credential
             where credential.organization_id=p_organization_id
         ) or exchange.owner_id in (
            select member.user_id::text from public.organization_members member
             where member.organization_id=p_organization_id
         );
    if to_regclass('public.bvx_device_consumption_receipts') is not null then
        return query execute
            'select jsonb_build_object(''record_type'',''device_delivery_metadata'',''data'',jsonb_build_object(''device_hash'',receipt.device_hash,''owner_id'',receipt.owner_id,''consumed_at'',receipt.consumed_at,''expires_at'',receipt.expires_at,''request_id'',receipt.request_id,''quarantined_at'',receipt.quarantined_at)) from public.bvx_device_consumption_receipts receipt where receipt.organization_id=$1 order by receipt.consumed_at'
            using p_organization_id;
    end if;
    if to_regclass('public.key_repositories') is not null then
        return query execute
            'select jsonb_build_object(''record_type'',''repository_relationship'',''data'',jsonb_build_object(''repository'',repository.repo,''source'',repository.source,''installed_at'',repository.installed_at,''last_seen_at'',repository.last_seen)) from public.key_repositories repository join public.api_keys credential on credential.key_hash=repository.key_hash where credential.organization_id=$1 order by repository.last_seen,repository.repo'
            using p_organization_id;
    end if;
    return query
        select jsonb_build_object('record_type','provider_configuration','data',jsonb_build_object(
            'key_id',credential.id,'provider',config.provider,'model',config.model,
            'credential_configured',false
        )) from public.provider_config config
          join public.api_keys credential on credential.key_hash=config.key_hash
         where credential.organization_id=p_organization_id and config.provider_api_key=''
         order by credential.id;
    return query
        select jsonb_build_object('record_type','encrypted_content','data',jsonb_build_object(
            'encryption_kind','application_envelope','ciphertext',config.provider_api_key,
            'context',jsonb_build_object('purpose','provider_credential','key_hash',config.key_hash),
            'output_record_type','provider_configuration',
            'content_field','provider_api_key',
            'metadata',jsonb_build_object('key_id',credential.id,'provider',config.provider,
                'model',config.model,'credential_configured',true)
        )) from public.provider_config config
          join public.api_keys credential on credential.key_hash=config.key_hash
         where credential.organization_id=p_organization_id and config.provider_api_key<>''
         order by credential.id;
    return query
        select jsonb_build_object('record_type','encrypted_content','data',jsonb_build_object(
            'encryption_kind','application_envelope','ciphertext',job.payload_ciphertext,
            'context',jsonb_build_object('purpose','durable_job','job_id',job.id::text,
                'organization_id',job.organization_id::text,'field','payload'),
            'output_record_type','ai_job_payload','content_field','payload',
            'metadata',jsonb_build_object('id',job.id,'customer_id',job.customer_id,
                'idempotency_key',job.idempotency_key,
                'operation',job.operation,'provider',job.provider,'model',job.model,
                'status',job.status,'attempts',job.attempts,'max_attempts',job.max_attempts,
                'available_at',job.available_at,'lease_owner',job.lease_owner,
                'lease_expires_at',job.lease_expires_at,
                'cancel_requested',job.cancel_requested,'last_error_code',job.last_error_code,
                'created_at',job.created_at,'updated_at',job.updated_at,
                'completed_at',job.completed_at,
                'expires_at',job.expires_at)
        )) from public.ai_jobs job where job.organization_id=p_organization_id order by job.id;
    return query
        select jsonb_build_object('record_type','encrypted_content','data',jsonb_build_object(
            'encryption_kind','application_envelope','ciphertext',job.result_ciphertext,
            'context',jsonb_build_object('purpose','durable_job','job_id',job.id::text,
                'organization_id',job.organization_id::text,'field','result'),
            'output_record_type','ai_job_result','content_field','result',
            'metadata',jsonb_build_object('id',job.id,'customer_id',job.customer_id,
                'idempotency_key',job.idempotency_key,
                'operation',job.operation,'provider',job.provider,'model',job.model,
                'status',job.status,'attempts',job.attempts,'max_attempts',job.max_attempts,
                'available_at',job.available_at,'lease_owner',job.lease_owner,
                'lease_expires_at',job.lease_expires_at,
                'cancel_requested',job.cancel_requested,'last_error_code',job.last_error_code,
                'created_at',job.created_at,'updated_at',job.updated_at,
                'completed_at',job.completed_at,'expires_at',job.expires_at)
        )) from public.ai_jobs job
         where job.organization_id=p_organization_id and job.result_ciphertext is not null
         order by job.id;
    return query
        select jsonb_build_object('record_type','encrypted_content','data',jsonb_build_object(
            'encryption_kind','semantic_cache','ciphertext',cache.response_ciphertext,
            'context',jsonb_build_object('purpose','semantic-response-cache',
                'tenant_namespace',cache.tenant_namespace,'exact_hash',cache.exact_hash,
                'model_identity',cache.model_id),
            'output_record_type','semantic_cache_content','content_field','response',
            'metadata',jsonb_build_object('exact_hash',cache.exact_hash,
                'context_hash',cache.context_hash,'tenant_namespace',cache.tenant_namespace,
                'model_id',cache.model_id,'embedding',to_jsonb(cache.embedding),
                'prompt_tokens',cache.prompt_tokens,'completion_tokens',cache.completion_tokens,
                'created_at',cache.created_at,'expires_at',cache.expires_at,
                'hit_count',cache.hit_count)
        )) from public.semantic_cache cache
         where cache.tenant_namespace=encode(digest(p_organization_id::text,'sha256'),'hex')
            or cache.tenant_namespace=encode(digest(p_organization_id::text||':unattributed','sha256'),'hex')
            or cache.tenant_namespace in (
                select encode(digest(p_organization_id::text||':'||customer.id::text,'sha256'),'hex')
                  from public.customers customer where customer.organization_id=p_organization_id
            )
         order by cache.created_at,cache.exact_hash;
end;
$$;

create or replace function public.compliance_export_subject(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns setof jsonb
language plpgsql
security definer
set search_path = public, auth, pg_temp
as $$
declare
    v_request public.data_subject_requests%rowtype;
    v_was_approved boolean;
begin
    perform public.compliance_actor_role(p_actor_id);
    perform public.compliance_assert_usage_export_schema();
    perform 1 from public.organizations where id = p_organization_id for update;
    if not found then raise exception 'organization not found' using errcode = 'P0002'; end if;
    select * into v_request from public.data_subject_requests
     where id = p_request_id and organization_id = p_organization_id for update;
    if not found or v_request.request_type <> 'export'
       or v_request.request_scope not in ('member', 'customer') then
        raise exception 'approved subject export request not found' using errcode = 'P0002';
    end if;
    if v_request.status = 'completed' then return; end if;
    if v_request.status not in ('approved', 'processing') then
        raise exception 'subject export request is not approved' using errcode = '55000';
    end if;
    if public.compliance_request_has_hold(p_organization_id, 'export') then
        raise exception 'subject export is blocked by legal hold' using errcode = '55000';
    end if;
    if v_request.request_scope = 'member' then
        perform 1 from public.organization_members
         where organization_id=p_organization_id and user_id=v_request.subject_id;
    else
        perform 1 from public.customers
         where organization_id=p_organization_id and id=v_request.subject_id;
    end if;
    if not found then raise exception 'subject not found in organization' using errcode='P0002'; end if;
    v_was_approved := v_request.status = 'approved';
    update public.data_subject_requests
       set status='processing', started_at=coalesce(started_at,clock_timestamp())
     where id=p_request_id and organization_id=p_organization_id;
    perform public.compliance_record_deadline_breach(v_request,p_actor_id);
    if v_was_approved then
        perform public.append_company_audit(
            p_organization_id,p_actor_id,public.compliance_actor_role(p_actor_id),
            p_request_id::text,'compliance.subject_export.started',
            v_request.request_scope,v_request.subject_id::text,'committed'
        );
    end if;
    if v_request.request_scope = 'member' then
        return query
            select jsonb_build_object('record_type','member','data',jsonb_build_object(
                'user_id',member.user_id,'email',auth_user.email,'role',member.role,
                'status',member.status,'created_at',member.created_at,
                'updated_at',member.updated_at,'disabled_at',member.disabled_at,
                'removed_at',member.removed_at
            ))
              from public.organization_members member
              join auth.users auth_user on auth_user.id=member.user_id
             where member.organization_id=p_organization_id
               and member.user_id=v_request.subject_id;
        return query
            select jsonb_build_object('record_type','member_identity_profile','data',jsonb_build_object(
                'user_id',auth_user.id,'email',auth_user.email,
                'phone',to_jsonb(auth_user)->'phone',
                'user_metadata',auth_user.raw_user_meta_data,
                'app_metadata',to_jsonb(auth_user)->'raw_app_meta_data',
                'email_change',to_jsonb(auth_user)->'email_change',
                'phone_change',to_jsonb(auth_user)->'phone_change',
                'created_at',auth_user.created_at,
                'updated_at',to_jsonb(auth_user)->'updated_at',
                'last_sign_in_at',to_jsonb(auth_user)->'last_sign_in_at',
                'email_confirmed_at',to_jsonb(auth_user)->'email_confirmed_at',
                'phone_confirmed_at',to_jsonb(auth_user)->'phone_confirmed_at',
                'confirmed_at',to_jsonb(auth_user)->'confirmed_at',
                'invited_at',to_jsonb(auth_user)->'invited_at'
            )) from auth.users auth_user where auth_user.id=v_request.subject_id;
        if to_regclass('public.profiles') is not null then
            return query execute
                'select jsonb_build_object(''record_type'',''member_application_profile'',''data'',to_jsonb(profile)) from public.profiles profile where profile.id=$1'
                using v_request.subject_id;
        end if;
        if to_regclass('public.billing_events') is not null then
            return query execute
                'select jsonb_build_object(''record_type'',''legacy_billing_event'',''data'',to_jsonb(event)) from public.billing_events event where event.user_id=$1 order by event.ts,event.id'
                using v_request.subject_id;
        end if;
        if to_regclass('public.legal_acceptances') is not null then
            return query execute
                'select jsonb_build_object(''record_type'',''legal_acceptance'',''data'',to_jsonb(acceptance)) from public.legal_acceptances acceptance where acceptance.user_id=$1 order by acceptance.accepted_at'
                using v_request.subject_id;
        end if;
        if to_regclass('auth.identities') is not null then
            return query execute
                'select jsonb_build_object(''record_type'',''authentication_identity'',''data'',jsonb_strip_nulls(jsonb_build_object(''id'',auth_identity.id,''user_id'',auth_identity.user_id,''provider'',to_jsonb(auth_identity)->''provider'',''identity_data'',to_jsonb(auth_identity)->''identity_data'',''last_sign_in_at'',to_jsonb(auth_identity)->''last_sign_in_at'',''created_at'',to_jsonb(auth_identity)->''created_at'',''updated_at'',to_jsonb(auth_identity)->''updated_at''))) from auth.identities auth_identity where auth_identity.user_id=$1 order by auth_identity.id'
                using v_request.subject_id;
        end if;
        if to_regclass('auth.sessions') is not null then
            return query execute
                'select jsonb_build_object(''record_type'',''authentication_session_metadata'',''data'',jsonb_strip_nulls(jsonb_build_object(''user_id'',auth_session.user_id,''factor_id'',to_jsonb(auth_session)->''factor_id'',''created_at'',to_jsonb(auth_session)->''created_at'',''updated_at'',to_jsonb(auth_session)->''updated_at'',''aal'',to_jsonb(auth_session)->''aal'',''not_after'',to_jsonb(auth_session)->''not_after'',''refreshed_at'',to_jsonb(auth_session)->''refreshed_at'',''user_agent'',to_jsonb(auth_session)->''user_agent'',''ip'',to_jsonb(auth_session)->''ip'',''tag'',to_jsonb(auth_session)->''tag''))) from auth.sessions auth_session where auth_session.user_id=$1 order by to_jsonb(auth_session)->>''created_at'''
                using v_request.subject_id;
        end if;
        if to_regclass('auth.mfa_factors') is not null then
            return query execute
                'select jsonb_build_object(''record_type'',''authentication_factor_metadata'',''data'',jsonb_strip_nulls(jsonb_build_object(''id'',factor.id,''user_id'',factor.user_id,''factor_type'',to_jsonb(factor)->''factor_type'',''friendly_name'',to_jsonb(factor)->''friendly_name'',''status'',to_jsonb(factor)->''status'',''phone'',to_jsonb(factor)->''phone'',''created_at'',to_jsonb(factor)->''created_at'',''updated_at'',to_jsonb(factor)->''updated_at'',''last_challenged_at'',to_jsonb(factor)->''last_challenged_at''))) from auth.mfa_factors factor where factor.user_id=$1 order by factor.id'
                using v_request.subject_id;
        end if;
        if to_regclass('auth.one_time_tokens') is not null then
            return query execute
                'select jsonb_build_object(''record_type'',''authentication_one_time_token_metadata'',''data'',jsonb_strip_nulls(jsonb_build_object(''user_id'',token.user_id,''token_type'',to_jsonb(token)->''token_type'',''relates_to'',to_jsonb(token)->''relates_to'',''created_at'',to_jsonb(token)->''created_at'',''updated_at'',to_jsonb(token)->''updated_at''))) from auth.one_time_tokens token where token.user_id=$1 order by to_jsonb(token)->>''created_at'''
                using v_request.subject_id;
        end if;
        return query
            select jsonb_build_object('record_type','organization_billing_relationship','data',jsonb_build_object(
                'organization_id',organization.id,'is_billing_owner',
                organization.billing_owner_id=v_request.subject_id
            )) from public.organizations organization where organization.id=p_organization_id;
        return query
            select jsonb_build_object('record_type','member_usage_metadata','data',jsonb_build_object(
                'id',usage.id,'organization_id',usage.organization_id,
                'ts',usage.ts,'timestamp',usage.ts,'customer_id',usage.customer_id,
                'owner_id',usage.owner_id,'project',usage.project,'environment',usage.environment,
                'source',usage.source,'repo',usage.repo,'repository',usage.repo,'client',usage.client,
                'agent',usage.agent,'call_site_id',usage.call_site_id,'framework',usage.framework,
                'gateway',usage.gateway,'operation',usage.operation,'provider',usage.provider,
                'model',usage.model,'baseline_tokens',usage.baseline_tokens,
                'optimized_tokens',usage.optimized_tokens,'tokens_saved',usage.tokens_saved,
                'savings_pct',usage.savings_pct,'fresh_input_tokens',usage.fresh_input_tokens,
                'cached_input_tokens',usage.cached_input_tokens,'cache_write_tokens',usage.cache_write_tokens,
                'cache_write_5m_tokens',to_jsonb(usage)->'cache_write_5m_tokens',
                'cache_write_1h_tokens',to_jsonb(usage)->'cache_write_1h_tokens',
                'cache_attributable',to_jsonb(usage)->'cache_attributable',
                'output_tokens',usage.output_tokens,'baseline_cost_usd',usage.baseline_cost_usd,
                'actual_cost_usd',usage.actual_cost_usd,'measured_savings_usd',usage.measured_savings_usd,
                'verified_savings_usd',usage.verified_savings_usd,'cost_saved_usd',usage.cost_saved_usd,
                'brevitas_fee_usd',usage.brevitas_fee_usd,'quality_proxy',usage.quality_proxy,
                'quality_status',usage.quality_status,'pricing_status',usage.pricing_status,
                'pricing_version',usage.pricing_version,'strategy',usage.strategy,
                'receipt_source',usage.receipt_source,'is_stream',usage.is_stream,
                'session_id',usage.session_id,'pipeline',usage.pipeline,'run_id',usage.run_id,
                'request_id',usage.request_id,
                'authoritative',usage.authoritative
            )) from public.usage_log usage
             where usage.organization_id=p_organization_id
               and usage.owner_id=v_request.subject_id::text order by usage.id;
        return query
            select jsonb_build_object('record_type','billing_account','data',jsonb_build_object(
                'user_id',account.user_id,'stripe_customer_id',account.stripe_customer_id,
                'stripe_subscription_id',account.stripe_subscription_id,
                'subscription_status',account.subscription_status,
                'checkout_session_id',account.checkout_session_id,
                'billing_started_at',account.billing_started_at,
                'current_period_start',account.current_period_start,
                'current_period_end',account.current_period_end,
                'last_invoice_id',account.last_invoice_id,
                'last_invoice_status',account.last_invoice_status,
                'stripe_subscription_event_created',account.stripe_subscription_event_created,
                'stripe_invoice_event_created',account.stripe_invoice_event_created,
                'created_at',account.created_at,'updated_at',account.updated_at
            )) from public.billing_accounts account where account.user_id=v_request.subject_id;
        return query
            select jsonb_build_object('record_type','billing_ledger','data',jsonb_build_object(
                'id',ledger.id,'usage_log_id',ledger.usage_log_id,'user_id',ledger.user_id,
                'occurred_at',ledger.occurred_at,'fee_microusd',ledger.fee_microusd,
                'status',ledger.status,'attempts',ledger.attempts,'reported_at',ledger.reported_at,
                'last_error',ledger.last_error,'created_at',ledger.created_at
            )) from public.billing_ledger ledger
             where ledger.user_id=v_request.subject_id order by ledger.id;
        return query
            select jsonb_build_object('record_type','api_key_metadata','data',jsonb_build_object(
                'id',credential.id,'name',credential.name,'key_type',credential.key_type,
                'scopes',credential.scopes,'environment',credential.environment,
                'prefix',credential.key_prefix,'service_account_id',credential.service_account_id,
                'owner_id',credential.owner_id,'created_by',credential.created_by,
                'created_at',credential.created,'expires_at',credential.expires_at,
                'last_used_at',credential.last_used_at,'revoked_at',credential.revoked_at
            )) from public.api_keys credential
             where credential.organization_id=p_organization_id
               and (credential.created_by=v_request.subject_id
                    or credential.owner_id=v_request.subject_id::text)
             order by credential.created,credential.id;
        return query
            select jsonb_build_object('record_type','service_account_creator_relationship','data',jsonb_build_object(
                'id',account.id,'name',account.name,'environment',account.environment,
                'scopes',account.scopes,'status',account.status,'created_by',account.created_by,
                'created_at',account.created_at,'expires_at',account.expires_at,
                'revoked_at',account.revoked_at,'updated_at',account.updated_at
            )) from public.service_accounts account
             where account.organization_id=p_organization_id
               and account.created_by=v_request.subject_id order by account.created_at,account.id;
        return query
            select jsonb_build_object('record_type','invitation_relationship','data',jsonb_build_object(
                'id',invitation.id,'role',invitation.role,'status',invitation.status,
                'relationship',case when invitation.invited_by=v_request.subject_id
                    then 'invited_by' else 'accepted_by' end,
                'created_at',invitation.created_at,'expires_at',invitation.expires_at,
                'accepted_at',invitation.accepted_at,'cancelled_at',invitation.cancelled_at
            )) from public.organization_invitations invitation
             where invitation.organization_id=p_organization_id
               and (invitation.invited_by=v_request.subject_id
                    or invitation.accepted_by=v_request.subject_id)
             order by invitation.created_at,invitation.id;
        return query
            select jsonb_build_object('record_type','administrative_audit_relationship','data',jsonb_build_object(
                'id',event.id,'request_id',event.request_id,'actor_role',event.actor_role,
                'actor_user_id',event.actor_user_id,
                'action',event.action,'target_type',event.target_type,'target_id',event.target_id,
                'details',event.details,'outcome',event.outcome,'occurred_at',event.occurred_at
            )) from public.audit_events event
             where event.organization_id=p_organization_id
               and (event.actor_user_id=v_request.subject_id
                    or event.actor_id=v_request.subject_id::text
                    or event.target_id=v_request.subject_id::text)
             order by event.id;
        return query
            select jsonb_build_object('record_type','data_rights_request','data',jsonb_build_object(
                'id',request.id,'request_type',request.request_type,'request_scope',request.request_scope,
                'subject_id',request.subject_id,'status',request.status,
                'evidence_reference',request.evidence_reference,
                'requested_at',request.requested_at,'due_at',request.due_at,
                'approved_at',request.approved_at,'approved_by',request.approved_by,
                'started_at',request.started_at,'completed_at',request.completed_at,
                'deadline_breached',request.deadline_breached,
                'deadline_breached_at',request.deadline_breached_at,
                'export_artifact_sha256',request.export_artifact_sha256,
                'export_attestation_sha256',request.export_attestation_sha256,
                'portable_record_count',request.portable_record_count,
                'portable_records_sha256',request.portable_records_sha256,
                'created_by',request.created_by
            )) from public.data_subject_requests request
             where request.organization_id=p_organization_id
               and request.subject_id=v_request.subject_id order by request.requested_at,request.id;
        return query
            select jsonb_build_object('record_type','device_authorization_metadata','data',jsonb_strip_nulls(jsonb_build_object(
                'device_hash',exchange.device_hash,'expires_at',exchange.expires_at,
                'approved_at',exchange.approved_at,'owner_id',nullif(exchange.owner_id,''),
                'organization_id',to_jsonb(exchange)->'organization_id',
                'quarantined_at',to_jsonb(exchange)->'quarantined_at'
            ))) from public.bvx_device_auth exchange
             where exchange.owner_id=v_request.subject_id::text;
        if to_regclass('public.bvx_device_consumption_receipts') is not null then
            return query execute
                'select jsonb_build_object(''record_type'',''device_delivery_metadata'',''data'',jsonb_build_object(''device_hash'',receipt.device_hash,''owner_id'',receipt.owner_id,''consumed_at'',receipt.consumed_at,''expires_at'',receipt.expires_at,''request_id'',receipt.request_id,''quarantined_at'',receipt.quarantined_at)) from public.bvx_device_consumption_receipts receipt where receipt.organization_id=$1 and receipt.owner_id=$2 order by receipt.consumed_at'
                using p_organization_id,v_request.subject_id;
        end if;
        if to_regclass('public.key_repositories') is not null then
            return query execute
                'select jsonb_build_object(''record_type'',''repository_relationship'',''data'',jsonb_build_object(''repository'',repository.repo,''source'',repository.source,''installed_at'',repository.installed_at,''last_seen_at'',repository.last_seen)) from public.key_repositories repository join public.api_keys credential on credential.key_hash=repository.key_hash where credential.organization_id=$1 and (credential.created_by=$2 or credential.owner_id=$2::text) order by repository.last_seen,repository.repo'
                using p_organization_id,v_request.subject_id;
        end if;
        return query
            select jsonb_build_object('record_type','provider_configuration','data',jsonb_build_object(
                'key_id',credential.id,'provider',config.provider,'model',config.model,
                'credential_configured',false
            )) from public.provider_config config
              join public.api_keys credential on credential.key_hash=config.key_hash
             where credential.organization_id=p_organization_id
               and (credential.created_by=v_request.subject_id
                    or credential.owner_id=v_request.subject_id::text)
               and config.provider_api_key='';
        return query
            select jsonb_build_object('record_type','encrypted_content','data',jsonb_build_object(
                'encryption_kind','application_envelope','ciphertext',config.provider_api_key,
                'context',jsonb_build_object('purpose','provider_credential','key_hash',config.key_hash),
                'output_record_type','provider_configuration','content_field','provider_api_key',
                'metadata',jsonb_build_object('key_id',credential.id,'provider',config.provider,
                    'model',config.model,'credential_configured',true)
            )) from public.provider_config config
              join public.api_keys credential on credential.key_hash=config.key_hash
             where credential.organization_id=p_organization_id
               and (credential.created_by=v_request.subject_id
                    or credential.owner_id=v_request.subject_id::text)
               and config.provider_api_key<>'';
        return query
            select jsonb_build_object('record_type','encrypted_content','data',jsonb_build_object(
                'encryption_kind','application_envelope','ciphertext',job.payload_ciphertext,
                'context',jsonb_build_object('purpose','durable_job','job_id',job.id::text,
                    'organization_id',job.organization_id::text,'field','payload'),
                'output_record_type','ai_job_payload','content_field','payload',
                'metadata',jsonb_build_object('id',job.id,'customer_id',job.customer_id,
                    'idempotency_key',job.idempotency_key,
                    'operation',job.operation,'provider',job.provider,'model',job.model,
                    'status',job.status,'attempts',job.attempts,'max_attempts',job.max_attempts,
                    'available_at',job.available_at,'lease_owner',job.lease_owner,
                    'lease_expires_at',job.lease_expires_at,
                    'cancel_requested',job.cancel_requested,'last_error_code',job.last_error_code,
                    'created_at',job.created_at,'updated_at',job.updated_at,
                    'completed_at',job.completed_at,'expires_at',job.expires_at)
            )) from public.ai_jobs job
             where job.organization_id=p_organization_id and job.key_hash in (
                select credential.key_hash from public.api_keys credential
                 where credential.organization_id=p_organization_id
                   and (credential.created_by=v_request.subject_id
                        or credential.owner_id=v_request.subject_id::text)
             ) order by job.id;
        return query
            select jsonb_build_object('record_type','encrypted_content','data',jsonb_build_object(
                'encryption_kind','application_envelope','ciphertext',job.result_ciphertext,
                'context',jsonb_build_object('purpose','durable_job','job_id',job.id::text,
                    'organization_id',job.organization_id::text,'field','result'),
                'output_record_type','ai_job_result','content_field','result',
                'metadata',jsonb_build_object('id',job.id,'customer_id',job.customer_id,
                    'idempotency_key',job.idempotency_key,
                    'operation',job.operation,'provider',job.provider,'model',job.model,
                    'status',job.status,'attempts',job.attempts,'max_attempts',job.max_attempts,
                    'available_at',job.available_at,'lease_owner',job.lease_owner,
                    'lease_expires_at',job.lease_expires_at,
                    'cancel_requested',job.cancel_requested,'last_error_code',job.last_error_code,
                    'created_at',job.created_at,'updated_at',job.updated_at,
                    'completed_at',job.completed_at,'expires_at',job.expires_at)
            )) from public.ai_jobs job
             where job.organization_id=p_organization_id and job.result_ciphertext is not null
               and job.key_hash in (
                select credential.key_hash from public.api_keys credential
                 where credential.organization_id=p_organization_id
                   and (credential.created_by=v_request.subject_id
                        or credential.owner_id=v_request.subject_id::text)
             ) order by job.id;
    else
        return query
            select jsonb_build_object('record_type','customer','data',jsonb_build_object(
                'id',customer.id,'external_id',customer.external_id,
                'display_name',customer.display_name,'status',customer.status,
                'cache_enabled',customer.cache_enabled,'metadata',customer.metadata,
                'created_at',customer.created_at,
                'updated_at',customer.updated_at
            )) from public.customers customer
             where customer.organization_id=p_organization_id and customer.id=v_request.subject_id;
        return query
            select jsonb_build_object('record_type','customer_usage_metadata','data',jsonb_build_object(
                'id',usage.id,'organization_id',usage.organization_id,
                'ts',usage.ts,'timestamp',usage.ts,'customer_id',usage.customer_id,
                'owner_id',usage.owner_id,'project',usage.project,'environment',usage.environment,
                'source',usage.source,'repo',usage.repo,'repository',usage.repo,'client',usage.client,
                'agent',usage.agent,'call_site_id',usage.call_site_id,'framework',usage.framework,
                'gateway',usage.gateway,'operation',usage.operation,'provider',usage.provider,
                'model',usage.model,'baseline_tokens',usage.baseline_tokens,
                'optimized_tokens',usage.optimized_tokens,'tokens_saved',usage.tokens_saved,
                'savings_pct',usage.savings_pct,'fresh_input_tokens',usage.fresh_input_tokens,
                'cached_input_tokens',usage.cached_input_tokens,'cache_write_tokens',usage.cache_write_tokens,
                'cache_write_5m_tokens',to_jsonb(usage)->'cache_write_5m_tokens',
                'cache_write_1h_tokens',to_jsonb(usage)->'cache_write_1h_tokens',
                'cache_attributable',to_jsonb(usage)->'cache_attributable',
                'output_tokens',usage.output_tokens,'baseline_cost_usd',usage.baseline_cost_usd,
                'actual_cost_usd',usage.actual_cost_usd,'measured_savings_usd',usage.measured_savings_usd,
                'verified_savings_usd',usage.verified_savings_usd,'cost_saved_usd',usage.cost_saved_usd,
                'brevitas_fee_usd',usage.brevitas_fee_usd,'quality_proxy',usage.quality_proxy,
                'quality_status',usage.quality_status,'pricing_status',usage.pricing_status,
                'pricing_version',usage.pricing_version,'strategy',usage.strategy,
                'receipt_source',usage.receipt_source,'is_stream',usage.is_stream,
                'session_id',usage.session_id,'pipeline',usage.pipeline,'run_id',usage.run_id,
                'request_id',usage.request_id,
                'authoritative',usage.authoritative
            )) from public.usage_log usage
             where usage.organization_id=p_organization_id
               and usage.customer_id=v_request.subject_id order by usage.id;
        return query
            select jsonb_build_object('record_type','administrative_audit_relationship','data',jsonb_build_object(
                'id',event.id,'request_id',event.request_id,'actor_role',event.actor_role,
                'actor_user_id',event.actor_user_id,
                'action',event.action,'target_type',event.target_type,'target_id',event.target_id,
                'details',event.details,'outcome',event.outcome,'occurred_at',event.occurred_at
            )) from public.audit_events event
             where event.organization_id=p_organization_id
               and event.target_id=v_request.subject_id::text order by event.id;
        return query
            select jsonb_build_object('record_type','data_rights_request','data',jsonb_build_object(
                'id',request.id,'request_type',request.request_type,'request_scope',request.request_scope,
                'subject_id',request.subject_id,'status',request.status,
                'evidence_reference',request.evidence_reference,
                'requested_at',request.requested_at,'due_at',request.due_at,
                'approved_at',request.approved_at,'approved_by',request.approved_by,
                'started_at',request.started_at,'completed_at',request.completed_at,
                'deadline_breached',request.deadline_breached,
                'deadline_breached_at',request.deadline_breached_at,
                'export_artifact_sha256',request.export_artifact_sha256,
                'export_attestation_sha256',request.export_attestation_sha256,
                'portable_record_count',request.portable_record_count,
                'portable_records_sha256',request.portable_records_sha256,
                'created_by',request.created_by
            )) from public.data_subject_requests request
             where request.organization_id=p_organization_id
               and request.subject_id=v_request.subject_id order by request.requested_at,request.id;
        return query
            select jsonb_build_object('record_type','encrypted_content','data',jsonb_build_object(
                'encryption_kind','application_envelope','ciphertext',job.payload_ciphertext,
                'context',jsonb_build_object('purpose','durable_job','job_id',job.id::text,
                    'organization_id',job.organization_id::text,'field','payload'),
                'output_record_type','ai_job_payload','content_field','payload',
                'metadata',jsonb_build_object('id',job.id,'customer_id',job.customer_id,
                    'idempotency_key',job.idempotency_key,
                    'operation',job.operation,'provider',job.provider,'model',job.model,
                    'status',job.status,'attempts',job.attempts,'max_attempts',job.max_attempts,
                    'available_at',job.available_at,'lease_owner',job.lease_owner,
                    'lease_expires_at',job.lease_expires_at,
                    'cancel_requested',job.cancel_requested,'last_error_code',job.last_error_code,
                    'created_at',job.created_at,'updated_at',job.updated_at,
                    'completed_at',job.completed_at,'expires_at',job.expires_at)
            )) from public.ai_jobs job
             where job.organization_id=p_organization_id
               and job.customer_id=v_request.subject_id order by job.id;
        return query
            select jsonb_build_object('record_type','encrypted_content','data',jsonb_build_object(
                'encryption_kind','application_envelope','ciphertext',job.result_ciphertext,
                'context',jsonb_build_object('purpose','durable_job','job_id',job.id::text,
                    'organization_id',job.organization_id::text,'field','result'),
                'output_record_type','ai_job_result','content_field','result',
                'metadata',jsonb_build_object('id',job.id,'customer_id',job.customer_id,
                    'idempotency_key',job.idempotency_key,
                    'operation',job.operation,'provider',job.provider,'model',job.model,
                    'status',job.status,'attempts',job.attempts,'max_attempts',job.max_attempts,
                    'available_at',job.available_at,'lease_owner',job.lease_owner,
                    'lease_expires_at',job.lease_expires_at,
                    'cancel_requested',job.cancel_requested,'last_error_code',job.last_error_code,
                    'created_at',job.created_at,'updated_at',job.updated_at,
                    'completed_at',job.completed_at,'expires_at',job.expires_at)
            )) from public.ai_jobs job
             where job.organization_id=p_organization_id and job.customer_id=v_request.subject_id
               and job.result_ciphertext is not null order by job.id;
        return query
            select jsonb_build_object('record_type','encrypted_content','data',jsonb_build_object(
                'encryption_kind','semantic_cache','ciphertext',cache.response_ciphertext,
                'context',jsonb_build_object('purpose','semantic-response-cache',
                    'tenant_namespace',cache.tenant_namespace,'exact_hash',cache.exact_hash,
                    'model_identity',cache.model_id),
                'output_record_type','semantic_cache_content','content_field','response',
                'metadata',jsonb_build_object('exact_hash',cache.exact_hash,
                    'context_hash',cache.context_hash,'tenant_namespace',cache.tenant_namespace,
                    'model_id',cache.model_id,'embedding',to_jsonb(cache.embedding),
                    'prompt_tokens',cache.prompt_tokens,'completion_tokens',cache.completion_tokens,
                    'created_at',cache.created_at,'expires_at',cache.expires_at,
                    'hit_count',cache.hit_count)
            )) from public.semantic_cache cache
             where cache.tenant_namespace=encode(
                digest(p_organization_id::text||':'||v_request.subject_id::text,'sha256'),'hex'
             ) order by cache.created_at,cache.exact_hash;
    end if;
    if to_regclass('public.support_records') is not null then
        if to_regprocedure('public.compliance_export_support_subject(uuid,text,uuid)') is null then
            raise exception 'support_records exists without its subject export adapter' using errcode='55000';
        end if;
        return query execute
            'select jsonb_build_object(''record_type'',''support_record'',''data'',record) from public.compliance_export_support_subject($1,$2,$3) record'
            using p_organization_id,v_request.request_scope,v_request.subject_id;
    end if;
end;
$$;

create or replace function public.compliance_complete_export(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text,
    p_artifact_sha256 text,
    p_attestation_sha256 text,
    p_portable_record_count integer,
    p_portable_records_sha256 text
) returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_request public.data_subject_requests%rowtype;
begin
    perform public.compliance_actor_role(p_actor_id);
    if p_artifact_sha256 is null
       or p_attestation_sha256 is null
       or p_portable_records_sha256 is null
       or p_portable_record_count is null
       or p_artifact_sha256 !~ '^[0-9a-f]{64}$'
       or p_attestation_sha256 !~ '^[0-9a-f]{64}$'
       or p_portable_records_sha256 !~ '^[0-9a-f]{64}$'
       or p_portable_record_count not between 0 and 10000000 then
        raise exception 'invalid portable export proof' using errcode = '22023';
    end if;
    perform 1 from public.organizations where id = p_organization_id for update;
    if not found then raise exception 'organization not found' using errcode = 'P0002'; end if;
    select * into v_request from public.data_subject_requests
     where id = p_request_id and organization_id = p_organization_id for update;
    if not found or v_request.request_type <> 'export' then
        raise exception 'tenant export request not found' using errcode = 'P0002';
    end if;
    if v_request.status = 'completed' then
        if v_request.export_artifact_sha256 <> p_artifact_sha256
           or v_request.export_attestation_sha256 <> p_attestation_sha256
           or v_request.portable_record_count <> p_portable_record_count
           or v_request.portable_records_sha256 <> p_portable_records_sha256 then
            raise exception 'export finalize idempotency conflict' using errcode = '23505';
        end if;
        return 'completed';
    end if;
    if v_request.status <> 'processing' then
        raise exception 'tenant export was not started' using errcode = '55000';
    end if;
    if public.compliance_request_has_hold(p_organization_id, 'export') then
        raise exception 'tenant export is blocked by legal hold' using errcode = '55000';
    end if;
    perform public.compliance_record_deadline_breach(v_request, p_actor_id);
    update public.data_subject_requests
       set status = 'completed', completed_at = clock_timestamp(),
           export_artifact_sha256 = p_artifact_sha256,
           export_attestation_sha256 = p_attestation_sha256,
           portable_record_count = p_portable_record_count,
           portable_records_sha256 = p_portable_records_sha256
     where id = p_request_id and organization_id = p_organization_id;
    perform public.append_company_audit(
        p_organization_id, p_actor_id, public.compliance_actor_role(p_actor_id),
        p_request_id::text, 'compliance.export.completed',
        'data_subject_request', p_request_id::text, 'committed'
    );
    return 'completed';
end;
$$;

create or replace function public.compliance_delete_tenant(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_request public.data_subject_requests%rowtype;
    v_key_hashes text[];
    v_member_ids uuid[];
    v_member_id uuid;
    v_billing_before bigint;
    v_billing_after bigint;
    v_was_approved boolean;
begin
    perform public.compliance_actor_role(p_actor_id);
    perform 1 from public.organizations where id = p_organization_id for update;
    if not found then raise exception 'organization not found' using errcode = 'P0002'; end if;
    select * into v_request from public.data_subject_requests
     where id = p_request_id and organization_id = p_organization_id for update;
    if not found or v_request.request_type <> 'delete'
       or v_request.request_scope <> 'tenant' then
        raise exception 'approved tenant deletion request not found' using errcode = 'P0002';
    end if;
    if v_request.status = 'completed' then
        if not exists (select 1 from public.backup_deletion_tombstones
                        where request_id = p_request_id and organization_id = p_organization_id) then
            raise exception 'completed deletion is missing its tombstone' using errcode = '55000';
        end if;
        return 'completed';
    end if;
    if v_request.status not in ('approved', 'processing') then
        raise exception 'tenant deletion request is not approved' using errcode = '55000';
    end if;
    if public.compliance_request_has_hold(p_organization_id, 'delete') then
        raise exception 'tenant deletion is blocked by legal hold' using errcode = '55000';
    end if;
    perform public.compliance_erase_support_records(p_organization_id,'tenant',null);
    v_was_approved := v_request.status = 'approved';
    update public.data_subject_requests
       set status = 'processing', started_at = coalesce(started_at, clock_timestamp())
     where id = p_request_id and organization_id = p_organization_id;
    perform public.compliance_record_deadline_breach(v_request, p_actor_id);
    if v_was_approved then
        perform public.append_company_audit(
            p_organization_id, p_actor_id, public.compliance_actor_role(p_actor_id),
            p_request_id::text, 'compliance.delete.started',
            'data_subject_request', p_request_id::text, 'committed'
        );
    end if;

    select coalesce(array_agg(key_hash), array[]::text[]) into v_key_hashes
      from public.api_keys where organization_id = p_organization_id;
    select coalesce(array_agg(distinct identity_id), array[]::uuid[]) into v_member_ids
      from (
        select user_id as identity_id from public.organization_members
         where organization_id=p_organization_id
        union all
        select billing_owner_id from public.organizations
         where id=p_organization_id and billing_owner_id is not null
      ) identities;
    select count(*) into v_billing_before
      from public.billing_ledger ledger
      join public.usage_log usage on usage.id = ledger.usage_log_id
     where usage.organization_id = p_organization_id;

    -- Revoke access before removing configuration. All statements share this
    -- function transaction; any failure rolls back the entire deletion.
    update public.api_keys set revoked_at = coalesce(revoked_at, clock_timestamp())
     where organization_id = p_organization_id;
    update public.service_accounts
       set status = 'revoked', revoked_at = coalesce(revoked_at, clock_timestamp()),
           updated_at = clock_timestamp()
     where organization_id = p_organization_id;

    delete from public.bvx_device_auth
     where key_hash = any(v_key_hashes)
        or owner_id in (select member_id::text from unnest(v_member_ids) member_id);
    delete from public.provider_config where key_hash = any(v_key_hashes);
    delete from public.ai_jobs where organization_id = p_organization_id;
    delete from public.semantic_cache cache
     where cache.tenant_namespace = encode(digest(p_organization_id::text, 'sha256'), 'hex')
        or cache.tenant_namespace = encode(
            digest(p_organization_id::text || ':unattributed', 'sha256'), 'hex'
        )
        or cache.tenant_namespace in (
            select encode(digest(p_organization_id::text || ':' || customer.id::text, 'sha256'), 'hex')
              from public.customers customer
             where customer.organization_id = p_organization_id
        );

    -- Keep only usage rows required by the immutable financial ledger. They are
    -- minimized to content-free financial evidence; billing/tax retention is
    -- seven years subject to counsel. Non-ledger usage follows the shorter policy.
    delete from public.usage_log usage
     where usage.organization_id = p_organization_id
       and not exists (select 1 from public.billing_ledger ledger
                        where ledger.usage_log_id = usage.id);
    update public.usage_log usage
       set customer_id = null,
           key_hash = 'deleted-' || p_organization_id::text,
           owner_id = '', project = 'Deleted', environment = 'Deleted', source = 'Deleted',
           repo = '', client = '', agent = '', call_site_id = '', framework = '', gateway = '',
           provider = '', model = '', session_id = '', pipeline = '', run_id = '', request_id = '',
           usage_raw = ''
     where usage.organization_id = p_organization_id;

    if to_regclass('public.key_repositories') is not null then
        execute 'delete from public.key_repositories where key_hash = any($1)'
          using v_key_hashes;
    end if;
    if to_regclass('public.bvx_device_consumption_receipts') is not null then
        execute 'delete from public.bvx_device_consumption_receipts where organization_id=$1'
          using p_organization_id;
    end if;
    delete from public.api_keys where organization_id = p_organization_id;
    delete from public.installations where organization_id = p_organization_id;
    delete from public.devices where organization_id = p_organization_id;
    delete from public.service_accounts where organization_id = p_organization_id;
    delete from public.organization_invitations where organization_id = p_organization_id;
    delete from public.organization_members where organization_id = p_organization_id;
    foreach v_member_id in array v_member_ids loop
        perform public.compliance_anonymize_unshared_user(v_member_id);
    end loop;
    delete from public.customers where organization_id = p_organization_id;
    update public.organizations
       set name = 'Deleted organization', legacy_owner_id = null,
           billing_owner_id = null, cache_enabled = false
     where id = p_organization_id;

    select count(*) into v_billing_after
      from public.billing_ledger ledger
      join public.usage_log usage on usage.id = ledger.usage_log_id
     where usage.organization_id = p_organization_id;
    if v_billing_after <> v_billing_before then
        raise exception 'financial preservation invariant failed' using errcode = '55000';
    end if;

    insert into public.backup_deletion_tombstones(
        request_id, organization_id, request_received_at, expires_at
    ) values (
        p_request_id, p_organization_id, v_request.requested_at,
        v_request.requested_at + interval '35 days'
    ) on conflict (request_id) do nothing;
    update public.data_subject_requests
       set status = 'completed', completed_at = clock_timestamp()
     where id = p_request_id and organization_id = p_organization_id;
    perform public.append_company_audit(
        p_organization_id, p_actor_id, public.compliance_actor_role(p_actor_id),
        p_request_id::text, 'compliance.delete.completed',
        'data_subject_request', p_request_id::text, 'committed'
    );
    return 'completed';
end;
$$;

create or replace function public.compliance_delete_subject(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_request public.data_subject_requests%rowtype;
    v_key_hashes text[] := array[]::text[];
    v_billing_before bigint;
    v_billing_after bigint;
    v_was_approved boolean;
begin
    perform public.compliance_actor_role(p_actor_id);
    perform 1 from public.organizations where id=p_organization_id for update;
    if not found then raise exception 'organization not found' using errcode='P0002'; end if;
    select * into v_request from public.data_subject_requests
     where id=p_request_id and organization_id=p_organization_id for update;
    if not found or v_request.request_type<>'delete'
       or v_request.request_scope not in ('member','customer') then
        raise exception 'approved subject deletion request not found' using errcode='P0002';
    end if;
    if v_request.status='completed' then
        if not exists (select 1 from public.backup_deletion_tombstones
                        where request_id=p_request_id and organization_id=p_organization_id) then
            raise exception 'completed subject deletion is missing its tombstone' using errcode='55000';
        end if;
        return 'completed';
    end if;
    if v_request.status not in ('approved','processing') then
        raise exception 'subject deletion request is not approved' using errcode='55000';
    end if;
    if public.compliance_request_has_hold(p_organization_id,'delete') then
        raise exception 'subject deletion is blocked by legal hold' using errcode='55000';
    end if;
    perform public.compliance_erase_support_records(
        p_organization_id,v_request.request_scope,v_request.subject_id);
    if v_request.request_scope='member' then
        perform 1 from public.organization_members
         where organization_id=p_organization_id and user_id=v_request.subject_id for update;
    else
        perform 1 from public.customers
         where organization_id=p_organization_id and id=v_request.subject_id for update;
    end if;
    if not found then raise exception 'subject not found in organization' using errcode='P0002'; end if;
    v_was_approved := v_request.status='approved';
    update public.data_subject_requests
       set status='processing',started_at=coalesce(started_at,clock_timestamp())
     where id=p_request_id and organization_id=p_organization_id;
    perform public.compliance_record_deadline_breach(v_request,p_actor_id);
    if v_was_approved then
        perform public.append_company_audit(
            p_organization_id,p_actor_id,public.compliance_actor_role(p_actor_id),
            p_request_id::text,'compliance.subject_delete.started',
            v_request.request_scope,v_request.subject_id::text,'committed'
        );
    end if;
    select count(*) into v_billing_before from public.billing_ledger ledger
      join public.usage_log usage on usage.id=ledger.usage_log_id
     where usage.organization_id=p_organization_id;

    if v_request.request_scope='member' then
        select coalesce(array_agg(key_hash),array[]::text[]) into v_key_hashes
          from public.api_keys
         where organization_id=p_organization_id
           and owner_id=v_request.subject_id::text
           and key_type in ('dashboard_session','device');
        delete from public.ai_jobs where key_hash=any(v_key_hashes);
        delete from public.bvx_device_auth
         where key_hash=any(v_key_hashes) or owner_id=v_request.subject_id::text;
        delete from public.provider_config where key_hash=any(v_key_hashes);
        if to_regclass('public.key_repositories') is not null then
            execute 'delete from public.key_repositories where key_hash=any($1)'
              using v_key_hashes;
        end if;
        if to_regclass('public.bvx_device_consumption_receipts') is not null then
            execute 'delete from public.bvx_device_consumption_receipts where organization_id=$1 and owner_id=$2'
              using p_organization_id,v_request.subject_id;
        end if;
        delete from public.api_keys where key_hash=any(v_key_hashes);
        update public.api_keys set owner_id='',created_by=null
         where organization_id=p_organization_id
           and owner_id=v_request.subject_id::text;
        update public.service_accounts set created_by=null,updated_at=clock_timestamp()
         where organization_id=p_organization_id and created_by=v_request.subject_id;
        update public.organization_invitations set accepted_by=null
         where organization_id=p_organization_id and accepted_by=v_request.subject_id
           and invited_by<>v_request.subject_id;
        delete from public.organization_invitations
         where organization_id=p_organization_id and invited_by=v_request.subject_id;
        delete from public.usage_log usage
         where usage.organization_id=p_organization_id
           and usage.owner_id=v_request.subject_id::text
           and not exists (select 1 from public.billing_ledger ledger
                            where ledger.usage_log_id=usage.id);
        update public.usage_log usage
           set owner_id='',customer_id=null,key_hash='deleted-'||p_organization_id::text,
               project='Deleted',environment='Deleted',source='Deleted',repo='',client='',agent='',
               call_site_id='',framework='',gateway='',provider='',model='',session_id='',
               pipeline='',run_id='',request_id='',usage_raw=''
         where usage.organization_id=p_organization_id
           and usage.owner_id=v_request.subject_id::text;
        delete from public.organization_members
         where organization_id=p_organization_id and user_id=v_request.subject_id;
        update public.organizations set billing_owner_id=null
         where id=p_organization_id and billing_owner_id=v_request.subject_id;
        perform public.compliance_anonymize_unshared_user(v_request.subject_id);
    else
        delete from public.ai_jobs
         where organization_id=p_organization_id and customer_id=v_request.subject_id;
        delete from public.semantic_cache
         where tenant_namespace=encode(
             digest(p_organization_id::text||':'||v_request.subject_id::text,'sha256'),'hex'
         );
        delete from public.usage_log usage
         where usage.organization_id=p_organization_id
           and usage.customer_id=v_request.subject_id
           and not exists (select 1 from public.billing_ledger ledger
                            where ledger.usage_log_id=usage.id);
        update public.usage_log usage
           set customer_id=null,key_hash='deleted-'||p_organization_id::text,
               owner_id='',project='Deleted',environment='Deleted',source='Deleted',
               repo='',client='',agent='',call_site_id='',framework='',gateway='',
               provider='',model='',session_id='',pipeline='',run_id='',request_id='',usage_raw=''
         where usage.organization_id=p_organization_id
           and usage.customer_id=v_request.subject_id;
        delete from public.customers
         where organization_id=p_organization_id and id=v_request.subject_id;
    end if;

    select count(*) into v_billing_after from public.billing_ledger ledger
      join public.usage_log usage on usage.id=ledger.usage_log_id
     where usage.organization_id=p_organization_id;
    if v_billing_after <> v_billing_before then
        raise exception 'subject financial preservation invariant failed' using errcode='55000';
    end if;
    insert into public.backup_deletion_tombstones(
        request_id,organization_id,request_received_at,expires_at
    ) values (
        p_request_id,p_organization_id,v_request.requested_at,
        v_request.requested_at+interval '35 days'
    ) on conflict (request_id) do nothing;
    update public.data_subject_requests
       set status='completed',completed_at=clock_timestamp()
     where id=p_request_id and organization_id=p_organization_id;
    perform public.append_company_audit(
        p_organization_id,p_actor_id,public.compliance_actor_role(p_actor_id),
        p_request_id::text,'compliance.subject_delete.completed',
        v_request.request_scope,v_request.subject_id::text,'committed'
    );
    return 'completed';
end;
$$;

-- Replay a completed deletion only inside an explicitly bootstrapped isolated
-- restore target. Production has no brevitas_restore.control table, so this RPC
-- fails closed there even for service_role. The independently protected
-- artifact hash/reference must match the bootstrap control record.
create or replace function public.compliance_replay_deletion_tombstone(
    p_backup_source_id text,
    p_organization_id uuid,
    p_request_id uuid,
    p_requested_at timestamptz,
    p_expires_at timestamptz,
    p_request_scope text,
    p_subject_id uuid,
    p_actor_id text,
    p_evidence_reference text,
    p_artifact_sha256 text
) returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_control_source text;
    v_control_artifact text;
    v_control_evidence text;
    v_raw_verified_at timestamptz;
    v_ready_at timestamptz;
    v_request public.data_subject_requests%rowtype;
    v_result text;
    v_existing_result text;
begin
    perform public.compliance_actor_role(p_actor_id);
    if p_actor_id not like 'system:restore%'
       or p_request_scope not in ('tenant','member','customer')
       or (p_request_scope='tenant' and p_subject_id is not null)
       or (p_request_scope<>'tenant' and p_subject_id is null)
       or p_expires_at <> p_requested_at+interval '35 days'
       or p_artifact_sha256 !~ '^[0-9a-f]{64}$'
       or to_regclass('brevitas_restore.control') is null
       or to_regclass('brevitas_restore.replay_evidence') is null then
        raise exception 'restore replay contract is unavailable or invalid' using errcode='55000';
    end if;
    execute 'select backup_source_id,deletion_artifact_sha256,deletion_evidence_reference,raw_verified_at,ready_at from brevitas_restore.control where singleton'
      into v_control_source,v_control_artifact,v_control_evidence,v_raw_verified_at,v_ready_at;
    if v_control_source is distinct from p_backup_source_id
       or v_control_artifact is distinct from p_artifact_sha256
       or v_control_evidence is distinct from p_evidence_reference
       or v_raw_verified_at is null
       or v_ready_at is not null then
        raise exception 'restore replay is not bound to bootstrap evidence' using errcode='55000';
    end if;
    execute 'select result from brevitas_restore.replay_evidence where request_id=$1 and artifact_sha256=$2'
      into v_existing_result using p_request_id,p_artifact_sha256;
    if v_existing_result is not null then return v_existing_result; end if;
    if not exists (select 1 from public.organizations where id=p_organization_id) then
        execute 'insert into brevitas_restore.replay_evidence(request_id,organization_id,request_scope,subject_id,artifact_sha256,result) values($1,$2,$3,$4,$5,$6) on conflict(request_id) do nothing'
          using p_request_id,p_organization_id,p_request_scope,p_subject_id,p_artifact_sha256,'already_absent';
        return 'already_absent';
    end if;
    perform 1 from public.organizations where id=p_organization_id for update;
    -- A completed authoritative tombstone is newer than this backup. Any active
    -- hold in the older snapshot is stale relative to that deletion proof.
    update public.legal_holds
       set active=false,released_by=p_actor_id,released_at=clock_timestamp()
     where organization_id=p_organization_id and active;
    insert into public.data_subject_requests(
        id,organization_id,request_type,request_scope,subject_id,status,
        evidence_reference,requested_at,due_at,approved_at,approved_by,created_by
    ) values (
        p_request_id,p_organization_id,'delete',p_request_scope,p_subject_id,'approved',
        p_evidence_reference,p_requested_at,p_requested_at+interval '30 days',
        clock_timestamp(),p_actor_id,p_actor_id
    ) on conflict(id) do nothing;
    select * into v_request from public.data_subject_requests where id=p_request_id for update;
    if v_request.organization_id<>p_organization_id or v_request.request_type<>'delete'
       or v_request.request_scope<>p_request_scope
       or v_request.subject_id is distinct from p_subject_id then
        raise exception 'restore tombstone request identity conflict' using errcode='23505';
    end if;
    if (p_request_scope='member' and not exists (
        select 1 from public.organization_members
         where organization_id=p_organization_id and user_id=p_subject_id
    )) or (p_request_scope='customer' and not exists (
        select 1 from public.customers
         where organization_id=p_organization_id and id=p_subject_id
    )) then
        insert into public.backup_deletion_tombstones(
            request_id,organization_id,request_received_at,expires_at
        ) values (p_request_id,p_organization_id,p_requested_at,p_expires_at)
        on conflict(request_id) do nothing;
        update public.data_subject_requests
           set status='completed',completed_at=coalesce(completed_at,clock_timestamp()),
               deadline_breached=clock_timestamp()>due_at,
               deadline_breached_at=case when clock_timestamp()>due_at
                    then coalesce(deadline_breached_at,clock_timestamp()) else deadline_breached_at end
         where id=p_request_id;
        v_result := 'already_absent';
    elsif p_request_scope='tenant' then
        v_result := public.compliance_delete_tenant(p_organization_id,p_request_id,p_actor_id);
    else
        v_result := public.compliance_delete_subject(p_organization_id,p_request_id,p_actor_id);
    end if;
    if not exists (
        select 1 from public.backup_deletion_tombstones
         where request_id=p_request_id and organization_id=p_organization_id
           and expires_at=p_expires_at
    ) then raise exception 'restore tombstone replay verification failed' using errcode='55000'; end if;
    perform public.append_company_audit(
        p_organization_id,p_actor_id,public.compliance_actor_role(p_actor_id),
        p_request_id::text,'compliance.restore_tombstone.replayed',
        'data_subject_request',p_request_id::text,'committed'
    );
    execute 'insert into brevitas_restore.replay_evidence(request_id,organization_id,request_scope,subject_id,artifact_sha256,result) values($1,$2,$3,$4,$5,$6) on conflict(request_id) do nothing'
      using p_request_id,p_organization_id,p_request_scope,p_subject_id,p_artifact_sha256,v_result;
    return v_result;
end;
$$;

-- Functions are EXECUTE-denied by default; only the server-side service role
-- may invoke workflow operations. Browser roles receive no table policies.
revoke all on function public.compliance_submit_data_request(uuid,uuid,text,text,text)
    from public, anon, authenticated;
revoke all on function public.compliance_submit_subject_request(uuid,uuid,text,text,uuid,text,text)
    from public, anon, authenticated;
revoke all on function public.compliance_approve_data_request(uuid,uuid,text)
    from public, anon, authenticated;
revoke all on function public.compliance_request_legal_hold_action(uuid,uuid,text,uuid,text,text,text,text,timestamptz)
    from public, anon, authenticated;
revoke all on function public.compliance_approve_legal_hold_action(uuid,uuid,text,text)
    from public, anon, authenticated;
revoke all on function public.compliance_export_tenant(uuid,uuid,text)
    from public, anon, authenticated;
revoke all on function public.compliance_export_subject(uuid,uuid,text)
    from public, anon, authenticated;
revoke all on function public.compliance_complete_export(uuid,uuid,text,text,text,integer,text)
    from public, anon, authenticated;
revoke all on function public.compliance_delete_tenant(uuid,uuid,text)
    from public, anon, authenticated;
revoke all on function public.compliance_delete_subject(uuid,uuid,text)
    from public, anon, authenticated;
revoke all on function public.compliance_replay_deletion_tombstone(text,uuid,uuid,timestamptz,timestamptz,text,uuid,text,text,text)
    from public, anon, authenticated;
revoke all on function public.compliance_run_retention(uuid,text,integer,boolean)
    from public, anon, authenticated;
revoke all on function public.compliance_retention_worker_cycle(uuid,uuid,uuid,uuid,text,text,integer)
    from public, anon, authenticated;
revoke all on function public.compliance_retention_worker_health()
    from public, anon, authenticated;
grant execute on function public.compliance_submit_data_request(uuid,uuid,text,text,text)
    to service_role;
grant execute on function public.compliance_submit_subject_request(uuid,uuid,text,text,uuid,text,text)
    to service_role;
grant execute on function public.compliance_approve_data_request(uuid,uuid,text)
    to service_role;
grant execute on function public.compliance_request_legal_hold_action(uuid,uuid,text,uuid,text,text,text,text,timestamptz)
    to service_role;
grant execute on function public.compliance_approve_legal_hold_action(uuid,uuid,text,text)
    to service_role;
grant execute on function public.compliance_export_tenant(uuid,uuid,text)
    to service_role;
grant execute on function public.compliance_export_subject(uuid,uuid,text)
    to service_role;
grant execute on function public.compliance_complete_export(uuid,uuid,text,text,text,integer,text)
    to service_role;
grant execute on function public.compliance_delete_tenant(uuid,uuid,text)
    to service_role;
grant execute on function public.compliance_delete_subject(uuid,uuid,text)
    to service_role;
grant execute on function public.compliance_replay_deletion_tombstone(text,uuid,uuid,timestamptz,timestamptz,text,uuid,text,text,text)
    to service_role;
grant execute on function public.compliance_run_retention(uuid,text,integer,boolean)
    to service_role;
grant execute on function public.compliance_retention_worker_cycle(uuid,uuid,uuid,uuid,text,text,integer)
    to service_role;
grant execute on function public.compliance_retention_worker_health()
    to service_role;

comment on table public.data_subject_requests is
    'Tenant-offboarding, member, and end-customer GDPR/CCPA workflow; primary completion target 30 days. Overdue approved requests remain processable and are audited as deadline_breached.';
comment on table public.legal_holds is
    'Compliance-controlled holds. Service role can read for fail-closed execution; only approval of a distinct-admin legal_hold_actions request mutates administrative hold state.';
comment on table public.legal_hold_actions is
    'Two-person create/release intent with immutable request fields and one pending-to-approved transition. Pending creates fail closed for matching workflows and tenant retention; pending releases never weaken a hold.';
comment on table public.backup_deletion_tombstones is
    'Immutable restore instruction; tenant data must age out of rotating backups within 35 days of request receipt.';
comment on function public.compliance_delete_tenant(uuid,uuid,text) is
    'Transactional tenant erasure preserving immutable 400-day admin/security audit and minimized billing/tax evidence retained seven years subject to counsel.';
comment on function public.compliance_delete_subject(uuid,uuid,text) is
    'Transactional member/end-customer erasure scoped to one authoritative tenant, preserving immutable audit and minimized billing/tax evidence.';
comment on function public.compliance_replay_deletion_tombstone(text,uuid,uuid,timestamptz,timestamptz,text,uuid,text,text,text) is
    'Restore-only deletion replay that fails closed without source-bound isolated-target control and completed raw verification.';
comment on function public.compliance_run_retention(uuid,text,integer,boolean) is
    'Service-only bounded retention count/apply job. Preserves active or pending-create legal holds and all usage linked to the seven-year financial ledger.';
comment on function public.compliance_retention_worker_cycle(uuid,uuid,uuid,uuid,text,text,integer) is
    'Dedicated Railway retention authority: one advisory-locked dry/apply/post-verify cycle with content-free health state.';
