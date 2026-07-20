"""OSS agent-pipeline A/B: real dollar cost WITHOUT vs WITH Brevitas.

Two realistic multi-agent workloads, using the ACTUAL agent/task definitions from
popular open-source repos (their real IP), driven through an OpenAI-compatible client:

  * marketing — crewAI-examples/crews/marketing_strategy: 5 roles (lead analyst,
    strategist, creative, creative director) over a shared brand brief + tasks.
  * finance   — virattt/ai-hedge-fund: investor-persona analysts (Buffett, Wood,
    Munger, Burry, Ackman) each ruling bullish/bearish on a shared 10-K-style fact
    sheet, then a portfolio manager synthesizes.

Both repos' upstream runners need external-data keys we don't have (Serper /
financialdatasets.ai), so their web/data TOOLS are disabled and the agents reason from
an in-context brief — the LLM-cost structure (shared context re-sent to every agent
across a pipeline) is identical, which is exactly what Brevitas optimizes.

A/B: BASELINE sends each agent the full shared context every call (raw client).
BREVITAS routes the same calls through BrevitasDropIn (auto caching/retrieval/router).
Both measured from REAL provider usage; cost priced from api/store PROVIDER_COSTS_PER_1M
including cached-token discounts. Spend-guarded (max_tokens small, ~6 agents/run).

Usage: python3 benchmarks/oss_ab.py --provider deepseek --workload marketing
       python3 benchmarks/oss_ab.py --provider openai   --workload finance
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from token_efficiency_model.lossless.dropin import BrevitasDropIn  # noqa: E402
from token_efficiency_model.lossless.provider_cache import savings_from_usage  # noqa: E402
from brevitas.resource_bounds import safe_close_resource  # noqa: E402


def _load_env():
    f = REPO / ".env.local"
    if f.exists():
        for ln in f.read_text().splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


PROVIDERS = {
    "deepseek": {"model": "deepseek-chat", "base": "https://api.deepseek.com/v1",
                 "key_env": "Deepseek_api_key",
                 "in": 0.27, "cached": 0.07, "out": 1.10},   # $/1M (deepseek docs)
    "openai": {"model": "gpt-4o-mini", "base": "https://api.openai.com/v1",
               "key_env": "OPENAI_API_KEY",
               "in": 0.15, "cached": 0.075, "out": 0.60},     # $/1M (openai docs)
}

# ── shared briefs (the big repeated context that every agent in the pipeline sees) ──
MARKETING_BRIEF = """CUSTOMER: NorthPeak Outdoor (northpeak.example.com), a direct-to-consumer
brand selling premium insulated water bottles and trail gear.
PROJECT: Launch the new "Summit 1L" vacuum bottle into the North American market for Q3.
CONSTRAINTS: mid-premium price point ($39), sustainability-conscious millennials + Gen-Z,
competitors are Hydro Flask, Yeti, Owala. Budget $180k. Channels open: paid social, creator
partnerships, email, retail endcaps. Brand voice: rugged, optimistic, understated.
KNOWN DATA: 62% of past buyers are 24-38; email list 210k; IG 88k followers; prior launch
(Summit 750ml) hit 41k units in 6 months with a 3.1x ROAS on paid social.""" * 3

# crewAI marketing_strategy: real roles + tasks (config/agents.yaml, tasks.yaml)
MARKETING_AGENTS = [
    ("Lead Market Analyst", "Conduct in-depth analysis of the product and competitors to guide strategy.",
     "Produce a concise competitor + audience positioning report."),
    ("Chief Marketing Strategist", "Synthesize insights into a marketing strategy.",
     "Produce a strategy with goals, key messages, tactics, channels and KPIs."),
    ("Creative Content Creator", "Develop high-impact campaign ideas and ad copy.",
     "Produce 5 campaign ideas, each with a one-line description and expected impact."),
    ("Creative Content Creator", "Turn approved ideas into marketing copy.",
     "Write 3 short ad copies tailored to the target audience."),
    ("Chief Creative Director", "Review the team's work for quality and brand alignment.",
     "Give an approval verdict with 3 specific improvement notes."),
]

FINANCE_BRIEF = """FACT SHEET — ticker NPO (NorthPeak Outdoor, fictional), FY2024 10-K excerpt.
Revenue $612M (+18% YoY); gross margin 54%; operating margin 16%; net income $71M.
Free cash flow $88M; cash $140M; total debt $95M; shares out 52M. ROE 22%, ROIC 19%.
5-yr revenue CAGR 21%; 5-yr EPS CAGR 24%. Current price $41; P/E 30; P/FCF 24; P/B 6.1.
Moat: brand + DTC repeat rate 47%. Risks: discretionary demand, input-cost (steel/resin)
volatility, competitor price wars (Yeti, Hydro Flask). Management: founder-led, 9% insider
ownership, no dilution in 3 yrs, disciplined buybacks. Guidance: 12-15% rev growth FY25.""" * 3

# ai-hedge-fund: real investor personas (src/agents/*.py system prompts, condensed)
FINANCE_AGENTS = [
    ("Warren Buffett", "You are Warren Buffett. Judge bullish/bearish/neutral using ONLY the facts. "
     "Weigh circle of competence, moat, management, financial strength, valuation vs intrinsic value."),
    ("Cathie Wood", "You are Cathie Wood. Judge bullish/bearish/neutral. Weigh disruptive growth, "
     "TAM expansion, innovation and long-run exponential potential over near-term valuation."),
    ("Charlie Munger", "You are Charlie Munger. Judge bullish/bearish/neutral. Demand a durable moat, "
     "rational management and a fair price; invert and avoid obvious stupidity."),
    ("Michael Burry", "You are Michael Burry. Judge bullish/bearish/neutral. Hunt for hidden risk, "
     "overvaluation and balance-sheet fragility; be contrarian and evidence-driven."),
    ("Bill Ackman", "You are Bill Ackman. Judge bullish/bearish/neutral. Look for high-quality "
     "businesses with pricing power, catalysts and capital-allocation upside."),
    ("Portfolio Manager", "You are the portfolio manager. Given the analysts' signals, output a final "
     "position (buy/hold/sell), a confidence 0-100, and one-sentence reasoning."),
]


def _mk_client(prov, optimized):
    cfg = PROVIDERS[prov]
    key = os.environ[cfg["key_env"]]
    if optimized:
        return BrevitasDropIn(base_url=cfg["base"], provider=prov, api_key=key)
    import openai
    return openai.OpenAI(api_key=key, base_url=cfg["base"])


def _call(client, optimized, model, messages, sid):
    if optimized:
        resp, _ = client.chat(messages=messages, model=model, session_id=sid, max_tokens=220)
        return resp
    return client.chat.completions.create(model=model, messages=messages, max_tokens=220)


def _usage_cost(usage, cfg) -> tuple[float, int, int]:
    """Real $ from provider usage incl. cached-token discount. Returns (usd, prompt, cached)."""
    prompt = usage.prompt_tokens
    out = usage.completion_tokens
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    fresh = prompt - cached
    usd = (fresh * cfg["in"] + cached * cfg["cached"] + out * cfg["out"]) / 1_000_000
    return usd, prompt, cached


def _run_with_client(prov, workload, optimized, client) -> dict:
    cfg = PROVIDERS[prov]
    if workload == "marketing":
        brief, agents, sysprefix = MARKETING_BRIEF, MARKETING_AGENTS, None
    else:
        brief, agents, sysprefix = FINANCE_BRIEF, FINANCE_AGENTS, None

    sid = f"{workload}-{'brev' if optimized else 'base'}"
    total_usd = 0.0
    total_prompt = 0
    total_cached = 0
    transcript = []
    # append-only pipeline: shared brief up front, each agent's output appended (real
    # multi-agent hand-off — the shared context is re-sent to every subsequent agent)
    history = [{"role": "user", "content": f"Shared project brief:\n{brief}"},
               {"role": "assistant", "content": "Brief received. Ready."}]

    for i, agent in enumerate(agents):
        if workload == "marketing":
            role, goal, out = agent
            sysmsg = f"You are the {role} at a digital marketing agency. {goal}"
            task = f"Task: {out}"
        else:
            role, sysmsg = agent
            task = "Based on the fact sheet and prior analysts, give your verdict. Be brief."
        messages = ([{"role": "system", "content": sysmsg}] + history +
                    [{"role": "user", "content": task}])
        for attempt in (1, 2):
            try:
                resp = _call(client, optimized, cfg["model"], messages, sid)
                break
            except Exception as e:
                if attempt == 2:
                    return {"error": f"{role}: {type(e).__name__}: {e}", "usd": total_usd}
                time.sleep(3)
        text = resp.choices[0].message.content or ""
        usd, prompt, cached = _usage_cost(resp.usage, cfg)
        total_usd += usd
        total_prompt += prompt
        total_cached += cached
        transcript.append({"agent": role, "usd": round(usd, 6), "prompt": prompt,
                           "cached": cached, "head": text[:70].replace("\n", " ")})
        history.append({"role": "user", "content": f"[{role} task] {task}"})
        history.append({"role": "assistant", "content": text})
        time.sleep(0.6)

    return {"usd": total_usd, "prompt_tokens": total_prompt, "cached_tokens": total_cached,
            "transcript": transcript}


def run(prov, workload, optimized) -> dict:
    client = _mk_client(prov, optimized)
    try:
        return _run_with_client(prov, workload, optimized, client)
    finally:
        safe_close_resource(client)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="deepseek", choices=list(PROVIDERS))
    ap.add_argument("--workload", default="marketing", choices=["marketing", "finance"])
    args = ap.parse_args()
    _load_env()
    if not os.environ.get(PROVIDERS[args.provider]["key_env"]):
        print(f"missing key {PROVIDERS[args.provider]['key_env']}"); return 1

    print(f"\n=== {args.workload} on {args.provider}: BASELINE (no Brevitas) ===", flush=True)
    base = run(args.provider, args.workload, optimized=False)
    print(f"  cost ${base.get('usd', 0):.6f}  prompt_tok {base.get('prompt_tokens')}  "
          f"cached {base.get('cached_tokens')}")

    print(f"=== {args.workload} on {args.provider}: WITH BREVITAS ===", flush=True)
    brev = run(args.provider, args.workload, optimized=True)
    print(f"  cost ${brev.get('usd', 0):.6f}  prompt_tok {brev.get('prompt_tokens')}  "
          f"cached {brev.get('cached_tokens')}")

    out = {"provider": args.provider, "workload": args.workload,
           "baseline": base, "brevitas": brev}
    if "usd" in base and "usd" in brev and base["usd"] > 0 and "error" not in base and "error" not in brev:
        saved = base["usd"] - brev["usd"]
        out["cost_saved_usd"] = round(saved, 6)
        out["cost_saved_pct"] = round(100 * saved / base["usd"], 1)
        print(f"\n  >>> SAVED ${saved:.6f}  ({out['cost_saved_pct']}% of baseline cost) <<<")
    for k in ("baseline", "brevitas"):
        if out[k].get("error"):
            print(f"  {k} ERROR: {out[k]['error']}")
    res = Path(__file__).parent / f"oss_ab_{args.workload}_{args.provider}.json"
    res.write_text(json.dumps(out, indent=2, default=str))
    print(f"  results -> {res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
