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
_ENCODER_LAST_TRIED = 0
_ENCODER_RETRY_DELAY = 300  # retry after 300 seconds (5 minutes) of failures


def _get_encoder():
    """Lazy-load a sentence-transformer; return None if unavailable (-> fail-safe).

    Allows retry after load failure with backoff: if load fails, retry after
    _ENCODER_RETRY_DELAY seconds rather than permanently giving up.
    """
    import time
    global _ENCODER, _ENCODER_LAST_TRIED

    # If we have a cached encoder, return it
    if _ENCODER is not None:
        return _ENCODER

    now = time.time()
    # If we tried recently and failed, wait before retrying
    if _ENCODER_LAST_TRIED > 0 and now - _ENCODER_LAST_TRIED < _ENCODER_RETRY_DELAY:
        return None  # Still in backoff period, return None (fail-safe)

    _ENCODER_LAST_TRIED = now
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
                     min_top_score: float = 0.2, use_adaptive: bool = False) -> dict:
    """Select the prior-context chunks relevant to `task` (Lever 4), fail-safe to full.

    Returns selected_context, baseline/optimized tokens (real tokenizer), savings_pct,
    and fallback metadata. Never raises; on any error returns the full context.

    Args:
        task: the query/question
        prior_context: chunks to retrieve from
        k: fixed k for non-adaptive retrieval (default 5)
        min_top_score: confidence threshold for fail-safe
        use_adaptive: if True, use adaptive-k + MaxSim reranking (validated algorithm);
                      if False, use fixed-k DPR (legacy/default)
    """
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

        if use_adaptive:
            # Use validated algorithm: adaptive-k + MaxSim reranking
            from .retrieval import AdaptiveRetrievalConfig, fetch_adaptive
            cfg = AdaptiveRetrievalConfig(
                max_k=max(10, k),
                min_k=k,
                min_top_score=min_top_score,
                fallback_to_full=True,
                use_maxsim_rerank=True,
            )
            chosen, meta = fetch_adaptive(
                r, task or " ".join(prior_context[:1]), prior_context, enc, cfg
            )
        else:
            # Legacy: fixed-k DPR
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
