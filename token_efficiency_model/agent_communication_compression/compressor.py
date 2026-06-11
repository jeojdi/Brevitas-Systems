from dataclasses import dataclass
from typing import List, Tuple

from ..common.metrics import estimate_tokens_many
from ..common.utils import normalize_whitespace, split_sentences
from ..common.utils import lexical_overlap
from collections import Counter


@dataclass
class CompressionStats:
    original_tokens: int
    compressed_tokens: int
    removed_redundant_sentences: int


class CommunicationCompressor:
    def __init__(self, level: int = 1):
        self.level = max(1, min(level, 3))

    def compress_messages(self, messages: List[str]) -> Tuple[List[str], CompressionStats]:
        cleaned = [normalize_whitespace(msg) for msg in messages if msg and msg.strip()]
        original_tokens = estimate_tokens_many(cleaned)
        # Group similar sentences across all messages to remove redundancy while preserving unique facts
        all_sentences = []
        for msg in cleaned:
            for s in split_sentences(msg):
                all_sentences.append(s)

        # simple fingerprinting by lower-case normalization to remove exact duplicates
        normalized_map = {}
        for s in all_sentences:
            key = s.lower().strip()
            normalized_map.setdefault(key, 0)
            normalized_map[key] += 1

        # cluster sentences by lexical overlap and collapse clusters
        clusters = []  # list of lists of sentences
        used = set()
        # Thresholds tuned so near-duplicate sentences (same meaning, different phrasing)
        # get collapsed. Lower = more aggressive clustering.
        threshold = {1: 0.50, 2: 0.28, 3: 0.20}.get(self.level, 0.28)

        for i, s in enumerate(all_sentences):
            if i in used:
                continue
            cluster = [s]
            used.add(i)
            for j in range(i + 1, len(all_sentences)):
                if j in used:
                    continue
                sim = lexical_overlap(s, all_sentences[j])
                if sim >= threshold:
                    cluster.append(all_sentences[j])
                    used.add(j)
            clusters.append(cluster)

        output = []
        removed = 0
        # Anchor keywords to preserve (do not summarize away)
        anchors = {"important", "critical", "decision", "action", "error", "fail", "resolved", "todo"}

        for cluster in clusters:
            if not cluster:
                continue
            if len(cluster) == 1:
                output.append(cluster[0])
                continue

            # pick sentences containing anchor words first
            anchor_sent = None
            for s in cluster:
                low = s.lower()
                if any(a in low for a in anchors):
                    anchor_sent = s
                    break

            if anchor_sent:
                output.append(anchor_sent)
                removed += len(cluster) - 1
                continue

            # Pick the most informative sentence: longest one (highest information density)
            rep = max(cluster, key=lambda x: len(x))
            output.append(rep)
            removed += len(cluster) - 1

        compressed_tokens = estimate_tokens_many(output)
        stats = CompressionStats(
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            removed_redundant_sentences=removed,
        )
        return output, stats
