-- Durable Stripe billing recovery for continuously running Railway workers.
--
-- Postgres is authoritative. A worker may submit usage only while it owns the
-- row lease. Stripe identifiers and idempotency keys remain derived from the
-- immutable ledger id. Financial ledger rows are retained for at least seven
-- years and may not be deleted through SQL.

alter table public.billing_ledger
    add column if not exists lease_owner text,
    add column if not exists lease_expires_at timestamptz,
    add column if not exists outbound_started_at timestamptz,
    add column if not exists last_attempt_at timestamptz,
    add column if not exists next_attempt_at timestamptz not null default now(),
    add column if not exists max_attempts integer not null default 5;

do $$
begin
    alter table public.billing_ledger
        add constraint billing_ledger_max_attempts_check
        check (max_attempts between 1 and 10);
exception
    when duplicate_object then null;
end;
$$;

-- The original constraint predates the terminal `dead` state.
alter table public.billing_ledger
    drop constraint if exists billing_ledger_status_check;
do $$
begin
    alter table public.billing_ledger
        add constraint billing_ledger_status_check
        check (status in ('pending', 'sending', 'reported', 'review', 'capped', 'expired', 'dead'));
exception
    when duplicate_object then null;
end;
$$;

create index if not exists billing_ledger_recovery_claim_idx
    on public.billing_ledger (next_attempt_at, lease_expires_at, id)
    where status in ('pending', 'sending');
create index if not exists billing_ledger_attention_idx
    on public.billing_ledger (status, occurred_at, id)
    where status in ('review', 'dead');

-- Rows left in `sending` by the pre-lease Vercel synchronizer have an unknown
-- Stripe outcome. Quarantine them for operator reconciliation; never infer
-- that a missing lease means they are safe to send again.
update public.billing_ledger
   set status = 'review',
       last_error = case
           when last_error = '' then 'legacy ambiguous send requires reconciliation'
           else last_error
       end
 where status = 'sending'
   and lease_expires_at is null;

-- Derive every historical seven-day boundary from Stripe's exact current
-- period. Unix/UTC intervals make the result independent of calendar months,
-- locale, daylight-saving transitions, and the worker's session timezone.
create or replace function public.billing_period_for_occurrence(
    p_occurred_at timestamptz,
    p_anchor_start timestamptz,
    p_anchor_end timestamptz
)
returns table (period_start timestamptz, period_end timestamptz)
language plpgsql
immutable
set search_path = public
set timezone = 'UTC'
as $$
declare
    week_offset bigint;
begin
    if p_occurred_at is null
       or p_anchor_start is null
       or p_anchor_end is null
       or p_anchor_end - p_anchor_start <> interval '7 days' then
        raise exception using
            errcode = '22023',
            message = 'invalid Stripe weekly billing-period anchor';
    end if;

    week_offset := floor(
        extract(epoch from (p_occurred_at - p_anchor_start)) / 604800
    )::bigint;
    period_start := p_anchor_start + week_offset * interval '7 days';
    period_end := period_start + interval '7 days';
    return next;
end;
$$;

-- Migration-time deterministic contract checks cover prior weeks, exact
-- half-open boundaries, and a US daylight-saving transition in UTC.
do $$
declare
    checked_start timestamptz;
    checked_end timestamptz;
begin
    select period.period_start, period.period_end
      into checked_start, checked_end
      from public.billing_period_for_occurrence(
          '2026-07-09 09:59:59+00',
          '2026-07-15 10:00:00+00',
          '2026-07-22 10:00:00+00'
      ) period;
    if checked_start <> '2026-07-08 10:00:00+00'
       or checked_end <> '2026-07-15 10:00:00+00' then
        raise exception 'weekly-anchor regression: expected prior seven-day period';
    end if;

    select period.period_start, period.period_end
      into checked_start, checked_end
      from public.billing_period_for_occurrence(
          '2026-07-22 10:00:00+00',
          '2026-07-15 10:00:00+00',
          '2026-07-22 10:00:00+00'
      ) period;
    if checked_start <> '2026-07-22 10:00:00+00'
       or checked_end <> '2026-07-29 10:00:00+00' then
        raise exception 'weekly-anchor regression: end boundary must enter next period';
    end if;

    select period.period_start, period.period_end
      into checked_start, checked_end
      from public.billing_period_for_occurrence(
          '2026-03-09 12:00:00+00',
          '2026-03-05 10:00:00+00',
          '2026-03-12 10:00:00+00'
      ) period;
    if checked_start <> '2026-03-05 10:00:00+00'
       or checked_end <> '2026-03-12 10:00:00+00' then
        raise exception 'weekly-anchor regression: UTC period changed across DST';
    end if;
end;
$$;

-- Atomically claim one row across replicas. SKIP LOCKED ensures that two
-- Railway workers never submit the same row concurrently. Expired `sending`
-- leases are reclaimable, but the application must reconcile them with Stripe
-- before deciding whether a safe same-identifier replay is possible.
create or replace function public.claim_billing_ledger_entries(
    p_owner text,
    p_lease_seconds integer,
    p_limit integer,
    p_cap_microusd bigint
)
returns table (
    id bigint,
    user_id uuid,
    occurred_at timestamptz,
    fee_microusd bigint,
    stripe_customer_id text,
    attempts integer,
    reclaimed boolean,
    outbound_started_at timestamptz,
    period_start timestamptz,
    period_end timestamptz,
    expected_period_microusd bigint
)
language plpgsql
security definer
set search_path = public
as $$
declare
    candidate public.billing_ledger%rowtype;
    account public.billing_accounts%rowtype;
    committed bigint;
    claim_period_start timestamptz;
    claim_period_end timestamptz;
    was_reclaimed boolean;
begin
    if nullif(btrim(p_owner), '') is null
       or p_lease_seconds not between 15 and 900
       or p_limit <> 1
       or p_cap_microusd <= 0 then
        raise exception 'invalid billing claim parameters';
    end if;

    -- A never-attempted row outside Stripe's reporting window cannot be sent.
    update public.billing_ledger ledger
       set status = 'expired',
           last_error = 'Stripe 35-day reporting window elapsed',
           lease_owner = null,
           lease_expires_at = null
     where ledger.status = 'pending'
       and ledger.occurred_at < now() - interval '34 days';

    -- A very old ambiguous send is outside Stripe's identifier-deduplication
    -- window. It requires human reconciliation instead of an unsafe replay.
    update public.billing_ledger ledger
       set status = 'review',
           last_error = 'ambiguous Stripe send exceeded safe replay window',
           lease_owner = null,
           lease_expires_at = null
     where ledger.status = 'sending'
       and ledger.lease_expires_at < now()
       and ledger.outbound_started_at < now() - interval '23 hours';

    update public.billing_ledger ledger
       set status = 'review',
           last_error = 'billing recovery attempts exhausted',
           lease_owner = null,
           lease_expires_at = null
     where ledger.status = 'sending'
       and ledger.lease_expires_at < now()
       and ledger.attempts >= ledger.max_attempts;

    for candidate in
        select ledger.*
          from public.billing_ledger ledger
         where ledger.attempts < ledger.max_attempts
           and ledger.next_attempt_at <= now()
           and (
               ledger.status = 'pending'
               or (ledger.status = 'sending' and ledger.lease_expires_at < now())
           )
         order by ledger.next_attempt_at, ledger.occurred_at, ledger.id
         for update skip locked
         limit p_limit
    loop
        -- Reclaimed means any expired prior lease, including a worker crash
        -- after claim but before it marked an outbound Stripe request.
        was_reclaimed := candidate.status = 'sending';
        perform pg_advisory_xact_lock(hashtextextended(candidate.user_id::text, 0));

        select billing_account.* into account
          from public.billing_accounts billing_account
         where billing_account.user_id = candidate.user_id;
        if not found
           or account.stripe_customer_id is null
           or account.subscription_status not in ('active', 'trialing') then
            update public.billing_ledger
               set status = 'dead',
                   last_error = 'billable Stripe account is unavailable',
                   lease_owner = null,
                   lease_expires_at = null
             where billing_ledger.id = candidate.id;
            continue;
        end if;

        begin
            select period.period_start, period.period_end
              into claim_period_start, claim_period_end
              from public.billing_period_for_occurrence(
                  candidate.occurred_at,
                  account.current_period_start,
                  account.current_period_end
              ) period;
        exception
        when invalid_parameter_value then
            update public.billing_ledger
               set status = 'review',
                   last_error = 'invalid Stripe weekly billing-period anchor',
                   lease_owner = null,
                   lease_expires_at = null
             where billing_ledger.id = candidate.id;
            continue;
        end;

        select coalesce(sum(ledger.fee_microusd), 0) into committed
          from public.billing_ledger ledger
         where ledger.user_id = candidate.user_id
           and ledger.occurred_at >= claim_period_start
           and ledger.occurred_at < claim_period_end
           and ledger.status in ('sending', 'reported', 'review');

        if not was_reclaimed and committed + candidate.fee_microusd > p_cap_microusd then
            update public.billing_ledger
               set status = 'capped',
                   last_error = 'weekly safety cap reached',
                   lease_owner = null,
                   lease_expires_at = null
             where billing_ledger.id = candidate.id;
            continue;
        end if;

        update public.billing_ledger
           set status = 'sending',
               attempts = billing_ledger.attempts + 1,
               lease_owner = p_owner,
               lease_expires_at = now() + make_interval(secs => p_lease_seconds),
               last_error = ''
         where billing_ledger.id = candidate.id;

        if not was_reclaimed then
            committed := committed + candidate.fee_microusd;
        end if;

        return query
        select candidate.id,
               candidate.user_id,
               candidate.occurred_at,
               candidate.fee_microusd,
               account.stripe_customer_id,
               candidate.attempts + 1,
               was_reclaimed,
               candidate.outbound_started_at,
               claim_period_start,
               claim_period_end,
               committed;
    end loop;
end;
$$;

create or replace function public.mark_billing_outbound_started(
    p_entry_id bigint,
    p_owner text
)
returns boolean
language sql
security definer
set search_path = public
as $$
    update public.billing_ledger
       set outbound_started_at = coalesce(outbound_started_at, now()),
           last_attempt_at = now()
     where id = p_entry_id
       and status = 'sending'
       and lease_owner = p_owner
       and lease_expires_at > now()
    returning true;
$$;

create or replace function public.renew_billing_ledger_lease(
    p_entry_id bigint,
    p_owner text,
    p_lease_seconds integer
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_lease_seconds not between 15 and 900 then
        raise exception 'invalid billing lease duration';
    end if;
    update public.billing_ledger
       set lease_expires_at = now() + make_interval(secs => p_lease_seconds)
     where id = p_entry_id
       and status = 'sending'
       and lease_owner = p_owner
       and lease_expires_at > now();
    return found;
end;
$$;

create or replace function public.complete_billing_ledger_entry(
    p_entry_id bigint,
    p_owner text,
    p_status text,
    p_error text default ''
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_status not in ('reported', 'review', 'dead') then
        raise exception 'invalid billing completion status';
    end if;
    update public.billing_ledger
       set status = p_status,
           reported_at = case when p_status = 'reported' then now() else reported_at end,
           last_error = left(coalesce(p_error, ''), 500),
           lease_owner = null,
           lease_expires_at = null
     where id = p_entry_id
       and status = 'sending'
       and lease_owner = p_owner;
    return found;
end;
$$;

-- Release only never-submitted claims. Ambiguous rows stay `sending` and become
-- recoverable after lease expiry; changing those to pending would enable a
-- blind duplicate send.
create or replace function public.release_billing_ledger_leases(p_owner text)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    released integer;
begin
    update public.billing_ledger
       set status = 'pending',
           attempts = greatest(0, attempts - 1),
           lease_owner = null,
           lease_expires_at = null,
           next_attempt_at = now()
     where status = 'sending'
       and lease_owner = p_owner
       and outbound_started_at is null;
    get diagnostics released = row_count;
    return released;
end;
$$;

create or replace function public.billing_recovery_health()
returns table (
    pending_count bigint,
    review_count bigint,
    dead_count bigint,
    stale_sending_count bigint,
    oldest_pending_seconds bigint
)
language sql
security definer
set search_path = public
as $$
    select count(*) filter (where status = 'pending'),
           count(*) filter (where status = 'review'),
           count(*) filter (where status = 'dead'),
           count(*) filter (where status = 'sending' and lease_expires_at < now()),
           coalesce(extract(epoch from now() - min(occurred_at) filter (where status = 'pending'))::bigint, 0)
      from public.billing_ledger;
$$;

-- The Vercel route is an authenticated manual-recovery control only. It never
-- calls Stripe. Operators must reconcile Stripe first and record a reason.
create or replace function public.manually_resolve_billing_ledger_entry(
    p_entry_id bigint,
    p_resolution text,
    p_note text
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_resolution not in ('reported', 'dead', 'pending')
       or length(btrim(coalesce(p_note, ''))) < 12 then
        raise exception 'invalid manual billing resolution';
    end if;
    update public.billing_ledger
       set status = p_resolution,
           reported_at = case when p_resolution = 'reported' then now() else reported_at end,
           last_error = left('manual recovery: ' || btrim(p_note), 500),
           lease_owner = null,
           lease_expires_at = null,
           next_attempt_at = case when p_resolution = 'pending' then now() else next_attempt_at end,
           max_attempts = case
               when p_resolution = 'pending' then least(10, greatest(max_attempts, attempts + 1))
               else max_attempts
           end,
           -- An explicit retry starts a new safe-send decision. The operator is
           -- attesting that Stripe did not accept the prior identifier.
           outbound_started_at = case when p_resolution = 'pending' then null else outbound_started_at end
     where id = p_entry_id
       and status in ('review', 'dead')
       and (p_resolution <> 'pending' or attempts < 10);
    return found;
end;
$$;

create or replace function public.prevent_billing_ledger_delete()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    raise exception 'billing ledger rows are immutable financial records retained for seven years';
end;
$$;

create or replace function public.prevent_billing_ledger_identity_change()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    if new.id is distinct from old.id
       or new.usage_log_id is distinct from old.usage_log_id
       or new.user_id is distinct from old.user_id
       or new.occurred_at is distinct from old.occurred_at
       or new.fee_microusd is distinct from old.fee_microusd
       or new.created_at is distinct from old.created_at then
        raise exception 'billing ledger identity, amount, source, and creation fields are immutable';
    end if;
    return new;
end;
$$;

drop trigger if exists prevent_billing_ledger_delete on public.billing_ledger;
create trigger prevent_billing_ledger_delete
before delete on public.billing_ledger
for each statement execute function public.prevent_billing_ledger_delete();

drop trigger if exists prevent_billing_ledger_identity_change on public.billing_ledger;
create trigger prevent_billing_ledger_identity_change
before update on public.billing_ledger
for each row execute function public.prevent_billing_ledger_identity_change();

revoke all on function public.claim_billing_ledger_entries(text, integer, integer, bigint) from public, anon, authenticated;
revoke all on function public.billing_period_for_occurrence(timestamptz, timestamptz, timestamptz) from public, anon, authenticated;
revoke all on function public.mark_billing_outbound_started(bigint, text) from public, anon, authenticated;
revoke all on function public.renew_billing_ledger_lease(bigint, text, integer) from public, anon, authenticated;
revoke all on function public.complete_billing_ledger_entry(bigint, text, text, text) from public, anon, authenticated;
revoke all on function public.release_billing_ledger_leases(text) from public, anon, authenticated;
revoke all on function public.billing_recovery_health() from public, anon, authenticated;
revoke all on function public.manually_resolve_billing_ledger_entry(bigint, text, text) from public, anon, authenticated;
revoke all on function public.prevent_billing_ledger_delete() from public, anon, authenticated;
revoke all on function public.prevent_billing_ledger_identity_change() from public, anon, authenticated;
grant execute on function public.claim_billing_ledger_entries(text, integer, integer, bigint) to service_role;
grant execute on function public.mark_billing_outbound_started(bigint, text) to service_role;
grant execute on function public.renew_billing_ledger_lease(bigint, text, integer) to service_role;
grant execute on function public.complete_billing_ledger_entry(bigint, text, text, text) to service_role;
grant execute on function public.release_billing_ledger_leases(text) to service_role;
grant execute on function public.billing_recovery_health() to service_role;
grant execute on function public.manually_resolve_billing_ledger_entry(bigint, text, text) to service_role;

comment on table public.billing_ledger is
    'Authoritative seven-year financial usage ledger. Never store prompts or responses.';

-- ROLLBACK PROCEDURE (maintenance window; pause all billing workers first):
-- 1. Verify no rows have status `dead`; resolve/export those records first.
-- 2. DROP TRIGGER IF EXISTS prevent_billing_ledger_identity_change ON public.billing_ledger;
-- 3. DROP TRIGGER IF EXISTS prevent_billing_ledger_delete ON public.billing_ledger;
-- 4. DROP FUNCTION IF EXISTS public.prevent_billing_ledger_identity_change();
-- 5. DROP FUNCTION IF EXISTS public.prevent_billing_ledger_delete();
-- 6. DROP FUNCTION IF EXISTS public.manually_resolve_billing_ledger_entry(bigint,text,text);
-- 7. DROP FUNCTION IF EXISTS public.billing_recovery_health();
-- 8. DROP FUNCTION IF EXISTS public.release_billing_ledger_leases(text);
-- 9. DROP FUNCTION IF EXISTS public.complete_billing_ledger_entry(bigint,text,text,text);
-- 10. DROP FUNCTION IF EXISTS public.renew_billing_ledger_lease(bigint,text,integer);
-- 11. DROP FUNCTION IF EXISTS public.mark_billing_outbound_started(bigint,text);
-- 12. DROP FUNCTION IF EXISTS public.claim_billing_ledger_entries(text,integer,integer,bigint);
-- 13. DROP FUNCTION IF EXISTS public.billing_period_for_occurrence(timestamptz,timestamptz,timestamptz);
-- 14. Restore the original status constraint and claim_billing_ledger_entry
--     function from 20260716_stripe_billing.sql. Keep all added columns in place
--     until the seven-year retention obligation expires; they are harmless.
