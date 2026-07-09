"""Lightweight semantic quality gate for lossy compression.

After LLMLingua-2 drops tokens we verify the compressed text still MEANS the same thing: embed
the original and the compressed prompt with the same MiniLM encoder already used for retrieval
(`api_adapter._get_encoder`) and compare cosine similarity. If it falls below a threshold the
compression is rejected and the caller falls back to the original prompt — so a meaning-degrading
compression can never silently ship.

Fail-open on infra: if the encoder can't load we can't measure, so we DON'T block — behaviour is
identical to having no gate. The gate only ever adds a rejection when it has positive evidence of
drift. Threshold is `BREVITAS_QUALITY_MIN_SIM` (default 0.75); set it to 0 to disable the gate.

Note on where this is applied: the message optimizer applies this floor PER SENTENCE of the Context
(not over the whole prompt), backed by a per-sentence information-density check that guarantees the
sentence's numbers/entities survive regardless of this value. So the semantic floor only governs how
much the context PROSE may be paraphrased: 0.75 allows more paraphrase (more savings, ~45% on a RAG
corpus, facts still fully retained); raise toward 0.9 for near-original phrasing on wording-sensitive
tasks (~29% savings).
"""

from __future__ import annotations

import os
from typing import Optional

_DEFAULT_MIN_SIM = 0.75


def min_similarity() -> float:
    """Cosine-similarity floor below which a compression is rejected. 0 disables the gate."""
    try:
        return float(os.getenv("BREVITAS_QUALITY_MIN_SIM", _DEFAULT_MIN_SIM))
    except ValueError:
        return _DEFAULT_MIN_SIM


def gate_enabled() -> bool:
    return min_similarity() > 0.0


def semantic_similarity(a: str, b: str) -> Optional[float]:
    """Cosine similarity of `a` and `b` under the shared MiniLM encoder (embeddings are
    L2-normalised, so the dot product IS cosine). Returns None if it can't be measured."""
    if not a or not b:
        return None
    try:
        import numpy as np

        from .api_adapter import _get_encoder
        enc = _get_encoder()
        if enc is None:
            return None
        vecs = enc.encode([a, b])          # normalized 384-dim vectors
        return float(np.dot(vecs[0], vecs[1]))
    except Exception:
        return None
