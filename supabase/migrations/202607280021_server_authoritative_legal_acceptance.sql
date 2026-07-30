-- Legal acceptance evidence must be asserted by the server, not by the browser.
--
-- record_legal_acceptance() is the only writer of public.legal_acceptances and it
-- copies signup metadata verbatim (20260714_legal_acceptances.sql:17-27, extended
-- by 20260715_analytics_privacy.sql:17-24):
--
--     if new.raw_user_meta_data->>'accepted_terms_at' is not null
--        and new.raw_user_meta_data->>'terms_version' is not null then
--       insert into public.legal_acceptances (...) values (
--         new.id, new.raw_user_meta_data->>'terms_version', new.created_at,
--         nullif(new.raw_user_meta_data->>'privacy_version',''), ...
--
-- raw_user_meta_data is whatever the browser passed as options.data to
-- supabase.auth.signUp -- GoTrue stores that field unvalidated, unlike
-- app_metadata -- and the values are hardcoded into a public bundle
-- (dashboard/src/components/Auth.jsx:108-114). Only accepted_at was
-- server-derived. The consequences are two: the recorded version of the terms a
-- user agreed to is client-chosen, and when the metadata is absent the `if` is
-- simply false, no row is written, and account creation succeeds anyway -- so
-- "no row" is indistinguishable from "never asked". Nothing anywhere requires a
-- row to exist (grep: legal_acceptances is read only by the compliance export at
-- 202607170007:1528 and :1892).
--
-- After this migration the trigger writes the versions the SERVER currently
-- publishes, from public.current_legal_versions(), and always writes a row:
-- raw_user_meta_data is reduced to the boolean signal that the checkbox was
-- ticked, recorded in the new `accepted` column. A signup that asserted nothing
-- is therefore recorded as presented-and-not-accepted instead of vanishing.
-- Bumping a published version is a one-line forward migration that replaces
-- current_legal_versions(), which is the point: the version becomes a
-- server-side release artifact.
--
-- Deliberately NOT done here, because both would break callers:
--   * no BEFORE UPDATE OR DELETE rejection trigger -- legal_acceptances.user_id
--     is `references auth.users(id) on delete cascade`, so a rejection trigger
--     (or `on delete restrict`) would make every GoTrue admin user-delete fail.
--   * no primary-key change for an append-only (user_id, document, version)
--     history. The trigger is AFTER INSERT on auth.users, so it fires exactly
--     once per user and cannot produce history on its own; a re-acceptance flow
--     is what would need that table shape, and no such flow exists yet.
--
-- dashboard/src/components/Auth.jsx should stop sending terms_version /
-- privacy_version, or keep them in sync with current_legal_versions(); the
-- literals it sends are now ignored. It is not changed here.
--
-- Forward-only and idempotent.

-- REVERSE: PITR-ONLY -- reverting to client-authored versions would corrupt legal acceptance evidence rows already written server-side

begin;

do $migration_precondition$
begin
    if to_regclass('public.legal_acceptances') is null
       or to_regprocedure('public.record_legal_acceptance()') is null then
        raise exception using
            errcode = '55000',
            message = '202607280021 requires public.legal_acceptances and its trigger';
    end if;
end;
$migration_precondition$;

-- Existing rows were written only when the client asserted acceptance, so the
-- default is correct for them and no backfill is needed.
alter table public.legal_acceptances
    add column if not exists accepted boolean not null default true;

comment on column public.legal_acceptances.accepted is
    'Whether the signup asserted acceptance of the presented documents. False rows record that the current versions were presented and not accepted.';

-- The published document versions, pinned server-side. This is the single source
-- of truth for what a new account is asked to accept; bump it in a new forward
-- migration when counsel publishes a new document.
create or replace function public.current_legal_versions()
returns jsonb
language sql
immutable
parallel safe
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object(
        'terms_version', '2026-07-14',
        'privacy_version', '2026-07-15'
    );
$function$;

comment on function public.current_legal_versions() is
    'Server-pinned published legal document versions written into legal_acceptances; never read from client metadata.';

create or replace function public.record_legal_acceptance()
returns trigger language plpgsql security definer set search_path = public as $$
declare
    v_versions jsonb := public.current_legal_versions();
    v_accepted boolean := new.raw_user_meta_data->>'accepted_terms_at' is not null;
begin
    insert into public.legal_acceptances (
        user_id, terms_version, accepted_at, privacy_version,
        analytics_notice_acknowledged_at, accepted
    ) values (
        new.id,
        v_versions->>'terms_version',
        new.created_at,
        v_versions->>'privacy_version',
        -- Client metadata is a signal, never a value: the timestamp is the
        -- server's own row-creation time.
        case when nullif(
                new.raw_user_meta_data->>'analytics_notice_acknowledged_at', ''
            ) is not null
            then new.created_at
        end,
        v_accepted
    ) on conflict (user_id) do nothing;
    return new;
end;
$$;

create or replace trigger on_auth_user_legal_acceptance
  after insert on auth.users
  for each row execute procedure public.record_legal_acceptance();

commit;
