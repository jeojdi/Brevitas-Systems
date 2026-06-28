from typing import List, Tuple

from ..common.utils import lexical_overlap
from collections import Counter
from ..common.utils import normalize_whitespace


class SmartContextPruner:
    def __init__(self, budget: int = 5):
        self.budget = max(1, budget)

    def prune(self, task_text: str, context_chunks: List[str]) -> Tuple[List[str], List[float]]:
        if not context_chunks:
            return [], []
        # Compute term frequencies across chunks to identify uniqueness
        token_counts = Counter()
        chunk_terms = []
        for chunk in context_chunks:
            terms = [w.lower() for w in normalize_whitespace(chunk).split() if len(w) > 3]
            chunk_terms.append(terms)
            for t in set(terms):
                token_counts[t] += 1

        scored = []
        total = len(context_chunks)
        for idx, chunk in enumerate(context_chunks):
            relevance = lexical_overlap(task_text, chunk)
            recency = (idx + 1) / total
            # uniqueness: fraction of terms in this chunk that are rare across all chunks
            terms = chunk_terms[idx]
            if terms:
                unique_terms = sum(1 for t in terms if token_counts.get(t, 0) == 1)
                uniqueness = unique_terms / len(terms)
            else:
                uniqueness = 0.0

            length_score = min(1.0, len(chunk.split()) / 60.0)

            # Combined score favors relevance, uniqueness, then recency and length
            score = 0.55 * relevance + 0.20 * uniqueness + 0.15 * recency + 0.10 * length_score
            scored.append((score, idx, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        selected = scored[: self.budget]
        selected.sort(key=lambda item: item[1])

        kept_chunks = [item[2] for item in selected]
        kept_scores = [item[0] for item in selected]
        return kept_chunks, kept_scores
