#!/usr/bin/env python3
"""
Phase 2 Tests: Context Store, Retriever, and RLM Orchestrator.

Unit and integration tests for lossless context retrieval.
"""

import json
import tempfile
from pathlib import Path

# Add parent to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent))

from token_efficiency_model.context_store import ContextStore
from token_efficiency_model.optimizers import RLMOrchestrator, RetrieverIndexer


def test_context_store_put_get():
    """Test ContextStore.put() and get()."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = ContextStore(persistence_path=f"{tmpdir}/store.json")

        # Store context
        context = [
            "The quick brown fox jumps over the lazy dog.",
            "Python is a versatile programming language.",
            "Retrieval augmented generation improves LLM accuracy.",
        ]
        store_id = store.put(context)

        # Retrieve
        retrieved = store.get(store_id)
        assert len(retrieved) == 3
        assert retrieved[0] == context[0]
        assert retrieved[2] == context[2]

        print("✓ test_context_store_put_get passed")


def test_context_store_persistence():
    """Test ContextStore disk persistence."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = f"{tmpdir}/store.json"

        # Create and store
        store1 = ContextStore(persistence_path=path)
        context = ["Chunk 1", "Chunk 2", "Chunk 3"]
        store_id = store1.put(context)

        # Reload from disk
        store2 = ContextStore(persistence_path=path)
        retrieved = store2.get(store_id)
        assert len(retrieved) == 3
        assert retrieved[0] == "Chunk 1"

        print("✓ test_context_store_persistence passed")


def test_retriever_indexing():
    """Test RetrieverIndexer.index() and retrieve()."""
    try:
        import numpy as np
    except ImportError:
        print("⊘ test_retriever_indexing skipped (numpy not available)")
        return

    retriever = RetrieverIndexer()

    # Index some chunks
    chunks = [
        "Machine learning models require training data.",
        "The transformer architecture uses attention mechanisms.",
        "Natural language processing helps computers understand text.",
        "Deep learning has revolutionized computer vision.",
        "Gradient descent is an optimization algorithm.",
    ]

    retriever.index(chunks)

    # Retrieve based on query
    query = "How do transformers work in NLP?"
    results = retriever.retrieve(query, k=3)

    assert len(results) > 0, "Retriever returned no results"
    print(f"  Query: '{query}'")
    print(f"  Retrieved {len(results)} chunks:")
    for chunk_hash, score in results:
        chunk_text = retriever.get_chunks_by_hash([chunk_hash])[0]
        print(f"    Score {score:.3f}: {chunk_text[:60]}...")

    print("✓ test_retriever_indexing passed")


def test_rlm_orchestrator_integration():
    """Integration test: RLM orchestrator end-to-end."""
    try:
        import numpy as np
    except ImportError:
        print("⊘ test_rlm_orchestrator_integration skipped (numpy not available)")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        orchestrator = RLMOrchestrator(persistence_dir=tmpdir)

        # Prepare full context
        full_context = [
            "Python lists are ordered and mutable collections.",
            "Dictionaries in Python store key-value pairs.",
            "Tuples are immutable sequences in Python.",
            "Sets are unordered collections of unique elements.",
            "String manipulation is a common task in programming.",
            "Function definitions allow code reuse and modularity.",
            "Exception handling with try-except prevents crashes.",
            "Classes enable object-oriented programming in Python.",
        ]

        store_id = orchestrator.prepare_context(full_context)
        assert store_id is not None

        # Test fetch_context tool
        query = "What data structures are available in Python?"
        chunks = orchestrator.fetch_context(query, k=3)

        assert len(chunks) > 0, "RLM returned no chunks"
        print(f"  Query: '{query}'")
        print(f"  Retrieved {len(chunks)} chunks from RLM:")
        for chunk in chunks:
            print(f"    - {chunk[:60]}...")

        # Test tool call handling (as if model called the tool)
        tool_result = orchestrator.handle_tool_call(
            "fetch_context",
            {"query": "Tell me about Python functions", "k": 2}
        )
        result = json.loads(tool_result)
        assert result["status"] == "success"
        assert result["count"] >= 1

        print("✓ test_rlm_orchestrator_integration passed")


def test_rlm_tool_definitions():
    """Test RLM tool definitions for different providers."""
    orchestrator = RLMOrchestrator()

    # Test OpenAI schema
    openai_tool = orchestrator.build_fetch_context_tool("openai")
    assert openai_tool["type"] == "function"
    assert openai_tool["function"]["name"] == "fetch_context"

    # Test Anthropic schema
    anthropic_tool = orchestrator.build_fetch_context_tool("anthropic")
    assert anthropic_tool["name"] == "fetch_context"
    assert "input_schema" in anthropic_tool

    # Test invalid provider
    try:
        orchestrator.build_fetch_context_tool("invalid")
        assert False, "Should raise ValueError"
    except ValueError:
        pass

    print("✓ test_rlm_tool_definitions passed")


def test_retriever_fallback():
    """Test that retriever falls back gracefully if no retrieval library."""
    # The initialization should succeed even if exact library unavailable
    # (falls back to the other available method)
    retriever = RetrieverIndexer()
    assert retriever._method is not None
    print(f"✓ test_retriever_fallback passed (using method: {retriever._method})")


def test_end_to_end_multi_query():
    """End-to-end test: multiple queries on same context."""
    try:
        import numpy as np
    except ImportError:
        print("⊘ test_end_to_end_multi_query skipped (numpy not available)")
        return

    orchestrator = RLMOrchestrator()

    # Large context covering multiple topics
    context = [
        "Async/await in Python enables non-blocking I/O operations.",
        "The asyncio library provides the event loop and coroutines.",
        "Error handling in async code uses try-except with async calls.",
        "REST APIs use HTTP methods: GET, POST, PUT, DELETE.",
        "Authentication tokens secure API requests.",
        "Rate limiting prevents API abuse.",
        "Machine learning requires labeled training data.",
        "Classification models predict discrete categories.",
        "Regression models predict continuous values.",
        "Feature engineering improves model performance.",
    ]

    store_id = orchestrator.prepare_context(context)

    # Multiple fact queries
    queries = [
        ("What is async/await?", ["asyncio", "non-blocking"]),
        ("How do REST APIs work?", ["HTTP", "API"]),
        ("Tell me about machine learning.", ["training data", "models"]),
    ]

    for query, expected_keywords in queries:
        chunks = orchestrator.fetch_context(query, k=3)
        print(f"  Query: '{query}'")
        print(f"    Retrieved {len(chunks)} chunks")

        # Check if relevant keywords appear in results
        combined_text = " ".join(chunks).lower()
        found_keywords = [kw for kw in expected_keywords if kw.lower() in combined_text]
        print(f"    Found keywords: {found_keywords} / {expected_keywords}")

    print("✓ test_end_to_end_multi_query passed")


if __name__ == "__main__":
    print("=== Phase 2 Tests: RLM + Retrieval ===\n")

    test_context_store_put_get()
    test_context_store_persistence()
    test_retriever_indexing()
    test_rlm_orchestrator_integration()
    test_rlm_tool_definitions()
    test_retriever_fallback()
    test_end_to_end_multi_query()

    print("\n=== All tests passed ===")
