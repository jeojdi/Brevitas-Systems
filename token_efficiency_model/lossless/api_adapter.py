"""Thin adapter so the API can use the validated lossless levers.

Exposes retrieval-based context reduction (Lever 4) with an accuracy-first fail-safe:
if the embedding model can't load, or retrieval confidence is low, the FULL context is
returned (never silently thinned). Savings are measured with the real tokenizer, never a
proxy. The heavy embedding model is lazy-loaded once per process.
"""

from __future__ import annotations

import re
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
        import numpy as np
        from fastembed import TextEmbedding

        # ONNX MiniLM — same 384-dim embeddings as sentence-transformers, without torch
        # (keeps the API image light enough to run on a standard container).
        model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")

        class _Enc:
            def encode(self, texts, normalize_embeddings=True):
                single = isinstance(texts, str)
                docs = [texts] if single else list(texts)
                vecs = np.asarray(list(model.embed(docs)), dtype=np.float32)
                if normalize_embeddings:
                    vecs = vecs / np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12, None)
                return vecs[0] if single else vecs

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


# --------------------------------------------------------------------------- #
# Chunk-level retrieval — lets retrieval slice INTO a large single document
# (e.g. a whole PDF/textbook in one message), not just keep/drop whole messages.
# --------------------------------------------------------------------------- #
_PARA = re.compile(r"\n\s*\n")
_SENT = re.compile(r"(?<=[.!?])\s+")


def chunk_text(text: str, target_tokens: int = 256) -> List[str]:
    """Split text into ~target_tokens chunks on paragraph (then sentence) boundaries.

    Greedy packing: paragraphs are packed until the budget is hit; paragraphs that are
    themselves larger than the budget are first broken on sentence boundaries. Lossless —
    concatenating the chunks back reproduces the same content (modulo separator whitespace)."""
    if not text or not text.strip():
        return []
    units: List[str] = []
    for para in _PARA.split(text):
        para = para.strip()
        if not para:
            continue
        if count_tokens(para) > target_tokens * 1.5:
            units.extend(s.strip() for s in _SENT.split(para) if s.strip())
        else:
            units.append(para)

    chunks: List[str] = []
    cur: List[str] = []
    cur_tok = 0
    for u in units:
        ut = count_tokens(u)
        if cur and cur_tok + ut > target_tokens:
            chunks.append("\n\n".join(cur))
            cur, cur_tok = [], 0
        cur.append(u)
        cur_tok += ut
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def select_chunk_indices(task: str, chunks: List[str], k: int = 12,
                         min_top_score: float = 0.2) -> dict:
    """Return the indices of the chunks relevant to `task`, in original order.

    Fail-safe: if the encoder is unavailable, the index is empty, or top score is below
    confidence, returns ALL indices with fallback_applied=True (never silently thins)."""
    n = len(chunks)
    if n == 0:
        return {"indices": [], "fallback_applied": False, "reason": "empty"}
    enc = _get_encoder()
    if enc is None:
        return {"indices": list(range(n)), "fallback_applied": True,
                "reason": "encoder_unavailable"}
    try:
        r = DenseRetriever(enc)
        r.index(chunks)  # ids default to range(n) -> hit[0] is the chunk index
        hits = r.retrieve(task or chunks[0], k=min(k, n))
    except Exception as e:
        return {"indices": list(range(n)), "fallback_applied": True,
                "reason": f"error:{type(e).__name__}"}
    if not hits:
        return {"indices": list(range(n)), "fallback_applied": True, "reason": "empty_index"}
    if hits[0][2] < min_top_score:
        return {"indices": list(range(n)), "fallback_applied": True,
                "reason": "low_confidence", "top_score": float(hits[0][2])}
    idx = sorted(int(h[0]) for h in hits)
    return {"indices": idx, "fallback_applied": False, "k": len(idx),
            "top_score": float(hits[0][2])}
