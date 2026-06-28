"""Lever 4 — retrieval instead of context-stuffing.

Faithful implementations of two published retrievers, plus a Brevitas fetch wrapper:

1. DPR dual-encoder dense retrieval  (Karpukhin et al., EMNLP 2020, arXiv:2004.04906)
   sim(q, p) = E_Q(q) · E_P(p)  (inner product); top-k by maximum inner product.

2. ColBERTv2 late interaction + residual compression  (Santhanam et al., NAACL 2022,
   arXiv:2112.01488)
   MaxSim:  S_{q,d} = Σ_{i∈q}  max_{j∈d} ( q_i · d_j )
   Residual compression: cluster token embeddings to centroids; store
   (centroid_id + quantized residual) instead of full float vectors (6–10× smaller).

The encoder is injected (any object exposing `.encode(list[str], normalize_embeddings=bool)`),
so tests can use a deterministic local encoder and the benchmark a real sentence-transformer.

Brevitas use (accuracy-first): `fetch_for_hop` returns only the top-k chunks relevant to a
hop, but FAILS SAFE to the full context if the index is empty or retrieval confidence is low.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np


# --------------------------------------------------------------------------- #
# 1. DPR dual-encoder dense retriever
# --------------------------------------------------------------------------- #
class DenseRetriever:
    """DPR-style retriever: score = inner product of query/passage embeddings; top-k MIPS."""

    def __init__(self, encoder, normalize: bool = True):
        self.encoder = encoder
        self.normalize = normalize
        self._emb: Optional[np.ndarray] = None
        self._chunks: List[str] = []
        self._ids: List = []

    def _encode(self, texts: Sequence[str]) -> np.ndarray:
        v = self.encoder.encode(list(texts), normalize_embeddings=self.normalize)
        return np.asarray(v, dtype=np.float32)

    def index(self, chunks: Sequence[str], ids: Optional[Sequence] = None) -> None:
        self._chunks = list(chunks)
        self._ids = list(ids) if ids is not None else list(range(len(self._chunks)))
        self._emb = self._encode(self._chunks) if self._chunks else None

    def retrieve(self, query: str, k: int = 5) -> List[Tuple[object, str, float]]:
        """Return up to k (id, chunk, score) by descending inner product.

        Empty index -> [] (the caller MUST treat this as a fail-safe signal)."""
        if self._emb is None or len(self._chunks) == 0:
            return []
        q = self._encode([query])[0]
        scores = self._emb @ q                       # DPR sim(q, p) = E_Q·E_P
        k = min(k, len(scores))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(self._ids[i], self._chunks[i], float(scores[i])) for i in top]

    # -- persistence so retrieve() never silently returns [] after a reload --- #
    def save(self, path: str) -> None:
        if self._emb is None:
            raise ValueError("nothing indexed")
        np.savez(path + ".npz", emb=self._emb)
        with open(path + ".json", "w") as f:
            json.dump({"chunks": self._chunks, "ids": self._ids}, f)

    def load(self, path: str) -> None:
        self._emb = np.load(path + ".npz")["emb"]
        meta = json.load(open(path + ".json"))
        self._chunks, self._ids = meta["chunks"], meta["ids"]


# --------------------------------------------------------------------------- #
# 2. ColBERTv2 — MaxSim late interaction + residual compression
# --------------------------------------------------------------------------- #
def maxsim(query_tokens: np.ndarray, doc_tokens: np.ndarray) -> float:
    """ColBERTv2 late interaction: S = Σ_i max_j (q_i · d_j)."""
    sim = query_tokens @ doc_tokens.T                # (Nq, Nd)
    return float(sim.max(axis=1).sum())


def _kmeans(emb: np.ndarray, n_centroids: int, iters: int = 10, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = len(emb)
    n_centroids = min(n_centroids, n)
    centroids = emb[rng.choice(n, n_centroids, replace=False)].copy()
    for _ in range(iters):
        d = ((emb[:, None, :] - centroids[None, :, :]) ** 2).sum(-1)
        assign = d.argmin(1)
        for c in range(n_centroids):
            members = emb[assign == c]
            if len(members):
                centroids[c] = members.mean(0)
    return centroids


@dataclass
class ResidualCode:
    assign: np.ndarray          # centroid index per vector
    qresid: np.ndarray          # quantized residual (stored as int8, but only nbits used)
    scale: float                # dequant scale
    nbits: int                  # bits per residual dimension (ColBERTv2 uses 1-2)
    centroids: np.ndarray       # float32[C, d]

    def nbytes(self) -> int:
        n, d = self.qresid.shape
        n_centroids = len(self.centroids)
        assign_bytes = n * (1 if n_centroids <= 256 else 2)          # packed centroid id
        residual_bytes = int(np.ceil(n * d * self.nbits / 8))        # nbits per dim
        centroid_bytes = self.centroids.astype(np.float32).nbytes    # shared, amortized
        return assign_bytes + residual_bytes + centroid_bytes


class ResidualCompressor:
    """ColBERTv2 residual compression: nearest-centroid + b-bit quantized residual.

    `nbits` per residual dimension matches the paper's 1-2 bit regime (default 2);
    nbits=8 gives near-lossless reconstruction at lower compression.
    """

    def __init__(self, n_centroids: int = 256, nbits: int = 2):
        self.n_centroids = n_centroids
        self.nbits = nbits
        self.centroids: Optional[np.ndarray] = None

    def fit(self, emb: np.ndarray) -> "ResidualCompressor":
        self.centroids = _kmeans(emb.astype(np.float32), self.n_centroids)
        return self

    def _qmax(self) -> int:
        return (1 << (self.nbits - 1)) - 1 if self.nbits > 1 else 1

    def encode(self, emb: np.ndarray) -> ResidualCode:
        assert self.centroids is not None, "call fit() first"
        d = ((emb[:, None, :] - self.centroids[None, :, :]) ** 2).sum(-1)
        assign = d.argmin(1).astype(np.int32)
        resid = emb - self.centroids[assign]
        qmax = self._qmax()
        scale = float(np.abs(resid).max()) or 1.0
        qresid = np.clip(np.round(resid / scale * qmax), -qmax, qmax).astype(np.int8)
        return ResidualCode(assign, qresid, scale, self.nbits, self.centroids.astype(np.float32))

    @staticmethod
    def decode(code: ResidualCode) -> np.ndarray:
        qmax = (1 << (code.nbits - 1)) - 1 if code.nbits > 1 else 1
        return code.centroids[code.assign] + code.qresid.astype(np.float32) * (code.scale / qmax)

    @staticmethod
    def full_nbytes(emb: np.ndarray) -> int:
        return emb.astype(np.float32).nbytes


# --------------------------------------------------------------------------- #
# Brevitas fetch wrapper — retrieval with accuracy-first fail-safe
# --------------------------------------------------------------------------- #
@dataclass
class RetrievalConfig:
    k: int = 5
    min_top_score: float = 0.2      # below this, retrieval is "unsure" -> use full context
    fallback_to_full: bool = True


def fetch_for_hop(retriever: DenseRetriever, query: str, full_context: Sequence[str],
                  cfg: RetrievalConfig = RetrievalConfig()) -> Tuple[List[str], dict]:
    """Return the chunks to send to the next hop. Fails safe to full context when the
    index is empty or the top score is below confidence threshold (never silently thin)."""
    hits = retriever.retrieve(query, cfg.k)
    if not hits:
        return list(full_context), {"fallback_applied": True, "reason": "empty_index"}
    if hits[0][2] < cfg.min_top_score and cfg.fallback_to_full:
        return list(full_context), {"fallback_applied": True, "reason": "low_confidence",
                                    "top_score": hits[0][2]}
    chosen = [c for (_, c, _) in hits]
    return chosen, {"fallback_applied": False, "k": len(chosen), "top_score": hits[0][2]}
