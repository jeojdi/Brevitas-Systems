"""Shared lossless optimization engine — used by the SDK wrapper, the drop-in client, and
the proxy so the router + caching + retrieval logic lives in ONE place.

optimize_request(): given a chat request body, asks the router whether to cache_only, retrieve,
or passthrough for this call, applies the chosen LOSSLESS strategy in-place, and returns the
decision. record_usage(): computes honest savings from the provider response and feeds the
real cache-hit rate back to the router so it adapts per provider/session.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .api_adapter import retrieval_select
from .provider_cache import apply_anthropic_cache, savings_from_usage
from .router import BrevitasRouter


def _msg_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _stable_context(messages: List[dict], system: Any = None) -> List[str]:
    """The repeatable prefix: system + all but the last (volatile) message."""
    ctx: List[str] = []
    if system:
        ctx.append(_msg_text(system) if not isinstance(system, str) else system)
    for m in messages[:-1]:
        t = _msg_text(m.get("content", ""))
        if t:
            ctx.append(t)
    return ctx


def optimize_request(body: dict, provider: str, router: BrevitasRouter,
                     session_id: str) -> dict:
    """Apply the router-chosen lossless strategy to `body` in place. Returns decision meta."""
    messages = body.get("messages", []) or []
    if not messages:
        return {"strategy": "passthrough", "reason": "no messages"}

    system = body.get("system")
    stable = _stable_context(messages, system)
    query = _msg_text(messages[-1].get("content", "")) if messages else ""

    decision = router.decide(session_id, stable, query)

    strategy = decision.strategy
    if strategy == "retrieve":
        # reduce the prior context to the relevant chunks (fail-safe to full inside)
        sel = retrieval_select(query[:200], stable, k=8, use_adaptive=True)
        if not sel["fallback_applied"] and sel["selected_context"]:
            keep = set(sel["selected_context"])
            # Build new message list: keep all assistant/tool turns; prune user/context text
            new_msgs = []
            for m in messages[:-1]:
                role = m.get("role", "")
                # Always keep assistant and tool roles (maintain conversation structure)
                if role == "assistant":
                    new_msgs.append(m)
                elif role == "tool":
                    new_msgs.append(m)
                elif role == "user":
                    # For user messages, only keep if content is in retrieved set
                    content_text = _msg_text(m.get("content", ""))
                    if content_text in keep:
                        new_msgs.append(m)
                else:
                    # Unknown role: preserve for safety
                    new_msgs.append(m)

            new_msgs.append(messages[-1])
            body["messages"] = new_msgs
            meta = {"strategy": "retrieve", "reason": decision.reason,
                    "kept": len(new_msgs), "of": len(messages),
                    "baseline_tokens": sel["baseline_tokens"],
                    "optimized_tokens": sel["optimized_tokens"]}
        else:
            strategy = "cache_only"  # retrieval bailed -> safe fall-through to caching
            meta = None
    else:
        meta = None
    if meta is None:
        meta = {"strategy": strategy, "reason": decision.reason}

    # Anthropic requires EXPLICIT cache_control markers (OpenAI/DeepSeek cache byte-identical
    # prefixes automatically). Apply on EVERY path — including after a retrieval rebuild and
    # on passthrough (the router's verdict is about the STABLE prefix; a huge context block
    # inside the last message is still cacheable). apply_anthropic_cache's own >=min_tokens
    # guard makes this a no-op when nothing is worth caching.
    if provider == "anthropic":
        plan = apply_anthropic_cache(body)
        meta["cache_breakpoints"] = plan.breakpoints
        meta["cached_prefix_tokens"] = plan.cached_prefix_tokens
    # OpenAI/DeepSeek: caching is automatic if prefix is byte-identical — we DON'T mutate it.
    return meta


def record_usage(usage: dict, provider: str, router: BrevitasRouter, session_id: str):
    """Honest savings from real usage + feed cache-hit feedback to the router."""
    s = savings_from_usage(usage, provider)
    if provider == "anthropic":
        prompt = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) \
                 + usage.get("cache_read_input_tokens", 0)
    else:
        prompt = usage.get("prompt_tokens", 0)
    router.observe_usage(session_id, prompt, s.cached_tokens)
    return s
