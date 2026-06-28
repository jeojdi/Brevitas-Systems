"""
ContextStore: lossless full context storage for RLM retrieval.

Stores complete context chunks keyed by content hash.
No pruning, no compression — just retrieval-ready storage.
"""

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Tuple


class ContextStore:
    """
    Stores full context chunks by hash.

    API:
    - put(context_chunks: List[str]) -> store_id: str
    - get(store_id: str) -> List[str]
    - retrieve_by_ids(chunk_ids: List[str]) -> List[str]
    """

    def __init__(self, persistence_path: str = ""):
        """
        Args:
            persistence_path: JSON file path for disk persistence.
                             If empty, in-memory only.
        """
        self._stores: Dict[str, List[str]] = {}  # store_id -> list of chunks
        self._chunks: Dict[str, str] = {}  # chunk_hash -> chunk text
        self.persistence_path = persistence_path
        self._load()

    def _chunk_hash(self, text: str) -> str:
        """SHA1 hash of chunk text, first 12 chars."""
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

    def _store_id_from_chunks(self, chunk_hashes: List[str]) -> str:
        """Generate a store_id from the list of chunk hashes."""
        combined = "|".join(sorted(chunk_hashes))
        return "ctx:" + hashlib.sha1(combined.encode("utf-8")).hexdigest()[:12]

    def put(self, context_chunks: List[str]) -> str:
        """
        Store full context chunks.

        Args:
            context_chunks: List of context strings (no pruning).

        Returns:
            store_id: Unique identifier for this context set.
        """
        chunk_hashes = []
        for chunk in context_chunks:
            chunk_hash = self._chunk_hash(chunk)
            if chunk_hash not in self._chunks:
                self._chunks[chunk_hash] = chunk
            chunk_hashes.append(chunk_hash)

        store_id = self._store_id_from_chunks(chunk_hashes)
        self._stores[store_id] = chunk_hashes
        self._persist()
        return store_id

    def get(self, store_id: str) -> List[str]:
        """
        Retrieve all chunks for a store_id.

        Args:
            store_id: Returned by put().

        Returns:
            List of context chunks in original order.
        """
        if store_id not in self._stores:
            return []

        chunk_hashes = self._stores[store_id]
        return [self._chunks[h] for h in chunk_hashes if h in self._chunks]

    def retrieve_by_ids(self, chunk_hashes: List[str]) -> List[str]:
        """Retrieve specific chunks by their hashes."""
        return [self._chunks[h] for h in chunk_hashes if h in self._chunks]

    def list_chunk_hashes(self, store_id: str) -> List[str]:
        """Get the list of chunk hashes for a store."""
        return self._stores.get(store_id, [])

    def _load(self) -> None:
        """Load from disk if persistence_path is set."""
        if not self.persistence_path:
            return

        path = Path(self.persistence_path)
        if not path.exists():
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._stores = data.get("stores", {})
            self._chunks = data.get("chunks", {})
        except Exception:
            self._stores = {}
            self._chunks = {}

    def _persist(self) -> None:
        """Persist to disk if persistence_path is set."""
        if not self.persistence_path:
            return

        payload = {
            "stores": self._stores,
            "chunks": self._chunks,
        }
        path = Path(self.persistence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8"
        )

    def __repr__(self) -> str:
        return f"ContextStore(num_stores={len(self._stores)}, num_chunks={len(self._chunks)})"
