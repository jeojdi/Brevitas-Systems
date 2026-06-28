"""End-to-end accuracy benchmark — REAL model on a REAL public benchmark.

Dataset : HotpotQA (distractor), the standard multi-hop QA benchmark (cached locally).
Model   : DeepSeek (deepseek-chat) via API — the only real generative model available here.
Scoring : official HotpotQA / SQuAD Exact-Match and F1 (normalize_answer below is the
          canonical implementation; NOT a custom metric).

What it measures (accuracy-first thesis): does Lever 4 retrieval preserve answer accuracy
while cutting prompt tokens? For each question we ask DeepSeek twice:
  (A) FULL context  : all 10 passages
  (B) LOSSLESS       : only the top-k passages from the DPR retriever (Lever 4)
and compare EM/F1 against the gold answer, plus real token usage (incl. DeepSeek cache hits).

NO fabricated data, NO self-made benchmark. Run:
    python benchmarks/levers/bench_e2e_accuracy.py 30 5      # n_questions=30, top_k=5
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

from token_efficiency_model.lossless.retrieval import DenseRetriever


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
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    top_k = int(sys.argv[2]) if len(sys.argv) > 2 else 5
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
    loss = {"em": [], "f1": [], "ptok": 0, "cached": 0}

    for idx, ex in enumerate(data):
        # (A) full context
        full_ctx = "\n\n".join(ex["passages"])
        ans_full, u_full = ask_deepseek(full_ctx, ex["q"], key)
        full["em"].append(exact_match(ans_full, ex["answer"]))
        full["f1"].append(f1_score(ans_full, ex["answer"]))
        full["ptok"] += int(u_full.get("prompt_tokens", 0))

        # (B) lossless retrieval (Lever 4)
        r = DenseRetriever(enc)
        r.index(ex["passages"])
        hits = r.retrieve(ex["q"], k=top_k)
        loss_ctx = "\n\n".join(h[1] for h in hits)
        ans_loss, u_loss = ask_deepseek(loss_ctx, ex["q"], key)
        loss["em"].append(exact_match(ans_loss, ex["answer"]))
        loss["f1"].append(f1_score(ans_loss, ex["answer"]))
        loss["ptok"] += int(u_loss.get("prompt_tokens", 0))
        loss["cached"] += int(u_loss.get("prompt_tokens_details", {}).get("cached_tokens", 0))

        print(f"[{idx+1}/{len(data)}] gold={ex['answer'][:30]!r} "
              f"full={ans_full[:30]!r}(EM{full['em'][-1]:.0f}) "
              f"loss={ans_loss[:30]!r}(EM{loss['em'][-1]:.0f})")

    def mean(x):
        return round(100 * sum(x) / len(x), 2) if x else 0.0

    results = {
        "dataset": "hotpot_qa/distractor (validation)",
        "model": "deepseek-chat",
        "n_questions": len(data),
        "top_k": top_k,
        "full_context": {"EM": mean(full["em"]), "F1": mean(full["f1"]), "prompt_tokens": full["ptok"]},
        "lossless_retrieval": {"EM": mean(loss["em"]), "F1": mean(loss["f1"]),
                               "prompt_tokens": loss["ptok"], "cached_tokens": loss["cached"]},
        "token_reduction_pct": round(100 * (1 - loss["ptok"] / max(1, full["ptok"])), 2),
        "EM_delta": round(mean(loss["em"]) - mean(full["em"]), 2),
        "F1_delta": round(mean(loss["f1"]) - mean(full["f1"]), 2),
    }
    with open(os.path.join(os.path.dirname(__file__), "results_e2e_accuracy.json"), "w") as f:
        json.dump(results, f, indent=2)
    print("\n=== E2E accuracy (HotpotQA + DeepSeek) ===")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
