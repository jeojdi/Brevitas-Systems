\set ON_ERROR_STOP on

-- Behavioural contract for the billing-arrangement attestation surface:
--   202607280009_billing_arrangement_attestation.sql  (the fail-closed table)
--   202607280030_billing_attestation_writer.sql       (the operator write path)
--
-- WHY THIS FILE EXISTS SEPARATELY FROM THE MIGRATION'S OWN DO BLOCK.
--
-- 202607280030 self-verifies every refusal and every grant, because those are
-- the properties that must hold on the production project. It CANNOT verify the
-- successful attestation path: that requires session_user = 'brevitas_attestor',
-- which needs SET SESSION AUTHORIZATION, which is superuser-only and is not
-- available to the role that applies migrations on a hosted Supabase project.
--
-- This harness runs against a local superuser, so this file is the only place
-- where the operator path is actually executed rather than described. It proves
-- the four things the lane exists to prove:
--
--   1. the operator path CAN attest (section 3),
--   2. anon / authenticated / service_role CANNOT -- both because the grant is
--      absent AND, with the grant forcibly restored, because the body still
--      refuses (sections 2 and 6),
--   3. the recorded evidence is non-empty, and the identity written into the
--      log is the one the database observed, not the one the caller typed
--      (sections 4 and 3),
--   4. an organization with no attestation still cannot settle a positive fee,
--      before attestation and again after revocation (sections 1, 5 and 7).
--
-- Everything runs inside one transaction that ends in ROLLBACK, so the file is
-- re-runnable at any point in the replay and leaves no financial rows behind --
-- which matters more here than elsewhere, because
-- public.organization_billing_arrangement_log is deliberately built so that
-- nothing can delete from it.
--
-- ROLE-SWITCHING IDIOM. Two different switches are used and they are not
-- interchangeable:
--   * `set local role X` changes current_user only, which is what PostgreSQL
--     checks for EXECUTE privilege. Used to prove the grant is absent.
--   * `set local session authorization X` changes session_user too, which is
--     what the function body checks. Used to prove the body's own refusal and
--     to reach the success path at all.
-- A test that only used `set local role` would prove nothing about the body,
-- because the privilege error fires first.

begin;

------------------------------------------------------------------
-- Section 0. Structure. Asserted independently of the migration's own DO block
-- so that a half-applied chain fails here with a clear message instead of
-- failing later with a confusing one.
------------------------------------------------------------------
do $$
begin
    if to_regclass('public.organization_billing_arrangement') is null then
        raise exception 'public.organization_billing_arrangement is missing (202607280009 '
                        'not applied)';
    end if;
    if to_regclass('public.organization_billing_arrangement_log') is null
       or to_regclass('public.billing_arrangement_request') is null then
        raise exception 'the attestation writer tables are missing (202607280030 not applied)';
    end if;
    if to_regprocedure('public.attest_billing_arrangement(uuid,text,text,text,uuid)') is null
       or to_regprocedure('public.revoke_billing_arrangement(uuid,text)') is null then
        raise exception 'the attestation writer functions are missing (202607280030 not applied)';
    end if;
    if not exists (select 1 from pg_roles where rolname = 'brevitas_attestor') then
        raise exception 'the brevitas_attestor operator role is missing (202607280030 not '
                        'applied)';
    end if;
    -- The operator role must be a key, not an account.
    if exists (
        select 1 from pg_roles
         where rolname = 'brevitas_attestor'
           and (rolsuper or rolbypassrls or rolcreaterole or rolcreatedb or rolinherit)
    ) then
        raise exception 'brevitas_attestor holds privileges beyond executing the attestation RPC';
    end if;
    if exists (
        select 1 from information_schema.table_privileges
         where grantee = 'brevitas_attestor' and table_schema = 'public'
    ) then
        raise exception 'brevitas_attestor holds direct table privileges; the RPC must be '
                        'its only reach into the schema';
    end if;
end;
$$;

------------------------------------------------------------------
-- Section 1. The fail-closed default, live. Before anything is attested, a
-- positive fee must be refused by name.
--
-- This is asserted FIRST and again in section 5 and section 7, because it is
-- the property everything else is in service of: if this ever passes silently,
-- every other assertion in this file is decoration.
------------------------------------------------------------------
do $$
declare
    v_org uuid := 'a77e5701-0000-4000-8000-000000000001';
    v_other_org uuid := 'a77e5701-0000-4000-8000-000000000002';
    v_start constant timestamptz := '2026-07-15 10:00:00+00';
    v_end constant timestamptz := '2026-07-22 10:00:00+00';
    v_message text;
    v_raised boolean;
begin
    insert into public.organizations (id, name)
    values (v_org, '202607280030 attestation surface probe'),
           (v_other_org, '202607280030 attestation surface neighbour'),
           -- Never attested by anything below; section 9 uses it to show that
           -- customer-side paperwork alone moves nothing.
           ('a77e5701-0000-4000-8000-000000000003',
            '202607280030 attestation surface paperwork-only');

    if public.organization_billing_arrangement_state(v_org) <> 'unattested' then
        raise exception 'a brand new organization did not default to unattested';
    end if;

    v_raised := false;
    begin
        perform public.assert_billing_period_settlement_allowed(v_org, v_start, v_end, 1::bigint);
    exception
        when sqlstate '55000' then
            get stacked diagnostics v_message = message_text;
            if position('unattested_billing_arrangement' in v_message) = 0 then
                raise exception 'unattested settlement halted for the wrong reason: %', v_message;
            end if;
            v_raised := true;
    end;
    if not v_raised then
        raise exception 'an organization with no attestation settled a positive fee';
    end if;
end;
$$;

------------------------------------------------------------------
-- Section 2. No runtime role can reach the writer.
--
-- Two layers, both checked: the introspected grant, and an actual call under
-- `set local role`. The call is what proves the grant is the operative one --
-- has_function_privilege() would keep returning false even if the function had
-- been dropped and replaced by something permissive.
------------------------------------------------------------------
do $$
declare
    -- A REAL organization, so that the direct-write probe below fails on
    -- privilege and nothing else. Pointing it at a random uuid would let a
    -- foreign-key error masquerade as the guarantee holding.
    v_org uuid := 'a77e5701-0000-4000-8000-000000000001';
    v_role text;
    v_denied boolean;
begin
    foreach v_role in array array['public', 'anon', 'authenticated', 'service_role']
    loop
        if has_function_privilege(v_role,
               'public.attest_billing_arrangement(uuid,text,text,text,uuid)', 'EXECUTE')
           or has_function_privilege(v_role,
               'public.revoke_billing_arrangement(uuid,text)', 'EXECUTE') then
            raise exception 'the attestation writer is executable by %', v_role;
        end if;
    end loop;

    foreach v_role in array array['anon', 'authenticated', 'service_role']
    loop
        v_denied := false;
        execute format('set local role %I', v_role);
        begin
            perform public.attest_billing_arrangement(
                gen_random_uuid(), 'marginal_per_call', 'impostor',
                'impostor evidence', gen_random_uuid());
        exception
            when insufficient_privilege then v_denied := true;
        end;
        reset role;
        if not v_denied then
            raise exception 'role % executed public.attest_billing_arrangement', v_role;
        end if;

        v_denied := false;
        execute format('set local role %I', v_role);
        begin
            perform public.revoke_billing_arrangement(gen_random_uuid(), 'impostor reason');
        exception
            when insufficient_privilege then v_denied := true;
        end;
        reset role;
        if not v_denied then
            raise exception 'role % executed public.revoke_billing_arrangement', v_role;
        end if;
    end loop;

    -- And the table underneath is still unwritable directly, which is
    -- 202607280009's guarantee and the reason the RPC has to exist at all.
    v_denied := false;
    set local role service_role;
    begin
        insert into public.organization_billing_arrangement
            (organization_id, arrangement, attested_by)
        values (v_org, 'marginal_per_call', 'service_role');
    exception
        when insufficient_privilege then v_denied := true;
    end;
    reset role;
    if not v_denied then
        raise exception 'service_role attested organization % into billability directly, '
                        'bypassing the RPC and the log', v_org;
    end if;
end;
$$;

------------------------------------------------------------------
-- Section 3. The operator path CAN attest -- the only place in the repository
-- where a successful attestation is executed.
--
-- Also proves the part of the log row the attester does not control:
-- attested_by is free text the caller typed, attested_session_user is captured
-- by the function from the database session. They are deliberately given
-- different values here so a body that echoed the caller's string into both
-- would be caught.
------------------------------------------------------------------
do $$
declare
    v_org uuid := 'a77e5701-0000-4000-8000-000000000001';
    v_request uuid;
    v_result jsonb;
    v_log record;
    v_request_status text;
begin
    insert into public.billing_arrangement_request
        (organization_id, self_declared_arrangement, agreement_reference,
         rate_presented, terms_version)
    values (v_org, 'marginal_per_call', 'MSA 2026-08-03 sec 4',
            '25% of verified savings', 'terms-2026-07')
    returning id into v_request;

    set local session authorization brevitas_attestor;

    if session_user <> 'brevitas_attestor' then
        raise exception 'the attestor session did not take effect (session_user=%)', session_user;
    end if;

    v_result := public.attest_billing_arrangement(
        v_org,
        'marginal_per_call',
        'Dana Okafor (Brevitas, contracts)',
        'MSA 2026-08-03 sec 4; DocuSign envelope 3f2a91c4',
        v_request
    );

    reset session authorization;

    if v_result->>'arrangement' <> 'marginal_per_call'
       or v_result->>'prior_arrangement' <> 'unattested'
       or (v_result->>'billable_arrangement')::boolean is not true
       or v_result->>'attested_session_user' <> 'brevitas_attestor' then
        raise exception 'attestation returned the wrong result: %', v_result;
    end if;

    if public.organization_billing_arrangement_state(v_org) <> 'marginal_per_call' then
        raise exception 'attestation did not change the organization''s arrangement state';
    end if;

    select * into v_log
      from public.organization_billing_arrangement_log
     where organization_id = v_org and action = 'attest'
     order by id desc
     limit 1;
    if not found then
        raise exception 'attestation wrote no log row';
    end if;
    if v_log.attested_by <> 'Dana Okafor (Brevitas, contracts)' then
        raise exception 'the log did not record who attested: %', v_log.attested_by;
    end if;
    if v_log.attested_session_user <> 'brevitas_attestor' then
        raise exception 'the log recorded a session identity the caller supplied: %',
            v_log.attested_session_user;
    end if;
    if v_log.attested_by = v_log.attested_session_user then
        raise exception 'the log conflated the named attester with the session identity';
    end if;
    if char_length(btrim(v_log.attested_evidence)) < 8
       or position('DocuSign envelope 3f2a91c4' in v_log.attested_evidence) = 0 then
        raise exception 'the log did not record the agreement that was cited: %',
            v_log.attested_evidence;
    end if;
    if v_log.prior_arrangement <> 'unattested' or v_log.new_arrangement <> 'marginal_per_call' then
        raise exception 'the log did not record the transition: % -> %',
            v_log.prior_arrangement, v_log.new_arrangement;
    end if;
    if v_log.request_id is distinct from v_request then
        raise exception 'the log did not record the request the attestation answers';
    end if;

    -- The customer-facing request is closed by the attestation, and closed only
    -- by it: service_role holds no UPDATE on that table.
    select status into v_request_status
      from public.billing_arrangement_request where id = v_request;
    if v_request_status <> 'attested' then
        raise exception 'the billing-arrangement request was left %', v_request_status;
    end if;

    -- ...and it cannot be spent twice.
    set local session authorization brevitas_attestor;
    begin
        perform public.attest_billing_arrangement(
            v_org, 'marginal_per_call', 'Dana Okafor (Brevitas, contracts)',
            'MSA 2026-08-03 sec 4; DocuSign envelope 3f2a91c4', v_request);
        reset session authorization;
        raise exception 'an already-attested request was reused for a second attestation';
    exception
        when sqlstate '55000' then
            reset session authorization;
    end;
end;
$$;

------------------------------------------------------------------
-- Section 4. Evidence and identity are required, and the vocabulary is not
-- extended. Every case here runs AS the attestor, so each refusal is the body's
-- own validation and not a privilege error wearing a disguise.
------------------------------------------------------------------
do $$
declare
    v_org uuid := 'a77e5701-0000-4000-8000-000000000002';
    v_other_org uuid := 'a77e5701-0000-4000-8000-000000000001';
    v_request uuid;
    v_foreign_request uuid;
    v_raised boolean;
    v_message text;
begin
    insert into public.billing_arrangement_request
        (organization_id, self_declared_arrangement)
    values (v_org, 'marginal_per_call')
    returning id into v_request;

    insert into public.billing_arrangement_request
        (organization_id, self_declared_arrangement)
    values (v_other_org, 'marginal_per_call')
    returning id into v_foreign_request;

    set local session authorization brevitas_attestor;

    -- Empty evidence, on the non-billable values too: an attestation nobody can
    -- audit later is not an attestation. 202607280009's table would have
    -- accepted this (attested_evidence defaults to ''); the writer will not.
    v_raised := false;
    begin
        perform public.attest_billing_arrangement(v_org, 'unknown', 'Dana Okafor', '   ', null);
    exception when sqlstate '22023' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'an attestation was accepted with blank evidence';
    end if;

    -- Short evidence is allowed for the unbillable values and refused for the
    -- one that permits a fee.
    v_raised := false;
    begin
        perform public.attest_billing_arrangement(
            v_org, 'marginal_per_call', 'Dana Okafor', 'MSA', v_request);
    exception when sqlstate '22023' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'marginal_per_call was attested on 3 characters of evidence';
    end if;

    -- An unnamed attester.
    v_raised := false;
    begin
        perform public.attest_billing_arrangement(
            v_org, 'marginal_per_call', '  ', 'MSA 2026-08-03 sec 4', v_request);
    exception when sqlstate '22023' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'an attestation was accepted with no named attester';
    end if;

    -- The billable value requires the customer to have asked.
    v_raised := false;
    begin
        perform public.attest_billing_arrangement(
            v_org, 'marginal_per_call', 'Dana Okafor', 'MSA 2026-08-03 sec 4', null);
    exception when sqlstate '22023' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'marginal_per_call was attested with no billing-arrangement request';
    end if;

    -- ...and it must be that customer's request, not somebody else's.
    v_raised := false;
    begin
        perform public.attest_billing_arrangement(
            v_org, 'marginal_per_call', 'Dana Okafor', 'MSA 2026-08-03 sec 4',
            v_foreign_request);
    exception when sqlstate '22023' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'an organization was attested against another organization''s request';
    end if;

    -- The vocabulary is 202607280009's and is closed.
    v_raised := false;
    begin
        perform public.attest_billing_arrangement(
            v_org, 'enterprise_handshake', 'Dana Okafor', 'MSA 2026-08-03 sec 4', v_request);
    exception when sqlstate '22023' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'an unrecognised billing arrangement was accepted';
    end if;

    -- An organization that does not exist.
    v_raised := false;
    begin
        perform public.attest_billing_arrangement(
            gen_random_uuid(), 'unknown', 'Dana Okafor', 'no such organization', null);
    exception when sqlstate '23503' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'an attestation was accepted for a nonexistent organization';
    end if;

    -- Dropped back to the harness identity to read the state: brevitas_attestor
    -- holds EXECUTE on the two writer functions and on NOTHING else, not even
    -- 202607280009's read-only state function. That is the intended shape of the
    -- role and this line is where it shows.
    reset session authorization;

    -- Nothing above may have written anything.
    if public.organization_billing_arrangement_state(v_org) <> 'unattested' then
        raise exception 'a refused attestation still changed the arrangement state';
    end if;

    -- The unbillable values ARE attestable, with short evidence and no request:
    -- recording "we looked, it is committed capacity" must stay cheap, because
    -- that is the value the system most needs written down.
    set local session authorization brevitas_attestor;
    perform public.attest_billing_arrangement(
        v_org, 'committed_capacity', 'Dana Okafor', 'PTU order form', null);
    reset session authorization;

    if public.organization_billing_arrangement_state(v_org) <> 'committed_capacity' then
        raise exception 'committed_capacity was not recorded';
    end if;
    if not exists (
        select 1 from public.organization_billing_arrangement_log
         where organization_id = v_org and new_arrangement = 'committed_capacity'
    ) then
        raise exception 'committed_capacity attestation wrote no log row';
    end if;
end;
$$;

------------------------------------------------------------------
-- Section 5. Attestation moves exactly one halting condition and no more.
--
-- The attested organization from section 3 has no usage at all, so a positive
-- fee must STILL be refused -- but now by an arithmetic condition, not by the
-- attestation. The committed_capacity organization from section 4 must still be
-- refused BY the attestation. Together these prove the wrapper is delegating
-- rather than short-circuiting.
------------------------------------------------------------------
do $$
declare
    v_attested_org uuid := 'a77e5701-0000-4000-8000-000000000001';
    v_capacity_org uuid := 'a77e5701-0000-4000-8000-000000000002';
    v_start constant timestamptz := '2026-07-15 10:00:00+00';
    v_end constant timestamptz := '2026-07-22 10:00:00+00';
    v_message text;
    v_raised boolean;
    v_result jsonb;
begin
    v_raised := false;
    begin
        perform public.assert_billing_period_settlement_allowed(
            v_attested_org, v_start, v_end, 1::bigint);
    exception
        when sqlstate '55000' then
            get stacked diagnostics v_message = message_text;
            if position('unattested_billing_arrangement' in v_message) <> 0 then
                raise exception 'an attested organization was still reported unattested: %',
                    v_message;
            end if;
            v_raised := true;
    end;
    if not v_raised then
        raise exception 'attestation alone permitted a fee against no savings at all';
    end if;

    v_result := public.assert_billing_period_settlement_allowed(
        v_attested_org, v_start, v_end, 0::bigint);
    if v_result->>'billing_arrangement' <> 'marginal_per_call' then
        raise exception 'the settlement guard does not see the attestation: %', v_result;
    end if;

    v_raised := false;
    begin
        perform public.assert_billing_period_settlement_allowed(
            v_capacity_org, v_start, v_end, 1::bigint);
    exception
        when sqlstate '55000' then
            get stacked diagnostics v_message = message_text;
            if position('unattested_billing_arrangement' in v_message) = 0 then
                raise exception 'committed_capacity halted for the wrong reason: %', v_message;
            end if;
            v_raised := true;
    end;
    if not v_raised then
        raise exception 'a committed-capacity organization settled a positive fee';
    end if;
end;
$$;

------------------------------------------------------------------
-- Section 6. Defence in depth: the body refuses even WITH the grant.
--
-- This simulates the one mistake the EXECUTE grant cannot survive -- a future
-- migration, or a hurried operator, running `grant execute ... to service_role`
-- -- and proves the function still refuses. The grant is made and dropped
-- inside this transaction, which rolls back regardless.
--
-- `set local session authorization` is required here rather than `set local
-- role`: with the grant restored, current_user no longer stops the call, so
-- only session_user distinguishes the impostor.
------------------------------------------------------------------
do $$
declare
    v_org uuid := 'a77e5701-0000-4000-8000-000000000001';
    v_request uuid;
    v_message text;
    v_raised boolean;
begin
    -- Reuse the open request section 4 left on this organization rather than
    -- opening a second one: the partial unique index deliberately permits only
    -- one open request per organization, and section 9 is where that is asserted.
    select id into v_request
      from public.billing_arrangement_request
     where organization_id = v_org and status = 'open'
     limit 1;
    if v_request is null then
        raise exception 'section 6 expected an open billing-arrangement request to attack';
    end if;

    grant execute on function public.attest_billing_arrangement(uuid,text,text,text,uuid)
        to service_role;
    grant execute on function public.revoke_billing_arrangement(uuid,text)
        to service_role;

    set local session authorization service_role;

    v_raised := false;
    begin
        perform public.attest_billing_arrangement(
            v_org, 'marginal_per_call', 'leaked service key',
            'a plausible looking agreement reference', v_request);
    exception
        when insufficient_privilege then
            get stacked diagnostics v_message = message_text;
            if position('attestation_denied' in v_message) = 0 then
                raise exception 'the body refused for the wrong reason: %', v_message;
            end if;
            v_raised := true;
    end;
    if not v_raised then
        raise exception 'a mistaken GRANT to service_role was enough to attest an '
                        'organization into billability';
    end if;

    v_raised := false;
    begin
        perform public.revoke_billing_arrangement(v_org, 'leaked service key revocation');
    exception
        when insufficient_privilege then v_raised := true;
    end;
    if not v_raised then
        raise exception 'a mistaken GRANT to service_role was enough to de-attest an '
                        'organization';
    end if;

    reset session authorization;

    revoke execute on function public.attest_billing_arrangement(uuid,text,text,text,uuid)
        from service_role;
    revoke execute on function public.revoke_billing_arrangement(uuid,text)
        from service_role;

    if public.organization_billing_arrangement_state(v_org) <> 'marginal_per_call' then
        raise exception 'the impostor changed the arrangement state';
    end if;
    if exists (
        select 1 from public.organization_billing_arrangement_log
         where attested_session_user <> 'brevitas_attestor'
    ) then
        raise exception 'a non-attestor session wrote an attestation log row';
    end if;
end;
$$;

------------------------------------------------------------------
-- Section 7. Revocation returns the organization to unbillable, records why,
-- and the fail-closed refusal from section 1 comes back.
------------------------------------------------------------------
do $$
declare
    v_org uuid := 'a77e5701-0000-4000-8000-000000000001';
    v_start constant timestamptz := '2026-07-15 10:00:00+00';
    v_end constant timestamptz := '2026-07-22 10:00:00+00';
    v_result jsonb;
    v_message text;
    v_raised boolean;
begin
    set local session authorization brevitas_attestor;

    -- A reason is mandatory: the log row is the only record of why an
    -- organization stopped being billable.
    v_raised := false;
    begin
        perform public.revoke_billing_arrangement(v_org, 'oops');
    exception when sqlstate '22023' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'an attestation was revoked with no stated reason';
    end if;

    v_result := public.revoke_billing_arrangement(
        v_org, 'MSA terminated 2026-09-01; customer moved to committed capacity');

    reset session authorization;

    if v_result->>'prior_arrangement' <> 'marginal_per_call'
       or v_result->>'arrangement' <> 'unattested'
       or (v_result->>'billable_arrangement')::boolean is not false then
        raise exception 'revocation returned the wrong result: %', v_result;
    end if;
    if public.organization_billing_arrangement_state(v_org) <> 'unattested' then
        raise exception 'revocation did not return the organization to unattested';
    end if;
    if not exists (
        select 1 from public.organization_billing_arrangement_log
         where organization_id = v_org
           and action = 'revoke'
           and prior_arrangement = 'marginal_per_call'
           and position('MSA terminated' in attested_evidence) <> 0
           and attested_session_user = 'brevitas_attestor'
    ) then
        raise exception 'revocation wrote no auditable log row';
    end if;

    v_raised := false;
    begin
        perform public.assert_billing_period_settlement_allowed(v_org, v_start, v_end, 1::bigint);
    exception
        when sqlstate '55000' then
            get stacked diagnostics v_message = message_text;
            if position('unattested_billing_arrangement' in v_message) = 0 then
                raise exception 'a revoked organization halted for the wrong reason: %',
                    v_message;
            end if;
            v_raised := true;
    end;
    if not v_raised then
        raise exception 'a revoked organization still settled a positive fee';
    end if;
end;
$$;

------------------------------------------------------------------
-- Section 8. The log is append-only for everyone, including the operator role,
-- and the history written above cannot be edited away.
--
-- The attestation log is the only audit trail the money path has. If any role
-- can rewrite it, "who attested this organization" has no answer.
------------------------------------------------------------------
do $$
declare
    v_role text;
    v_denied boolean;
    v_before bigint;
begin
    select count(*) into v_before from public.organization_billing_arrangement_log;
    if v_before = 0 then
        raise exception 'the sections above wrote no log rows; section 8 would be vacuous';
    end if;

    foreach v_role in array array['public', 'anon', 'authenticated',
                                  'service_role', 'brevitas_attestor']
    loop
        if has_table_privilege(v_role, 'public.organization_billing_arrangement_log', 'INSERT')
           or has_table_privilege(v_role, 'public.organization_billing_arrangement_log', 'UPDATE')
           or has_table_privilege(v_role, 'public.organization_billing_arrangement_log', 'DELETE')
           or has_table_privilege(v_role, 'public.organization_billing_arrangement_log', 'TRUNCATE')
        then
            raise exception 'role % can mutate the attestation log', v_role;
        end if;
    end loop;

    foreach v_role in array array['anon', 'authenticated', 'service_role']
    loop
        v_denied := false;
        execute format('set local role %I', v_role);
        begin
            update public.organization_billing_arrangement_log
               set attested_by = 'rewritten', attested_evidence = 'rewritten';
        exception when insufficient_privilege then v_denied := true;
        end;
        reset role;
        if not v_denied then
            raise exception 'role % rewrote the attestation log', v_role;
        end if;

        v_denied := false;
        execute format('set local role %I', v_role);
        begin
            delete from public.organization_billing_arrangement_log;
        exception when insufficient_privilege then v_denied := true;
        end;
        reset role;
        if not v_denied then
            raise exception 'role % deleted attestation history', v_role;
        end if;
    end loop;

    -- The operator role too: it may attest, and it may not edit the record of
    -- having attested.
    v_denied := false;
    set local session authorization brevitas_attestor;
    begin
        delete from public.organization_billing_arrangement_log;
    exception when insufficient_privilege then v_denied := true;
    end;
    reset session authorization;
    if not v_denied then
        raise exception 'brevitas_attestor deleted its own attestation history';
    end if;

    v_denied := false;
    set local session authorization brevitas_attestor;
    begin
        update public.organization_billing_arrangement
           set arrangement = 'marginal_per_call'
         where true;
    exception when insufficient_privilege then v_denied := true;
    end;
    reset session authorization;
    if not v_denied then
        raise exception 'brevitas_attestor wrote the attestation table directly, bypassing '
                        'the log';
    end if;

    if (select count(*) from public.organization_billing_arrangement_log) <> v_before then
        raise exception 'the attestation log changed size during section 8';
    end if;
end;
$$;

------------------------------------------------------------------
-- Section 9. The customer-facing request table is inert paperwork.
--
-- The API must be able to record that a customer accepted terms, browsers must
-- not see it, and the API must NOT be able to close a request it created --
-- otherwise "a request was attested" would be a claim the application could
-- make about itself.
------------------------------------------------------------------
do $$
declare
    -- A never-attested organization, so the "requests move nothing" assertion
    -- below is about this section's own inserts and nothing else.
    v_org uuid := 'a77e5701-0000-4000-8000-000000000003';
    v_role text;
    v_denied boolean;
    v_count bigint;
    v_request uuid;
begin
    foreach v_role in array array['anon', 'authenticated']
    loop
        if has_table_privilege(v_role, 'public.billing_arrangement_request', 'SELECT')
           or has_table_privilege(v_role, 'public.billing_arrangement_request', 'INSERT') then
            raise exception 'browser role % can reach billing_arrangement_request', v_role;
        end if;
    end loop;

    if has_table_privilege('service_role', 'public.billing_arrangement_request', 'UPDATE')
       or has_table_privilege('service_role', 'public.billing_arrangement_request', 'DELETE') then
        raise exception 'the API can close or discard its own billing-arrangement request';
    end if;

    set local role service_role;
    insert into public.billing_arrangement_request
        (organization_id, self_declared_arrangement, agreement_reference)
    values (v_org, 'marginal_per_call', 'clickthrough 2026-07-30')
    returning id into v_request;
    select count(*) into v_count
      from public.billing_arrangement_request where organization_id = v_org;
    reset role;
    if v_count < 1 then
        raise exception 'the API could not record a billing-arrangement request';
    end if;

    v_denied := false;
    set local role service_role;
    begin
        update public.billing_arrangement_request set status = 'attested' where id = v_request;
    exception when insufficient_privilege then v_denied := true;
    end;
    reset role;
    if not v_denied then
        raise exception 'the API marked its own billing-arrangement request attested';
    end if;

    -- Creating requests, however many, must move nothing. A customer declaring
    -- marginal_per_call about themselves leaves them exactly as unbillable as
    -- they were.
    if public.organization_billing_arrangement_state(v_org) <> 'unattested' then
        raise exception 'a billing-arrangement request changed the attested arrangement';
    end if;

    -- One open request per organization, so a customer clicking repeatedly does
    -- not build a queue for an operator to sift.
    v_denied := false;
    set local role service_role;
    begin
        insert into public.billing_arrangement_request
            (organization_id, self_declared_arrangement)
        values (v_org, 'marginal_per_call');
    exception when unique_violation then v_denied := true;
    end;
    reset role;
    if not v_denied then
        raise exception 'an organization accumulated two open billing-arrangement requests';
    end if;
end;
$$;

------------------------------------------------------------------
-- Section 9b. The capture RPC: owner-only, idempotent, and billability-neutral.
--
-- The owner check lives in SQL rather than in api/company_admin.py because that
-- module has two service backends and each would have to get it right
-- separately. This section is what makes that claim testable.
------------------------------------------------------------------
do $$
declare
    v_org uuid := 'a77e5701-0000-4000-8000-000000000003';
    v_owner uuid := 'a77e5701-0000-4000-8000-0000000000a1';
    v_admin uuid := 'a77e5701-0000-4000-8000-0000000000a2';
    v_result jsonb;
    v_first uuid;
    v_start constant timestamptz := '2026-07-15 10:00:00+00';
    v_end constant timestamptz := '2026-07-22 10:00:00+00';
    v_message text;
    v_raised boolean;
    v_denied boolean;
begin
    insert into auth.users (id, email)
    values (v_owner, 'attestation-owner@example.invalid'),
           (v_admin, 'attestation-admin@example.invalid');
    insert into public.organization_members (organization_id, user_id, role, status)
    values (v_org, v_owner, 'company_owner', 'active'),
           (v_org, v_admin, 'company_admin', 'active');

    -- Section 9 left an open request on this organization; clear it so the
    -- created/idempotent distinction below is this section's own doing.
    delete from public.billing_arrangement_request where organization_id = v_org;

    if has_function_privilege('anon',
           'public.open_billing_arrangement_request(uuid,uuid,text,text,text,text,text)',
           'EXECUTE')
       or has_function_privilege('authenticated',
           'public.open_billing_arrangement_request(uuid,uuid,text,text,text,text,text)',
           'EXECUTE') then
        raise exception 'a browser role can open a billing-arrangement request';
    end if;

    -- A company_admin is NOT enough: accepting commercial terms is the one
    -- company-admin action that must stay with the owner.
    set local role service_role;
    v_result := public.open_billing_arrangement_request(
        v_org, v_admin, 'marginal_per_call', 'MSA draft', '25%', 'terms-2026-07',
        'attestation-suite-admin');
    reset role;
    if (v_result->>'ok')::boolean is not false or v_result->>'code' <> 'denied' then
        raise exception 'a company_admin accepted commercial terms: %', v_result;
    end if;
    if exists (select 1 from public.billing_arrangement_request where organization_id = v_org) then
        raise exception 'a denied request still wrote a row';
    end if;
    -- The denial is audited, and the audit row survived the denial rather than
    -- being rolled back with it.
    if not exists (
        select 1 from public.audit_events
         where organization_id = v_org
           and action = 'billing_arrangement.request.denied'
           and outcome = 'denied'
    ) then
        raise exception 'a denied billing-arrangement request left no audit row';
    end if;

    -- The owner may, and it is idempotent.
    set local role service_role;
    v_result := public.open_billing_arrangement_request(
        v_org, v_owner, 'marginal_per_call', 'MSA 2026-08-03', '25% of verified savings',
        'terms-2026-07', 'attestation-suite-owner');
    reset role;
    if (v_result->>'ok')::boolean is not true or (v_result->>'created')::boolean is not true then
        raise exception 'the owner could not accept commercial terms: %', v_result;
    end if;
    v_first := (v_result->>'request_id')::uuid;

    set local role service_role;
    v_result := public.open_billing_arrangement_request(
        v_org, v_owner, 'marginal_per_call', 'MSA 2026-08-03', '25% of verified savings',
        'terms-2026-07', 'attestation-suite-owner');
    reset role;
    if (v_result->>'created')::boolean is not false
       or (v_result->>'request_id')::uuid <> v_first then
        raise exception 'a second acceptance opened a second request: %', v_result;
    end if;
    if (select count(*) from public.billing_arrangement_request
         where organization_id = v_org) <> 1 then
        raise exception 'the capture RPC is not idempotent';
    end if;

    -- And accepting terms made NOTHING billable. The response says so, and the
    -- settlement guard agrees.
    if v_result->>'arrangement_state' <> 'unattested'
       or (v_result->>'attested')::boolean is not false then
        raise exception 'the capture RPC reported an organization as attested: %', v_result;
    end if;
    if public.organization_billing_arrangement_state(v_org) <> 'unattested' then
        raise exception 'accepting commercial terms attested the organization';
    end if;
    v_raised := false;
    begin
        perform public.assert_billing_period_settlement_allowed(v_org, v_start, v_end, 1::bigint);
    exception
        when sqlstate '55000' then
            get stacked diagnostics v_message = message_text;
            if position('unattested_billing_arrangement' in v_message) = 0 then
                raise exception 'accepting terms halted for the wrong reason: %', v_message;
            end if;
            v_raised := true;
    end;
    if not v_raised then
        raise exception 'accepting commercial terms was enough to settle a positive fee';
    end if;

    -- The attestor role has no business opening requests on a customer's behalf.
    v_denied := false;
    set local session authorization brevitas_attestor;
    begin
        perform public.open_billing_arrangement_request(
            v_org, v_owner, 'marginal_per_call', 'x', 'x', 'x', 'attestor-forging');
    exception when insufficient_privilege then v_denied := true;
    end;
    reset session authorization;
    if not v_denied then
        raise exception 'brevitas_attestor opened a billing-arrangement request on a '
                        'customer''s behalf, manufacturing the precondition for its own '
                        'attestation';
    end if;
end;
$$;

------------------------------------------------------------------
-- Section 10. Nothing in the settlement path reads customer-declared
-- paperwork. Asserted over the functions' own source, so that adding such a
-- read fails here rather than in an invoice.
------------------------------------------------------------------
do $$
declare
    v_source text := '';
    v_proc text;
begin
    foreach v_proc in array array[
        'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)',
        'public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)',
        'public.organization_billing_arrangement_state(uuid)',
        'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)',
        'public.settle_billing_period(uuid,timestamptz,text,boolean)',
        'public.promote_billing_period_settlement(bigint,text,text)'
    ]
    loop
        if to_regprocedure(v_proc) is not null then
            v_source := v_source || E'\n' || pg_get_functiondef(to_regprocedure(v_proc));
        end if;
    end loop;

    if position('billing_arrangement_request' in v_source) <> 0 then
        raise exception 'a settlement-path function reads billing_arrangement_request; '
                        'customer-declared paperwork must never be an input to the money path';
    end if;
    -- The money path must read the ATTESTATION, though -- if it stopped, every
    -- refusal asserted above would become decoration.
    if position('organization_billing_arrangement_state' in v_source) = 0 then
        raise exception 'the settlement guard no longer consults the billing-arrangement '
                        'attestation';
    end if;
end;
$$;

do $$
begin
    if current_user is distinct from session_user then
        raise exception 'a role switch leaked out of the attestation fixture';
    end if;
    if session_user = 'brevitas_attestor' then
        raise exception 'the attestor session leaked out of the attestation fixture';
    end if;
end;
$$;

rollback;
