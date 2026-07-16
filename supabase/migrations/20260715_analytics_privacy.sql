-- Record the exact analytics/privacy notice acknowledged by new accounts.
-- Existing rows intentionally remain null rather than manufacturing consent history.
alter table public.legal_acceptances
  add column if not exists privacy_version text,
  add column if not exists analytics_notice_acknowledged_at timestamptz;

create or replace function public.record_legal_acceptance()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if new.raw_user_meta_data->>'accepted_terms_at' is not null
     and new.raw_user_meta_data->>'terms_version' is not null then
    insert into public.legal_acceptances (
      user_id, terms_version, accepted_at, privacy_version,
      analytics_notice_acknowledged_at
    ) values (
      new.id,
      new.raw_user_meta_data->>'terms_version',
      new.created_at,
      nullif(new.raw_user_meta_data->>'privacy_version', ''),
      nullif(new.raw_user_meta_data->>'analytics_notice_acknowledged_at', '')::timestamptz
    ) on conflict (user_id) do nothing;
  end if;
  return new;
end;
$$;
