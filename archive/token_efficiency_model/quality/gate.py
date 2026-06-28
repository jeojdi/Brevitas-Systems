"""Real quality gate using embedding similarity + LLM-as-judge.

Replaces the fake heuristic quality_proxy_score with evidence-based evaluation.
Measures semantic equivalence between optimized and reference answers.
"""

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
import logging

import requests
from sentence_transformers import SentenceTransformer, util


logger = logging.getLogger(__name__)


def _load_deepseek_key() -> str:
    """Load DeepSeek API key from environment or .env.local.

    Returns empty string if not found (will be caught by gate with clear error).
    """
    # First check environment
    key = os.environ.get("Deepseek_api_key", "")
    if key:
        return key

    # Try to load from .env.local
    env_local = Path(__file__).parent.parent.parent / ".env.local"
    if env_local.exists():
        try:
            for line in env_local.read_text().splitlines():
                if "Deepseek_api_key" in line:
                    # Parse KEY=VALUE
                    if "=" in line:
                        _, val = line.split("=", 1)
                        key = val.strip()
                        if key:
                            os.environ["Deepseek_api_key"] = key
                            return key
        except Exception as e:
            logger.warning(f"Failed to read .env.local: {e}")

    return ""


@dataclass
class QualityAssessment:
    """Result of quality assessment."""
    score: float  # 0.0-1.0 retention score
    passed: bool  # whether score >= floor
    embedding_similarity: float  # cosine similarity (0-1)
    judge_score: float  # LLM judge rating (0-1)
    judge_reasoning: Optional[str] = None
    degraded: bool = False  # True if judge unavailable (embedding-only fallback)
    fallback_reason: Optional[str] = None  # clear reason why degraded


@dataclass
class QualityGateConfig:
    """Configuration for quality gate."""
    floor: float = 0.8  # minimum acceptable score
    embedding_weight: float = 0.5  # weight for embedding similarity
    judge_weight: float = 0.5  # weight for LLM judge
    model_name: str = "all-MiniLM-L6-v2"  # sentence-transformers model
    judge_model: str = "deepseek-chat"  # DeepSeek model for judging
    max_judge_retries: int = 1
    timeout: int = 10  # seconds for judge API call


class QualityGate:
    """Quality gate that assesses answer quality using embedding + LLM judge."""

    def __init__(self, config: Optional[QualityGateConfig] = None):
        self.config = config or QualityGateConfig()

        # Load embedding model (fast, local)
        try:
            self.model = SentenceTransformer(self.config.model_name)
            logger.info(f"Loaded embedding model: {self.config.model_name}")
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            self.model = None

        # DeepSeek API setup — try to load from env or .env.local
        self.deepseek_api_key = _load_deepseek_key()
        self.deepseek_base_url = "https://api.deepseek.com/v1"

        if not self.deepseek_api_key:
            logger.error("CRITICAL: Deepseek_api_key not found. Gate will degrade to embedding-only (unreliable for billing)")
            logger.error("  - Set Deepseek_api_key in environment, or")
            logger.error("  - Add 'Deepseek_api_key=sk-...' to .env.local")

    def assess(
        self,
        optimized_answer: str,
        reference_answer: str,
        question: str,
    ) -> QualityAssessment:
        """Assess quality of optimized answer vs reference answer.

        Args:
            optimized_answer: The answer from the optimized/compressed pipeline
            reference_answer: The answer from the full context / baseline
            question: The question being answered (for judge context)

        Returns:
            QualityAssessment with score, passed boolean, and reasoning
        """

        # Step 1: Embedding-based similarity (always available)
        embedding_sim = self._embedding_similarity(optimized_answer, reference_answer)

        # Step 2: LLM-as-judge (may fail gracefully)
        judge_score, judge_reasoning, fallback_reason = self._llm_judge(
            optimized_answer, reference_answer, question
        )

        # Step 3: Combine scores (weighted average)
        is_degraded = False
        if judge_score is not None:
            # Both available: weighted average
            combined_score = (
                self.config.embedding_weight * embedding_sim +
                self.config.judge_weight * judge_score
            )
        else:
            # Judge failed: mark as degraded and use embedding only with penalty
            is_degraded = True
            combined_score = embedding_sim * 0.9  # 10% penalty for unverified judge
            judge_score = embedding_sim  # fallback: use embedding as judge estimate

        # Step 4: Normalize to [0, 1] and apply floor
        final_score = max(0.0, min(1.0, combined_score))
        passed = final_score >= self.config.floor

        # FAIL LOUD: if degraded, never mark as passed (force re-evaluation)
        if is_degraded and passed:
            logger.warning(f"DEGRADED ASSESSMENT: Embedding-only evaluation passed but judge unavailable. Score={final_score:.3f}. Not billing this answer.")
            passed = False

        return QualityAssessment(
            score=final_score,
            passed=passed,
            embedding_similarity=embedding_sim,
            judge_score=judge_score,
            judge_reasoning=judge_reasoning,
            degraded=is_degraded,
            fallback_reason=fallback_reason,
        )

    def _embedding_similarity(self, text1: str, text2: str) -> float:
        """Compute cosine similarity between two texts using embeddings.

        Returns:
            Cosine similarity score 0-1. Returns 0.0 if model unavailable.
        """
        if not self.model:
            return 0.0

        if not text1.strip() or not text2.strip():
            return 0.0 if text1 != text2 else 1.0

        try:
            embeddings = self.model.encode([text1, text2], convert_to_tensor=True)
            similarity = util.cos_sim(embeddings[0], embeddings[1]).item()
            # Clamp to [0, 1]
            return max(0.0, min(1.0, float(similarity)))
        except Exception as e:
            logger.error(f"Embedding similarity computation failed: {e}")
            return 0.0

    def _llm_judge(
        self,
        optimized_answer: str,
        reference_answer: str,
        question: str,
    ) -> tuple[Optional[float], Optional[str], Optional[str]]:
        """Call DeepSeek to judge semantic equivalence.

        Returns:
            (score, reasoning, fallback_reason) where score is None on failure.
            Score is 0-1 representing semantic equivalence.
        """
        if not self.deepseek_api_key:
            return None, None, "DeepSeek API key not configured"

        prompt = self._build_judge_prompt(optimized_answer, reference_answer, question)

        for attempt in range(self.config.max_judge_retries):
            try:
                response = self._call_deepseek(prompt)
                score, reasoning = self._parse_judge_response(response)
                if score is not None:
                    return score, reasoning, None
            except Exception as e:
                logger.warning(f"Judge attempt {attempt + 1} failed: {e}")
                if attempt == self.config.max_judge_retries - 1:
                    return None, None, f"Judge API failed: {str(e)}"

        return None, None, "Judge API exhausted retries"

    def _build_judge_prompt(
        self,
        optimized_answer: str,
        reference_answer: str,
        question: str,
    ) -> str:
        """Build prompt for LLM judge."""
        return f"""You are a semantic equivalence judge. Rate whether the optimized answer is semantically equivalent to the reference answer.

Question: {question}

Reference Answer (full context):
{reference_answer}

Optimized Answer (compressed pipeline):
{optimized_answer}

Rate the semantic equivalence on a scale of 0-1:
- 1.0: Answers are equivalent in meaning and completeness
- 0.8: Minor differences but core meaning preserved
- 0.6: Significant differences but key points overlap
- 0.4: Some overlap but key information missing
- 0.2: Minimal overlap, mostly different
- 0.0: Completely different or incorrect

Respond ONLY with a JSON object on a single line:
{{"score": <number>, "reasoning": "<brief explanation>"}}
"""

    def _call_deepseek(self, prompt: str) -> str:
        """Call DeepSeek API."""
        headers = {
            "Authorization": f"Bearer {self.deepseek_api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.config.judge_model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 100,
            "temperature": 0.5,
        }

        response = requests.post(
            f"{self.deepseek_base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.config.timeout,
        )
        response.raise_for_status()

        content = response.json()["choices"][0]["message"]["content"]
        return content

    def _parse_judge_response(self, response: str) -> tuple[Optional[float], Optional[str]]:
        """Parse judge response JSON."""
        import json

        try:
            # Try to extract JSON if embedded in text
            start_idx = response.find("{")
            end_idx = response.rfind("}") + 1
            if start_idx != -1 and end_idx > start_idx:
                json_str = response[start_idx:end_idx]
                data = json.loads(json_str)
                score = float(data.get("score", 0.0))
                reasoning = str(data.get("reasoning", ""))
                return max(0.0, min(1.0, score)), reasoning
        except Exception as e:
            logger.error(f"Failed to parse judge response: {e}")

        return None, None


# Module-level instance for convenience
_default_gate = None


def get_default_gate(config: Optional[QualityGateConfig] = None) -> QualityGate:
    """Get or create default quality gate instance."""
    global _default_gate
    if _default_gate is None or config is not None:
        _default_gate = QualityGate(config)
    return _default_gate


def assess(
    optimized_answer: str,
    reference_answer: str,
    question: str,
    config: Optional[QualityGateConfig] = None,
) -> QualityAssessment:
    """Assess answer quality (convenience function).

    Args:
        optimized_answer: Answer from optimized pipeline
        reference_answer: Answer from full context
        question: The question being answered
        config: Optional config; uses default if None

    Returns:
        QualityAssessment with score and passed boolean
    """
    gate = get_default_gate(config)
    return gate.assess(optimized_answer, reference_answer, question)
