# Brevitas handoff — 2026-07-31

Written for someone picking this up cold. Everything below was verified against
the live production database (`wyfz`) or the repo at `37b010e` on the date shown.
Where something is inferred rather than measured, it says so.

---

## 1. The one-paragraph version

The billing system is built, hardened, and correct. **It has never billed
anyone.** Production has 45,038 usage rows and **0 that are billable**, because
every one arrived from the downloadable `bvx` local proxy, which is
`authoritative=false` by design and can never be charged for. One company is
attested and has an active Stripe subscription. Zero `organization_service` keys
exist, which is the credential a customer needs to send billable traffic — so no
customer can currently reach the paid path at all. That is the single gate
between here and the first dollar, and it is not a code problem.

---

## 2. How the money actually works

1. A customer routes their LLM traffic through **our hosted gateway**
   (`api.brevitassystems.com`) instead of calling the provider directly.
2. We cache. A repeated request is served from cache, so they never pay the
   provider for it. That difference is `verified_savings_usd`.
3. Each week, a settlement computes **25% of verified savings** as our fee.
4. That fee becomes a Stripe meter event on a weekly metered subscription.

**Only step 1 through our hosted proxy is billable.** `POST /v1/usage` forces
`authoritative=False`, so anything the local `bvx` proxy reports is display-only,
forever. This is deliberate anti-forgery, not an oversight: a customer could
otherwise self-report savings we never witnessed.

### The four gates a fee passes

| gate | where | what it stops |
|---|---|---|
| `authoritative=true` | `api/server.py` `_hosted_proxy_receipt` | savings we did not witness |
| attestation | `organization_billing_arrangement` | billing a customer nobody approved |
| halting conditions | `202607280008` | implausible fees (ceilings, zero-spend, cumulative) |
| promotion | `promote_billing_period_settlement` | money moving without a human |

**Two of these are human by design and must stay that way.**
`attest_billing_arrangement` is executable by **no** PostgREST role — not even
`service_role` — so a leaked service key cannot make a customer billable.
`promote_billing_period_settlement` is `supabase_admin`/`postgres` only. Do not
grant either to `service_role`, and do not wrap them in a `SECURITY DEFINER`
function: a wrapper bypasses the grant, which is the only enforcement promotion
has.

---

## 3. Live production state (verified 2026-07-31)

| | |
|---|---|
| Organizations | 5 (all `cache_enabled = true`) |
| Attested / billable | 1 (`Brevitas`, `d1715dcd-…`) |
| Stripe subscriptions active | 1 (`cus_Uz7rXJAJEI2Vzv`) |
| Usage rows | 45,038 |
| **Billable (`authoritative`) rows** | **0** |
| Settlements ever created | 0 |
| **`organization_service` keys** | **0** |
| `anon`-executable SECURITY DEFINER fns | **0** (was 99) |
| Migrations applied | through `202607280038` |
| First billing period closes | 2026-08-07 |

Stripe live catalog verified 14/14: price `price_1TxKifF8wUpbDF4nZO3PdkKu`, meter
event `brevitas_fee_microusd`, weekly metered, sum aggregation, API version
`2026-06-24.dahlia`.

---

## 4. Do this first

### 4a. Production drift on the fee cap — VERIFIED, unfixed

`claim_billing_ledger_entry` and `claim_billing_ledger_entries` on wyfz are the
**2026-07-20** revision of `202607200006_company_billing_authorization.sql`. Two
later edits (`e20d02a`, `fa9a2a0`) were never applied.

Measured: repo head contains `outbound_started_at is not null` **twice**;
production contains it **zero** times. So production counts *every* `review` row
toward the period fee cap and `expected_period_microusd`, including rows provably
never sent to Stripe.

This is live mispricing exposure on the cap. It is currently harmless only
because nothing is billing. **Fix before billing turns on.**

### 4b. Mint an `organization_service` key

Nothing can bill until one exists. `brevitas connect` mints one;
`brevitas connect --multi-tenant` leaves it unpinned so a missing
`X-Brevitas-Customer-ID` is a hard 400 (correct when the customer resells to
their own end users).

### 4c. Sweep the 43 org-less API keys

`scripts/db/orgless_key_sweep_*.sql` reports them and **emits** the remediation
statements without running anything — the scripts are physically read-only
(an injected `UPDATE` is refused by the server). Revoking a key a customer is
using is an outage, so read the blast radius before running anything.

---

## 5. Things that will bite you

### Caching is gated four deep, and three gates are silent

`brevitas/semantic_cache.py` refuses to cache unless **`temperature` is
explicitly 0**, and refuses streams, tool calls, and non-text user messages.
Nothing customer-facing documents this. Typical real traffic — unset temperature,
streaming on — produces **zero cache hits, therefore zero savings, therefore zero
fee**, silently. If a customer reports "$0 savings", check this first.

### Only hosted-proxy traffic bills

Say this out loud in every sales conversation. A customer who installs `bvx` and
routes their whole company through it generates **$0**, correctly, forever.

### The test fakes have drifted from the database twice

Both times a fake encoded a *stricter* contract than the real SQL, so the suite
passed over a real money bug:

- `FakeSettlementStore` returned `occurred_at = period_end - 5 min` with a comment
  admitting `period_end` was "provably OUTSIDE reconcile's window" — hiding a
  defect where the first Stripe timeout froze a customer's billing permanently.
- The settlement-sweep fake halted on `if not account.attested`
  **unconditionally**, while the real gate is fee-conditional — hiding a defect
  where a `$0` draft permanently forecloses a week's revenue.

Mutation testing did **not** catch either, because the defect was in the
simulator, not the code under test. When you touch billing, check the fake
against the SQL before trusting a green suite.

### "Is this migration applied?" needs an EXACT fingerprint

wyfz has no `schema_migrations` table, so applied-ness is answered by
fingerprinting `pg_proc.prosrc`. A **loose** `LIKE` gave a false positive during
this work, because a pre-migration body contained the same substring. Match on
something only the new version can contain.

---

## 6. Provider lanes

| lane | status | notes |
|---|---|---|
| Anthropic direct | works | `/v1/messages` |
| OpenAI + compatible | works | 12 routes, incl. DeepSeek/Groq/xAI/resellers |
| **AWS Bedrock** | **built, not deployed** | bearer API key, no SigV4, no credential custody |
| **Azure OpenAI** | **built, not deployed** | `api-key` or Entra bearer, validated resource host |
| Google Vertex | specced only | model rides the URL; needs a path-parameterised route |

**Bedrock needs no new price rows** — Bedrock Claude is at on-demand parity with
Anthropic direct, and `model_price` resolves the route first with family
fallback. Proven to the cent through real settlement evidence: `$6.60` baseline,
`$1.50` fee, identical on both lanes. A `("bedrock", …)` row would win the moment
one is added.

### Deliberate refusals — do not "fix" these

- **Bedrock Converse.** `normalize_usage` drops its `cacheRead`/`cacheWrite`
  fields and `count_request_tokens` does not recognise its content blocks, so
  Converse traffic would measure **zero savings while looking healthy**.
- **`eu.` / `apac.` / `us-gov.` Bedrock inference profiles.** They bill *above*
  on-demand parity. Normalising them would understate spend, overstate savings,
  and **inflate the customer's invoice**. Refusing costs us revenue; accepting
  would cost them money.
- **Azure PTU.** Prepaid capacity has no marginal token cost, so those rows are
  unpriced and correctly bill `$0`.
- **Azure optimisation.** `body["model"]` is the customer's *deployment name*,
  which `MODEL_PRICES` cannot resolve, so a router would pick lossy strategies
  from a cost model keyed on a name it does not know.

---

## 7. The proxy gate — treat as security code

`api/server.py` `_protect_model_proxy`:

```python
if request.url.path not in _PROXY_PATHS:   # plus the prefix arm
    return await call_next(request)
```

**A path that misses this gate falls through to the app with no
authentication.** Everything that makes a proxied request safe hangs off passing
it: `x-brevitas-key`, the `proxy:invoke` scope, tenant scoping, admission
control, and the receipt bridge that makes a row billable.

The prefix arm (`/bedrock/`, `/azure/`) was attacked with 65 raw-socket hostile
targets against live uvicorn on both httptools and h11, and a 300,860-path
gate-vs-router parity fuzz: zero upstream reaches, zero violations.
Canonicalisation requires the routed form to equal the bytes on the wire.

If you add a route, add it here, and re-run `tests/test_proxy_gate_prefix.py`.

---

## 8. Onboarding a customer today

1. They sign up → workspace created → **caching on by default** (`202607280038`).
2. `pip install brevitas-systems` → `brevitas connect` → approve in browser.
   ⚠️ This currently asks them to open **devtools** and copy a Supabase
   `access_token` out of Local Storage. It works; it is embarrassing.
3. They set `base_url` and two headers. **Both are required** — the gateway
   authenticates on `X-Brevitas-Key` alone and `api_key=` is *not* consulted:

```python
client = Anthropic(
    base_url="https://api.brevitassystems.com/v1",
    api_key=os.environ["ANTHROPIC_API_KEY"],
    default_headers={
        "X-Brevitas-Key": os.environ["BREVITAS_API_KEY"],
        "X-Brevitas-Customer-ID": "their-company",
    },
)
```

4. Stripe Checkout → the webhook opens the arrangement **request** automatically.
5. **A human attests them.** `scripts/db/pending_arrangements.sql` lists who is
   waiting and emits the paste-ready statement. Connect as:

```
psql "host=aws-1-us-east-1.pooler.supabase.com port=5432 \
      user=brevitas_attestor.wyfzmfnswtzyhwbltbpy dbname=postgres sslmode=require"
```

The direct host is **IPv6-only** and will not resolve on an IPv4 machine — use
the pooler, and it is `aws-1`, not `aws-0`.

6. `brevitas billing-check` ⚠️ calls `GET /v1/billing/readiness`, which **does not
   exist** on the API server. It degrades with a clear message. Build it or drop
   the command.

---

## 9. Known-unfinished, roughly in priority order

1. **Production drift on the fee cap** (§4a) — verified, unfixed.
2. **Dashboard cache toggle.** Caching is now on by default with **no visible off
   switch** — only an API call the customer does not know exists. That is a worse
   consent story than off-by-default was, and it is the necessary companion to
   `202607280038`.
3. **Deploy the Bedrock and Azure lanes.** Built and tested; not live.
4. **The unrecognised-model credential path.** An unknown model with no upstream
   override still routes to `api.openai.com` carrying `authorization`. Closing it
   means not forwarding a credential to an upstream the caller never named, which
   risks breaking legitimate OpenAI traffic on model names we do not know yet.
   Needs a decision, not a quiet patch.
5. **`GET /v1/billing/readiness`** (§8.6).
6. **The devtools step** in `brevitas connect` (§8.2).
7. **53.4% of production usage rows are `unpriced`** (24,036 of 45,038), mostly
   aggregator traffic. Safe — unpriced rows never enter the fee basis — but half
   the savings story is invisible to the customer.

---

## 10. Rules to keep

- **Never** `supabase db push`. Remote migration history is empty. Apply one file
  at a time: `supabase db query --linked -f <migration>`.
- **Never** grant `attest_billing_arrangement` or
  `promote_billing_period_settlement` to `service_role`, and never wrap them.
- **Never** arm `BREVITAS_BILLING_SETTLEMENT_SWEEP_ENABLED` without reading
  `api/billing_settlement_sweep.py` first. It is off, deliberately.
- `npm run build:dashboard` — never `cd dashboard && npm run build`, which emits a
  bundle with no Supabase config.
- Migrations are checksum-frozen. Editing one means refreezing
  `scripts/ci/migration-frozen-checksums.txt` and it must never be edited after
  being applied to production.

## Full gate

```
node scripts/ci/verify-migrations.mjs
npm test                                   # 338
.venv/bin/python -m pytest -q              # 1976
createdb -h 127.0.0.1 -U <you> ci && \
  DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/ci \
  bash scripts/ci/run-migration-tests.sh   # needs local postgres@17
```
