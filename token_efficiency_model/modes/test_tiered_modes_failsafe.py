"""Test suite for Brevitas Phase 4 tiered modes — FAIL-SAFE quality gate with real answers.

CRITICAL TESTS:
1. Balanced/max_savings with REAL answers: gate on real outputs
2. Balanced/max_savings WITHOUT answers: FALLBACK (UNVERIFIED fail-safe)
3. Max_savings with WRONG optimized answer: gate fails → MANDATORY fallback
4. Lossless: never invokes compressor or gate (unchanged)
"""

import pytest
from unittest.mock import Mock

from .tiered_orchestrator import (
    BrevitasMode,
    ModeConfig,
    TieredModeOrchestrator,
)
from ..quality.gate import QualityAssessment


@pytest.fixture
def mock_quality_gate():
    """Mock quality gate for testing."""
    gate = Mock()
    # By default, gate passes
    gate.assess = Mock(return_value=QualityAssessment(
        score=0.95,
        passed=True,
        embedding_similarity=0.95,
        judge_score=0.95,
        judge_reasoning="Answers are equivalent",
        degraded=False,
    ))
    return gate


@pytest.fixture
def mock_rlm_orchestrator():
    """Mock RLM orchestrator for testing."""
    rlm = Mock()
    rlm.prepare_context = Mock(return_value="store-123")
    return rlm


@pytest.fixture
def orchestrator(mock_quality_gate, mock_rlm_orchestrator):
    """Create a TieredModeOrchestrator with mocks."""
    return TieredModeOrchestrator(
        quality_gate=mock_quality_gate,
        rlm_orchestrator=mock_rlm_orchestrator,
    )


@pytest.fixture
def sample_data():
    """Sample task data for testing."""
    return {
        "task_text": "What is the capital of France?",
        "incoming_messages": ["Paris is the capital.", "It's in north-central France."],
        "prior_context": [
            "France is a country in Europe.",
            "Paris is the largest city in France.",
            "The Eiffel Tower is in Paris.",
            "France is known for wine and cheese.",
            "The population of France is about 67 million.",
        ],
    }


class TestFailSafeBalancedMode:
    """Test fail-safe behavior in balanced mode."""

    def test_balanced_with_real_answers_gates_on_them(self, orchestrator, sample_data, mock_quality_gate):
        """Balanced mode with real answers invokes gate on real outputs."""
        config = ModeConfig(mode=BrevitasMode.BALANCED)
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="Paris is the capital of France.",
            reference_answer="The capital of France is Paris."
        )

        # Gate should be invoked
        assert result.metadata["quality_gate_invoked"] is True
        # Gate was called with real answers
        mock_quality_gate.assess.assert_called_once()
        call_kwargs = mock_quality_gate.assess.call_args[1]
        assert call_kwargs["optimized_answer"] == "Paris is the capital of France."
        assert call_kwargs["reference_answer"] == "The capital of France is Paris."

    def test_balanced_without_answers_failsafe_fallback(self, orchestrator, sample_data):
        """Balanced mode WITHOUT answers: FAIL-SAFE fallback (UNVERIFIED)."""
        config = ModeConfig(mode=BrevitasMode.BALANCED)
        result = orchestrator.process(**sample_data, config=config)  # no answers

        # FAIL-SAFE: fallback applied
        assert result.fallback_applied is True
        # No gate invoked (answers unavailable)
        assert result.metadata["quality_gate_invoked"] is False
        # No assessment (unverified)
        assert result.quality_assessment is None
        # Full context returned
        assert result.optimized_context == sample_data["prior_context"]
        assert result.optimized_messages == sample_data["incoming_messages"]

    def test_balanced_gate_fails_on_real_answers(self, orchestrator, sample_data, mock_quality_gate):
        """Balanced mode: gate failure on real answers triggers fallback."""
        # Make gate fail
        mock_quality_gate.assess = Mock(return_value=QualityAssessment(
            score=0.6,
            passed=False,
            embedding_similarity=0.6,
            judge_score=0.6,
            judge_reasoning="Critical info missing",
            degraded=False,
        ))

        config = ModeConfig(mode=BrevitasMode.BALANCED, quality_floor=0.8)
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="France is in Europe.",  # Wrong answer
            reference_answer="The capital of France is Paris."
        )

        # Fallback applied
        assert result.fallback_applied is True
        # Assessment recorded
        assert result.quality_assessment is not None
        assert result.quality_assessment.passed is False
        # Full context returned
        assert result.optimized_context == sample_data["prior_context"]
        assert result.optimized_messages == sample_data["incoming_messages"]


class TestFailSafeMaxSavingsMode:
    """Test MANDATORY fail-safe behavior in max_savings mode."""

    def test_max_savings_with_real_answers_gates_on_them(self, orchestrator, sample_data, mock_quality_gate):
        """Max_savings mode with real answers invokes gate on real outputs."""
        config = ModeConfig(mode=BrevitasMode.MAX_SAVINGS)
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="Paris is France's capital.",
            reference_answer="The capital of France is Paris, a major city."
        )

        # Gate should be invoked
        assert result.metadata["quality_gate_invoked"] is True
        # Gate was called with real answers
        mock_quality_gate.assess.assert_called_once()
        call_kwargs = mock_quality_gate.assess.call_args[1]
        assert call_kwargs["optimized_answer"] == "Paris is France's capital."
        assert call_kwargs["reference_answer"] == "The capital of France is Paris, a major city."

    def test_max_savings_without_answers_mandatory_fallback(self, orchestrator, sample_data):
        """Max_savings WITHOUT answers: MANDATORY FAIL-SAFE fallback (UNVERIFIED)."""
        config = ModeConfig(mode=BrevitasMode.MAX_SAVINGS)
        result = orchestrator.process(**sample_data, config=config)  # no answers

        # MANDATORY FAIL-SAFE: fallback applied
        assert result.fallback_applied is True
        # No gate invoked (answers unavailable)
        assert result.metadata["quality_gate_invoked"] is False
        # No assessment (unverified)
        assert result.quality_assessment is None
        # Full context returned (NEVER ship degraded)
        assert result.optimized_context == sample_data["prior_context"]
        assert result.optimized_messages == sample_data["incoming_messages"]

    def test_max_savings_gate_fails_mandatory_fallback(self, orchestrator, sample_data, mock_quality_gate):
        """Max_savings: gate failure triggers MANDATORY fallback (never ship degraded)."""
        # Make gate fail (degraded answer detected)
        mock_quality_gate.assess = Mock(return_value=QualityAssessment(
            score=0.5,  # well below floor
            passed=False,
            embedding_similarity=0.5,
            judge_score=0.5,
            judge_reasoning="Critical information missing in compressed version",
            degraded=False,
        ))

        config = ModeConfig(mode=BrevitasMode.MAX_SAVINGS, quality_floor=0.8)
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="Something about France.",  # Deliberately wrong
            reference_answer="The capital of France is Paris, located on the Seine river."
        )

        # MANDATORY fallback
        assert result.fallback_applied is True
        # Assessment recorded for audit
        assert result.quality_assessment is not None
        assert result.quality_assessment.passed is False
        assert result.quality_assessment.score == 0.5
        # Full context returned (never ship degraded)
        assert result.optimized_context == sample_data["prior_context"]
        assert result.optimized_messages == sample_data["incoming_messages"]

    def test_max_savings_passes_only_on_high_quality(self, orchestrator, sample_data, mock_quality_gate):
        """Max_savings passes compressed answer ONLY if gate passes on real answers."""
        # Gate passes with high score
        mock_quality_gate.assess = Mock(return_value=QualityAssessment(
            score=0.92,
            passed=True,
            embedding_similarity=0.92,
            judge_score=0.92,
            judge_reasoning="Answers are equivalent",
            degraded=False,
        ))

        config = ModeConfig(mode=BrevitasMode.MAX_SAVINGS)
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="Paris is the capital of France.",
            reference_answer="The capital of France is Paris."
        )

        # No fallback
        assert result.fallback_applied is False
        # Compressed answer shipped (gate passed)
        # Optimized messages are compressed (not equal to incoming)
        assert result.quality_assessment.passed is True


class TestLosslessNeverCompresses:
    """Test that lossless mode never invokes lossy compression."""

    def test_lossless_never_invokes_compression(self, orchestrator, sample_data):
        """Lossless mode never invokes CommunicationCompressor."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        result = orchestrator.process(**sample_data, config=config)

        assert result.metadata["compression_invoked"] is False
        assert "compression_stats" not in result.metadata

    def test_lossless_never_invokes_gate(self, orchestrator, sample_data, mock_quality_gate):
        """Lossless mode never invokes quality gate."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        result = orchestrator.process(**sample_data, config=config)

        # Gate should NOT be called
        mock_quality_gate.assess.assert_not_called()
        assert result.metadata["quality_gate_invoked"] is False

    def test_lossless_ignores_answer_params(self, orchestrator, sample_data):
        """Lossless mode ignores answer params (never gates)."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="Some answer",
            reference_answer="Some other answer"
        )

        # Lossless returns full context regardless
        assert result.optimized_context == sample_data["prior_context"]
        assert result.optimized_messages == sample_data["incoming_messages"]
        assert result.fallback_applied is False


class TestModelCallerIntegration:
    """Test integration with model_caller for generating real answers."""

    def test_max_savings_uses_model_caller_to_generate_answers(self, orchestrator, sample_data):
        """Max_savings can use model_caller to generate real answers for gating."""
        def mock_model_caller(messages, context):
            # Simulate model generating answers
            optimized = "Paris is France's capital."
            reference = "The capital of France is Paris."
            return (optimized, reference)

        config = ModeConfig(mode=BrevitasMode.MAX_SAVINGS)
        result = orchestrator.process(
            **sample_data,
            config=config,
            model_caller=mock_model_caller
        )

        # Answers were generated
        assert result.metadata.get("answers_generated_by_caller") is True
        # Gate was invoked (answers available)
        assert result.metadata["quality_gate_invoked"] is True

    def test_max_savings_fallback_when_model_caller_fails(self, orchestrator, sample_data):
        """Max_savings falls back if model_caller raises exception."""
        def failing_model_caller(messages, context):
            raise RuntimeError("Model unavailable")

        config = ModeConfig(mode=BrevitasMode.MAX_SAVINGS)
        result = orchestrator.process(
            **sample_data,
            config=config,
            model_caller=failing_model_caller
        )

        # Model caller failed, treated as unverified
        assert result.metadata.get("answers_generated_by_caller") is False
        # Mandatory fallback
        assert result.fallback_applied is True
        # Full context returned
        assert result.optimized_context == sample_data["prior_context"]
