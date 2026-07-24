\set ON_ERROR_STOP on

-- This bootstrap is for an isolated PostgreSQL 16 restore database only. It
-- deliberately does not create auth/public application tables; pg_restore
-- must create those from the encrypted archive.
create extension if not exists pgcrypto;
create extension if not exists vector;

do $$
begin
    if not exists (select 1 from pg_roles where rolname='anon') then
        create role anon nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname='authenticated') then
        create role authenticated nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname='service_role') then
        create role service_role nologin bypassrls;
    end if;
end;
$$;

create schema if not exists brevitas_restore;
revoke all on schema brevitas_restore from public,anon,authenticated;
grant usage on schema brevitas_restore to service_role;

create table if not exists brevitas_restore.control (
    singleton boolean primary key default true check(singleton),
    target_mode text not null check(target_mode='ephemeral-postgres'),
    target_id text not null check(target_id ~ '^[A-Za-z0-9._:-]{3,128}$'),
    target_environment text not null
        check(target_environment in ('local','test','development','staging','production')),
    expected_database_name text not null,
    backup_source_id text not null check(backup_source_id ~ '^[A-Za-z0-9._:-]{3,128}$'),
    source_environment text not null,
    backup_manifest_sha256 text not null check(backup_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    deletion_artifact_sha256 text not null check(deletion_artifact_sha256 ~ '^[0-9a-f]{64}$'),
    deletion_evidence_reference text not null
        check(deletion_evidence_reference ~ '^[A-Za-z0-9._:-]{8,128}$'),
    bootstrapped_at timestamptz not null default clock_timestamp(),
    raw_verified_at timestamptz,
    replay_verified_at timestamptz,
    ready_at timestamptz,
    check (raw_verified_at is not null or (replay_verified_at is null and ready_at is null)),
    check (replay_verified_at is not null or ready_at is null)
);
create table if not exists brevitas_restore.replay_evidence (
    request_id uuid primary key,
    organization_id uuid not null,
    request_scope text not null check(request_scope in ('tenant','member','customer')),
    subject_id uuid,
    artifact_sha256 text not null check(artifact_sha256 ~ '^[0-9a-f]{64}$'),
    result text not null check(result in ('completed','already_absent')),
    replayed_at timestamptz not null default clock_timestamp(),
    check((request_scope='tenant' and subject_id is null)
          or (request_scope<>'tenant' and subject_id is not null))
);

revoke all on all tables in schema brevitas_restore from public,anon,authenticated,service_role;
grant select,update on brevitas_restore.control to service_role;
grant select on brevitas_restore.replay_evidence to service_role;

insert into brevitas_restore.control(
    singleton,target_mode,target_id,target_environment,expected_database_name,backup_source_id,
    source_environment,backup_manifest_sha256,deletion_artifact_sha256,
    deletion_evidence_reference
) values (
    true,:'target_mode',:'target_id',:'target_environment',:'expected_database_name',:'backup_source_id',
    :'source_environment',:'backup_manifest_sha256',:'deletion_artifact_sha256',
    :'deletion_evidence_reference'
) on conflict(singleton) do nothing;

select exists (
    select 1 from brevitas_restore.control
     where singleton and target_mode=:'target_mode' and target_id=:'target_id'
       and target_environment=:'target_environment'
       and expected_database_name=:'expected_database_name'
       and backup_source_id=:'backup_source_id'
       and source_environment=:'source_environment'
       and backup_manifest_sha256=:'backup_manifest_sha256'
       and deletion_artifact_sha256=:'deletion_artifact_sha256'
       and deletion_evidence_reference=:'deletion_evidence_reference'
       and ready_at is null
) as bootstrap_matches \gset
\if :bootstrap_matches
\else
  \echo 'restore bootstrap idempotency/evidence conflict'
  \quit 3
\endif
