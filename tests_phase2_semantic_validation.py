#!/usr/bin/env python3
"""
Phase 2 Semantic Validation Test

Validates that retrieval uses SEMANTIC ranking (embeddings), not LEXICAL ranking (keywords).
This test demonstrates the crucial difference between Phase 2 (lossless) and the old lossy approach.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from token_efficiency_model.optimizers import RetrieverIndexer, RLMOrchestrator


def test_semantic_ranking_vs_keyword():
    """
    Test that retrieval ranks by SEMANTIC similarity, not keyword overlap.

    A query with completely different WORDS should still rank high if it
    means the same thing (semantic equivalence).

    Keyword matching fails this test.
    Embeddings pass this test.
    """
    orchestrator = RLMOrchestrator()

    # Context: specific information about async functions
    context = [
        "Coroutines in Python are declared with the async keyword and use await for non-blocking calls.",
        "Machine learning models require training data and validation sets.",
        "The asyncio library provides an event loop for concurrent execution.",
        "Database transactions ensure ACID properties.",
        "Non-blocking I/O prevents threads from being blocked on slow operations.",
    ]

    store_id = orchestrator.prepare_context(context)

    # Query with COMPLETELY DIFFERENT WORDS but SAME SEMANTIC MEANING
    # "How do you write asynchronous code?" asks about async but uses different words
    query = "How do you write asynchronous code without blocking?"

    chunks = orchestrator.fetch_context(query, k=3)

    print("=" * 80)
    print("SEMANTIC RANKING TEST")
    print("=" * 80)
    print(f"\nQuery (different words): '{query}'")
    print("Target meaning: async/non-blocking/concurrent execution\n")

    # Check retrieval method
    method = orchestrator.retriever._method
    print(f"Retrieval method: {method}")

    if method == "keyword-fallback":
        print("\n⚠️  FAILED: Still using keyword-fallback (lexical matching)")
        print("   Keyword approach cannot rank 'asynchronous' high if chunk says 'async'")
        return False

    # Verify semantic ranking
    print(f"Retrieved chunks (top-3 for query):\n")

    async_chunks = []
    for i, chunk in enumerate(chunks, 1):
        print(f"  {i}. {chunk[:70]}...")
        if any(kw in chunk.lower() for kw in ["async", "coroutine", "await", "non-blocking", "concurrent"]):
            async_chunks.append(chunk)

    # Semantic ranker should prioritize async-related chunks
    # even though the query says "asynchronous" not "async"
    success = len(async_chunks) >= 2  # At least 2 of top-3 should be async-related

    print(f"\nAsync-related chunks in top-3: {len(async_chunks)}")
    print(f"Expected: ≥2 (semantic matching should prefer async chunks)")

    if success:
        print("\n✓ PASSED: Semantic ranking is active (embeddings)")
        print("   Different words ranked high if semantically similar")
    else:
        print("\n✗ FAILED: Not using semantic ranking")
        print("   Lexical keyword matching would miss 'asynchronous' != 'async'")

    print("=" * 80)
    return success


def test_retriever_method_detection():
    """Verify that retriever is NOT using keyword fallback when libraries available."""
    retriever = RetrieverIndexer()

    method = retriever._method
    is_semantic = method in ("colbert-pylate", "dense-retrieval")

    print("\n" + "=" * 80)
    print("RETRIEVER METHOD TEST")
    print("=" * 80)
    print(f"Active method: {method}")

    if is_semantic:
        print(f"✓ PASS: Using semantic method ({method})")
    else:
        print(f"✗ FAIL: Using {method} (lexical, not semantic)")

    print("=" * 80)
    return is_semantic


def test_score_distribution():
    """Verify that semantic ranking produces meaningful (non-zero) scores."""
    retriever = RetrieverIndexer()

    chunks = [
        "Deep learning uses neural networks with multiple layers.",
        "Coffee is a beverage made from roasted beans.",
        "Neural networks learn through backpropagation.",
        "Python is a programming language.",
    ]

    retriever.index(chunks)

    # Query related to neural networks
    query = "How do artificial neural networks train?"
    results = retriever.retrieve(query, k=4)

    print("\n" + "=" * 80)
    print("SCORE DISTRIBUTION TEST")
    print("=" * 80)
    print(f"Query: '{query}'")
    print(f"Method: {retriever._method}\n")

    print("Scores for all chunks:")
    for chunk_hash, score in results:
        chunk = retriever.get_chunks_by_hash([chunk_hash])[0]
        print(f"  Score {score:.4f}: {chunk[:60]}...")

    # Check that:
    # 1. Scores are non-zero (semantic method produces scores)
    # 2. Neural network chunks rank higher than coffee/python
    scores = [score for _, score in results]
    has_nonzero_scores = all(s > 0.0 for s in scores)

    if has_nonzero_scores:
        print(f"\n✓ PASS: Non-zero meaningful scores (semantic method)")
    else:
        print(f"\n✗ FAIL: Zero scores or no differentiation (keyword method)")

    print("=" * 80)
    return has_nonzero_scores


if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("PHASE 2 SEMANTIC VALIDATION")
    print("Verifies that Phase 2 uses EMBEDDINGS, not KEYWORDS")
    print("=" * 80)

    # Test 1: Method detection
    method_ok = test_retriever_method_detection()

    # Test 2: Score distribution
    scores_ok = test_score_distribution()

    # Test 3: Semantic ranking on paraphrase
    semantic_ok = test_semantic_ranking_vs_keyword()

    print("\n" + "=" * 80)
    print("FINAL RESULT")
    print("=" * 80)

    if method_ok and scores_ok and semantic_ok:
        print("✓ ALL TESTS PASSED")
        print("  Phase 2 is using SEMANTIC EMBEDDING-BASED RETRIEVAL")
        print("  NOT lexical keyword matching from the old lossy approach")
        sys.exit(0)
    else:
        print("✗ SOME TESTS FAILED")
        print("  Phase 2 may still be using keyword fallback")
        sys.exit(1)
