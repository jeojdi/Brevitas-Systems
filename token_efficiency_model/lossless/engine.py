"""Shared lossless optimization engine — used by the SDK wrapper, the drop-in client, and
the proxy so the router + caching + retrieval + compression logic lives in ONE place.

optimize_request(): given a chat request body, asks the router which lever to use for this
call — cache_only, retrieve (chunk-level, slices INTO big documents), compress (LLMLingua-2,
lossy, opt-in), or passthrough — applies it in-place, and returns the decision. Every lever
fails safe: retrieval/compression fall through to cache_only if they can't help, so the call
never silently loses context. record_usage(): computes honest savings and feeds the real
cache-hit rate back to the router so it adapts per provider/session.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .api_adapter import chunk_text, retrieval_select, select_chunk_indices
from .provider_cache import apply_anthropic_cache, count_tokens, savings_from_usage
from .router import BrevitasRouter

# messages/system blocks at least this large get chunked so retrieval can slice INTO them
CHUNK_MIN_TOKENS = 512
# below this a message is too small to be worth compressing
COMPRESS_MIN_TOKENS = 200


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
                     session_id: str, task_hint: Optional[str] = None) -> dict:
    """Apply the router-chosen lever to `body` in place. Returns decision meta."""
    messages = body.get("messages", []) or []
    if not messages:
        return {"strategy": "passthrough", "reason": "no messages"}

    system = body.get("system")
    stable = _stable_context(messages, system)
    query = _msg_text(messages[-1].get("content", "")) if messages else ""

    decision = router.decide(session_id, stable, query, task_hint)
    strategy = decision.strategy
    base = {"reason": decision.reason, "task": decision.task, "costs": decision.costs}

    if strategy == "retrieve":
        # 1) chunk-level retrieval: slice INTO large documents (PDF/textbook in one message)
        meta = _apply_chunk_retrieval(body, query)
        # 2) fall back to message-level retrieval (multi-turn context made of many messages)
        if meta is None:
            meta = _apply_message_retrieval(body, query, messages)
        if meta is not None:
            return {"strategy": "retrieve", **base, **meta}
        strategy = "cache_only"   # retrieval bailed -> safe fall-through to caching
        base["reason"] = decision.reason + " (retrieval unavailable -> cache)"

    elif strategy == "compress":
        meta = _apply_compression(body, decision.task)
        if meta is not None:
            return {"strategy": "compress", **base, **meta}
        strategy = "cache_only"   # llmlingua unavailable -> safe fall-through to caching
        base["reason"] = decision.reason + " (compression unavailable -> cache)"

    # cache_only / passthrough
    if provider == "anthropic" and strategy != "passthrough":
        plan = apply_anthropic_cache(body)   # inject cache_control breakpoints
        return {"strategy": "cache_only", **base,
                "cache_breakpoints": plan.breakpoints,
                "cached_prefix_tokens": plan.cached_prefix_tokens}
    # OpenAI/DeepSeek: caching is automatic if prefix is byte-identical — we DON'T mutate it.
    return {"strategy": strategy, **base}


def _apply_chunk_retrieval(body: dict, query: str) -> Optional[dict]:
    """Retrieve relevant chunks from within LARGE string blocks (system + prior messages).

    Returns meta on success, or None if there's nothing large to slice or retrieval fails
    safe (encoder missing / low confidence) — caller then falls back to caching."""
    messages = body.get("messages", []) or []
    system = body.get("system")

    slots: List[tuple] = []   # (kind, ref, text)
    if isinstance(system, str) and count_tokens(system) >= CHUNK_MIN_TOKENS:
        slots.append(("system", None, system))
    for i, m in enumerate(messages[:-1]):
        c = m.get("content")
        if isinstance(c, str) and count_tokens(c) >= CHUNK_MIN_TOKENS:
            slots.append(("message", i, c))
    if not slots:
        return None

    global_chunks: List[str] = []
    prov: List[int] = []                  # slot index for each global chunk
    for si, (_, _, text) in enumerate(slots):
        for ch in chunk_text(text):
            global_chunks.append(ch)
            prov.append(si)
    if not global_chunks:
        return None

    baseline = sum(count_tokens(c) for c in global_chunks)
    sel = select_chunk_indices(query[:300], global_chunks, k=12)
    if sel["fallback_applied"]:
        return None
    keep = set(sel["indices"])

    optimized = 0
    for si, (kind, ref, _) in enumerate(slots):
        survivors = [global_chunks[gi] for gi in range(len(global_chunks))
                     if prov[gi] == si and gi in keep]
        new_text = "\n\n".join(survivors)
        optimized += count_tokens(new_text)
        if kind == "system":
            body["system"] = new_text
        else:
            messages[ref]["content"] = new_text

    return {"baseline_tokens": baseline, "optimized_tokens": optimized,
            "kept": len(keep), "of": len(global_chunks),
            "top_score": sel.get("top_score"), "level": "chunk"}


def _apply_message_retrieval(body: dict, query: str, messages: List[dict]) -> Optional[dict]:
    """Keep only the prior MESSAGES relevant to the query (multi-turn context). Fail-safe."""
    stable = [_msg_text(m.get("content", "")) for m in messages[:-1]]
    stable = [s for s in stable if s]
    if len(stable) < 2:
        return None
    sel = retrieval_select(query[:200], stable, k=8)
    if sel["fallback_applied"] or not sel["selected_context"]:
        return None
    keep = set(sel["selected_context"])
    new_msgs = [m for m in messages[:-1] if _msg_text(m.get("content", "")) in keep]
    new_msgs.append(messages[-1])
    if len(new_msgs) >= len(messages):
        return None
    body["messages"] = new_msgs
    return {"baseline_tokens": sel["baseline_tokens"],
            "optimized_tokens": sel["optimized_tokens"],
            "kept": len(new_msgs), "of": len(messages), "level": "message"}


def _apply_compression(body: dict, task: str) -> Optional[dict]:
    """Compress large prior-message text with LLMLingua-2 (LOSSY). Returns None if compression
    is unavailable or didn't actually shrink anything (fail-safe to caching)."""
    from .task_router import TaskCompressionRouter

    messages = body.get("messages", []) or []
    tcr = TaskCompressionRouter()
    before = after = 0
    changed = False
    for m in messages[:-1]:
        c = m.get("content")
        if not isinstance(c, str) or count_tokens(c) < COMPRESS_MIN_TOKENS:
            continue
        res = tcr.route(c, task_hint=task)
        opt = res.optimization
        if opt.lossy and opt.tokens_after < opt.tokens_before:
            m["content"] = opt.optimized
            before += opt.tokens_before
            after += opt.tokens_after
            changed = True
    if not changed:
        return None
    return {"baseline_tokens": before, "optimized_tokens": after, "level": "compress"}


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
