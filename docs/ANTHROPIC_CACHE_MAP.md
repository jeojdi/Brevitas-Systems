# Anthropic Prompt Cache — Empirical Map

**Measured live on `claude-haiku-4-5`, 2026-08-10, on Brevitas's own probe key. Total spend $0.23 (cap $5). Every row is backed by Anthropic's own usage receipt (`cache_read_input_tokens` vs `cache_creation_input_tokens`).**

This is proprietary measurement — the *actual* behavior, not the documented behavior. It is the first entry in the continuously-re-probed cache map that is Brevitas's real moat. Re-run `scripts/anthropic_cache_probe.py` (needs `BREVITAS_ANTHROPIC_PROBE=1`) on a schedule; when a number here moves, a provider changed something silently and there is money in the delta.

## Findings

| # | Probe | Measured result | Anthropic's docs | Verdict |
|---|---|---|---|---|
| P1 | **TTL cliff** | HIT at 270s and **exactly 300s**; MISS at 330s and beyond | "5 minutes" | **Holds, slightly generous** — the entry is still alive *at* 5:00 and dies between 5:00 and 5:30 |
| P2 | **Refresh-on-read** | **CONFIRMED.** A prefix read at 4min then 8min = HIT; an identical prefix read only at 8min = MISS. The "chain" prefix, read every 60–90s, stayed warm a full 7.5 min on one write. | Documented but unquantified | **Every read resets the 5-min clock, free.** This is the single most important economic fact. |
| P3 | **Min cacheable tokens** | 4094 tok → `write=0` (silently uncached); 4351 tok → `write=4338` (cached) | 4096 for Haiku 4.5 | **Exactly 4096.** Below it, caching fails with **no error** — `cache_creation=0` and full-price billing. |
| P4 | **Breakpoint limit** | 1 and 4 cache normally; **5 → HTTP 400** "A maximum of 4 blocks with cache_control may be provided." | Max 4 | **Hard error, not a silent cap.** Over-injection breaks the request outright. |
| P5 | **1h tier** | The `ttl:"1h"` prefix was still a HIT at 6min — the exact moment the default-tier prefixes died. Same timeline, controlled contrast. | 1h opt-in tier | **Live and behaves.** 8× longer life for a 2× (vs 1.25×) write premium. |

## What each finding means for a Brevitas lever

**P2 (refresh-on-read) is the money fact, and it reframes warming.** Because every real read resets the clock for free, **warming is only needed to bridge gaps longer than 5 minutes.** A customer whose users fire more often than every 5 minutes needs *zero* warming — pinging them is pure waste (this is exactly the "organic self-refresh" suppression the learned scheduler now enforces). Warming's whole addressable market on Anthropic is the gap *between* 5 minutes and however long until the user returns. And when that gap is long, there are two bridges — keep-alive pings vs a one-time 1h write — and the cheaper one depends on the gap, which is a per-customer prediction, not a constant.

**P1 (cliff at 5:00–5:30, not before) is slack a doc-reader doesn't know they have.** A cloner trusting "5 minutes" abandons or re-warms at exactly 300s. The measurement says the entry is still alive at 300s and safe to ~315s. Small, but it's the shape of the whole thesis: the real number is not the published number, and only measurement finds the gap.

**P3 (silent 4096 floor) is a diagnostic you can sell.** A customer whose stable prefix sits just under 4096 tokens is paying full price on every request and getting *nothing* from caching, with no error to tell them. "We'll show you every prompt that's silently missing the cache floor" is a onboarding hook no competitor offers because they don't measure per-prefix.

**P4 (hard 400 at 5 breakpoints) is a passthrough-fidelity guardrail.** Any breakpoint-injection logic must respect the 4-cap or it breaks the customer's request. Ours does; a naive injector that adds a 5th on top of a caller's 4 would 400 their traffic.

**P5 (1h tier live) confirms the tiered-breakpoint lever.** Put the frozen tools+system prefix on 1h and the volatile conversation tail on 5m: the big stable bytes survive an hour (amortizing the 2× write over many turns) while the churny tail pays the cheaper 1.25× write. The scheduler's multi-action menu already models this; P5 confirms the tier it depends on is real.

## The raw timeline (one write batch, staggered single reads)

```
t=0     11 fresh writes land (~7700 cache-creation tokens each)
+60s    chain read      → HIT  (read=7726)
+270s   fan270 read     → HIT  (read=7721)   4.5 min: warm
+300s   fan300 read     → HIT  (read=7756)   5.0 min exactly: STILL warm
+330s   fan330 read     → MISS (write=7747)  5.5 min: dead  ← cliff is here
+360s   fan360 read     → MISS ; 1h-tier read → HIT (read=7766)  ← default dead, 1h alive
+390s.. fan390/420/450  → MISS (confirm)
+480s   R1 (read@4min then@8min) → HIT (read=7704)   ← refresh kept it alive
+480s   R2 (read only @8min)     → MISS (write=7680) ← no refresh, expired
        chain (read every ~60-90s) → HIT at every point through 7.5 min
```

Receipts: `scratchpad/receipts.json`. Harness: `scripts/anthropic_cache_probe.py`.

## Next probes worth the money (per the cross-provider sweep)

1. **Fan-out formation lag** — how many milliseconds after the first response starts streaming does the cache become readable? Determines the serialize-then-release lever's timing on agent swarms.
2. **20-block lookback** — construct a turn that appends >20 content blocks and confirm the prior cache goes unreachable; measure the reprocessing cost.
3. **Re-probe P1–P5 weekly** — the March 2026 TTL regression is the precedent; the map's value is catching the *next* one first.
