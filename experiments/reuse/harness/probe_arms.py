"""Go/no-go probe: do the arm controls actually move the response usage fields?

Verifying arms from configuration rather than from usage is false-positive item 1. This probe
checks the arms the only way that counts - by reading what the API reported.

Run: python3 -m experiments.reuse.harness.probe_arms
"""

from __future__ import annotations

import json
import sys

from .model import call, new_session_id

MODEL = "claude-haiku-4-5-20251001"
CWD = "/private/tmp"

# Comfortably above the minimum cacheable prefix, and shaped like an agent's context: a body of
# reference material that every turn of the loop re-sends.
PREFIX = "\n".join(
    f"RECORD {i:04d} | account=ACC{i:05d} | region=r{i % 7} | status={'open' if i % 3 else 'closed'} "
    f"| balance={1000 + i * 37} | opened=day{i % 365} | tier={'gold' if i % 5 == 0 else 'standard'}"
    for i in range(420)
)


def turn(n: int) -> str:
    return (
        f"Reference table:\n{PREFIX}\n\n"
        f"Question {n}: how many records have region=r{n % 7} and status=open? "
        "Reply with only the integer."
    )


def run_arm(arm: str) -> dict:
    session = new_session_id()
    turns = []
    for i in range(3):
        result = call(
            turn(i), model=MODEL, arm=arm, cwd=CWD, session_id=session, resume=(i > 0)
        )
        turns.append(
            {
                "turn": i,
                "input": result.input_tokens,
                "cache_creation": result.cache_creation_tokens,
                "cache_read": result.cache_read_tokens,
                "eph_5m": result.ephemeral_5m_tokens,
                "eph_1h": result.ephemeral_1h_tokens,
                "output": result.output_tokens,
                "cache_read_fraction": round(result.cache_read_fraction, 4),
                "model_version": result.model_version,
                "text": result.text[:40],
            }
        )
        print(f"  arm {arm} turn {i}: {json.dumps(turns[-1])}")
    return {"arm": arm, "turns": turns}


def main() -> int:
    print("probing arm A (DISABLE_PROMPT_CACHING=1)")
    arm_a = run_arm("A")
    print("probing arm B (ENABLE_PROMPT_CACHING_1H=1)")
    arm_b = run_arm("B")

    a_cache = sum(t["cache_creation"] + t["cache_read"] for t in arm_a["turns"])
    b_read = sum(t["cache_read"] for t in arm_b["turns"])
    b_1h = sum(t["eph_1h"] for t in arm_b["turns"])
    b_5m = sum(t["eph_5m"] for t in arm_b["turns"])
    later = arm_b["turns"][1:]
    b_frac = (
        sum(t["cache_read"] for t in later)
        / max(1, sum(t["cache_read"] + t["cache_creation"] + t["input"] for t in later))
    )

    checks = {
        "arm A has zero cache activity": a_cache == 0,
        "arm B reads from cache on later turns": b_read > 0,
        "arm B writes to the 1h tier, not the 5m tier": b_1h > 0 and b_5m == 0,
        "arm B cache-read fraction on later turns exceeds 50%": b_frac > 0.50,
    }
    print()
    for name, ok in checks.items():
        print(f"[{'ok  ' if ok else 'FAIL'}] {name}")
    print(f"\narm B cache-read fraction on turns 2-3: {b_frac:.3f}  (1h={b_1h}, 5m={b_5m})")

    with open("experiments/reuse/pilot/arm_probe.json", "w", encoding="utf-8") as handle:
        json.dump(
            {"arm_a": arm_a, "arm_b": arm_b, "checks": checks, "b_read_fraction_later": b_frac},
            handle,
            indent=2,
        )
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
