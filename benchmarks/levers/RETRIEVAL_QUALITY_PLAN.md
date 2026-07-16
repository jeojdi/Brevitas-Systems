# Retrieval quality plan

## Decision

Use hybrid dense + BM25 retrieval, Reciprocal Rank Fusion, and a bounded second evidence
hop. Keep automatic retrieval off unless `BREVITAS_RETRIEVAL_ENABLED=1`; the explicit
retrieval endpoint remains available for evaluation.

The direct Python SDK keeps that opt-in default. BVX `0.1.22` pins model `0.9.11` and opts
its managed optimizer into retrieval; set `BREVITAS_RETRIEVAL_ENABLED=0` to disable it.

Retrieval is context-reducing, not lossless. Cache-only remains the byte-preserving default.

## Evidence behind the design

- [Dense Passage Retrieval](https://aclanthology.org/2020.emnlp-main.550/) establishes the
  dense semantic baseline.
- [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)
  provides a training-free way to combine rankings whose raw scores are not comparable.
- [Analysis of Fusion Functions for Hybrid Retrieval](https://arxiv.org/abs/2210.11934)
  shows lexical and semantic retrieval are complementary, while warning that fusion
  parameters should be validated rather than assumed universal.
- [Multi-hop Dense Retrieval](https://arxiv.org/abs/2009.12756) and
  [IRCoT](https://aclanthology.org/2023.acl-long.557/) show why one-shot retrieval is
  insufficient when the next document depends on evidence from the first.
- [Lost in the Middle](https://aclanthology.org/2024.tacl-1.9/) shows that sending more
  context is not automatically safer; models can fail to use evidence buried in long inputs.
- [HotpotQA](https://aclanthology.org/D18-1259/) supplies document-level supporting facts
  for testing multi-document evidence coverage.

## Implemented pipeline

1. Rank supplied context with normalized MiniLM dense embeddings.
2. Independently rank it with local BM25 for exact names, dates, IDs, and rare terms.
3. Fuse both rankings with RRF (`k=60`).
4. Inspect the best first-hop passages for exact links to other supplied passage titles,
   including conservative aliases and reverse links.
5. Protect at most two new bridge passages in the final evidence set.
6. Fall back to full context for empty, broad, low-confidence, or negligible-savings queries.
7. Restore original conversation order in the proxy.

The previous sentence-level “MaxSim” heuristic remains for backwards-compatible experiments,
but production no longer describes it as a faithful ColBERTv2 reranker.

## Validation gates and results

Evidence gate on a held-out 200-question HotpotQA slice:

- supporting-title recall: **99.25%**
- every supporting title retained: **98.5% of questions**
- average passages sent: **7.95 of 10**

Paired live DeepSeek gate on 50 questions, reusing the same saved full-context arm:

- exact match: **68.0% retrieval vs 66.0% full context**
- F1: **79.55 retrieval vs 76.98 full context**
- prompt-token reduction: **23.16%**
- substantive full-correct regressions: **0**
- supporting-title recall: **99.0%**

The one mechanical exact-match regression is semantically equivalent: `IFFHS World's Best
Goalkeeper` versus the gold `World's Best Goalkeeper`.

## Default-on gate

Do not enable retrieval globally from this n=50 result. Its paired confidence interval is still
wide, and HotpotQA has only ten candidate passages per question. Enable it per customer only
after a representative paired workload passes all of these:

- no material safety or correctness regression;
- mean F1 delta no worse than -1 point;
- at least 10% prompt-token reduction after provider-cache pricing;
- manual review of every full-correct/retrieval-wrong case;
- enough samples for the paired confidence interval to support the decision.
