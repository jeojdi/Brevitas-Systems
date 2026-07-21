-- Move public waitlist submissions behind the server-owned API boundary.
-- Browser roles have no table privileges and cannot invoke the write RPC.

begin;

create table if not exists public.waitlist (
    id bigserial primary key,
    email varchar(255) unique not null,
    name varchar(100),
    company varchar(100),
    role varchar(100),
    pipeline_shape text,
    monthly_spend varchar(50),
    orchestrator varchar(100),
    notes text,
    design_partner boolean not null default false,
    ip_address varchar(45),
    request_id varchar(32),
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

alter table public.waitlist
    add column if not exists pipeline_shape text,
    add column if not exists monthly_spend varchar(50),
    add column if not exists orchestrator varchar(100),
    add column if not exists notes text,
    add column if not exists design_partner boolean default false,
    add column if not exists ip_address varchar(45),
    add column if not exists request_id varchar(32),
    add column if not exists created_at timestamptz default timezone('utc', now()),
    add column if not exists updated_at timestamptz default timezone('utc', now());

update public.waitlist
set design_partner = false
where design_partner is null;

alter table public.waitlist
    alter column email set not null,
    alter column design_partner set default false,
    alter column design_partner set not null,
    alter column created_at set default timezone('utc', now()),
    alter column updated_at set default timezone('utc', now());

create unique index if not exists waitlist_email_unique_idx
    on public.waitlist (email);
create index if not exists idx_waitlist_created_at
    on public.waitlist (created_at desc);

-- NOT VALID preserves legacy leads that predate these bounds while enforcing
-- every insert and update after this migration. Operators may remediate and
-- validate historical rows separately without making this security deploy lossy.
do $waitlist_constraints$
begin
    if not exists (
        select 1 from pg_catalog.pg_constraint
        where conrelid = 'public.waitlist'::regclass
          and conname = 'waitlist_email_canonical_check'
    ) then
        alter table public.waitlist add constraint waitlist_email_canonical_check
            check (
                email = pg_catalog.lower(pg_catalog.btrim(email))
                and pg_catalog.char_length(email) between 3 and 254
                and email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'
            ) not valid;
    end if;
    if not exists (
        select 1 from pg_catalog.pg_constraint
        where conrelid = 'public.waitlist'::regclass
          and conname = 'waitlist_field_lengths_check'
    ) then
        alter table public.waitlist add constraint waitlist_field_lengths_check
            check (
                (name is null or pg_catalog.char_length(name) <= 100)
                and (company is null or pg_catalog.char_length(company) <= 100)
                and (role is null or pg_catalog.char_length(role) <= 100)
                and (pipeline_shape is null or pg_catalog.char_length(pipeline_shape) <= 2000)
                and (monthly_spend is null or pg_catalog.char_length(monthly_spend) <= 50)
                and (orchestrator is null or pg_catalog.char_length(orchestrator) <= 100)
                and (notes is null or pg_catalog.char_length(notes) <= 4000)
                and (ip_address is null or pg_catalog.char_length(ip_address) <= 45)
                and (request_id is null or pg_catalog.char_length(request_id) <= 32)
            ) not valid;
    end if;
end;
$waitlist_constraints$;

alter table public.waitlist enable row level security;

-- Remove every historical/manual browser policy, not only policy names known to
-- this repository. Table privileges below are also revoked as defense in depth.
do $waitlist_policies$
declare
    policy_record record;
begin
    for policy_record in
        select policyname
        from pg_catalog.pg_policies
        where schemaname = 'public' and tablename = 'waitlist'
    loop
        execute pg_catalog.format(
            'drop policy %I on public.waitlist',
            policy_record.policyname
        );
    end loop;
end;
$waitlist_policies$;

revoke all on table public.waitlist
    from public, anon, authenticated, service_role;
do $waitlist_sequence$
begin
    if pg_catalog.to_regclass('public.waitlist_id_seq') is not null then
        revoke all on sequence public.waitlist_id_seq
            from public, anon, authenticated, service_role;
    end if;
end;
$waitlist_sequence$;

-- Revoke any manually-created overload before installing the canonical RPC so
-- an old security-definer signature cannot remain a browser write backdoor.
do $waitlist_function_overloads$
declare
    function_record record;
begin
    for function_record in
        select procedure.oid::regprocedure as signature
        from pg_catalog.pg_proc procedure
        join pg_catalog.pg_namespace namespace
          on namespace.oid = procedure.pronamespace
        where namespace.nspname = 'public'
          and procedure.proname = 'submit_waitlist_signup'
    loop
        execute pg_catalog.format(
            'revoke all on function %s from public, anon, authenticated, service_role',
            function_record.signature
        );
    end loop;
end;
$waitlist_function_overloads$;

-- Migration 200010 deliberately upgrades this same input signature from
-- boolean to jsonb. PostgreSQL cannot CREATE OR REPLACE across return types,
-- and replaying this older migration after the complete chain must not
-- downgrade the later shared-admission contract. Install/refresh the boolean
-- form only when it is absent or is already the boolean form.
do $install_waitlist_boolean_contract$
declare
    v_signature regprocedure := to_regprocedure(
        'public.submit_waitlist_signup(text,text,text,text,text,text,text,text,boolean)'
    );
    v_result text;
begin
    if v_signature is not null then
        v_result := pg_catalog.pg_get_function_result(v_signature);
        if v_result not in ('boolean', 'jsonb') then
            raise exception 'unexpected waitlist RPC return contract: %', v_result
                using errcode = '55000';
        end if;
    end if;

    if v_signature is null or v_result = 'boolean' then
        execute $definition$
            create or replace function public.submit_waitlist_signup(
                p_email text,
                p_name text default null,
                p_company text default null,
                p_role text default null,
                p_pipeline_shape text default null,
                p_monthly_spend text default null,
                p_orchestrator text default null,
                p_notes text default null,
                p_design_partner boolean default false
            )
            returns boolean
            language sql
            security definer
            set search_path = pg_catalog, public, pg_temp
            as $body$
                with inserted as (
                    insert into public.waitlist (
                        email,
                        name,
                        company,
                        role,
                        pipeline_shape,
                        monthly_spend,
                        orchestrator,
                        notes,
                        design_partner
                    ) values (
                        pg_catalog.lower(pg_catalog.btrim(p_email)),
                        nullif(pg_catalog.btrim(p_name), ''),
                        nullif(pg_catalog.btrim(p_company), ''),
                        nullif(pg_catalog.btrim(p_role), ''),
                        nullif(pg_catalog.btrim(p_pipeline_shape), ''),
                        nullif(pg_catalog.btrim(p_monthly_spend), ''),
                        nullif(pg_catalog.btrim(p_orchestrator), ''),
                        nullif(pg_catalog.btrim(p_notes), ''),
                        coalesce(p_design_partner, false)
                    )
                    on conflict do nothing
                    returning 1
                )
                select exists(select 1 from inserted);
            $body$
        $definition$;
    end if;
end;
$install_waitlist_boolean_contract$;

revoke all on function public.submit_waitlist_signup(
    text, text, text, text, text, text, text, text, boolean
) from public, anon, authenticated, service_role;
grant execute on function public.submit_waitlist_signup(
    text, text, text, text, text, text, text, text, boolean
) to service_role;

do $comment_waitlist_boolean_contract$
begin
    if pg_catalog.pg_get_function_result(to_regprocedure(
        'public.submit_waitlist_signup(text,text,text,text,text,text,text,text,boolean)'
    )) = 'boolean' then
        execute $comment$
            comment on function public.submit_waitlist_signup(
                text, text, text, text, text, text, text, text, boolean
            ) is 'Server-only idempotent waitlist submission boundary; browser roles are denied.'
        $comment$;
    end if;
end;
$comment_waitlist_boolean_contract$;

commit;
