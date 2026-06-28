# Benchmark results (measured)

All numbers below are copied from the recorded `benchmarks/levers/results_*.json` / logs.
Each row is labelled by **kind** so nothing is mistaken for a real-model result that isn't.

Kinds:
- **algorithmic** — deterministic measurement on synthetic inputs (exact by construction).
- **real-model** — a real model on a real public dataset with the dataset's official metric.

## Lever 1 — provider-native caching  *(simulated provider accounting)*

Provider cache accounting simulated from documented discount rates (no live keys here);
breakpoint logic and `savings_from_usage` are unit-tested. Re-run with live keys to get
traffic-real numbers.

| scenario | result |
|---|---|
| Anthropic 6-turn loop | **70.69%** input-cost savings; 4 breakpoints; cached prefix 2500 tok; prefix byte-identical |
| OpenAI steady-state call | **42.0%** savings (4200/5000 cached) |

## Lever 2 — content-addressed dedup  *(algorithmic)*

| scenario | result |
|---|---|
| 6 agents sharing a 40 KB spec | **71.62%** dedup savings (276,559 → 78,474 bytes), lossless |
| single edit to a 60 KB artifact | **6.61%** incremental transfer (3,971 bytes), lossless |

## Lever 3 — delta transmission  *(algorithmic)*

| scenario | result |
|---|---|
| code edit (Myers) | delta **1.91%** of file (119 / 6,218 bytes), lossless |
| large blob (rsync) | delta **2.15%** (2,036 / 94,492 bytes), lossless |
| 6-turn plan revision | **18.17%** of full resend, lossless; drift fails safe |

## Lever 4 — retrieval index quality  *(real-model: MiniLM on HotpotQA, n=100)*

| metric | result | target |
|---|---|---|
| recall@5 | **0.79** | ≥0.70 |
| recall@2 | 0.615 | — |
| token reduction @ k=5 | **55.35%** | ≥40% |
| ColBERTv2 residual compression | **10.59×** | 6–10× (paper) |
| recall retention (compressed vs full) | **0.9684** | ≥0.95 |

## End-to-end answer accuracy  *(real-model: DeepSeek on HotpotQA, official EM/F1, n=40)*

This is the honest test of "does the lever preserve **answer** accuracy". Each question is
answered by DeepSeek twice — full context vs retrieval — and scored against the gold answer.

| condition | EM | F1 | prompt tokens | token reduction |
|---|---|---|---|---|
| full context (10 passages) | 62.5 | 78.37 | 57,623 | — |
| retrieval **k=8** | **62.5** | 77.20 | 44,427 | **22.9%** |
| retrieval **k=5** | 55.0 | 70.01 | 25,870 | **55.1%** |

**The frontier, stated plainly:**
- At **k=8**, end-to-end **EM is fully preserved (Δ0.0)**, F1 within 1.2 pts, for ~23% savings.
- At **k=5**, savings jump to ~55% but EM drops ~7.5 pts — because HotpotQA is multi-hop
  (needs *both* gold passages) and recall@5 ≈ 0.79, so a needed passage is sometimes dropped.

**Implication (accuracy-first):** retrieval is a *tunable* tradeoff, not free. For
accuracy-critical multi-hop work, prefer higher k (or **RLM**, Lever 5, which keeps the whole
context as a variable and never drops a passage). DeepSeek's own prompt cache was active in
these runs (6,144 cached tokens at k=5; 18,048 at k=8), so caching stacks on top.

## Reproduce

```bash
python benchmarks/levers/bench_lever1_caching.py
python benchmarks/levers/bench_lever2_dedup.py
python benchmarks/levers/bench_lever3_delta.py
python benchmarks/levers/bench_lever4_retrieval.py 100         # needs cached MiniLM
python benchmarks/levers/bench_e2e_accuracy.py 40 8            # needs DeepSeek key + cached HotpotQA
python -m pytest token_efficiency_model/lossless -q           # 36 unit tests
```
