"""
RLM (Recursive Language Model) Orchestrator.

Implements the RLM depth-1 loop from arXiv:2512.24601.
Model can call fetch_context(query) to retrieve relevant chunks from the full context.
"""

import json
from typing import List, Dict, Any, Optional, Callable, Tuple

from .retrieval import RetrieverIndexer
from ..context_store import ContextStore


class RLMOrchestrator:
    """
    RLM depth-1 loop orchestrator.

    Manages the context store and retrieval index.
    Provides fetch_context tool interface for model tool-use.
    """

    def __init__(
        self,
        context_store: Optional[ContextStore] = None,
        retriever: Optional[RetrieverIndexer] = None,
        persistence_dir: str = "",
    ):
        """
        Args:
            context_store: ContextStore instance (created if None).
            retriever: RetrieverIndexer instance (created if None).
            persistence_dir: Directory for disk persistence.
        """
        self.context_store = context_store or ContextStore(
            persistence_path=(
                f"{persistence_dir}/context_store.json"
                if persistence_dir
                else ""
            )
        )
        self.retriever = retriever or RetrieverIndexer()
        self.persistence_dir = persistence_dir
        self._current_store_id: Optional[str] = None

    def prepare_context(self, context_chunks: List[str]) -> str:
        """
        Store and index full context.

        Args:
            context_chunks: List of context strings (typically prior_context from pipeline).

        Returns:
            store_id: Used to later fetch from this context.
        """
        # Store in context store
        store_id = self.context_store.put(context_chunks)
        self._current_store_id = store_id

        # Get the chunk hashes and index
        chunk_hashes = self.context_store.list_chunk_hashes(store_id)
        chunks = self.context_store.get(store_id)
        self.retriever.index(chunks, chunk_hashes)

        return store_id

    def fetch_context(
        self,
        query: str,
        k: int = 5,
        store_id: Optional[str] = None,
    ) -> List[str]:
        """
        Fetch relevant context chunks (RLM tool).

        This is the tool that the model can call during tool-use loops.

        Args:
            query: Query string from the model.
            k: Top-k chunks to retrieve.
            store_id: Which context to search (defaults to current).

        Returns:
            List of top-k relevant chunks.
        """
        if store_id is None:
            store_id = self._current_store_id

        if not store_id:
            return []

        # Retrieve via late-interaction
        ranked_hashes = self.retriever.retrieve(query, k=k)

        # Map back to chunk text
        chunks = []
        for chunk_hash, score in ranked_hashes:
            chunk = self.context_store._chunks.get(chunk_hash)
            if chunk:
                chunks.append(chunk)

        return chunks

    def build_fetch_context_tool(
        self,
        provider: str = "openai",
    ) -> Dict[str, Any]:
        """
        Build a tool definition for fetch_context in the provider's schema.

        Args:
            provider: One of "openai", "anthropic", or "groq".

        Returns:
            Tool definition dict.
        """
        if provider in ("openai", "groq", "deepseek"):
            # OpenAI tools schema
            return {
                "type": "function",
                "function": {
                    "name": "fetch_context",
                    "description": (
                        "Fetch relevant context chunks from the full knowledge base. "
                        "Use this to retrieve specific pieces of context needed to answer the question."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": (
                                    "The search query. Describe what information you need. "
                                    "E.g., 'Find information about error handling in async code'"
                                ),
                            },
                            "k": {
                                "type": "integer",
                                "description": "Number of chunks to retrieve (default: 5)",
                                "default": 5,
                            },
                        },
                        "required": ["query"],
                    },
                },
            }
        elif provider == "anthropic":
            # Anthropic tools schema (more native)
            return {
                "name": "fetch_context",
                "description": (
                    "Fetch relevant context chunks from the full knowledge base. "
                    "Use this to retrieve specific pieces of context needed to answer the question."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "The search query. Describe what information you need. "
                                "E.g., 'Find information about error handling in async code'"
                            ),
                        },
                        "k": {
                            "type": "integer",
                            "description": "Number of chunks to retrieve (default: 5)",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            }
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    def handle_tool_call(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
    ) -> str:
        """
        Handle a tool call from the model.

        Args:
            tool_name: Name of the tool (e.g., "fetch_context").
            tool_input: Arguments dict with "query" and optional "k".

        Returns:
            JSON-serialized result.
        """
        if tool_name == "fetch_context":
            query = tool_input.get("query", "")
            k = tool_input.get("k", 5)
            chunks = self.fetch_context(query, k=k)
            result = {
                "status": "success",
                "chunks": chunks,
                "count": len(chunks),
            }
            return json.dumps(result)
        else:
            return json.dumps({
                "status": "error",
                "message": f"Unknown tool: {tool_name}",
            })

    def __repr__(self) -> str:
        return (
            f"RLMOrchestrator("
            f"current_store={self._current_store_id}, "
            f"context_store={self.context_store}, "
            f"retriever={self.retriever})"
        )
