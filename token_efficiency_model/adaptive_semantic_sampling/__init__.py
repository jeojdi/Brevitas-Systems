"""Adaptive Semantic Sampling for Token Efficiency

Intelligently samples important contexts based on:
- Semantic relevance to the current task
- Frequency of entity/concept mentions
- Temporal recency (for stateful systems)
- Entropy-based importance scoring
"""

from .sampler import AdaptiveSemanticSampler

__all__ = ["AdaptiveSemanticSampler"]
