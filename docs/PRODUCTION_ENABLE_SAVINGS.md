# Turning savings back on in production

Prepared 2026-07-30. **Nothing here has been applied.** Every step is yours to run, in this order,
and each says what it changes and how to check it worked.

Why this document exists: production has recorded **zero** authoritative, priced usage rows since
2026-07-15 — 12k rows/day of real traffic, all of it non-billable. The root cause is not a bug in
billing (`docs/SAVINGS_DROUGHT_DIAGNOSIS.md`): the savings-producing path is switched **off**, in
two independent places that both default to off, and it fails **silently** — the proxy still
answers 200, still writes usage rows, and simply records `verified_savings_usd = 0` forever.

---

## 0. Do this first — it is losing evidence every 5 minutes

The deployed worker calls `purge_warm_state(7)` on a 300-second loop, deleting `warm_budget_ledger`
rows older than 7 days. That table is the only record of what a warming ping cost, and settlement
*recomputes* the warm deduction from it. A 7-day billing period is therefore always settled after
its earliest days have been purged, which makes the deduction shrink and the fee ceiling rise —
an overcharge built from the customer's own provider budget.

Migration `202607280017` floors this at 365 days inside the function and **is already applied to
wyfz**, so the structural fix is in. Set the variable anyway so config and structure agree:

**Railway → worker service → Variables**

    BREVITAS_WARM_RETENTION_DAYS=365

Redeploy the worker. Verify:

```sql
-- expect: floor honoured, i.e. nothing younger than 365 days is being deleted
select count(*) as rows, min(day) as oldest, max(day) as newest
  from public.warm_budget_ledger;
```

---

## 1. The two switches that stop savings from existing

Both default **false**. With either one off, the proxy serves traffic normally and books zero
savings, with no error anywhere — exactly the shape production shows today.

### 1a. Process-level (the API service)

`brevitas/proxy.py:483` reads this; without it the cache layer never runs at all.

**Railway → API service → Variables**

    BREVITAS_CACHE_ENABLED=true

### 1b. Per-tenant opt-in (the database)

`api/server.py:1705` → `brevitas/proxy.py:501-505`: even with the process switch on, each tenant
must be opted in. This is deliberate — caching changes response provenance, so it is consent-based.

```sql
-- Inspect first. Never blanket-enable every org; pick the ones that have agreed.
select id, name, cache_enabled
  from public.organizations
 order by name;

-- Then, per org you have consent for:
update public.organizations
   set cache_enabled = true
 where id = '<organization uuid>';
```

Run through the sanctioned path, one file at a time:
`supabase db query --linked -f <file>.sql`

---

## 2. Billing configuration that is currently missing

`BREVITAS_BILLING_WEEKLY_CAP_USD` is absent from **both** Vercel and Railway. The Next.js side
requires `0 < cap <= 100000`; the worker **raises at startup** without it. Pick a deliberately low
ceiling for the first weeks — it is a safety net, not a target.

**Vercel → Project → Settings → Environment Variables** (Preview *and* Production)

    BREVITAS_BILLING_WEEKLY_CAP_USD=25
    BREVITAS_PUBLIC_URL=https://brevitassystems.com

**Railway → worker service**

    BREVITAS_BILLING_WEEKLY_CAP_USD=25

Leave `BREVITAS_BILLING_ENABLED=false` everywhere until the go/no-go list in
`docs/STRIPE_TEST_PLAN.md` §5 is walked. It is `true` only in local `.env.local`.

---

## 3. Confirm savings are actually being produced

After 1a + 1b and a redeploy, send real traffic through the hosted proxy, then:

```sql
select date_trunc('hour', ts) as hour,
       count(*)                                        as rows,
       count(*) filter (where authoritative)           as authoritative,
       count(*) filter (where pricing_status = 'priced') as priced,
       count(*) filter (where cache_attributable)      as brevitas_cache,
       count(*) filter (where verified_savings_usd > 0) as with_savings,
       coalesce(sum(verified_savings_usd), 0)          as savings_usd
  from public.usage_log
 where ts >= now() - interval '6 hours'
 group by 1 order by 1 desc;
```

**What good looks like:** `authoritative` and `priced` climbing, and `with_savings > 0` on the
second and subsequent identical requests (the first call is a cache miss and saves nothing — that
is correct, not a fault).

**If `authoritative` stays 0:** traffic is not traversing the hosted proxy. Local `bvx` proxies
report over `POST /v1/usage` and are forced non-authoritative by design (the anti-forgery
boundary). That is an integration question, not a billing one.

**If `priced` stays 0:** no provider receipt is reaching the pricing path.

---

## 4. Known gap — cache savings still bill $0

Even with everything above working, a period whose savings come only from cache replays will
**settle at zero**. `202607280008`'s `zero_spend_concentration` halts any period where zero-spend
rows contribute more than half the savings, and a cache replay is a zero-spend row by construction.

The fix (`202607280028`) is written but **quarantined** — its adversarial review found one blocker
and three high findings, including a path where a single $0.0000000001 payment unlocked $125 of
billing. It is untracked, unregistered, and **not applied anywhere**. Do not apply it.

Pending owner decision on the replacement design, which should treat the earlier paid call as the
**price source** (bill 25% of `tokens_saved × the customer's observed price`) rather than as a
mere existence gate.

---

## 5. Order of operations

1. `BREVITAS_WARM_RETENTION_DAYS=365` — now, independent of everything else
2. Merge PR #39 and let Vercel + Railway deploy (the database is already ahead of the code, which
   is the safe direction)
3. `BREVITAS_CACHE_ENABLED=true` + per-tenant `cache_enabled`
4. `BREVITAS_BILLING_WEEKLY_CAP_USD` on both hosts
5. Watch §3 for a few hours — confirm savings exist before any billing flip
6. Live Stripe setup, then `BREVITAS_BILLING_ENABLED=true` last
