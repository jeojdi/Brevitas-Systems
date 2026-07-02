"""Price the two metered arms and compare. Reads meter JSONL (one record/call) and
computes real $ per provider incl. cached-token discounts."""
import json, sys
from pathlib import Path

# $/1M tokens (provider docs); cached = cached-input price
RATES = {
    "openai":    {"in": 0.15,  "cached": 0.075, "out": 0.60},   # gpt-4o-mini
    "deepseek":  {"in": 0.27,  "cached": 0.07,  "out": 1.10},   # deepseek-chat
    "anthropic": {"in": 0.80,  "cached": 0.08,  "out": 4.00,    # claude-haiku-4-5
                  "write": 1.00},                                # write=1.25x handled below
}


def price_line(rec):
    prov = rec["provider"]
    u = rec.get("usage", {}) or {}
    r = RATES.get(prov)
    if not r:
        return 0.0, 0, 0
    if prov == "anthropic":
        fresh = u.get("input_tokens", 0)
        read = u.get("cache_read_input_tokens", 0)
        write = u.get("cache_creation_input_tokens", 0)
        out = u.get("output_tokens", 0)
        usd = (fresh * r["in"] + read * r["cached"] + write * r["in"] * 1.25
               + out * r["out"]) / 1e6
        return usd, fresh + read + write, read
    prompt = u.get("prompt_tokens", 0)
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    out = u.get("completion_tokens", 0)
    fresh = prompt - cached
    usd = (fresh * r["in"] + cached * r["cached"] + out * r["out"]) / 1e6
    return usd, prompt, cached


def summarize(path):
    by = {}
    total = 0.0
    calls = 0
    for ln in Path(path).read_text().splitlines():
        if not ln.strip():
            continue
        rec = json.loads(ln)
        usd, prompt, cached = price_line(rec)
        prov = rec["provider"]
        b = by.setdefault(prov, {"usd": 0.0, "calls": 0, "prompt": 0, "cached": 0})
        b["usd"] += usd; b["calls"] += 1; b["prompt"] += prompt; b["cached"] += cached
        total += usd; calls += 1
    return {"total_usd": total, "calls": calls, "by_provider": by}


base = summarize(sys.argv[1])
brev = summarize(sys.argv[2])
print("BASELINE:", json.dumps(base, indent=2))
print("BREVITAS:", json.dumps(brev, indent=2))
if base["total_usd"] > 0:
    saved = base["total_usd"] - brev["total_usd"]
    print(f"\n>>> total baseline ${base['total_usd']:.6f}  brevitas ${brev['total_usd']:.6f}"
          f"  SAVED ${saved:.6f} ({100*saved/base['total_usd']:.1f}%) <<<")
Path("/tmp/oss-ab/hf_ab_summary.json").write_text(json.dumps(
    {"baseline": base, "brevitas": brev}, indent=2))
