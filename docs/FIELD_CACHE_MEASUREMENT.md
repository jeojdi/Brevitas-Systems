# Field cache measurement + cross-agent billing probe

**Date:** 2026-08-14. **Live spend:** $1.0782 (probe key, `claude-haiku-4-5`).
**Harnesses:** `scripts/field_cache_measure.py` (offline, read-only),
`scripts/xagent_billing_probe.py` (live, `BREVITAS_XAGENT_PROBE=1`, $3.00 cap).
**Receipts:** `field_cache.json`, `xagent_receipts.json`, `x4_receipts.json`.

This round exists because a 47-agent prior-art sweep found that every claim in
`DOES_AI_NATIVE_REDIS_WORK.md` was either vendor-documented, already published,
or resting on evidence that did not support it. Two objections were load-bearing:

1. **No field data.** Every "in the field" claim rested on synthetic corpora or
   on `random.Random` traces inside `scripts/warm_replay_sim.py`.
2. **The serial-fan-out artifact.** The cross-agent result was serial by
   construction; real agent fan-out is concurrent.

Both are now addressed with real data.

---

## Part 1 — Field measurement (no API spend, no synthetic traffic)

Claude Code session transcripts carry Anthropic's own usage receipt for every
assistant turn (`cache_read_input_tokens`, `cache_creation_input_tokens`, and
the `ephemeral_5m` / `ephemeral_1h` split). That is real billed production
traffic of a long-context coding agent — the exact workload the literature
studies — and it was already on disk.

**Population:** 112 sessions, 9,224 billed requests, 2,493,264,308 prompt tokens.

### 1.1 Realized cache performance

| Quantity | Value |
|---|---|
| Prompt tokens | 2,493,264,308 |
| Billed at the 0.10× read rate | 2,430,091,774 (**97.47%**) |
| Cache writes | 63,134,138 |
| Fresh (uncached) input | 38,396 |
| 1h tier share of all writes | **88.8%** |
| Billed vs. no-cache counterfactual | $1,212 vs $7,601 → **caching saved 84.1%** |

This **corroborates** the field literature (arXiv 2607.13080's 99.3% hit rate /
88.6% saving) and **refutes our own "f is effectively zero"** headline. That
claim was an artifact of scanning string literals in eight repositories at HEAD,
which cannot see runtime-assembled prompts — the mechanism that generates
essentially all cacheable prefix.

### 1.2 Mechanism split — the decisive number

| Mechanism | Hit rate | Share of all cache value |
|---|---|---|
| Within-session continuation (growing prefix) | 97.63% | **99.86%** |
| Cold-open / cross-session (shared credential) | 44.02% | **0.14%** |

75 of 112 sessions did read cache on their very first request, so cross-session
sharing is **real and common** — but it is worth almost nothing, because a
session's first request is one of ~82 and carries a small share of its tokens.

This is the reconciliation the whole programme needed. The 70–88% figures in the
market are **within-session growing-prefix continuation**. The cross-agent
sharing mechanism we measured at 66% under ceiling conditions accounts for
**0.14%** of realized value in real traffic. Both results are correct; they are
answers to different questions.

### 1.3 The arrival distribution warming turns on

| | median | p90 | p99 |
|---|---|---|---|
| Inter-request gap | 15.4 s | 117.3 s | 3,925 s |

P(gap ≤ 300s) = **93.7%** · P(300s < gap ≤ 1h) = **5.2%** · P(gap > 1h) = **1.1%**

This replaces the simulator's synthetic arrivals with measured ones, and it
**independently confirms the warming verdict on real data**: 93.7% of gaps never
reach even the 5m cliff, and the agent already buys the 1h tier for 88.8% of
writes. The window where warming could add anything over the 1h tier is the 5.2%
of gaps in (300s, 1h] — and inside that window the tier already covers it.

### 1.4 The TTL cliff, in situ

Recovery = `cache_read(cur) / prompt_tokens(prev)`. In a growing conversation
every request writes new cache, so `cache_creation > 0` is the steady state, not
a miss; recovery is the signal that tracks expiry.

| gap (s) | pairs | median recovery | loss rate |
|---|---|---|---|
| 0–30 | 6,620 | 100.0% | 0.2% |
| 240–300 | 49 | 100.0% | 4.1% |
| 300–330 | 23 | 100.0% | 4.3% |
| 1800–3600 | 67 | 100.0% | 9.0% |
| **3600–7200** | **33** | **0.0%** | **72.7%** |
| 7200–86400 | 62 | 0.0% | 75.8% |

**The field cliff is at 3600s, not 300s** — because the workload buys the 1h
tier. Median recovery goes 100% → 0% exactly at the tier boundary. 38.3M tokens
were re-paid after a lost prefix (~$144 at the 5m write rate, ~12% of the bill).

Caveat: observational, not a controlled probe. Recovery can also fall because
earlier context was edited, so the loss rate is an **upper bound** on expiry.

---

## Part 2 — Live cross-agent billing probe

### 2.1 X1 — concurrency breaks the closed form

Five callers fired at one fresh above-floor prefix with controlled skew; we
count how many are billed `cache_creation`.

| inter-arrival skew | writers (of 5), 2 reps | mean |
|---|---|---|
| 0 ms | 5, 5 | **5.0** |
| 50 ms | 5, 1 | 3.0 |
| 200 ms | 2, 5 | 3.5 |
| 1,000 ms | 1, 1 | **1.0** |
| 5,000 ms | 1, 1 | **1.0** |

**Under genuinely simultaneous fan-out every caller pays the write premium.** The
saving identity `f·(N − 1.25 − 0.10(N−1))/N` assumes exactly one writer and
N−1 readers; at 0 ms skew there are five writers and zero readers, so the model
does not merely lose accuracy, it inverts — the cached arm pays 5 × 1.25× with
no reads at all. The identity is recovered once skew exceeds ~1 s.

This is the regime practitioners actually deploy (parallel sub-agents launched
together), and it is a **new result**: no published work prices a concurrent
fan-out against a commercial cache.

**Limitation, stated plainly:** n = 2 per cell. The 50 ms and 200 ms cells
disagree between reps (5 vs 1, and 2 vs 5), which is what a race should look
like, but n = 2 cannot characterize the transition. The endpoints (0 ms → 5
writers; ≥1 s → 1 writer) replicate cleanly; the window between them does not
yet have a measured shape.

### 2.2 X2 — the +24% inversion is a depth-1 artifact, and it flips at k=2

Cost of the cached arm vs the uncached arm as a function of reuse depth:

| reuse depth k | 1 | 2 | 3 | 5 | 10 |
|---|---|---|---|---|---|
| cached vs plain | **+24.83%** | −32.08% | −51.69% | −66.52% | −77.84% |

The +24.83% independently replicates the previously reported +24.12%, and now
has its explanation: at k=1 the cached arm pays the 1.25× write premium and
collects zero reads. **The penalty flips sign at the second call.** "Caching made
my bill worse" is real, but it is precisely the cost of marking a prefix that is
never reused — not a general property of enabling caching.

### 2.3 X3 + X4 — invalidation is breakpoint-granular, and the floor truncates it

With a **single** breakpoint, all ten perturbation axes behaved identically —
leading space, trailing space, CRLF, NFC/NFD normalization, case flip, and
single-token insertion at head, middle, **and tail** all gave **0% recovery**.
Only the byte-identical control recovered 100%.

This corrects a claim in `DOES_AI_NATIVE_REDIS_WORK.md`. "Relocating volatile
tokens tailward is −91.7%/call" is **not** true of moving a token toward the tail
*within* a cached block; it is true only of moving it **past the last
breakpoint**. Inside one block, position is irrelevant — any byte kills all of it.

X4 splits the prefix into 4 individually-cached segments and perturbs one token
in segment j:

| perturbed | segment 1 | segment 2 | segment 3 | segment 4 | past last breakpoint |
|---|---|---|---|---|---|
| breakpoint-granular predicts | 0% | 25% | 50% | 75% | 100% |
| **measured** (rep 0 / rep 1) | 0% / 0% | **0% / 0%** | 50.4% / 49.9% | 74.9% / 74.8% | 100% / 100% |

Segments 3, 4 and past-the-last-breakpoint confirm breakpoint granularity to
within 0.5pp. **Segment 2 deviates, and the deviation is the finding:** the
surviving prefix there is ~2,194 tokens, which is below the model's ~4,096-token
minimum cacheable length, so it cannot be cached at all. At segment 3 the
survivors total ~4,388 tokens and clear the floor by 292 tokens — a razor-thin
margin that the data lands on exactly.

So the rule is not `recovery = (j−1)/n`. It is:

```
recovery = (j−1)/n   if   (j−1)/n · L ≥ floor(model)
         = 0         otherwise
```

**The minimum-cacheable-length floor truncates the breakpoint staircase from
below.** Segmentation buys partial invalidation only for segments far enough in
that the surviving prefix independently clears the floor — which is a concrete,
actionable constraint on any prefix-compiler design, and is not documented
anywhere we could find.

---

## What this changes

**Retracted / corrected:**
- "f is effectively zero in the field" — refuted by our own field data (97.47%).
- "67% is a hard ceiling; 70–88% is arithmetically impossible" — false; the
  identity is monotone in N with asymptote 0.9f. Corrected in place.
- "Relocating volatile tokens tailward" — only past the last breakpoint.

**Strengthened:**
- The warming verdict now rests on a measured arrival distribution rather than
  on `random.Random`, and survives.
- The +24% inversion is replicated and now has a sign-flip point (k=2).

**New:**
- Concurrent fan-out makes every caller pay the write premium (X1).
- The floor truncates the breakpoint invalidation staircase (X4).
- 99.86% of realized cache value is within-session continuation (field).

## Limitations

- Field data is **one user, one agent harness** (Claude Code), 112 sessions.
  Single-subject; the arrival distribution is not claimed to generalize.
- **No invoice has been reconciled.** Every dollar is rate-card arithmetic over
  vendor-reported counters. The "receipts-grade" framing should not be used
  externally until a dedicated-account reconciliation is run.
- X1 n = 2 per cell; the race window is bounded, not characterized.
- One model (`claude-haiku-4-5`) for all live arms; floors and tiers are
  per-model, so the X4 floor interaction needs re-running per model.
- Transcripts are the user's own; publishing any corpus derived from them
  requires a separate privacy pass.
