# Anthropic Prompt-Cache Behavior Map (Empirical)

**Proprietary measurement. Do not distribute.**

What the Anthropic prompt cache *actually* does, measured against the live
`/v1/messages` API on Brevitas's own funded probe key, with the provider's own
usage receipt (`cache_read_input_tokens` vs `cache_creation_input_tokens`) as the
evidence for every claim. This is the founding-thesis measurement: the cache is
documented only in prose, its timing is opaque, and the economics of warming rest
on where exactly the TTL cliff falls and whether a read refreshes the clock.

- **Harness:** `scripts/anthropic_cache_probe.py` (re-runnable; guarded by
  `BREVITAS_ANTHROPIC_PROBE=1`, hard $5.00 list-price spend cap, probe key only).
- **Model:** `claude-haiku-4-5` (cheapest priced Anthropic model in `MODEL_PRICES`).
- **Run:** 2026-08-09/10, seed `20260810`. Prefixes are synthetic (seeded word
  list), never customer data. `max_tokens` 4–8; every call byte-identical and
  well-formed.
- **Total live spend: $0.2336 of the $5.00 cap.** Receipts dumped to
  `receipts.json` (scratchpad).
- **Scope guardrail honored:** behavioral characterization only. No cross-tenant
  access, no rate-limit probing, no exploitation of the cross-user cache-sharing
  side channel, no malformed traffic. Every result below comes from our own
  key writing and reading our own synthetic prefixes.

Pricing basis ($/Mtok, from `brevitas/receipts.py MODEL_PRICES`, marginally above
current public haiku list so the cap over-books rather than under-books):
`input 1.00 · cache-read 0.10 (0.10×) · cache-write-5m 1.25 (1.25×) ·
cache-write-1h 2.00 (2.00×) · output 5.00`.

---

## Headline results

| Probe | Measured | Anthropic docs | Verdict |
|---|---|---|---|
| P3 min cacheable prefix | `claude-haiku-4-5`: floor between **4094 and 4351 tok** (≈4096) | 4096 tok for Haiku 4.5 | **MATCH** (this model only — the floor is per-model) |
| P4 max `cache_control` breakpoints | 4 OK, **5 → hard HTTP 400** | max 4 | **MATCH** (fails closed) |
| P1 raw TTL cliff (single read, uncontaminated) | HIT ≤300s, **MISS ≥330s** → TTL ∈ (300, 330] s | "at least 5 min", refreshed on use | **MATCH**, cliff just past 5:00 |
| P1 refresh chain (read every ≤90s) | warm through **450s** (7.5 min) | reads refresh TTL | **MATCH** — indefinite with touches |
| P2 refresh-on-read across 8 min | read@4m ⇒ **HIT@8m**; no read ⇒ **MISS@8m** | implied, never quantified | **CONFIRMED** — the warming thesis |
| P5 1h tier | write `ttl:"1h"` ⇒ **HIT@6m** while 5m tier is dead | 1h extended TTL (beta flag) | **MATCH** — tier live, outlives 5m clock |

---

## P3 — Minimum cacheable prefix (fast probe)

**Method.** Ten fresh prefixes sized through the documented floor, each written
once. `cache_creation_input_tokens > 0` iff the prefix was eligible; the smallest
eligible size is the empirical floor. Sizes labeled by exact `count_tokens`.

| Measured prefix tokens | `cache_creation_input_tokens` | Cached? |
|---:|---:|:--|
| 807 / 1038 / 1540 / 2091 / 3099 / 3817 | 0 | no |
| **4094** | **0** | **no** |
| **4351** | **4338** | **yes** |
| 4808 / 5612 | 4795 / 5599 | yes |

**Receipt evidence (the boundary pair):**
- 4094 tok → `{"input_tokens":4094,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}` — no error, silently uncached.
- 4351 tok → `{"input_tokens":13,"cache_creation_input_tokens":4338,"cache_creation":{"ephemeral_5m_input_tokens":4338}}`.

**Docs comparison: MATCH.** Anthropic documents a **4096-token** minimum for
Haiku 4.5. The floor sits in (4094, 4351]; a 4094-token prefix caches nothing.
The generic "1024" figure people quote from the Sonnet docs is **wrong for
Haiku 4.5 by 4×**.

### The floor is per-model, not global

There is **no single Anthropic floor.** Only the `claude-haiku-4-5` row below is
measured here; every other row is the value the shipped code actually gates on,
read from `_ANTHROPIC_MIN` in
`token_efficiency_model/lossless/provider_cache.py` (docs-sourced there, verified
2026-07-01, **unverified on live infra by us**). Sourcing is stated per row —
this file's discipline is measured vs. remains, and a docs number is not a
measurement.

| Floor (tok) | Models | Source |
|---:|---|---|
| **4096** | **`claude-haiku-4-5`** | **measured here** — (4094, 4351], receipts above |
| 4096 | `claude-opus-4-6`, `claude-opus-4-5` | `provider_cache.py` `_ANTHROPIC_MIN` (docs) |
| 2048 | `claude-opus-4-7`, `claude-haiku-3-5` / `claude-3-5-haiku`, `claude-mythos-preview`, and — via the generic `claude-haiku` catch-all row — any other Haiku not matched above | `provider_cache.py` `_ANTHROPIC_MIN` (docs) |
| 1024 | everything unlisted — the Sonnet family, `claude-opus-4-8`, `claude-opus-4-1` (never a Haiku: the catch-all keeps unlisted Haikus at 2048) | `provider_cache.py` default |
| 512 | `claude-fable`, `claude-mythos`, `claude-opus-5` | `provider_cache.py` `_ANTHROPIC_MIN` (docs) |

Two consequences worth stating plainly, both visible in the table:

- **The floor is not monotonic across generations, and not uniform within a
  family.** Haiku 4.5 (4096) sits 2× above Haiku 3.5 (2048); Opus spans 512
  (Opus 5) to 4096 (Opus 4.5/4.6). A floor inferred from the family name is a
  coin flip. Only a per-model lookup is safe.
- **Haiku 4.5 is the worst case we know of and the model we measured** — so the
  4096 figure quoted elsewhere in this file is a *ceiling on the floors*, not a
  constant.

**Brevitas lever.** Any prefix under its model's floor is **structurally
unwarmable** — a keep-alive ping writes nothing and bills the customer for a ping
that can never convert. The candidate filter must gate on the *per-model* floor
(the table above), not a global constant, or we overclaim on short-prefix traffic
for every model whose floor exceeds the constant. The failure mode is silent
(HTTP 200, `cache_creation=0`), so it cannot be caught by status codes — only by
reading the receipt, which is exactly what the settlement path must do.

---

## P4 — Breakpoint count (fast probe)

**Method.** Split one large prefix into N `cache_control` system blocks; N ∈ {1,4,5}.

| Breakpoints | Result |
|---:|:--|
| 1 | 200, `cache_creation_input_tokens: 7682` |
| 4 | 200, `cache_creation_input_tokens: 7755` |
| **5** | **HTTP 400** — `"A maximum of 4 blocks with cache_control may be provided. Found 5."` |

**Docs comparison: MATCH, and it fails closed.** The 4-breakpoint maximum is a
**hard request-validation error**, not a silent cap that drops the 5th block. A
malformed multi-breakpoint request is rejected wholesale (no tokens billed on the
400), so we can never silently lose a breakpoint and mis-price.

**Brevitas lever.** Multi-segment prefixes (system + tools + long context) can
carry **up to 4** independently-refreshable cache segments. A warming ping that
touches the *outermost* breakpoint refreshes everything before it, so one ping
covers the whole ≤4-segment stack — we never need to fan pings per segment. If an
upstream request already carries 4 breakpoints, our proxy must **not** inject a
5th for warming instrumentation — that would 400 the customer's real request.

---

## P1 — TTL cliff (slow probe, real wall-clock)

The load-bearing measurement. Two independent constructions on one ~8-minute
timeline, all prefixes written at t=0.

### Construction A — single-read fan (uncontaminated raw TTL)
Each gap point is its **own** fresh prefix, read **exactly once** at that gap, so
no read refreshes another. This isolates the raw TTL.

| Gap (measured elapsed) | `cache_read` | `cache_creation` | Outcome |
|---:|---:|---:|:--|
| 270 s (4.5 m) | 7721 | 0 | **HIT** |
| 300 s (5.0 m) | 7756 | 0 | **HIT** |
| **330 s (5.5 m)** | **0** | **7747** | **MISS** |
| 360 s (6.0 m) | 0 | 7647 | MISS |
| 390 / 420 / 450 s | 0 | 7698 / 7650 / 7774 | MISS |

**The cliff is between 300 s and 330 s.** Receipts:
`fan300 → cache_read_input_tokens:7756` (warm at exactly 5:00);
`fan330 → cache_creation_input_tokens:7747` (cold, fully re-written at 5:30).

### Construction B — refresh chain (touched every ≤90 s)
One prefix, read at 60/150/240/300/360/450 s. **Every read HIT**
(`cache_read_input_tokens:7726` at each), including **450 s (7.5 min)** — 2.5×
past a single TTL. A prefix touched inside each window stays warm indefinitely.

**Docs comparison: MATCH.** Anthropic states the ephemeral TTL is a **5-minute
minimum, refreshed on each use**. Measured: an untouched entry survives to 300 s
and is gone by 330 s — the "at least 5 minutes" is really *5:00 plus a small
grace*, not materially longer. A touched entry never expires. No drift.

**Brevitas lever.** This is the physics the whole warming product prices against,
now measured rather than assumed:
- **Effective keep-alive interval ≤ ~300 s.** With the cliff at (300, 330] s, a
  safety margin below 300 s (the shipped `safety_margin_seconds: 60` → 240 s
  cadence) is correct and not over-conservative. A 270-s ping cadence would also
  hold; a 330-s cadence would drop entries.
- **Any customer gap > ~330 s is real money on the table** (a cold re-write at
  **1.25×** vs a warm read at **0.10×** — a **12.5× per-token swing** on the
  prefix). Gaps ≤300 s are organically warm and **not ours to bill**.

---

## P2 — Refresh-on-read (slow probe) — the warming thesis, proven

**Method.** Two fresh prefixes, both written at t=0.
- **R1 (warmed):** read at **240 s**, then again at **480 s**.
- **R2 (control):** read **only** at **480 s**.
Both returns land at 8:00 — two full TTLs past the write. The only difference is
R1's single intervening read.

| Prefix | Read @240 s | Read @480 s (8 min) |
|---|:--|:--|
| **R1 warmed** | **HIT** `cache_read:7704` | **HIT** `cache_read:7704` |
| **R2 control** | — | **MISS** `cache_creation:7680` |

**Receipt evidence:**
- `R1@480 → {"cache_read_input_tokens":7704,"cache_creation_input_tokens":0}`
- `R2ctrl@480 → {"cache_read_input_tokens":0,"cache_creation_input_tokens":7680}`

**Docs comparison: CONFIRMED and QUANTIFIED.** The docs say a read "refreshes"
the TTL but never demonstrate it bridging multiple windows. Here it is,
mechanically: R1's read at 4:00 reset the clock to ~4:00+TTL ≈ 540 s, so the 8:00
return (480 s) still landed inside it and read warm. R2, with no intervening
read, died at 5:00 and paid the full write premium again at 8:00. **A single read
at t=4 min carried a 7704-token prefix across an 8-minute gap a 5-minute TTL
cannot span.**

**Brevitas lever — this is the company.** A keep-alive **read** (max_tokens=1)
is priced at **0.10×** and refreshes the clock exactly like a real arrival. So a
returning request after an 8-minute idle costs:
- **Unwarmed:** cold write = **1.25×** the prefix.
- **Warmed (one ping):** ping (≈0.10× at ping time) + warm read (0.10×) ≈ **0.20×**.

That's the ~**6× swing** the warming ledger books — now demonstrated end-to-end
on live infra with provider receipts, not modeled. The invariant this run also
validates: warming only converts on gaps that *exceed* the TTL (P1); pinging a
customer whose own traffic stays inside 300 s is pure cost, which is why the
policy must gate on measured inter-arrival, not just presence.

---

## P5 — 1-hour extended tier (sample, not swept)

**Method.** One prefix written with `cache_control:{"type":"ephemeral","ttl":"1h"}`
(sent with `anthropic-beta: extended-cache-ttl-2025-04-11`), read once at **360 s**
— past the 5-minute default cliff that killed the P1 fan prefixes on the *same
timeline*.

| Leg | Receipt |
|---|:--|
| write `ttl:"1h"` | `cache_creation:{"ephemeral_1h_input_tokens":7766,"ephemeral_5m_input_tokens":0}` |
| read @360 s (6 min) | `cache_read_input_tokens:7766`, `cache_creation:0` → **HIT** |

**Docs comparison: MATCH, plus an undocumented receipt detail.** The 1h tier is
**live** and behaves: at t=360 s the default-tier fan prefixes were already cold
(P1), while the 1h prefix read warm. Notably the usage receipt **splits cache
writes by tier** — `cache_creation.ephemeral_1h_input_tokens` vs
`ephemeral_5m_input_tokens` — so tier attribution is observable per call and we
never have to guess which tier a write landed in. The beta header was accepted
without error (tier may now be GA-adjacent; header still honored).

**Brevitas lever.** For predictable long-cadence traffic (cron/batch every
5–60 min), a **1h write (2.0×) amortized over many warm reads (0.10×)** can beat
repeated **5m writes + pings**. The break-even is a function of arrival density:
above ~1 arrival per ~5 min the 5m-tier + ping is cheaper; for sparse-but-regular
patterns the 1h tier removes the ping entirely. Because the receipt reports the
tier split, the settlement path can price 1h and 5m writes distinctly (2.0× vs
1.25×) with zero ambiguity — `receipts.py` should carry a `write_1h` rate rather
than reuse the 5m `write`.

---

## What we measured vs. what remains

**Measured this run (all with live provider receipts):** `claude-haiku-4-5`'s
min-token floor (4096 — that model only; the floor is per-model, see P3),
breakpoint max (4, hard 400), raw TTL cliff (300–330 s), indefinite refresh via
periodic reads (to 450 s), refresh-on-read bridging an 8-min gap, and the 1h tier
outliving the 5m clock at 6 min. Total **$0.2336**.

**Not yet measured (harness is left runnable for these):**
- **Cliff resolution.** The raw cliff is bracketed to (300, 330] s. A follow-up
  fan at 300/305/310/315/320/325 s would pin it to ±5 s (~$0.06, ~6 min).
- **1h tier exact TTL.** Confirmed alive at 6 min; its own expiry (nominal 3600 s)
  is unmeasured — a single read at ~62 min would confirm the far cliff (~$0.02,
  but a 1-hour wait).
- **Cross-model floors.** Only `claude-haiku-4-5`'s 4096 floor is measured here.
  **Every other row of the P3 tier table is docs-sourced and unverified on live
  infra** — the **512 tier** (Fable / Mythos / Opus 5) is the one worth probing
  first: it is the only tier that gates *below* the 1024 default, so if the real
  floor is higher than the docs say, we mark prefixes that never cache and book a
  write that never happened. An over-stated floor merely misses savings; an
  under-stated one over-claims, so probe the low tier first. One boundary-pair
  probe per tier (~$0.05 each, the P3 method) would convert the whole table from
  docs to measured.
- **Grace-window stability.** The (300, 330] grace was seen once; whether the
  cliff is 300 s hard or has a consistent ~15–30 s grace wants 2–3 repeats to
  distinguish jitter from a real grace band.

None of the gaps change the shipped levers: the ≤300 s keep-alive cadence, the
model-specific token floor, and the 6× warmed-vs-cold economics are all
established by the receipts above.

---

*Harness: `scripts/anthropic_cache_probe.py`. Reproduce:
`BREVITAS_ANTHROPIC_PROBE=1 .venv/bin/python scripts/anthropic_cache_probe.py`.
Guarded by a $5 list-price cap; synthetic prefixes only; probe key only.*
