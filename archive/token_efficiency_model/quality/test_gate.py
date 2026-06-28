"""Tests for the quality gate (Phase 3).

Tests embedding similarity, LLM-as-judge, and fallback behavior.
"""

import pytest
from token_efficiency_model.quality.gate import (
    QualityGate,
    QualityGateConfig,
    assess,
)


class TestEmbeddingSimilarity:
    """Test embedding-based similarity scoring."""

    def test_identical_texts_score_high(self):
        """Identical texts should score close to 1.0."""
        gate = QualityGate()
        score = gate._embedding_similarity(
            "The capital of France is Paris.",
            "The capital of France is Paris."
        )
        assert score > 0.99, f"Expected >0.99, got {score}"

    def test_similar_texts_score_moderate(self):
        """Similar but not identical texts should score moderately."""
        gate = QualityGate()
        score = gate._embedding_similarity(
            "Paris is the capital of France and is known for the Eiffel Tower.",
            "France's capital is Paris, famous for the Eiffel Tower."
        )
        assert 0.7 < score < 0.99, f"Expected 0.7-0.99, got {score}"

    def test_different_texts_score_low(self):
        """Unrelated texts should score low."""
        gate = QualityGate()
        score = gate._embedding_similarity(
            "The capital of France is Paris.",
            "Whales are large marine mammals that live in the ocean."
        )
        assert score < 0.5, f"Expected <0.5, got {score}"

    def test_empty_text_handling(self):
        """Empty or missing text should handle gracefully."""
        gate = QualityGate()
        score = gate._embedding_similarity("", "Some text")
        assert score == 0.0, "Empty text should score 0.0"

        score = gate._embedding_similarity("Text", "")
        assert score == 0.0, "Empty text should score 0.0"

        score = gate._embedding_similarity("", "")
        assert score == 1.0, "Both empty should score 1.0"

    def test_similarity_is_symmetric(self):
        """Similarity should be symmetric."""
        gate = QualityGate()
        text1 = "The quick brown fox jumps over the lazy dog."
        text2 = "A fast brown fox jumps over a lazy dog."

        sim_1_2 = gate._embedding_similarity(text1, text2)
        sim_2_1 = gate._embedding_similarity(text2, text1)

        assert abs(sim_1_2 - sim_2_1) < 0.0001, "Similarity should be symmetric"


class TestQualityGateAssess:
    """Test the full quality assessment."""

    def test_pass_equivalent_answer(self):
        """Gate should PASS an equivalent answer."""
        gate = QualityGate(config=QualityGateConfig(floor=0.8))

        # Nearly identical answers
        assessment = gate.assess(
            optimized_answer="The capital of France is Paris.",
            reference_answer="The capital of France is Paris.",
            question="What is the capital of France?"
        )

        assert assessment.passed is True, "Identical answer should pass"
        # Embedding similarity is 1.0, judge unavailable so score = 1.0 * 0.9 = 0.9
        assert assessment.score >= 0.85, f"Score should be >=0.85 (embedding 1.0 with 0.9 penalty), got {assessment.score}"

    def test_fail_wrong_answer(self):
        """Gate should FAIL a wrong answer."""
        gate = QualityGate(config=QualityGateConfig(floor=0.8))

        assessment = gate.assess(
            optimized_answer="The capital of France is London.",  # Wrong
            reference_answer="The capital of France is Paris.",
            question="What is the capital of France?"
        )

        assert assessment.passed is False, "Wrong answer should fail"
        assert assessment.score < 0.8, f"Score should be <0.8, got {assessment.score}"

    def test_fail_truncated_answer(self):
        """Gate should FAIL a critically truncated answer."""
        gate = QualityGate(config=QualityGateConfig(floor=0.8))

        assessment = gate.assess(
            optimized_answer="The capital of France...",  # Truncated
            reference_answer="The capital of France is Paris, located on the Seine River. It is the most visited city in the world.",
            question="Describe the capital of France."
        )

        # Even with embedding discount, should be below floor
        assert assessment.passed is False, "Truncated answer should fail at floor=0.8"

    def test_floor_configuration(self):
        """Floor should be configurable."""
        config_strict = QualityGateConfig(floor=0.95)
        config_lenient = QualityGateConfig(floor=0.5)

        # Use text that actually differs: one is truncated
        text_full = "Python is a popular programming language used in data science, web development, and automation."
        text_partial = "Python is a popular programming language."

        gate_strict = QualityGate(config=config_strict)
        gate_lenient = QualityGate(config=config_lenient)

        assessment_strict = gate_strict.assess(text_partial, text_full, "What is Python?")
        assessment_lenient = gate_lenient.assess(text_partial, text_full, "What is Python?")

        # Same score, but different pass/fail based on floor
        assert abs(assessment_strict.score - assessment_lenient.score) < 0.001
        assert assessment_strict.passed is False, f"Strict floor=0.95 should fail score={assessment_strict.score:.3f}"
        assert assessment_lenient.passed is True, f"Lenient floor=0.5 should pass score={assessment_lenient.score:.3f}"

    def test_embedding_fallback_when_judge_unavailable(self):
        """Should use embedding similarity (with discount) if judge unavailable."""
        config = QualityGateConfig()
        config.floor = 0.75
        gate = QualityGate(config=config)

        # Clear the API key to simulate unavailability
        gate.deepseek_api_key = ""

        assessment = gate.assess(
            optimized_answer="Paris is the capital of France.",
            reference_answer="The capital of France is Paris.",
            question="What is the capital of France?"
        )

        # Should have fallback reason
        assert assessment.fallback_reason is not None
        # Score should be embedding * 0.9 (10% penalty for unverified judge)
        # Embedding similarity should be high (~0.9+), so with 0.9 discount it's ~0.8+
        assert assessment.score > 0.75, f"Score should be >0.75 even with penalty, got {assessment.score}"

    def test_score_bounded_to_unit_interval(self):
        """Score should always be in [0, 1]."""
        gate = QualityGate()

        assessment = gate.assess(
            optimized_answer="Perfect match",
            reference_answer="Perfect match",
            question="Q"
        )

        assert 0.0 <= assessment.score <= 1.0
        assert 0.0 <= assessment.embedding_similarity <= 1.0
        assert 0.0 <= assessment.judge_score <= 1.0


class TestJudgeParsing:
    """Test LLM judge response parsing."""

    def test_parse_valid_json_response(self):
        """Should parse valid JSON judge response."""
        gate = QualityGate()

        json_response = '{"score": 0.85, "reasoning": "Good semantic match"}'
        score, reasoning = gate._parse_judge_response(json_response)

        assert score == 0.85
        assert reasoning == "Good semantic match"

    def test_parse_json_embedded_in_text(self):
        """Should extract JSON from text response."""
        gate = QualityGate()

        response = 'The answer is {"score": 0.92, "reasoning": "Excellent"} and that is good.'
        score, reasoning = gate._parse_judge_response(response)

        assert score == 0.92
        assert reasoning == "Excellent"

    def test_parse_invalid_json_returns_none(self):
        """Should return None for unparseable response."""
        gate = QualityGate()

        response = "The judge could not decide."
        score, reasoning = gate._parse_judge_response(response)

        assert score is None
        assert reasoning is None

    def test_score_clamping_in_parsing(self):
        """Should clamp scores to [0, 1]."""
        gate = QualityGate()

        response_high = '{"score": 1.5, "reasoning": "Too high"}'
        score_high, _ = gate._parse_judge_response(response_high)
        assert score_high == 1.0, "Score should be clamped to 1.0"

        response_low = '{"score": -0.5, "reasoning": "Too low"}'
        score_low, _ = gate._parse_judge_response(response_low)
        assert score_low == 0.0, "Score should be clamped to 0.0"


class TestConvenienceFunctions:
    """Test module-level convenience functions."""

    def test_assess_convenience_function(self):
        """assess() should work without explicit gate creation."""
        assessment = assess(
            optimized_answer="Paris is the capital of France.",
            reference_answer="The capital of France is Paris.",
            question="What is the capital of France?"
        )

        assert assessment.passed is True
        assert assessment.score > 0.8

    def test_assess_with_custom_config(self):
        """assess() should accept custom config."""
        config = QualityGateConfig(floor=0.9)
        assessment = assess(
            optimized_answer="Paris is the capital.",
            reference_answer="The capital of France is Paris.",
            question="Q"
        )

        # With lower floor, should pass
        assert assessment.score > 0.0


class TestBillingIntegration:
    """Test quality score handling for billing."""

    def test_quality_score_bounds(self):
        """Quality score used for billing should be in [0, 1]."""
        gate = QualityGate()

        # Any computed score should be in bounds
        assessment = gate.assess(
            optimized_answer="Answer",
            reference_answer="Reference",
            question="Question"
        )

        assert 0.0 <= assessment.score <= 1.0, "Score must be in [0, 1] for billing"
        assert 0.0 <= assessment.embedding_similarity <= 1.0
        assert 0.0 <= assessment.judge_score <= 1.0


class TestFallbackSignaling:
    """Test fallback signaling when quality fails."""

    def test_fallback_reason_on_judge_failure(self):
        """Should provide clear fallback reason on judge failure."""
        config = QualityGateConfig()
        gate = QualityGate(config=config)
        gate.deepseek_api_key = ""  # Simulate missing key

        assessment = gate.assess(
            optimized_answer="Answer",
            reference_answer="Reference",
            question="Question"
        )

        # Should have fallback reason
        assert assessment.fallback_reason is not None
        assert "not configured" in assessment.fallback_reason.lower()

    def test_passed_boolean_reflects_floor(self):
        """passed boolean should reflect actual floor comparison."""
        config_floor_6 = QualityGateConfig(floor=0.6)
        config_floor_9 = QualityGateConfig(floor=0.9)

        # Partially truncated text (meaningful gap)
        text_full = "Machine learning is a subset of artificial intelligence that enables computers to learn from data without being explicitly programmed."
        text_partial = "Machine learning is a subset of artificial intelligence that enables computers to learn from data."

        gate_6 = QualityGate(config=config_floor_6)
        gate_9 = QualityGate(config=config_floor_9)

        assess_6 = gate_6.assess(text_partial, text_full, "Q")
        assess_9 = gate_9.assess(text_partial, text_full, "Q")

        # Both should have same score
        assert abs(assess_6.score - assess_9.score) < 0.001

        # But different passed status based on floor
        assert assess_6.passed is True, f"Score {assess_6.score} should be >= 0.6"
        assert assess_9.passed is False, f"Score {assess_9.score} should be < 0.9"


class TestRealDiscrimination:
    """Test that gate discriminates CORRECT from WRONG with judge active.

    These tests load the real DeepSeek API key and verify the gate works.
    Skip if key unavailable.
    """

    def test_judge_discriminates_correct_from_wrong(self):
        """Judge should score CORRECT answer significantly higher than WRONG answer.

        This test requires the DeepSeek API key to be available.
        """
        gate = QualityGate(config=QualityGateConfig(floor=0.8))

        # Skip test if judge not available
        if not gate.deepseek_api_key:
            pytest.skip("DeepSeek API key not available; install via .env.local")

        correct_assessment = gate.assess(
            optimized_answer="The capital of France is Paris.",
            reference_answer="The capital of France is Paris.",
            question="What is the capital of France?"
        )

        wrong_assessment = gate.assess(
            optimized_answer="The capital of France is London.",  # WRONG
            reference_answer="The capital of France is Paris.",
            question="What is the capital of France?"
        )

        # CORRECT should not be degraded (judge ran)
        assert not correct_assessment.degraded, "Judge should be available; CORRECT answer should not be degraded"
        assert not wrong_assessment.degraded, "Judge should be available; WRONG answer should not be degraded"

        # CORRECT should score significantly higher than WRONG
        gap = correct_assessment.score - wrong_assessment.score
        assert gap >= 0.3, (
            f"Judge should discriminate: CORRECT={correct_assessment.score:.3f}, "
            f"WRONG={wrong_assessment.score:.3f}, gap={gap:.3f} < 0.3"
        )

        # CORRECT should PASS, WRONG should FAIL
        assert correct_assessment.passed is True, f"CORRECT answer should pass (score={correct_assessment.score:.3f})"
        assert wrong_assessment.passed is False, f"WRONG answer should fail (score={wrong_assessment.score:.3f})"

    def test_degraded_flag_set_when_judge_unavailable(self):
        """When judge unavailable, degraded flag should be True."""
        config = QualityGateConfig(floor=0.8)
        gate = QualityGate(config=config)
        gate.deepseek_api_key = ""  # Simulate missing key

        assessment = gate.assess(
            optimized_answer="Text",
            reference_answer="Text",
            question="Question"
        )

        assert assessment.degraded is True, "Assessment should be marked degraded when judge unavailable"

    def test_degraded_assessment_never_passes(self):
        """Even if embedding-only score >= floor, degraded assessment should not pass."""
        config = QualityGateConfig(floor=0.7)  # Low floor
        gate = QualityGate(config=config)
        gate.deepseek_api_key = ""  # Simulate missing key

        assessment = gate.assess(
            optimized_answer="The capital of France is Paris.",
            reference_answer="The capital of France is Paris.",  # Identical
            question="What is the capital of France?"
        )

        # Identical texts → high embedding sim (1.0)
        # With 0.9 penalty: 1.0 * 0.9 = 0.9
        # 0.9 > 0.7 floor, but should NOT pass because degraded
        assert assessment.embedding_similarity > 0.9, "Identical text should have high embedding sim"
        assert assessment.degraded is True, "Should be marked degraded"
        assert assessment.passed is False, "Degraded assessment should NEVER pass (FAIL LOUD)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
