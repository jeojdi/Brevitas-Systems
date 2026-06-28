"""Benchmark — Lever 4 Adaptive (MaxSim reranking + adaptive-k vs fixed k=5 and k=8).

Measures retrieval accuracy (recall, token reduction) using:
  1. Fixed k=5 (baseline with low accuracy)
  2. Fixed k=8 (baseline with no loss but high token cost)
  3. Adaptive + MaxSim reranking (target: high accuracy + high savings)

Metrics:
  * recall@k     : fraction of gold supporting passages retrieved (DPR quality)
  * avg_k_chosen : average passages selected by adaptive strategy
  * token_reduction: avg tokens sent (adaptive) vs full context (all 10 passages)

Run:
    python benchmarks/levers/bench_lever4_adaptive.py            # default N=100
    python benchmarks/levers/bench_lever4_adaptive.py 60
"""

import json
import os
import sys

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np

from token_efficiency_model.lossless.retrieval import (
    AdaptiveRetrievalConfig,
    DenseRetriever,
    fetch_adaptive,
)


def _tok(s: str) -> int:
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(s, disallowed_special=()))
    except Exception:
        return max(1, int(len(s.split()) * 1.3))


def load_examples(n: int):
    from datasets import load_dataset
    ds = load_dataset("hotpot_qa", "distractor", split="validation")
    out = []
    for i in range(min(n, len(ds))):
        ex = ds[i]
        titles = ex["context"]["title"]
        sents = ex["context"]["sentences"]
        passages = [t + ". " + " ".join(s) for t, s in zip(titles, sents)]
        gold = set(ex["supporting_facts"]["title"])
        gold_idx = {j for j, t in enumerate(titles) if t in gold}
        if gold_idx:
            out.append({"q": ex["question"], "passages": passages, "gold": gold_idx, "titles": titles})
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    class Enc:
        def encode(self, texts, normalize_embeddings=True):
            return model.encode(texts, normalize_embeddings=normalize_embeddings,
                                show_progress_bar=False, batch_size=64)

    enc = Enc()
    examples = load_examples(n)

    # Baselines
    recall_k5 = []
    recall_k8 = []
    tokens_k5 = 0
    tokens_k8 = 0
    full_tokens = 0

    # Adaptive + MaxSim
    recall_adaptive = []
    tokens_adaptive = 0
    avg_k_adaptive = []

    for ex in examples:
        r = DenseRetriever(enc)
        r.index(ex["passages"])

        # Full context
        full_tokens += sum(_tok(p) for p in ex["passages"])

        # Fixed k=5
        hits_5 = r.retrieve(ex["q"], k=5)
        got_5 = {h[0] for h in hits_5}
        recall_k5.append(len(got_5 & ex["gold"]) / len(ex["gold"]))
        tokens_k5 += sum(_tok(ex["passages"][i]) for i in got_5)

        # Fixed k=8
        hits_8 = r.retrieve(ex["q"], k=8)
        got_8 = {h[0] for h in hits_8}
        recall_k8.append(len(got_8 & ex["gold"]) / len(ex["gold"]))
        tokens_k8 += sum(_tok(ex["passages"][i]) for i in got_8)

        # Adaptive + MaxSim
        # Uses elbow method: finds largest score gap (diminishing returns)
        cfg = AdaptiveRetrievalConfig(
            max_k=10,
            use_maxsim_rerank=True,
        )
        chunks, meta = fetch_adaptive(r, ex["q"], ex["passages"], encoder=enc, cfg=cfg)
        if not meta["fallback_applied"]:
            # Find which indices were chosen (by chunk matching)
            chosen_idx = {j for j, p in enumerate(ex["passages"]) if p in chunks}
            recall_adaptive.append(len(chosen_idx & ex["gold"]) / len(ex["gold"]))
            tokens_adaptive += sum(_tok(p) for p in chunks)
            avg_k_adaptive.append(meta["k_chosen"])
        else:
            # Fallback is full context; recall is perfect but costly
            recall_adaptive.append(1.0)
            tokens_adaptive += full_tokens
            avg_k_adaptive.append(len(ex["passages"]))

    results = {
        "n_examples": len(examples),
        "baseline_k5": {
            "recall": round(float(np.mean(recall_k5)), 4),
            "token_reduction_pct": round(100 * (1 - tokens_k5 / full_tokens), 2),
        },
        "baseline_k8": {
            "recall": round(float(np.mean(recall_k8)), 4),
            "token_reduction_pct": round(100 * (1 - tokens_k8 / full_tokens), 2),
        },
        "adaptive_maxsim": {
            "recall": round(float(np.mean(recall_adaptive)), 4),
            "avg_k_chosen": round(float(np.mean(avg_k_adaptive)), 2),
            "token_reduction_pct": round(100 * (1 - tokens_adaptive / full_tokens), 2),
        },
    }

    checks = {
        "recall_k5>=0.70": results["baseline_k5"]["recall"] >= 0.70,
        "recall_k8>=0.95": results["baseline_k8"]["recall"] >= 0.95,
        "recall_adaptive>=0.90": results["adaptive_maxsim"]["recall"] >= 0.90,
        "adaptive_saves>=35%": results["adaptive_maxsim"]["token_reduction_pct"] >= 35.0,
        "adaptive_better_than_k5": results["adaptive_maxsim"]["recall"] > results["baseline_k5"]["recall"],
    }
    passed = all(checks.values())
    report = {"lever": "4_adaptive", "results": results, "checks": checks, "passed": passed}
    with open(os.path.join(os.path.dirname(__file__), "results_lever4_adaptive.json"), "w") as f:
        json.dump(report, f, indent=2)

    print("=== Lever 4 — adaptive + MaxSim reranking ===")
    print(json.dumps(results, indent=2))
    print("\nchecks:", json.dumps(checks, indent=2))
    print("PASSED" if passed else "FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
