"""End-to-end accuracy + token savings on a REAL benchmark with a REAL model.

Dataset : HotpotQA (distractor) — real multi-hop QA (real Wikipedia passages + human
          questions). NOT synthetic. This is the realistic context-reduction workload.
Models  : deepseek-chat OR gpt-4o-mini (pick via arg). Real API calls, real usage tokens.
Scoring : official HotpotQA / SQuAD Exact-Match and F1 (verbatim; do not modify).

Conditions per question (same model, same prompt, only the context differs):
  (A) FULL         : all 10 passages
  (B) FIXED k=5    : top-5 DPR
  (C) FIXED k=8    : top-8 DPR
  (D) ADAPTIVE     : adaptive-k + ColBERTv2 MaxSim rerank

Run:  python benchmarks/levers/bench_e2e_providers.py 50 deepseek
      python benchmarks/levers/bench_e2e_providers.py 50 openai
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
    AdaptiveRetrievalConfig, DenseRetriever, fetch_adaptive,
)


# --- official scoring ------------------------------------------------------ #
def normalize_answer(s):
    s = s.lower()
    s = "".join(ch for ch in s if ch not in set(string.punctuation))
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    return " ".join(s.split())


def exact_match(p, g):
    return float(normalize_answer(p) == normalize_answer(g))


def f1_score(p, g):
    pt, gt = normalize_answer(p).split(), normalize_answer(g).split()
    common = Counter(pt) & Counter(gt)
    same = sum(common.values())
    if same == 0:
        return 0.0
    prec, rec = same / len(pt), same / len(gt)
    return 2 * prec * rec / (prec + rec)


# --- provider config ------------------------------------------------------- #
def _key(name):
    for line in open(os.path.join(ROOT, ".env.local")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"no {name} in .env.local")


PROVIDERS = {
    "deepseek": {"url": "https://api.deepseek.com/v1/chat/completions",
                 "model": "deepseek-chat", "key_name": "Deepseek_api_key"},
    "openai":   {"url": "https://api.openai.com/v1/chat/completions",
                 "model": "gpt-4o-mini", "key_name": "OPENAI_API_KEY"},
}


def ask(provider, context, question, retries=3):
    cfg = PROVIDERS[provider]
    prompt = (
        "Answer the question using ONLY the context. Reply with the shortest exact answer "
        "(a name, entity, yes/no, or number). No explanation.\n\n"
        f"Context:\n{context}\n\nQuestion: {question}\nAnswer:"
    )
    body = {"model": cfg["model"], "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 32, "temperature": 0.0}
    req = urllib.request.Request(cfg["url"], data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {_key(cfg['key_name'])}",
                                          "Content-Type": "application/json"})
    for attempt in range(retries):
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=90).read())
            u = d.get("usage", {})
            cached = u.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            return d["choices"][0]["message"]["content"].strip(), int(u.get("prompt_tokens", 0)), int(cached)
        except Exception:
            if attempt == retries - 1:
                return "", 0, 0
            time.sleep(2 * (attempt + 1))


def load_hotpot(n):
    from datasets import load_dataset
    ds = load_dataset("hotpot_qa", "distractor", split="validation")
    out = []
    for i in range(min(n, len(ds))):
        ex = ds[i]
        passages = [t + ". " + " ".join(s)
                    for t, s in zip(ex["context"]["title"], ex["context"]["sentences"])]
        out.append({"q": ex["question"], "answer": ex["answer"], "passages": passages})
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    provider = sys.argv[2] if len(sys.argv) > 2 else "deepseek"
    assert provider in PROVIDERS

    from sentence_transformers import SentenceTransformer
    st = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    class Enc:
        def encode(self, texts, normalize_embeddings=True):
            return st.encode(texts, normalize_embeddings=normalize_embeddings,
                            show_progress_bar=False, batch_size=64)

    enc = Enc()
    data = load_hotpot(n)
    C = {c: {"em": [], "f1": [], "ptok": 0, "cached": 0, "k": []}
         for c in ("full", "k5", "k8", "adaptive")}

    for idx, ex in enumerate(data):
        full_ctx = "\n\n".join(ex["passages"])
        a, pt, ca = ask(provider, full_ctx, ex["q"])
        C["full"]["em"].append(exact_match(a, ex["answer"])); C["full"]["f1"].append(f1_score(a, ex["answer"]))
        C["full"]["ptok"] += pt; C["full"]["cached"] += ca

        r = DenseRetriever(enc); r.index(ex["passages"])
        for cond, k in (("k5", 5), ("k8", 8)):
            ctx = "\n\n".join(h[1] for h in r.retrieve(ex["q"], k=k))
            a, pt, ca = ask(provider, ctx, ex["q"])
            C[cond]["em"].append(exact_match(a, ex["answer"])); C[cond]["f1"].append(f1_score(a, ex["answer"]))
            C[cond]["ptok"] += pt; C[cond]["cached"] += ca

        cfg = AdaptiveRetrievalConfig(max_k=10, use_maxsim_rerank=True)
        chunks, meta = fetch_adaptive(r, ex["q"], ex["passages"], encoder=enc, cfg=cfg)
        a, pt, ca = ask(provider, "\n\n".join(chunks), ex["q"])
        C["adaptive"]["em"].append(exact_match(a, ex["answer"])); C["adaptive"]["f1"].append(f1_score(a, ex["answer"]))
        C["adaptive"]["ptok"] += pt; C["adaptive"]["cached"] += ca
        C["adaptive"]["k"].append(meta.get("k_chosen", len(ex["passages"])))

        if (idx + 1) % 10 == 0:
            print(f"  ...{idx+1}/{len(data)} done")

    def m(x):
        return round(100 * sum(x) / len(x), 2) if x else 0.0

    full_pt = C["full"]["ptok"]
    res = {"dataset": "hotpot_qa/distractor (validation)", "model": PROVIDERS[provider]["model"],
           "n_questions": len(data), "full_context": {
               "EM": m(C["full"]["em"]), "F1": m(C["full"]["f1"]), "prompt_tokens": full_pt,
               "cached_tokens": C["full"]["cached"]}}
    keymap = {"k5": "fixed_k5", "k8": "fixed_k8", "adaptive": "adaptive_maxsim"}
    for cond in ("k5", "k8", "adaptive"):
        d = {"EM": m(C[cond]["em"]), "F1": m(C[cond]["f1"]), "prompt_tokens": C[cond]["ptok"],
             "cached_tokens": C[cond]["cached"],
             "EM_delta": round(m(C[cond]["em"]) - m(C["full"]["em"]), 2),
             "F1_delta": round(m(C[cond]["f1"]) - m(C["full"]["f1"]), 2),
             "token_reduction_pct": round(100 * (1 - C[cond]["ptok"] / max(1, full_pt)), 2)}
        if cond == "adaptive":
            d["avg_k_chosen"] = round(sum(C["adaptive"]["k"]) / len(C["adaptive"]["k"]), 2)
        res[keymap[cond]] = d

    out = os.path.join(os.path.dirname(__file__), f"results_e2e_{provider}_n{n}.json")
    with open(out, "w") as f:
        json.dump(res, f, indent=2)
    print(f"\n=== {provider} n={n} ===")
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
