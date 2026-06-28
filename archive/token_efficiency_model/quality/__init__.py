"""Quality gate module for real answer assessment.

This module provides the Phase 3 quality gate to replace the fake heuristic quality_proxy_score.
Uses embedding cosine similarity + LLM-as-judge (DeepSeek) to evaluate answer quality.
"""

from .gate import QualityAssessment, assess, QualityGateConfig

__all__ = ["QualityAssessment", "assess", "QualityGateConfig"]
