"""Live tri-provider plug-and-play acceptance test (goal gate) — REALISTIC scenario.

Default scenario = a real coding-agent session: three REAL source files from this repo
are the context, and the conversation grows append-only across turns (exactly how real
assistant/agent traffic behaves, and exactly where cache economics matter). Questions
have answers verifiable from the actual code, so correctness is objective.

Per provider (Anthropic / OpenAI / DeepSeek) it verifies:
  1. Answers are correct (response contains the expected code fact).
  2. Provider caching actually engages: cached tokens > 0 on turns 2+.
  3. The honest savings report shows real input-side savings (> 0%) on warm turns.
  4. Block-style message content round-trips without crashing.

Spend guard: 4 calls/provider, max_tokens=200, ~6K-token context → a few cents total.
Keys: env or .env.local (ANTHROPIC_API_KEY, OPENAI_API_KEY, Deepseek_api_key).

Usage:
  python3 benchmarks/live_e2e.py                       # realistic agent scenario, all 3
  python3 benchmarks/live_e2e.py --providers deepseek  # subset
  python3 benchmarks/live_e2e.py --scenario doc        # simple doc-QA (debug only)
Exit 0 only if every requested provider passes every assertion.
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


def _load_env_local() -> None:
    env = REPO / ".env.local"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# --------------------------------------------------------------------------- #
# Scenario A (default, realistic): coding-agent session over REAL repo files.
# Append-only history — the stable prefix grows each turn like real agent traffic.
# --------------------------------------------------------------------------- #
AGENT_FILES = [
    "token_efficiency_model/lossless/router.py",
    "token_efficiency_model/lossless/provider_cache.py",
    "token_efficiency_model/lossless/api_adapter.py",
]

# (question, [any-of expected substrings — from the real code])
AGENT_TURNS = [
    ("Which strategy strings can BrevitasRouter.decide() return? List them exactly.",
     ["cache_only"]),
    ("What is the value of MIN_CACHEABLE in router.py, and what does it represent?",
     ["1024"]),
    ("In savings_from_usage(), which usage field holds Anthropic's cached-read tokens?",
     ["cache_read_input_tokens"]),
    ("In retrieval_select(), what happens if the sentence-transformer encoder fails to "
     "load? Quote the fallback behavior.",
     ["encoder_unavailable", "full context", "fallback", "falls back", "fail-safe"]),
]

AGENT_SYSTEM = ("You are a senior engineer analyzing the Brevitas codebase. Answer "
                "precisely and concisely using ONLY the provided source files. Quote "
                "identifiers and constants exactly as they appear in the code.")


def build_agent_context() -> str:
    parts = []
    for rel in AGENT_FILES:
        src = (REPO / rel).read_text()
        parts.append(f"### File: {rel}\n```python\n{src}\n```\n")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Scenario B (debug-only): synthetic doc QA with planted facts.
# --------------------------------------------------------------------------- #
def build_doc_context() -> str:
    parts = ["INTERNAL OPERATIONS HANDBOOK (fictional test corpus)\n",
             "Section 1. The launch code for Project Aurora is BLUE-7492.\n"]
    for i in range(60):
        parts.append(f"Section {i + 2}. Standard guidance item {i}: document each step, "
                     f"file weekly summaries, escalate exceptions; incident {1000 + i} "
                     f"showed skipping review increases rework.\n")
    parts.append("Section 63. The database password rotation happens every 17 days.\n")
    return "".join(parts)


DOC_TURNS = [
    ("What is the launch code for Project Aurora?", ["BLUE-7492"]),
    ("How often does the database password rotation happen?", ["17"]),
    ("State the launch code for Project Aurora exactly.", ["BLUE-7492"]),
    ("What is stated in Section 63?", ["17"]),
]

DOC_SYSTEM = "Answer questions using only the provided handbook."


PROVIDERS = {
    "anthropic": {"model": "claude-haiku-4-5-20251001", "base_url": "", "key_env": "ANTHROPIC_API_KEY"},
    "openai": {"model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
    "deepseek": {"model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "key_env": "Deepseek_api_key"},
}


def _answer_text(resp, provider: str) -> str:
    if provider == "anthropic":
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return resp.choices[0].message.content or ""


def run_provider(name: str, system: str, context: str, turns) -> dict:
    cfg = PROVIDERS[name]
    key = os.environ.get(cfg["key_env"], "")
    result = {"provider": name, "model": cfg["model"], "turns": [], "errors": []}
    if not key:
        result["errors"].append(f"missing key {cfg['key_env']}")
        return result

    client = BrevitasDropIn(base_url=cfg["base_url"] or "https://api.openai.com/v1",
                            provider=name, api_key=key)

    # Append-only history, like a real agent session. Context rides in the FIRST user
    # message as a block-style content list (assertion 4: block content must not crash).
    history: list[dict] = []
    for turn, (q, expects) in enumerate(turns, start=1):
        if turn == 1:
            user_msg = {"role": "user",
                        "content": [{"type": "text", "text": context},
                                    {"type": "text", "text": f"\n\nQuestion: {q}"}]}
        else:
            user_msg = {"role": "user", "content": q}
        messages = history + [user_msg]

        kwargs: dict = {"max_tokens": 200}
        if name == "anthropic":
            kwargs["system"] = system
            send = messages
        else:
            send = [{"role": "system", "content": system}] + messages

        for attempt in (1, 2):
            try:
                resp, sav = client.chat(messages=send, model=cfg["model"],
                                        session_id=f"e2e-{name}", **kwargs)
                break
            except Exception as e:
                if attempt == 2:
                    result["errors"].append(f"turn {turn}: {type(e).__name__}: {e}")
                    return result
                time.sleep(3)

        text = _answer_text(resp, name)
        correct = any(x.lower() in text.lower() for x in expects)
        result["turns"].append({
            "turn": turn, "correct": correct, "expect_any": expects,
            "input_fresh": sav.input_fresh, "input_cached": sav.input_cached,
            "output_tokens": sav.output_tokens, "cached_tokens": sav.cached_tokens,
            "input_savings_pct": sav.input_savings_pct, "savings_pct": sav.savings_pct,
            "retrieval_applied": sav.retrieval_applied,
            "answer_head": text[:100].replace("\n", " "),
        })
        # grow the history exactly like a real session (append-only)
        history = messages + [{"role": "assistant", "content": text}]
        time.sleep(1)
    return result


def evaluate(result: dict) -> list[str]:
    fails = []
    if result["errors"]:
        return [f"errors: {result['errors']}"]
    turns = result["turns"]
    wrong = [t["turn"] for t in turns if not t["correct"]]
    if wrong:
        fails.append(f"incorrect answers on turns {wrong}")
    if sum(t["cached_tokens"] for t in turns[1:]) <= 0:
        fails.append("no cached tokens on any warm turn (2+) — caching never engaged")
    if max((t["input_savings_pct"] for t in turns[1:]), default=0.0) <= 0.0:
        fails.append("input savings never exceeded 0% on warm turns")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", default="anthropic,openai,deepseek")
    ap.add_argument("--scenario", default="agent", choices=["agent", "doc"])
    args = ap.parse_args()
    _load_env_local()

    if args.scenario == "agent":
        system, context, turns = AGENT_SYSTEM, build_agent_context(), AGENT_TURNS
    else:
        system, context, turns = DOC_SYSTEM, build_doc_context(), DOC_TURNS

    overall_ok = True
    report = {"scenario": args.scenario}
    for name in [p.strip() for p in args.providers.split(",") if p.strip()]:
        print(f"\n=== {name} ({args.scenario}) ===", flush=True)
        res = run_provider(name, system, context, turns)
        fails = evaluate(res)
        report[name] = {"result": res, "fails": fails}
        for t in res["turns"]:
            print(f"  turn {t['turn']}: correct={t['correct']} fresh={t['input_fresh']} "
                  f"cached={t['input_cached']} out={t['output_tokens']} "
                  f"retr={t['retrieval_applied']} input_sav={t['input_savings_pct']:.1f}% "
                  f"total_sav={t['savings_pct']:.1f}%")
        if res["errors"]:
            print(f"  errors: {res['errors']}")
        print(f"  -> {'PASS' if not fails else 'FAIL: ' + '; '.join(fails)}")
        overall_ok &= not fails

    out = Path(__file__).parent / "live_e2e_results.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nresults written to {out}")
    print("OVERALL:", "PASS" if overall_ok else "FAIL")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
