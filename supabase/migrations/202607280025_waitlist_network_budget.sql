-- A per-network admission budget for the waitlist endpoint, charged BEFORE the
-- existing global window.
--
-- WHAT IS MISSING TODAY. 202607200010 gives the waitlist two shared windows: a
-- global one (120/minute across every Vercel instance) and a per-email-hash one
-- (3 per 10 minutes). Both are correct for what they measure, and neither
-- measures the submitter. A single host that varies the email address consumes
-- the ENTIRE global window at 120 requests/minute -- the identity window never
-- fires because every identity is new -- and the endpoint is then closed for
-- every legitimate submitter until the fixed window rolls. The global limit is
-- therefore an availability floor, not an abuse control: it converts a
-- single-source flood into a denial of service against everyone else.
--
-- WHAT THIS ADDS. public.consume_waitlist_network_budget() is a third window
-- keyed on the caller's NETWORK rather than on the claimed identity, charged
-- first so a flooding network is stopped before it can spend global budget.
--
-- PRIVACY. The function never sees an address. Its p_network_key argument is a
-- salted SHA-256 of the client IP's network prefix (/24 for IPv4, /48 for IPv6)
-- computed by the caller, and it is stored in
-- shared_endpoint_rate_limits.identity_hash, whose CHECK constraint
-- (202607200010) admits only 64 lowercase hex characters. A raw address cannot
-- be written to this table even by mistake: the constraint rejects it, and the
-- precondition below re-asserts the constraint is present before installing a
-- function that depends on it. The prefix (not the host address) is the unit so
-- that the record cannot single out one subscriber line, and the salt is held
-- by the caller so the stored value is not a lookup table away from an address.
--
-- CONTRACT.
--   public.consume_waitlist_network_budget(p_network_key text, p_limit int,
--                                          p_window_seconds int) returns json
--   -> {"allowed": bool, "retry_after_seconds": int}
-- service_role only. retry_after_seconds is 0 when allowed, otherwise the whole
-- seconds remaining in the current window (at least 1, never more than the
-- window itself, so a caller cannot be told to wait longer than the limiter
-- will actually hold it).
--
-- WINDOW SEMANTICS match 202607200010 exactly: a fixed window opened by the
-- first admitted request, counted with an upsert whose DO UPDATE is guarded by
-- `expires_at > now and request_count < limit`, so the not-found path IS the
-- denial and no read-then-write race exists. Serialization is the same
-- mechanism too, one advisory transaction lock per namespace.
--
-- LOCK ORDER. This function takes only the network-scoped lock, and it is
-- charged BEFORE submit_waitlist_signup, which takes
-- brevitas.waitlist.global.v1 and then brevitas.waitlist.identity.v1. Every
-- transaction that touches both therefore acquires network -> global ->
-- identity, so no inversion is possible against the existing functions. Two
-- different networks never contend at all, which is the point: the whole defect
-- being fixed is one network's traffic blocking another's.
--
-- RETENTION. The table is content-free, but it must still be bounded, and a
-- per-network key space is the one namespace an attacker can enumerate. Three
-- things bound it:
--   1. Every call deletes its OWN expired row (required for correctness anyway:
--      the guarded upsert cannot reopen a window whose row still exists).
--   2. Every call sweeps up to 64 OTHER expired waitlist.network rows with
--      FOR UPDATE SKIP LOCKED, so each admitted insert of one row is matched by
--      up to 64 removals. The table drains strictly faster than this function
--      can fill it, and SKIP LOCKED means the sweep never blocks and never
--      deadlocks against a concurrent call holding a different network lock.
--   3. public.purge_expired_shared_endpoint_rate_limits() gives the janitor a
--      bounded, all-scope sweep for the rows the other two limiters leave
--      behind when their endpoints go quiet. It deletes only rows that are
--      already expired, so it can never widen anyone's budget.
--
-- DEGRADATION. Callers must treat a missing function (this migration not yet
-- applied) as "no network budget configured" and fall through to the existing
-- global/identity windows, which are unchanged by this migration. Nothing here
-- alters submit_waitlist_signup.

-- REVERSE: DDL: drop function if exists public.consume_waitlist_network_budget(text, integer, integer); drop function if exists public.purge_expired_shared_endpoint_rate_limits(integer); delete from public.shared_endpoint_rate_limits where endpoint_scope = 'waitlist.network';

begin;

do $migration_precondition$
begin
    if to_regclass('public.shared_endpoint_rate_limits') is null
       or to_regprocedure(
              'public.submit_waitlist_signup('
              || 'text,text,text,text,text,text,text,text,boolean)'
          ) is null then
        raise exception using
            errcode = '55000',
            message = '202607280025 requires 202607200010 first';
    end if;
    -- The privacy property of this migration is inherited from the column
    -- constraint, so refuse to install if the constraint is not there.
    if not exists (
        select 1
          from pg_constraint identity_constraint
         where identity_constraint.conrelid
                   = 'public.shared_endpoint_rate_limits'::regclass
           and identity_constraint.conname
                   = 'shared_endpoint_rate_limits_identity_check'
    ) then
        raise exception using
            errcode = '55000',
            message = '202607280025 requires the shared limiter identity-hash constraint';
    end if;
end;
$migration_precondition$;

create or replace function public.consume_waitlist_network_budget(
    p_network_key text,
    p_limit integer,
    p_window_seconds integer
)
returns json
language plpgsql
security definer
set search_path = pg_catalog, public, extensions, pg_temp
as $function$
declare
    v_network_key text := pg_catalog.lower(pg_catalog.btrim(coalesce(p_network_key, '')));
    v_now timestamptz;
    v_window interval;
    v_expires_at timestamptz;
    v_count integer;
    v_retry_after integer;
begin
    -- Fail closed and loudly on a malformed budget. These arguments come from
    -- server code, never from a request body, so an out-of-range value is a
    -- deployment defect; silently widening the window would be the worst
    -- possible response to it.
    if v_network_key !~ '^[0-9a-f]{64}$' then
        raise invalid_parameter_value using
            message = 'waitlist network budget requires a salted 64-hex network key';
    end if;
    if p_limit is null or p_limit not between 1 and 1000000 then
        raise invalid_parameter_value using
            message = 'waitlist network budget limit out of range';
    end if;
    if p_window_seconds is null or p_window_seconds not between 1 and 86400 then
        raise invalid_parameter_value using
            message = 'waitlist network budget window out of range';
    end if;
    v_window := pg_catalog.make_interval(secs => p_window_seconds);

    -- One lock per network. Acquired before the waitlist global/identity locks
    -- by construction, because this budget is charged first.
    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'brevitas.waitlist.network.v1:' || v_network_key, 0));
    v_now := pg_catalog.clock_timestamp();

    -- (1) Own expired row. Held under this network's lock, so no concurrent
    -- caller for the same key can reinsert between the delete and the upsert.
    delete from public.shared_endpoint_rate_limits
     where endpoint_scope = 'waitlist.network'
       and identity_hash = v_network_key
       and expires_at <= v_now;

    -- (2) Bounded sweep of other networks' expired rows. SKIP LOCKED keeps this
    -- wait-free: a row another transaction is already deleting is simply left
    -- for that transaction.
    with expired as (
        select limiter.endpoint_scope, limiter.identity_hash
          from public.shared_endpoint_rate_limits limiter
         where limiter.endpoint_scope = 'waitlist.network'
           and limiter.identity_hash <> v_network_key
           and limiter.expires_at <= v_now
         order by limiter.expires_at
         limit 64
           for update skip locked
    )
    delete from public.shared_endpoint_rate_limits target
     using expired
     where target.endpoint_scope = expired.endpoint_scope
       and target.identity_hash = expired.identity_hash;

    insert into public.shared_endpoint_rate_limits (
        endpoint_scope, identity_hash, window_started_at, expires_at, request_count
    ) values (
        'waitlist.network', v_network_key, v_now, v_now + v_window, 1
    )
    on conflict (endpoint_scope, identity_hash) do update
       set request_count = public.shared_endpoint_rate_limits.request_count + 1
     where public.shared_endpoint_rate_limits.expires_at > v_now
       and public.shared_endpoint_rate_limits.request_count < p_limit
    returning request_count, expires_at into v_count, v_expires_at;

    if not found then
        select limiter.expires_at into v_expires_at
          from public.shared_endpoint_rate_limits limiter
         where limiter.endpoint_scope = 'waitlist.network'
           and limiter.identity_hash = v_network_key;
        v_retry_after := greatest(
            1,
            pg_catalog.ceil(
                extract(epoch from (v_expires_at - v_now))
            )::integer
        );
        return pg_catalog.json_build_object(
            'allowed', false,
            'retry_after_seconds', least(v_retry_after, p_window_seconds)
        );
    end if;

    return pg_catalog.json_build_object(
        'allowed', true,
        'retry_after_seconds', 0
    );
end;
$function$;

revoke all on function public.consume_waitlist_network_budget(text, integer, integer)
    from public, anon, authenticated, service_role;
grant execute on function public.consume_waitlist_network_budget(text, integer, integer)
    to service_role;

-- Bounded janitor for every limiter namespace. Expired rows only: an unexpired
-- row is somebody's live budget and deleting it would hand back their limit.
create or replace function public.purge_expired_shared_endpoint_rate_limits(
    p_max_rows integer default 5000
)
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public, extensions, pg_temp
as $function$
declare
    v_max_rows integer := least(greatest(coalesce(p_max_rows, 5000), 1), 100000);
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_deleted integer;
begin
    with expired as (
        select limiter.endpoint_scope, limiter.identity_hash
          from public.shared_endpoint_rate_limits limiter
         where limiter.expires_at <= v_now
         order by limiter.expires_at
         limit v_max_rows
           for update skip locked
    )
    delete from public.shared_endpoint_rate_limits target
     using expired
     where target.endpoint_scope = expired.endpoint_scope
       and target.identity_hash = expired.identity_hash;
    get diagnostics v_deleted = row_count;
    return v_deleted;
end;
$function$;

revoke all on function public.purge_expired_shared_endpoint_rate_limits(integer)
    from public, anon, authenticated, service_role;
grant execute on function public.purge_expired_shared_endpoint_rate_limits(integer)
    to service_role;

comment on function public.consume_waitlist_network_budget(text, integer, integer) is
    'Atomic server-only waitlist network admission: fixed window per salted /24 or /48 network hash, charged before the global window.';
comment on function public.purge_expired_shared_endpoint_rate_limits(integer) is
    'Bounded janitor for public.shared_endpoint_rate_limits: deletes only already-expired fixed-window rows.';

commit;
