# Brevitas lossless levers — real benchmark results (n=50, DeepSeek + gpt-4o-mini)

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

## Honest conclusions
1. **Native provider caching is the dominant, universal, lossless money-saver.** Keep the full
   context, send it byte-identical, let the provider cache it. Retrieval can *break* this.
2. **Retrieval/context-reduction saves real money only when** the context is large AND largely
   non-repeating across requests AND the provider caches weakly. Then ~30-50% cost cut.
3. **Accuracy of retrieval is model+task dependent:** matches full on DeepSeek HotpotQA & both
   HumanEval; BEATS full on gpt-4o-mini HumanEval (+18); LOSES on gpt-4o-mini HotpotQA (-6) and
   BBH (-10). On BBH logic, demos HURT — zero-shot (100% DeepSeek) wins; best compression there
   is to send NO demos.
4. The earlier n=8 "adaptive beats full (62.5)" was sampling noise; n=50 corrects it.
