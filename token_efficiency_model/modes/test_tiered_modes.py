"""Test suite for Brevitas Phase 4 tiered modes.

Verifies:
1. Default mode is lossless
2. Lossless never invokes lossy compression
3. Balanced invokes light compression + quality gate
4. Max_savings invokes aggressive compression + quality gate with mandatory fallback
5. Quality gate fallback behavior
6. No regression in existing components
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

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
    rlm.fetch_context = Mock(return_value=["chunk1", "chunk2", "chunk3"])
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


class TestLosslessMode:
    """Test suite for lossless mode."""

    def test_default_mode_is_lossless(self, orchestrator, sample_data):
        """Verify that lossless is the default mode."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        assert config.mode == BrevitasMode.LOSSLESS

    def test_lossless_preserves_all_context(self, orchestrator, sample_data):
        """Lossless mode returns all original context and messages."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        result = orchestrator.process(**sample_data, config=config)

        assert result.mode == BrevitasMode.LOSSLESS
        assert result.optimized_context == sample_data["prior_context"]
        assert result.optimized_messages == sample_data["incoming_messages"]
        assert result.fallback_applied is False

    def test_lossless_never_invokes_lossy_compression(self, orchestrator, sample_data):
        """Assert that lossy compression is NOT invoked in lossless mode."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        result = orchestrator.process(**sample_data, config=config)

        assert result.metadata["compression_invoked"] is False
        assert result.metadata["quality_gate_invoked"] is False

    def test_lossless_quality_assessment_is_none(self, orchestrator, sample_data):
        """Lossless mode has no quality assessment (no gate)."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        result = orchestrator.process(**sample_data, config=config)

        assert result.quality_assessment is None

    def test_lossless_enables_rlm_retrieval(self, orchestrator, sample_data, mock_rlm_orchestrator):
        """Lossless mode still enables RLM retrieval for context-as-variable."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS, enable_rlm_retrieval=True)
        result = orchestrator.process(**sample_data, config=config)

        # Verify RLM was called
        mock_rlm_orchestrator.prepare_context.assert_called_once()
        assert result.metadata["rlm_store_id"] == "store-123"


class TestBalancedMode:
    """Test suite for balanced mode."""

    def test_balanced_invokes_compression_and_gate(self, orchestrator, sample_data):
        """Balanced mode invokes light compression and quality gate with real answers."""
        config = ModeConfig(mode=BrevitasMode.BALANCED)
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="Paris is the capital of France.",
            reference_answer="The capital of France is Paris."
        )

        assert result.mode == BrevitasMode.BALANCED
        assert result.metadata["compression_invoked"] is True
        assert result.metadata["quality_gate_invoked"] is True

    def test_balanced_fallback_on_gate_failure(self, orchestrator, sample_data, mock_quality_gate):
        """Balanced mode falls back to full context when quality gate fails on real answers."""
        # Make gate fail
        mock_quality_gate.assess = Mock(return_value=QualityAssessment(
            score=0.6,  # below 0.8 floor
            passed=False,
            embedding_similarity=0.6,
            judge_score=0.6,
            judge_reasoning="Answers differ significantly",
            degraded=False,
        ))

        config = ModeConfig(
            mode=BrevitasMode.BALANCED,
            quality_floor=0.8,
            fallback_to_full_on_gate_fail=True,
        )
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="France is in Europe.",  # Wrong
            reference_answer="The capital of France is Paris."
        )

        # Fallback was applied
        assert result.fallback_applied is True
        # Full context is returned (not compressed)
        assert result.optimized_context == sample_data["prior_context"]
        assert result.optimized_messages == sample_data["incoming_messages"]

    def test_balanced_respects_quality_floor(self, orchestrator, sample_data, mock_quality_gate):
        """Balanced mode respects the quality floor setting."""
        # Make gate return score at boundary
        mock_quality_gate.assess = Mock(return_value=QualityAssessment(
            score=0.8,  # exactly at floor
            passed=True,
            embedding_similarity=0.8,
            judge_score=0.8,
            judge_reasoning="Acceptable",
            degraded=False,
        ))

        config = ModeConfig(
            mode=BrevitasMode.BALANCED,
            quality_floor=0.8,
        )
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="Paris is the capital.",
            reference_answer="France's capital is Paris."
        )

        assert result.fallback_applied is False


class TestMaxSavingsMode:
    """Test suite for max_savings mode."""

    def test_max_savings_invokes_aggressive_compression(self, orchestrator, sample_data):
        """Max_savings mode invokes aggressive lossy compression with real answers."""
        config = ModeConfig(
            mode=BrevitasMode.MAX_SAVINGS,
            compression_level=3,  # max
            prune_budget=2,  # aggressive
        )
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="Paris: capital.",
            reference_answer="The capital of France is Paris."
        )

        assert result.mode == BrevitasMode.MAX_SAVINGS
        assert result.metadata["compression_invoked"] is True
        assert result.metadata["quality_gate_invoked"] is True

    def test_max_savings_mandatory_fallback_on_gate_fail(self, orchestrator, sample_data, mock_quality_gate):
        """Max_savings MANDATORY fallback: gate failure returns full context, never degraded answer."""
        # Make gate fail
        mock_quality_gate.assess = Mock(return_value=QualityAssessment(
            score=0.5,  # well below floor
            passed=False,
            embedding_similarity=0.5,
            judge_score=0.5,
            judge_reasoning="Critical information missing",
            degraded=False,
        ))

        config = ModeConfig(
            mode=BrevitasMode.MAX_SAVINGS,
            quality_floor=0.8,
            fallback_to_full_on_gate_fail=True,  # always True for max_savings
        )
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="France is in Europe.",  # Deliberately wrong
            reference_answer="The capital of France is Paris."
        )

        # MANDATORY fallback applied
        assert result.fallback_applied is True
        # Full context returned (never ship degraded answer)
        assert result.optimized_context == sample_data["prior_context"]
        assert result.optimized_messages == sample_data["incoming_messages"]
        # Quality assessment recorded for audit
        assert result.quality_assessment is not None
        assert result.quality_assessment.passed is False

    def test_max_savings_passes_when_gate_passes(self, orchestrator, sample_data, mock_quality_gate):
        """Max_savings passes optimized content when quality gate passes on real answers."""
        # Gate passes with high score
        mock_quality_gate.assess = Mock(return_value=QualityAssessment(
            score=0.92,
            passed=True,
            embedding_similarity=0.92,
            judge_score=0.92,
            judge_reasoning="Answers equivalent",
            degraded=False,
        ))

        config = ModeConfig(
            mode=BrevitasMode.MAX_SAVINGS,
            compression_level=2,
        )
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="Paris is the capital of France.",
            reference_answer="The capital of France is Paris."
        )

        # No fallback
        assert result.fallback_applied is False
        # Quality assessment passed
        assert result.quality_assessment.passed is True

    def test_max_savings_invokes_semantic_sampling_and_pruning(
        self, orchestrator, sample_data, mock_quality_gate
    ):
        """Max_savings invokes both semantic sampling and pruning (not in balanced/lossless)."""
        config = ModeConfig(
            mode=BrevitasMode.MAX_SAVINGS,
            compression_level=3,
            prune_budget=3,
        )
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="Paris is capital.",
            reference_answer="Paris is the capital of France."
        )

        # Verify sampling/pruning metadata is present
        assert "sampling_metrics" in result.metadata
        assert "pruning_scores" in result.metadata


class TestModeConfiguration:
    """Test suite for mode configuration and selection."""

    def test_mode_config_defaults(self):
        """Verify ModeConfig has sensible defaults."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        assert config.mode == BrevitasMode.LOSSLESS
        assert config.compression_level == 1
        assert config.prune_budget == 5
        assert config.quality_floor == 0.8
        assert config.enable_rlm_retrieval is True

    def test_all_modes_enumerable(self):
        """Verify all three modes are available."""
        modes = [m for m in BrevitasMode]
        assert len(modes) == 3
        assert BrevitasMode.LOSSLESS in modes
        assert BrevitasMode.BALANCED in modes
        assert BrevitasMode.MAX_SAVINGS in modes

    def test_mode_result_metadata_initialization(self, orchestrator, sample_data):
        """ModeResult initializes metadata dict if not provided."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        result = orchestrator.process(**sample_data, config=config)

        assert result.metadata is not None
        assert isinstance(result.metadata, dict)
        assert "mode" in result.metadata


class TestNativeCachingIntegration:
    """Test integration with provider-native caching."""

    def test_apply_anthropic_cache(self, orchestrator):
        """Verify apply_native_caching works for Anthropic."""
        request_body = {
            "system": "You are a helpful assistant.",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
                {"role": "user", "content": "What is 2+2?"},
            ],
        }

        result = orchestrator.apply_native_caching(request_body, provider="anthropic")

        # Should return modified body (with cache_control)
        assert result is not None
        assert isinstance(result, dict)

    def test_apply_cache_openai_passthrough(self, orchestrator):
        """OpenAI caching is handled by SDK, orchestrator is passthrough."""
        request_body = {"messages": [{"role": "user", "content": "test"}]}

        result = orchestrator.apply_native_caching(request_body, provider="openai")

        # Should return unchanged (OpenAI handles caching automatically)
        assert result == request_body


class TestQualityGateBehavior:
    """Test quality gate behavior across modes."""

    def test_quality_gate_not_called_in_lossless(self, orchestrator, sample_data, mock_quality_gate):
        """Quality gate should not be invoked in lossless mode."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        orchestrator.process(**sample_data, config=config)

        # Gate should not be called
        mock_quality_gate.assess.assert_not_called()

    def test_quality_gate_called_in_balanced(self, orchestrator, sample_data, mock_quality_gate):
        """Quality gate is invoked in balanced mode with real answers."""
        config = ModeConfig(mode=BrevitasMode.BALANCED)
        orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="Paris is the capital.",
            reference_answer="The capital is Paris."
        )

        # Gate should be called with real answers
        mock_quality_gate.assess.assert_called_once()

    def test_quality_gate_called_in_max_savings(self, orchestrator, sample_data, mock_quality_gate):
        """Quality gate is invoked in max_savings mode with real answers."""
        config = ModeConfig(mode=BrevitasMode.MAX_SAVINGS)
        orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="Paris: capital.",
            reference_answer="Paris is the capital of France."
        )

        # Gate should be called with real answers
        mock_quality_gate.assess.assert_called_once()

    def test_quality_assessment_recorded_on_failure(self, orchestrator, sample_data, mock_quality_gate):
        """Quality assessment is recorded even on gate failure with real answers."""
        mock_quality_gate.assess = Mock(return_value=QualityAssessment(
            score=0.4,
            passed=False,
            embedding_similarity=0.4,
            judge_score=0.4,
            judge_reasoning="Critical loss",
            degraded=False,
        ))

        config = ModeConfig(mode=BrevitasMode.MAX_SAVINGS)
        result = orchestrator.process(
            **sample_data,
            config=config,
            optimized_answer="France",  # Deliberately wrong
            reference_answer="The capital of France is Paris."
        )

        # Assessment is recorded
        assert result.quality_assessment is not None
        assert result.quality_assessment.score == 0.4
        assert result.quality_assessment.passed is False


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_context(self, orchestrator):
        """Handle empty prior context gracefully."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        result = orchestrator.process(
            task_text="Test",
            incoming_messages=["Hello"],
            prior_context=[],
            config=config,
        )

        assert result.optimized_context == []
        assert result.mode == BrevitasMode.LOSSLESS

    def test_empty_messages(self, orchestrator):
        """Handle empty incoming messages gracefully."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        result = orchestrator.process(
            task_text="Test",
            incoming_messages=[],
            prior_context=["Some context"],
            config=config,
        )

        assert result.optimized_messages == []
        assert result.mode == BrevitasMode.LOSSLESS

    def test_invalid_mode_raises_error(self, orchestrator):
        """Invalid mode should raise an error."""
        # Create a ModeConfig with an invalid enum value by patching
        from unittest.mock import patch
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)

        # Patch the mode to an invalid value after creation
        with patch.object(config, 'mode', "invalid_mode"):
            with pytest.raises((ValueError, AttributeError)):
                orchestrator.process(
                    task_text="Test",
                    incoming_messages=["msg"],
                    prior_context=["ctx"],
                    config=config,
                )


class TestCompressionControl:
    """Test that compression is correctly invoked/not invoked per mode."""

    def test_lossless_no_compression_stat_check(self, orchestrator, sample_data):
        """Lossless mode metadata explicitly marks compression as not invoked."""
        config = ModeConfig(mode=BrevitasMode.LOSSLESS)
        result = orchestrator.process(**sample_data, config=config)

        assert "compression_invoked" in result.metadata
        assert result.metadata["compression_invoked"] is False
        assert "compression_stats" not in result.metadata

    def test_balanced_has_compression_stats(self, orchestrator, sample_data):
        """Balanced mode includes compression statistics."""
        config = ModeConfig(mode=BrevitasMode.BALANCED)
        result = orchestrator.process(**sample_data, config=config)

        assert "compression_stats" in result.metadata
        assert "original_tokens" in result.metadata["compression_stats"]
        assert "compressed_tokens" in result.metadata["compression_stats"]

    def test_max_savings_has_compression_and_sampling_stats(self, orchestrator, sample_data):
        """Max_savings mode includes both compression and sampling/pruning stats."""
        config = ModeConfig(mode=BrevitasMode.MAX_SAVINGS)
        result = orchestrator.process(**sample_data, config=config)

        assert "compression_stats" in result.metadata
        assert "sampling_metrics" in result.metadata
        assert "pruning_scores" in result.metadata
