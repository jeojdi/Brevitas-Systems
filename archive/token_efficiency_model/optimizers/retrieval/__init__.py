"""
Phase 2: Lossless context retrieval using ColBERT late-interaction.

Indexes full context and retrieves precise chunks via MaxSim late-interaction.
Uses PyLate (https://github.com/ixia-research/PyLate) if available;
falls back to sentence-transformers for dense retrieval.
"""

from .indexer import RetrieverIndexer

__all__ = ["RetrieverIndexer"]
