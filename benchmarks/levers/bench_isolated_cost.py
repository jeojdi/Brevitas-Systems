"""Isolated per-condition COST test — removes within-question cache contamination.

Each condition is run as its OWN full sequence over N questions (so provider prefix-cache
reflects only realistic CROSS-call repetition, not leakage from other conditions on the same
question). Reports real input COST using provider cache pricing.

Two workload patterns are simulated to answer "is it cheaper to drop in?":
  * UNIQUE  : each call sends a different context (HotpotQA passages) — retrieval's best case.
  * SHARED  : a fixed large context (system+KB) is reused every call, only the question varies
              — the typical agent/chatbot pattern, caching's best case.

Conditions: FULL (all passages) vs ADAPTIVE (retrieved). Real cost from real usage.

Run:  python benchmarks/levers/bench_isolated_cost.py 30 deepseek
"""

import json
import os
import sys
import time
import urllib.request

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from token_efficiency_model.lossless.retrieval import (
    AdaptiveRetrievalConfig, DenseRetriever, fetch_adaptive,
)

# input-side pricing (USD per 1M tokens): (fresh, cached)
PRICE = {
    "deepseek-chat": {"url": "https://api.deepseek.com/v1/chat/completions",
                      "key": "Deepseek_api_key", "fresh": 0.27, "cached": 0.027},
    "gpt-4o-mini":   {"url": "https://api.openai.com/v1/chat/completions",
                      "key": "OPENAI_API_KEY", "fresh": 0.15, "cached": 0.075},
}
MODEL = {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini"}


def _key(name):
    for line in open(os.path.join(ROOT, ".env.local")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(name)


def ask(model, prompt, retries=3):
    cfg = PRICE[model]
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 32, "temperature": 0.0}
    req = urllib.request.Request(cfg["url"], data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {_key(cfg['key'])}",
                                          "Content-Type": "application/json"})
    for attempt in range(retries):
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=90).read())
            u = d.get("usage", {})
            return (int(u.get("prompt_tokens", 0)),
                    int(u.get("prompt_tokens_details", {}).get("cached_tokens", 0)))
        except Exception:
            if attempt == retries - 1:
                return 0, 0
            time.sleep(2 * (attempt + 1))


def usd(model, prompt, cached):
    p = PRICE[model]
    return ((prompt - cached) * p["fresh"] + cached * p["cached"]) / 1_000_000


def run_sequence(model, prompts):
    """Run a list of prompts in order; return total real input USD cost."""
    cost = 0.0
    for pr in prompts:
        ptok, ctok = ask(model, pr)
        cost += usd(model, ptok, ctok)
    return cost


def load_hotpot(n):
    from datasets import load_dataset
    ds = load_dataset("hotpot_qa", "distractor", split="validation")
    out = []
    for i in range(min(n, len(ds))):
        ex = ds[i]
        passages = [t + ". " + " ".join(s)
                    for t, s in zip(ex["context"]["title"], ex["context"]["sentences"])]
        out.append({"q": ex["question"], "passages": passages})
    return out


def build(ctx, q):
    return ("Answer using ONLY the context. Shortest exact answer.\n\n"
            f"Context:\n{ctx}\n\nQuestion: {q}\nAnswer:")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    model = MODEL[sys.argv[2] if len(sys.argv) > 2 else "deepseek"]
    data = load_hotpot(n)

    from sentence_transformers import SentenceTransformer
    st = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    class Enc:
        def encode(self, t, normalize_embeddings=True):
            return st.encode(t, normalize_embeddings=normalize_embeddings, show_progress_bar=False)

    enc = Enc()

    # precompute prompts for each condition / pattern
    full_unique, adaptive_unique = [], []
    for ex in data:
        full_unique.append(build("\n\n".join(ex["passages"]), ex["q"]))
        r = DenseRetriever(enc); r.index(ex["passages"])
        cfg = AdaptiveRetrievalConfig(max_k=10, use_maxsim_rerank=True)
        chunks, _ = fetch_adaptive(r, ex["q"], ex["passages"], encoder=enc, cfg=cfg)
        adaptive_unique.append(build("\n\n".join(chunks), ex["q"]))

    # SHARED pattern: one fixed large KB (first question's 10 passages) reused for every call;
    # full = whole KB each time (caches); adaptive = retrieve from KB per question (varies).
    kb_passages = data[0]["passages"] + data[1]["passages"]   # ~20-passage fixed KB
    full_shared, adaptive_shared = [], []
    kb_retriever = DenseRetriever(enc); kb_retriever.index(kb_passages)
    for ex in data:
        full_shared.append(build("\n\n".join(kb_passages), ex["q"]))
        cfg = AdaptiveRetrievalConfig(max_k=10, use_maxsim_rerank=True)
        chunks, _ = fetch_adaptive(kb_retriever, ex["q"], kb_passages, encoder=enc, cfg=cfg)
        adaptive_shared.append(build("\n\n".join(chunks), ex["q"]))

    print(f"running isolated sequences (model={model}, n={n})...")
    res = {"model": model, "n": n, "note": "each condition isolated; cost in USD (input-side)"}
    for pattern, fu, ad in [("UNIQUE_context", full_unique, adaptive_unique),
                            ("SHARED_KB", full_shared, adaptive_shared)]:
        cf = run_sequence(model, fu)
        ca = run_sequence(model, ad)
        res[pattern] = {
            "full_cost_usd": round(cf, 6),
            "adaptive_cost_usd": round(ca, 6),
            "cost_save_pct": round(100 * (1 - ca / cf), 2) if cf > 0 else 0.0,
            "cheaper": "adaptive" if ca < cf else "full",
        }
        print(f"  {pattern}: full=${cf:.5f}  adaptive=${ca:.5f}  -> {res[pattern]['cheaper']} cheaper "
              f"({res[pattern]['cost_save_pct']:+.1f}% for adaptive)")

    out = os.path.join(os.path.dirname(__file__), f"results_isolated_cost_{model}_n{n}.json")
    with open(out, "w") as f:
        json.dump(res, f, indent=2)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
