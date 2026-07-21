"""Lever 1 — provider-native prompt caching, measured honestly.

This finishes the native-caching lever from REVAMP_PLAN.md:

1. apply_anthropic_cache(): place cache_control breakpoints ONLY where the cached prefix
   is >= the provider minimum (1024 tokens), on stable blocks (tools/system/prior turns),
   never on the volatile last user message, using up to 4 breakpoints. (The existing
   optimizers/provider_cache/anthropic.py never actually counted tokens.)

2. savings_from_usage(): read the REAL cache fields from the provider response and compute
   honest savings = (what uncached would cost) vs (actual billed cost with cache discount):
     * Anthropic: cache_read_input_tokens (~0.1x), cache_creation_input_tokens (~1.25x)
     * OpenAI/DeepSeek: cached-token receipt fields (model-specific rates)

Provider facts (docs): Anthropic min cacheable 1024 tok, cache read ~10% of input price;
OpenAI automatic >=1024 tok with model-specific discounts; DeepSeek V4 Flash cache
hits cost 2% of fresh input at the 2026-07-16 list price.
"""

from __future__ import annotations

import hashlib
import json
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
        return max(0, int(len((text or "").split()) * 1.3))


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
def _cc(ttl: str = "") -> dict:
    """Build a cache_control marker; ttl='1h' selects Anthropic's long-TTL tier
    (2x write instead of 1.25x, refreshed free on every use — docs-verified)."""
    return {"type": "ephemeral", "ttl": "1h"} if ttl == "1h" else {"type": "ephemeral"}


def _mark(obj_holder: dict, key: str, ttl: str = "") -> bool:
    """Attach cache_control to the last text block of body[key] (system) or a message
    content. Converts a string to a one-element block list. Returns True if marked."""
    val = obj_holder.get(key)
    if isinstance(val, str):
        obj_holder[key] = [{"type": "text", "text": val, "cache_control": _cc(ttl)}]
        return True
    if isinstance(val, list) and val:
        if isinstance(val[-1], dict) and "cache_control" not in val[-1]:
            val[-1]["cache_control"] = _cc(ttl)
            return True
    return False


def _mark_content(msg: dict, ttl: str = "") -> bool:
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = [{"type": "text", "text": content,
                           "cache_control": _cc(ttl)}]
        return True
    if isinstance(content, list) and content:
        if isinstance(content[-1], dict) and "cache_control" not in content[-1]:
            content[-1]["cache_control"] = _cc(ttl)
            return True
    return False


@dataclass
class CachePlan:
    breakpoints: int
    cached_prefix_tokens: int
    positions: List[str]
    ttl: str = ""                 # "" = default 5m tier; "1h" = long-TTL tier


# Anthropic per-model minimum cacheable prompt length (tokens), from the prompt-caching
# docs (platform.claude.com/docs/.../prompt-caching, verified 2026-07-01). Prompts below
# the minimum are silently not cached, so markers there are inert — but the ROUTER's
# expectations must use the real threshold. Longest-prefix match; default 1024.
_ANTHROPIC_MIN = [
    ("claude-mythos-preview", 2048),
    ("claude-fable", 512),
    ("claude-mythos", 512),
    ("claude-haiku-4-5", 4096),
    ("claude-opus-4-6", 4096),
    ("claude-opus-4-5", 4096),
    ("claude-opus-4-7", 2048),
    ("claude-haiku-3-5", 2048),
    ("claude-3-5-haiku", 2048),
    ("claude-haiku", 2048),
]


def anthropic_min_tokens(model: str, default: int = 1024) -> int:
    m = (model or "").lower()
    for prefix, n in _ANTHROPIC_MIN:
        if m.startswith(prefix):
            return n
    return default


def apply_anthropic_cache(body: dict, min_tokens: int = 1024,
                          max_breakpoints: int = 4, ttl: str = "") -> CachePlan:
    """Insert cache_control breakpoints on the stable prefix in-place; return a CachePlan.

    A breakpoint is only placed where the cumulative prefix (tools + system + prior turns,
    up to that block) is >= min_tokens, so the provider will actually cache it. The volatile
    TAIL (the final content block of the last user message) is never marked — but earlier
    blocks INSIDE the last user message are stable context (the classic "big document +
    question" first turn) and are markable. Up to `max_breakpoints` are placed, preferring
    the blocks closest to the tail (maximum cached coverage).

    Haiku-family models have a 2048-token cache minimum (Anthropic docs); others 1024.

    Idempotent under reuse: callers (real customer code included) reuse message dicts
    across turns, so markers from previous calls persist in the history. Anthropic
    rejects requests with >4 cache_control blocks, so ALL existing markers are stripped
    first and at most `max_breakpoints` fresh ones are placed at the latest stable
    positions (server-side cache persistence is keyed by content, not by old markers)."""
    if not isinstance(body, dict):
        return CachePlan(0, 0, [])
    _strip_cache_control(body)
    # per-model minimum from the provider docs (Haiku 4.5 / Opus 4.5-4.6 need 4096;
    # Fable/Mythos 512; default 1024) — markers below it are silently inert
    min_tokens = max(min_tokens, anthropic_min_tokens(str(body.get("model", ""))))
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
        segments.append((lambda: _mark_tools(body, ttl), tt, "tools"))
    if body.get("system"):
        sysv = body["system"]
        if isinstance(sysv, list):
            # per-BLOCK segments so a stable block ahead of a volatile block (the CR2
            # template split) can carry its own breakpoint — a single whole-system
            # marker would cache THROUGH the volatile tail and miss every run
            for j, blk in enumerate(sysv):
                if isinstance(blk, dict) and blk.get("type") == "text":
                    segments.append((lambda b=blk: _mark_block(b, ttl),
                                     count_tokens(blk.get("text", "")),
                                     f"system_block[{j}]"))
        else:
            segments.append((lambda: _mark(body, "system", ttl), _content_tokens(sysv), "system"))
    stable_end = last_user_idx if last_user_idx >= 0 else len(messages)
    for i in range(stable_end):
        msg = messages[i]
        segments.append((lambda m=msg: _mark_content(m, ttl), _content_tokens(msg.get("content")),
                         f"message[{i}]"))
    # Non-final blocks INSIDE the last user message are stable context too (the "big
    # document + question in one turn" pattern). Only the FINAL block is the volatile tail.
    if last_user_idx >= 0:
        last_content = messages[last_user_idx].get("content")
        if isinstance(last_content, list) and len(last_content) >= 2:
            for j, block in enumerate(last_content[:-1]):
                if isinstance(block, dict) and block.get("type") == "text":
                    segments.append((lambda b=block: _mark_block(b, ttl),
                                     count_tokens(block.get("text", "")),
                                     f"last_msg_block[{j}]"))

    # cumulative tokens; candidate breakpoints are segments whose prefix >= min_tokens
    cum = 0
    candidates = []  # (index_in_segments, cum_after, label)
    for idx, (_, tok, label) in enumerate(segments):
        cum += tok
        if cum >= min_tokens:
            candidates.append((idx, cum, label))

    if not candidates:
        return CachePlan(0, cum, [], ttl)

    chosen = candidates[-max_breakpoints:]  # closest to the tail = most coverage
    placed = []
    for idx, cum_after, label in chosen:
        if segments[idx][0]():
            placed.append(label)
    return CachePlan(len(placed), chosen[-1][1] if chosen else 0, placed, ttl)


def _strip_cache_control(body: dict) -> None:
    """Remove every cache_control marker from tools/system/messages (see docstring)."""
    def _strip(blocks) -> None:
        if isinstance(blocks, list):
            for b in blocks:
                if isinstance(b, dict) and "cache_control" in b:
                    del b["cache_control"]
    _strip(body.get("tools"))
    _strip(body.get("system"))
    for m in body.get("messages", []) or []:
        if isinstance(m, dict):
            _strip(m.get("content"))


def count_cache_control(body: dict) -> int:
    """Count caller-supplied cache markers without reading or changing content."""
    if not isinstance(body, dict):
        return 0
    # Current Anthropic clients can request automatic prompt caching with a
    # request-wide top-level cache_control object. It owns the whole policy, so
    # Brevitas must not add up to four more explicit breakpoints.
    total = 1 if isinstance(body.get("cache_control"), dict) else 0
    for blocks in (body.get("tools"), body.get("system")):
        if isinstance(blocks, list):
            total += sum(1 for block in blocks
                         if isinstance(block, dict) and "cache_control" in block)
    for message in body.get("messages", []) or []:
        blocks = message.get("content") if isinstance(message, dict) else None
        if isinstance(blocks, list):
            total += sum(1 for block in blocks
                         if isinstance(block, dict) and "cache_control" in block)
    return total


def _mark_block(block: dict, ttl: str = "") -> bool:
    """Attach cache_control to a specific content block (stable blocks inside the last
    user message). Never called on the final (volatile) block."""
    if isinstance(block, dict) and "cache_control" not in block:
        block["cache_control"] = _cc(ttl)
        return True
    return False


def _mark_tools(body: dict, ttl: str = "") -> bool:
    tools = body.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict) \
            and "cache_control" not in tools[-1]:
        tools[-1]["cache_control"] = _cc(ttl)
        return True
    return False


# --------------------------------------------------------------------------- #
# 1b. OpenAI GPT-5.6 prompt-cache routing and optional explicit breakpoints
# --------------------------------------------------------------------------- #

@dataclass
class OpenAICachePlan:
    supported: bool = False
    key_added: bool = False
    breakpoint_added: bool = False
    stable_prefix_tokens: int = 0
    owner: str = "none"          # none | caller | brevitas


def _openai_cache_capable(model: str) -> bool:
    # Fail closed: older models reject the new options/breakpoint fields. Extend this
    # allowlist only when a later family is verified against the official API docs.
    return (model or "").lower().startswith("gpt-5.6")


def _has_openai_breakpoint(value: Any) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("prompt_cache_breakpoint"), dict):
            return True
        return any(_has_openai_breakpoint(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_openai_breakpoint(item) for item in value)
    return False


def apply_openai_cache(body: dict, tenant_key: str = "", *,
                       explicit_breakpoint: bool = False,
                       min_tokens: int = 1024) -> OpenAICachePlan:
    """Add current GPT-5.6 cache routing fields without touching prompt text.

    A deterministic, tenant-scoped prompt_cache_key improves cache routing. Explicit
    breakpoints are supported but opt-in because writes are billed at 1.25x; when on,
    the last stable text block is marked and request-wide mode/TTL are set exactly as
    documented. Caller-owned keys/options/breakpoints are always preserved.
    """
    if not isinstance(body, dict) or not _openai_cache_capable(str(body.get("model", ""))):
        return OpenAICachePlan()
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return OpenAICachePlan(supported=True)

    stable_messages = [m for m in messages[:-1] if isinstance(m, dict)]
    last = messages[-1] if isinstance(messages[-1], dict) else {}
    last_content = last.get("content")
    stable_final_blocks = last_content[:-1] if isinstance(last_content, list) else []
    stable_view: dict[str, Any] = {"messages": stable_messages}
    if stable_final_blocks:
        stable_view["final"] = {"role": last.get("role"), "content": stable_final_blocks}
    if body.get("tools"):
        stable_view["tools"] = body["tools"]

    stable_text = json.dumps(stable_view, sort_keys=True, separators=(",", ":"), default=str)
    stable_tokens = count_tokens(stable_text)
    caller_owned = ("prompt_cache_key" in body or "prompt_cache_options" in body
                    or _has_openai_breakpoint(messages))
    plan = OpenAICachePlan(supported=True, stable_prefix_tokens=stable_tokens,
                           owner="caller" if caller_owned else "none")
    if stable_tokens < min_tokens:
        return plan

    if "prompt_cache_key" not in body:
        prefix_hash = hashlib.sha256(stable_text.encode("utf-8")).hexdigest()[:20]
        tenant = (tenant_key or "local")[:20]
        body["prompt_cache_key"] = f"brevitas:{tenant}:{prefix_hash}"
        plan.key_added = True
        if not caller_owned:
            plan.owner = "brevitas"

    if not explicit_breakpoint or "prompt_cache_options" in body \
            or _has_openai_breakpoint(messages):
        return plan

    target_holder: dict | None = None
    target_key = ""
    if stable_final_blocks and isinstance(stable_final_blocks[-1], dict):
        target_holder, target_key = stable_final_blocks[-1], "block"
    elif stable_messages:
        target_holder, target_key = stable_messages[-1], "content"

    if target_holder is None:
        return plan
    if target_key == "block":
        target_holder["prompt_cache_breakpoint"] = {"mode": "explicit"}
    else:
        content = target_holder.get("content")
        if isinstance(content, str):
            target_holder["content"] = [{
                "type": ("input_text" if body.get("_brevitas_operation") == "responses"
                         else "text"),
                "text": content,
                "prompt_cache_breakpoint": {"mode": "explicit"},
            }]
        elif isinstance(content, list) and content and isinstance(content[-1], dict):
            content[-1]["prompt_cache_breakpoint"] = {"mode": "explicit"}
        else:
            return plan
    body["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
    plan.breakpoint_added = True
    plan.owner = "brevitas"
    return plan


# --------------------------------------------------------------------------- #
# 2. Honest savings from real provider usage
# --------------------------------------------------------------------------- #
# Per-provider price ratios, RELATIVE to the fresh-input price (= 1.0), from provider pricing.
# This keeps cost numbers in token-magnitude (so usage reporting stays sane) while accounting for
# the real cache discount AND output (output is NEVER cached and is often pricier than input).
#   cache_read  = cached-input price / fresh-input price
#   cache_write = (anthropic only) cache-creation surcharge
#   output      = output price / fresh-input price
# DeepSeek V4 Flash:      in $0.14, cache-hit $0.0028, out $0.28/1M -> .02, 2.0
# OpenAI gpt-4o-mini:     in $0.15, cached   $0.075, out $0.60/1M -> cache_read .50,  output 4.0
# Anthropic Sonnet:       in $3.00, cache-rd $0.30,  out $15.0/1M -> cache_read .10,  output 5.0
_RATES = {
    "deepseek":  {"cache_read": 0.02,  "cache_write": 1.0, "output": 2.0},
    "openai":    {"cache_read": 0.50,  "cache_write": 1.0, "output": 4.0},
    "anthropic": {"cache_read": 0.10,  "cache_write": 1.25, "output": 5.0},
    "groq":      {"cache_read": 1.00,  "cache_write": 1.0, "output": 4.0},
}
_DEFAULT_RATES = {"cache_read": 0.50, "cache_write": 1.0, "output": 4.0}

# Per-MODEL overrides (longest-prefix match on the lowercased model id). The provider
# rows above are the fallback, but ratios genuinely differ per model — e.g. the gpt-4.1
# family caches at 25% of input price where gpt-4o caches at 50% — and %-of-savings
# billing must use the ratios of the model that was actually called.
#   DeepSeek V4 Flash: in $0.14,  hit $0.0028, out $0.28 /1M
#   DeepSeek V4 Pro:   in $0.435, hit $0.003625, out $0.87 /1M
#   gpt-4o(-mini):     cached = 50% of input; out = 4x input
#   gpt-4.1(-mini/nano): cached = 25% of input; out = 4x input
#   claude (all):      cache read 10%, write 1.25x, out = 5x input
_MODEL_RATES = [
    ("deepseek-v4-pro",   {"cache_read": 1 / 120, "cache_write": 1.0, "output": 2.0}),
    ("deepseek-v4-flash", {"cache_read": 0.02, "cache_write": 1.0, "output": 2.0}),
    ("deepseek-reasoner", {"cache_read": 0.02, "cache_write": 1.0, "output": 2.0}),
    ("deepseek-chat",     {"cache_read": 0.02, "cache_write": 1.0, "output": 2.0}),
    ("o4-mini",           {"cache_read": 0.25, "cache_write": 1.0, "output": 4.0}),
    ("gpt-4.1",           {"cache_read": 0.25,  "cache_write": 1.0, "output": 4.0}),
    ("gpt-4o",            {"cache_read": 0.50,  "cache_write": 1.0, "output": 4.0}),
    ("claude",            {"cache_read": 0.10,  "cache_write": 1.25, "output": 5.0}),
]


def rates_for(provider: str, model: str = "") -> Dict[str, float]:
    """Rate ratios for a specific model, falling back to the provider row."""
    m = (model or "").lower()
    # OpenAI's current prompt-caching guide specifies a 1.25x write price for
    # GPT-5.6 and later families. Reads retain the provider/model read ratio.
    if m.startswith("gpt-5.6"):
        return {**_RATES["openai"], "cache_write": 1.25}
    if m:
        for prefix, r in _MODEL_RATES:
            if m.startswith(prefix):
                return r
    return _RATES.get((provider or "").lower(), _DEFAULT_RATES)


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


def savings_from_usage(usage: dict, provider: str, model: str = "") -> Savings:
    """Honest savings from a provider `usage` object, including OUTPUT tokens.

    Output is never cached and is billed at full price, so the headline savings_pct reflects the
    real total-bill cut (input + output), not just the input side. Pass `model` so the
    ratios match the model actually called (billing-grade accuracy)."""
    provider = provider.lower()
    r = rates_for(provider, model)
    if provider == "anthropic":
        fresh = int(usage.get("input_tokens", 0))
        write = int(usage.get("cache_creation_input_tokens", 0))
        read = int(usage.get("cache_read_input_tokens", 0))
        output = int(usage.get("output_tokens", 0))
        in_uncached = (fresh + write + read) * 1.0
        # tier-accurate write premium when the response breaks it down: 5m writes bill
        # 1.25x, 1h writes 2x (provider docs). Falls back to the flat premium.
        bd = usage.get("cache_creation") or {}
        w5 = int(bd.get("ephemeral_5m_input_tokens", 0) or 0)
        w1h = int(bd.get("ephemeral_1h_input_tokens", 0) or 0)
        if write > 0 and (w5 + w1h) == write:
            write_cost = w5 * 1.25 + w1h * 2.0
        else:
            write_cost = write * r["cache_write"]
        in_actual = fresh * 1.0 + write_cost + read * r["cache_read"]
        cached = read
        detail = {"input": fresh, "cache_write": write, "cache_write_5m": w5,
                  "cache_write_1h": w1h, "cache_read": read, "output": output}
    else:  # openai / deepseek style
        prompt = int(usage.get("prompt_tokens", 0))
        details = usage.get("prompt_tokens_details", {}) or {}
        cached = int(details.get("cached_tokens", 0) or usage.get("prompt_cache_hit_tokens", 0))
        write = int(details.get("cache_write_tokens", 0) or 0)
        if prompt == 0 and ("prompt_cache_hit_tokens" in usage
                            or "prompt_cache_miss_tokens" in usage):
            prompt = cached + int(usage.get("prompt_cache_miss_tokens", 0) or 0)
        fresh = max(0, prompt - cached - write)
        output = int(usage.get("completion_tokens", 0))
        in_uncached = prompt * 1.0
        in_actual = (fresh * 1.0 + write * r["cache_write"]
                     + cached * r["cache_read"])
        detail = {"prompt": prompt, "cached": cached, "cache_write": write,
                  "output": output, "cache_read_rate": r["cache_read"],
                  "cache_write_rate": r["cache_write"]}

    out_cost = output * r["output"]
    uncached = in_uncached + out_cost          # total cost if nothing cached
    actual = in_actual + out_cost              # real total cost
    savings_pct = round(100 * (1 - actual / uncached), 2) if uncached > 0 else 0.0
    input_savings = round(100 * (1 - in_actual / in_uncached), 2) if in_uncached > 0 else 0.0
    return Savings(round(uncached, 2), round(actual, 2), savings_pct, cached,
                   input_fresh=fresh + write, input_cached=cached, output_tokens=output,
                   input_savings_pct=input_savings, detail=detail)
