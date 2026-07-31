-- The attestation WRITER that 202607280009 deliberately did not build.
--
-- WHAT 202607280009 LEFT OPEN, IN ITS OWN WORDS
--
--   TODO(billing-attestation-writer): there is deliberately NO application
--   write path for public.organization_billing_arrangement -- service_role gets
--   SELECT only, so no runtime code (and no leaked service key) can attest an
--   org into billability. Populating it is an out-of-band, human, reviewed act
--   (a migration or a DBA psql session) [...] BLOCKER for whoever lifts the
--   Phase 0 freeze: decide and document who is entitled to attest and what
--   evidence they must cite, then build that process (not an RPC) before the
--   first settlement.
--
-- That posture is correct and is NOT relaxed here. What it costs today is that
-- every attestation is hand-written DML against a seven-year financial record,
-- typed into a psql session by a human under time pressure, with no record of
-- who typed it, no prior value, and no enforced evidence. The failure mode is
-- not "someone attests an org they should not" -- it is "someone attests the
-- wrong org, or the wrong arrangement, and nothing anywhere can reconstruct
-- what they were looking at".
--
-- WHAT THIS MIGRATION CHANGES, AND WHAT IT REFUSES TO CHANGE
--
-- CHANGED: the hand-written SQL. An operator now calls one SECURITY DEFINER
-- function that validates the vocabulary, requires non-empty cited evidence,
-- records the prior value, records who attested and under which session
-- identity, and appends an insert-only log row that outlives the organization.
--
-- NOT CHANGED, and this is the whole safety property:
--   * public.organization_billing_arrangement still grants INSERT/UPDATE/DELETE
--     to NOBODY -- not service_role, not anon, not authenticated. The DO block
--     at the end re-asserts 202607280009's own assertion so that a future
--     migration cannot quietly loosen it while this file still claims to be the
--     only writer.
--   * The write path is NOT reachable from any runtime role. EXECUTE on both
--     functions is revoked from public, anon, authenticated AND service_role,
--     and granted to exactly one role -- brevitas_attestor -- that no deployed
--     service holds credentials for.
--   * The functions additionally refuse to run unless session_user is literally
--     'brevitas_attestor'. That is defence in depth against the one failure the
--     grant cannot survive: a later migration, or a hurried operator, running
--     `grant execute ... to service_role`. The grant would then exist and the
--     body would still refuse.
--   * The HUMAN JUDGEMENT IS NOT AUTOMATED. 202607280009's reasoning is exactly
--     right: usage_log.actual_cost_usd is static published list price
--     (brevitas/receipts.py MODEL_PRICES) with no organization dimension, so a
--     committed-capacity customer is INDISTINGUISHABLE in the data from a
--     pay-as-you-go one. No query can decide this. A person still has to read
--     the actual provider agreement. All this migration removes is the typing.
--
-- THE CUSTOMER-FACING HALF IS INERT BY CONSTRUCTION
--
-- public.billing_arrangement_request exists so a company owner accepting
-- commercial terms in the dashboard produces a durable, reviewable record --
-- what they were shown, which agreement they cited, what they declared their
-- own arrangement to be. service_role may INSERT and SELECT it, because the API
-- must be able to write it.
--
-- NOTHING IN THE SETTLEMENT PATH READS THAT TABLE. It is paperwork, not
-- permission. self_declared_arrangement is an INPUT TO THE HUMAN and is never
-- the value written to public.organization_billing_arrangement. A customer (or
-- a leaked service key) may create as many requests as they like and no fee
-- becomes possible, because the only thing that moves money is a row in
-- 202607280009's table, and nothing holding a service key can write one. The DO
-- block asserts this by reading the settlement functions' own source.
--
-- WHAT IS STILL ENFORCED BY HUMAN DISCIPLINE, SAID OUT LOUD
--
-- The schema cannot stop someone from putting BREVITAS_ATTESTOR_DSN into
-- Railway, Vercel or GitHub Actions. If that credential is ever deployed
-- alongside application code, every guarantee above collapses to "the app can
-- attest itself". The role is created here with NO PASSWORD precisely so that
-- the default state is unusable -- until a human sets one out of band, nobody
-- can connect as brevitas_attestor at all, and this migration is safe to apply
-- anywhere. Keeping the DSN out of deployed environments is a process control,
-- not a schema control, and pretending otherwise would be dishonest.
--
-- Nor can the RPC enforce that an attestation is TRUE. It enforces that the
-- attester named themselves and cited at least eight characters of evidence.
-- "Who is entitled to attest, and what evidence counts" remains the open
-- business decision 202607280009 named; this migration gives that decision a
-- place to be recorded and audited, not an answer.
--
-- REVERSE: PITR-ONLY -- public.organization_billing_arrangement_log is insert-only
-- attestation evidence and dropping it destroys the record of who attested which
-- organization into billability, which is the only audit trail the money path has.
--
-- Safety: additive. Three tables, two functions, one login role with no
-- password, and revokes. It grants nothing to anon/authenticated, grants no new
-- write to service_role on any pre-existing table, and cannot cause a fee to be
-- charged -- an attestation only removes one halting condition; every
-- 202607280008 arithmetic condition still applies afterwards.

begin;

-- ---------------------------------------------------------------------------
-- Preconditions. This migration is meaningless without 202607280009's table and
-- its settlement guard; refuse rather than install a writer for something that
-- is not there.
-- ---------------------------------------------------------------------------
do $attestation_writer_precondition$
begin
    if to_regclass('public.organization_billing_arrangement') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280030 requires public.organization_billing_arrangement '
                      'from 202607280009',
            hint    = 'Apply 202607280009 first.';
    end if;
    if to_regprocedure('public.organization_billing_arrangement_state(uuid)') is null then
        raise exception using
            errcode = '42883',
            message = '202607280030 requires public.organization_billing_arrangement_state '
                      'from 202607280009',
            hint    = 'Apply 202607280009 first.';
    end if;
    if to_regprocedure(
           'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)'
       ) is null then
        raise exception using
            errcode = '42883',
            message = '202607280030 requires public.assert_billing_period_settlement_allowed '
                      'from 202607280009',
            hint    = 'This migration writes the input that guard reads; it does not '
                      'replace the guard.';
    end if;
    -- Same refusal 202607280008 and 202607280009 made: a per-row fee path
    -- bypasses the attestation entirely, so a writer for it must not install
    -- while that trigger is attached.
    if exists (
        select 1
          from pg_trigger trigger_state
         where trigger_state.tgrelid = 'public.usage_log'::regclass
           and trigger_state.tgname = 'queue_brevitas_fee_after_usage'
           and not trigger_state.tgisinternal
    ) then
        raise exception using
            errcode = '55000',
            message = '202607280030 refuses to install the attestation writer while the '
                      'per-row fee trigger is attached',
            hint    = 'Reapply 202607280006 first.';
    end if;
end;
$attestation_writer_precondition$;

-- ---------------------------------------------------------------------------
-- The operator role.
--
-- NO PASSWORD IS SET HERE, deliberately and permanently. A role with no
-- password cannot authenticate under scram/md5, so the fail-closed state after
-- this migration is "nobody can attest anything". A human sets the password out
-- of band, into a password manager, and never into a deployed environment. That
-- also makes this migration safe to apply in CI and on a throwaway project: it
-- creates a credential that does not yet work.
--
-- NOINHERIT so that even if the role is later granted membership in something
-- broad, it does not silently acquire those privileges on connect.
-- ---------------------------------------------------------------------------
do $attestor_role$
begin
    if not exists (select 1 from pg_roles where rolname = 'brevitas_attestor') then
        create role brevitas_attestor login noinherit;
    end if;
end;
$attestor_role$;

-- Tightening only, and re-applied on every apply so a reapply cannot leave the
-- role more permissive than this file describes. LOGIN is deliberately NOT
-- re-asserted: an operator who disables the role during an incident must not
-- have it silently re-enabled by a replay of the chain.
--
-- SUPERUSER / BYPASSRLS / REPLICATION are split out from the attributes above.
-- Changing any of those three is superuser-only, and the `postgres` role on a
-- hosted Supabase project is NOT a superuser, so asserting them unconditionally
-- made this whole migration unappliable there:
--
--   ERROR: permission denied to alter role
--   DETAIL: Only roles with the SUPERUSER attribute may alter roles with the
--           SUPERUSER attribute.
--
-- (Observed applying this file to the caestus-labs project; the file is
-- explicitly transactional, so the failed apply left nothing behind.) All three
-- are already OFF for a freshly CREATEd role, so the ALTER was belt-and-braces
-- rather than the thing that establishes the property. What actually has to
-- hold is the RESULTING STATE, so that is what is asserted below -- and it is
-- asserted unconditionally, on every apply, whether or not the ALTER was
-- permitted. A pre-existing brevitas_attestor that someone had made SUPERUSER,
-- BYPASSRLS or REPLICATION therefore still aborts this migration loudly instead
-- of being silently accepted.
alter role brevitas_attestor noinherit nocreatedb nocreaterole;

do $attestor_attributes$
declare
    v_super boolean;
    v_bypassrls boolean;
    v_replication boolean;
begin
    -- Try the superuser-only tightening, but never let the lack of superuser
    -- abort the migration: on Supabase these are already false and the
    -- assertion below is what proves it.
    begin
        execute 'alter role brevitas_attestor nosuperuser nobypassrls noreplication';
    exception
        when insufficient_privilege then
            null;
    end;

    select rolsuper, rolbypassrls, rolreplication
      into v_super, v_bypassrls, v_replication
      from pg_roles where rolname = 'brevitas_attestor';

    if v_super or v_bypassrls or v_replication then
        raise exception '202607280030: brevitas_attestor must not hold SUPERUSER, '
                        'BYPASSRLS or REPLICATION (super=% bypassrls=% replication=%)',
                        v_super, v_bypassrls, v_replication
              using hint = 'The attestor is an operator credential reached only through two '
                           'SECURITY DEFINER functions. Any of these attributes would let it '
                           'write organization_billing_arrangement directly, with no log row.';
    end if;
end;
$attestor_attributes$;

grant usage on schema public to brevitas_attestor;

-- The operator role reaches the attestation ONLY through the two SECURITY
-- DEFINER functions below. It holds no direct privilege on any table, including
-- the attestation table itself, so a mistyped ad-hoc UPDATE from an attestor
-- session fails on privilege rather than silently rewriting a financial record
-- with no log row. Stated as a blanket revoke so the property holds by
-- construction rather than by having remembered every table.
revoke all on all tables in schema public from brevitas_attestor;
revoke all on all sequences in schema public from brevitas_attestor;
revoke all on all functions in schema public from brevitas_attestor;

comment on role brevitas_attestor is
    'Out-of-band operator role for billing-arrangement attestation. Holds EXECUTE '
    'on public.attest_billing_arrangement and public.revoke_billing_arrangement '
    'and NOTHING else -- no table privileges at all. Created without a password '
    'so the fail-closed default is unusable. Its DSN must never appear in any '
    'deployed environment (Railway, Vercel, GitHub Actions); that is a process '
    'control, not a schema control.';

-- ---------------------------------------------------------------------------
-- The insert-only attestation log.
--
-- No foreign key to public.organizations, on purpose. An attestation is
-- evidence about a commercial decision; deleting the organization must not
-- delete the record that someone attested it into billability. The
-- organization_id is therefore stored as a bare uuid.
--
-- No role anywhere is granted UPDATE or DELETE. service_role gets SELECT so an
-- operator tool and the billing-readiness surface can read history; the only
-- writer is the SECURITY DEFINER function below, which inserts as the table
-- owner.
-- ---------------------------------------------------------------------------
create table if not exists public.organization_billing_arrangement_log (
    id bigserial primary key,
    organization_id uuid not null,
    action text not null,
    prior_arrangement text not null,
    new_arrangement text not null,
    attested_by text not null,
    attested_evidence text not null,
    attested_session_user text not null,
    request_id uuid,
    recorded_at timestamptz not null default now()
);

alter table public.organization_billing_arrangement_log
    drop constraint if exists organization_billing_arrangement_log_action_check;
alter table public.organization_billing_arrangement_log
    add constraint organization_billing_arrangement_log_action_check
    check (action in ('attest', 'revoke'));

alter table public.organization_billing_arrangement_log
    drop constraint if exists organization_billing_arrangement_log_evidence_check;
alter table public.organization_billing_arrangement_log
    add constraint organization_billing_arrangement_log_evidence_check
    check (char_length(btrim(attested_evidence)) >= 1);

alter table public.organization_billing_arrangement_log
    drop constraint if exists organization_billing_arrangement_log_attested_by_check;
alter table public.organization_billing_arrangement_log
    add constraint organization_billing_arrangement_log_attested_by_check
    check (char_length(btrim(attested_by)) between 1 and 200);

create index if not exists organization_billing_arrangement_log_org_idx
    on public.organization_billing_arrangement_log (organization_id, recorded_at desc);

alter table public.organization_billing_arrangement_log enable row level security;

revoke all on table public.organization_billing_arrangement_log
    from public, anon, authenticated, service_role, brevitas_attestor;
grant select on table public.organization_billing_arrangement_log to service_role;
revoke all on sequence public.organization_billing_arrangement_log_id_seq
    from public, anon, authenticated, service_role, brevitas_attestor;

comment on table public.organization_billing_arrangement_log is
    'Insert-only history of every billing-arrangement attestation and revocation. '
    'No role holds UPDATE or DELETE, and the only INSERT path is '
    'public.attest_billing_arrangement / public.revoke_billing_arrangement running '
    'SECURITY DEFINER. Deliberately has NO foreign key to public.organizations: '
    'deleting an organization must not delete the evidence that someone attested '
    'it into billability.';
comment on column public.organization_billing_arrangement_log.attested_by is
    'The human the operator named when attesting. Free text; the RPC enforces '
    'that it is non-empty, not that it is true.';
comment on column public.organization_billing_arrangement_log.attested_session_user is
    'The database session_user that executed the attestation, captured by the '
    'function itself rather than supplied by the caller. This is the part of the '
    'row the attester cannot choose.';

-- ---------------------------------------------------------------------------
-- The customer-facing capture table. INERT: nothing in the settlement path
-- reads it, and the DO block at the end proves that by reading the settlement
-- functions' source.
-- ---------------------------------------------------------------------------
create table if not exists public.billing_arrangement_request (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null
        references public.organizations(id) on delete cascade,
    requested_by_user_id uuid references auth.users(id) on delete set null,
    requested_at timestamptz not null default now(),
    self_declared_arrangement text not null,
    agreement_reference text not null default '',
    rate_presented text not null default '',
    terms_version text not null default '',
    status text not null default 'open',
    decided_at timestamptz,
    decided_by text not null default '',
    decided_note text not null default ''
);

alter table public.billing_arrangement_request
    drop constraint if exists billing_arrangement_request_self_declared_check;
alter table public.billing_arrangement_request
    add constraint billing_arrangement_request_self_declared_check
    check (self_declared_arrangement in (
        'marginal_per_call',
        'committed_capacity',
        'unknown'
    ));

alter table public.billing_arrangement_request
    drop constraint if exists billing_arrangement_request_status_check;
alter table public.billing_arrangement_request
    add constraint billing_arrangement_request_status_check
    check (status in ('open', 'attested', 'declined'));

-- One open request per organization: a customer clicking "enable billing"
-- repeatedly must not produce a queue of identical rows for an operator to sift.
create unique index if not exists billing_arrangement_request_one_open_idx
    on public.billing_arrangement_request (organization_id)
    where status = 'open';

create index if not exists billing_arrangement_request_status_idx
    on public.billing_arrangement_request (status, requested_at desc);

alter table public.billing_arrangement_request enable row level security;

revoke all on table public.billing_arrangement_request
    from public, anon, authenticated, service_role, brevitas_attestor;
-- The API writes these on behalf of a signed-in company owner and reads them
-- back to show status. It cannot UPDATE or DELETE them: the transition to
-- 'attested' belongs to the SECURITY DEFINER function alone.
grant select, insert on table public.billing_arrangement_request to service_role;

comment on table public.billing_arrangement_request is
    'A company owner''s request to be billed, captured when they accept commercial '
    'terms. PAPERWORK, NOT PERMISSION: nothing in the settlement path reads this '
    'table, and a row here permits no fee whatsoever. It exists so that the human '
    'attestation in public.organization_billing_arrangement has a reviewable '
    'record behind it -- what the customer was shown, which agreement they cited, '
    'and what they declared their own arrangement to be.';
comment on column public.billing_arrangement_request.self_declared_arrangement is
    'What the CUSTOMER says their provider arrangement is. An input to the human '
    'attester, never the value written to public.organization_billing_arrangement. '
    'A customer declaring marginal_per_call does not make them billable.';

-- ---------------------------------------------------------------------------
-- Opening a request. The API could INSERT directly -- service_role holds the
-- grant -- but then "only a company owner may accept commercial terms" would
-- live only in Python, in a file whose two service backends would each have to
-- get it right. It is enforced here instead, against the same
-- public.lock_company_actor_role the rest of the company-admin surface uses.
--
-- Conventions borrowed deliberately from that surface:
--   * returns {ok:false, code:'denied'} rather than raising on an authorization
--     failure, because CompanyAdminSupabaseService._result reads exactly that
--     shape AND because raising would roll back the denial audit row written
--     immediately before it. A denied attempt to enable billing is worth
--     keeping.
--   * malformed arguments still RAISE, since those are programming errors the
--     request body should already have rejected.
--   * idempotent: a second click returns the open request rather than a
--     unique-violation, so the dashboard needs no special case and an operator
--     never sees a queue of identical rows.
-- ---------------------------------------------------------------------------
create or replace function public.open_billing_arrangement_request(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_self_declared_arrangement text,
    p_agreement_reference text,
    p_rate_presented text,
    p_terms_version text,
    p_request_id text
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_role text;
    v_declared text;
    v_audit_request text;
    v_existing_id uuid;
    v_id uuid;
begin
    if p_organization_id is null or p_actor_user_id is null then
        raise exception using
            errcode = '22023',
            message = 'a billing-arrangement request requires an organization and an actor';
    end if;

    v_declared := btrim(coalesce(p_self_declared_arrangement, ''));
    if v_declared not in ('marginal_per_call', 'committed_capacity', 'unknown') then
        raise exception using
            errcode = '22023',
            message = format('unrecognised self-declared arrangement: %L',
                             p_self_declared_arrangement);
    end if;

    v_audit_request := btrim(coalesce(p_request_id, ''));
    if v_audit_request !~ '^[A-Za-z0-9._:-]{8,128}$' then
        v_audit_request := gen_random_uuid()::text;
    end if;

    v_role := public.lock_company_actor_role(p_organization_id, p_actor_user_id);

    -- Only the owner. Accepting commercial terms is the one company-admin
    -- action a company_admin must not be able to take on the owner's behalf.
    if coalesce(v_role, '') <> 'company_owner' then
        perform public.append_company_audit(
            p_organization_id, p_actor_user_id::text,
            coalesce(nullif(v_role, ''), 'none'), v_audit_request,
            'billing_arrangement.request.denied', 'organization',
            p_organization_id::text, 'denied');
        return jsonb_build_object('ok', false, 'code', 'denied');
    end if;

    select id into v_existing_id
      from public.billing_arrangement_request
     where organization_id = p_organization_id and status = 'open'
     for update;

    if found then
        return jsonb_build_object(
            'ok', true,
            'created', false,
            'request_id', v_existing_id,
            'arrangement_state',
                public.organization_billing_arrangement_state(p_organization_id),
            'attested', false
        );
    end if;

    insert into public.billing_arrangement_request
        (organization_id, requested_by_user_id, self_declared_arrangement,
         agreement_reference, rate_presented, terms_version)
    values
        (p_organization_id, p_actor_user_id, v_declared,
         left(coalesce(p_agreement_reference, ''), 500),
         left(coalesce(p_rate_presented, ''), 200),
         left(coalesce(p_terms_version, ''), 100))
    returning id into v_id;

    perform public.append_company_audit(
        p_organization_id, p_actor_user_id::text, v_role, v_audit_request,
        'billing_arrangement.request', 'organization',
        p_organization_id::text, 'committed');

    return jsonb_build_object(
        'ok', true,
        'created', true,
        'request_id', v_id,
        -- Returned so the caller cannot render "billing enabled". Opening a
        -- request changes NOTHING about billability; a human still has to
        -- attest, and this value says so.
        'arrangement_state',
            public.organization_billing_arrangement_state(p_organization_id),
        'attested', false
    );
end;
$$;

revoke all on function public.open_billing_arrangement_request(
    uuid, uuid, text, text, text, text, text)
    from public, anon, authenticated;
grant execute on function public.open_billing_arrangement_request(
    uuid, uuid, text, text, text, text, text)
    to service_role;

comment on function public.open_billing_arrangement_request(
    uuid, uuid, text, text, text, text, text) is
    'Records a company OWNER accepting commercial terms, as '
    'public.billing_arrangement_request. Owner-only, enforced here rather than in '
    'the API so both company-admin service backends inherit it. Idempotent per '
    'open request. Grants no billability whatsoever: the returned '
    'arrangement_state is the organization''s real (almost always ''unattested'') '
    'state and attested is always false, so a caller cannot render "billing '
    'enabled" off this response.';

-- ---------------------------------------------------------------------------
-- The attestation RPC.
-- ---------------------------------------------------------------------------
create or replace function public.attest_billing_arrangement(
    p_organization_id uuid,
    p_arrangement text,
    p_attested_by text,
    p_evidence text,
    p_request_id uuid default null
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_arrangement text;
    v_attested_by text;
    v_evidence text;
    v_prior text;
    v_log_id bigint;
    v_request_status text;
    v_request_org uuid;
begin
    -- FIRST, before anything else is even validated. Defence in depth: if a
    -- future migration or a hurried operator ever runs `grant execute ... to
    -- service_role`, the grant exists and this still refuses. The check is on
    -- session_user, not current_user, because current_user inside a SECURITY
    -- DEFINER function is the function owner and would tell us nothing about
    -- who connected.
    if session_user <> 'brevitas_attestor' then
        raise exception using
            errcode = '42501',
            message = 'attestation_denied: public.attest_billing_arrangement may only be '
                      'executed by the brevitas_attestor operator role',
            detail  = format('session_user=%s', session_user),
            hint    = 'Billing-arrangement attestation is an out-of-band human act. No '
                      'application role, and no leaked service key, may attest an '
                      'organization into billability.';
    end if;

    if p_organization_id is null then
        raise exception using
            errcode = '22023',
            message = 'attestation requires an organization';
    end if;
    if not exists (
        select 1 from public.organizations where id = p_organization_id
    ) then
        raise exception using
            errcode = '23503',
            message = 'attestation names an organization that does not exist',
            detail  = format('organization=%s', p_organization_id);
    end if;

    v_arrangement := btrim(coalesce(p_arrangement, ''));
    if v_arrangement not in ('marginal_per_call', 'committed_capacity', 'unknown') then
        raise exception using
            errcode = '22023',
            message = format('unrecognised billing arrangement: %L', p_arrangement),
            hint    = 'marginal_per_call (billable) | committed_capacity | unknown. The '
                      'vocabulary is 202607280009''s and is not extended here.';
    end if;

    v_attested_by := btrim(coalesce(p_attested_by, ''));
    if char_length(v_attested_by) < 1 or char_length(v_attested_by) > 200 then
        raise exception using
            errcode = '22023',
            message = 'attestation requires a named attester (1-200 characters)',
            hint    = 'Name the person who read the agreement, not the tool that ran the '
                      'command.';
    end if;

    -- 202607280009 allows attested_evidence to default to empty string. That is
    -- deliberately tightened here: an attestation nobody can audit later is
    -- worth very little, and this is the write path so it is the place to
    -- require it. The eight-character floor on the billable arrangement is a
    -- floor, not a validation -- the function enforces that evidence was cited,
    -- never that it is true.
    v_evidence := btrim(coalesce(p_evidence, ''));
    if char_length(v_evidence) < 1 then
        raise exception using
            errcode = '22023',
            message = 'attestation requires cited evidence',
            hint    = 'Cite the provider agreement, order form, or invoice the '
                      'arrangement was read from.';
    end if;
    if v_arrangement = 'marginal_per_call' and char_length(v_evidence) < 8 then
        raise exception using
            errcode = '22023',
            message = 'attesting marginal_per_call requires at least 8 characters of '
                      'cited evidence',
            detail  = format('evidence_length=%s', char_length(v_evidence)),
            hint    = 'This is the only arrangement that permits a positive fee. Cite the '
                      'document.';
    end if;

    -- The billable arrangement additionally requires that the customer actually
    -- asked to be billed. A request row is not permission -- it permits nothing
    -- on its own -- but attesting an organization into billability that never
    -- accepted terms should not be a single command away.
    if p_request_id is null and v_arrangement = 'marginal_per_call' then
        raise exception using
            errcode = '22023',
            message = 'attesting marginal_per_call requires the billing-arrangement '
                      'request it answers',
            hint    = 'The customer must have accepted commercial terms first; pass that '
                      'public.billing_arrangement_request id.';
    end if;

    if p_request_id is not null then
        select status, organization_id
          into v_request_status, v_request_org
          from public.billing_arrangement_request
         where id = p_request_id
           for update;
        if not found then
            raise exception using
                errcode = '23503',
                message = 'attestation cites a billing-arrangement request that does not exist',
                detail  = format('request=%s', p_request_id);
        end if;
        if v_request_org <> p_organization_id then
            raise exception using
                errcode = '22023',
                message = 'attestation cites a billing-arrangement request belonging to a '
                          'different organization',
                detail  = format('request=%s request_organization=%s attested_organization=%s',
                                 p_request_id, v_request_org, p_organization_id);
        end if;
        if v_request_status <> 'open' then
            raise exception using
                errcode = '55000',
                message = format('billing-arrangement request is already %s', v_request_status),
                detail  = format('request=%s', p_request_id);
        end if;
    end if;

    v_prior := public.organization_billing_arrangement_state(p_organization_id);

    insert into public.organization_billing_arrangement
        (organization_id, arrangement, attested_by, attested_evidence, attested_at, updated_at)
    values
        (p_organization_id, v_arrangement, v_attested_by, v_evidence, now(), now())
    on conflict (organization_id) do update
       set arrangement       = excluded.arrangement,
           attested_by       = excluded.attested_by,
           attested_evidence = excluded.attested_evidence,
           attested_at       = excluded.attested_at,
           updated_at        = now();

    insert into public.organization_billing_arrangement_log
        (organization_id, action, prior_arrangement, new_arrangement,
         attested_by, attested_evidence, attested_session_user, request_id)
    values
        (p_organization_id, 'attest', v_prior, v_arrangement,
         v_attested_by, v_evidence, session_user, p_request_id)
    returning id into v_log_id;

    if p_request_id is not null then
        update public.billing_arrangement_request
           set status      = 'attested',
               decided_at  = now(),
               decided_by  = v_attested_by
         where id = p_request_id;
    end if;

    return jsonb_build_object(
        'organization_id', p_organization_id,
        'action', 'attest',
        'prior_arrangement', v_prior,
        'arrangement', v_arrangement,
        'attested_by', v_attested_by,
        'attested_session_user', session_user,
        'evidence_length', char_length(v_evidence),
        'request_id', p_request_id,
        'log_id', v_log_id,
        -- Reported, not promised. An attestation removes ONE halting condition;
        -- every 202607280008 arithmetic condition still applies afterwards.
        'billable_arrangement', v_arrangement = 'marginal_per_call'
    );
end;
$$;

-- ---------------------------------------------------------------------------
-- De-attestation, on the same single grant, so returning an organization to
-- unbillable also never needs hand-written DML.
-- ---------------------------------------------------------------------------
create or replace function public.revoke_billing_arrangement(
    p_organization_id uuid,
    p_reason text
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_reason text;
    v_prior text;
    v_log_id bigint;
begin
    if session_user <> 'brevitas_attestor' then
        raise exception using
            errcode = '42501',
            message = 'attestation_denied: public.revoke_billing_arrangement may only be '
                      'executed by the brevitas_attestor operator role',
            detail  = format('session_user=%s', session_user);
    end if;

    if p_organization_id is null then
        raise exception using
            errcode = '22023',
            message = 'revocation requires an organization';
    end if;

    v_reason := btrim(coalesce(p_reason, ''));
    if char_length(v_reason) < 8 then
        raise exception using
            errcode = '22023',
            message = 'revoking an attestation requires a stated reason of at least 8 '
                      'characters',
            hint    = 'The log row is the only record of why an organization stopped '
                      'being billable.';
    end if;

    v_prior := public.organization_billing_arrangement_state(p_organization_id);

    delete from public.organization_billing_arrangement
     where organization_id = p_organization_id;

    -- Logged even when there was nothing to delete: "someone tried to
    -- de-attest this org" is itself worth keeping, and the prior value recorded
    -- is 'unattested', which is the truth.
    insert into public.organization_billing_arrangement_log
        (organization_id, action, prior_arrangement, new_arrangement,
         attested_by, attested_evidence, attested_session_user, request_id)
    values
        (p_organization_id, 'revoke', v_prior, 'unattested',
         session_user, v_reason, session_user, null)
    returning id into v_log_id;

    return jsonb_build_object(
        'organization_id', p_organization_id,
        'action', 'revoke',
        'prior_arrangement', v_prior,
        'arrangement', 'unattested',
        'reason', v_reason,
        'attested_session_user', session_user,
        'log_id', v_log_id,
        'billable_arrangement', false
    );
end;
$$;

-- The single grant. Note the explicit revoke from service_role: the bootstrap
-- and a live Supabase project both carry ALTER DEFAULT PRIVILEGES granting ALL
-- on new functions to anon/authenticated/service_role, so a newly created
-- function is EXECUTE-able by a leaked service key until this line runs.
revoke all on function public.attest_billing_arrangement(uuid, text, text, text, uuid)
    from public, anon, authenticated, service_role;
revoke all on function public.revoke_billing_arrangement(uuid, text)
    from public, anon, authenticated, service_role;
grant execute on function public.attest_billing_arrangement(uuid, text, text, text, uuid)
    to brevitas_attestor;
grant execute on function public.revoke_billing_arrangement(uuid, text)
    to brevitas_attestor;

comment on function public.attest_billing_arrangement(uuid, text, text, text, uuid) is
    'THE attestation writer. Replaces hand-written DML against '
    'public.organization_billing_arrangement (202607280009), which grants '
    'INSERT/UPDATE/DELETE to no role at all and still does. Executable only by '
    'brevitas_attestor -- enforced twice, by the EXECUTE grant and by a '
    'session_user check inside the body that survives a mistaken future GRANT. '
    'Requires a named attester and non-empty cited evidence, and at least 8 '
    'characters of evidence plus an open public.billing_arrangement_request for '
    'the only billable value, marginal_per_call. Appends to '
    'public.organization_billing_arrangement_log, which nobody may UPDATE or '
    'DELETE. Enforces that evidence was cited; it cannot enforce that it is true.';
comment on function public.revoke_billing_arrangement(uuid, text) is
    'Returns an organization to the unattested (unbillable) state and logs why. '
    'Same single grant and same session_user check as '
    'public.attest_billing_arrangement.';

-- ---------------------------------------------------------------------------
-- Contract. Everything below is checked at apply time and the DO block leaves
-- no rows behind.
--
-- NOTE ON COVERAGE: the SUCCESSFUL attestation path cannot be exercised here.
-- It requires session_user = 'brevitas_attestor', and reaching that inside a
-- migration needs SET SESSION AUTHORIZATION, which is superuser-only and is not
-- available to the role that applies migrations on a hosted Supabase project.
-- The positive path -- attest, log row written, request marked attested, the
-- 202607280009 guard changing its answer, then revoke -- is proven in
-- scripts/ci/migration-attestation-surface-assertions.sql, which runs against a
-- local superuser. What IS proven here is every refusal and every grant, which
-- are the properties that must hold on the production project.
-- ---------------------------------------------------------------------------
do $attestation_writer_contract$
declare
    v_message text;
    v_role record;
    v_settlement_source text;
    v_probe_org uuid;
    v_probe_request uuid;
    v_refused boolean;
begin
    ----------------------------------------------------------------------
    -- 1. 202607280009's guarantee, re-asserted here rather than assumed.
    ----------------------------------------------------------------------
    if has_table_privilege('service_role', 'public.organization_billing_arrangement', 'INSERT')
       or has_table_privilege('service_role', 'public.organization_billing_arrangement', 'UPDATE')
       or has_table_privilege('service_role', 'public.organization_billing_arrangement', 'DELETE')
       or has_table_privilege('anon', 'public.organization_billing_arrangement', 'INSERT')
       or has_table_privilege('authenticated', 'public.organization_billing_arrangement', 'INSERT')
       or has_table_privilege('brevitas_attestor',
              'public.organization_billing_arrangement', 'INSERT')
       or has_table_privilege('brevitas_attestor',
              'public.organization_billing_arrangement', 'UPDATE')
       or has_table_privilege('brevitas_attestor',
              'public.organization_billing_arrangement', 'DELETE') then
        raise exception '202607280030 granted a direct write on the attestation table; '
                        'the RPC must be the only writer and 202607280009''s revoke must hold';
    end if;

    ----------------------------------------------------------------------
    -- 2. No runtime role may reach either function.
    ----------------------------------------------------------------------
    for v_role in
        select unnest(array['public', 'anon', 'authenticated', 'service_role']) as name
    loop
        if has_function_privilege(v_role.name,
               'public.attest_billing_arrangement(uuid,text,text,text,uuid)', 'EXECUTE')
           or has_function_privilege(v_role.name,
               'public.revoke_billing_arrangement(uuid,text)', 'EXECUTE') then
            raise exception
                '202607280030 left the attestation writer executable by %; a leaked '
                'service key could attest an organization into billability', v_role.name;
        end if;
    end loop;

    if not has_function_privilege('brevitas_attestor',
           'public.attest_billing_arrangement(uuid,text,text,text,uuid)', 'EXECUTE')
       or not has_function_privilege('brevitas_attestor',
           'public.revoke_billing_arrangement(uuid,text)', 'EXECUTE') then
        raise exception '202607280030 did not leave the attestation writer callable by '
                        'the one role that is supposed to hold it';
    end if;

    -- The capture RPC is the mirror image: the API must reach it, browsers must
    -- not, and the attestor role has no business calling it.
    if has_function_privilege('anon',
           'public.open_billing_arrangement_request(uuid,uuid,text,text,text,text,text)',
           'EXECUTE')
       or has_function_privilege('authenticated',
           'public.open_billing_arrangement_request(uuid,uuid,text,text,text,text,text)',
           'EXECUTE')
       or has_function_privilege('brevitas_attestor',
           'public.open_billing_arrangement_request(uuid,uuid,text,text,text,text,text)',
           'EXECUTE') then
        raise exception '202607280030 exposed the billing-request capture RPC beyond the API';
    end if;
    if not has_function_privilege('service_role',
           'public.open_billing_arrangement_request(uuid,uuid,text,text,text,text,text)',
           'EXECUTE') then
        raise exception '202607280030 left the API unable to record a billing request';
    end if;

    -- And it must not be a back door into the attestation itself.
    if position('organization_billing_arrangement ' in pg_get_functiondef(to_regprocedure(
           'public.open_billing_arrangement_request(uuid,uuid,text,text,text,text,text)'))) <> 0
    then
        raise exception '202607280030: the customer-facing capture RPC touches the '
                        'attestation table; it must only ever write paperwork';
    end if;

    ----------------------------------------------------------------------
    -- 3. The session_user check fires, and it fires FIRST. This DO block runs
    --    as whoever applied the migration, which is never brevitas_attestor, so
    --    the refusal is live-tested here rather than merely read off a grant.
    --
    --    The probe attests a REAL organization with entirely valid arguments,
    --    created and deleted inside this block, precisely so that removing the
    --    session_user check makes this probe SUCCEED rather than fail on some
    --    unrelated validation error. Catching `others` and insisting on the
    --    attestation_denied marker is what makes "refused" mean "refused for
    --    being the wrong caller" instead of "refused for any reason at all".
    ----------------------------------------------------------------------
    insert into public.organizations (name)
    values ('202607280030 attestation writer contract probe')
    returning id into v_probe_org;

    insert into public.billing_arrangement_request
        (organization_id, self_declared_arrangement, agreement_reference)
    values (v_probe_org, 'marginal_per_call', '202607280030 contract probe')
    returning id into v_probe_request;

    v_refused := false;
    begin
        perform public.attest_billing_arrangement(
            v_probe_org, 'marginal_per_call', '202607280030 contract probe',
            '202607280030 contract probe evidence', v_probe_request
        );
    exception
        when others then
            get stacked diagnostics v_message = message_text;
            v_refused := position('attestation_denied' in v_message) <> 0;
            if not v_refused then
                raise exception '202607280030 refused for the wrong reason: %', v_message;
            end if;
    end;
    if not v_refused then
        raise exception '202607280030 attested an organization from a non-attestor session';
    end if;

    v_refused := false;
    begin
        perform public.revoke_billing_arrangement(
            v_probe_org, '202607280030 contract probe reason'
        );
    exception
        when others then
            get stacked diagnostics v_message = message_text;
            v_refused := position('attestation_denied' in v_message) <> 0;
            if not v_refused then
                raise exception '202607280030 revoke refused for the wrong reason: %', v_message;
            end if;
    end;
    if not v_refused then
        raise exception '202607280030 revoked an attestation from a non-attestor session';
    end if;

    -- Neither probe may have written anything anywhere.
    if public.organization_billing_arrangement_state(v_probe_org) <> 'unattested' then
        raise exception '202607280030 contract probe changed an arrangement state';
    end if;
    if exists (
        select 1 from public.organization_billing_arrangement_log
         where organization_id = v_probe_org
    ) then
        raise exception '202607280030 contract probe wrote an attestation log row';
    end if;

    delete from public.billing_arrangement_request where organization_id = v_probe_org;
    delete from public.organizations where id = v_probe_org;

    ----------------------------------------------------------------------
    -- 4. The log is append-only for every role, and invisible to browsers.
    ----------------------------------------------------------------------
    for v_role in
        select unnest(array['public', 'anon', 'authenticated',
                            'service_role', 'brevitas_attestor']) as name
    loop
        if has_table_privilege(v_role.name,
               'public.organization_billing_arrangement_log', 'UPDATE')
           or has_table_privilege(v_role.name,
               'public.organization_billing_arrangement_log', 'DELETE')
           or has_table_privilege(v_role.name,
               'public.organization_billing_arrangement_log', 'TRUNCATE')
           or has_table_privilege(v_role.name,
               'public.organization_billing_arrangement_log', 'INSERT') then
            raise exception
                '202607280030 let % mutate the attestation log; it is insert-only and the '
                'SECURITY DEFINER function is its only writer', v_role.name;
        end if;
    end loop;
    if has_table_privilege('anon', 'public.organization_billing_arrangement_log', 'SELECT')
       or has_table_privilege('authenticated',
              'public.organization_billing_arrangement_log', 'SELECT')
       or has_table_privilege('brevitas_attestor',
              'public.organization_billing_arrangement_log', 'SELECT') then
        raise exception '202607280030 exposed the attestation log beyond service_role';
    end if;
    if not has_table_privilege('service_role',
           'public.organization_billing_arrangement_log', 'SELECT') then
        raise exception '202607280030 left the attestation log unreadable by the API';
    end if;

    ----------------------------------------------------------------------
    -- 5. The customer-facing request table: writable by the API, invisible to
    --    browsers, and NOT mutable by the API (only the RPC closes a request).
    ----------------------------------------------------------------------
    if has_table_privilege('anon', 'public.billing_arrangement_request', 'SELECT')
       or has_table_privilege('authenticated', 'public.billing_arrangement_request', 'SELECT')
       or has_table_privilege('anon', 'public.billing_arrangement_request', 'INSERT')
       or has_table_privilege('authenticated', 'public.billing_arrangement_request', 'INSERT') then
        raise exception '202607280030 exposed billing-arrangement requests to a browser role';
    end if;
    if has_table_privilege('service_role', 'public.billing_arrangement_request', 'UPDATE')
       or has_table_privilege('service_role', 'public.billing_arrangement_request', 'DELETE') then
        raise exception '202607280030 let the API close its own billing-arrangement request';
    end if;
    if not has_table_privilege('service_role', 'public.billing_arrangement_request', 'INSERT')
       or not has_table_privilege('service_role', 'public.billing_arrangement_request', 'SELECT') then
        raise exception '202607280030 left the API unable to record a billing request';
    end if;

    ----------------------------------------------------------------------
    -- 6. The request table is INERT. If any settlement-path function ever
    --    starts reading it, a customer-writable row becomes an input to the
    --    money path and this migration's central claim is false.
    ----------------------------------------------------------------------
    v_settlement_source := concat_ws(
        E'\n',
        pg_get_functiondef(to_regprocedure(
            'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)')),
        pg_get_functiondef(to_regprocedure(
            'public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)')),
        pg_get_functiondef(to_regprocedure('public.organization_billing_arrangement_state(uuid)')),
        case
            when to_regprocedure(
                'public.settle_billing_period(uuid,timestamptz,text,boolean)') is not null
            then pg_get_functiondef(to_regprocedure(
                'public.settle_billing_period(uuid,timestamptz,text,boolean)'))
            else ''
        end
    );
    if position('billing_arrangement_request' in v_settlement_source) <> 0 then
        raise exception '202607280030: a settlement-path function reads '
                        'billing_arrangement_request; customer-declared paperwork must '
                        'never be an input to the money path';
    end if;

    ----------------------------------------------------------------------
    -- 7. The operator role is a key, not an account: no superuser, no bypassrls,
    --    no inherited privilege, and no direct table access anywhere.
    ----------------------------------------------------------------------
    if exists (
        select 1 from pg_roles
         where rolname = 'brevitas_attestor'
           and (rolsuper or rolbypassrls or rolcreaterole or rolcreatedb or rolinherit)
    ) then
        raise exception '202607280030 created brevitas_attestor with privileges beyond '
                        'executing the attestation RPC';
    end if;
    if exists (
        select 1
          from information_schema.table_privileges
         where grantee = 'brevitas_attestor'
           and table_schema = 'public'
    ) then
        raise exception '202607280030 left brevitas_attestor holding direct table '
                        'privileges; the RPC must be its only reach into the schema';
    end if;

    ----------------------------------------------------------------------
    -- 8. And the whole point: an organization nobody has attested still cannot
    --    settle a positive fee. This migration must not have made anything
    --    billable by existing.
    ----------------------------------------------------------------------
    begin
        perform public.assert_billing_period_settlement_allowed(
            gen_random_uuid(),
            '2026-07-15 10:00:00+00'::timestamptz,
            '2026-07-22 10:00:00+00'::timestamptz,
            1::bigint
        );
        raise exception '202607280030: an unattested organization settled a positive fee';
    exception
        when sqlstate '55000' then
            get stacked diagnostics v_message = message_text;
            if position('unattested_billing_arrangement' in v_message) = 0 then
                raise exception '202607280030: unattested settlement halted for the wrong '
                                'reason: %', v_message;
            end if;
    end;
end;
$attestation_writer_contract$;

commit;
