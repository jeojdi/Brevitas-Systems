"""Thin adapter so the API can use the validated lossless levers.

Exposes retrieval-based context reduction (Lever 4) with an accuracy-first fail-safe:
if the embedding model can't load, or retrieval confidence is low, the FULL context is
returned (never silently thinned). Savings are measured with the real tokenizer, never a
proxy. The heavy embedding model is lazy-loaded once per process.
"""

from __future__ import annotations

from typing import List, Optional

from .provider_cache import count_tokens
from .retrieval import DenseRetriever, RetrievalConfig, fetch_for_hop

_ENCODER = None
_ENCODER_TRIED = False


def _get_encoder():
    """Lazy-load a sentence-transformer; return None if unavailable (-> fail-safe)."""
    global _ENCODER, _ENCODER_TRIED
    if _ENCODER_TRIED:
        return _ENCODER
    _ENCODER_TRIED = True
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

        class _Enc:
            def encode(self, texts, normalize_embeddings=True):
                return model.encode(texts, normalize_embeddings=normalize_embeddings,
                                    show_progress_bar=False, batch_size=64)

        _ENCODER = _Enc()
    except Exception:
        _ENCODER = None
    return _ENCODER


def retrieval_select(task: str, prior_context: List[str], k: int = 5,
                     min_top_score: float = 0.2) -> dict:
    """Select the prior-context chunks relevant to `task` (Lever 4), fail-safe to full.

    Returns selected_context, baseline/optimized tokens (real tokenizer), savings_pct,
    and fallback metadata. Never raises; on any error returns the full context."""
    baseline = sum(count_tokens(c) for c in prior_context)
    if not prior_context:
        return {"selected_context": [], "baseline_tokens": 0, "optimized_tokens": 0,
                "savings_pct": 0.0, "fallback_applied": False, "reason": "empty_context"}

    enc = _get_encoder()
    if enc is None:
        return {"selected_context": list(prior_context), "baseline_tokens": baseline,
                "optimized_tokens": baseline, "savings_pct": 0.0,
                "fallback_applied": True, "reason": "encoder_unavailable"}
    try:
        r = DenseRetriever(enc)
        r.index(prior_context)
        chosen, meta = fetch_for_hop(
            r, task or " ".join(prior_context[:1]), prior_context,
            RetrievalConfig(k=k, min_top_score=min_top_score, fallback_to_full=True),
        )
    except Exception as e:
        return {"selected_context": list(prior_context), "baseline_tokens": baseline,
                "optimized_tokens": baseline, "savings_pct": 0.0,
                "fallback_applied": True, "reason": f"error:{type(e).__name__}"}

    optimized = sum(count_tokens(c) for c in chosen)
    savings = round(max(0.0, (1 - optimized / max(1, baseline)) * 100), 2)
    return {"selected_context": chosen, "baseline_tokens": baseline,
            "optimized_tokens": optimized, "savings_pct": savings,
            "fallback_applied": meta.get("fallback_applied", False),
            "reason": meta.get("reason", "retrieved")}
