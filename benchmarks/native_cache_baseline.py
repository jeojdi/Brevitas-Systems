#!/usr/bin/env python3
"""The missing baseline: Brevitas vs a customer who already turned on provider-native caching.

WHY THIS EXISTS
---------------
Every savings number in `benchmarks/` compares Brevitas against a baseline with the
provider cache DISABLED. `cache_benchmark.py` does it explicitly -- its BEFORE arm
injects `[CACHE_BUST_<i>_<ns>]` at position 0 of both system and user so the prefix
can never match (see cache_benchmark.py, "BEFORE: inject cache-busting token"), and
its AFTER arm additionally discards a warm-up call, so the cache-WRITE cost never
enters the AFTER total. Both choices inflate the reported delta.

No measurement in this repo answers the question a buyer actually asks: *I already
set cache_control / I already send a stable prefix. What does Brevitas add on top?*
This script answers exactly that and nothing else.

DESIGN
------
Two arms, same account, same model, same workload, same session shape:

  NATIVE   what a competent customer does unaided -- byte-stable prefix, and for
           Anthropic a single cache_control breakpoint at the end of the system
           block. No Brevitas code runs.
  BREVITAS the identical workload passed through `engine.optimize_request`, i.e.
           the real proxy optimization path.

Each arm gets its own random salt at position 0 of the system prompt so the second
arm can never inherit the first arm's provider-side cache. Within an arm the salt is
constant, so the arm's own prefix caching works normally.

Cost comes from the PROVIDER RECEIPT via `brevitas.receipts.calculate_costs` -- the
same function billing uses -- never from a local token estimate. The headline number
is `incremental_pct`: the fraction of the native-cache arm's cost that Brevitas
removes. That number, not the cache-busted one, is what belongs in a data room.

USAGE
-----
  export ANTHROPIC_API_KEY=...            # or OPENAI_API_KEY / DEEPSEEK_API_KEY
  python benchmarks/native_cache_baseline.py --provider anthropic \
      --model claude-sonnet-5 --turns 12 --sessions 3

RUNTIME
-------
2 arms x sessions x turns provider calls, issued serially so cache state is
deterministic. At ~2-4 s per call: 12 turns x 3 sessions ~= 72 calls ~= 4 min.
Token spend is roughly `sessions * turns * prefix_tokens` per arm at the provider's
uncached rate for the first turn of each session and the cached rate thereafter.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from brevitas.receipts import calculate_costs, normalize_usage  # noqa: E402
from token_efficiency_model.lossless.engine import optimize_request  # noqa: E402
from token_efficiency_model.lossless.provider_cache import (  # noqa: E402
    apply_anthropic_cache, count_tokens)
from token_efficiency_model.lossless.router import BrevitasRouter  # noqa: E402

ENDPOINTS = {
    "anthropic": ("https://api.anthropic.com/v1/messages", "ANTHROPIC_API_KEY"),
    "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1/chat/completions", "DEEPSEEK_API_KEY"),
}

# A stable prefix big enough that every provider will cache it (Anthropic needs
# >=1024 tokens, 2048 for Haiku; OpenAI needs >=1024).
_DOC = (
    "Section {i}. The operating agreement between the parties provides that each "
    "quarterly statement must reconcile bookings, deferred revenue, and recognized "
    "revenue, and that any variance greater than two percent be explained in a "
    "footnote to the statement with reference to the originating contract. "
)


def stable_prefix(target_tokens: int = 4000) -> str:
    out, i = "", 0
    while count_tokens(out) < target_tokens:
        i += 1
        out += _DOC.format(i=i)
    return out


QUESTIONS = [
    "Which section governs footnote requirements?",
    "What variance threshold triggers an explanation?",
    "Summarize the reconciliation obligation in one sentence.",
    "Does the agreement mention deferred revenue?",
    "What must each quarterly statement reconcile?",
]


def build_body(provider: str, model: str, salt: str, prefix: str, turn: int) -> dict:
    system = f"[{salt}]\nYou are a contract analyst.\n\n{prefix}"
    question = QUESTIONS[turn % len(QUESTIONS)] + f" (turn {turn})"
    if provider == "anthropic":
        return {"model": model, "max_tokens": 128, "system": system,
                "messages": [{"role": "user", "content": question}]}
    return {"model": model, "max_tokens": 128, "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": question}]}


def native_cache(provider: str, body: dict) -> dict:
    """What an unaided customer does: one obvious breakpoint on the stable prefix.

    OpenAI and DeepSeek cache byte-identical prefixes automatically, so for those the
    honest 'native' arm is simply the unmodified byte-stable request.
    """
    if provider == "anthropic":
        apply_anthropic_cache(body, max_breakpoints=1)
    return body


def call(provider: str, model: str, body: dict, timeout: float = 90.0) -> dict:
    url, env = ENDPOINTS[provider]
    key = os.environ.get(env, "")
    if not key:
        sys.exit(f"ERROR: {env} is not set")
    if provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
    else:
        headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
    response = httpx.post(url, headers=headers, json=body, timeout=timeout)
    response.raise_for_status()
    return response.json()


def run_arm(arm: str, provider: str, model: str, prefix: str,
            sessions: int, turns: int, sleep: float) -> dict:
    salt = f"{arm}-{secrets.token_hex(8)}"
    total_cost = 0.0
    receipts = []
    for session in range(sessions):
        router = BrevitasRouter(provider=provider)
        session_id = f"{arm}-s{session}"
        for turn in range(turns):
            body = build_body(provider, model, salt, prefix, turn)
            if arm == "native":
                body = native_cache(provider, deepcopy(body))
            else:
                optimize_request(body, provider, router, session_id)
            data = call(provider, model, body)
            receipt = normalize_usage(data.get("usage"), provider)
            costs = calculate_costs(provider, model, receipt.input_tokens, receipt)
            if costs["pricing_status"] != "priced":
                sys.exit(f"ERROR: {provider}/{model} is not in MODEL_PRICES; "
                         "add it before trusting any cost number")
            total_cost += costs["actual_cost_usd"]
            receipts.append({
                "session": session, "turn": turn,
                "fresh": receipt.fresh_input_tokens,
                "cached": receipt.cached_input_tokens,
                "write": receipt.cache_write_tokens,
                "output": receipt.output_tokens,
                "cost_usd": costs["actual_cost_usd"],
            })
            print(f"  {arm} s{session} t{turn:02d} "
                  f"fresh={receipt.fresh_input_tokens:6d} "
                  f"cached={receipt.cached_input_tokens:6d} "
                  f"write={receipt.cache_write_tokens:6d} "
                  f"${costs['actual_cost_usd']:.6f}", flush=True)
            if sleep:
                time.sleep(sleep)
    input_tokens = sum(r["fresh"] + r["cached"] + r["write"] for r in receipts)
    cached_tokens = sum(r["cached"] for r in receipts)
    return {
        "arm": arm, "calls": len(receipts), "total_cost_usd": round(total_cost, 8),
        "input_tokens": input_tokens, "cached_tokens": cached_tokens,
        "cache_hit_pct": round(100 * cached_tokens / max(1, input_tokens), 2),
        "receipts": receipts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=sorted(ENDPOINTS), default="anthropic")
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--turns", type=int, default=12)
    parser.add_argument("--sessions", type=int, default=3)
    parser.add_argument("--prefix-tokens", type=int, default=4000)
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="seconds between calls; keep 0 to stay inside the 5m cache TTL")
    parser.add_argument("--out", default="benchmarks/native_cache_baseline_results.json")
    args = parser.parse_args()

    prefix = stable_prefix(args.prefix_tokens)
    print(f"stable prefix: {count_tokens(prefix)} tokens; "
          f"{args.sessions} sessions x {args.turns} turns x 2 arms = "
          f"{args.sessions * args.turns * 2} provider calls\n")

    native = run_arm("native", args.provider, args.model, prefix,
                     args.sessions, args.turns, args.sleep)
    print()
    brevitas = run_arm("brevitas", args.provider, args.model, prefix,
                       args.sessions, args.turns, args.sleep)

    delta = native["total_cost_usd"] - brevitas["total_cost_usd"]
    incremental_pct = 100 * delta / native["total_cost_usd"] if native["total_cost_usd"] else 0.0
    summary = {
        "provider": args.provider, "model": args.model,
        "sessions": args.sessions, "turns": args.turns,
        "prefix_tokens": count_tokens(prefix),
        "native": {k: v for k, v in native.items() if k != "receipts"},
        "brevitas": {k: v for k, v in brevitas.items() if k != "receipts"},
        "incremental_savings_usd": round(delta, 8),
        "incremental_savings_pct": round(incremental_pct, 2),
        "note": ("incremental_savings_pct is Brevitas's benefit OVER a customer who "
                 "already uses provider-native caching. It is NOT comparable to the "
                 "cache-busted numbers in cache_benchmark.py."),
    }
    print("\n=== RESULT ===")
    print(json.dumps(summary, indent=2))
    Path(args.out).write_text(json.dumps(
        {**summary, "native_receipts": native["receipts"],
         "brevitas_receipts": brevitas["receipts"]}, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
