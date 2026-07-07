"""
Local sentence-embedding for the semantic response cache.

Runs a small CPU model (bge-small-en-v1.5, ~130 MB) so prompt text never leaves
the proxy and there is no per-embedding API cost. Optional: if sentence-transformers
is not installed (the base install), embed() returns None and the semantic cache
degrades to exact-hash matching only — nothing breaks, you just miss reworded hits.

Install the semantic layer:  pip install "brevitas-systems[semanticcache]"
"""
from __future__ import annotations

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_model = None
_load_failed = False


def available() -> bool:
    """True if the embedding model can be loaded (semantic layer is usable)."""
    return _get_model() is not None


def _get_model():
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    except Exception:
        # library absent or model unavailable — disable the semantic layer quietly
        _load_failed = True
        _model = None
    return _model


def embed(text: str):
    """Return a normalized float32 numpy vector for `text`, or None if unavailable.

    Normalized so cosine similarity == dot product at compare time.
    """
    if not text:
        return None
    model = _get_model()
    if model is None:
        return None
    try:
        return model.encode(text, normalize_embeddings=True).astype("float32")
    except Exception:
        return None
