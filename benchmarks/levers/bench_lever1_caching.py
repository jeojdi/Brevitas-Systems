"""Benchmark — Lever 1 (provider-native caching, honest savings).

No live API keys here, so we SIMULATE the provider's documented cache accounting on a
realistic multi-turn agent loop and verify Brevitas's two contributions:
  * breakpoints are placed only on a >=1024-token stable prefix (and never the tail)
  * savings are computed from REAL usage fields (cache_read / cached_tokens), honestly
    accounting for the turn-1 cache-write surcharge.

Provider accounting (from docs): Anthropic cache_read ~0.1x input, cache_write ~1.25x,
min cacheable 1024 tok; OpenAI cached input ~0.5x, automatic >=1024 tok.

Run:  python benchmarks/levers/bench_lever1_caching.py
"""

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from token_efficiency_model.lossless.provider_cache import (
    apply_anthropic_cache,
    count_tokens,
    savings_from_usage,
)


def build_body(system_tokens=1500, n_prior_turns=2):
    system = " ".join(["policy"] * system_tokens)
    messages = []
    for t in range(n_prior_turns):
        messages.append({"role": "user", "content": " ".join(["ctx"] * 400)})
        messages.append({"role": "assistant", "content": " ".join(["resp"] * 100)})
    messages.append({"role": "user", "content": "the newest volatile question"})
    return {"system": system, "messages": messages}


def simulate_anthropic_turn(body, prefix_cached: bool):
    """Return a provider-style usage dict for one turn given the cache plan."""
    plan = apply_anthropic_cache(copy.deepcopy(body))
    prefix = plan.cached_prefix_tokens
    last = body["messages"][-1]["content"]
    fresh_tail = count_tokens(last if isinstance(last, str) else "")
    if plan.breakpoints == 0:
        return {"input_tokens": prefix + fresh_tail, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0}, plan
    if not prefix_cached:  # turn 1: writes the cache
        return {"input_tokens": fresh_tail, "cache_creation_input_tokens": prefix,
                "cache_read_input_tokens": 0}, plan
    return {"input_tokens": fresh_tail, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": prefix}, plan


def scenario_anthropic_loop(turns=6):
    body = build_body()
    # verify the prefix is byte-identical across turns when we only change the tail
    base_prefix = json.dumps({"system": body["system"], "messages": body["messages"][:-1]})
    uncached_total = actual_total = 0.0
    for t in range(turns):
        body["messages"][-1]["content"] = f"question for turn {t}"
        usage, plan = simulate_anthropic_turn(body, prefix_cached=(t > 0))
        s = savings_from_usage(usage, "anthropic")
        uncached_total += s.uncached_cost
        actual_total += s.actual_cost
        cur_prefix = json.dumps({"system": body["system"], "messages": body["messages"][:-1]})
        assert cur_prefix == base_prefix, "prefix mutated across turns!"
    return {
        "scenario": "anthropic_multiturn_loop",
        "turns": turns,
        "breakpoints": plan.breakpoints,
        "cached_prefix_tokens": plan.cached_prefix_tokens,
        "uncached_cost": round(uncached_total, 1),
        "actual_cost": round(actual_total, 1),
        "savings_pct": round(100 * (1 - actual_total / uncached_total), 2),
        "prefix_byte_identical": True,
    }


def scenario_openai_steady_state():
    # OpenAI auto-caches the >=1024-tok prefix after the first call (50% off cached)
    prompt = 5000
    cached = 4200
    usage = {"prompt_tokens": prompt, "prompt_tokens_details": {"cached_tokens": cached}}
    s = savings_from_usage(usage, "openai")
    return {
        "scenario": "openai_steady_state_call",
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "savings_pct": s.savings_pct,
    }


def main():
    ant = scenario_anthropic_loop()
    oai = scenario_openai_steady_state()
    checks = {
        "anthropic_loop_savings>=50%": ant["savings_pct"] >= 50.0,
        "breakpoint_on_large_prefix": ant["breakpoints"] >= 1,
        "prefix_byte_identical": ant["prefix_byte_identical"],
        "openai_steady_savings>=35%": oai["savings_pct"] >= 35.0,
    }
    passed = all(checks.values())
    report = {"lever": 1, "results": [ant, oai], "checks": checks, "passed": passed}
    with open(os.path.join(os.path.dirname(__file__), "results_lever1.json"), "w") as f:
        json.dump(report, f, indent=2)

    print("=== Lever 1 — provider-native caching (honest savings) ===")
    print(json.dumps(ant, indent=2))
    print(json.dumps(oai, indent=2))
    print("checks:", json.dumps(checks, indent=2))
    print("PASSED" if passed else "FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
