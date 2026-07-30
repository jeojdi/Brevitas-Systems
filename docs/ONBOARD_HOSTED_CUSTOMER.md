# Onboarding a hosted (billable) customer

Written 2026-07-30. This is the path that produces **billable** savings. The local
`bvx` install is a fine product but is architecturally unbillable on
percentage-of-savings: its receipts arrive over `POST /v1/usage`, which forces
`authoritative=false` (`api/server.py:4907`) because a client-side proxy may not
certify its own savings. Confirmed live: 963 production rows in three hours, all
`receipt_source='proxy'`, all `authoritative=0`, all with a blank
`organization_id`.

Everything below is per-customer and takes minutes once the prerequisites are done.

---

## Prerequisites (once, not per customer)

| # | Item | Owner | State as of 2026-07-30 |
|---|------|-------|------------------------|
| P1 | Billing schema on wyfz | me | **done** — chain + realignment, md5-verified |
| P2 | App code deployed | done | **done** — PR #39 merged, `a459289` |
| P3 | `BREVITAS_CACHE_ENABLED=true` on the API service | you | **done** |
| P4 | Per-tenant `organizations.cache_enabled` | me | **done** for all 5 orgs |
| P5 | Settlement sender (`202607280029`) built, reviewed, applied to wyfz | me | **in progress** — the last code blocker |
| P6 | `BREVITAS_BILLING_WEEKLY_CAP_USD` on Vercel + Railway | you | **missing in both** |
| P7 | Live Stripe: rotate leaked key, live catalog, webhook, portal | you + me | not started |

**Nothing bills until P5 and P7.** P5 is mine; P7 needs your Stripe dashboard.

---

## Per-customer, step by step

### 1. Create their organization and a service account

The customer needs an `organization_service` key, **not** a device key from
`bvx login`. Only that key type re-resolves through
`_authoritative_service_key_context()` (`api/server.py:1405-1414`) and carries a
real `organization_id` — which is what makes their usage attributable and
therefore billable.

Issue it from the company-admin API (`api/company_admin.py:1559`):

    POST /v1/company/service-accounts

Give the key the scopes it actually needs: `proxy:invoke`, `usage:write`, and
`customer:route` (see step 2 for why the last one matters).

### 2. Decide the customer-identity header now, not later

An `organization_service` key **hard-400s on every proxy call** without an
`X-Brevitas-Customer-ID` header:

> `api/server.py:1755-1757` — "Organization service proxy calls require
> X-Brevitas-Customer-ID"

This is deliberate: one org key can route traffic for many end customers, and the
header says which. It is also the single most likely thing to make a new
integration fail on the first request, and the hosted README snippets omit it.

Two shapes:

- **The customer is the tenant** (the normal case for a design partner): pick a
  stable external id for them, e.g. their company slug, and have them send it on
  every request.
- **The customer resells to their own customers**: they send their end-customer's
  id, and you get per-end-customer attribution for free. Needs
  `customer:auto_provision` if you want ids created on first sight.

### 3. Point them at the hosted endpoint

This is the whole client-side change — one config line, not an install:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.brevitassystems.com/v1",
    api_key="<their Brevitas organization_service key>",
    default_headers={"X-Brevitas-Customer-ID": "<their tenant id>"},
)
```

Their provider key: they either configure it with you, or send it per-request as
BYOK (`byok_provider` / `byok_key`, request-scoped, never stored, never logged —
`api/server.py:4087-4090`). BYOK is the better story for a privacy-sensitive
partner and worth leading with.

### 4. Turn caching on for their org

Savings only exist if the cache runs. Both halves are required — the process
flag (already set) **and** the per-tenant opt-in:

```sql
update public.organizations
   set cache_enabled = true
 where id = '<their organization uuid>';
```

Apply through the sanctioned path: `supabase db query --linked -f <file>.sql`.

### 5. Attest the billing arrangement

`202607280009` makes `organization_billing_arrangement` unwritable even by
`service_role`: **no org can be billed until a human attests the arrangement out
of band.** That is the last thing standing between a bug and an unauthorized
charge, so it is deliberately manual.

Attest only after the customer has actually agreed to 25%-of-verified-savings in
writing. The attestation should record which agreement it reflects.

### 6. Verify their traffic is billable BEFORE promising an invoice

Run this after they send real traffic. Every column must be non-zero.

```sql
select count(*)                                          as rows,
       count(*) filter (where authoritative)             as authoritative,
       count(*) filter (where pricing_status = 'priced') as priced,
       count(*) filter (where cache_attributable)        as brevitas_cache,
       count(*) filter (where verified_savings_usd > 0)  as with_savings,
       round(coalesce(sum(verified_savings_usd), 0)::numeric, 6) as savings_usd
  from public.usage_log
 where organization_id = '<their organization uuid>'
   and ts >= now() - interval '24 hours';
```

- `authoritative = 0` → they are still on the local proxy, or using a device key
  instead of a service-account key. Not billable. Fix before continuing.
- `priced = 0` → no provider receipt is reaching pricing.
- `with_savings = 0` but `brevitas_cache > 0` → cache is running but every request
  is a miss; savings appear on the *second* identical request, not the first.

### 7. Subscribe them, then settle their first week

1. They complete Stripe Checkout from the dashboard (`subscription_status` becomes
   `active`, with a period exactly 604800 seconds wide).
2. Let a full seven-day period close.
3. `settle_billing_period(org, period_anchor, '<who>')` → a `draft` with the fee
   at 25% of verified savings.
4. **Read the draft before promoting it.** This is the human checkpoint.
5. `promote_billing_period_settlement(id, 'operator:<you>', '<note>')` → `pending`.
6. The worker claims it and reports it to Stripe; Stripe invoices.

Steps 3-5 are manual by design, one action per org per week. Fine for five
partners; automate it before fifty.

---

## Known gap to disclose honestly

If a customer's savings come **only** from cache replays, today's rules settle
them at **$0**. `202607280008`'s `zero_spend_concentration` halts any period where
zero-spend rows contribute more than half the savings, and a cache hit is a
zero-spend row by construction. The fix (`202607280028`) is written but
quarantined — its review found a path where a $0.0000000001 payment unlocked $125
of billing — pending a redesign that treats the earlier paid request as the
**price source** rather than a mere existence gate.

Until that lands, a customer whose savings are purely cache-driven will settle at
zero. Know that before you promise anyone an invoice.
