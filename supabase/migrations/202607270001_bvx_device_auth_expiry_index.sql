-- Index bvx_device_auth on expires_at.
--
-- consume_bvx_device and the device-pairing cleanup path filter/delete rows by
-- `expires_at > now()` on every `bvx login` exchange. Without an index this is a
-- sequential scan of the whole table on each request. Forward-only and
-- idempotent: safe to run repeatedly and on a fresh or partially-built database.

begin;

create index if not exists bvx_device_auth_expiry_idx
    on public.bvx_device_auth (expires_at);

commit;
