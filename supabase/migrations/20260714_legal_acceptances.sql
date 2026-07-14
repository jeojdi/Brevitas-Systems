create table if not exists public.legal_acceptances (
  user_id uuid primary key references auth.users(id) on delete cascade,
  terms_version text not null,
  accepted_at timestamptz not null
);

alter table public.legal_acceptances enable row level security;

drop policy if exists "Users can view own legal acceptance" on public.legal_acceptances;
create policy "Users can view own legal acceptance"
  on public.legal_acceptances for select
  using (auth.uid() = user_id);

create or replace function public.record_legal_acceptance()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if new.raw_user_meta_data->>'accepted_terms_at' is not null
     and new.raw_user_meta_data->>'terms_version' is not null then
    insert into public.legal_acceptances (user_id, terms_version, accepted_at)
    values (
      new.id,
      new.raw_user_meta_data->>'terms_version',
      new.created_at
    ) on conflict (user_id) do nothing;
  end if;
  return new;
end;
$$;

create or replace trigger on_auth_user_legal_acceptance
  after insert on auth.users
  for each row execute procedure public.record_legal_acceptance();

-- The signup checkbox shipped before this table, so retain those acceptance records too.
insert into public.legal_acceptances (user_id, terms_version, accepted_at)
select
  id,
  raw_user_meta_data->>'terms_version',
  created_at
from auth.users
where raw_user_meta_data->>'accepted_terms_at' is not null
  and raw_user_meta_data->>'terms_version' is not null
on conflict (user_id) do nothing;
