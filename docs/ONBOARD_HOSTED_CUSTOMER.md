# Onboarding a hosted (billable) customer

Rewritten 2026-07-30 around `brevitas connect`. Order of this document is the order
things actually happen: **what the customer does**, then **what the operator does**,
then **what either side verifies**.

## Why hosted, and why the local install can never be the billable path

The hosted proxy is the only path that produces billable savings.

The local `bvx install` / `brevitas start` proxy is a good product and the right
answer for a privacy-sensitive team, but it is **architecturally unbillable on
percentage-of-savings**. Its receipts arrive over `POST /v1/usage`, which forces
`authoritative=False` (`api/server.py:4907`) because a client-side proxy may not
certify its own savings. That is a deliberate anti-forgery property, not a bug to
fix later.

Confirmed live: production traffic is 100% `receipt_source='proxy'`,
`authoritative=0`, with a blank `organization_id`. There is no amount of local-proxy
traffic that turns into an invoice.

So: **lead with hosted.** Offer local as the privacy-first option and say plainly
that it is analytics, not billing.

---

## Implementation status of this flow

This document describes the flow on branch `security/enterprise-audit-2026-07-30`.
Each row must be true of the deployment you are pointing a customer at before you
hand them this document. A manual fallback is given for every step, so the doc is
usable even where a row is still open.

| Piece | Artifact | Fallback if not yet shipped |
|---|---|---|
| `brevitas connect` | `brevitas/cli.py` (click group, `pyproject.toml:46`) | Dashboard → Company Administration → Create service account |
| `POST /v1/device-auth/*` `purpose='hosted_service'` | `api/server.py` (2430 / 2449 / 2571) | same as above |
| `service_accounts.default_customer_external_id` | **NOT SHIPPED** — designed only, no migration defines it | send `X-Brevitas-Customer-ID` on every request — always valid, and required today |
| `GET /v1/billing/readiness`, `brevitas billing-check` | **NOT SHIPPED** — no such route in `api/` | the SQL in §3.2 |
| `attest_billing_arrangement()` + `brevitas-ops attest` | migration `202607280030`, `scripts/ops/attest.py` | hand-written SQL by a human with a non-`service_role` DSN |

`bvx connect` is **not** deliverable from this repository. `bvx` is a Go CLI released
separately (`docs/INSTALL.md:23`). Everything here ships as `brevitas connect` from
this repo; `bvx connect` is a thin Go wrapper over the same three endpoints and is a
separate release.

---

# 1. What the customer does

One command. No local proxy, no `uvicorn`, no loopback base URL.

```bash
pip install brevitas-systems
brevitas connect
```

`connect` opens the browser, the customer signs in and approves (the same
browser-authorization exchange `bvx install` already uses), and the CLI receives an
`organization_service` key scoped `proxy:invoke`, `usage:write`, `usage:read_own`,
`customer:route`, `customer:auto_provision`, expiring in 90 days.

Approving requires the `service_accounts:manage` permission — `company_owner` or
`company_admin`. A `member` or `billing_admin` gets a 403 and the CLI says to ask an
owner to run it.

It prints:

```
Connected  Acme, Inc.  ·  org 8f3c…9a21  ·  service account bvx:macbook-pro
Key        bvt_…c41d      shown once — this CLI does not store it
Customer   acme           pinned to this key; send it on every request
Endpoint   https://api.brevitassystems.com/v1
Expires    2026-10-28     (90 days — rotate before then)

Billable: not yet — 0 authoritative rows. Send one request, then:  brevitas billing-check
```

## 1.1 The three lines that go into their app

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.brevitassystems.com/v1",
    api_key=os.environ["BREVITAS_API_KEY"],
    default_headers={"X-Brevitas-Customer-ID": "acme"},
)
```

```bash
export BREVITAS_API_KEY=bvt_...
export OPENAI_BASE_URL=https://api.brevitassystems.com/v1
export BREVITAS_CUSTOMER_ID=acme
```

That is the whole client-side change. No install, no background service.

## 1.2 `X-Brevitas-Customer-ID` — read this before the first request

**An `organization_service` key hard-400s on every proxy call without this header.**
Verbatim, from `api/server.py:1755-1757`:

```
400  {"detail": "Organization service proxy calls require X-Brevitas-Customer-ID"}
```

This is the single most likely reason a new integration fails on its first request.
It is deliberate: one org key can route traffic for many end customers, and the
header is what says which one. Attribution is never inferred, never fuzzy.

**Send it on every request. There is no exception and no fallback.**

A per-key `default_customer_external_id` pin — so a header-less call would resolve
to a default instead of 400ing — was designed but is **NOT SHIPPED**. The column
exists in no migration; `202607280030` is attestation-only. An earlier draft of
this document described the pin in the present tense, which would have sent a
design partner into exactly the first-request failure this document exists to
prevent. The gate at `api/server.py:1755-1757` is unconditional today.

Two shapes, both of which send the header:

- **The customer is the tenant** (the normal design-partner case). Pick a stable
  external id — their company slug — and set it once in their client's default
  headers, as in the snippet above. One line, sent on every request.
- **The customer resells to their own customers.** They send their end-customer's
  id per request and get per-end-customer attribution for free. This is the case
  the mandatory header exists for: silently attributing a reseller's second tenant
  to their first would be unfixable after the fact.

Nobody should discover their attribution model by reading an invoice.

## 1.3 Their provider key

Either configured with us, or sent per-request as BYOK (`byok_provider` /
`byok_key` — request-scoped, never stored, never logged, `api/server.py:4087-4090`).
BYOK is the better story for a privacy-sensitive partner and worth leading with.

## 1.4 What `connect` writes to disk

- **Nothing secret, by default.** `brevitas/config.py` states Brevitas keeps no
  config file and `brevitas config` persists nothing; silently writing a billable
  secret would reverse that posture.
- Always: `${XDG_CONFIG_HOME:-~/.config}/brevitas/connection.json`, mode `0600`,
  **secret-free** — base url, org id and name, service account id, key prefix,
  customer external id, expiry. Enough to name the org in `billing-check` and to
  warn 14 days before the 90-day expiry. No key, no key hash.
- `--env-file [PATH]` is **opt-in**. It refuses a git-tracked path or one not matched
  by `.gitignore`, appends only, refuses to replace an existing `BREVITAS_API_KEY`
  without `--force`, `chmod 0600`s the file, and echoes exactly which lines it added.
- `--store-key` (opt-in) puts the secret in the OS keyring, mirroring what `bvx`
  already does for device keys.

Default off means the key exists exactly twice: in the customer's secret manager,
and hashed in `api_keys`.

---

# 2. What the operator does

Exactly two things, and only one of them is per-customer.

## 2.1 Per-org, once: caching

Savings only exist if the cache runs, and both halves are required — the process
flag `BREVITAS_CACHE_ENABLED=true` on the API service (set once, globally) **and**
the per-tenant opt-in:

```sql
update public.organizations
   set cache_enabled = true
 where id = '<their organization uuid>';
```

Apply through the sanctioned path only: `supabase db query --linked -f <file>.sql`.
Never `db push` — production has no migration ledger.

## 2.2 Per-org, once: attest the billing arrangement

`202607280009` makes `public.organization_billing_arrangement` unwritable **even by
`service_role`**. No org can be billed until a human attests. That is the last thing
standing between a bug and an unauthorized charge, and it stays deliberately manual.

What changes is only that the SQL is gone, not the judgement:

```bash
brevitas-ops attest --list
# → open requests: org, who accepted terms, when, agreement reference,
#   self-declared arrangement, and last-24h authoritative/priced/savings counts

BREVITAS_ATTESTOR_DSN=… brevitas-ops attest \
    --org 8f3c…9a21 \
    --arrangement marginal_per_call \
    --evidence "MSA 2026-08-03 §4, DocuSign 3f2a…" \
    --request <request uuid>
```

This calls `public.attest_billing_arrangement(...)`, a `security definer` function
whose EXECUTE is revoked from `public`, `anon`, `authenticated` **and `service_role`**
and granted to exactly one role, `brevitas_attestor`, that no deployed service holds.
Inside, it re-checks `session_user = 'brevitas_attestor'` so a future accidental
GRANT still cannot open it, and it writes an insert-only log row (prior value, new
value, attested by, `session_user`, evidence, request id, timestamp).

### What stops a customer — or a leaked service key — self-attesting

1. `organization_billing_arrangement` still has no INSERT/UPDATE/DELETE for
   `service_role`, `anon`, or `authenticated`.
2. EXECUTE on the attest/revoke functions is granted to one role, held by no service.
3. The `session_user` check inside the function survives a mis-grant.
4. `BREVITAS_ATTESTOR_DSN` must appear in **no** deployed environment — not Railway,
   not Vercel, not GitHub Actions.
5. The customer-facing request row is inert. Nothing in the settlement path reads
   it. A customer can generate a thousand and no fee becomes possible.

Point 4 is the one guarantee that rests on human discipline rather than on the
schema. Saying so is better than pretending otherwise; CI asserts the absence, but
CI is not the database.

### What stays slow because it is a safety gate

A human still has to read the actual provider agreement and decide
`marginal_per_call` vs `committed_capacity`. `202607280009`'s reasoning is right:
`usage_log.actual_cost_usd` is static list price with no organization dimension, so
a PTU customer is *indistinguishable* from a pay-as-you-go one in the data. The
customer's `self_declared_arrangement` is an **input to that human**, never the
value written.

Attest only after the customer has agreed to 25%-of-verified-savings in writing, and
record which agreement the attestation reflects. The RPC enforces "evidence is
non-empty and at least 8 characters". It cannot enforce "evidence is true".

## 2.3 Then, weekly

1. Customer completes Stripe Checkout from the dashboard → `subscription_status`
   becomes `active`, period exactly 604800 seconds wide.
2. Let a full seven-day period close.
3. `select public.settle_billing_period(<org>, <anchor>, '<who>');` → a `draft`.
4. **Read the draft before promoting it.** This is the human checkpoint.
5. `select public.promote_billing_period_settlement(<id>, 'operator:<you>', '<note>');`
   → `pending`.
6. The `202607280029` claim/send worker reports it; Stripe invoices.

Steps 3-5 are one action per org per week. Fine for five partners; automate before
fifty.

---

# 3. Verifying — before anyone promises an invoice

## 3.1 The customer's own check

```bash
brevitas billing-check
```

Renders `GET /v1/billing/readiness`: a fixed checklist where every "no" names its own
fix — hosted key, customer routing, cache enabled, authoritative rows, priced rows,
cache-attributable rows, rows with savings, verified savings, billing arrangement,
subscription — and a single `billable` boolean. It exits non-zero unless `billable`
is true, so a design partner can wire it into their own smoke test.

Auth is a dashboard session for an org member, **or** an `organization_service` key
with `usage:read_own`, so it works from CI with the same key the app uses.

Three honesty rules that route inherits from `/v1/admin/billing/settlement`:

- **Never report `0` for a failed query.** An unavailable check returns
  `{"ok": null, "detail": "unavailable"}`. A confident `$0.00` over a broken read is
  the exact failure that route exists to avoid.
- **Report rows and savings, never a fee.** Settlement is period-scoped and netted
  (0007 / 0008 / 0012 / 0013). A per-window fee estimate here would be a number we
  cannot honour.
- **Say when savings are cache-replay only** (see §4) rather than letting the
  customer discover it from a $0 invoice.

A customer seeing `billing_arrangement: unattested`, `billable: false` while their
savings are real is the fail-closed default working *visibly*. That state is not an
operator secret.

## 3.2 The operator's SQL (also the fallback if `/v1/billing/readiness` is not live)

Run after real traffic. Every column must be non-zero.

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

- `authoritative = 0` → still on the local proxy, or using a device key rather than a
  service-account key. **Not billable.** Fix before continuing.
- `organization_id` blank → same diagnosis; the local proxy path carries no org.
- `priced = 0` → no provider receipt is reaching pricing.
- `with_savings = 0` but `brevitas_cache > 0` → the cache is running but every request
  is a first-sight miss. Savings appear on the *second* identical request.

---

# 4. Known gaps to disclose honestly

**Cache-only savings settle at $0 today.** If a customer's savings come *only* from
cache replays, today's rules settle them at zero. `202607280008`'s
`zero_spend_concentration` halts any period where zero-spend rows contribute more
than half the savings, and a cache hit is a zero-spend row by construction. The fix
(`202607280028`) is written but **quarantined** — review found a path where a
$0.0000000001 payment unlocked $125 of billing — pending a redesign that treats the
earlier paid request as the *price source* rather than a mere existence gate.

Until that lands, a customer whose savings are purely cache-driven settles at zero.
Know that before you promise anyone an invoice, and do not paper over it in
onboarding copy.

**90-day key expiry silently stops traffic.** The service key `connect` mints expires
in 90 days. `connection.json` lets `billing-check` warn 14 days out, but a customer
who ignores it loses their integration, not just their billing.

**The local proxy banner.** `brevitas start` should say out loud that its receipts
are recorded `authoritative=false` — analytics, not billing. Whether the local
install remains the headline product is a positioning decision, but the honest line
ships either way.
