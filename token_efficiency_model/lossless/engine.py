"""Shared lossless optimization engine — used by the SDK wrapper, the drop-in client, and
the proxy so the router + caching + retrieval logic lives in ONE place.

optimize_request(): given a chat request body, asks the router whether to cache_only, retrieve,
or passthrough for this call, applies the chosen LOSSLESS strategy in-place, and returns the
decision. record_usage(): computes honest savings from the provider response and feeds the
real cache-hit rate back to the router so it adapts per provider/session.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .api_adapter import retrieval_select
from .provider_cache import apply_anthropic_cache, savings_from_usage
from .router import BrevitasRouter

# Cache-stable retrieval layout (brief b1): per session, the set of retrieved context
# messages is APPEND-ONLY. Once a chunk has been sent to the provider it stays sent, in
# first-seen order, so the retrieved prefix is byte-identical turn-over-turn and the
# provider prefix cache HITS it — retrieval then composes with caching instead of
# busting it (the token-savings-≠-dollar-savings root cause). Strictly lossless vs pure
# per-turn retrieval: we only ever ADD context, never drop what retrieval already chose.
_RETRIEVED_MAX_SESSIONS = 1024
_RETRIEVED_MAX_CHUNKS = 256    # per-session cap; past it, fail safe to full context
_retrieved_blocks: "OrderedDict[str, List[str]]" = OrderedDict()

# b9 counterfactual "do no harm" state, per pipeline. The v2 gate had a death spiral:
# reordering busts the cache -> observed hit rate falls -> the gate reads "provider isn't
# caching well" -> keeps reordering. The fix is a COUNTERFACTUAL baseline: snapshot the
# hit rate observed BEFORE the first reorder; if the post-reorder hit rate (EWMA over
# >= _B9_MIN_POST observations) drops below that snapshot minus a margin, b9 locks OFF
# for the whole pipeline — sticky, so a reorder that broke a working cache never repeats.
_B9_MAX_PIPES = 1024
_B9_MIN_POST = 3
_B9_MARGIN = 0.05
_b9_pipes: "OrderedDict[str, dict]" = OrderedDict()


def _b9_state(pipeline: str) -> dict:
    st = _b9_pipes.get(pipeline)
    if st is None:
        if len(_b9_pipes) >= _B9_MAX_PIPES:
            _b9_pipes.popitem(last=False)
        st = {"reordered": False, "locked": False,
              "pre_hit": 0.0, "post_hit": -1.0, "post_n": 0}
        _b9_pipes[pipeline] = st
    else:
        _b9_pipes.move_to_end(pipeline)
    return st


def _accumulate_retrieved(session_id: str, selected: List[str]) -> List[str]:
    """Union `selected` into the session's append-only retrieved set (stable order)."""
    acc = _retrieved_blocks.get(session_id)
    if acc is None:
        if len(_retrieved_blocks) >= _RETRIEVED_MAX_SESSIONS:
            _retrieved_blocks.popitem(last=False)
        acc = []
        _retrieved_blocks[session_id] = acc
    else:
        _retrieved_blocks.move_to_end(session_id)
    seen = set(acc)
    for chunk in selected:
        if chunk not in seen:
            acc.append(chunk)
            seen.add(chunk)
    return list(acc)  # copy — never hand out the live internal list


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
                     session_id: str, pipeline: str = "", agent: str = "") -> dict:
    """Apply the router-chosen lossless strategy to `body` in place. Returns decision meta.

    When a multi-agent `pipeline` label is present, shared context is first promoted to a
    byte-identical leading prefix (brief b9) so it caches across agents whose system
    prompts differ — lossless (reorder only, proven-shared content only)."""
    messages = body.get("messages", []) or []
    if not messages:
        return {"strategy": "passthrough", "reason": "no messages"}
    if body.get("model"):
        router.model = str(body["model"])   # per-model rates in the router's cost model

    # b9 (cache-aware): promote shared context to a cacheable leading prefix for
    # multi-agent pipelines — but ONLY once we've OBSERVED that the provider is NOT
    # already caching the natural prefix well, and only if the promoted shared block
    # would cache MORE tokens than it already does. This prevents the regression where
    # a blind reorder breaks a cache the provider was already serving (Don't Break the
    # Cache, arXiv 2601.06007). With <2 observations we stay conservative and don't reorder.
    if pipeline:
        st9 = _b9_state(pipeline)
        if not st9["locked"]:
            from .provider_cache import count_tokens as _ct
            from .shared_prefix import layout_ex as _shared_layout_ex
            obs_hit, obs_count = router.observed_cache(session_id)
            total_tok = sum(_ct(_msg_text(m.get("content", ""))) for m in messages)
            if obs_count >= 2:
                natural_cached = obs_hit * total_tok       # provider already caches this much
            else:
                natural_cached = float(total_tok)          # unknown → assume well-cached (don't reorder)
            new_msgs, reordered = _shared_layout_ex(
                pipeline, agent or session_id, messages,
                natural_cached_tokens=natural_cached, count_tokens=_ct)
            body["messages"] = messages = new_msgs
            if reordered and not st9["reordered"]:
                # counterfactual baseline: the natural hit rate BEFORE we touched anything
                st9["reordered"] = True
                st9["pre_hit"] = obs_hit if obs_count >= 1 else 0.0

    system = body.get("system")
    stable = _stable_context(messages, system)
    query = _msg_text(messages[-1].get("content", "")) if messages else ""

    decision = router.decide(session_id, stable, query)

    strategy = decision.strategy
    if strategy == "retrieve":
        # reduce the prior context to the relevant chunks (fail-safe to full inside)
        sel = retrieval_select(query[:200], stable, k=8, use_adaptive=True)
        if not sel["fallback_applied"] and sel.get("baseline_tokens", 0) > 0:
            # feed the MEASURED keep fraction back so the router's retrieve arm is
            # priced from data, not the 0.6 prior
            router.observe_retrieval(session_id, sel["baseline_tokens"],
                                     sel["optimized_tokens"])
        acc: List[str] = []
        if not sel["fallback_applied"] and sel["selected_context"]:
            # Cache-stable layout (b1): union this turn's picks into the session's
            # APPEND-ONLY retrieved set so the sent prefix stays byte-identical and
            # the provider cache hits it. `keep` only ever grows across turns.
            acc = _accumulate_retrieved(session_id, list(sel["selected_context"]))
        if acc and len(acc) <= _RETRIEVED_MAX_CHUNKS:
            # (an unbounded append-only set eventually re-approaches full context AND
            # dropping accumulated chunks would bust the prefix — past the cap the only
            # lossless move is to stop pruning and send everything: cache_only)
            keep = set(acc)
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
                    # For user messages, only keep if content is in the accumulated set
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
            router.note_strategy(session_id, "cache_only")  # full context actually sent
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


def record_usage(usage: dict, provider: str, router: BrevitasRouter, session_id: str,
                 pipeline: str = "", model: str = ""):
    """Honest savings from real usage + feed cache-hit feedback to the router (and, when a
    pipeline label is present, to the b9 do-no-harm lock)."""
    s = savings_from_usage(usage, provider, model=model)
    if provider == "anthropic":
        prompt = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) \
                 + usage.get("cache_read_input_tokens", 0)
    else:
        prompt = usage.get("prompt_tokens", 0)
    router.observe_usage(session_id, prompt, s.cached_tokens)

    # b9 counterfactual check: is the pipeline caching WORSE since we reordered?
    if pipeline and prompt > 0:
        st9 = _b9_pipes.get(pipeline)
        if st9 is not None and st9["reordered"] and not st9["locked"]:
            hit = max(0.0, min(1.0, s.cached_tokens / prompt))
            st9["post_hit"] = hit if st9["post_hit"] < 0 else 0.5 * st9["post_hit"] + 0.5 * hit
            st9["post_n"] += 1
            if st9["post_n"] >= _B9_MIN_POST and st9["post_hit"] < st9["pre_hit"] - _B9_MARGIN:
                st9["locked"] = True   # sticky: we did harm once; never reorder this pipeline again
    return s
