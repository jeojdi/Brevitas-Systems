from typing import Iterable
import warnings

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")
    def _count(text: str) -> int:
        return len(_enc.encode(text, disallowed_special=()))
except Exception:
    def _count(text: str) -> int:
        return max(1, int(len(text.split()) * 1.3))


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, _count(text))


def estimate_tokens_many(chunks: Iterable[str]) -> int:
    return sum(estimate_tokens(chunk) for chunk in chunks)


def compression_ratio(original_tokens: int, optimized_tokens: int) -> float:
    if original_tokens <= 0:
        return 0.0
    return max(0.0, min(1.0, optimized_tokens / original_tokens))


def savings_pct(original_tokens: int, optimized_tokens: int) -> float:
    if original_tokens <= 0:
        return 0.0
    return (1.0 - compression_ratio(original_tokens, optimized_tokens)) * 100.0


def quality_proxy_score(compression_strength: float, prune_strength: float, route_fit: float) -> float:
    """DEPRECATED: Fake heuristic quality score. Use real quality gate instead.

    This function uses arbitrary weights and does not measure actual answer quality.
    Replaced by token_efficiency_model.quality.gate.assess() which uses embedding
    similarity + LLM-as-judge.

    Phase 3 (Quality Gate & Billing) marks this as fallback only. Do not use on live path.
    """
    warnings.warn(
        "quality_proxy_score is deprecated; use token_efficiency_model.quality.gate.assess() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    quality = 1.0
    quality -= 0.18 * compression_strength
    quality -= 0.22 * prune_strength
    quality += 0.25 * route_fit
    return max(0.0, min(1.0, quality))


def steady_state_savings_pct(steady_state_tokens: int, baseline_tokens: int) -> float:
    return savings_pct(baseline_tokens, steady_state_tokens)


def quality_floor_penalty(quality: float, floor: float = 0.98) -> float:
    if quality >= floor:
        return 0.0
    return (floor - quality) * 2.0
