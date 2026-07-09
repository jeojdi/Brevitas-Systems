"""Information-Density metric — score what a compression RETAINED, not how many tokens it cut.

Raw "% tokens saved" is the wrong target: a compression that cuts 40% but drops a constraint or a
number has lost the plot. What matters is that the load-bearing information survives. This scores
retention per class — numbers, entities, constraints/directives, formatting, examples, and the
task verb — between the original and compressed text. The compression is only accepted when the
CRITICAL classes are essentially fully retained; otherwise the caller backs off to a lighter
compression. The goal: keep information ~99% while cutting tokens.
"""

from __future__ import annotations

import os
import re
from typing import Dict

from .prompt_structure import _ENTITY, _FENCE, _HIGH_VALUE, _IDENT, _NUM, _TASK_VERB

# Classes that must survive for a compression to be acceptable. Style/entities can drift a little
# (paraphrase), but a dropped number, constraint, format directive, or the task itself is a fail.
_CRITICAL = ("numbers", "constraints", "formatting", "task")


def _ratio(originals, compressed_text: str) -> float:
    """Fraction of the original items (as a set) that still appear in the compressed text."""
    items = set(originals)
    if not items:
        return 1.0
    kept = sum(1 for it in items if it in compressed_text)
    return kept / len(items)


def information_density(original: str, compressed: str, min_retain: float | None = None) -> Dict:
    """Retention ratios per information class + an overall accept flag.

    Returns {numbers, entities, constraints, formatting, examples, task, overall_ok, min_retain}.
    `overall_ok` is True only when every CRITICAL class is retained at >= min_retain
    (default 0.99, override via BREVITAS_INFO_DENSITY_MIN).
    """
    if min_retain is None:
        try:
            min_retain = float(os.getenv("BREVITAS_INFO_DENSITY_MIN", "0.99"))
        except ValueError:
            min_retain = 0.99

    numbers = _ratio(_NUM.findall(original), compressed)
    entities = _ratio(_ENTITY.findall(original) + _IDENT.findall(original), compressed)
    constraints = _ratio(_HIGH_VALUE.findall(original), compressed)
    # formatting = the exact directive phrases (json/markdown/snake_case/…) — same source as
    # constraints here, kept as its own axis so callers can weight it independently.
    formatting = constraints
    examples = _ratio(_FENCE.findall(original), compressed)
    task = 1.0 if not _TASK_VERB.search(original) else (1.0 if _TASK_VERB.search(compressed) else 0.0)

    scores = {"numbers": round(numbers, 4), "entities": round(entities, 4),
              "constraints": round(constraints, 4), "formatting": round(formatting, 4),
              "examples": round(examples, 4), "task": round(task, 4)}
    overall_ok = all(scores[c] >= min_retain for c in _CRITICAL)
    return {**scores, "overall_ok": overall_ok, "min_retain": min_retain}
