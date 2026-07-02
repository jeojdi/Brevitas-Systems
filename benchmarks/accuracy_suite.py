"""Tri-provider LOSSLESS-accuracy benchmark suite (Claude / OpenAI / DeepSeek).

Purpose: prove Brevitas is lossless on standardized public benchmarks — accuracy WITH
Brevitas must match accuracy WITHOUT (the caching/routing path sends the model the same
content) — while measuring the real token/dollar savings. Few-shot prompts give the
providers a byte-identical shared exemplar prefix to cache (realistic for benchmarking
and for any templated production traffic).

Benchmarks (all offline in the HF cache):
  * bbh   — BBH logical_deduction_five_objects  (LOGICAL REASONING)
  * mmlu  — MMLU formal_logic                    (LOGICAL REASONING)
  * arc   — ARC-Challenge                        (science reasoning, multiple choice)
  * code  — HumanEval, executed pass@1           (SWE / code-generation proxy)

Arms per question (same model, same content, few-shot prefix identical across Qs):
  BASELINE — raw provider client
  BREVITAS — BrevitasDropIn (auto caching + router; one session per provider×benchmark)

Scored vs GROUND TRUTH (no self-eval). Cost from REAL provider usage incl. cached-token
discounts. Budget-guarded (small N, cheap models). SWE-bench-full (repo+docker execution)
is out of scope for cost/runtime; HumanEval is the executed code proxy.

Usage:
  python benchmarks/accuracy_suite.py --provider deepseek --n 25
  python benchmarks/accuracy_suite.py --provider openai   --n 15 --benchmarks bbh,mmlu,arc,code
  python benchmarks/accuracy_suite.py --provider anthropic --n 15
"""
from __future__ import annotations
import argparse, json, os, re, sys, time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for ln in (ROOT / ".env.local").read_text().splitlines():
    if "=" in ln and not ln.strip().startswith("#"):
        k, _, v = ln.partition("="); os.environ.setdefault(k.strip(), v.strip())

from token_efficiency_model.lossless.dropin import BrevitasDropIn  # noqa: E402
sys.path.insert(0, str(ROOT / "benchmarks" / "levers"))
from bench_humaneval_swe import extract_code, passes  # reuse the executed grader  # noqa: E402

PROV = {
    "deepseek": {"model": "deepseek-chat", "base": "https://api.deepseek.com/v1",
                 "key": "Deepseek_api_key", "in": 0.27, "cached": 0.07, "out": 1.10},
    "openai": {"model": "gpt-4o-mini", "base": "https://api.openai.com/v1",
               "key": "OPENAI_API_KEY", "in": 0.15, "cached": 0.075, "out": 0.60},
    "anthropic": {"model": "claude-haiku-4-5-20251001", "base": "",
                  "key": "ANTHROPIC_API_KEY", "in": 0.80, "cached": 0.08, "out": 4.00},
}


def raw_client(p):
    c = PROV[p]; key = os.environ[c["key"]]
    if p == "anthropic":
        import anthropic; return anthropic.Anthropic(api_key=key)
    import openai; return openai.OpenAI(api_key=key, base_url=c["base"])


def call(p, client, optimized, system, user, sid, max_tokens):
    # temperature=0: deterministic decoding so the lossless proof is CLEAN — identical
    # content in => identical tokens out => identical accuracy. Any accuracy delta at
    # temp>0 would be sampling noise, not Brevitas. (Anthropic omits an explicit temp
    # to keep it at its deterministic default of 0 for haiku with no sampling knobs set.)
    c = PROV[p]
    if optimized:
        resp, _ = client.chat(messages=[{"role": "system", "content": system},
                                        {"role": "user", "content": user}],
                              model=c["model"], session_id=sid, max_tokens=max_tokens,
                              temperature=0)
    elif p == "anthropic":
        resp = client.messages.create(model=c["model"], max_tokens=max_tokens,
                    temperature=0, system=system, messages=[{"role": "user", "content": user}])
    else:
        resp = client.chat.completions.create(model=c["model"], max_tokens=max_tokens,
                    temperature=0, messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}])
    return resp


def read(resp, p):
    if p == "anthropic":
        u = resp.usage
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        fresh, read_, write = u.input_tokens, getattr(u, "cache_read_input_tokens", 0) or 0, \
            getattr(u, "cache_creation_input_tokens", 0) or 0
        out = u.output_tokens
        c = PROV[p]
        usd = (fresh*c["in"] + read_*c["cached"] + write*c["in"]*1.25 + out*c["out"])/1e6
        return text, usd, fresh+read_+write, read_
    u = resp.usage
    text = resp.choices[0].message.content or ""
    cached = (getattr(u, "prompt_tokens_details", None).cached_tokens
              if getattr(u, "prompt_tokens_details", None) else 0) or 0
    c = PROV[p]
    usd = ((u.prompt_tokens-cached)*c["in"] + cached*c["cached"] + u.completion_tokens*c["out"])/1e6
    return text, usd, u.prompt_tokens, cached


# ---------------------------------------------------------------- benchmark specs
def load_bench(name, n):
    from datasets import load_dataset
    if name == "bbh":
        ds = list(load_dataset("lukaemon/bbh", "logical_deduction_five_objects", split="test"))
        pool, test = ds[:5], ds[5:5+n]
        shots = "\n\n".join(f"Question: {d['input']}\nAnswer: {d['target']}" for d in pool)
        sysmsg = ("Solve the logic puzzle. End your reply with 'The answer is (X)'.\n\n"
                  "Worked examples:\n" + shots)
        items = [{"q": f"Question: {d['input']}\nAnswer:", "gold": d["target"]} for d in test]
        return sysmsg, items, "mc"
    if name == "mmlu":
        ds = list(load_dataset("cais/mmlu", "formal_logic", split="test"))
        pool, test = ds[:5], ds[5:5+n]
        def fmt(d):
            opts = "\n".join(f"({chr(65+i)}) {o}" for i, o in enumerate(d["choices"]))
            return f"Question: {d['question']}\n{opts}"
        shots = "\n\n".join(fmt(d)+f"\nAnswer: ({chr(65+d['answer'])})" for d in pool)
        sysmsg = ("Answer the multiple-choice logic question. End with 'The answer is (X)'.\n\n"
                  "Worked examples:\n" + shots)
        items = [{"q": fmt(d)+"\nAnswer:", "gold": f"({chr(65+d['answer'])})"} for d in test]
        return sysmsg, items, "mc"
    if name == "arc":
        ds = list(load_dataset("ai2_arc", "ARC-Challenge", split="test"))
        pool, test = ds[:5], ds[5:5+n]
        def fmt(d):
            opts = "\n".join(f"({l}) {t}" for l, t in zip(d["choices"]["label"], d["choices"]["text"]))
            return f"Question: {d['question']}\n{opts}"
        shots = "\n\n".join(fmt(d)+f"\nAnswer: ({d['answerKey']})" for d in pool)
        sysmsg = ("Answer the multiple-choice science question. End with 'The answer is (X)'.\n\n"
                  "Worked examples:\n" + shots)
        items = [{"q": fmt(d)+"\nAnswer:", "gold": f"({d['answerKey']})"} for d in test]
        return sysmsg, items, "mc"
    if name == "code":
        ds = list(load_dataset("openai_humaneval", split="test"))[:n]
        sysmsg = ("You are an expert Python programmer. Complete the function. Return ONLY "
                  "the full function in a ```python code block.")
        items = [{"q": d["prompt"], "gold": d, "entry": d["entry_point"]} for d in ds]
        return sysmsg, items, "code"
    raise SystemExit(f"unknown benchmark {name}")


def _mc_correct(text, gold):
    m = re.findall(r"[Tt]he answer is\s*\(?([A-Ea-e])\)?", text)
    letter = (m[-1].upper() if m else (re.findall(r"\(([A-E])\)", text) or [""])[-1])
    goldletter = re.sub(r"[()]", "", gold).strip().upper()[:1]
    return letter == goldletter


def run_arm(p, name, sysmsg, items, kind, optimized):
    client = BrevitasDropIn(base_url=PROV[p]["base"] or "https://api.openai.com/v1",
                            provider=p, api_key=os.environ[PROV[p]["key"]]) if optimized \
        else raw_client(p)
    sid = f"bench-{p}-{name}-{'b' if optimized else 'r'}"
    correct = 0; usd = 0.0; ptok = 0; cached = 0; n = 0
    mx = 600 if kind == "code" else 400
    for it in items:
        for attempt in (1, 2):
            try:
                resp = call(p, client, optimized, sysmsg, it["q"], sid, mx); break
            except Exception as e:
                if attempt == 2:
                    resp = None; break
                time.sleep(3)
        if resp is None:
            continue
        text, cost, pt, ca = read(resp, p)
        usd += cost; ptok += pt; cached += ca; n += 1
        if kind == "code":
            ok = passes(it["gold"]["prompt"], text, it["gold"]["test"], it["entry"])
        else:
            ok = _mc_correct(text, it["gold"])
        correct += int(ok)
        time.sleep(0.3)
    return {"n": n, "accuracy": round(correct/max(1, n), 4), "usd": round(usd, 6),
            "prompt_tokens": ptok, "cached_tokens": cached}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=list(PROV))
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--benchmarks", default="bbh,mmlu,arc,code")
    args = ap.parse_args()
    p = args.provider
    if not os.environ.get(PROV[p]["key"]):
        sys.exit(f"missing {PROV[p]['key']}")

    out = {"provider": p, "model": PROV[p]["model"], "n": args.n, "benchmarks": {}}
    for name in [b.strip() for b in args.benchmarks.split(",") if b.strip()]:
        sysmsg, items, kind = load_bench(name, args.n)
        print(f"\n[{p}/{name}] baseline…", flush=True)
        base = run_arm(p, name, sysmsg, items, kind, optimized=False)
        print(f"  baseline: acc={base['accuracy']} ${base['usd']:.6f} cached={base['cached_tokens']}/{base['prompt_tokens']}")
        print(f"[{p}/{name}] brevitas…", flush=True)
        brev = run_arm(p, name, sysmsg, items, kind, optimized=True)
        print(f"  brevitas: acc={brev['accuracy']} ${brev['usd']:.6f} cached={brev['cached_tokens']}/{brev['prompt_tokens']}")
        saved = base["usd"] - brev["usd"]
        out["benchmarks"][name] = {
            "baseline": base, "brevitas": brev,
            "accuracy_delta": round(brev["accuracy"] - base["accuracy"], 4),
            "cost_saved_pct": round(100*saved/base["usd"], 1) if base["usd"] > 0 else 0.0,
            "lossless": abs(brev["accuracy"] - base["accuracy"]) < 1e-9}
        b = out["benchmarks"][name]
        print(f"  => acc Δ {b['accuracy_delta']:+.4f}  saved {b['cost_saved_pct']}%  "
              f"lossless={b['lossless']}")
    res = ROOT / "benchmarks" / f"accuracy_suite_{p}.json"
    res.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nresults -> {res}")


if __name__ == "__main__":
    main()
