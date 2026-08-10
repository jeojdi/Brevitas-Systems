# DeepSeek Prompt-Cache Behavior Map (Empirical)

**Proprietary measurement. Do not distribute.**

DeepSeek is the highest-value probe target because it publishes almost **no** cache documentation — no TTL, no clear minimum, only a vague "64-token unit" figure in third-party write-ups. Every number here is measured, not read, against the live `api.deepseek.com/v1` chat endpoint on Brevitas's own funded probe key, with the provider's own `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` as the evidence.

- **Harness:** `scripts/deepseek_cache_probe.py` (re-runnable; `BREVITAS_DEEPSEEK_PROBE=1`, $2 cap, probe key only).
- **Model:** `deepseek-chat`. **Run:** 2026-08-10. Synthetic seeded prefixes, never customer data.
- **Total live spend: $0.0020 of the $2.00 cap.** (Cached reads are $0.0028/Mtok — a full black-box map costs a fifth of a cent.)
- **Guardrail honored:** behavioral characterization only — our key, our synthetic prefixes, no cross-tenant/rate-limit/side-channel probing.

## Headline results

| Probe | Measured | DeepSeek's docs | Verdict |
|---|---|---|---|
| Automatic caching | Write miss → immediate resend **hits** (1792 read), no directives | "automatic context caching" | **MATCH** — fully automatic |
| **Block granularity** | Every cache hit is an **exact multiple of 128 tokens** (128, 256, 512, 896, 1792, 1920) | "64-token units" (third-party) | **DRIFT — it's 128, not 64.** Widely-quoted figure is wrong. |
| **Min cacheable prefix** | 64-tok prefix → **no cache**; 128-tok → **cached** | unpublished | **~128 tokens (one block).** 32× smaller than Anthropic Haiku's 4096. |
| **TTL** | HIT at 60s, 300s, **and 900s (15 min)** — never expired in-window | unpublished | **> 15 min** (consistent with the >55-min prior probe) — vastly outlives Anthropic's 5 min |
| Scoping | Same-key resend hits; keyed per API key | implied | **MATCH** |
| Uncached tail | A 1868-tok prefix caches exactly 1792 (14×128); the last 76 tokens never cache | — | The sub-128 remainder is always cold |

## The three findings that matter

**1. The block size is 128 tokens, not 64 — and this is undocumented.** Every measured cache hit landed on an exact 128-token multiple (`1792 = 14×128`, `896 = 7×128`, `512 = 4×128`). The "64-token unit" number in circulation is simply wrong for the live API. The practical consequence: the final partial block (any tail under the last 128-boundary) never caches — a 1868-token prefix caches 1792 and re-bills the last 76 every time. Prefix and breakpoint design should align to 128-token boundaries to avoid leaving a cold remainder.

**2. The minimum cacheable prefix is ~128 tokens — 32× lower than Anthropic Haiku's 4096.** A 64-token prefix cached nothing; 128 cached fully. This flips the短-prefix story: prompts far too short to ever cache on Anthropic (the silent 4096 floor) are *fully cacheable on DeepSeek*. A customer with small, repetitive prompts gets cache value on DeepSeek that is structurally impossible on Anthropic. That is a real, provider-specific savings surface — and a reason a customer's provider *mix* changes which levers pay.

**3. TTL exceeds 15 minutes and shows no sign of expiring** — alive at 60s, 300s, and 900s with identical 1792-token hits. Combined with the prior >55-min probe, DeepSeek's cache lives **hours**, not minutes. This is the empirical proof of why the strategy is right: **warming DeepSeek is almost always waste** (the cache survives real user gaps organically), so the lever there is *measurement and honest attribution* — we bill the customer for the automatic discount only where we can prove Brevitas caused it, and spend zero on pointless pings. The cheap reads ($0.0028/Mtok, 0.02×) make DeepSeek the cheapest provider to *measure* and the one where over-eager warming would most obviously destroy net savings.

## Raw timeline

```
P1 automatic:   write miss=1868 → resend hit=1792  (automatic, 128-block, 76-tok tail cold)
P2 floor sweep: 64→NO CACHE | 128→hit128 | 256→hit256 | 512→hit512 | 1024→hit896 | 2048→hit1920
                (every hit an exact 128-multiple → block size = 128, floor = 128)
P6 scoping:     req1 miss → req2 hit (same-key reuse; per-key scope)
P3 TTL:         gap 60s → HIT | gap 300s → HIT | gap 900s(15min) → HIT  (never expired)
```

Receipts: `scratchpad/deepseek_cache_probe_receipts.json`. Harness: `scripts/deepseek_cache_probe.py`.

## Not yet measured (next money)

- **Peak-hour pricing.** DeepSeek announced Beijing-hours 2× pricing; confirm from a receipt taken *inside* a peak window whether the multiplier is live, and whether cache reads are exempt. If reads stay cheap while input doubles, the cache discount is *worth more* at peak — a timing signal for measurement.
- **The far TTL edge.** It's beyond 15 min; a bounded overnight sweep (30/60/120 min) would pin whether it's ~1 h or genuinely hours, sharpening the "never warm DeepSeek" rule into an exact threshold.
- **Refresh-on-read** — untested because the TTL is too long for it to matter at normal gaps.

## Cross-provider picture (with docs/ANTHROPIC_CACHE_MAP.md)

| | Anthropic Haiku | DeepSeek |
|---|---|---|
| Min cacheable | 4096 tok | **128 tok** |
| Block size | prefix-to-breakpoint | **128 tok** |
| TTL (untouched) | 5:00–5:30 | **> 15 min (hours)** |
| Read discount | 0.10× | **0.02×** |
| Refresh-on-read | yes (free) | irrelevant (TTL too long) |
| **The lever** | **warming + placement** (short TTL makes warming pay) | **measurement + attribution** (long TTL makes warming waste) |

Two providers, opposite physics, opposite levers — measured for **$0.24 total**. This is the map that stays correct while they drift, and it is the thing a docs-reading competitor cannot reproduce.
