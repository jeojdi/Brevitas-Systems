# Historical exploratory results — not Brevitas-attributable evidence

> These runs predate the paired-control protocol. Arm order and cache namespaces were not
> isolated, so the percentages below must not be used as product savings claims. Retrieval
> also removes context and is quality-affecting; it is not a lossless lever.

Real public datasets (HotpotQA, BBH, HumanEval), real models, official metrics, real token
usage from the providers. No fabricated data. HumanEval pass@1 is REAL code execution.

## Accuracy + raw token savings

### OVERALL AGENT — HotpotQA distractor (multi-hop QA), n=50
| model | full EM | adaptive EM | EM Δ | raw token save |
|---|---|---|---|---|
| deepseek-chat | 64.0 | 64.0 | **0.0** | 38.7% |
| gpt-4o-mini | 64.0 | 58.0 | **-6.0** | 38.6% |

### SWE — HumanEval pass@1 (executed), n=50, demos pool=12
| model | full | adaptive | zero-shot | Δ(adapt-full) | raw token save |
|---|---|---|---|---|---|
| deepseek-chat | 92.0 | 92.0 | 96.0 | 0.0 | 53.9% |
| gpt-4o-mini | 64.0 | **82.0** | 60.0 | **+18.0** | 53.7% |

### LOGIC — BBH logical_deduction, n=50, demos pool=16
| model | full | adaptive | zero-shot | Δ(adapt-full) | raw token save |
|---|---|---|---|---|---|
| deepseek-chat | 84.0 | 94.0 | **100.0** | +10.0 | 62.7% |
| gpt-4o-mini | 76.0 | 66.0 | 76.0 | -10.0 | 62.5% |

## ⚠️ THE BIG FINDING: raw token savings ≠ COST savings (provider caching)

Computed real input-cost (cached tokens billed at ~0.1x DeepSeek, ~0.5x gpt-4o-mini):

| benchmark | model | raw token save | **REAL COST save** |
|---|---|---|---|
| HotpotQA | deepseek | -38.7% | **+54% MORE expensive** |
| HotpotQA | gpt-4o-mini | -38.6% | -37.6% (cheaper) |
| BBH | deepseek | -62.7% | **+83% MORE expensive** |
| BBH | gpt-4o-mini | -62.5% | -31.4% (cheaper) |
| HumanEval | deepseek | -53.9% | **+93% MORE expensive** |
| HumanEval | gpt-4o-mini | -53.7% | -30.3% (cheaper) |

**Why:** strong-caching providers (DeepSeek, Anthropic) bill a *repeated* full prefix at ~10%.
Retrieval VARIES the context per request → defeats the prefix cache → billed at full price.
So fewer tokens can cost MORE money. On gpt-4o-mini (weaker caching here) retrieval still wins.
This empirically reproduces the warning in the project's own REVAMP_PLAN: lossy/varying context
"destroys these free caches → we may be paying full price while believing compression saved money."

**Caveat on the cost numbers:** the benchmark ran 4 conditions sequentially per question, which
contaminates cache measurement (later conditions reuse earlier ones' cache). The DIRECTION is
robust and theory-backed; exact cost % needs an isolated per-condition run.

## Defensible conclusions
1. Provider caching can reduce billed cost without reducing provider input tokens. Cache writes
   may carry a premium, so no universal positive ROI follows from these runs.
2. Retrieval reduces provider input by removing context and can change per-example behavior.
3. The observed task and provider differences justify paired, randomized, isolated reruns; they
   do not establish a general Brevitas-incremental percentage.

## ISOLATED COST TEST (clean, no cache contamination) — n=30, real USD

| provider | UNIQUE context (diff every call) | SHARED context (reused, agent-typical) |
|---|---|---|
| DeepSeek (cache 90% off) | **retrieval −38.9% cheaper** | **full+cache wins; retrieval +74% MORE** |
| gpt-4o-mini (cache 50% off) | **retrieval −38.7% cheaper** | **retrieval −40.7% cheaper** |

Driver: DeepSeek caches a repeated full context at 0.1x → so cheap that retrieval (full-price
varying tokens) can't beat it when context repeats. OpenAI caches at only 0.5x → fewer tokens
still wins. The earlier "retrieval +54% more expensive everywhere" was a contaminated artifact;
clean isolated test shows it is PATTERN- and PROVIDER-dependent.

RECOMMENDATION: rerun with separate control/treatment credentials, isolated cache namespaces,
randomized arm order, fixed transcripts, cold/warm results, repeated trials, and confidence intervals.
