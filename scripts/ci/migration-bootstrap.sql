\set ON_ERROR_STOP on

create extension if not exists pgcrypto;
create extension if not exists vector;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'anon') then
        create role anon nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticated') then
        create role authenticated nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'service_role') then
        create role service_role nologin bypassrls;
    end if;
end;
$$;

-- Reproduce the PRIVILEGE BASELINE a real Supabase project ships with, not just
-- the role names. A hosted project grants USAGE on schema public to the
-- PostgREST roles and sets default privileges so every subsequently created
-- table, sequence and function in public starts with ALL privileges for
-- anon/authenticated/service_role. Without this, a freshly created relation in
-- CI starts with ZERO privileges for those roles, which makes every
-- `revoke all on table ... from public, anon, authenticated` in the migration
-- chain a no-op under test -- i.e. every revoke assertion in the suite passes
-- whether or not the revoke exists, and the harness models a strictly MORE
-- restrictive world than production. That is the wrong direction for a security
-- test: it is exactly how public.billing_monthly shipped anon-SELECTable past
-- ~40 assertion files.
--
-- ALTER DEFAULT PRIVILEGES without FOR ROLE attaches to the CURRENT role, and
-- that is deliberate here: run-migration-tests.sh applies the bootstrap and
-- every migration over the same single DATABASE_URL, so the creating role is
-- always this one. Do not add FOR ROLE without also changing that assumption.
-- migration-reset-database.sql drops schema public, which discards these
-- pg_default_acl rows, so bootstrap_database() must be re-run after every reset
-- (it is).
grant usage on schema public to anon, authenticated, service_role;
alter default privileges in schema public
    grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public
    grant all on sequences to anon, authenticated, service_role;
alter default privileges in schema public
    grant all on functions to anon, authenticated, service_role;

create schema if not exists auth;
create table if not exists auth.users (
    id uuid primary key default gen_random_uuid(),
    email text unique,
    phone text,
    encrypted_password text,
    raw_user_meta_data jsonb not null default '{}'::jsonb,
    raw_app_meta_data jsonb not null default '{}'::jsonb,
    confirmation_token text not null default '',
    recovery_token text not null default '',
    email_change_token_new text not null default '',
    email_change_token_current text not null default '',
    phone_change_token text not null default '',
    reauthentication_token text not null default '',
    email_change text not null default '',
    phone_change text not null default '',
    banned_until timestamptz,
    deleted_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create or replace function auth.uid()
returns uuid
language sql
stable
as $$
    select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
$$;
