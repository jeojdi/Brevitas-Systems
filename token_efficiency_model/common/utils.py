import re
from typing import List


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str) -> List[str]:
    text = normalize_whitespace(text)
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [chunk.strip() for chunk in parts if chunk.strip()]


def lexical_overlap(a: str, b: str) -> float:
    a_terms = set(re.findall(r"[a-zA-Z0-9_]+", a.lower()))
    b_terms = set(re.findall(r"[a-zA-Z0-9_]+", b.lower()))
    if not a_terms or not b_terms:
        return 0.0
    inter = len(a_terms & b_terms)
    union = len(a_terms | b_terms)
    return inter / max(1, union)
