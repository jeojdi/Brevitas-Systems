"""
Phase 2: Lossless context storage for RLM (Recursive Language Models).

Stores full context without pre-pruning. Keyed by content hash.
Supports both in-memory and disk persistence.
"""

from .store import ContextStore

__all__ = ["ContextStore"]
