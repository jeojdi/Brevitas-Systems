"""End-to-end RLM benchmark — REAL model on a REAL public benchmark.

Compares three conditions on HotpotQA (distractor), scored with official EM/F1:
  (A) FULL      : all 10 passages in one prompt (baseline accuracy)
  (B) RETRIEVAL : top-k passages (Lever 4) in one prompt
  (C) RLM       : all 10 passages held as the REPL variable P; the model writes code to
                  inspect/slice P and calls sub_llm on slices (Lever 5, arXiv:2512.24601).

Tracks REAL total prompt tokens per condition (RLM sums tokens across all its model calls).
Model: OpenAI or DeepSeek (--model). NO fabricated data; NO self-made benchmark/metric.

Run:  python benchmarks/levers/bench_rlm_e2e.py 15 openai
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
from token_efficiency_model.lossless.rlm import RLM


# --- official HotpotQA/SQuAD scoring --------------------------------------- #
def normalize_answer(s):
    s = s.lower()
    s = "".join(ch for ch in s if ch not in set(string.punctuation))
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    return " ".join(s.split())


def em(p, g):
    return float(normalize_answer(p) == normalize_answer(g))


def f1(p, g):
    pt, gt = normalize_answer(p).split(), normalize_answer(g).split()
    common = Counter(pt) & Counter(gt)
    same = sum(common.values())
    if same == 0:
        return 0.0
    prec, rec = same / len(pt), same / len(gt)
    return 2 * prec * rec / (prec + rec)


# --- real model callers (track tokens) ------------------------------------- #
def _key(name):
    for line in open(os.path.join(ROOT, ".env.local")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"no {name}")


class ModelCaller:
    def __init__(self, provider):
        self.provider = provider
        if provider == "openai":
            self.url = "https://api.openai.com/v1/chat/completions"
            self.model = "gpt-4o-mini"
            self.key = _key("OPENAI_API_KEY")
        else:
            self.url = "https://api.deepseek.com/v1/chat/completions"
            self.model = "deepseek-chat"
            self.key = _key("Deepseek_api_key")
        self.prompt_tokens = 0
        self.calls = 0

    def __call__(self, prompt, max_tokens=400):
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "temperature": 0.0}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.key}",
                                              "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                r = urllib.request.urlopen(req, timeout=90)
                d = json.loads(r.read())
                self.prompt_tokens += int(d.get("usage", {}).get("prompt_tokens", 0))
                self.calls += 1
                return d["choices"][0]["message"]["content"].strip()
            except Exception:
                if attempt == 2:
                    return ""
                time.sleep(2 * (attempt + 1))


def short_answer(model, context, question):
    return model(
        "Answer using ONLY the context. Reply with the shortest exact answer "
        "(name/entity/yes-no/number), no explanation.\n\n"
        f"Context:\n{context}\n\nQuestion: {question}\nAnswer:", max_tokens=32)


def load_hotpot(n):
    from datasets import load_dataset
    ds = load_dataset("hotpot_qa", "distractor", split="validation")
    out = []
    for i in range(min(n, len(ds))):
        ex = ds[i]
        titles, sents = ex["context"]["title"], ex["context"]["sentences"]
        passages = [f"[{j}] {t}. " + " ".join(s) for j, (t, s) in enumerate(zip(titles, sents))]
        out.append({"q": ex["question"], "answer": ex["answer"], "passages": passages})
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    provider = sys.argv[2] if len(sys.argv) > 2 else "openai"
    data = load_hotpot(n)

    from sentence_transformers import SentenceTransformer
    st = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    class Enc:
        def encode(self, texts, normalize_embeddings=True):
            return st.encode(texts, normalize_embeddings=normalize_embeddings,
                            show_progress_bar=False, batch_size=64)

    enc = Enc()
    conds = {c: {"em": [], "f1": [], "model": ModelCaller(provider)} for c in ("full", "retrieval", "rlm")}

    for idx, ex in enumerate(data):
        P = "\n\n".join(ex["passages"])
        # A) full
        a = short_answer(conds["full"]["model"], P, ex["q"])
        # B) retrieval k=5
        r = DenseRetriever(enc); r.index(ex["passages"])
        rc = "\n\n".join(h[1] for h in r.retrieve(ex["q"], k=5))
        b = short_answer(conds["retrieval"]["model"], rc, ex["q"])
        # C) RLM over P
        rlm = RLM(conds["rlm"]["model"], max_iters=4)
        c = rlm.run(P, ex["q"]).answer
        for cond, ans in (("full", a), ("retrieval", b), ("rlm", c)):
            conds[cond]["em"].append(em(ans, ex["answer"]))
            conds[cond]["f1"].append(f1(ans, ex["answer"]))
        print(f"[{idx+1}/{len(data)}] gold={ex['answer'][:25]!r} "
              f"full={a[:20]!r} retr={b[:20]!r} rlm={c[:20]!r}")

    def m(x):
        return round(100 * sum(x) / len(x), 2) if x else 0.0

    results = {"dataset": "hotpot_qa/distractor", "model": conds["full"]["model"].model,
               "n": len(data)}
    for c in ("full", "retrieval", "rlm"):
        results[c] = {"EM": m(conds[c]["em"]), "F1": m(conds[c]["f1"]),
                      "prompt_tokens": conds[c]["model"].prompt_tokens,
                      "model_calls": conds[c]["model"].calls}
    with open(os.path.join(os.path.dirname(__file__), "results_rlm_e2e.json"), "w") as f:
        json.dump(results, f, indent=2)
    print("\n=== RLM e2e (HotpotQA + real model) ===")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
