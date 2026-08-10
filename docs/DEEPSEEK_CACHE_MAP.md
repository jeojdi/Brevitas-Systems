# DeepSeek Prompt-Cache Behavior Map (Empirical)

**Proprietary measurement. Do not distribute.**

What DeepSeek's *automatic* prompt cache actually does, measured against the live
OpenAI-compatible `/v1/chat/completions` API on Brevitas's own funded probe key,
with the provider's own usage receipt (`prompt_cache_hit_tokens` vs
`prompt_cache_miss_tokens`) as the evidence for every claim.

DeepSeek is the highest-value target precisely because it publishes almost **no**
cache documentation. The [KV-cache guide](https://api-docs.deepseek.com/guides/kv_cache)
says caching is automatic ("enabled by default for all users"), gives **no**
minimum cacheable prefix, and states the lifetime only as prose — *"Once the cache
is no longer in use, it will be automatically cleared, usually within a few hours
to a few days."* No TTL number, no floor, no scope definition. Every number below
therefore exists nowhere in their docs — it is proprietary measurement.

- **Harness:** `scripts/deepseek_cache_probe.py` (re-runnable; guarded by
  `BREVITAS_DEEPSEEK_PROBE=1`, hard **$2.00** list-price spend cap, probe key only).
- **Model:** `deepseek-chat` (the row in `brevitas/receipts.py MODEL_PRICES`;
  rates match the current V4-Flash tier).
- **Run:** 2026-08-10, seed `20260810`. Prefixes are synthetic (seeded word
  list), never customer data. `max_tokens` 6; every call byte-identical and
  well-formed.
- **Total live spend: $0.002037 of the $2.00 cap.** Receipts dumped to
  `deepseek_cache_probe_receipts.json` (scratchpad).
- **Scope guardrail honored:** behavioral characterization only. No cross-tenant
  access, no rate-limit probing, no exploitation of any cross-user cache-sharing
  side channel, no malformed traffic. The scoping probe (P6) tests **same-key
  reuse only** — cross-key isolation was deliberately **not** probed (single probe
  key; cross-tenant probing is out of scope by guardrail, not merely skipped).

Pricing basis ($/Mtok, from `brevitas/receipts.py MODEL_PRICES`): `input 0.14 ·
cache-hit 0.0028 (0.02×) · output 0.28`. DeepSeek caching is **automatic and
write-free** — there is no cache-write rate; a miss is billed at the full input
rate, a hit at the 0.02× rate. The **50× per-token swing** between a miss and a
hit is the entire economic surface here.

---

## Block size RESOLVED: 128 tokens (proven 2026-08-10, +$0.0004)

The initial sweep left 64-vs-128 ambiguous — every observed hit (128, 256, 512, 896, 1792, 1920) is a multiple of *both* 64 and 128, so it proved nothing. A targeted follow-up settles it: a **201-token prompt cached exactly 128, leaving 73 uncached** (`hit=128, miss=73`). If the block were 64 tokens, 201 would cache 192 (3×64) and strand only 9; instead the cacheable prefix rounds **down to the nearest 128**, stranding 73 (and 73 > 64, so a 64-block would have fit but did not form). **Block size = 128 tokens. Floor = 128 (one block).** The widely-quoted "64-token unit" figure is wrong for the live API. (This is the measurement discipline in miniature: the first sweep looked conclusive but wasn't, and one two-cent probe designed to *distinguish* the hypotheses settled it.)

## Headline results

| Probe | Measured | DeepSeek docs | Verdict |
|---|---|---|---|
| P1 automatic caching | resend HIT `hit=1792` with **no directive** | "enabled by default" | **CONFIRMED** — fully automatic |
| P2 min cacheable prefix | 98-tok prompt **never caches**; first hit at prompt **163 tok** (hit=128) | *silent — no floor documented* | **NEW DATUM** — floor ≈128 tok, **not** 64 |
| P2 cache granularity | every hit is an exact **multiple of 64** (128/256/512/896/1792/1920) | "cache prefix units" (no number) | **NEW DATUM** — 64-token blocks, tail uncached |
| P3 TTL (bounded) | HIT at 60 / 300 / **900 s** — alive through 15 min | "a few hours to a few days" | **CONSISTENT** — TTL ≫ 15 min |
| P4 refresh-on-read | moot: entry alive at 300 s untouched | (not documented) | **N/A** — long TTL makes refresh irrelevant |
| P5 peak-hour pricing | Beijing 16:54 (in rumored peak); receipt carries **no price field** | 2× peak announced, effective **TBA** | **NOT LIVE / INVISIBLE in receipt** |
| P6 scoping (same-key) | second identical request HITs | scope undocumented | **CONFIRMED account-scoped** (cross-key not tested) |

**The through-line vs Anthropic:** Anthropic's default cache dies at a hard
**300–330 s** cliff (see `docs/ANTHROPIC_CACHE_MAP.md`); DeepSeek's is still warm
at **900 s** and documented to live *hours to days*. That single fact inverts the
product: on Anthropic, warming (a sub-300 s keep-alive ping) is the lever; on
DeepSeek, an idle prefix stays warm for free, so **there is nothing to warm** —
the lever is **measurement**, not warming.

---

## P1 — Is it automatic? (fast probe)

**Method.** Write a 1,500-word synthetic prefix (a `system` message), then
immediately resend the byte-identical request. No cache directive of any kind is
sent (DeepSeek is OpenAI-compatible; there is no `cache_control` to send).

| Leg | Receipt | Outcome |
|---|---|---|
| write | `prompt_tokens=1868, prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=1868` | MISS (cold) |
| resend | `prompt_tokens=1868, prompt_cache_hit_tokens=1792, prompt_cache_miss_tokens=76` | **HIT** |

**Docs comparison: CONFIRMED.** Caching is fully automatic — an immediate resend
converts `1792 / 1868` tokens (95.9%) to the 0.02× hit rate with zero directives.
The cost fell from **$0.000262 → $0.000016** (a **16×** drop) on the resend.

Note the `hit=1792` is exactly `28 × 64`; the trailing `76` tokens (the tail past
the last full 64-block plus the fixed user suffix) bill as a miss. This is the
64-block quantization measured directly in P2.

**Brevitas lever.** DeepSeek gives the cache discount away automatically on any
repeated prefix. So Brevitas cannot bill "we enabled caching" here — the customer
already has it. The only honest, billable delta is the **incremental** input we
save *over* the customer's already-cached baseline (exactly what
`benchmarks/native_cache_baseline_results_deepseek_n36.json` measures, and why its
incremental savings sit near zero). Any DeepSeek settlement row must be computed
against the native-cache baseline, not a cache-busted one.

---

## P2 — Minimum cacheable prefix + granularity (fast probe)

The load-bearing fast probe. DeepSeek claims **64-token storage units** but
documents **no floor** at which caching begins. We sweep target sizes, writing
each fresh prefix once and immediately resending it; `prompt_cache_hit_tokens > 0`
on the resend means that size caches.

| Target tok | Measured `prompt_tokens` | `hit` on resend | Cached? |
|---:|---:|---:|:--|
| 64  | **98**  | **0**   | **no** |
| 128 | **163** | **128** | **yes** |
| 256 | 287  | 256  | yes |
| 512 | 540  | 512  | yes |
| 1024 | 1015 | 896  | yes |
| 2048 | 2024 | 1920 | yes |

**Receipt evidence (the boundary pair):**
- 98-tok prompt → `{"prompt_cache_hit_tokens":0,"prompt_cache_miss_tokens":98}` — no error, silently uncached.
- 163-tok prompt → `{"prompt_cache_hit_tokens":128,"prompt_cache_miss_tokens":35}` — first hit, exactly 2×64.

**Two proprietary findings:**

1. **Caching does NOT kick in at 64 tokens.** A 98-token prompt caches *nothing*.
   The floor sits in **(98, 163] prompt tokens**, and the smallest hit ever
   observed is **128 tokens (2 × 64-block)**. So the effective minimum cacheable
   prefix is **≈128 tokens** — a *higher* floor than the single 64-token storage
   unit implies, but **~8× lower than OpenAI's documented 1024-token floor** and
   ~32× lower than Anthropic Haiku's measured 4096 (`ANTHROPIC_CACHE_MAP.md`).
2. **Hits are quantized to 64-token blocks, tail uncached.** Every hit is an exact
   multiple of 64 (128, 256, 512, `896=14×64`, `1792=28×64`, `1920=30×64`). The
   remainder above the last 64-boundary (e.g. `1015−896=119`, `2024−1920=104`)
   always bills as a miss. DeepSeek rounds the cacheable prefix **down** to the
   nearest 64.

**Docs comparison: NEW DATUM.** DeepSeek documents neither the floor nor the
rounding direction. Both are measured here for the first time.

**Brevitas lever.** The candidate filter for DeepSeek routes must gate on a
**~128-token minimum** — but far more importantly, model on the **64-block
floor of the hit**, not the raw prefix length. A prefix of 1,015 tokens only ever
recovers 896 at the hit rate; the last ~119 are structurally un-discountable. A
savings estimate that credits the *full* prefix at the hit rate over-claims by the
64-block remainder on every call. The settlement path already reads the receipt,
so it books the real `prompt_cache_hit_tokens` — but any *pre-call projection*
must floor to 64 or it overstates.

---

## P3 — TTL cliff, bounded (slow probe, real wall-clock)

DeepSeek publishes **no TTL** — only "a few hours to a few days." A prior Brevitas
probe found the cache alive **>55 min**, so we do **not** hunt the far edge (it
could be days; we cannot wait). We **bound** it: three fresh prefixes written at
t=0, each read **exactly once** at a staggered gap so no read refreshes another.

| Gap (measured elapsed) | `hit` | `miss` | Outcome |
|---:|---:|---:|:--|
| 60 s (1 m)  | 1792 | 98 | **HIT (alive)** |
| 300 s (5 m) | 1792 | 60 | **HIT (alive)** |
| **900 s (15 m)** | **1792** | **47** | **HIT (alive)** |

**Receipt evidence:** `ttl900@read(gap=900s) → {"prompt_cache_hit_tokens":1792,"prompt_cache_miss_tokens":47}`.

**The cache is still fully warm at 15 minutes, untouched.** We did not find the
cliff — by design. The finding is a **lower bound**: *TTL > 15 min in our window*,
which is consistent with (a) the prior probe's >55 min and (b) the docs' "hours to
days." That gulf is itself the proprietary datum.

**Docs comparison: CONSISTENT.** The prose "hours to days" is not contradicted;
we place a hard floor of 15 min under it with live receipts.

**Brevitas lever — this is why DeepSeek's product is measurement, not warming.**
Anthropic's default entry is dead by 330 s (`ANTHROPIC_CACHE_MAP.md` P1), so a
sub-300 s keep-alive ping is the whole warming economy. DeepSeek's entry is warm
past 15 min *for free*, so:
- **A keep-alive ping is almost pure cost.** Any realistic conversational or
  batch cadence (sub-15-min) is organically warm; pinging it converts nothing and
  bills the customer for a ping. Warming DeepSeek is **structurally unprofitable**
  and the policy must **not** schedule DeepSeek pings on short gaps.
- **The lever is measuring the discount the customer already gets** and pricing
  the incremental input delta honestly (P1). On DeepSeek, Brevitas sells
  *proof and settlement*, not a warmed cache.

---

## P4 — Refresh-on-read (folded into P3)

**Method / finding.** Refresh-on-read only matters when the TTL is short enough
that a read must reset the clock to survive. DeepSeek's untouched entry was alive
at **300 s** (P3) with no intervening read, and still alive at **900 s**. With a
multi-hour-to-multi-day lifetime, whether a read *also* extends it is economically
moot — the entry outlives any warming cadence we would run regardless. **No extra
spend incurred.** (Contrast Anthropic, where refresh-on-read is *the* thesis
because the base TTL is 5 min — see `ANTHROPIC_CACHE_MAP.md` P2.)

---

## P5 — Peak-hour pricing (docs + clock + receipt)

**Method.** DeepSeek's receipt carries **only token counts** — there is no
per-call price/cost field in the OpenAI-compatible `usage` object — so peak vs
standard pricing **cannot be read off a receipt**. We record the current Beijing
clock, whether we sit inside the rumored peak windows, and confirm the receipt
exposes no price. (Minimal spend — reuses an existing receipt.)

| Signal | Value |
|---|---|
| UTC at run | 2026-08-10 08:54 UTC |
| Beijing at run | 2026-08-10 16:54 (Mon), hour = 16.90 |
| Inside rumored peak window (≈09–12 / 14–18 Beijing) | **True** (afternoon window) |
| Receipt exposes a per-call price field | **False** — `usage` has token counts only |

**Docs / market comparison.** DeepSeek's historical **off-peak discount** (50%
chat / 75% reasoner, **16:30–00:30 UTC**) was a *V3/R1* feature and retired with
those aliases. For V4, DeepSeek **announced a 2× peak-hour surcharge on
2026-06-30**, but its effective date is **TBA** and it was **not active** as of the
last public check (2026-07-31). We ran *inside* a rumored afternoon peak window
and observed nothing in the receipt that would distinguish peak from standard —
because the receipt never carries price.

**Brevitas lever.** Two consequences:
1. **Time-of-day pricing is invisible in the token receipt.** Brevitas cannot
   detect a peak/off-peak multiplier from `usage` alone; it must be applied from a
   **time-aware rate card** keyed on the call's UTC timestamp, not inferred. If/when
   the 2× surcharge goes live, `MODEL_PRICES` needs a *time-banded* DeepSeek row or
   every peak-hour DeepSeek bill is understated by 2×.
2. **Timing arbitrage is real once the surcharge lands.** Because the cache lives
   *hours to days* (P3), a prefix written cheaply off-peak stays warm into a peak
   window — so DeepSeek's long TTL and its time-of-day pricing **compound**: write
   cold off-peak, read warm during peak, and the expensive-window reads land at
   0.02× regardless. That is a genuine Brevitas scheduling lever, but it is a
   *when-to-write* lever, not a warming lever.

---

## P6 — Scoping (fast, same-key only)

**Method.** Two byte-identical requests on **our** probe key; does the second hit?

| Leg | Receipt | Outcome |
|---|---|---|
| req1 | `prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=1886` | MISS |
| req2 | `prompt_cache_hit_tokens=1792, prompt_cache_miss_tokens=94` | **HIT** |

**Finding: the cache is account/key-scoped** — a prior write on our key is visible
to a later read on the same key. **Cross-key isolation was NOT tested**: we hold a
single probe key, and probing whether one tenant's prefix leaks into another's is
exactly the cross-tenant cache-sharing side channel the guardrails forbid. That
test is **out of scope by policy**, not merely unimplemented.

**Docs comparison.** DeepSeek describes caching in terms of "a user's" requests
but never defines scope; same-key reuse is confirmed here, cross-key left
deliberately unmeasured.

**Brevitas lever.** Same-key reuse is all Brevitas needs: a customer's own
repeated prefixes on their own key hit the cache, which is the surface we measure
and settle. We make **no** claim about cross-key behavior and must never build a
lever that depends on cross-tenant cache state.

---

## What we measured vs. what remains

**Measured this run (all with live provider receipts):** automatic caching (P1),
the minimum cacheable prefix (≈128 tok, **not** 64) and 64-block hit quantization
(P2), a hard TTL floor of **15 min untouched** (P3), the receipt's silence on
time-of-day price (P5), and same-key account scoping (P6). Total **$0.002037**.

**Not measured (harness left runnable):**
- **The TTL far edge.** Bounded to *> 15 min*; the real expiry ("hours to days")
  is unmeasured by design — a single read at, say, +2 h / +12 h / +24 h would
  bracket it (trivial spend, long waits). Worth one scheduled long-gap read to
  turn "hours to days" into a number.
- **Exact floor to ±1 block.** The floor is in (98, 163] prompt tokens with the
  first hit at 128; a finer sweep (100/112/128/144 tok) would pin whether the
  trigger is "prompt ≥ 128" or "prompt long enough to contain 2 full 64-blocks."
- **Peak surcharge activation.** Not live as of this run; a re-run once DeepSeek
  ships the 2× window would confirm it still never surfaces in the receipt (and
  force the time-banded rate-card row).
- **`deepseek-v4-flash` / `deepseek-v4-pro` parity.** Only `deepseek-chat` is
  measured; the V4 aliases (same/greater floor?) are unverified on live infra.

None of the gaps change the shipped levers: on DeepSeek the cache is **automatic,
long-lived, and free to keep warm**, so the product is **honest measurement of the
customer's already-native cache discount** — not warming, which is structurally
unprofitable here.

---

*Harness: `scripts/deepseek_cache_probe.py`. Reproduce:
`BREVITAS_DEEPSEEK_PROBE=1 .venv/bin/python scripts/deepseek_cache_probe.py`.
Guarded by a $2 list-price cap; synthetic prefixes only; probe key only. Not
committed by this run.*
