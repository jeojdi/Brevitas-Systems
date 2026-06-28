#!/usr/bin/env python
"""Demonstration of the Phase 3 quality gate.

Run with: python -m token_efficiency_model.quality.demo
"""

from token_efficiency_model.quality.gate import assess, QualityGateConfig


def demo_pass_case():
    """Demonstrate a passing quality assessment."""
    print("\n" + "="*70)
    print("DEMO 1: PASSING CASE (Equivalent Answer)")
    print("="*70)

    optimized = "The capital of France is Paris."
    reference = "Paris is the capital of France."
    question = "What is the capital of France?"

    assessment = assess(optimized, reference, question)

    print(f"\nQuestion: {question}")
    print(f"\nReference (full context): {reference}")
    print(f"\nOptimized (compressed):  {optimized}")
    print(f"\n--- Assessment ---")
    print(f"Embedding Similarity: {assessment.embedding_similarity:.3f}")
    print(f"Judge Score:         {assessment.judge_score:.3f}")
    print(f"Combined Score:      {assessment.score:.3f}")
    print(f"Passed (floor=0.8):  {'✓ YES' if assessment.passed else '✗ NO'}")
    if assessment.fallback_reason:
        print(f"Fallback Reason:     {assessment.fallback_reason}")


def demo_fail_case_wrong_answer():
    """Demonstrate a failing quality assessment (wrong answer)."""
    print("\n" + "="*70)
    print("DEMO 2: FAILING CASE (Wrong Answer)")
    print("="*70)

    optimized = "The capital of France is London."  # Wrong!
    reference = "The capital of France is Paris."
    question = "What is the capital of France?"

    assessment = assess(optimized, reference, question)

    print(f"\nQuestion: {question}")
    print(f"\nReference (full context): {reference}")
    print(f"\nOptimized (compressed):  {optimized}")
    print(f"\n--- Assessment ---")
    print(f"Embedding Similarity: {assessment.embedding_similarity:.3f}")
    print(f"Judge Score:         {assessment.judge_score:.3f}")
    print(f"Combined Score:      {assessment.score:.3f}")
    print(f"Passed (floor=0.8):  {'✓ YES' if assessment.passed else '✗ NO'}")
    if assessment.fallback_reason:
        print(f"Fallback Reason:     {assessment.fallback_reason}")
    print(f"\n→ Billing Impact: Quality failed → NO FEE CHARGED")


def demo_fail_case_truncation():
    """Demonstrate a failing quality assessment (truncated answer)."""
    print("\n" + "="*70)
    print("DEMO 3: FAILING CASE (Truncated Answer)")
    print("="*70)

    optimized = "The Eiffel Tower is a..."  # Truncated
    reference = (
        "The Eiffel Tower is an iron lattice monument located in Paris, France. "
        "Designed by Gustave Eiffel, it was constructed for the 1889 World's Fair. "
        "Standing 330 meters tall, it is the most visited paid monument in the world. "
        "Originally intended to be temporary, it has become the iconic symbol of Paris."
    )
    question = "Describe the Eiffel Tower."

    assessment = assess(optimized, reference, question)

    print(f"\nQuestion: {question}")
    print(f"\nReference (full context):")
    print(f"  {reference}")
    print(f"\nOptimized (compressed): {optimized}")
    print(f"\n--- Assessment ---")
    print(f"Embedding Similarity: {assessment.embedding_similarity:.3f}")
    print(f"Judge Score:         {assessment.judge_score:.3f}")
    print(f"Combined Score:      {assessment.score:.3f}")
    print(f"Passed (floor=0.8):  {'✓ YES' if assessment.passed else '✗ NO'}")
    if assessment.fallback_reason:
        print(f"Fallback Reason:     {assessment.fallback_reason}")
    print(f"\n→ Billing Impact: Quality failed → FALLBACK TO FULL CONTEXT")


def demo_configurable_floor():
    """Demonstrate how floor affects pass/fail decision."""
    print("\n" + "="*70)
    print("DEMO 4: Configurable Floor (Same Answer, Different Thresholds)")
    print("="*70)

    optimized = "Python is a popular programming language."
    reference = "Python is a popular, high-level programming language used in data science and web development."
    question = "What is Python?"

    print(f"\nQuestion: {question}")
    print(f"\nReference: {reference}")
    print(f"\nOptimized: {optimized}")

    # Try with different floors
    for floor in [0.5, 0.75, 0.85, 0.95]:
        config = QualityGateConfig(floor=floor)
        assessment = assess(optimized, reference, question, config=config)
        status = "✓ PASS" if assessment.passed else "✗ FAIL"
        print(f"\nFloor={floor}: Score={assessment.score:.3f} → {status}")


def demo_billing_scenarios():
    """Demonstrate billing impact of quality assessment."""
    print("\n" + "="*70)
    print("DEMO 5: Billing Scenarios")
    print("="*70)

    scenarios = [
        {
            "name": "Verified High Quality",
            "baseline_tokens": 10000,
            "compressed_tokens": 5000,
            "quality_score": 0.92,
            "floor": 0.8,
        },
        {
            "name": "Unverified (No Judge)",
            "baseline_tokens": 10000,
            "compressed_tokens": 5000,
            "quality_score": None,
            "floor": 0.8,
        },
        {
            "name": "Below Floor",
            "baseline_tokens": 10000,
            "compressed_tokens": 5000,
            "quality_score": 0.65,
            "floor": 0.8,
        },
    ]

    for scenario in scenarios:
        print(f"\n{scenario['name']}:")
        print(f"  Baseline: {scenario['baseline_tokens']} tokens")
        print(f"  Compressed: {scenario['compressed_tokens']} tokens")
        print(f"  Quality Score: {scenario['quality_score']}")

        # Simplified billing logic (see api/server.py for full implementation)
        tokens_saved = scenario['baseline_tokens'] - scenario['compressed_tokens']
        quality_verified = (
            scenario['quality_score'] is not None and
            scenario['quality_score'] >= scenario['floor']
        )

        if quality_verified:
            fee_usd = tokens_saved / 1_000_000 * 0.10 * 0.001  # Rough estimate
            print(f"  ✓ Quality verified → Fee charged: ${fee_usd:.6f}")
        else:
            status = (
                "unverified" if scenario['quality_score'] is None
                else "failed"
            )
            print(f"  ✗ Quality {status} → NO FEE CHARGED")


if __name__ == "__main__":
    print("\n" + "#"*70)
    print("# PHASE 3: REAL QUALITY GATE DEMONSTRATION")
    print("#"*70)

    try:
        demo_pass_case()
        demo_fail_case_wrong_answer()
        demo_fail_case_truncation()
        demo_configurable_floor()
        demo_billing_scenarios()

        print("\n" + "="*70)
        print("SUMMARY")
        print("="*70)
        print("""
The quality gate uses:
1. Embedding similarity (fast, local, ~100-300ms)
2. LLM-as-judge (accurate, ~500-1500ms, cost-disciplined)
3. Configurable floor (default 0.8, tunable per customer)

Key behaviors:
- PASSES: Returns score ≥ floor → Bill savings, proceed
- FAILS: Returns score < floor → NO SAVINGS BILLED, signal rehydrate
- Judge unavailable: Uses embedding only with 10% penalty, still gates

See token_efficiency_model/quality/INTEGRATION.md for full documentation.
""")

    except Exception as e:
        print(f"\nError running demo: {e}")
        import traceback
        traceback.print_exc()
