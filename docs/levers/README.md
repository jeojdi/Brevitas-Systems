# Brevitas Lossless Levers — algorithms, sources, and real benchmarks

This package (`token_efficiency_model/lossless/`) implements token-saving levers, each a
**faithful implementation of a published algorithm**, validated against the papers' own
metrics and — where a real model is available — against a **real public benchmark**.

> **Grounding rule (matches REVAMP_PLAN.md):** every lever maps to a real paper/standard.
> No hand-rolled algorithms presented as novel; no fabricated benchmark numbers. Where a
> result is from a real model on a real dataset it says so; where a number is deterministic
> (algorithmic) or simulated it says that too.

## The levers

| # | Lever | Source (verified) | File |
|---|-------|-------------------|------|
| 1 | Provider-native caching | Anthropic `cache_control` docs; OpenAI prompt-caching docs | `provider_cache.py` |
| 2 | Content-addressed dedup | IPFS Merkle DAG (Benet 2014, arXiv:1407.3561); LBFS content-defined chunking (Muthitacharoen et al., SOSP 2001) | `content_store.py` |
| 3 | Delta transmission | Myers O(ND) diff (1986); VCDIFF (RFC 3284); rsync rolling checksum (Tridgell & Mackerras, TR-CS-96-05, 1996) | `delta.py` |
| 4 | Retrieval (not stuffing) | DPR (Karpukhin et al., EMNLP 2020, arXiv:2004.04906); ColBERTv2 MaxSim + residual compression (Santhanam et al., NAACL 2022, arXiv:2112.01488) | `retrieval.py` |
| 5 | Recursive Language Model | RLM (Zhang, Kraska, Khattab, MIT, arXiv:2512.24601) | `rlm.py` |

Accuracy-first invariants enforced in code: caching never mutates the byte-stable prefix;
dedup/delta re-hash on read and fail-safe to full content on any mismatch; retrieval
fail-safes to full context on empty index / low confidence.

## Algorithm map

See [`ALGORITHMS.md`](./ALGORITHMS.md) for the exact algorithm extracted from each primary
source (pseudocode + equations) and how it maps onto Brevitas.

## Benchmarks

Two kinds, clearly labelled:

- **Algorithmic / deterministic** (`benchmarks/levers/bench_lever{1,2,3}_*.py`): dedup ratio,
  delta size, cache math. Exact by construction; inputs are synthetic and the scripts say so.
- **Real model on a real public benchmark** (`benchmarks/levers/bench_lever4_retrieval.py`,
  `bench_e2e_accuracy.py`): HotpotQA (cached) with `all-MiniLM-L6-v2` embeddings and
  **DeepSeek** as the answering model, scored with the **official HotpotQA EM/F1**.

See [`BENCHMARKS.md`](./BENCHMARKS.md) for the measured numbers and the honest
accuracy/savings frontier.

## Wiring status

- `POST /v1/compress/retrieval` (additive) exposes Lever 4 with the fail-safe; records
  **real** token savings and **no** quality proxy. The legacy lossy `/v1/compress` path is
  unchanged.
- Levers 1/2/3/5 are validated library modules, not yet on the live request path.

## A note on the RLM citation

Earlier internal review flagged `arXiv:2512.24601` (RLM) as possibly fabricated **because a
restricted web search could not surface it**. That flag was wrong: the paper is real
(verified from its arXiv source package — Zhang, Kraska, Khattab, MIT CSAIL). The citation
stands. The narrower, valid point: the legacy `rlm_orchestrator.py` *cited* RLM while only
doing flat retrieval; `lossless/rlm.py` now implements RLM's Algorithm 1 faithfully.
