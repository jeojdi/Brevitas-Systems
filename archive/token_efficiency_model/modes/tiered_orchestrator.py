"""Tiered mode orchestrator for Brevitas Phase 4.

Composes Phase 1-3 components (native caching, RLM retrieval, quality gate)
into three optimization modes with appropriate fallback behavior.

CRITICAL: Quality gate only passes with REAL model outputs.
Absence of evidence = fail-safe: fallback to full context.
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Dict, Any, Callable, Tuple
import logging

from ..optimizers.provider_cache.anthropic import apply_anthropic_cache
from ..optimizers.rlm_orchestrator import RLMOrchestrator
from ..quality.gate import QualityGate, QualityGateConfig, QualityAssessment
from ..agent_communication_compression import CommunicationCompressor
from ..adaptive_semantic_sampling import AdaptiveSemanticSampler
from ..smart_context_pruning import SmartContextPruner


logger = logging.getLogger(__name__)


class BrevitasMode(str, Enum):
    """Tiered optimization modes for Brevitas."""
    LOSSLESS = "lossless"          # cache + RLM retrieval, no lossy compression
    BALANCED = "balanced"            # cache + retrieval + light compression + gate
    MAX_SAVINGS = "max_savings"      # cache + retrieval + aggressive compression + gate


@dataclass
class ModeConfig:
    """Configuration for a tiered optimization mode."""
    mode: BrevitasMode
    # Lossy compression settings (only for balanced/max_savings)
    compression_level: int = 1  # 1-3, ignored for lossless
    prune_budget: int = 5       # context chunks to keep, ignored for lossless
    # Quality gate settings
    quality_floor: float = 0.8  # minimum acceptable retention score
    apply_quality_gate: bool = False  # True for balanced/max_savings
    # Fallback behavior
    fallback_to_full_on_gate_fail: bool = True  # always true for non-lossless
    # RLM retrieval settings
    enable_rlm_retrieval: bool = True  # True for all modes
    retrieval_k: int = 5  # top-k chunks to retrieve


@dataclass
class ModeResult:
    """Result of tiered mode processing."""
    mode: BrevitasMode
    optimized_context: List[str]
    optimized_messages: List[str]
    quality_assessment: Optional[QualityAssessment] = None
    fallback_applied: bool = False  # True if quality gate failed or answers unverified
    metadata: Dict[str, Any] = None  # {"compression_stats", "compression_invoked", ...}

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class TieredModeOrchestrator:
    """Orchestrator that applies tiered optimization based on mode.

    FAIL-SAFE PRINCIPLE: Quality gate only accepts REAL model outputs.
    If answers are unavailable or unverified, fallback_applied=True
    and full context is returned (never ship degraded answer on absent evidence).
    """

    def __init__(
        self,
        quality_gate: Optional[QualityGate] = None,
        rlm_orchestrator: Optional[RLMOrchestrator] = None,
    ):
        """
        Initialize the tiered mode orchestrator.

        Args:
            quality_gate: QualityGate instance; created if None.
            rlm_orchestrator: RLMOrchestrator instance; created if None.
        """
        self.quality_gate = quality_gate or QualityGate()
        self.rlm_orchestrator = rlm_orchestrator or RLMOrchestrator()

    def process(
        self,
        task_text: str,
        incoming_messages: List[str],
        prior_context: List[str],
        config: ModeConfig,
        optimized_answer: Optional[str] = None,
        reference_answer: Optional[str] = None,
        model_caller: Optional[Callable[[List[str], List[str]], Tuple[str, str]]] = None,
    ) -> ModeResult:
        """
        Process a request through the tiered mode pipeline.

        FAIL-SAFE: Quality gate only passes with REAL model outputs.
        If optimized_answer/reference_answer are None and no model_caller provided,
        balanced/max_savings modes fallback to full context (UNVERIFIED).

        Args:
            task_text: The task/question.
            incoming_messages: List of incoming message strings.
            prior_context: List of prior context chunks.
            config: ModeConfig specifying which mode and settings.
            optimized_answer: Real answer from optimized pipeline (for gating).
            reference_answer: Real answer from full context baseline (for gating).
            model_caller: Optional callable(messages, context) -> (optimized_ans, reference_ans).
                If provided and answers are None, will call to generate real outputs for gating.

        Returns:
            ModeResult with optimized context/messages and quality assessment.
        """
        metadata = {"mode": config.mode.value}

        # If mode requires quality gate but answers are missing, try to generate them
        if (config.mode in (BrevitasMode.BALANCED, BrevitasMode.MAX_SAVINGS) and
                optimized_answer is None and reference_answer is None and model_caller is not None):
            try:
                optimized_answer, reference_answer = model_caller(incoming_messages, prior_context)
                metadata["answers_generated_by_caller"] = True
            except Exception as e:
                logger.warning(f"model_caller failed to generate answers: {e}. Treating as UNVERIFIED.")
                optimized_answer = reference_answer = None
                metadata["answers_generated_by_caller"] = False

        if config.mode == BrevitasMode.LOSSLESS:
            return self._process_lossless(
                task_text, incoming_messages, prior_context, config, metadata
            )
        elif config.mode == BrevitasMode.BALANCED:
            return self._process_balanced(
                task_text, incoming_messages, prior_context, config, metadata,
                optimized_answer, reference_answer
            )
        elif config.mode == BrevitasMode.MAX_SAVINGS:
            return self._process_max_savings(
                task_text, incoming_messages, prior_context, config, metadata,
                optimized_answer, reference_answer
            )
        else:
            raise ValueError(f"Unknown mode: {config.mode}")

    def _process_lossless(
        self,
        task_text: str,
        incoming_messages: List[str],
        prior_context: List[str],
        config: ModeConfig,
        metadata: Dict[str, Any],
    ) -> ModeResult:
        """
        Lossless mode: native caching + RLM retrieval, NO lossy compression.

        Assertions:
        - Lossy compressor is NEVER invoked
        - Quality gate is NEVER invoked
        """
        metadata["compression_invoked"] = False
        metadata["quality_gate_invoked"] = False

        # Step 1: Prepare RLM context store and retrieval
        if config.enable_rlm_retrieval:
            store_id = self.rlm_orchestrator.prepare_context(prior_context)
            metadata["rlm_store_id"] = store_id
        else:
            metadata["rlm_store_id"] = None

        # Step 2: Return full context + messages (lossless)
        result = ModeResult(
            mode=BrevitasMode.LOSSLESS,
            optimized_context=prior_context,
            optimized_messages=incoming_messages,
            quality_assessment=None,  # no gate needed for lossless
            fallback_applied=False,
            metadata=metadata,
        )

        return result

    def _process_balanced(
        self,
        task_text: str,
        incoming_messages: List[str],
        prior_context: List[str],
        config: ModeConfig,
        metadata: Dict[str, Any],
        optimized_answer: Optional[str] = None,
        reference_answer: Optional[str] = None,
    ) -> ModeResult:
        """
        Balanced mode: cache + retrieval + light tail-only compression + quality gate.

        Applies lossy compression (lightly) then runs quality gate on REAL answers.
        If answers unavailable (UNVERIFIED) or gate fails, falls back to full context.

        FAIL-SAFE: Without real answers, fallback_applied=True (never ship unverified).
        """
        metadata["compression_invoked"] = True

        # Step 1: Light lossy compression (low level, e.g., level 1)
        compression_level = min(1, config.compression_level)  # cap at 1 for balanced
        compressor = CommunicationCompressor(level=compression_level)
        compressed_msgs, compression_stats = compressor.compress_messages(incoming_messages)
        metadata["compression_stats"] = {
            "original_tokens": compression_stats.original_tokens,
            "compressed_tokens": compression_stats.compressed_tokens,
            "removed_redundant": compression_stats.removed_redundant_sentences,
        }

        # Step 2: Prepare RLM context (full, not pruned)
        if config.enable_rlm_retrieval:
            store_id = self.rlm_orchestrator.prepare_context(prior_context)
            metadata["rlm_store_id"] = store_id

        # Step 3: Quality gate on REAL answers only
        # FAIL-SAFE: If answers are None, treat as UNVERIFIED and fallback
        if optimized_answer is None or reference_answer is None:
            logger.warning(
                f"Balanced mode: answers unavailable (UNVERIFIED). "
                f"Fail-safe: falling back to full context without gating."
            )
            metadata["quality_gate_invoked"] = False
            metadata["unverified_reason"] = "answers_unavailable"
            return ModeResult(
                mode=BrevitasMode.BALANCED,
                optimized_context=prior_context,
                optimized_messages=incoming_messages,
                quality_assessment=None,  # no assessment without real answers
                fallback_applied=True,  # FAIL-SAFE
                metadata=metadata,
            )

        # Real answers available: gate on them
        metadata["quality_gate_invoked"] = True
        assessment = self.quality_gate.assess(
            optimized_answer=optimized_answer,
            reference_answer=reference_answer,
            question=task_text,
        )
        metadata["quality_assessment"] = {
            "score": assessment.score,
            "passed": assessment.passed,
            "embedding_sim": assessment.embedding_similarity,
            "judge_score": assessment.judge_score,
        }

        # Step 4: Fallback on gate failure
        if not assessment.passed and config.fallback_to_full_on_gate_fail:
            logger.warning(
                f"Quality gate failed in balanced mode (score={assessment.score:.3f}). "
                f"Falling back to full context."
            )
            return ModeResult(
                mode=BrevitasMode.BALANCED,
                optimized_context=prior_context,
                optimized_messages=incoming_messages,
                quality_assessment=assessment,
                fallback_applied=True,
                metadata=metadata,
            )

        return ModeResult(
            mode=BrevitasMode.BALANCED,
            optimized_context=prior_context,
            optimized_messages=compressed_msgs,
            quality_assessment=assessment,
            fallback_applied=False,
            metadata=metadata,
        )

    def _process_max_savings(
        self,
        task_text: str,
        incoming_messages: List[str],
        prior_context: List[str],
        config: ModeConfig,
        metadata: Dict[str, Any],
        optimized_answer: Optional[str] = None,
        reference_answer: Optional[str] = None,
    ) -> ModeResult:
        """
        Max savings mode: cache + retrieval + aggressive lossy compression + quality gate.

        Applies full lossy pipeline then runs quality gate on REAL answers.
        MANDATORY fallback on gate failure or unverified answers (never ship degraded).

        CRITICAL SAFETY: Without real answers, MANDATORY fallback_applied=True.
        Absence of evidence for quality = assume worst and return full context.
        """
        metadata["compression_invoked"] = True

        # Step 1: Aggressive lossy compression (full level)
        compressor = CommunicationCompressor(level=config.compression_level)
        compressed_msgs, compression_stats = compressor.compress_messages(incoming_messages)
        metadata["compression_stats"] = {
            "original_tokens": compression_stats.original_tokens,
            "compressed_tokens": compression_stats.compressed_tokens,
            "removed_redundant": compression_stats.removed_redundant_sentences,
        }

        # Step 2: Aggressive context pruning (semantic sampling + pruning)
        sampler = AdaptiveSemanticSampler(
            budget=max(1, config.prune_budget),
            relevance_weight=0.35,
            frequency_weight=0.25,
            recency_weight=0.20,
            entropy_weight=0.20,
            novelty_weight=0.40,
        )
        sampled_context, sampling_metrics = sampler.sample(
            contexts=prior_context,
            task_text=task_text,
            adaptive_budget=config.prune_budget,
        )
        metadata["sampling_metrics"] = sampling_metrics

        pruner = SmartContextPruner(budget=max(1, int(config.prune_budget * 0.8)))
        pruned_context, pruning_scores = pruner.prune(task_text, sampled_context)
        metadata["pruning_scores"] = pruning_scores

        # Step 3: Prepare RLM context (on the pruned context)
        if config.enable_rlm_retrieval:
            store_id = self.rlm_orchestrator.prepare_context(pruned_context)
            metadata["rlm_store_id"] = store_id

        # Step 4: Quality gate on REAL answers (MANDATORY in max_savings)
        # FAIL-SAFE: If answers are None, MANDATORY fallback (never ship unverified degraded answer)
        if optimized_answer is None or reference_answer is None:
            logger.critical(
                f"Max_savings mode: answers unavailable (UNVERIFIED). "
                f"MANDATORY fallback to full context (fail-safe: never ship degraded answer)."
            )
            metadata["quality_gate_invoked"] = False
            metadata["unverified_reason"] = "answers_unavailable"
            return ModeResult(
                mode=BrevitasMode.MAX_SAVINGS,
                optimized_context=prior_context,
                optimized_messages=incoming_messages,
                quality_assessment=None,  # no assessment without real answers
                fallback_applied=True,  # MANDATORY FAIL-SAFE
                metadata=metadata,
            )

        # Real answers available: gate on them (MANDATORY pass required)
        metadata["quality_gate_invoked"] = True
        assessment = self.quality_gate.assess(
            optimized_answer=optimized_answer,
            reference_answer=reference_answer,
            question=task_text,
        )
        metadata["quality_assessment"] = {
            "score": assessment.score,
            "passed": assessment.passed,
            "embedding_sim": assessment.embedding_similarity,
            "judge_score": assessment.judge_score,
        }

        # Step 5: MANDATORY fallback on gate failure (never ship degraded answer)
        if not assessment.passed:
            logger.critical(
                f"Quality gate FAILED in max_savings mode (score={assessment.score:.3f}). "
                f"MANDATORY fallback to full context (no degraded answer shipped)."
            )
            return ModeResult(
                mode=BrevitasMode.MAX_SAVINGS,
                optimized_context=prior_context,
                optimized_messages=incoming_messages,
                quality_assessment=assessment,
                fallback_applied=True,  # MANDATORY
                metadata=metadata,
            )

        return ModeResult(
            mode=BrevitasMode.MAX_SAVINGS,
            optimized_context=pruned_context,
            optimized_messages=compressed_msgs,
            quality_assessment=assessment,
            fallback_applied=False,
            metadata=metadata,
        )

    def apply_native_caching(
        self,
        request_body: dict,
        provider: str = "anthropic",
    ) -> dict:
        """
        Apply provider-native caching to the request body.

        Args:
            request_body: The request body (e.g., Anthropic API body).
            provider: Provider name ("anthropic", "openai", "deepseek", etc.).

        Returns:
            Modified request body with cache_control injected (if applicable).
        """
        if provider == "anthropic":
            return apply_anthropic_cache(request_body)
        # Other providers (OpenAI, DeepSeek) handle caching automatically
        # just ensure prefix stability
        return request_body
