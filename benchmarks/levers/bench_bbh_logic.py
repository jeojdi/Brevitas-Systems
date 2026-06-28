"""LOGIC benchmark — BBH logical_deduction (real) with retrieval of in-context demos.

Dataset : BIG-Bench Hard `logical_deduction_five_objects` (250 real logic puzzles, cached).
Models  : deepseek-chat OR gpt-4o-mini.
Metric  : exact-match of the chosen option (A)-(E) vs gold target. Official BBH style.

Lever under test: retrieval of in-context demonstrations (Liu et al. 2022, "What Makes Good
In-Context Examples for GPT-3"). A pool of real solved puzzles is the "context". We compare:
  (A) FULL few-shot : ALL pool demos in the prompt (expensive)
  (B) ADAPTIVE      : adaptively-retrieved relevant demos (cheap, Lever 4)
  (C) ZERO-SHOT     : no demos (reference floor)
and measure accuracy + real prompt-token usage. Tests: can retrieval cut demo tokens
without losing logic accuracy?

Run:  python benchmarks/levers/bench_bbh_logic.py 50 deepseek
"""

import json
import os
import re
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

PROVIDERS = {
    "deepseek": {"url": "https://api.deepseek.com/v1/chat/completions",
                 "model": "deepseek-chat", "key_name": "Deepseek_api_key"},
    "openai":   {"url": "https://api.openai.com/v1/chat/completions",
                 "model": "gpt-4o-mini", "key_name": "OPENAI_API_KEY"},
}


def _key(name):
    for line in open(os.path.join(ROOT, ".env.local")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"no {name}")


def ask(provider, prompt, retries=3):
    cfg = PROVIDERS[provider]
    body = {"model": cfg["model"], "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400, "temperature": 0.0}
    req = urllib.request.Request(cfg["url"], data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {_key(cfg['key_name'])}",
                                          "Content-Type": "application/json"})
    for attempt in range(retries):
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=90).read())
            u = d.get("usage", {})
            return (d["choices"][0]["message"]["content"].strip(),
                    int(u.get("prompt_tokens", 0)),
                    int(u.get("prompt_tokens_details", {}).get("cached_tokens", 0)))
        except Exception:
            if attempt == retries - 1:
                return "", 0, 0
            time.sleep(2 * (attempt + 1))


def extract_option(text):
    m = re.findall(r"\(([A-E])\)", text)
    return f"({m[-1]})" if m else ""


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    provider = sys.argv[2] if len(sys.argv) > 2 else "deepseek"
    from datasets import load_dataset
    ds = load_dataset("lukaemon/bbh", "logical_deduction_five_objects", split="test")

    test = [ds[i] for i in range(min(n, len(ds)))]
    pool = [ds[i] for i in range(n, min(n + 16, len(ds)))]   # 16 real demos, disjoint from test
    demo_texts = [f"Question: {d['input']}\nAnswer: The answer is {d['target']}." for d in pool]
    demo_q = [d["input"] for d in pool]

    from sentence_transformers import SentenceTransformer
    st = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    class Enc:
        def encode(self, texts, normalize_embeddings=True):
            return st.encode(texts, normalize_embeddings=normalize_embeddings,
                            show_progress_bar=False, batch_size=64)

    enc = Enc()
    instr = ("Solve the logic puzzle. End your reply with 'The answer is (X)' where X is the "
             "correct option letter.\n\n")

    C = {c: {"em": [], "ptok": 0, "cached": 0, "k": []} for c in ("full", "adaptive", "zeroshot")}

    for idx, ex in enumerate(test):
        q = f"Question: {ex['input']}\nAnswer:"
        gold = ex["target"]

        # FULL few-shot
        a, pt, ca = ask(provider, instr + "\n\n".join(demo_texts) + "\n\n" + q)
        C["full"]["em"].append(float(extract_option(a) == gold)); C["full"]["ptok"] += pt; C["full"]["cached"] += ca

        # ADAPTIVE retrieved demos
        r = DenseRetriever(enc); r.index(demo_texts, ids=list(range(len(demo_texts))))
        cfg = AdaptiveRetrievalConfig(max_k=min(8, len(demo_texts)), use_maxsim_rerank=True)
        chunks, meta = fetch_adaptive(r, ex["input"], demo_texts, encoder=enc, cfg=cfg)
        a, pt, ca = ask(provider, instr + "\n\n".join(chunks) + "\n\n" + q)
        C["adaptive"]["em"].append(float(extract_option(a) == gold)); C["adaptive"]["ptok"] += pt
        C["adaptive"]["cached"] += ca; C["adaptive"]["k"].append(meta.get("k_chosen", len(demo_texts)))

        # ZERO-SHOT
        a, pt, ca = ask(provider, instr + q)
        C["zeroshot"]["em"].append(float(extract_option(a) == gold)); C["zeroshot"]["ptok"] += pt

        if (idx + 1) % 10 == 0:
            print(f"  ...{idx+1}/{len(test)}")

    def m(x):
        return round(100 * sum(x) / len(x), 2) if x else 0.0

    fp = C["full"]["ptok"]
    res = {"benchmark": "BBH logical_deduction_five_objects", "task": "logic",
           "model": PROVIDERS[provider]["model"], "n_questions": len(test), "n_demos_pool": len(demo_texts),
           "full_fewshot": {"accuracy": m(C["full"]["em"]), "prompt_tokens": fp, "cached_tokens": C["full"]["cached"]},
           "adaptive_retrieved_demos": {
               "accuracy": m(C["adaptive"]["em"]), "prompt_tokens": C["adaptive"]["ptok"],
               "cached_tokens": C["adaptive"]["cached"],
               "avg_k_chosen": round(sum(C["adaptive"]["k"]) / len(C["adaptive"]["k"]), 2),
               "acc_delta_vs_full": round(m(C["adaptive"]["em"]) - m(C["full"]["em"]), 2),
               "token_reduction_pct": round(100 * (1 - C["adaptive"]["ptok"] / max(1, fp)), 2)},
           "zeroshot": {"accuracy": m(C["zeroshot"]["em"]), "prompt_tokens": C["zeroshot"]["ptok"]}}

    out = os.path.join(os.path.dirname(__file__), f"results_bbh_logic_{provider}_n{n}.json")
    with open(out, "w") as f:
        json.dump(res, f, indent=2)
    print(f"\n=== BBH logic {provider} n={n} ===")
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
