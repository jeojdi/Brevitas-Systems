"""Structural prompt parser — a prompt is not one thing.

Compressing a prompt as one undifferentiated blob of words forces a bad tradeoff: to keep the
answer stable you can only compress lightly. But the parts that DRIVE the output — the task, the
constraints, the formatting/style directives, and the examples — are a small fraction of the
tokens. The bulk is usually CONTEXT (background, repeated explanation, duplicate history), which is
exactly the part that can be compressed hard without changing the answer.

So we split a prompt into typed segments and mark only CONTEXT (and unlabeled background prose) as
compressible; everything output-driving is protected byte-for-byte. Order is preserved, so joining
the segments back reproduces the original exactly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

# Roles a prompt segment can play. Only CONTEXT is compressed; the rest shape the output.
TASK = "task"
CONTEXT = "context"
STYLE = "style"
FORMATTING = "formatting"
CONSTRAINTS = "constraints"
EXAMPLES = "examples"
OTHER = "other"

# Only CONTEXT is ever compressed. Everything else — including unknown labeled sections (OTHER) —
# is protected: if we don't recognize a label, we don't gamble on compressing it.
_COMPRESSIBLE_ROLES = {CONTEXT}

_FENCE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)

# Labeled sections: "Audience: AI founders", "Context:\n...". Maps a leading label to a role.
_LABELS = {
    "task": TASK, "instruction": TASK, "instructions": TASK, "objective": TASK, "goal": TASK,
    "question": TASK, "q": TASK, "query": TASK, "ask": TASK,
    "context": CONTEXT, "background": CONTEXT, "history": CONTEXT, "reference": CONTEXT,
    "document": CONTEXT, "notes": CONTEXT,
    "style": STYLE, "tone": STYLE, "voice": STYLE, "audience": STYLE, "persona": STYLE,
    "format": FORMATTING, "formatting": FORMATTING, "output": FORMATTING, "schema": FORMATTING,
    "length": CONSTRAINTS, "platform": CONSTRAINTS, "constraint": CONSTRAINTS,
    "constraints": CONSTRAINTS, "requirements": CONSTRAINTS, "rules": CONSTRAINTS,
    "example": EXAMPLES, "examples": EXAMPLES,
}
_LABEL_RE = re.compile(r"^\s*([A-Za-z][A-Za-z /]{1,30}?)\s*:\s*(.*)$", re.DOTALL)

# High-value directive lines: never compress these, they steer the output directly.
_HIGH_VALUE = re.compile(
    r"\b(json|yaml|xml|markdown|csv|html|table|bullet|snake_case|camelcase|kebab-case|"
    r"never|always|must|do not|don't|only|exactly|verbatim|cite|sources?|"
    r"step[- ]by[- ]step|word[s]?|sentence[s]?|paragraph[s]?|character[s]?|schema)\b",
    re.IGNORECASE,
)
# The imperative ask ("Write a...", "Build me...", "Please summarize..."). A short politeness
# prefix is allowed so "Please explain ..." is still recognized as the task, not bulk context.
_TASK_VERB = re.compile(
    r"^\s*(?:please\s+|kindly\s+|could you\s+|can you\s+|i(?:'d| would)\s+like\s+you\s+to\s+)?"
    r"(write|build|create|make|generate|summar(?:y|ize|ise)|explain|draft|design|"
    r"implement|refactor|translate|classify|extract|answer|produce|compose)\b",
    re.IGNORECASE,
)

_NUM = re.compile(r"\b\d[\d.,]*\b")
_ENTITY = re.compile(r"\b[A-Z][A-Za-z0-9]+\b")
_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b")  # snake_case-ish identifiers
_PARA = re.compile(r"\n\s*\n")

# Prose shorter than this (in words) is treated as a directive/instruction, not bulk context —
# short lines are usually the load-bearing ones ("Output JSON.", "Tone: technical").
_CONTEXT_MIN_WORDS = 12


@dataclass
class Segment:
    role: str
    text: str
    compressible: bool


def _classify_unlabeled(text: str) -> str:
    """Role for a paragraph with no explicit label."""
    stripped = text.strip()
    if _FENCE.fullmatch(stripped):
        return EXAMPLES
    if _TASK_VERB.search(stripped) and len(stripped.split()) <= 60:
        return TASK
    if _HIGH_VALUE.search(stripped) and len(stripped.split()) < _CONTEXT_MIN_WORDS:
        return CONSTRAINTS
    if len(stripped.split()) < _CONTEXT_MIN_WORDS:
        # short, non-directive line — protect it; too little to gain, easy to distort
        return CONSTRAINTS
    return CONTEXT


def _role_for_label(label: str) -> str:
    return _LABELS.get(label.strip().lower(), OTHER)


def parse(text: str) -> List[Segment]:
    """Split `text` into typed, order-preserving segments. Reassembly is exact:
    ``"".join(s.text for s in parse(t)) == t``."""
    if not text:
        return []

    segments: List[Segment] = []
    # First split out fenced code so a label/verb regex never reaches inside it.
    for i, part in enumerate(_FENCE.split(text)):
        if not part:
            continue
        if i % 2 == 1:                       # fenced code block
            segments.append(Segment(EXAMPLES, part, False))
            continue
        segments.extend(_parse_prose(part))
    return segments


def _parse_prose(prose: str) -> List[Segment]:
    """Classify a fence-free prose run, splitting on blank lines then labeled lines."""
    out: List[Segment] = []
    # keep separators so concatenation is lossless: split but re-attach the delimiters
    pieces = re.split(r"(\n\s*\n)", prose)
    for piece in pieces:
        if not piece:
            continue
        if piece.strip() == "":               # pure whitespace separator — attach, no role change
            out.append(Segment(OTHER, piece, False))
            continue
        m = _LABEL_RE.match(piece)
        if m and m.group(1).strip().lower() in _LABELS:
            role = _role_for_label(m.group(1))
            body = m.group(2)
            compressible = role in _COMPRESSIBLE_ROLES and len(body.split()) >= _CONTEXT_MIN_WORDS
            out.append(Segment(role, piece, compressible))
        else:
            role = _classify_unlabeled(piece)
            out.append(Segment(role, piece, role in _COMPRESSIBLE_ROLES))
    return out


def high_value_tokens(text: str) -> List[str]:
    """Tokens that must survive even inside a compressible segment: numbers, entities, and
    snake_case-ish identifiers actually present. Feeds LLMLingua-2 `force_tokens`."""
    seen = dict.fromkeys(
        _NUM.findall(text) + _IDENT.findall(text) + _ENTITY.findall(text)
    )
    return list(seen)[:80]   # cap: large/odd force lists make LLMLingua-2 assert
