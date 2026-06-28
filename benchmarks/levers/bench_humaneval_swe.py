"""SWE benchmark — HumanEval (real) with retrieval of in-context code demos.

Dataset : OpenAI HumanEval (164 real Python problems w/ unit tests, cached).
Models  : deepseek-chat OR gpt-4o-mini.
Metric  : pass@1 — generated code is EXECUTED against the real test cases (official metric).

Lever under test: retrieval of in-context demonstrations. A pool of real solved problems is
the "context". We compare:
  (A) FULL few-shot : ALL pool demos in the prompt (expensive)
  (B) ADAPTIVE      : adaptively-retrieved relevant demos (cheap, Lever 4)
  (C) ZERO-SHOT     : just the target function (reference)
and measure pass@1 + real prompt-token usage. Tests: can retrieval cut demo tokens without
losing code correctness?

Run:  python benchmarks/levers/bench_humaneval_swe.py 50 deepseek
"""

import json
import os
import re
import subprocess
import sys
import tempfile
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
            "max_tokens": 700, "temperature": 0.0}
    req = urllib.request.Request(cfg["url"], data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {_key(cfg['key_name'])}",
                                          "Content-Type": "application/json"})
    for attempt in range(retries):
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=120).read())
            u = d.get("usage", {})
            return (d["choices"][0]["message"]["content"],
                    int(u.get("prompt_tokens", 0)),
                    int(u.get("prompt_tokens_details", {}).get("cached_tokens", 0)))
        except Exception:
            if attempt == retries - 1:
                return "", 0, 0
            time.sleep(2 * (attempt + 1))


def extract_code(text, entry_point):
    """Pull the function body/def from a model reply (handles ```python fences)."""
    if "```" in text:
        blocks = re.findall(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
        for b in blocks:
            if f"def {entry_point}" in b:
                return b
        if blocks:
            return blocks[0]
    return text


def passes(prompt, completion, test, entry_point, timeout=10):
    """Execute prompt+completion+test in a subprocess; return True iff tests pass."""
    code = extract_code(completion, entry_point)
    if f"def {entry_point}" in code:
        program = code + "\n\n" + test + f"\n\ncheck({entry_point})\n"
    else:  # model returned only the body — append to the prompt signature
        program = prompt + code + "\n\n" + test + f"\n\ncheck({entry_point})\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(program)
        path = f.name
    try:
        r = subprocess.run([sys.executable, path], capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    provider = sys.argv[2] if len(sys.argv) > 2 else "deepseek"
    from datasets import load_dataset
    ds = load_dataset("openai_humaneval", split="test")

    test = [ds[i] for i in range(min(n, len(ds)))]
    pool = [ds[i] for i in range(n, min(n + 12, len(ds)))]
    demo_texts = [f"# Example:\n{d['prompt']}{d['canonical_solution']}" for d in pool]

    from sentence_transformers import SentenceTransformer
    st = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    class Enc:
        def encode(self, texts, normalize_embeddings=True):
            return st.encode(texts, normalize_embeddings=normalize_embeddings,
                            show_progress_bar=False, batch_size=64)

    enc = Enc()
    instr = "Complete the Python function. Return ONLY the full function definition.\n\n"

    C = {c: {"pass": [], "ptok": 0, "cached": 0, "k": []} for c in ("full", "adaptive", "zeroshot")}

    for idx, ex in enumerate(test):
        target = ex["prompt"]
        # FULL few-shot
        a, pt, ca = ask(provider, instr + "\n\n".join(demo_texts) + "\n\n# Now complete:\n" + target)
        C["full"]["pass"].append(float(passes(ex["prompt"], a, ex["test"], ex["entry_point"])))
        C["full"]["ptok"] += pt; C["full"]["cached"] += ca

        # ADAPTIVE retrieved demos
        r = DenseRetriever(enc); r.index(demo_texts, ids=list(range(len(demo_texts))))
        cfg = AdaptiveRetrievalConfig(max_k=min(6, len(demo_texts)), use_maxsim_rerank=True)
        chunks, meta = fetch_adaptive(r, target, demo_texts, encoder=enc, cfg=cfg)
        a, pt, ca = ask(provider, instr + "\n\n".join(chunks) + "\n\n# Now complete:\n" + target)
        C["adaptive"]["pass"].append(float(passes(ex["prompt"], a, ex["test"], ex["entry_point"])))
        C["adaptive"]["ptok"] += pt; C["adaptive"]["cached"] += ca
        C["adaptive"]["k"].append(meta.get("k_chosen", len(demo_texts)))

        # ZERO-SHOT
        a, pt, ca = ask(provider, instr + target)
        C["zeroshot"]["pass"].append(float(passes(ex["prompt"], a, ex["test"], ex["entry_point"])))
        C["zeroshot"]["ptok"] += pt

        if (idx + 1) % 10 == 0:
            print(f"  ...{idx+1}/{len(test)}")

    def m(x):
        return round(100 * sum(x) / len(x), 2) if x else 0.0

    fp = C["full"]["ptok"]
    res = {"benchmark": "HumanEval", "task": "swe", "metric": "pass@1 (executed)",
           "model": PROVIDERS[provider]["model"], "n_problems": len(test), "n_demos_pool": len(demo_texts),
           "full_fewshot": {"pass@1": m(C["full"]["pass"]), "prompt_tokens": fp, "cached_tokens": C["full"]["cached"]},
           "adaptive_retrieved_demos": {
               "pass@1": m(C["adaptive"]["pass"]), "prompt_tokens": C["adaptive"]["ptok"],
               "cached_tokens": C["adaptive"]["cached"],
               "avg_k_chosen": round(sum(C["adaptive"]["k"]) / len(C["adaptive"]["k"]), 2),
               "pass_delta_vs_full": round(m(C["adaptive"]["pass"]) - m(C["full"]["pass"]), 2),
               "token_reduction_pct": round(100 * (1 - C["adaptive"]["ptok"] / max(1, fp)), 2)},
           "zeroshot": {"pass@1": m(C["zeroshot"]["pass"]), "prompt_tokens": C["zeroshot"]["ptok"]}}

    out = os.path.join(os.path.dirname(__file__), f"results_humaneval_swe_{provider}_n{n}.json")
    with open(out, "w") as f:
        json.dump(res, f, indent=2)
    print(f"\n=== HumanEval SWE {provider} n={n} ===")
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
