create table if not exists public.legal_acceptances (
  user_id uuid primary key references auth.users(id) on delete cascade,
  terms_version text not null,
  accepted_at timestamptz not null
);

alter table public.legal_acceptances enable row level security;

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
      now()
    );
  end if;
  return new;
end;
$$;

create or replace trigger on_auth_user_legal_acceptance
  after insert on auth.users
  for each row execute procedure public.record_legal_acceptance();
