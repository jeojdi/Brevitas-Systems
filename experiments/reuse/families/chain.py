"""Family: deep chain.

A value is threaded through a sequence of transformation modules, each of which conditions on the
previous step's result. This is the opposite extreme from `wide`: the dependency graph is one long
path, so a change at the root invalidates everything and a change at the last module invalidates
one operation. Running both shapes is what turns the result into a curve rather than a number.

Ground truth is exact and free: the generator computes every intermediate value in Python, so the
grader checks the model's answer against arithmetic rather than against another model.

Arm B gets the strongest structure available here - the whole chain runs as one conversation, so
the accumulating prefix is cached by the provider. That is the configuration a competent engineer
would ship for a sequential agent loop.
"""

from __future__ import annotations

import random

from ..harness.runner import OpSpec

NAME = "chain"
SHAPE = "deep-chain"

MODULUS = 10007

_GUIDANCE = "\n".join(
    f"  G{i:03d}. {text}"
    for i, text in enumerate(
        [
            "Apply exactly one transformation, the one named in the module given to you.",
            "All arithmetic is performed modulo 10007 and the result is always in [0, 10006].",
            "Never apply a transformation from a module you were not given.",
            "Never carry over a constant from an earlier step; use only the constant in this module.",
            "The incoming value is the integer supplied as PREVIOUS VALUE.",
            "For a chain start, the incoming value is the module's seed constant.",
            "Emit the resulting integer and nothing else: no words, no units, no punctuation.",
            "Do not show working. Do not restate the module. Do not explain.",
            "If the module specifies ADD, the result is (previous + constant) mod 10007.",
            "If the module specifies MUL, the result is (previous * constant) mod 10007.",
            "If the module specifies SUB, the result is (previous - constant) mod 10007.",
            "If the module specifies XOR, the result is (previous xor constant) mod 10007.",
        ]
        * 26
    )
)

SYSTEM = f"""You are a deterministic transformation engine in a computation pipeline.

Each step gives you one module and one incoming value. Apply the module's transformation to the
incoming value and emit the result.

RULES

{_GUIDANCE}

OUTPUT FORMAT

A single integer on one line. Nothing else whatsoever."""

_OPS = ("ADD", "MUL", "SUB", "XOR")


def generate(seed: int, size: int) -> dict[str, str]:
    rng = random.Random(f"chain-{seed}")
    artifacts: dict[str, str] = {}
    for i in range(size):
        op = rng.choice(_OPS)
        const = rng.randrange(2, 500)
        # The identifier is deliberately non-numeric and the modulus is deliberately absent.
        # A mutator that hits a module's ID changes the artifact hash without changing any correct
        # answer, which is a perturbation that does not perturb; one that hits the modulus would
        # contradict the answer key, which computes with a fixed modulus. The only mutable number
        # in the artifact is the constant, and changing it genuinely moves every downstream value.
        artifacts[f"module/{i:03d}.mod"] = (
            f"MODULE m{chr(97 + i % 26)}{chr(97 + (i // 26) % 26)}\n"
            f"OPERATION: {op}\nCONSTANT: {const}\n"
        )
    artifacts["module/seed.mod"] = f"SEED CONSTANT: {rng.randrange(1, MODULUS)}\n"
    return artifacts


def _module(text: str) -> tuple[str, int]:
    op = const = None
    for line in text.splitlines():
        if line.startswith("OPERATION:"):
            op = line.split(":", 1)[1].strip()
        elif line.startswith("CONSTANT:"):
            const = int(line.split(":", 1)[1].strip())
    return op or "ADD", const or 0


def _apply(op: str, value: int, const: int) -> int:
    if op == "ADD":
        return (value + const) % MODULUS
    if op == "MUL":
        return (value * const) % MODULUS
    if op == "SUB":
        return (value - const) % MODULUS
    return (value ^ const) % MODULUS


def answer_key(artifacts: dict[str, str]) -> dict[str, object]:
    """Every intermediate value, computed exactly. Free ground truth at every depth."""
    seed_line = artifacts["module/seed.mod"]
    value = int(seed_line.split(":", 1)[1].strip())
    mods = sorted(a for a in artifacts if a.startswith("module/") and a != "module/seed.mod")
    key: dict[str, object] = {}
    for i, name in enumerate(mods):
        op, const = _module(artifacts[name])
        value = _apply(op, value, const)
        key[f"step{i:03d}"] = value
    key["final"] = value
    return key


def plan(artifacts: dict[str, str]) -> list[OpSpec]:
    mods = sorted(a for a in artifacts if a.startswith("module/") and a != "module/seed.mod")
    specs: list[OpSpec] = []
    for i, name in enumerate(mods):
        upstream = [f"step{i - 1:03d}"] if i else []
        reads = [name] if i else [name, "module/seed.mod"]

        def make_prompt(arts: dict[str, str], ups: dict[str, str], n=name, idx=i):
            if idx == 0:
                seed_value = arts["module/seed.mod"].split(":", 1)[1].strip()
                previous = seed_value
            else:
                previous = list(ups.values())[0].strip()
            return f"MODULE:\n{arts[n]}\nPREVIOUS VALUE: {previous}\n\nEmit the resulting integer."

        specs.append(
            OpSpec(
                op_id=f"step{i:03d}",
                kind="model",
                op_type="apply_module",
                args={"module": name, "index": i},
                artifacts=reads,
                upstream=upstream,
                # One conversation for the whole chain: the accumulating prefix is exactly what
                # provider prefix caching is designed to serve, so this is arm B at its best.
                session_group="chain",
                system=SYSTEM,
                prompt_fn=make_prompt,
            )
        )
    if specs:
        specs[-1].is_final = True
    return specs


def grade(outputs: dict[str, str], key: dict[str, object], artifacts: dict[str, str]) -> dict[str, object]:
    mods = sorted(a for a in artifacts if a.startswith("module/") and a != "module/seed.mod")
    correct = 0
    per_step: dict[str, bool] = {}
    for i in range(len(mods)):
        step = f"step{i:03d}"
        raw = (outputs.get(step, "") or "").strip().split()
        try:
            got = int(raw[0]) if raw else None
        except ValueError:
            got = None
        ok = got == key.get(step)
        per_step[step] = ok
        correct += 1 if ok else 0
    final_ok = per_step.get(f"step{len(mods) - 1:03d}", False)
    score = correct / len(mods) if mods else 0.0
    # The task outcome is the final value; per-step accuracy is reported as a diagnostic because
    # an error at step k poisons every step after it and would otherwise be counted many times.
    return {"score": score, "passed": bool(final_ok), "n": len(mods), "correct": correct,
            "final_correct": bool(final_ok), "per_item": per_step}
