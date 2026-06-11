create table if not exists user_keys (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid references auth.users(id) on delete cascade not null unique,
  api_key    text not null,
  created_at timestamptz default now()
);

alter table user_keys enable row level security;

create policy "users can access only their own key"
  on user_keys for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);
