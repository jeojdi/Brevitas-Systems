"""Benchmark — Lever 4 (retrieval: DPR dense retrieval + ColBERTv2 residual compression).

Real benchmark on cached hotpot_qa (distractor) with a cached sentence-transformer.
Metrics:
  * recall@k         : fraction of gold supporting passages retrieved (DPR quality)
  * token_reduction  : tokens sent (top-k passages) vs full context (all 10 passages)
  * compression_ratio: ColBERTv2 residual-compressed index bytes vs full float32
  * recall_retention : recall using compressed embeddings / recall using full embeddings

Run:
    python benchmarks/levers/bench_lever4_retrieval.py            # default N=100
    python benchmarks/levers/bench_lever4_retrieval.py 60
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
    DenseRetriever,
    ResidualCompressor,
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

    K = 5
    recall_at = {2: [], 5: []}
    full_tokens = sent_tokens = 0
    pooled = []                 # all passage embeddings for compression test
    per_q_emb = []              # (emb, gold) for retention test

    for ex in examples:
        r = DenseRetriever(enc)
        r.index(ex["passages"])
        emb = r._emb
        pooled.append(emb)
        per_q_emb.append((emb, ex["gold"], ex["q"]))
        for k in recall_at:
            hits = r.retrieve(ex["q"], k=k)
            got = {h[0] for h in hits}
            recall_at[k].append(len(got & ex["gold"]) / len(ex["gold"]))
        # token accounting at K
        full_tokens += sum(_tok(p) for p in ex["passages"])
        topk_idx = [h[0] for h in r.retrieve(ex["q"], k=K)]
        sent_tokens += sum(_tok(ex["passages"][i]) for i in topk_idx)

    recall = {f"recall@{k}": round(float(np.mean(v)), 4) for k, v in recall_at.items()}
    token_reduction = round(100 * (1 - sent_tokens / full_tokens), 2)

    # ColBERTv2 residual compression on the pooled passage embeddings
    allemb = np.concatenate(pooled, 0).astype(np.float32)
    comp = ResidualCompressor(n_centroids=max(16, len(allemb) // 32), nbits=2).fit(allemb)
    code = comp.encode(allemb)
    comp_ratio = round(ResidualCompressor.full_nbytes(allemb) / code.nbytes(), 2)

    # recall retention: retrieve using decompressed per-question embeddings
    qenc = enc
    retained, baseline = [], []
    cursor = 0
    for emb, gold, q in per_q_emb:
        m = len(emb)
        dec = comp.decode(code)[cursor:cursor + m]
        cursor += m
        qv = np.asarray(qenc.encode([q]), dtype=np.float32)[0]
        for store, mat in ((baseline, emb), (retained, dec)):
            sc = mat @ qv
            top = np.argsort(-sc)[:5]
            store.append(len(set(top.tolist()) & gold) / len(gold))
    recall_retention = round(float(np.mean(retained)) / max(1e-9, float(np.mean(baseline))), 4)

    results = {
        "n_examples": len(examples),
        **recall,
        "token_reduction_pct@5": token_reduction,
        "colbertv2_compression_ratio": comp_ratio,
        "recall_retention_compressed": recall_retention,
    }
    checks = {
        "recall@5>=0.70": recall["recall@5"] >= 0.70,
        "token_reduction>=40%": token_reduction >= 40.0,
        "compression_ratio>=3x": comp_ratio >= 3.0,
        "recall_retention>=0.95": recall_retention >= 0.95,
    }
    passed = all(checks.values())
    report = {"lever": 4, "results": results, "checks": checks, "passed": passed}
    with open(os.path.join(os.path.dirname(__file__), "results_lever4.json"), "w") as f:
        json.dump(report, f, indent=2)

    print("=== Lever 4 — retrieval (DPR + ColBERTv2) ===")
    print(json.dumps(results, indent=2))
    print("checks:", json.dumps(checks, indent=2))
    print("PASSED" if passed else "FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
