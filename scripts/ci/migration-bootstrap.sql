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
