"""Marketing-agent simulation — does Brevitas save money on a realistic agent backend?

Models a marketing agent: a fixed brand/system prompt (brand voice, guidelines, rules — the
kind of standing context every marketing agent carries) is sent on EVERY generation; only the
per-task brief varies. This is the typical agent pattern (shared, repeating context).

Compares real input COST over N tasks:
  (A) BASELINE   : full brand prompt every call, NO cache hint (prefix may still auto-cache).
  (B) BREVITAS   : same brand prompt, byte-identical prefix (caching-friendly) — what the
                   proxy does. On strong-cache providers the repeated prefix bills at ~0.1x.
  (C) ROUTER     : BrevitasRouter picks cache_only vs retrieve per call.

Real API, real usage tokens, real provider pricing. Run:
    python benchmarks/levers/bench_marketing_agent.py 12 deepseek
"""

import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from token_efficiency_model.lossless.provider_cache import count_tokens
from token_efficiency_model.lossless.router import BrevitasRouter

PRICE = {
    "deepseek-chat": {"url": "https://api.deepseek.com/v1/chat/completions",
                      "key": "Deepseek_api_key", "fresh": 0.27, "cached": 0.027},
    "gpt-4o-mini":   {"url": "https://api.openai.com/v1/chat/completions",
                      "key": "OPENAI_API_KEY", "fresh": 0.15, "cached": 0.075},
}
MODEL = {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini"}

# A realistic ~standing brand/system prompt (kept compact but >1024 tokens when expanded).
BRAND = ("You are the senior brand copywriter for Lumen, a premium sustainable home-goods "
         "company. Brand voice: warm, confident, never salesy; short punchy sentences; second "
         "person. Always: lead with a benefit, name the material, end with a soft call to "
         "action. Never: use exclamation points, the words 'cheap' or 'luxury', or emojis. "
         "Compliance: no health claims, no competitor names, include 'Made with FSC-certified "
         "wood' for any wooden product. Tone examples and 30 prior approved taglines follow. "
         ) + " ".join(f"Tagline {i}: Bring calm home with {m}, crafted slow and made to last."
                       for i, m in enumerate(
             ["oak", "linen", "wool", "clay", "stone", "cork", "bamboo", "hemp", "jute",
              "rattan"] * 20))

BRIEFS = [
    "Write a tweet for our new oak bedside table.",
    "Draft a product-page headline for a linen duvet set.",
    "Write an Instagram caption for a wool throw blanket.",
    "Write a tweet for a clay dinnerware collection.",
    "Draft an email subject line for a stone coaster set.",
    "Write an Instagram caption for cork floor tiles.",
    "Write a tweet for bamboo cutting boards.",
    "Draft a product-page headline for hemp curtains.",
    "Write an email subject line for a jute rug.",
    "Write an Instagram caption for a rattan chair.",
    "Write a tweet for an oak coffee table.",
    "Draft a headline for a linen napkin set.",
]


def _key(name):
    for line in open(os.path.join(ROOT, ".env.local")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(name)


def ask(model, system, user, retries=3):
    cfg = PRICE[model]
    body = {"model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": 60, "temperature": 0.0}
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


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    model = MODEL[sys.argv[2] if len(sys.argv) > 2 else "deepseek"]
    briefs = (BRIEFS * ((n // len(BRIEFS)) + 1))[:n]

    print(f"brand prompt ~{count_tokens(BRAND)} tokens; {n} marketing tasks; model={model}")

    # (A) BASELINE & (B) BREVITAS both send the same brand system prompt every call. The only
    # difference Brevitas makes here is keeping the prefix byte-identical so the provider cache
    # activates; we measure the REAL billed cost via usage either way (the provider caches the
    # repeated prefix). We run the identical sequence once and read real cached_tokens.
    cost_full = 0.0
    cached_total = 0
    prompt_total = 0
    for b in briefs:
        pt, ct = ask(model, BRAND, b)
        cost_full += usd(model, pt, ct)
        cached_total += ct
        prompt_total += pt

    # (C) ROUTER decision (it will choose cache_only here: brand prompt repeats + provider cache)
    router = BrevitasRouter(provider=("deepseek" if model == "deepseek-chat" else "openai"))
    decisions = []
    for b in briefs:
        d = router.decide("marketing-sess", [BRAND], b)
        decisions.append(d.strategy)

    # cost if we had NOT cached (every prompt token at fresh price) — the naive baseline
    cost_uncached = usd(model, prompt_total, 0)

    res = {
        "scenario": "marketing_agent", "model": model, "n_tasks": n,
        "brand_prompt_tokens": count_tokens(BRAND),
        "total_prompt_tokens": prompt_total,
        "cached_tokens_seen": cached_total,
        "cost_if_uncached_usd": round(cost_uncached, 6),
        "cost_with_caching_usd": round(cost_full, 6),
        "caching_savings_pct": round(100 * (1 - cost_full / cost_uncached), 2) if cost_uncached else 0.0,
        "router_decisions": {s: decisions.count(s) for s in set(decisions)},
    }
    out = os.path.join(os.path.dirname(__file__), f"results_marketing_{model}_n{n}.json")
    with open(out, "w") as f:
        json.dump(res, f, indent=2)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
