import re
from typing import List


def critical_context_recall(must_keep: List[str], surviving_text: str) -> float:
    """
    Calculate the fraction of required substrings that appear in the surviving text.

    Args:
        must_keep: List of substrings that should survive the pipeline.
        surviving_text: The text that survived sampling and pruning.

    Returns:
        Fraction of must_keep items found in surviving_text, in [0.0, 1.0].
        Empty must_keep returns 1.0 (vacuous truth).
    """
    # Normalize surviving text: lowercase and collapse whitespace
    normalized_text = re.sub(r'\s+', ' ', surviving_text.lower()).strip()

    # Filter must_keep: remove empty/whitespace-only entries
    filtered_must_keep = [
        re.sub(r'\s+', ' ', item.lower()).strip()
        for item in must_keep
    ]
    filtered_must_keep = [item for item in filtered_must_keep if item]

    # Edge case: empty must_keep returns 1.0
    if not filtered_must_keep:
        return 1.0

    # Count how many items are found in surviving text
    found_count = sum(1 for item in filtered_must_keep if item in normalized_text)

    return found_count / len(filtered_must_keep)
