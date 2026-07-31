\set ON_ERROR_STOP on

-- Behavioural contract for 202607280036_settlement_evidence_erasure_fence.sql.
--
-- The property under test: NO erasure or retention path may delete a
-- public.usage_log row at or below the usage_log_watermark_id of a non-void
-- public.period_settlement_ledger row for the same organization, and the
-- preservation invariant must ABORT the transaction if one ever is.
--
-- This is the only place in scripts/ci that exercises that property. The
-- pre-existing coverage cannot: scripts/ci/migration-compliance-evidence-assertions.sql
-- fences on public.billing_ledger, which has had no writer since
-- 202607280006 dropped queue_brevitas_fee_after_usage, so every fence and every
-- before/after count in it compares 0 to 0. Deleting the entire settlement
-- evidence base used to be silent.
--
-- Four organizations, deliberately disjoint, so this file is order-independent
-- with respect to the other assertion files and to itself:
--   A  tenant erasure          7e100000-...-0001
--   B  subject erasure, member 7e100000-...-0002
--   C  nightly retention       7e100000-...-0003
--   D  mutation: the fence is removed and the invariant must fire
--                              7e100000-...-0004
-- All fixture state is discarded by the trailing rollback.
--
-- IDENTIFYING ROWS AFTER ERASURE: by (organization_id, id) relative to the
-- settlement watermark, never by request_id. Erasure PRESERVES a pinned row and
-- then MINIMIZES it, and the minimizing UPDATE blanks request_id along with
-- every other content column (202607280036:311 for the tenant path, :495 for the
-- subject path). Matching on request_id therefore cannot tell "deleted" from
-- "preserved and minimized" -- it reports both as absent, which made an earlier
-- draft of this file fail against a fence that was working correctly.
-- organization_id and id are never rewritten by any erasure or retention path,
-- and "at or below the watermark" is the property under test stated directly.
begin;

-- ---------------------------------------------------------------------------
-- 0. Registration guard. If 202607280036 is not in the manifests the harness
--    never applied it, and every assertion below would fail for the wrong
--    reason.
-- ---------------------------------------------------------------------------
do $$
begin
    if not exists (
        select 1 from pg_proc procedure
          join pg_namespace namespace on namespace.oid = procedure.pronamespace
         where namespace.nspname = 'public'
           and procedure.proname = 'compliance_delete_tenant_pre_company_identity'
           and procedure.prosrc like '%period_settlement_ledger%'
    ) then
        raise exception
            '202607280036_settlement_evidence_erasure_fence.sql is not registered in scripts/ci/migration-fresh-manifest.txt and scripts/ci/migration-upgrade-manifest.txt';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 1. Fixture.
-- ---------------------------------------------------------------------------
insert into auth.users (id, email) values
    ('7e000000-0000-4000-8000-000000000001', 'settlement-fence-a@example.invalid'),
    ('7e000000-0000-4000-8000-000000000002', 'settlement-fence-b@example.invalid'),
    ('7e000000-0000-4000-8000-000000000003', 'settlement-fence-b-member@example.invalid'),
    ('7e000000-0000-4000-8000-000000000004', 'settlement-fence-c@example.invalid'),
    ('7e000000-0000-4000-8000-000000000005', 'settlement-fence-d@example.invalid');

insert into public.organizations (id, name, billing_owner_id) values
    ('7e100000-0000-4000-8000-000000000001', 'Settlement fence A',
     '7e000000-0000-4000-8000-000000000001'),
    ('7e100000-0000-4000-8000-000000000002', 'Settlement fence B',
     '7e000000-0000-4000-8000-000000000002'),
    ('7e100000-0000-4000-8000-000000000003', 'Settlement fence C',
     '7e000000-0000-4000-8000-000000000004'),
    ('7e100000-0000-4000-8000-000000000004', 'Settlement fence D',
     '7e000000-0000-4000-8000-000000000005');

insert into public.organization_members (organization_id, user_id, role, status) values
    ('7e100000-0000-4000-8000-000000000001',
     '7e000000-0000-4000-8000-000000000001', 'company_owner', 'active'),
    ('7e100000-0000-4000-8000-000000000002',
     '7e000000-0000-4000-8000-000000000002', 'company_owner', 'active'),
    ('7e100000-0000-4000-8000-000000000002',
     '7e000000-0000-4000-8000-000000000003', 'member', 'active'),
    ('7e100000-0000-4000-8000-000000000003',
     '7e000000-0000-4000-8000-000000000004', 'company_owner', 'active'),
    ('7e100000-0000-4000-8000-000000000004',
     '7e000000-0000-4000-8000-000000000005', 'company_owner', 'active');

-- Three usage rows per organization, in ascending id:
--   *-lookback  before the settled period; read by billing_observed_model_price
--               (202607280031:881-889) and by the anchor set (:1057-1068)
--   *-inperiod  inside the settled period; the eligible set (:1078-1089)
--   *-late      after the watermark; NOT evidence, and must still be erased
-- ts is 20 months back for the first two so that the 13-month retention cutoff
-- also selects them, which is what makes organization C's assertion meaningful.
insert into public.usage_log (
    key_hash, owner_id, organization_id, customer_id, request_id, ts, provider,
    model, project, environment, source, session_id, pipeline, run_id,
    usage_raw, authoritative, pricing_status, receipt_source,
    actual_cost_usd, verified_savings_usd, tokens_saved, output_tokens
)
select
    'settlement-fence-key-' || org.tag,
    org.owner::text,
    org.id,
    null,
    'settlement-fence-' || org.tag || '-' || row_spec.slot,
    now() - row_spec.age,
    'anthropic', 'claude-3-5-sonnet',
    'Billing Pipeline', 'production', 'sdk',
    'session-' || org.tag, 'nightly-batch', 'run-' || org.tag,
    '{"prompt":"customer content"}',
    true, 'priced', 'proxy',
    1.0, 4.0, 1000, 500
  from (values
        ('a', '7e100000-0000-4000-8000-000000000001'::uuid, '7e000000-0000-4000-8000-000000000001'::uuid),
        ('b', '7e100000-0000-4000-8000-000000000002'::uuid, '7e000000-0000-4000-8000-000000000003'::uuid),
        ('c', '7e100000-0000-4000-8000-000000000003'::uuid, '7e000000-0000-4000-8000-000000000004'::uuid),
        ('d', '7e100000-0000-4000-8000-000000000004'::uuid, '7e000000-0000-4000-8000-000000000005'::uuid)
       ) as org(tag, id, owner)
 cross join (values
        ('lookback', interval '20 months'),
        ('inperiod', interval '20 months' - interval '3 days'),
        ('late',     interval '20 months' - interval '10 days')
       ) as row_spec(slot, age)
 order by org.tag, row_spec.age desc;

-- One settlement per organization, pinned at the 'inperiod' row's id. 'pending'
-- is deliberate: per 202607280008 a pending settlement is already money.
insert into public.period_settlement_ledger (
    organization_id, period_start, period_end,
    verified_savings_usd, warm_spend_usd, fee_microusd,
    usage_row_count, usage_log_watermark_id, billing_owner_id, status
)
select
    organization.id,
    now() - interval '20 months' - interval '1 day',
    now() - interval '20 months' + interval '6 days',
    8, 0, 2000000, 2,
    (select usage.id from public.usage_log usage
      where usage.organization_id = organization.id
        and usage.request_id like '%-inperiod'),
    organization.billing_owner_id,
    'pending'
  from public.organizations organization
 where organization.id in (
        '7e100000-0000-4000-8000-000000000001',
        '7e100000-0000-4000-8000-000000000002',
        '7e100000-0000-4000-8000-000000000003',
        '7e100000-0000-4000-8000-000000000004');

insert into public.data_subject_requests (
    id, organization_id, request_type, request_scope, subject_id, status,
    evidence_reference, requested_at, due_at, approved_at, approved_by, created_by
) values
    ('7e300000-0000-4000-8000-000000000001',
     '7e100000-0000-4000-8000-000000000001', 'delete', 'tenant', null,
     'approved', 'evidence:settlement-fence:a', now() - interval '1 day',
     now() + interval '29 days', now(), 'system:settlement-fence',
     'system:settlement-fence'),
    ('7e300000-0000-4000-8000-000000000002',
     '7e100000-0000-4000-8000-000000000002', 'delete', 'member',
     '7e000000-0000-4000-8000-000000000003',
     'approved', 'evidence:settlement-fence:b', now() - interval '1 day',
     now() + interval '29 days', now(), 'system:settlement-fence',
     'system:settlement-fence'),
    ('7e300000-0000-4000-8000-000000000004',
     '7e100000-0000-4000-8000-000000000004', 'delete', 'tenant', null,
     'approved', 'evidence:settlement-fence:d', now() - interval '1 day',
     now() + interval '29 days', now(), 'system:settlement-fence',
     'system:settlement-fence');

-- The fixture is only meaningful if the watermark really does sit between the
-- pinned rows and the late one.
do $$
declare
    v_tag text;
    v_mark bigint;
begin
    foreach v_tag in array array['a', 'b', 'c', 'd'] loop
        select settlement.usage_log_watermark_id into v_mark
          from public.period_settlement_ledger settlement
          join public.organizations organization
            on organization.id = settlement.organization_id
         where organization.name = 'Settlement fence ' || upper(v_tag);
        if v_mark is null then
            raise exception 'fixture % has no settlement watermark', v_tag;
        end if;
        if not exists (
            select 1 from public.usage_log usage
             where usage.request_id = 'settlement-fence-' || v_tag || '-lookback'
               and usage.id < v_mark
        ) or not exists (
            select 1 from public.usage_log usage
             where usage.request_id = 'settlement-fence-' || v_tag || '-late'
               and usage.id > v_mark
        ) then
            raise exception
                'fixture % does not straddle the watermark, so it proves nothing', v_tag;
        end if;
    end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- 2. TENANT ERASURE must not delete evidence at or below the watermark, and
--    must still delete everything above it.
-- ---------------------------------------------------------------------------
select public.compliance_delete_tenant(
    '7e100000-0000-4000-8000-000000000001',
    '7e300000-0000-4000-8000-000000000001',
    'system:settlement-fence');

do $$
declare
    v_org constant uuid := '7e100000-0000-4000-8000-000000000001';
    v_mark bigint;
    v_pinned integer;
    v_above integer;
begin
    select settlement.usage_log_watermark_id into v_mark
      from public.period_settlement_ledger settlement
     where settlement.organization_id = v_org;
    if v_mark is null then
        raise exception
            'tenant erasure destroyed the settlement row itself, not just its evidence';
    end if;
    select count(*) into v_pinned from public.usage_log usage
     where usage.organization_id = v_org and usage.id <= v_mark;
    select count(*) into v_above from public.usage_log usage
     where usage.organization_id = v_org and usage.id > v_mark;

    -- The lookback row and the watermark row itself. Both are inside the set
    -- billing_period_settlement_evidence re-reads, so both must survive.
    if v_pinned <> 2 then
        raise exception
            'tenant erasure deleted usage evidence at or below a live settlement watermark: % of 2 pinned rows survive',
            v_pinned;
    end if;
    if v_above <> 0 then
        raise exception
            'the settlement fence over-preserves: % row(s) above the watermark survived tenant erasure',
            v_above;
    end if;
    -- Preservation is not a licence to keep content. The surviving rows must
    -- still be minimized by the UPDATE that follows the fenced delete.
    if exists (
        select 1 from public.usage_log usage
         where usage.organization_id = v_org and usage.id <= v_mark
           and (usage.usage_raw <> '' or usage.owner_id <> ''
                or usage.session_id <> '' or usage.pipeline <> ''
                or usage.request_id <> '')
    ) then
        raise exception
            'tenant erasure preserved settlement evidence but left customer content on it';
    end if;
    -- ...and the money the settlement was computed from must be intact.
    if not exists (
        select 1 from public.usage_log usage
         where usage.id = v_mark
           and usage.verified_savings_usd = 4.0
           and usage.authoritative
           and usage.pricing_status = 'priced'
    ) then
        raise exception 'tenant erasure destroyed the retained financial columns';
    end if;
    if not exists (
        select 1 from public.data_subject_requests request
         where request.id = '7e300000-0000-4000-8000-000000000001'
           and request.status = 'completed'
    ) then
        raise exception 'the settlement fence broke the tenant deletion state machine';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. SUBJECT ERASURE, member branch. Same property, and the PERVERSE direction
--    matters more here: removing one subject's rows from the unfloored
--    sum(eligible.row_savings) (202607280031:1097) moves an already-invoiced
--    basis, upward if that subject's verified_savings_usd netted negative.
-- ---------------------------------------------------------------------------
select public.compliance_delete_subject(
    '7e100000-0000-4000-8000-000000000002',
    '7e300000-0000-4000-8000-000000000002',
    'system:settlement-fence');

do $$
declare
    v_org constant uuid := '7e100000-0000-4000-8000-000000000002';
    v_mark bigint;
    v_pinned integer;
    v_above integer;
begin
    select settlement.usage_log_watermark_id into v_mark
      from public.period_settlement_ledger settlement
     where settlement.organization_id = v_org;
    select count(*) into v_pinned from public.usage_log usage
     where usage.organization_id = v_org and usage.id <= v_mark;
    select count(*) into v_above from public.usage_log usage
     where usage.organization_id = v_org and usage.id > v_mark;

    if v_pinned <> 2 then
        raise exception
            'subject erasure deleted usage evidence at or below a live settlement watermark: % of 2 pinned rows survive',
            v_pinned;
    end if;
    if v_above <> 0 then
        raise exception
            'the settlement fence over-preserves: % row(s) above the watermark survived subject erasure',
            v_above;
    end if;
    if exists (
        select 1 from public.usage_log usage
         where usage.organization_id = v_org and usage.id <= v_mark
           and (usage.usage_raw <> '' or usage.owner_id <> ''
                or usage.request_id <> '')
    ) then
        raise exception
            'subject erasure preserved settlement evidence but left subject content on it';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. NIGHTLY RETENTION. This is the automated vector -- scripts/dr/retention.sh:69
--    runs it unattended -- and it is the one the original finding did not name.
--    Both the dry-run candidate count and the apply-time delete must be fenced,
--    or the immutable compliance_retention_runs evidence row records a number
--    that does not match what was deleted.
-- ---------------------------------------------------------------------------
do $$
declare
    v_org constant uuid := '7e100000-0000-4000-8000-000000000003';
    v_dry jsonb;
    v_applied jsonb;
    v_mark bigint;
    v_pinned integer;
    v_above integer;
begin
    select settlement.usage_log_watermark_id into v_mark
      from public.period_settlement_ledger settlement
     where settlement.organization_id = v_org;

    v_dry := public.compliance_run_retention(
        '7e400000-0000-4000-8000-000000000001', 'system:settlement-fence',
        10000, false);
    v_applied := public.compliance_run_retention(
        '7e400000-0000-4000-8000-000000000002', 'system:settlement-fence',
        10000, true);

    select count(*) into v_pinned from public.usage_log usage
     where usage.organization_id = v_org and usage.id <= v_mark;
    select count(*) into v_above from public.usage_log usage
     where usage.organization_id = v_org and usage.id > v_mark;

    -- Organization C is never erased, so retention is the only thing that could
    -- have touched these rows. Its 'late' row is older than the 13-month cutoff
    -- and no settlement pins it, so retention MUST take it -- otherwise the
    -- fence is simply preserving everything and proves nothing.
    if v_above <> 0 then
        raise exception
            'retention did not delete % 13-month-old row(s) that no settlement pins',
            v_above;
    end if;
    if v_pinned <> 2 then
        raise exception
            'nightly retention deleted usage evidence at or below a live settlement watermark: % of 2 pinned rows survive',
            v_pinned;
    end if;
    -- The dry run has to see the same fenced candidate set the apply deletes,
    -- otherwise the retention evidence row is a lie.
    if (v_dry->>'usage_candidates')::integer
       <> (v_applied->>'usage_deleted')::integer then
        raise exception
            'the retention dry-run candidate count and the fenced delete disagree: % vs %',
            v_dry->>'usage_candidates', v_applied->>'usage_deleted';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 5. MUTATION. Everything above would still pass if the fence merely deleted
--    nothing at all. This section removes the fence from the live function --
--    mechanically, by rewriting prosrc, so the mutant differs from the shipped
--    body in exactly that predicate and nothing else -- and requires that the
--    preservation invariant then ABORTS the erasure.
--
--    Before 202607280036 this mutant WAS the shipped body, and it completed
--    silently.
-- ---------------------------------------------------------------------------
savepoint before_mutation;

do $mutate$
declare
    v_src text;
    v_mutated text;
begin
    select procedure.prosrc into v_src
      from pg_proc procedure
      join pg_namespace namespace on namespace.oid = procedure.pronamespace
     where namespace.nspname = 'public'
       and procedure.proname = 'compliance_delete_tenant_pre_company_identity';

    -- Strip only the delete fence. The invariant's own `and exists (...)`
    -- probes do not match: they are not `and not exists`.
    v_mutated := regexp_replace(
        v_src,
        'and not exists \(select 1 from public\.period_settlement_ledger settlement[^;]*usage\.id\)',
        '', 'g');
    if v_mutated = v_src then
        raise exception
            'the mutation found no settlement delete fence to remove -- the fence is not where this file thinks it is';
    end if;
    if v_mutated not like '%settlement evidence preservation invariant failed%' then
        raise exception 'the mutation removed the invariant as well as the fence';
    end if;

    execute format(
        'create or replace function'
        || ' public.compliance_delete_tenant_pre_company_identity('
        || 'p_organization_id uuid, p_request_id uuid, p_actor_id text)'
        || ' returns text language plpgsql security definer'
        || ' set search_path = public, pg_temp as %L',
        v_mutated);
end;
$mutate$;

do $$
declare
    v_raised boolean := false;
    v_message text;
begin
    begin
        perform public.compliance_delete_tenant(
            '7e100000-0000-4000-8000-000000000004',
            '7e300000-0000-4000-8000-000000000004',
            'system:settlement-fence');
    exception when others then
        v_raised := true;
        v_message := sqlerrm;
    end;
    if not v_raised then
        raise exception
            'the preservation invariant is vacuous: erasure destroyed settlement evidence and returned normally';
    end if;
    if v_message not like '%settlement evidence preservation invariant failed%' then
        raise exception
            'erasure failed for the wrong reason, so the invariant is still unproven: %',
            v_message;
    end if;
end;
$$;

rollback to savepoint before_mutation;

-- The mutant is gone with the savepoint; the shipped body is live again and
-- still refuses to delete the pinned rows.
do $$
begin
    if not exists (
        select 1 from pg_proc procedure
          join pg_namespace namespace on namespace.oid = procedure.pronamespace
         where namespace.nspname = 'public'
           and procedure.proname = 'compliance_delete_tenant_pre_company_identity'
           and procedure.prosrc like
               '%not exists (select 1 from public.period_settlement_ledger settlement%'
    ) then
        raise exception 'the mutation was not rolled back';
    end if;
end;
$$;

select public.compliance_delete_tenant(
    '7e100000-0000-4000-8000-000000000004',
    '7e300000-0000-4000-8000-000000000004',
    'system:settlement-fence');

do $$
declare
    v_org constant uuid := '7e100000-0000-4000-8000-000000000004';
    v_mark bigint;
    v_pinned integer;
begin
    select settlement.usage_log_watermark_id into v_mark
      from public.period_settlement_ledger settlement
     where settlement.organization_id = v_org;
    select count(*) into v_pinned from public.usage_log usage
     where usage.organization_id = v_org and usage.id <= v_mark;
    -- D's row above the watermark was already taken by section 4's retention
    -- run, so the two pinned rows are all that should remain.
    if v_pinned <> 2 then
        raise exception
            'the unmutated body still deleted evidence at or below the watermark: % of 2 pinned rows survive',
            v_pinned;
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. A 'void' settlement is a retracted draft and pins nothing. Without this
--    the fence could be trivially satisfied by any row in the table.
-- ---------------------------------------------------------------------------
do $$
declare
    v_deleted integer;
begin
    insert into auth.users (id, email) values
        ('7e000000-0000-4000-8000-000000000006', 'settlement-fence-void@example.invalid');
    insert into public.organizations (id, name, billing_owner_id) values
        ('7e100000-0000-4000-8000-000000000005', 'Settlement fence VOID',
         '7e000000-0000-4000-8000-000000000006');
    insert into public.usage_log (
        key_hash, owner_id, organization_id, request_id, ts, authoritative,
        pricing_status, receipt_source, verified_savings_usd
    ) values (
        'settlement-fence-key-void', '7e000000-0000-4000-8000-000000000006',
        '7e100000-0000-4000-8000-000000000005', 'settlement-fence-void-row',
        now() - interval '20 months', true, 'priced', 'proxy', 1
    );
    insert into public.period_settlement_ledger (
        organization_id, period_start, period_end, verified_savings_usd,
        warm_spend_usd, fee_microusd, usage_log_watermark_id, status
    )
    select '7e100000-0000-4000-8000-000000000005',
           now() - interval '20 months' - interval '1 day',
           now() - interval '20 months' + interval '6 days',
           4, 0, 0,
           (select usage.id from public.usage_log usage
             where usage.request_id = 'settlement-fence-void-row'),
           'void';

    perform public.compliance_run_retention(
        '7e400000-0000-4000-8000-000000000003', 'system:settlement-fence',
        10000, true);
    select count(*) into v_deleted from public.usage_log usage
     where usage.organization_id = '7e100000-0000-4000-8000-000000000005';
    if v_deleted <> 0 then
        raise exception
            'a void (retracted) settlement pinned usage evidence it does not own';
    end if;
end;
$$;

rollback;
