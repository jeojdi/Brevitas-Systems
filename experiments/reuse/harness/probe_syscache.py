"""Can arm B get cross-request prefix cache hits through `claude -p`?

The first tuning probe found that a large shared prefix placed in the USER message of a fresh
session produced no cache activity at all - not even a write. Anthropic's own docs say caches are
workspace-scoped and reusable across users and sessions, so that is a limitation of how the CLI
places cache_control breakpoints, not a limitation of the provider.

This matters a great deal. Wide independent workloads (one call per document) are exactly where a
cross-run reuse layer should look best, and if arm B cannot cache across requests there, arm C's
margin would be an artifact of the harness rather than a property of the idea. That is
false-positive item 1, the single easiest way to manufacture a win.

Anthropic's render order is tools -> system -> messages, so stable content belongs in the system
prompt. This probe tests whether a large shared prefix in --system-prompt is cached and read
across SEPARATE sessions.

Run: python3 -m experiments.reuse.harness.probe_syscache
"""

from __future__ import annotations

import json

from .model import call, new_session_id
from . import model as model_mod

MODEL = "claude-haiku-4-5-20251001"  # 4096-token minimum cacheable prefix
CWD = "/private/tmp"
N = 5

# Comfortably over the 4,096-token floor for this model.
SHARED_PREFIX = "\n".join(
    f"  rule_{i:03d}: when field {i} is absent, emit null; when ambiguous, prefer the first "
    f"occurrence in document order; never infer a value that is not literally present."
    for i in range(400)
)


def doc(i: int) -> str:
    return (
        f"INVOICE {i:05d}\nVendor: Northgate Supply Company\n"
        f"Total: {14200 + i * 13} GBP\nLine items: {3 + i % 4}\nTerms: net {15 + (i % 3) * 15}\n"
    )


def run(name: str, system: str) -> dict:
    original = model_mod.SYSTEM_PROMPT
    model_mod.SYSTEM_PROMPT = system
    try:
        turns = []
        for i in range(N):
            r = call(
                "Extract vendor, total and terms as compact JSON.\n\n" + doc(i),
                model=MODEL,
                arm="B",
                cwd=CWD,
                session_id=new_session_id(),  # a genuinely separate session every time
            )
            turns.append(
                {
                    "i": i,
                    "input": r.input_tokens,
                    "creation": r.cache_creation_tokens,
                    "read": r.cache_read_tokens,
                    "eph_1h": r.ephemeral_1h_tokens,
                    "output": r.output_tokens,
                }
            )
            print(f"  {name}[{i}]: {json.dumps(turns[-1])}")
    finally:
        model_mod.SYSTEM_PROMPT = original

    later = turns[1:]
    billable = sum(t["input"] + t["creation"] + t["read"] for t in later)
    return {
        "config": name,
        "turns": turns,
        "read_fraction_after_first": (sum(t["read"] for t in later) / billable) if billable else 0.0,
        "total_billable_input": sum(t["input"] + t["creation"] + t["read"] for t in turns),
    }


def main() -> int:
    print("large shared prefix in --system-prompt, fresh session per request")
    sys_big = run("system_prefix", "You extract invoice fields.\n\nRULES:\n" + SHARED_PREFIX)

    print("same prefix in the user message, fresh session per request (control)")
    original = model_mod.SYSTEM_PROMPT
    turns = []
    for i in range(N):
        r = call(
            "RULES:\n" + SHARED_PREFIX + "\n\nExtract vendor, total and terms as compact JSON.\n\n" + doc(i),
            model=MODEL, arm="B", cwd=CWD, session_id=new_session_id(),
        )
        turns.append({"i": i, "input": r.input_tokens, "creation": r.cache_creation_tokens,
                      "read": r.cache_read_tokens, "output": r.output_tokens})
        print(f"  user_prefix[{i}]: {json.dumps(turns[-1])}")
    model_mod.SYSTEM_PROMPT = original
    later = turns[1:]
    billable = sum(t["input"] + t["creation"] + t["read"] for t in later)
    user_prefix = {
        "config": "user_prefix",
        "turns": turns,
        "read_fraction_after_first": (sum(t["read"] for t in later) / billable) if billable else 0.0,
        "total_billable_input": sum(t["input"] + t["creation"] + t["read"] for t in turns),
    }

    results = [sys_big, user_prefix]
    print()
    for r in results:
        print(
            f"{r['config']:16s} read_fraction_after_first={r['read_fraction_after_first']:.3f} "
            f"billable_input={r['total_billable_input']:7d}"
        )

    works = sys_big["read_fraction_after_first"] > 0.30
    print(
        "\nCROSS-REQUEST CACHING VIA SYSTEM PROMPT: "
        + ("WORKS - arm B can be tuned properly on wide workloads" if works
           else "DOES NOT WORK through this CLI path - arm B would be under-tuned on wide "
                "workloads and the report must say so")
    )
    with open("experiments/reuse/pilot/syscache.json", "w", encoding="utf-8") as h:
        json.dump({"results": results, "cross_request_caching_works": works}, h, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
