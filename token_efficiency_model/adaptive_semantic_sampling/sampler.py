"""Adaptive Semantic Sampler Implementation."""

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Set, Tuple


@dataclass
class SamplingMetrics:
    """Metrics for sampling decision"""
    relevance_score: float
    importance_score: float
    frequency_score: float
    recency_score: float
    combined_score: float


class AdaptiveSemanticSampler:
    """
    Intelligently samples critical contexts to maximize information retention
    while minimizing token usage.
    
    Uses multi-modal scoring:
    - Semantic relevance (TF-IDF style scoring)
    - Entity frequency (how often concepts appear)
    - Temporal recency (weighted by position in history)
    - Information entropy (how much unique information)
    """
    
    def __init__(self, 
                 budget: int = 4,
                 relevance_weight: float = 0.35,
                 frequency_weight: float = 0.25,
                 recency_weight: float = 0.20,
                 entropy_weight: float = 0.20,
                 novelty_weight: float = 0.40):
        self.budget = budget
        self.relevance_weight = relevance_weight
        self.frequency_weight = frequency_weight
        self.recency_weight = recency_weight
        self.entropy_weight = entropy_weight
        self.novelty_weight = novelty_weight
        
        # Vocabularies for semantic understanding
        self.task_keywords: Set[str] = set()
        self.entity_index: Dict[str, List[int]] = defaultdict(list)
        self.concept_frequency: Counter = Counter()

    def _normalized_weights(self) -> Tuple[float, float, float, float]:
        total = (
            self.relevance_weight
            + self.frequency_weight
            + self.recency_weight
            + self.entropy_weight
        )
        if total <= 0:
            return 0.35, 0.25, 0.20, 0.20

        return (
            self.relevance_weight / total,
            self.frequency_weight / total,
            self.recency_weight / total,
            self.entropy_weight / total,
        )
    
    def _extract_keywords(self, text: str) -> Set[str]:
        """Extract important keywords from text"""
        # Remove common stop words
        stop_words = {
            'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
            'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'be', 'been',
            'been', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
            'could', 'should', 'may', 'might', 'must', 'can', 'this', 'that',
            'these', 'those', 'i', 'you', 'he', 'she', 'it', 'we', 'they'
        }
        
        words = re.findall(r"[a-zA-Z]{3,}", text.lower())
        keywords = {w for w in words 
                   if len(w) > 3 and w not in stop_words and w.isalpha()}
        return keywords
    
    def _calculate_relevance(self, context: str, task_text: str) -> float:
        """Calculate semantic relevance to task"""
        task_keywords = self._extract_keywords(task_text)
        context_keywords = self._extract_keywords(context)
        
        if not task_keywords:
            return 0.0
        
        # Jaccard similarity
        intersection = len(task_keywords & context_keywords)
        union = len(task_keywords | context_keywords)
        
        if union == 0:
            return 0.0
        
        relevance = intersection / union
        
        # Boost for exact phrase matches
        task_text_lower = task_text.lower()
        for keyword in context_keywords:
            if keyword in task_text_lower:
                relevance = min(1.0, relevance + 0.1)
        
        return min(1.0, relevance)
    
    def _calculate_frequency_score(self, context: str, 
                                  all_contexts: List[str]) -> float:
        """Score based on entity/concept frequency"""
        keywords = self._extract_keywords(context)
        
        if not keywords:
            return 0.0
        
        # Count keyword occurrences across all contexts
        total_occurrences = 0
        for keyword in keywords:
            for other_context in all_contexts:
                if keyword in other_context.lower():
                    total_occurrences += 1
        
        # Normalize: frequent concepts are important
        avg_frequency = total_occurrences / len(keywords) if keywords else 0
        freq_score = min(1.0, avg_frequency / len(all_contexts))
        
        return freq_score
    
    def _calculate_recency_score(self, index: int, total_contexts: int) -> float:
        """Score based on position recency (recently mentioned contexts are more relevant)"""
        if total_contexts == 0:
            return 0.0
        
        # Exponential decay: recent contexts score higher
        position_ratio = index / total_contexts
        recency = math.exp(-2.0 * (1.0 - position_ratio))
        
        return min(1.0, recency)
    
    def _calculate_entropy(self, context: str, 
                          other_contexts: List[str]) -> float:
        """Calculate information entropy - unique information content"""
        keywords = self._extract_keywords(context)
        
        if not keywords:
            return 0.0
        
        # Count how many keywords are unique to this context
        all_other_keywords = set()
        for other in other_contexts:
            all_other_keywords.update(self._extract_keywords(other))
        
        unique_keywords = keywords - all_other_keywords
        uniqueness = len(unique_keywords) / len(keywords) if keywords else 0
        
        # Also score by context length (more information)
        length_score = min(1.0, len(context.split()) / 50.0)
        
        entropy = 0.6 * uniqueness + 0.4 * length_score
        return min(1.0, entropy)
    
    def score_contexts(self, 
                      contexts: List[str],
                      task_text: str) -> List[Tuple[int, float, SamplingMetrics]]:
        """
        Score all contexts and return indexed scores
        
        Returns: List of (index, combined_score, metrics) tuples
        """
        scores = []
        
        rel_w, freq_w, rec_w, ent_w = self._normalized_weights()

        for idx, context in enumerate(contexts):
            relevance = self._calculate_relevance(context, task_text)
            frequency = self._calculate_frequency_score(context, contexts)
            recency = self._calculate_recency_score(idx, len(contexts))
            
            # Entropy calculated against all other contexts
            other_contexts = contexts[:idx] + contexts[idx+1:]
            entropy = self._calculate_entropy(context, other_contexts)
            
            # Combined weighted score
            combined = (
                rel_w * relevance
                + freq_w * frequency
                + rec_w * recency
                + ent_w * entropy
            )
            
            metrics = SamplingMetrics(
                relevance_score=relevance,
                importance_score=frequency,
                frequency_score=frequency,
                recency_score=recency,
                combined_score=combined
            )
            
            scores.append((idx, combined, metrics))
        
        return scores

    def _keyword_overlap(self, left: str, right: str) -> float:
        left_keywords = self._extract_keywords(left)
        right_keywords = self._extract_keywords(right)
        if not left_keywords or not right_keywords:
            return 0.0

        union = left_keywords | right_keywords
        if not union:
            return 0.0
        return len(left_keywords & right_keywords) / len(union)

    def _select_with_novelty(
        self,
        contexts: List[str],
        scored: List[Tuple[int, float, SamplingMetrics]],
        budget: int,
        forced_indices: Set[int],
    ) -> Tuple[List[int], float]:
        chosen: List[int] = sorted(i for i in forced_indices if 0 <= i < len(contexts))
        novelty_gains: List[float] = []

        if len(chosen) >= budget:
            return chosen[:budget], 0.0

        selected_set = set(chosen)
        candidates = list(scored)

        while len(chosen) < budget and candidates:
            best_idx = None
            best_score = float("-inf")
            best_novelty = 0.0

            for idx, base_score, _ in candidates:
                if idx in selected_set:
                    continue

                max_similarity = 0.0
                if chosen:
                    max_similarity = max(
                        self._keyword_overlap(contexts[idx], contexts[selected_idx])
                        for selected_idx in chosen
                    )

                novelty = 1.0 - max_similarity
                reranked = (1.0 - self.novelty_weight) * base_score + self.novelty_weight * novelty

                if reranked > best_score:
                    best_score = reranked
                    best_idx = idx
                    best_novelty = novelty

            if best_idx is None:
                break

            chosen.append(best_idx)
            selected_set.add(best_idx)
            novelty_gains.append(best_novelty)

        return chosen, (sum(novelty_gains) / len(novelty_gains) if novelty_gains else 0.0)

    def _anchor_indices(
        self,
        contexts: List[str],
        scored: List[Tuple[int, float, SamplingMetrics]],
    ) -> Set[int]:
        if not contexts:
            return set()

        anchors: Set[int] = {len(contexts) - 1}
        if scored:
            anchors.add(max(scored, key=lambda item: item[1])[0])

        return anchors
    
    def sample(self, 
              contexts: List[str],
              task_text: str,
              adaptive_budget: int = None) -> Tuple[List[str], Dict[str, any]]:
        """
        Adaptively sample the most important contexts
        
        Args:
            contexts: List of context strings
            task_text: The current task
            adaptive_budget: Override default budget based on complexity
        
        Returns:
            (sampled_contexts, debug_info)
        """
        if not contexts:
            return [], {"sampled_count": 0, "total_count": 0}
        
        budget = adaptive_budget or self.budget
        budget = max(1, min(budget, len(contexts)))
        
        # Score all contexts
        scores = self.score_contexts(contexts, task_text)
        
        scores.sort(key=lambda x: x[1], reverse=True)

        anchors = self._anchor_indices(contexts, scores)
        sampled_indices, avg_novelty_gain = self._select_with_novelty(
            contexts=contexts,
            scored=scores,
            budget=budget,
            forced_indices=anchors,
        )
        sampled_indices = sorted(sampled_indices)
        sampled_contexts = [contexts[i] for i in sampled_indices]

        selected_metrics = [entry[2] for entry in scores if entry[0] in set(sampled_indices)]
        top_scored = scores[:budget]
        diversity_score = 0.0
        if len(sampled_contexts) > 1:
            pair_similarities = []
            for left_idx in range(len(sampled_contexts)):
                for right_idx in range(left_idx + 1, len(sampled_contexts)):
                    pair_similarities.append(
                        self._keyword_overlap(sampled_contexts[left_idx], sampled_contexts[right_idx])
                    )
            if pair_similarities:
                diversity_score = 1.0 - (sum(pair_similarities) / len(pair_similarities))
        
        # Debug info
        debug_info = {
            "sampled_count": len(sampled_contexts),
            "total_count": len(contexts),
            "budget": budget,
            "average_relevance": (
                sum(s.relevance_score for s in selected_metrics) / len(selected_metrics)
                if selected_metrics else 0.0
            ),
            "average_importance": (
                sum(s.importance_score for s in selected_metrics) / len(selected_metrics)
                if selected_metrics else 0.0
            ),
            "average_recency": (
                sum(s.recency_score for s in selected_metrics) / len(selected_metrics)
                if selected_metrics else 0.0
            ),
            "average_entropy": (
                sum(s.combined_score for s in selected_metrics) / len(selected_metrics)
                if selected_metrics else 0.0
            ),
            "diversity_score": diversity_score,
            "average_novelty_gain": avg_novelty_gain,
            "anchors_preserved": len(anchors.intersection(set(sampled_indices))),
            "top_3_scores": [top_scored[i][1] for i in range(min(3, len(top_scored)))],
        }
        
        return sampled_contexts, debug_info
    
    def sample_with_fallback(self,
                            contexts: List[str],
                            task_text: str,
                            token_budget: int = 100,
                            avg_tokens_per_context: int = 20) -> List[str]:
        """
        Sample contexts while respecting a token budget
        Adapts sampling based on available tokens
        """
        if not contexts:
            return []
        
        # Calculate adaptive budget based on token constraints
        tokens_available = token_budget
        max_contexts = tokens_available // avg_tokens_per_context
        adaptive_budget = max(1, min(max_contexts, len(contexts)))
        
        sampled, _ = self.sample(contexts, task_text, adaptive_budget=adaptive_budget)
        return sampled
