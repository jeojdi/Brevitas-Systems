"""Family: wide independent extraction.

Structured-field extraction over a corpus of generated records, no cross-document dependency.
This is the shape most favourable to a naive reuse layer - every operation is an independent
leaf, so a change to one document invalidates exactly one operation and nothing else.

Ground truth is exact and free because the documents are generated from seeded templates: the
generator knows the answer by construction, so the grader never needs a human or an LLM.

Arm B is tuned as hard as the provider allows: the whole field specification lives in the system
prompt, which is byte-identical across every operation and therefore served from cache at the
0.1x read rate rather than rewritten at the 2.0x write rate. The specification is deliberately
long enough to clear the largest minimum cacheable prefix in the model line-up (4,096 tokens for
haiku-4-5), because below that floor a request silently does not cache at all.
"""

from __future__ import annotations

import json
import random

from ..harness.runner import OpSpec

NAME = "wide"
SHAPE = "wide-independent"

VENDORS = [
    "Northgate Supply Company", "Ashford Industrial Ltd", "Pellworth Trading",
    "Caldmore Logistics", "Brackenhill Components", "Yardley Fabrication",
    "Denholm Materials", "Ravensworth Freight", "Kingsmoor Utilities", "Thornbury Optics",
]
CURRENCIES = ["GBP", "EUR", "USD", "CAD"]
STATUSES = ["approved", "pending", "disputed", "settled"]

_RULES = "\n".join(
    f"  R{i:03d}. {text}"
    for i, text in enumerate(
        [
            "A field that is absent from the record is emitted as null, never as an empty string.",
            "Numeric fields are emitted as integers with no thousands separators and no currency symbol.",
            "When a value appears more than once, prefer the first occurrence in document order.",
            "Never infer a value that is not literally present in the record text.",
            "Whitespace inside a text value is collapsed to single spaces and trimmed at both ends.",
            "Case is preserved exactly as it appears in the record for all text fields.",
            "A value that is present but unparseable as its declared type is emitted as null.",
            "Currency codes are emitted as the three-letter code exactly as written in the record.",
            "The status field is emitted in lower case regardless of how it appears in the record.",
            "Line item counts are the number of distinct item lines, not the sum of quantities.",
        ]
        * 30
    )
)

SYSTEM = f"""You extract structured fields from procurement records.

FIELD SPECIFICATION

  vendor        the legal entity issuing the record, verbatim
  total         the record total as written, integer, no separators and no currency symbol
  currency      ISO 4217 three-letter code exactly as written
  items         the number of distinct line items
  terms         payment terms in days, 0 when due on receipt
  status        one of: approved, pending, disputed, settled, lower case

EXTRACTION RULES

{_RULES}

OUTPUT FORMAT

A single line of compact JSON with exactly the six keys above, in the order above, no
whitespace between tokens, and nothing else. No preamble, no code fence, no commentary."""


def generate(seed: int, size: int) -> dict[str, str]:
    """Deterministic corpus. The same seed produces byte-identical documents."""
    rng = random.Random(f"wide-{seed}")
    artifacts: dict[str, str] = {}
    for i in range(size):
        vendor = rng.choice(VENDORS)
        total = rng.randrange(1000, 999999)
        currency = rng.choice(CURRENCIES)
        items = rng.randrange(1, 24)
        terms = rng.choice([0, 15, 30, 45, 60, 90])
        status = rng.choice(STATUSES)
        artifacts[f"doc/{i:04d}.txt"] = (
            f"PROCUREMENT RECORD {i:05d}\n"
            f'Issued by: "{vendor}"\n'
            f"Record total: {total} {currency}\n"
            f"Distinct line items: {items}\n"
            f"Payment terms: net {terms}\n"
            f"Approval status: {status}\n"
        )
    return artifacts


def _parse(text: str) -> dict[str, object]:
    """Read the ground truth back out of a generated document. Pure string handling, no model."""
    fields: dict[str, object] = {}
    for line in text.splitlines():
        if line.startswith("Issued by:"):
            fields["vendor"] = line.split('"')[1]
        elif line.startswith("Record total:"):
            parts = line.split(":", 1)[1].split()
            fields["total"] = int(parts[0])
            fields["currency"] = parts[1]
        elif line.startswith("Distinct line items:"):
            fields["items"] = int(line.split(":", 1)[1].strip())
        elif line.startswith("Payment terms:"):
            fields["terms"] = int(line.split("net", 1)[1].strip())
        elif line.startswith("Approval status:"):
            fields["status"] = line.split(":", 1)[1].strip().lower()
    return fields


def answer_key(artifacts: dict[str, str]) -> dict[str, object]:
    return {name: _parse(text) for name, text in sorted(artifacts.items()) if name.startswith("doc/")}


def plan(artifacts: dict[str, str]) -> list[OpSpec]:
    docs = sorted(a for a in artifacts if a.startswith("doc/"))
    specs: list[OpSpec] = []
    for i, doc in enumerate(docs):
        specs.append(
            OpSpec(
                op_id=f"extract{i:04d}",
                kind="model",
                op_type="extract_fields",
                args={"doc": doc, "spec_version": 1},
                artifacts=[doc],
                upstream=[],
                session_group=None,  # independent calls; the shared system prompt is the cache
                system=SYSTEM,
                prompt_fn=lambda arts, _ups, d=doc: f"RECORD TO EXTRACT:\n{arts[d]}",
            )
        )
    specs.append(
        OpSpec(
            op_id="ledger",
            kind="tool",
            op_type="assemble_ledger",
            args={"n": len(docs)},
            artifacts=[],
            upstream=[f"extract{i:04d}" for i in range(len(docs))],
            tool_fn=lambda _arts, ups: json.dumps(
                {k: v for k, v in sorted(ups.items())}, sort_keys=True
            ),
            is_final=True,
        )
    )
    return specs


def grade(outputs: dict[str, str], key: dict[str, object], artifacts: dict[str, str]) -> dict[str, object]:
    """Deterministic. A document passes only if all six fields match the generated truth."""
    docs = sorted(a for a in artifacts if a.startswith("doc/"))
    correct = 0
    per_doc: dict[str, bool] = {}
    for i, doc in enumerate(docs):
        raw = outputs.get(f"extract{i:04d}", "")
        try:
            got = json.loads(raw.strip().strip("`").removeprefix("json").strip())
        except (json.JSONDecodeError, AttributeError):
            per_doc[doc] = False
            continue
        want = key[doc]
        ok = all(str(got.get(f, "")).strip().lower() == str(want.get(f, "")).strip().lower()
                 for f in ("vendor", "total", "currency", "items", "terms", "status"))
        per_doc[doc] = ok
        correct += 1 if ok else 0
    score = correct / len(docs) if docs else 0.0
    return {"score": score, "passed": score >= 0.90, "n": len(docs), "correct": correct,
            "per_item": per_doc}


def mutate(text: str, rng) -> tuple[str, str] | None:
    """Family-specific perturbation verified to change an extracted field.

    The generic number mutator can land on the record identifier in the header line, which changes
    the content hash while changing none of the six extracted fields - a perturbation that does not
    perturb. Two such inert perturbations appeared in the 30% cells of the warm matrix. Here the
    edit is confined to the six fields the grader reads, and is checked against the family's own
    parser before being returned.
    """
    before = _parse(text)
    lines = text.splitlines()
    edits: list[tuple[list[str], str]] = []
    for i, line in enumerate(lines):
        n = rng.randrange(1, 999999)
        if line.startswith("Issued by:"):
            new = rng.choice([v for v in VENDORS if v not in line])
            edits.append((lines[:i] + [f'Issued by: "{new}"'] + lines[i + 1:], f"vendor -> {new}"))
        elif line.startswith("Record total:"):
            cur = line.split(":", 1)[1].split()
            newc = rng.choice([c for c in CURRENCIES if c != cur[1]])
            edits.append((lines[:i] + [f"Record total: {n} {cur[1]}"] + lines[i + 1:], f"total -> {n}"))
            edits.append((lines[:i] + [f"Record total: {cur[0]} {newc}"] + lines[i + 1:], f"currency -> {newc}"))
        elif line.startswith("Distinct line items:"):
            v = rng.randrange(1, 24)
            edits.append((lines[:i] + [f"Distinct line items: {v}"] + lines[i + 1:], f"items -> {v}"))
        elif line.startswith("Payment terms:"):
            v = rng.choice([0, 15, 30, 45, 60, 90])
            edits.append((lines[:i] + [f"Payment terms: net {v}"] + lines[i + 1:], f"terms -> net {v}"))
        elif line.startswith("Approval status:"):
            v = rng.choice([s for s in STATUSES if s not in line])
            edits.append((lines[:i] + [f"Approval status: {v}"] + lines[i + 1:], f"status -> {v}"))

    rng.shuffle(edits)
    for new_lines, detail in edits:
        candidate = "\n".join(new_lines) + "\n"
        if _parse(candidate) != before:
            return candidate, detail + " [verified to change an extracted field]"
    return None
