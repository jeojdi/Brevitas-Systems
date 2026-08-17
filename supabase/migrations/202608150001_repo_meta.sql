-- Per-organization repository metadata: a display-name alias for repositories that
-- are otherwise auto-discovered from usage_log, plus an `added` flag for repos a user
-- pre-registers before any traffic has arrived. The canonical `repo` key is never
-- rewritten (usage attribution stays intact); this table only decorates it.
--
-- Ships dark: api/ reads this table degrade-safe (PGRST205 → no aliases) so the
-- Projects tab keeps working before this migration is applied.

create table if not exists public.repo_meta (
    organization_id uuid not null,
    repo            text not null,
    display_name    text not null default '',
    added           boolean not null default false,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    primary key (organization_id, repo)
);

create index if not exists repo_meta_org_idx
    on public.repo_meta(organization_id, updated_at desc);

alter table public.repo_meta enable row level security;

-- Service-role backend access only. Dashboard users and anonymous clients cannot
-- read or write repository aliases directly through PostgREST; all access is mediated
-- by the api/ layer, which scopes every call to the caller's organization.
revoke all on public.repo_meta from public, anon, authenticated;
