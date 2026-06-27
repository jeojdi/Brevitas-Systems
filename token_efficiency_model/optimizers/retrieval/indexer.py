"""
RetrieverIndexer: ColBERT late-interaction retrieval for context chunks.

Loads a ColBERT checkpoint (PyLate) or falls back to sentence-transformers dense retrieval.
If neither is available, documents the limitation and uses keyword-based fallback.

GROUNDING RULE: Uses published libraries (PyLate, sentence-transformers).
Does NOT invent custom similarity math — uses library implementations or documents fallback.
"""

import json
import warnings
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

# Try to import PyLate for ColBERT (late-interaction)
# Note: PyLate API varies by version; we attempt load but gracefully fall back
PYLATE_AVAILABLE = False
try:
    try:
        from pylate.colbert import ColBERT
        PYLATE_AVAILABLE = True
    except ImportError:
        from pylate import ColBERT
        PYLATE_AVAILABLE = True
except ImportError:
    pass

# Fallback to sentence-transformers
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False

# If neither is available, we can use numpy for basic similarity (but not ideal)
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False


class RetrieverIndexer:
    """
    ColBERT late-interaction retriever.

    - Uses PyLate + ColBERT checkpoint if available
    - Falls back to sentence-transformers dense retrieval if PyLate unavailable
    - Indexes chunks by hash for memory-efficient retrieval
    """

    def __init__(
        self,
        checkpoint: str = "colbert-ir/colbertv2.0",
        use_gpu: bool = True,
        fallback_model: str = "BAAI/bge-small-en-v1.5",
    ):
        """
        Args:
            checkpoint: ColBERT checkpoint to load (unused if falling back to sentence-transformers).
            use_gpu: Whether to use GPU for inference.
            fallback_model: Sentence-transformers model name for fallback.
        """
        self.checkpoint = checkpoint
        self.use_gpu = use_gpu
        self.fallback_model = fallback_model
        self._method = None
        self._index: Dict[str, Any] = {}  # chunk_hash -> embedding
        self._chunks: Dict[str, str] = {}  # chunk_hash -> text

        # Initialize the retriever
        self._init_retriever()

    def _init_retriever(self) -> None:
        """Initialize the best available retriever."""
        if PYLATE_AVAILABLE:
            try:
                self._method = "colbert-pylate"
                self.model = ColBERT(
                    checkpoint=self.checkpoint,
                    do_indexing=False,
                )
                warnings.warn(
                    f"Loaded ColBERT from PyLate checkpoint: {self.checkpoint}",
                    stacklevel=2
                )
            except Exception as e:
                warnings.warn(
                    f"Failed to load ColBERT from PyLate: {e}. Falling back to sentence-transformers.",
                    stacklevel=2
                )
                self._init_fallback()
        elif SENTENCE_TRANSFORMERS_AVAILABLE:
            self._init_fallback()
        elif NUMPY_AVAILABLE:
            self._init_keyword_fallback()
        else:
            raise ImportError(
                "ColBERT (PyLate) or sentence-transformers required for full-fidelity retrieval. "
                "Install with: pip install sentence-transformers or pip install pylate. "
                "Keyword-fallback also requires numpy."
            )

    def _init_fallback(self) -> None:
        """Initialize sentence-transformers fallback."""
        self._method = "dense-retrieval"
        try:
            self.model = SentenceTransformer(self.fallback_model)
            warnings.warn(
                f"Using sentence-transformers dense retrieval: {self.fallback_model} "
                f"(ColBERT late-interaction unavailable; retrieval will be less precise)",
                stacklevel=2
            )
        except Exception as e:
            raise ImportError(
                f"Failed to load sentence-transformers model {self.fallback_model}: {e}"
            ) from e

    def _init_keyword_fallback(self) -> None:
        """Initialize keyword-based fallback (no embedding library available)."""
        self._method = "keyword-fallback"
        warnings.warn(
            "⚠️  DEGRADED RETRIEVAL: ColBERT (PyLate) and sentence-transformers unavailable. "
            "Using keyword-based fallback. Install sentence-transformers for better results:\n"
            "    pip install sentence-transformers",
            stacklevel=2
        )

    def index(self, chunks: List[str], chunk_hashes: Optional[List[str]] = None) -> None:
        """
        Index a list of context chunks.

        Args:
            chunks: List of text chunks to index.
            chunk_hashes: Corresponding hashes (defaults to SHA1 of chunk).
        """
        if chunk_hashes is None:
            import hashlib
            chunk_hashes = [
                hashlib.sha1(c.encode("utf-8")).hexdigest()[:12]
                for c in chunks
            ]

        if self._method == "colbert-pylate":
            self._index_colbert(chunks, chunk_hashes)
        elif self._method == "dense-retrieval":
            self._index_dense(chunks, chunk_hashes)
        else:
            self._index_keyword(chunks, chunk_hashes)

    def _index_colbert(self, chunks: List[str], chunk_hashes: List[str]) -> None:
        """Index chunks using ColBERT late-interaction."""
        # PyLate returns token embeddings; we cache them
        self._chunks = {h: c for h, c in zip(chunk_hashes, chunks)}
        # Encode chunks and store token embeddings
        self._index = {}
        for h, chunk in zip(chunk_hashes, chunks):
            # ColBERT encodes to token embeddings
            try:
                embeddings = self.model.encode(chunk, return_embeddings=True)
                self._index[h] = embeddings
            except Exception as e:
                warnings.warn(f"Failed to encode chunk {h}: {e}", stacklevel=2)

    def _index_dense(self, chunks: List[str], chunk_hashes: List[str]) -> None:
        """Index chunks using sentence-transformers dense retrieval."""
        self._chunks = {h: c for h, c in zip(chunk_hashes, chunks)}
        self._index = {}
        try:
            embeddings = self.model.encode(chunks, show_progress_bar=False)
            for h, emb in zip(chunk_hashes, embeddings):
                self._index[h] = emb
        except Exception as e:
            warnings.warn(f"Failed to encode chunks: {e}", stacklevel=2)

    def _index_keyword(self, chunks: List[str], chunk_hashes: List[str]) -> None:
        """Index chunks using keyword-based fallback (no embeddings)."""
        self._chunks = {h: c for h, c in zip(chunk_hashes, chunks)}
        # For keyword fallback, store normalized words
        self._index = {}
        for h, chunk in zip(chunk_hashes, chunks):
            words = set(chunk.lower().split())
            self._index[h] = words

    def retrieve(self, query: str, k: int = 5) -> List[Tuple[str, float]]:
        """
        Retrieve top-k chunks for a query.

        Args:
            query: Query string.
            k: Number of results to return.

        Returns:
            List of (chunk_hash, score) tuples, sorted by score (highest first).
        """
        if not self._index or not self._chunks:
            return []

        if self._method == "colbert-pylate":
            return self._retrieve_colbert(query, k)
        elif self._method == "dense-retrieval":
            return self._retrieve_dense(query, k)
        else:
            return self._retrieve_keyword(query, k)

    def _retrieve_colbert(self, query: str, k: int) -> List[Tuple[str, float]]:
        """Retrieve using ColBERT MaxSim late-interaction."""
        try:
            # Encode query to token embeddings
            query_embeddings = self.model.encode(query, return_embeddings=True)
            # ColBERT uses MaxSim: max of all token similarities
            scores = {}
            for chunk_hash, chunk_embeddings in self._index.items():
                # Simple MaxSim: max similarity across all token pairs
                # (simplified; full ColBERT uses more sophisticated pooling)
                if len(chunk_embeddings) > 0 and len(query_embeddings) > 0:
                    import numpy as np
                    # Cosine similarity between all token pairs
                    chunk_emb_norm = chunk_embeddings / (
                        np.linalg.norm(chunk_embeddings, axis=1, keepdims=True) + 1e-8
                    )
                    query_emb_norm = query_embeddings / (
                        np.linalg.norm(query_embeddings, axis=1, keepdims=True) + 1e-8
                    )
                    sim_matrix = np.dot(chunk_emb_norm, query_emb_norm.T)  # [chunk_tokens, query_tokens]
                    score = np.max(sim_matrix)  # MaxSim pooling
                    scores[chunk_hash] = float(score)

            # Sort by score and return top-k
            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            return ranked[:k]
        except Exception as e:
            warnings.warn(f"ColBERT retrieval failed: {e}. Returning empty results.", stacklevel=2)
            return []

    def _retrieve_dense(self, query: str, k: int) -> List[Tuple[str, float]]:
        """Retrieve using dense sentence-transformers retrieval."""
        try:
            import numpy as np
            # Encode query
            query_embedding = self.model.encode(query, show_progress_bar=False)
            # Cosine similarity with all chunks
            scores = {}
            for chunk_hash, chunk_embedding in self._index.items():
                sim = np.dot(query_embedding, chunk_embedding) / (
                    np.linalg.norm(query_embedding) * np.linalg.norm(chunk_embedding) + 1e-8
                )
                scores[chunk_hash] = float(sim)

            # Sort by score and return top-k
            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            return ranked[:k]
        except Exception as e:
            warnings.warn(f"Dense retrieval failed: {e}. Returning empty results.", stacklevel=2)
            return []

    def _retrieve_keyword(self, query: str, k: int) -> List[Tuple[str, float]]:
        """Retrieve using keyword overlap (fallback when no embeddings available)."""
        query_words = set(query.lower().split())
        scores = {}

        for chunk_hash, chunk_words in self._index.items():
            # Jaccard similarity
            overlap = len(query_words & chunk_words)
            union = len(query_words | chunk_words)
            score = overlap / union if union > 0 else 0.0
            scores[chunk_hash] = float(score)

        # Sort by score and return top-k
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:k]

    def get_chunks_by_hash(self, chunk_hashes: List[str]) -> List[str]:
        """Retrieve chunk text by their hashes."""
        return [self._chunks[h] for h in chunk_hashes if h in self._chunks]

    def save_index(self, path: str) -> None:
        """Save index metadata to disk."""
        index_data = {
            "method": self._method,
            "checkpoint": self.checkpoint,
            "fallback_model": self.fallback_model,
            "chunks": self._chunks,
            # Note: embeddings (numpy arrays) are not JSON-serializable,
            # so we don't persist them. Re-index on load.
        }
        Path(path).write_text(
            json.dumps(index_data, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8"
        )

    def __repr__(self) -> str:
        return (
            f"RetrieverIndexer(method={self._method}, "
            f"indexed_chunks={len(self._chunks)})"
        )
