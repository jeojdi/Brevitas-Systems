"""Shared request optimization engine — used by the SDK wrapper, drop-in client, and
the proxy so the router + caching + retrieval logic lives in ONE place.

optimize_request(): given a chat request body, asks the router whether to cache_only, retrieve,
or passthrough for this call, applies the chosen strategy in-place, and returns the
decision. record_usage(): computes honest savings from the provider response and feeds the
real cache-hit rate back to the router so it adapts per provider/session.

Caching is byte-preserving. Retrieval is context-reducing and can change model behavior, so
automatic retrieval is disabled unless ``BREVITAS_RETRIEVAL_ENABLED=1``. The explicit retrieval
API remains available for paired workload evaluation.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .api_adapter import retrieval_select
from .provider_cache import apply_anthropic_cache, count_cache_control, savings_from_usage
from .router import BrevitasRouter

# Cache-stable retrieval layout (brief b1): per session, the set of retrieved context
# messages is APPEND-ONLY. Once a chunk has been sent to the provider it stays sent, in
# first-seen order, so the retrieved prefix is byte-identical turn-over-turn and the
# provider prefix cache HITS it — retrieval then composes with caching instead of
# busting it (the token-savings-≠-dollar-savings root cause). Relative to per-turn retrieval,
# the accumulator only ADDS context; it never drops a block retrieval already chose.
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


def _retrieval_enabled() -> bool:
    return os.environ.get("BREVITAS_RETRIEVAL_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _reorder_enabled() -> bool:
    """Message reordering (b9 shared-prefix promotion) is OFF by default: it can change
    a causal conversation's meaning and it can bust the provider's own prefix cache.
    Opt in explicitly with BREVITAS_MESSAGE_REORDER=1."""
    return os.environ.get("BREVITAS_MESSAGE_REORDER", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _lever_allowed(lever: str) -> bool:
    """Fail-closed gate check: a tripped/failed/missing quality gate returns False so we
    fall back to full context. Any import/eval error also returns False (safe: the lever
    it guards is opt-in, so disabling it never breaks the lossless default path)."""
    try:
        from token_efficiency_model.quality.gate import lever_allowed
        return lever_allowed(lever)
    except Exception:
        return False


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
    if isinstance(content, dict):
        return _msg_text(content.get("text", content.get("content", "")))
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _message_structure_rewrite_safe(messages: Any) -> bool:
    """Whether messages may be reordered or removed without breaking API structure.

    Provider message arrays are not just prose.  System directives, tool calls/results,
    images, documents, and newer typed content blocks have ordering/adjacency rules.  In
    particular, Anthropic's mid-conversation system messages must remain next to the turn
    they govern and tool results must remain paired with their tool calls.  Treat every
    such request as an opaque ordered sequence: native caching may still annotate it, but
    b9 promotion and retrieval must not reorder or prune it.

    This intentionally uses a narrow allow-list.  A new provider block type therefore
    fails safe until Brevitas explicitly understands its structural contract.
    """
    if not isinstance(messages, list) or not messages:
        return False
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return False
        role = message.get("role")
        # A single leading plain-text system instruction is structurally stable and
        # retrieval preserves it. Mid-conversation system directives remain opaque.
        if role == "system" and index == 0:
            pass
        elif role not in {"user", "assistant"}:
            return False
        if any(key in message for key in (
            "tool_calls", "tool_call_id", "function_call", "function_call_output",
            "output_config", "recipient", "name",
        )):
            return False
        # Plain strings are the only content representation the retrieval/reordering
        # code can currently prove is free of tool/media/directive semantics.
        if not isinstance(message.get("content"), str):
            return False
    return True


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

    When a multi-agent `pipeline` label is present, shared context may be promoted to a
    leading prefix (brief b9) so it caches across agents whose system prompts differ.
    Reordering preserves information but can change model behavior, so it is explicitly
    marked quality-affecting and cannot inherit byte-preserving billing treatment."""
    messages = body.get("messages", []) or []
    if not messages:
        return {"strategy": "passthrough", "reason": "no messages",
                "quality_status": "byte_preserving", "response_faithful": True}
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        return {
            "strategy": "passthrough",
            "reason": "unsupported message structure",
            "quality_status": "byte_preserving",
            "response_faithful": True,
        }
    structure_rewrite_safe = _message_structure_rewrite_safe(messages)
    request_reordered = False
    if body.get("model"):
        router.model = str(body["model"])   # per-model rates in the router's cost model

    # response_faithful: True only while the request we send stays byte-faithful to the
    # ORIGINAL (so its answer is valid for the original request key). Any content-changing
    # transform — retrieval pruning or a message reorder — flips this to False, and the
    # proxy then refuses to cache the response. Byte-lossless additions (Anthropic
    # cache_control markers, template split) keep it True.
    response_faithful = True

    # b9 (cache-aware): promote shared context to a cacheable leading prefix for
    # multi-agent pipelines — but ONLY once we've OBSERVED that the provider is NOT
    # already caching the natural prefix well, and only if the promoted shared block
    # would cache MORE tokens than it already does. This prevents the regression where
    # a blind reorder breaks a cache the provider was already serving (Don't Break the
    # Cache, arXiv 2601.06007). With <2 observations we stay conservative and don't reorder.
    if (pipeline and structure_rewrite_safe and _reorder_enabled()
            and _lever_allowed("reorder")):
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
            if reordered:
                # reordering context can change a causal conversation's answer, so the
                # response is no longer valid under the original request key.
                response_faithful = False
                request_reordered = True
                if not st9["reordered"]:
                    # counterfactual baseline: natural hit rate BEFORE we touched anything
                    st9["reordered"] = True
                    st9["pre_hit"] = obs_hit if obs_count >= 1 else 0.0

    system = body.get("system")
    stable = _stable_context(messages, system)
    query = _msg_text(messages[-1].get("content", "")) if messages else ""
    # A common first-turn shape puts a large reusable document and a short
    # question in separate blocks of the final user message. Treat every block
    # except the last as stable so ROI/retrieval decisions see the same cacheable
    # prefix that apply_anthropic_cache can mark.
    if messages:
        final_content = messages[-1].get("content")
        if isinstance(final_content, list) and len(final_content) >= 2:
            stable.extend(_msg_text(block) for block in final_content[:-1]
                          if _msg_text(block))
            query = _msg_text(final_content[-1])

    decision = router.decide(session_id, stable, query)

    strategy = decision.strategy
    if strategy == "retrieve" and not structure_rewrite_safe:
        strategy = "cache_only"
        router.note_strategy(session_id, "cache_only")
        meta = {
            "strategy": "cache_only",
            "reason": "message_structure_preserved",
            "router_recommendation": "retrieve",
            "quality_status": "byte_preserving",
        }
    elif strategy == "retrieve" and not _retrieval_enabled():
        strategy = "cache_only"
        router.note_strategy(session_id, "cache_only")
        meta = {
            "strategy": "cache_only",
            "reason": "retrieval_opt_in_required",
            "router_recommendation": "retrieve",
            "quality_status": "byte_preserving",
        }
    elif strategy == "retrieve" and not _lever_allowed("retrieval"):
        # Quality gate tripped/failed/missing → force the original full context.
        strategy = "cache_only"
        router.note_strategy(session_id, "cache_only")
        meta = {
            "strategy": "cache_only",
            "reason": "retrieval_gate_tripped",
            "router_recommendation": "retrieve",
            "quality_status": "byte_preserving",
        }
    elif strategy == "retrieve":
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
            # dropping accumulated chunks would bust the prefix — past the cap the safest
            # move is to stop pruning and send everything: cache_only)
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
            # Context was dropped: the answer is NOT valid under the original request key.
            response_faithful = False
            meta = {"strategy": "retrieve", "reason": decision.reason,
                    "kept": len(new_msgs), "of": len(messages),
                    "baseline_tokens": sel["baseline_tokens"],
                    "optimized_tokens": sel["optimized_tokens"],
                    "retrieval_method": sel.get("method"),
                    "bridge_expansions": sel.get("bridge_expansions", 0),
                    "quality_status": "experimental_unverified"}
        else:
            strategy = "cache_only"  # retrieval bailed -> safe fall-through to caching
            router.note_strategy(session_id, "cache_only")  # full context actually sent
            meta = {
                "strategy": "cache_only",
                "reason": f"retrieval_fallback:{sel.get('reason', 'unknown')}",
                "retrieval_method": sel.get("method"),
                "quality_status": "byte_preserving",
            }
    else:
        meta = None
    if meta is None:
        meta = {"strategy": strategy, "reason": decision.reason}
    if request_reordered:
        meta["router_strategy"] = meta.get("strategy", strategy)
        meta["strategy"] = "shared_prefix_reorder"
        meta["quality_status"] = "experimental_unverified"
        meta["semantic_order_changed"] = True

    # Anthropic requires EXPLICIT cache_control markers (OpenAI/DeepSeek cache byte-identical
    # prefixes automatically). Apply on EVERY path — including after a retrieval rebuild and
    # on passthrough (the router's verdict is about the STABLE prefix; a huge context block
    # inside the last message is still cacheable). apply_anthropic_cache's own >=min_tokens
    # guard makes this a no-op when nothing is worth caching.
    if provider == "anthropic":
        # Cross-run template mining (CR2): learn where the system prompt goes volatile
        # across recurring runs, and split the block at that boundary so the stable
        # prefix carries its own breakpoint. Byte-lossless: Anthropic joins text blocks
        # with NO separator (verified live via /v1/messages/count_tokens — identical
        # token counts single vs split), so the model sees identical input.
        # BREVITAS_TEMPLATE_SPLIT=0 is the kill-switch.
        import os as _os
        sysv = body.get("system")
        if isinstance(sysv, str) and sysv:
            from .template_miner import default_miner
            b9boundary = default_miner.observe_boundary(f"tm:{session_id}", sysv)
            if 0 < b9boundary < len(sysv):
                meta["template_volatile_at"] = b9boundary
                if _os.environ.get("BREVITAS_TEMPLATE_SPLIT", "0") not in ("0", "false", "no"):
                    body["system"] = [{"type": "text", "text": sysv[:b9boundary]},
                                      {"type": "text", "text": sysv[b9boundary:]}]
        # TTL tier by observed run spacing (cross-run lever): calls spaced past the
        # 5-minute TTL but within ~an hour re-pay the 1.25x write EVERY run on the 5m
        # tier; the 1h tier writes once at 2x, then each spaced run reads at 0.1x and
        # refreshes the hour for free (provider docs). Persisted gap_ewma makes this
        # survive restarts.
        gap = router.session_gap(session_id)
        ttl = "1h" if 300.0 < gap <= 3600.0 else ""
        existing = count_cache_control(body)
        if existing:
            # Respect caller-owned cache policy. It is not Brevitas-attributable
            # and must not be stripped/re-priced as our optimization.
            meta["cache_breakpoints"] = existing
            meta["cache_control_owner"] = "caller"
        else:
            if gap > 3600.0:
                allowed, roi_reason = False, "reuse_outside_cache_ttl"
            else:
                allowed, roi_reason = router.cache_write_allowed(session_id, ttl)
            meta["cache_roi"] = roi_reason
            if allowed:
                plan = apply_anthropic_cache(body, ttl=ttl)
                meta["cache_breakpoints"] = plan.breakpoints
                meta["cached_prefix_tokens"] = plan.cached_prefix_tokens
                meta["cache_control_owner"] = "brevitas"
                if plan.ttl:
                    meta["cache_ttl"] = plan.ttl
            else:
                meta["cache_breakpoints"] = 0
                meta["cached_prefix_tokens"] = 0
    # OpenAI/DeepSeek: caching is automatic if prefix is byte-identical — we DON'T mutate it.
    meta["response_faithful"] = response_faithful
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
    creation = usage.get("cache_creation") or {}
    if not isinstance(creation, dict):
        creation = {}
    router.observe_usage(
        session_id, prompt, s.cached_tokens,
        cache_write_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
        cache_write_5m_tokens=int(creation.get("ephemeral_5m_input_tokens", 0) or 0),
        cache_write_1h_tokens=int(creation.get("ephemeral_1h_input_tokens", 0) or 0),
    )

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
