"""End-to-end accuracy with adaptive + MaxSim reranking vs fixed k baselines.

Dataset : HotpotQA (distractor), the standard multi-hop QA benchmark (cached locally).
Model   : DeepSeek (deepseek-chat) via API — the only real generative model available here.
Scoring : official HotpotQA / SQuAD Exact-Match and F1.

What it measures: does adaptive + MaxSim preserve answer accuracy while cutting more
tokens than fixed k=5 and fewer tokens than fixed k=8? For each question we ask DeepSeek:
  (A) FULL context      : all 10 passages
  (B) FIXED k=5         : top-5 DPR passages
  (C) FIXED k=8         : top-8 DPR passages
  (D) ADAPTIVE + MaxSim : adaptive-k with late-interaction reranking
and compare EM/F1 against the gold answer, plus real token usage.

NO fabricated data, NO self-made benchmark. Run:
    python benchmarks/levers/bench_e2e_adaptive.py 8   # n_questions=8 (DeepSeek cost)
"""

import json
import os
import re
import string
import sys
import time
import urllib.request
from collections import Counter

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from token_efficiency_model.lossless.retrieval import (
    AdaptiveRetrievalConfig,
    DenseRetriever,
    fetch_adaptive,
)


# --- official HotpotQA / SQuAD scoring (Rajpurkar et al.; do not modify) ---- #
def normalize_answer(s):
    def remove_articles(text):
        return re.sub(r"\b(a|an|the)\b", " ", text)

    def white_space_fix(text):
        return " ".join(text.split())

    def remove_punc(text):
        return "".join(ch for ch in text if ch not in set(string.punctuation))

    def lower(text):
        return text.lower()

    return white_space_fix(remove_articles(remove_punc(lower(s))))


def exact_match(pred, gold):
    return float(normalize_answer(pred) == normalize_answer(gold))


def f1_score(pred, gold):
    pt = normalize_answer(pred).split()
    gt = normalize_answer(gold).split()
    common = Counter(pt) & Counter(gt)
    same = sum(common.values())
    if same == 0:
        return 0.0
    p = same / len(pt)
    r = same / len(gt)
    return 2 * p * r / (p + r)


# --- DeepSeek -------------------------------------------------------------- #
def _key():
    for line in open(os.path.join(ROOT, ".env.local")):
        if line.startswith("Deepseek_api_key="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no DeepSeek key")


def ask_deepseek(context, question, key, retries=3):
    prompt = (
        "Answer the question using ONLY the context. Reply with the shortest exact answer "
        "(a name, entity, yes/no, or number). No explanation.\n\n"
        f"Context:\n{context}\n\nQuestion: {question}\nAnswer:"
    )
    body = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 32,
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    for attempt in range(retries):
        try:
            r = urllib.request.urlopen(req, timeout=60)
            d = json.loads(r.read())
            return d["choices"][0]["message"]["content"].strip(), d.get("usage", {})
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def load_hotpot(n):
    from datasets import load_dataset

    ds = load_dataset("hotpot_qa", "distractor", split="validation")
    out = []
    for i in range(min(n, len(ds))):
        ex = ds[i]
        titles = ex["context"]["title"]
        sents = ex["context"]["sentences"]
        passages = [t + ". " + " ".join(s) for t, s in zip(titles, sents)]
        out.append({"q": ex["question"], "answer": ex["answer"], "passages": passages})
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    key = _key()

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    class Enc:
        def encode(self, texts, normalize_embeddings=True):
            return model.encode(texts, normalize_embeddings=normalize_embeddings,
                                show_progress_bar=False, batch_size=64)

    enc = Enc()
    data = load_hotpot(n)

    full = {"em": [], "f1": [], "ptok": 0}
    fixed_k5 = {"em": [], "f1": [], "ptok": 0, "cached": 0}
    fixed_k8 = {"em": [], "f1": [], "ptok": 0, "cached": 0}
    adaptive = {"em": [], "f1": [], "ptok": 0, "cached": 0, "avg_k": []}

    for idx, ex in enumerate(data):
        print(f"\n[{idx+1}/{len(data)}] {ex['q'][:50]}...")

        # (A) full context
        full_ctx = "\n\n".join(ex["passages"])
        ans_full, u_full = ask_deepseek(full_ctx, ex["q"], key)
        full["em"].append(exact_match(ans_full, ex["answer"]))
        full["f1"].append(f1_score(ans_full, ex["answer"]))
        full["ptok"] += int(u_full.get("prompt_tokens", 0))
        print(f"  FULL  : {ans_full[:40]!r} EM={full['em'][-1]:.0f}")

        # (B) fixed k=5
        r = DenseRetriever(enc)
        r.index(ex["passages"])
        hits_k5 = r.retrieve(ex["q"], k=5)
        ctx_k5 = "\n\n".join(h[1] for h in hits_k5)
        ans_k5, u_k5 = ask_deepseek(ctx_k5, ex["q"], key)
        fixed_k5["em"].append(exact_match(ans_k5, ex["answer"]))
        fixed_k5["f1"].append(f1_score(ans_k5, ex["answer"]))
        fixed_k5["ptok"] += int(u_k5.get("prompt_tokens", 0))
        fixed_k5["cached"] += int(u_k5.get("prompt_tokens_details", {}).get("cached_tokens", 0))
        print(f"  K=5   : {ans_k5[:40]!r} EM={fixed_k5['em'][-1]:.0f}")

        # (C) fixed k=8
        hits_k8 = r.retrieve(ex["q"], k=8)
        ctx_k8 = "\n\n".join(h[1] for h in hits_k8)
        ans_k8, u_k8 = ask_deepseek(ctx_k8, ex["q"], key)
        fixed_k8["em"].append(exact_match(ans_k8, ex["answer"]))
        fixed_k8["f1"].append(f1_score(ans_k8, ex["answer"]))
        fixed_k8["ptok"] += int(u_k8.get("prompt_tokens", 0))
        fixed_k8["cached"] += int(u_k8.get("prompt_tokens_details", {}).get("cached_tokens", 0))
        print(f"  K=8   : {ans_k8[:40]!r} EM={fixed_k8['em'][-1]:.0f}")

        # (D) adaptive + MaxSim
        # Elbow method: automatically find optimal k by largest score gap
        cfg = AdaptiveRetrievalConfig(
            max_k=10,
            use_maxsim_rerank=True,
        )
        chunks_adp, meta_adp = fetch_adaptive(r, ex["q"], ex["passages"], encoder=enc, cfg=cfg)
        ctx_adp = "\n\n".join(chunks_adp)
        ans_adp, u_adp = ask_deepseek(ctx_adp, ex["q"], key)
        adaptive["em"].append(exact_match(ans_adp, ex["answer"]))
        adaptive["f1"].append(f1_score(ans_adp, ex["answer"]))
        adaptive["ptok"] += int(u_adp.get("prompt_tokens", 0))
        adaptive["cached"] += int(u_adp.get("prompt_tokens_details", {}).get("cached_tokens", 0))
        adaptive["avg_k"].append(meta_adp.get("k_chosen", len(ex["passages"])))
        print(f"  ADAPT : {ans_adp[:40]!r} EM={adaptive['em'][-1]:.0f} k={meta_adp.get('k_chosen', 'full')}")

    def mean(x):
        return round(100 * sum(x) / len(x), 2) if x else 0.0

    results = {
        "dataset": "hotpot_qa/distractor (validation)",
        "model": "deepseek-chat",
        "n_questions": len(data),
        "full_context": {
            "EM": mean(full["em"]),
            "F1": mean(full["f1"]),
            "prompt_tokens": full["ptok"],
        },
        "fixed_k5": {
            "EM": mean(fixed_k5["em"]),
            "F1": mean(fixed_k5["f1"]),
            "prompt_tokens": fixed_k5["ptok"],
            "cached_tokens": fixed_k5["cached"],
            "EM_delta": round(mean(fixed_k5["em"]) - mean(full["em"]), 2),
            "token_reduction_pct": round(100 * (1 - fixed_k5["ptok"] / max(1, full["ptok"])), 2),
        },
        "fixed_k8": {
            "EM": mean(fixed_k8["em"]),
            "F1": mean(fixed_k8["f1"]),
            "prompt_tokens": fixed_k8["ptok"],
            "cached_tokens": fixed_k8["cached"],
            "EM_delta": round(mean(fixed_k8["em"]) - mean(full["em"]), 2),
            "token_reduction_pct": round(100 * (1 - fixed_k8["ptok"] / max(1, full["ptok"])), 2),
        },
        "adaptive_maxsim": {
            "EM": mean(adaptive["em"]),
            "F1": mean(adaptive["f1"]),
            "prompt_tokens": adaptive["ptok"],
            "cached_tokens": adaptive["cached"],
            "avg_k_chosen": round(sum(adaptive["avg_k"]) / len(adaptive["avg_k"]), 2),
            "EM_delta": round(mean(adaptive["em"]) - mean(full["em"]), 2),
            "token_reduction_pct": round(100 * (1 - adaptive["ptok"] / max(1, full["ptok"])), 2),
        },
    }

    with open(os.path.join(os.path.dirname(__file__), "results_e2e_adaptive.json"), "w") as f:
        json.dump(results, f, indent=2)

    print("\n\n=== E2E Accuracy with Adaptive + MaxSim (HotpotQA + DeepSeek) ===")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
