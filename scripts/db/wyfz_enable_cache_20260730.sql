-- Enable the per-tenant cache opt-in on wyfz, 2026-07-30.
--
-- WHY. Production has recorded zero billable savings since 2026-07-15 while
-- serving ~12k usage rows/day. The cause is not a billing bug: the savings path
-- is switched off in two independent places that both default to false and both
-- fail SILENTLY -- the proxy still answers 200, still writes usage rows, and
-- simply records verified_savings_usd = 0 forever.
--   1. process-level  BREVITAS_CACHE_ENABLED   (brevitas/proxy.py:483)
--   2. per-tenant     organizations.cache_enabled
--                     (api/server.py:1705 -> brevitas/proxy.py:501-505)
-- This file is HALF the switch -- #2. Without #1 set on the Railway API service
-- it produces no savings on its own.
--
-- SCOPE. All five organizations currently on wyfz, all owner-controlled
-- workspaces. Per-tenant opt-in is consent-based by design (caching changes
-- response provenance), so this list is explicit rather than a blanket UPDATE:
-- a future customer org must be added deliberately, with their agreement.
--
-- REVERSIBLE. Set cache_enabled = false for any id to undo. No data is written
-- or destroyed; this only changes whether the proxy may serve a cached replay.

begin;

update public.organizations
   set cache_enabled = true
 where id in (
   '9ec450cd-1922-4b32-beaf-ea3ca6aea173',  -- My workspace    (5892 rows / 7d)
   'd1715dcd-17d5-4970-893c-ee7321d8bfd3',  -- Brevitas        (1543 rows / 7d)
   '81b6e61d-b24a-4f68-9316-50d1ca09e3c4',  -- MyWorkspace123  (24 rows / 7d)
   '6e98fa14-9dbd-4798-98cd-ecc48816b622',  -- My workspace    (0)
   '7aadff24-0a47-4e31-a885-03e965e8b65a'   -- tie             (0)
 );

commit;

-- Postcondition: every row must read true.
select name, cache_enabled
  from public.organizations
 order by name;
