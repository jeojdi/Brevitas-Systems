"""Lever 4 — retrieval instead of context-stuffing.

Retrieval primitives used by Brevitas, plus an accuracy-first fetch wrapper:

1. DPR dual-encoder dense retrieval  (Karpukhin et al., EMNLP 2020, arXiv:2004.04906)
   sim(q, p) = E_Q(q) · E_P(p)  (inner product); top-k by maximum inner product.

2. ColBERTv2 late interaction + residual compression  (Santhanam et al., NAACL 2022,
   arXiv:2112.01488)
   MaxSim:  S_{q,d} = Σ_{i∈q}  max_{j∈d} ( q_i · d_j )
   Residual compression: cluster token embeddings to centroids; store
   (centroid_id + quantized residual) instead of full float vectors (6–10× smaller).

3. BM25 sparse retrieval + Reciprocal Rank Fusion (Cormack et al., SIGIR 2009),
   which recovers exact names, identifiers, and dates that a small dense encoder can miss.

4. A bounded second evidence hop.  When a highly ranked passage explicitly references the
   title of another available passage, that linked passage is protected in the final set.  This
   is a deterministic, no-extra-LLM approximation of iterative multi-hop retrieval.

The encoder is injected (any object exposing `.encode(list[str], normalize_embeddings=bool)`),
so tests can use a deterministic local encoder and the benchmark a real sentence-transformer.

Brevitas use (accuracy-first): `fetch_for_hop` returns only the top-k chunks relevant to a
hop, but FAILS SAFE to the full context if the index is empty or retrieval confidence is low.
"""

from __future__ import annotations

import html
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

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
# 2. BM25 sparse retrieval + rank fusion
# --------------------------------------------------------------------------- #
_LEXICAL_TOKEN = re.compile(r"[a-z0-9]+")
_LEXICAL_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "both", "by",
    "did", "do", "does", "during", "for", "from", "had", "has", "have", "how",
    "i", "in", "into", "is", "it", "its", "of", "on", "or", "that", "the",
    "their", "this", "to", "was", "were", "what", "when", "where", "which",
    "who", "whom", "why", "with",
})


def _lexical_tokens(text: str, *, drop_stopwords: bool = False) -> List[str]:
    tokens = _LEXICAL_TOKEN.findall((text or "").casefold())
    if drop_stopwords:
        return [token for token in tokens if token not in _LEXICAL_STOPWORDS]
    return tokens


class BM25Retriever:
    """Small in-process BM25 retriever for the per-request context collection.

    Brevitas normally ranks tens or hundreds of already supplied context blocks, so a local
    index avoids another service and lets exact lexical evidence complement dense semantics.
    The scoring equation is Robertson BM25 with the common ``k1=1.2, b=0.75`` defaults.
    """

    def __init__(self, k1: float = 1.2, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._chunks: List[str] = []
        self._ids: List = []
        self._tokens: List[List[str]] = []
        self._doc_freq: Counter = Counter()
        self._avg_len = 0.0

    def index(self, chunks: Sequence[str], ids: Optional[Sequence] = None) -> None:
        self._chunks = list(chunks)
        self._ids = list(ids) if ids is not None else list(range(len(self._chunks)))
        self._tokens = [_lexical_tokens(chunk) for chunk in self._chunks]
        self._doc_freq = Counter(token for tokens in self._tokens for token in set(tokens))
        self._avg_len = (
            sum(len(tokens) for tokens in self._tokens) / len(self._tokens)
            if self._tokens else 0.0
        )

    def retrieve(self, query: str, k: int = 5) -> List[Tuple[object, str, float]]:
        if not self._chunks or k <= 0:
            return []
        query_tokens = _lexical_tokens(query, drop_stopwords=True)
        if not query_tokens:
            return []
        n_docs = len(self._chunks)
        scores = np.zeros(n_docs, dtype=np.float32)
        avg_len = max(1.0, self._avg_len)
        for i, doc_tokens in enumerate(self._tokens):
            counts = Counter(doc_tokens)
            length_norm = 1.0 - self.b + self.b * len(doc_tokens) / avg_len
            score = 0.0
            for token in query_tokens:
                frequency = counts[token]
                if frequency == 0:
                    continue
                doc_frequency = self._doc_freq[token]
                inverse_doc_frequency = math.log(
                    1.0 + (n_docs - doc_frequency + 0.5) / (doc_frequency + 0.5)
                )
                score += inverse_doc_frequency * (
                    frequency * (self.k1 + 1.0)
                ) / (frequency + self.k1 * length_norm)
            scores[i] = score
        limit = min(k, n_docs)
        # Stable full sort is intentional: context collections are small, and deterministic
        # ties matter for reproducible quality experiments.
        ranked = np.argsort(-scores, kind="stable")[:limit]
        return [(self._ids[i], self._chunks[i], float(scores[i])) for i in ranked]


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[Tuple[object, str, float]]],
    rank_constant: int = 60,
) -> List[Tuple[object, str, float]]:
    """Fuse heterogeneous rankings without pretending their raw scores are comparable."""
    scores: Dict[object, float] = {}
    chunks: Dict[object, str] = {}
    first_seen: Dict[object, int] = {}
    seen_counter = 0
    for ranking in rankings:
        for rank, (chunk_id, chunk, _score) in enumerate(ranking, start=1):
            if chunk_id not in first_seen:
                first_seen[chunk_id] = seen_counter
                seen_counter += 1
            chunks[chunk_id] = chunk
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rank_constant + rank)
    ordered_ids = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], first_seen[chunk_id]))
    return [(chunk_id, chunks[chunk_id], scores[chunk_id]) for chunk_id in ordered_ids]


# --------------------------------------------------------------------------- #
# 3. ColBERTv2 — MaxSim late interaction + residual compression
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
# 3. ColBERTv2 MaxSim reranker — late-interaction top-N to final-k pruning
# --------------------------------------------------------------------------- #
class MaxSimReranker:
    """Rerank top-N DPR results using a sentence-level MaxSim heuristic.

    This is not a trained ColBERTv2 reranker: the injected sentence encoder does not expose
    contextual token embeddings.  The operator remains useful for experiments and backwards
    compatibility, but production quality-first retrieval uses dense+sparse fusion below.
    """

    def __init__(self, encoder, normalize: bool = True):
        self.encoder = encoder
        self.normalize = normalize

    def _tokenize_to_sentences(self, text: str) -> List[str]:
        """Simple sentence segmentation for late-interaction approximation."""
        # Split on common sentence boundaries
        sents = re.split(r'(?<=[.!?])\s+', text.strip())
        return [s.strip() for s in sents if s.strip()]

    def _maxsim_score(self, query: str, passage: str) -> float:
        """MaxSim: sum of max token-similarity per query token. Sentence-level approx."""
        q_sents = self._tokenize_to_sentences(query)
        p_sents = self._tokenize_to_sentences(passage)
        if not q_sents or not p_sents:
            return 0.0
        q_emb = self.encoder.encode(q_sents, normalize_embeddings=self.normalize)
        p_emb = self.encoder.encode(p_sents, normalize_embeddings=self.normalize)
        return maxsim(np.asarray(q_emb, dtype=np.float32),
                      np.asarray(p_emb, dtype=np.float32))

    def rerank(self, query: str, candidates: List[Tuple[object, str, float]],
               top_k: int) -> List[Tuple[object, str, float]]:
        """Rerank top_k from candidates by MaxSim score (without re-encoding passages).

        Keeps DPR ranking as tiebreaker; returns at most top_k results."""
        if not candidates or top_k <= 0:
            return []
        # Recompute MaxSim scores for all candidates
        scored = []
        for idx, (cid, chunk, dpr_score) in enumerate(candidates):
            maxsim_score = self._maxsim_score(query, chunk)
            scored.append((cid, chunk, maxsim_score, dpr_score, idx))  # DPR score as tiebreaker
        # Sort by MaxSim, then DPR (to preserve relative order among tied candidates)
        scored.sort(key=lambda x: (-x[2], -x[3]))
        # Return top_k with combined score
        return [(cid, chunk, maxsim_score) for cid, chunk, maxsim_score, _, _ in scored[:top_k]]


# --------------------------------------------------------------------------- #
# 4. Adaptive-k: find score elbow with min_k safety baseline
# --------------------------------------------------------------------------- #
@dataclass
class AdaptiveRetrievalConfig:
    """Adaptive retrieval: find the score "elbow" where relevance drops sharply.

    Strategy:
      1. Retrieve top-N (e.g., 10) DPR results
      2. Rerank top-N with MaxSim (optional)
      3. Find the largest gap in scores (the "knee" / elbow point)
      4. Keep max(min_k, elbow_k) passages, respecting max_k cap
      5. Fail-safe to full context if top-k confidence is too low

    Rationale: The elbow method is robust for normalized embeddings where absolute
    score thresholds don't work well. Large gaps indicate diminishing returns.
    min_k ensures we never drop below a safe recall baseline.
    """
    max_k: int = 10                 # never retrieve more than this
    min_k: int = 5                  # always keep at least this many (safety baseline)
    min_top_score: float = 0.2      # below this, top passage is "unsure" -> use full context
    fallback_to_full: bool = True
    use_maxsim_rerank: bool = True  # enable ColBERTv2 MaxSim reranking


# --------------------------------------------------------------------------- #
# Brevitas fetch wrapper — retrieval with accuracy-first fail-safe
# --------------------------------------------------------------------------- #
@dataclass
class RetrievalConfig:
    k: int = 5
    min_top_score: float = 0.2      # below this, retrieval is "unsure" -> use full context
    fallback_to_full: bool = True


@dataclass
class QualityFirstRetrievalConfig:
    """Conservative hybrid retrieval configuration.

    ``min_k`` is the caller's evidence budget.  The bridge hop may replace a low-ranked
    passage but never reduces that budget.  Retrieval remains context-reducing—not lossless—
    so callers should keep it opt-in until their own paired workload clears a quality gate.
    """

    max_k: int = 10
    min_k: int = 8
    min_top_score: float = 0.2
    fallback_to_full: bool = True
    rank_constant: int = 60
    candidate_multiplier: int = 4
    bridge_seed_k: int = 3
    max_bridge_expansions: int = 2


def _passage_title(text: str) -> str:
    """Extract a conservative title prefix used only for exact cross-passage links."""
    first_line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    candidates = []
    for match in re.finditer(r"\.\s+", first_line[:240]):
        candidate = first_line[:match.start()].strip()
        if 3 <= len(candidate) <= 120 and len(candidate.split()) <= 18:
            candidates.append((candidate, first_line[match.end():]))
    # Wikipedia-style inputs often start ``Title. Title is ...``. Looking for that repeated
    # prefix handles titles containing periods, such as ``No. 2 Squadron RAAF``.
    for candidate, remainder in reversed(candidates):
        folded_candidate = html.unescape(candidate).casefold()
        folded_remainder = html.unescape(remainder).casefold()
        if folded_remainder.startswith(folded_candidate):
            return candidate
    candidate = candidates[0][0] if candidates else first_line.strip()
    if 3 <= len(candidate) <= 120 and len(candidate.split()) <= 18:
        return candidate
    return ""


_GENERIC_TITLE_WORDS = frozenset({
    "administration", "airport", "album", "association", "city", "conference",
    "county", "history", "lake", "memorial", "park", "school", "season", "society",
    "state", "station", "team", "university",
})


def _passage_title_aliases(title: str) -> List[str]:
    """Conservative aliases for links such as ``Winner (band)`` -> ``Winner``.

    Aliases are only used when unique candidate passages are already in the supplied context;
    they never trigger an external fetch.  This keeps the bridge hop bounded and auditable.
    """
    clean = html.unescape(title or "").strip()
    aliases = [clean] if clean else []
    without_parenthetical = re.sub(r"\s*\([^)]*\)\s*$", "", clean).strip()
    if without_parenthetical and without_parenthetical != clean:
        aliases.append(without_parenthetical)
    before_comma = without_parenthetical.split(",", 1)[0].strip()
    if len(before_comma) >= 5 and before_comma != without_parenthetical:
        aliases.append(before_comma)
    words = re.findall(r"[^\W_]+", without_parenthetical, flags=re.UNICODE)
    initialism_words = [
        word for word in words
        if word.casefold() not in _LEXICAL_STOPWORDS and word.casefold() not in {"of", "for"}
    ]
    if 2 <= len(initialism_words) <= 8:
        initialism = "".join(word[0] for word in initialism_words).upper()
        if len(initialism) >= 2:
            aliases.append(initialism)
    looks_like_named_entity = bool(words) and all(
        word[:1].isupper() or len(word) == 1 for word in words
    )
    if looks_like_named_entity and 2 <= len(words) <= 4:
        surname = words[-1]
        if len(surname) >= 5 and surname.casefold() not in _GENERIC_TITLE_WORDS:
            aliases.append(surname)
    # Longest first avoids a short alias taking priority when both match.
    return sorted(set(aliases), key=lambda value: (-len(value), value.casefold()))


def _alias_match_length(text: str, aliases: Sequence[str]) -> int:
    folded = html.unescape(text or "").casefold()
    return max(
        (
            len(alias) for alias in aliases
            if (len(alias) >= 4 or (len(alias) >= 2 and alias.isupper()))
            and re.search(r"(?<!\w)" + re.escape(alias.casefold()) + r"(?!\w)", folded)
        ),
        default=0,
    )


def _linked_bridge_ids(
    ranked: Sequence[Tuple[object, str, float]],
    full_context: Sequence[str],
    seed_k: int,
) -> List[object]:
    """Find second-hop passages explicitly named by the best first-hop passages."""
    titles = [_passage_title(chunk) for chunk in full_context]
    aliases = [_passage_title_aliases(title) for title in titles]
    rank_by_id = {chunk_id: rank for rank, (chunk_id, _chunk, _score) in enumerate(ranked)}
    linked_by_id: Dict[object, Tuple[int, int, int, int, object]] = {}
    for seed_rank, (seed_id, seed_text, _score) in enumerate(ranked[:seed_k]):
        seed_index = int(seed_id)
        for candidate_id, title in enumerate(titles):
            if candidate_id == seed_index or not title:
                continue
            # Follow links in either direction.  In multi-hop evidence, the first passage may
            # name the bridge target, or a lower-ranked bridge passage may name the first hop.
            forward_match = _alias_match_length(seed_text, aliases[candidate_id])
            reverse_match = _alias_match_length(full_context[candidate_id], aliases[seed_index])
            if forward_match or reverse_match:
                # A first-hop passage naming its target is stronger than the reverse
                # relation. Within a direction, prefer earlier seeds and longer aliases.
                direction = 0 if forward_match else 1
                match_length = forward_match or reverse_match
                priority = (
                    direction,
                    seed_rank,
                    -match_length,
                    rank_by_id.get(candidate_id, len(ranked)),
                    candidate_id,
                )
                previous = linked_by_id.get(candidate_id)
                if previous is None or priority < previous:
                    linked_by_id[candidate_id] = priority
    linked = sorted(linked_by_id.values())
    return [candidate_id for *_priority, candidate_id in linked]


def fetch_quality_first(
    retriever: DenseRetriever,
    query: str,
    full_context: Sequence[str],
    cfg: Optional[QualityFirstRetrievalConfig] = None,
) -> Tuple[List[str], dict]:
    """Hybrid one-hop retrieval plus a bounded deterministic second evidence hop."""
    cfg = cfg or QualityFirstRetrievalConfig()
    context = list(full_context)
    if not context:
        return [], {"fallback_applied": False, "reason": "empty_context"}

    candidate_k = min(
        len(context),
        max(cfg.max_k, cfg.max_k * max(1, cfg.candidate_multiplier), 32),
    )
    dense = retriever.retrieve(query, candidate_k)
    if not dense:
        return context, {"fallback_applied": True, "reason": "empty_index"}
    if dense[0][2] < cfg.min_top_score and cfg.fallback_to_full:
        return context, {
            "fallback_applied": True,
            "reason": "low_confidence",
            "top_score": dense[0][2],
        }

    sparse_retriever = BM25Retriever()
    sparse_retriever.index(context)
    sparse = sparse_retriever.retrieve(query, candidate_k)
    fused = reciprocal_rank_fusion([dense, sparse], cfg.rank_constant) if sparse else dense
    if not fused:
        return context, {"fallback_applied": True, "reason": "no_candidates"}

    chosen_k = min(len(context), max(1, min(cfg.min_k, cfg.max_k)))
    chosen = list(fused[:chosen_k])
    chosen_ids = {chunk_id for chunk_id, _chunk, _score in chosen}
    protected_ids = {chunk_id for chunk_id, _chunk, _score in fused[:cfg.bridge_seed_k]}
    bridge_ids = _linked_bridge_ids(fused, context, cfg.bridge_seed_k)
    applied_bridges: List[object] = []
    fused_by_id = {chunk_id: (chunk_id, chunk, score) for chunk_id, chunk, score in fused}

    for bridge_id in bridge_ids:
        if bridge_id in chosen_ids:
            protected_ids.add(bridge_id)
            continue
        if len(applied_bridges) >= cfg.max_bridge_expansions:
            break
        replacement_index = next(
            (
                index for index in range(len(chosen) - 1, -1, -1)
                if chosen[index][0] not in protected_ids
            ),
            None,
        )
        if replacement_index is None:
            break
        removed_id = chosen[replacement_index][0]
        bridge_item = fused_by_id.get(
            bridge_id,
            (bridge_id, context[int(bridge_id)], 0.0),
        )
        chosen[replacement_index] = bridge_item
        chosen_ids.remove(removed_id)
        chosen_ids.add(bridge_id)
        protected_ids.add(bridge_id)
        applied_bridges.append(bridge_id)

    # Return relevance order.  The proxy restores original message order when rebuilding a
    # conversation, while the explicit retrieval API may intentionally put evidence first.
    fused_rank = {chunk_id: rank for rank, (chunk_id, _chunk, _score) in enumerate(fused)}
    chosen.sort(key=lambda item: fused_rank.get(item[0], len(fused)))
    return [chunk for _chunk_id, chunk, _score in chosen], {
        "fallback_applied": False,
        "reason": "retrieved",
        "k_chosen": len(chosen),
        "top_score": dense[0][2],
        "method": "hybrid_rrf_bridge",
        "bridge_expansions": len(applied_bridges),
        "dense_sparse_top_overlap": len(
            {item[0] for item in dense[:chosen_k]} & {item[0] for item in sparse[:chosen_k]}
        ),
    }


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


def fetch_adaptive(retriever: DenseRetriever, query: str, full_context: Sequence[str],
                   encoder=None, cfg: AdaptiveRetrievalConfig = None) -> Tuple[List[str], dict]:
    """Adaptive retrieval with optional MaxSim reranking. Fails safe to full context.

    Args:
        retriever: DenseRetriever with indexed passages
        query: the question
        full_context: fallback passages (used if index empty or confidence too low)
        encoder: optional; needed for MaxSim reranking
        cfg: AdaptiveRetrievalConfig with max_k, min_k, etc.

    Returns:
        (chosen_chunks, metadata_dict) where metadata includes k_chosen, scores, etc.
    """
    if cfg is None:
        cfg = AdaptiveRetrievalConfig()

    # Retrieve top candidates (batch before reranking)
    candidates = retriever.retrieve(query, k=cfg.max_k)
    if not candidates:
        return list(full_context), {"fallback_applied": True, "reason": "empty_index"}

    # Optionally rerank with MaxSim
    if cfg.use_maxsim_rerank and encoder is not None:
        reranker = MaxSimReranker(encoder)
        candidates = reranker.rerank(query, candidates, cfg.max_k)

    # Adaptive-k: find the knee in score curve (largest relative drop between consecutive passages)
    # but ensure we keep at least min_k passages for recall safety
    chosen = []
    if candidates:
        # Find largest consecutive gap
        max_gap_idx = 0
        max_gap = 0.0
        for i in range(len(candidates) - 1):
            gap = candidates[i][2] - candidates[i + 1][2]
            if gap > max_gap:
                max_gap = gap
                max_gap_idx = i
        # Keep passages: max(min_k, elbow_k), respecting max_k hard cap
        knee_k = max_gap_idx + 1  # include the passage where the largest gap occurred
        chosen_k = max(cfg.min_k, knee_k)  # ensure minimum for recall safety
        chosen_k = min(chosen_k, cfg.max_k)  # respect hard cap
        chosen = candidates[:chosen_k]

    if not chosen:
        return list(full_context), {"fallback_applied": True, "reason": "no_candidates"}

    # Confidence check
    top_score = chosen[0][2]
    if top_score < cfg.min_top_score and cfg.fallback_to_full:
        return list(full_context), {"fallback_applied": True, "reason": "low_confidence",
                                    "top_score": top_score}

    result_chunks = [c for (_, c, _) in chosen]
    return result_chunks, {
        "fallback_applied": False,
        "k_chosen": len(chosen),
        "top_score": top_score,
        "scores": [float(s) for _, _, s in chosen],
        "method": "adaptive_maxsim" if cfg.use_maxsim_rerank else "adaptive_dpr",
    }
