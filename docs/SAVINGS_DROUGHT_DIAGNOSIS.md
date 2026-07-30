# Savings drought: root-cause analysis from the code

**Question.** Production has had **zero `authoritative` rows and zero `pricing_status='priced'`
rows since 2026-07-15**, while real traffic flows (12k rows/day since 07-27). Why?

**Scope of this document.** Read-only analysis of this checkout
(`chore/retire-per-row-fee-trigger` @ `71e20ef`, dirty working tree). Every claim cites a
`file:line` I opened. **No production access was used**; every hypothesis below ends with a
single read-only check for the supervisor to run.

**Verdict up front.** This is **not one fault, it is three independent ones**, and they compose
so that fixing any one alone changes nothing:

| # | Fault | Kills |
|---|---|---|
| A | No traffic traverses the in-process hosted bridge, so nothing is `authoritative` | `authoritative`, and therefore `verified_savings_usd` and `brevitas_fee_usd` |
| B | `pricing_status` is decided **synchronously at insert** and is failing its own precondition (`receipt_available` / `model_price`) — independently of `authoritative` | `priced`, `baseline_cost_usd`, `actual_cost_usd`, `measured_savings_usd` |
| C | `public.usage_log` on wyfz was missing 11 columns, so **every** `POST /v1/usage` 400'd from 07-18 to 07-27 | all rows in the gap window |

Fault C is already remediated (that is *why* traffic "resumes" on 07-27). Faults A and B are
live. Fault B is the more diagnostic one, because **pricing does not depend on `authoritative`
at all** — see §3.

---

## 1. Every condition required to mint an authoritative PRICED row with `verified_savings_usd > 0`

The chain, in execution order. All of these must hold for one request.

### 1.1 Routing — the request must reach the hosted proxy, in the hosted process

1. **Path must be a proxy path.** `_PROXY_PATHS` = `/v1/messages`, `/v1/chat/completions`,
   `/v1/responses`, `/v1/embeddings`, `/v1/completions` and their `/openai/*` aliases
   (`api/server.py:1220-1224`). Anything else (including `/v1/usage`) is not the bridge.
2. **The proxy router must be mounted in the same process as the reporter.** It is:
   `api/server.py:5985-5990` imports `proxy_app` and calls
   `set_usage_reporter(_hosted_proxy_receipt)` at import time, then
   `app.include_router(proxy_app.router)`. The container runs exactly this one app
   (`Dockerfile` `CMD … python -m api.serve` → `api/serve.py:28` `uvicorn.run("api.server:app")`).
   So **any** request that lands on a `_PROXY_PATHS` route of the Railway service produces an
   authoritative row. There is no env flag that disables the bridge.
3. **Corollary — the in-process reporter is the *only* difference.** `brevitas/proxy.py:773-797`:
   `_emit_usage` uses `_usage_reporter` when installed; when it is `None` (i.e. a **local** `bvx`
   proxy on a customer machine) it falls back to `brevitas._compress.report_usage`, which does an
   HTTP `POST {base_url}/v1/usage` (`brevitas/_compress.py:248-253`, base URL default
   `https://api.brevitassystems.com`, `brevitas/config.py:7,44`). That HTTP handler hardcodes
   `authoritative=False` (`api/server.py:4778-4783`) — by design, anti-forgery. **Same payload,
   same `receipt_source="proxy"`, structurally unbillable.**
4. **Edge routing.** `https://brevitassystems.com/v1/*` is rewritten to `BREVITAS_API_URL`
   (`next.config.ts` `rewrites().afterFiles`, `{ source: '/v1/:path*', destination:
   `${backendApiHost}/v1/:path*` }`), and `resolveBackendApiHost()` hard-fails a production build
   without an `https:` `BREVITAS_API_URL`. So the README's hosted config
   (`README.md:170` `ANTHROPIC_BASE_URL="https://brevitassystems.com"`) does reach the bridge —
   **but only for `/v1/*`**. `/openai/v1/chat/completions` (the local-proxy form,
   `README.md:46`) has **no Vercel rewrite** and would 404 at the marketing origin.
   `vercel.json` contains no rewrites at all; all of it is `next.config.ts`.

### 1.2 Auth — `_protect_model_proxy` (`api/server.py:1727-1786`)

5. `X-Brevitas-Key` present (`:1731-1737`; missing ⇒ 401, or 503 in production).
6. Key resolves and **`permits("proxy:invoke")`** (`:1753-1754`; else 403).
7. **If `key_type == "organization_service"`, `X-Brevitas-Customer-ID` is mandatory** — else a
   hard **400 "Organization service proxy calls require X-Brevitas-Customer-ID"**
   (`:1755-1758`). ⚠️ The documented Claude Code / Codex header sets are
   `X-Brevitas-Key`, `X-Brevitas-Repo`, `X-Brevitas-Client` — **no customer id**
   (`README.md:160-173`). Any org-service key following the docs 400s on every call.
8. Rate/admission lease must be granted (`:1796-1813`; else 429/503).
9. `_proxy_auth_context.set(auth_context)` (`:1768`) — needed later by `_hosted_proxy_receipt`.

### 1.3 Provider leg — a receipt must exist

10. The upstream call must succeed (`_upstream_ok`, `brevitas/proxy.py:1292`) and, for streams,
    the stream must **complete**: `if completed:` gates the whole receipt write
    (`brevitas/proxy.py:1259-1275`). A client disconnect ⇒ no row at all.
11. `has_receipt = receipt.total_tokens > 0` (`brevitas/proxy.py:952`). This becomes the wire
    field `receipt_available` (`:982`) and, when false, the strategy is **suffixed
    `:missing_receipt`** (`:988`) — a directly observable fingerprint in `usage_log.strategy`.
    Streamed Anthropic responses are parsed by `SSEUsageParser` (`:1244,1253,1265`); streamed
    OpenAI/xAI only carry usage because the proxy now injects
    `stream_options.include_usage` (`:95-105`, kill switch `BREVITAS_STREAM_USAGE_INJECT`) —
    that injection landed **2026-07-28** (`c103d87`), so anything streamed before it has
    `receipt_available=false`.

### 1.4 Receipt write — `_hosted_proxy_receipt` (`api/server.py:5857-5908`)

12. Non-empty raw key, and `_key_validity(kh) == "valid"` (`:5868-5875`; both exits log
    `hosted proxy receipt dropped reason=…`).
13. `request_id` is re-minted server-side into the reserved `proxy:` namespace unless it already
    is (`:5882-5883`; `RECEIPT_ID_PREFIX`, `brevitas/proxy.py:736,739-770`). A collision logs
    `hosted proxy receipt dropped reason=duplicate_request_id` (`:5905-5908`).
14. Calls `_record_usage_report(…, authoritative=True, …)` (`:5900-5901`) — **the only
    `authoritative=True` in non-test code in the repo** (verified by grepping
    `authoritative=True` across `*.py`; the only other hits are
    `api/server.py:5821` and `api/worker.py:260`, which are *health-dependency* descriptors, and
    tests).

### 1.5 Pricing — `_record_usage_report` (`api/server.py:4526-4722`)

15. Not a dedupe hit on `(key_hash, request_id, authoritative)` (`:4548-4552`).
16. **`body.receipt_available` must be true**, else `costs` is hardcoded to
    `pricing_status="unpriced"` with all cost legs `None` (`:4581-4587`). Default on the model is
    `True` (`api/server.py:4347`), so this is only false when a client explicitly says so.
17. **`model_price(provider, model)` must resolve**, else `calculate_costs` returns
    `pricing_status="unpriced"` (`brevitas/receipts.py:364-372`). Resolution is exact-alias +
    longest dated-snapshot prefix only, never family guessing (`:346-361`). Reseller providers
    (`openrouter`, `together`, `fireworks`, `groq`, `perplexity`, `:322-324`) are canonicalised to
    themselves and have **no `MODEL_PRICES` rows at all** ⇒ always unpriced. The table
    (`:281-312`) has `grok-4.5` and `grok-4.1-fast` but **not** the live `grok-4.3` /
    `grok-4.20-*`; it has `deepseek-chat`, `claude-sonnet-5`, `gpt-5.6`.
18. `pricing_status` is then persisted verbatim (`:4703`).

### 1.6 Verified savings — the money gate (`api/server.py:4615-4670`)

19. `mode = _verification_mode(strategy, cache_attributable)` must be `"byte_preserving"`
    (`:4389-4400`): the strategy must contain one of `exact_cache, native_cache, cache_only,
    passthrough, byte_preserving, lossless`, **or** `cache_attributable` must be true.
    Anything containing `retrieve|retrieval|llmlingua|lossy|semantic_cache|compress` is
    `quality_affecting` and needs an explicit `quality_verified=True` that no client sends.
20. `quality_status == "verified"` (`:4618-4633`).
21. `strategy != "cache_warm"` (`:4663`) — warming pings are spend, never savings.
22. `authoritative` true (`:4661`).
23. **And `measured_savings_usd` must actually be > 0.** This is the quietest condition:
    - `baseline_tokens = receipt.input_tokens + (baseline_tokens − compressed_tokens)`
      (`_anchor_token_legs`, `:4495-4523`). Anchoring moves the **level**, never the **delta**
      (`:4460-4476`).
    - For a plain passthrough the proxy sends `compressed_tokens = baseline`
      (`brevitas/proxy.py:979`) ⇒ delta 0.
    - With delta 0, `measured > 0` requires the **native-cache discount** branch, i.e.
      `cache_attributable=True`: only then is the baseline priced at the full input rate for all
      tokens while the actual uses the cached rate (`brevitas/receipts.py:382-397`).
    - `cache_attributable` is `meta["cache_control_owner"] == "brevitas"`
      (`brevitas/proxy.py:1213`), which requires that **the caller did not set its own
      `cache_control`** — a caller-owned breakpoint sets owner `"caller"` and is explicitly not
      attributable (`token_efficiency_model/lossless/engine.py:393-398`), and the injection
      must not be disabled by `BREVITAS_ANTHROPIC_CACHE=0` (`:399-402`), by the
      `cache_injection` tenant lever (`:403-406`), or by cache ROI (`:408-422`).
      **Claude Code sets its own `cache_control`.** For OpenAI, only explicit-breakpoint mode is
      attributable (`:435`).
24. Fee = `max(0, verified) * BREVITAS_FEE_RATE` (`:4670`) — a pure function of `verified`, so
    zero verified ⇒ zero fee. Nothing downstream can repair it.

---

## 2. Where `priced` is set — answered explicitly

**`pricing_status` is written exactly once, synchronously, by the request that inserts the row.**

- Set at `api/server.py:4703` from `costs["pricing_status"]`, computed at `:4581-4587` by
  `calculate_costs` (`brevitas/receipts.py:364-405`).
- Default when a writer omits it: `'unpriced'` (`api/store.py:633`).
- **There is no worker, job, cron, trigger or RPC that re-prices a row.** `api/worker.py`
  mentions `pricing_status` only to price its *own* warm-ping row before inserting it
  (`api/worker.py:666-695`). No migration contains an `UPDATE … SET pricing_status`
  (grepped `supabase/migrations/*.sql`). Every other reference is a **read** predicate
  (`202607280013_period_settlement_writer.sql:306`,
  `202607280008_billing_halting_conditions.sql:362`, the `usage_stats` RPCs, etc.).
- **Pricing is keyed on nothing but the request body**: `receipt_available`, `provider`, `model`,
  and the receipt token categories.

### Consequence — this localizes the fault

`priced` **does not depend on `authoritative`**, on any worker, or on a provider receipt arriving
later. So `priced = 0` on 100% of rows — *including the non-authoritative ones* — cannot be
explained by fault A. It means that for **every** row inserted, at insert time, either
`receipt_available` was false or `model_price()` returned `None`. That is an independent bug in
the client→API payload, and it is separately fatal: with `pricing_status='unpriced'`,
`measured_savings_usd` is `None` (`:4584-4586`), so even if fault A were fixed tomorrow,
`verified = float(measured or 0) = 0.0` (`:4660`) and **every authoritative row would still bill
$0.**

Fix order therefore matters: **B before A.**

---

## 3. Observables that discriminate each failure

`usage_log` columns are self-describing enough to separate almost every branch above. All of
these are read-only projections; `ts` is the timestamp column (not `created_at`).

| Condition that failed | Fingerprint in `public.usage_log` |
|---|---|
| Never reached the hosted bridge (fault A) | `authoritative=false` **and** `receipt_source='proxy'` — a local `bvx` reporting over HTTP. The hosted bridge writes `receipt_source='proxy'` **and** `authoritative=true`; nothing else can. |
| Advisory `/v1/compress` telemetry, not a provider call at all | `receipt_source='sdk'`, `provider=''`, `model=''`, `strategy` like `lossy:…\|ctx:…`, `request_id=''`, `pricing_status='unpriced'` (`_safe_record_usage` never passes pricing at all, `api/server.py:3739-3751`, `1891-1912`) |
| Worker warm ping | `receipt_source='worker'`, `strategy='cache_warm'`, `request_id` like `warm:%` (`api/worker.py:676-680`) |
| Backfilled import | `receipt_source='import'`, `pricing_version='historical'` (`api/import_usage.py:62-63`) |
| **No provider receipt** (`receipt_available=false`) | `strategy` **ends in `:missing_receipt`** (`brevitas/proxy.py:988`) and `fresh_input_tokens = cached_input_tokens = cache_write_tokens = output_tokens = 0`, `baseline_cost_usd IS NULL` |
| **Unknown/unpriced model** | receipt token columns **non-zero** but `pricing_status='unpriced'`, `pricing_version=''`, `baseline_cost_usd IS NULL`. Inspect `provider`/`model` — expect resellers or `grok-4.3`/`grok-4.20-*` |
| Quality-affecting strategy blocked billing | `quality_status IN ('unverified','failed','stream_tripped')` with `pricing_status='priced'` |
| Zero delta / no attributable cache | `pricing_status='priced'`, `quality_status='verified'`, `authoritative=true`, but `measured_savings_usd <= 0` and `cache_attributable=false` |
| Caller-owned cache breakpoints | `cache_attributable=false` with `cached_input_tokens > 0` |
| Receipt dropped before insert | **not visible in the table** — only in Railway logs: `hosted proxy receipt dropped reason=missing_key\|invalid\|duplicate_request_id`, `hosted proxy receipt write failed error_type=…` (`api/server.py:5869,5874,5903,5908`), or `usage receipt emit failed error_type=…` (`brevitas/proxy.py:804`) |

**The single most informative query** (one row per shape; run read-only):

```sql
select receipt_source,
       authoritative,
       pricing_status,
       (strategy like '%:missing_receipt') as missing_receipt,
       provider, model,
       count(*)                                            as rows,
       min(ts) as first_ts, max(ts) as last_ts,
       sum((fresh_input_tokens + cached_input_tokens
            + cache_write_tokens + output_tokens) > 0)::int as with_receipt_tokens,
       sum(cache_attributable::int)                        as attributable,
       count(*) filter (where baseline_cost_usd is not null) as costed,
       count(distinct organization_id)                     as orgs
from public.usage_log
where ts >= '2026-07-27'
group by 1,2,3,4,5,6
order by rows desc
limit 50;
```

That one result set decides between every hypothesis in §5.

---

## 4. The traffic-gap signature (07-18 … 07-26 empty, resumes 07-27)

**This is fault C, and it is already explained and already fixed.**

`scripts/db/wyfz_usage_log_remediation.sql` (committed `2fadc43`, **2026-07-27 01:02 -0700**,
"docs(db): add verified wyfz usage_log remediation from live 400 diagnosis") states it verbatim:

> The production API (Railway) writes usage rows to project `wyfzmfnswtzyhwbltbpy`, but that
> project's `public.usage_log` is frozen at roughly the pre-enterprise-tenancy schema. It is
> **MISSING 11 columns** the current code (`_usage_row` in `api/store.py`) writes, so **every
> POST /v1/usage fails with 400 Bad Request** … i.e. usage tracking is fully broken in
> production right now.

`api/store.py:4256-4267` posts the **whole** `_usage_row` dict to PostgREST in one call, so a
single unknown column rejects the entire insert — all-or-nothing, which is exactly the observed
all-or-nothing gap. The 11 columns are the enterprise-tenancy trio
(`organization_id`, `customer_id`, **`authoritative`**), the cache trio
(`cache_write_5m_tokens`, `cache_write_1h_tokens`, `cache_attributable`) and the five
receipt-accounting columns. The corroborating context is `docs/PROD_DB_RECONCILIATION.md:1-5`
("the API on Railway keeps failing its healthcheck and billing cannot be turned on") plus the
07-28 dual-stack healthcheck fix (`b9fb98a`).

So the gap boundaries are **schema, not traffic**: writes were 400ing until the columns were
added on 07-27, then resumed. Note the consequence — `authoritative` was *added* by that script
with `not null default false`, so on wyfz **`authoritative=false` before 07-27 carries no
information at all**; only 07-27-onward values are signal. (`no-billable-usage-since-jul-17`
memory says the same about the 07-17 migration.)

**And the 07-17 boundary of the savings drought is a separate, precisely datable code change.**
The last row with non-zero `verified_savings_usd` is 2026-07-17 03:19:25 UTC. Commit
`3cd4cca` *"fix(accounting): align savings to provider receipts"* is dated
**2026-07-16 20:19:13 -0700 = 2026-07-17 03:19:13 UTC** — the same minute. Its diff to
`api/server.py` replaced the old rule

```python
verified = max(0.0, float(measured or 0)) if quality_status == "verified" else 0.0
# where quality_status = "verified" iff body.quality_score >= BREVITAS_QUALITY_FLOOR (0.8)
```

with strategy classification (`_verification_mode`) plus the `receipt_available` short-circuit
that hardcodes `pricing_status="unpriced"`. Before that commit **any** report with a decent
`quality_score` produced verified savings; after it, a report must be classified
byte-preserving *and* be priced *and* (from `088e7e4`, 2026-07-20) be `authoritative`. Nothing
about the traffic changed at 03:19 — the *rule* changed, mid-flight, and no client was ever
updated to satisfy the new rule.

**Best single explanation for both halves of the signature:** the gap is the wyfz `usage_log`
400 (schema), and the all-non-authoritative resumption is the pre-existing, by-design fact that
the only client integration anyone actually runs is the **local** `bvx` proxy — which reports
over HTTP and is forced non-authoritative — combined with the 07-17 rule change that stopped
paying non-authoritative, unpriced reports. The 07-27 resumption volume (5.5k → 11.4k → 12.4k
rows/day) coincides with the caching-pivot multi-agent build window, i.e. it is highly likely to
be **self-generated dev/benchmark/probe traffic**, not customers — `count(distinct
organization_id)` and the `source`/`client` labels in the query above settle that in one shot.

### Config surfaces checked (none of these can be the routing fault)

- `Dockerfile` / `api/serve.py` — one process, proxy + management API together; bridge always installed.
- `railway.json`, `railway.toml` — `numReplicas 2`, healthcheck `/v1/health/ready`. Nothing usage-related. `deploy/railway.json` is the separate *compress* service (`services/compress/Dockerfile`, healthcheck `/ready`) and never writes usage.
- `deploy/railway-worker.json`, `deploy/cloud-run-*-staging.yaml`, `docs/CLOUD_RUN_STAGING.md` — staging/worker only; the worker's only usage write is the warm ping (`receipt_source='worker'`, non-authoritative).
- `next.config.ts` — `/v1/:path*` → `BREVITAS_API_URL`, hard-failing if unset in production. `vercel.json` has no rewrites.
- `brevitas/config.py:7,44` — SDK default `https://api.brevitassystems.com`, overridable by `BREVITAS_BASE_URL`.
- `BREVITAS_PROXY_AUTH` (`api/server.py:939-946`) must be true in production or the proxy 503s wholesale — an all-or-nothing switch, so it cannot produce "rows but non-authoritative".

---

## 5. Ranked hypotheses, each with one read-only check and its fix

Ranking is by (probability × explanatory coverage). H1 and H2 are near-certain and
**both** must be fixed; the rest are alternates for the residue.

---

### H1 — Nobody uses the hosted proxy: 100% of receipts arrive over HTTP `POST /v1/usage` from local `bvx` proxies, which is hardcoded non-authoritative. *(explains `authoritative=0`)*

Evidence: the only documented zero-code integration is the **local** proxy
(`README.md:35-47`, `brevitas/cli.py:61-62,431-432`, `brevitas/__init__.py:35-36`,
`docs/TECHNICAL_AUDIT.md:17`), whose `_usage_reporter` is `None` ⇒ HTTP transport
(`brevitas/proxy.py:776-797`) ⇒ `authoritative=False` (`api/server.py:4778-4783`). The hosted
form exists but is buried at `README.md:155-173`. The
`no-billable-usage-since-jul-17` memory already observed `receipt_source='proxy'` +
`authoritative=false` on recent rows, which is precisely this shape.

**Check (read-only SQL):**
```sql
select receipt_source, authoritative, count(*) as rows,
       count(distinct organization_id) as orgs,
       count(distinct client) as clients, max(ts) as last_ts
from public.usage_log
where ts >= '2026-07-27'
group by 1,2 order by rows desc;
```
*Confirms if:* every row is `('proxy', false)` (plus maybe `('sdk', false)`), and **zero**
`('proxy', true)`. *Kills if:* any `authoritative=true` row exists after 07-27.

**Fix:** make the hosted proxy the default paid path — the local proxy can never mint billable
savings and must not be sold as if it could. Concretely: (a) lead the docs with
`ANTHROPIC_BASE_URL=https://brevitassystems.com` + the required headers; (b) have
`bvx start` warn loudly that local mode is measurement-only and not billed; (c) if local-mode
billing is ever wanted, it needs a *signed* receipt scheme, not a flag flip — do **not** touch
the forced `authoritative=False` at `api/server.py:4778-4783`.

> Line-number note: the task brief cited `api/server.py:4090-4098` / `:4959-4972` / `:5040-5041`
> for the anti-forgery and bridge code. Those offsets are stale against this dirty working tree —
> `4090` is now inside `CompressRequest`. In **this** tree the numbers are: forced
> `authoritative=False` at `:4778-4783`, `_hosted_proxy_receipt` at `:5857-5908`,
> `set_usage_reporter` at `:5986`. All citations in this document are against this tree.

---

### H2 — Every receipt is `receipt_available=false` (no provider token receipt), so pricing short-circuits to `unpriced` and `measured_savings_usd` is `NULL`. *(explains `priced=0` on 100%, incl. non-authoritative rows)*

Evidence: `pricing_status` is decided at insert and gated first on `receipt_available`
(`api/server.py:4581-4587`); the wire value is `has_receipt = receipt.total_tokens > 0`
(`brevitas/proxy.py:952,982`). Streamed OpenAI/xAI responses carried **no usage object at all**
until `stream_options.include_usage` injection shipped 2026-07-28 (`brevitas/proxy.py:95-105`,
commit `c103d87`) — and a customer's *local* `bvx` only gets that after upgrading their pip
package (`brevitas/__init__.py:__version__ = "0.9.11"`). Streaming is the default for Claude
Code / Codex / most agent frameworks.

**Check (read-only SQL):**
```sql
select (strategy like '%:missing_receipt')                    as missing_receipt_label,
       (fresh_input_tokens + cached_input_tokens
        + cache_write_tokens + output_tokens) > 0              as has_receipt_tokens,
       is_stream, pricing_status, provider,
       count(*) as rows, max(ts) as last_ts
from public.usage_log
where ts >= '2026-07-27'
group by 1,2,3,4,5 order by rows desc limit 40;
```
*Confirms if:* `missing_receipt_label = true` and/or `has_receipt_tokens = false` dominates
(expect it to correlate with `is_stream = true`). *Kills if:* receipt token columns are
non-zero while `pricing_status='unpriced'` → that is **H3**, not H2.

**Fix:** ensure a receipt exists on every metered call. (a) Confirm
`BREVITAS_STREAM_USAGE_INJECT` is **not** disabled on the Railway API; (b) ship + require an SDK
version ≥ the `c103d87` build for local proxies; (c) for Anthropic streams verify
`SSEUsageParser` really lands `message_delta` usage (it is fed per chunk at
`brevitas/proxy.py:1253` and only harvested when `completed`, `:1259-1265` — a truncated stream
silently drops the whole receipt); (d) add an alert on the ratio of `:missing_receipt` rows,
because today it is invisible.

---

### H3 — Receipts exist but the model is unpriced (`model_price()` → `None`), e.g. a reseller provider or a model absent from `MODEL_PRICES`. *(alternate explanation for `priced=0`)*

Evidence: `calculate_costs` returns `unpriced` with all legs `None` when `model_price` misses
(`brevitas/receipts.py:364-372`); resolution is exact/dated-prefix only (`:346-361`); reseller
providers have **no rows at all** (`:322-324`) — `tests/test_cloud_usage_api.py:1151` asserts
exactly that; and the live xAI models per the caching-pivot memory are `grok-4.3` /
`grok-4.20-*`, neither of which is in the table (`:309-310` has only `grok-4.5`,
`grok-4.1-fast`).

**Check (read-only SQL):**
```sql
select provider, model, pricing_status, pricing_version,
       count(*) as rows,
       count(*) filter (where baseline_cost_usd is not null) as costed
from public.usage_log
where ts >= '2026-07-27'
  and (fresh_input_tokens + cached_input_tokens
       + cache_write_tokens + output_tokens) > 0
group by 1,2,3,4 order by rows desc limit 40;
```
*Confirms if:* rows with real receipt tokens are `unpriced` and their `(provider, model)` is not
in `MODEL_PRICES` (`brevitas/receipts.py:281-312`). *Kills if:* that projection returns no rows
(⇒ H2).

**Fix:** add the missing `(provider, model)` rows to `MODEL_PRICES` — with real published rates,
never guessed — and add a startup/observability counter for `unpriced` inserts keyed by
`(provider, model)` so a new model launch degrades loudly instead of silently unbilling.
For genuine resellers, decide explicitly whether they are priceable at all.

---

### H4 — Traffic is priced and authoritative-eligible but `measured_savings_usd <= 0` because nothing is `cache_attributable` (caller-owned `cache_control`, or the injection/lever is off).

Evidence: passthrough sends delta 0 (`brevitas/proxy.py:979`); with delta 0 the only positive
term is the native-cache discount, which requires `cache_attributable`
(`brevitas/receipts.py:390-397`, `api/server.py:4592-4609`); attribution requires
`cache_control_owner == "brevitas"` (`brevitas/proxy.py:1213`) which is skipped when the caller
already set `cache_control` (`token_efficiency_model/lossless/engine.py:393-398`) — **Claude Code
does** — or when `BREVITAS_ANTHROPIC_CACHE=0` (`:399-402`) or the `cache_injection` lever is
denied (`:403-406`). This is the fault that will bite *after* H1+H2 are fixed.

**Check (read-only SQL):**
```sql
select cache_attributable, quality_status, strategy,
       count(*) as rows,
       sum(cached_input_tokens) as cached_tok,
       sum(coalesce(measured_savings_usd,0)) as measured,
       sum(verified_savings_usd) as verified
from public.usage_log
where ts >= '2026-07-27' and pricing_status = 'priced'
group by 1,2,3 order by rows desc limit 30;
```
*Confirms if:* priced rows exist with `cached_input_tokens > 0` but `cache_attributable=false`
and `measured <= 0`. *Config half:* confirm `BREVITAS_ANTHROPIC_CACHE` is unset-or-1 on the
Railway API (**name only**; do not print values) and that no tenant has the `cache_injection`
lever denied.

**Fix:** product decision, already framed by the caching pivot — either (a) accept
caller-owned cache as unbillable and market the measured (unbilled) savings, or (b) define a
Brevitas-attributable contribution on top of caller markers (e.g. Brevitas-added breakpoints /
TTL upgrade) and price only that delta. Do not widen `cache_attributable` to caller-owned
markers: that would bill customers for their own caching.

---

### H5 — Org-service keys 400 on every hosted-proxy call for want of `X-Brevitas-Customer-ID`, so the bridge is unreachable for exactly the enterprise cohort.

Evidence: `api/server.py:1755-1758` returns 400 when `key_type == "organization_service"` and no
customer id; the documented Claude Code / Codex header sets omit it (`README.md:160-173`).

**Check (config + read-only):** for the org(s) that should be billing, read `api_keys.key_type`
on wyfz (read-only `select key_type, count(*) from public.api_keys group by 1`), then check the
Railway API logs for 400s with detail `Organization service proxy calls require
X-Brevitas-Customer-ID` on `/v1/messages` since 07-27. *Confirms if:* such 400s exist, or
enterprise keys are `organization_service` while the docs omit the header.

**Fix:** documentation + a friendlier default — add `X-Brevitas-Customer-ID` to every hosted
snippet in `README.md`/`integration.md`, and consider deriving a default customer for
single-tenant orgs rather than hard-400ing (the 400 is correct for multi-tenant attribution, so
this is a docs fix first).

---

### H6 — Receipts are being written but silently dropped before insert (invalid key, duplicate `request_id`, or a store exception).

Evidence: four log-only exits, none of which leave a row:
`api/server.py:5869` (`missing_key`), `:5874` (validity), `:5903` (write failed), `:5908`
(`duplicate_request_id`); plus `brevitas/proxy.py:804`. `docs/SECURITY_AUDIT_2026-07-30.md:207`
describes the constant-`request_id` suppression class; the current tree mints ids server-side
(`brevitas/proxy.py:739-770`) and reserves the `proxy:` namespace on the intake path
(`api/server.py:4767-4777`), so this only bites if **prod runs a build older than that
hardening**.

**Check (no SQL):** `curl -s https://api.brevitassystems.com/v1/version` (public, non-secret,
`api/server.py:5851-5854`) and compare the returned build sha against the commit that added the
`request_id` minting; then grep the Railway API logs for `hosted proxy receipt dropped` /
`usage receipt emit failed`. *Confirms if:* those log lines appear, or the deployed sha predates
the hardening.

**Fix:** redeploy to a build containing the server-side `request_id` minting, and promote those
four `logger.warning/error` calls to counters with an alert — a dropped receipt is lost revenue
and is currently detectable only by reading logs.

---

## 6. Corrections to existing docs found while doing this

1. **`BILLING_CORRECTNESS_PLAN.md:36-38` is wrong** where it says *"Authoritative rows come from
   the worker (`BREVITAS_WORKER_BILLING_ROLE=authoritative`)"*. That env var selects the
   worker's **billing-sync** role and is validated at `api/worker.py:94-101`; it has nothing to
   do with `usage_log.authoritative`. The worker's only usage write is the warm ping, which goes
   through `_safe_record_usage` and never passes `authoritative`
   (`api/worker.py:674-696`) ⇒ `false` (`api/store.py:593`). The **only** authoritative writer in
   non-test code is `api/server.py:5900`. The doc's own §"one correction" is otherwise right, and
   its open question — *"whether the worker path is producing authoritative rows at all in
   production"* — is answerable from the code alone: **it cannot, ever.**
2. `BILLING_CORRECTNESS_PLAN.md:57` ("Whether the API actually does that re-anchoring is
   unconfirmed and is the crux of Phase 2") is now confirmed: it does, at
   `api/server.py:4576` via `_anchor_token_legs` (`:4449-4523`) — and the docstring at
   `:4470-4476` states plainly that anchoring **cannot** recover savings from a zero delta.
   Phase 2 should be re-scoped around `cache_attributable` (H4), not anchoring.
3. `docs/DATA_MIGRATION_amjcc_to_wyfz.md`'s "sequencing hazard" preamble still describes the
   pre-07-27 project split as current; the `supabase-project-split` memory records it as
   resolved. It is not the cause of the traffic gap — the `usage_log` 400 is.
