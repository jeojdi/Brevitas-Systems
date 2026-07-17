-- Safe install-time association between account keys and repository labels.
-- Stores repository names only; never absolute paths, remotes, source, or raw API keys.

create table if not exists public.key_repositories (
    key_hash text not null references public.api_keys(key_hash) on delete cascade,
    owner_id text not null default '',
    repo text not null,
    source text not null default 'bvx',
    installed_at timestamptz not null default now(),
    last_seen timestamptz not null default now(),
    primary key (key_hash, repo)
);

create index if not exists key_repositories_owner_idx
    on public.key_repositories(owner_id, last_seen desc);
create index if not exists key_repositories_repo_idx
    on public.key_repositories(repo, last_seen desc);

alter table public.key_repositories enable row level security;

-- Service-role backend access only. Dashboard users and anonymous clients cannot
-- query key fingerprints or account/repository relationships through PostgREST.
revoke all on public.key_repositories from public, anon, authenticated;
