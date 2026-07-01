"""Lever 1 — provider-native prompt caching, measured honestly.

This finishes the native-caching lever from REVAMP_PLAN.md:

1. apply_anthropic_cache(): place cache_control breakpoints ONLY where the cached prefix
   is >= the provider minimum (1024 tokens), on stable blocks (tools/system/prior turns),
   never on the volatile last user message, using up to 4 breakpoints. (The existing
   optimizers/provider_cache/anthropic.py never actually counted tokens.)

2. savings_from_usage(): read the REAL cache fields from the provider response and compute
   honest savings = (what uncached would cost) vs (actual billed cost with cache discount):
     * Anthropic: cache_read_input_tokens (~0.1x), cache_creation_input_tokens (~1.25x)
     * OpenAI/DeepSeek: usage.prompt_tokens_details.cached_tokens (~0.5x)

Provider facts (docs): Anthropic min cacheable 1024 tok, cache read ~10% of input price;
OpenAI automatic >=1024 tok, cached input ~50% off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# token counting
# --------------------------------------------------------------------------- #
try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text or "", disallowed_special=()))
except Exception:  # pragma: no cover
    def count_tokens(text: str) -> int:
        return max(1, int(len((text or "").split()) * 1.3))


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        text = block.get("text", "")
        # Only return text if it's a string (not empty string, but still str type)
        if isinstance(text, str):
            return text
        # If text is not a str, try content as fallback, but only if it's str
        content = block.get("content", "")
        if isinstance(content, str):
            return content
        # If content is a list or other non-str, return empty string (not the list)
        return ""
    return ""


def _content_tokens(content: Any) -> int:
    if isinstance(content, str):
        return count_tokens(content)
    if isinstance(content, list):
        return sum(count_tokens(_block_text(b)) for b in content)
    return 0


# --------------------------------------------------------------------------- #
# 1. Anthropic cache_control placement (with real 1024-token guard)
# --------------------------------------------------------------------------- #
def _mark(obj_holder: dict, key: str) -> bool:
    """Attach cache_control to the last text block of body[key] (system) or a message
    content. Converts a string to a one-element block list. Returns True if marked."""
    val = obj_holder.get(key)
    if isinstance(val, str):
        obj_holder[key] = [{"type": "text", "text": val, "cache_control": {"type": "ephemeral"}}]
        return True
    if isinstance(val, list) and val:
        if isinstance(val[-1], dict) and "cache_control" not in val[-1]:
            val[-1]["cache_control"] = {"type": "ephemeral"}
            return True
    return False


def _mark_content(msg: dict) -> bool:
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = [{"type": "text", "text": content,
                           "cache_control": {"type": "ephemeral"}}]
        return True
    if isinstance(content, list) and content:
        if isinstance(content[-1], dict) and "cache_control" not in content[-1]:
            content[-1]["cache_control"] = {"type": "ephemeral"}
            return True
    return False


@dataclass
class CachePlan:
    breakpoints: int
    cached_prefix_tokens: int
    positions: List[str]


def apply_anthropic_cache(body: dict, min_tokens: int = 1024,
                          max_breakpoints: int = 4) -> CachePlan:
    """Insert cache_control breakpoints on the stable prefix in-place; return a CachePlan.

    A breakpoint is only placed where the cumulative prefix (tools + system + prior turns,
    up to that block) is >= min_tokens, so the provider will actually cache it. The volatile
    last user message is never marked. Up to `max_breakpoints` are placed, preferring the
    blocks closest to the tail (maximum cached coverage)."""
    if not isinstance(body, dict):
        return CachePlan(0, 0, [])
    messages = body.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return CachePlan(0, 0, [])

    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and messages[i].get("role") == "user":
            last_user_idx = i
            break

    # ordered stable segments with a (marker-fn, token-count, label)
    segments: List[tuple] = []
    if body.get("tools"):
        tt = sum(count_tokens(str(t)) for t in body["tools"])
        segments.append((lambda: _mark_tools(body), tt, "tools"))
    if body.get("system"):
        segments.append((lambda: _mark(body, "system"), _content_tokens(body["system"]), "system"))
    stable_end = last_user_idx if last_user_idx >= 0 else len(messages)
    for i in range(stable_end):
        msg = messages[i]
        segments.append((lambda m=msg: _mark_content(m), _content_tokens(msg.get("content")),
                         f"message[{i}]"))

    # cumulative tokens; candidate breakpoints are segments whose prefix >= min_tokens
    cum = 0
    candidates = []  # (index_in_segments, cum_after, label)
    for idx, (_, tok, label) in enumerate(segments):
        cum += tok
        if cum >= min_tokens:
            candidates.append((idx, cum, label))

    if not candidates:
        return CachePlan(0, cum, [])

    chosen = candidates[-max_breakpoints:]  # closest to the tail = most coverage
    placed = []
    for idx, cum_after, label in chosen:
        if segments[idx][0]():
            placed.append(label)
    return CachePlan(len(placed), chosen[-1][1] if chosen else 0, placed)


def _mark_tools(body: dict) -> bool:
    tools = body.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict) \
            and "cache_control" not in tools[-1]:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
        return True
    return False


# --------------------------------------------------------------------------- #
# 2. Honest savings from real provider usage
# --------------------------------------------------------------------------- #
# Per-provider price ratios, RELATIVE to the fresh-input price (= 1.0), from provider pricing.
# This keeps cost numbers in token-magnitude (so usage reporting stays sane) while accounting for
# the real cache discount AND output (output is NEVER cached and is often pricier than input).
#   cache_read  = cached-input price / fresh-input price
#   cache_write = (anthropic only) cache-creation surcharge
#   output      = output price / fresh-input price
# DeepSeek deepseek-chat: in $0.27, cache-hit $0.07, out $1.10/1M -> cache_read .259, output 4.07
# OpenAI gpt-4o-mini:     in $0.15, cached   $0.075, out $0.60/1M -> cache_read .50,  output 4.0
# Anthropic Sonnet:       in $3.00, cache-rd $0.30,  out $15.0/1M -> cache_read .10,  output 5.0
_RATES = {
    "deepseek":  {"cache_read": 0.259, "cache_write": 1.0, "output": 4.07},
    "openai":    {"cache_read": 0.50,  "cache_write": 1.0, "output": 4.0},
    "anthropic": {"cache_read": 0.10,  "cache_write": 1.25, "output": 5.0},
    "groq":      {"cache_read": 1.00,  "cache_write": 1.0, "output": 4.0},
}
_DEFAULT_RATES = {"cache_read": 0.50, "cache_write": 1.0, "output": 4.0}


@dataclass
class Savings:
    uncached_cost: float          # cost-units if NOTHING were cached (incl. output)
    actual_cost: float            # real cost-units (incl. output)
    savings_pct: float            # TOTAL savings incl. output (this is your real bill cut)
    cached_tokens: int
    input_fresh: int = 0
    input_cached: int = 0
    output_tokens: int = 0
    input_savings_pct: float = 0.0  # input-only savings (ignores output) — for reference
    detail: Dict[str, Any] = None


def savings_from_usage(usage: dict, provider: str) -> Savings:
    """Honest savings from a provider `usage` object, including OUTPUT tokens.

    Output is never cached and is billed at full price, so the headline savings_pct reflects the
    real total-bill cut (input + output), not just the input side."""
    provider = provider.lower()
    r = _RATES.get(provider, _DEFAULT_RATES)
    if provider == "anthropic":
        fresh = int(usage.get("input_tokens", 0))
        write = int(usage.get("cache_creation_input_tokens", 0))
        read = int(usage.get("cache_read_input_tokens", 0))
        output = int(usage.get("output_tokens", 0))
        in_uncached = (fresh + write + read) * 1.0
        in_actual = fresh * 1.0 + write * r["cache_write"] + read * r["cache_read"]
        cached = read
        detail = {"input": fresh, "cache_write": write, "cache_read": read, "output": output}
    else:  # openai / deepseek style
        prompt = int(usage.get("prompt_tokens", 0))
        details = usage.get("prompt_tokens_details", {}) or {}
        cached = int(details.get("cached_tokens", 0) or usage.get("prompt_cache_hit_tokens", 0))
        fresh = prompt - cached
        write = 0
        output = int(usage.get("completion_tokens", 0))
        in_uncached = prompt * 1.0
        in_actual = fresh * 1.0 + cached * r["cache_read"]
        detail = {"prompt": prompt, "cached": cached, "output": output,
                  "cache_read_rate": r["cache_read"]}

    out_cost = output * r["output"]
    uncached = in_uncached + out_cost          # total cost if nothing cached
    actual = in_actual + out_cost              # real total cost
    savings_pct = round(100 * (1 - actual / uncached), 2) if uncached > 0 else 0.0
    input_savings = round(100 * (1 - in_actual / in_uncached), 2) if in_uncached > 0 else 0.0
    return Savings(round(uncached, 2), round(actual, 2), savings_pct, cached,
                   input_fresh=fresh + write, input_cached=cached, output_tokens=output,
                   input_savings_pct=input_savings, detail=detail)
