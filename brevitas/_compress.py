"""
Shared compression + usage-reporting logic used by both the SDK wrappers and the proxy.
All compression is done by calling the Brevitas REST API so every call is tracked.
"""
from __future__ import annotations

import httpx

from .config import get as _cfg
from .session import BrevitasSession

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")
    def _count_tokens(text: str) -> int:
        return len(_enc.encode(text, disallowed_special=()))
except Exception:
    def _count_tokens(text: str) -> int:
        return max(1, int(len(text.split()) * 1.3))


def count_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += _count_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += _count_tokens(block.get("text", ""))
    return total


def compress_messages(
    messages: list[dict],
    session: BrevitasSession,
    task: str = "",
    complexity: float = 0.5,
    compression_level: int = 2,
    prune_budget: int = 5,
    pipeline: str = "",
    agent: str = "",
    run_id: str = "",
) -> tuple[list[dict], int, int]:
    """
    Compress a messages list via the Brevitas API, preserving prefix stability.
    Only the LAST user message is compressed (volatile tail).
    All earlier messages (system + tools + prior turns = stable prefix) are left BYTE-IDENTICAL.
    Returns (compressed_messages, baseline_tokens, compressed_tokens).
    If compression fails or is disabled, returns the original messages unchanged.
    """
    cfg = _cfg()
    if not cfg.get("enabled") or not cfg.get("api_key"):
        baseline = count_messages_tokens(messages)
        return messages, baseline, baseline

    baseline_tokens = count_messages_tokens(messages)

    # Find the index of the last user-role message (volatile tail)
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break

    # If no user message, return unchanged
    if last_user_idx < 0:
        return messages, baseline_tokens, baseline_tokens

    # Extract only the last user message for compression
    last_msg = messages[last_user_idx]
    content = last_msg.get("content", "")
    if isinstance(content, str):
        last_text = content
    elif isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        last_text = " ".join(parts)
    else:
        return messages, baseline_tokens, baseline_tokens

    prior = session.prior_context()

    try:
        resp = httpx.post(
            f"{cfg['base_url']}/v1/compress",
            headers={"X-API-Key": cfg["api_key"]},
            json={
                "task": task or (last_text[:200] if last_text else ""),
                "messages": [last_text],  # Only compress the last user message
                "prior_context": prior,
                "complexity": complexity,
                "compression_level": compression_level,
                "prune_budget": prune_budget,
                "pipeline": pipeline,
                "agent": agent,
                "run_id": run_id,
                # report_usage() below records the billing row via /v1/usage;
                # don't also record here or every call double-counts.
                "meter": False,
            },
            timeout=cfg.get("timeout", 30),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return messages, baseline_tokens, baseline_tokens

    compressed_texts = data.get("compressed_messages", [last_text])

    # Rebuild messages: prefix stays IDENTICAL, only last user message may change
    out_messages: list[dict] = []
    for i, m in enumerate(messages):
        if i == last_user_idx and compressed_texts:
            # Replace only the last user message's TEXT content with compressed version
            # Preserve any non-text blocks (tool_result, images, etc.)
            new_m = dict(m)
            if isinstance(m.get("content"), list):
                # Mixed content: replace text blocks, preserve others
                new_content = []
                text_replaced = False
                for block in m.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        # Replace the first text block with compressed text
                        if not text_replaced:
                            new_content.append({"type": "text", "text": compressed_texts[0]})
                            text_replaced = True
                        # Skip other text blocks (already included in compression)
                    else:
                        # Preserve non-text blocks (tool_result, images, etc.)
                        new_content.append(block)
                # If no text block was found, add the compressed text
                if not text_replaced:
                    new_content.insert(0, {"type": "text", "text": compressed_texts[0]})
                new_m["content"] = new_content
            else:
                # String content: replace with compressed text
                new_m["content"] = compressed_texts[0]
            out_messages.append(new_m)
        else:
            # All other messages pass through unchanged (same dict, same content)
            out_messages.append(m)

    # Remember the pipeline's quality estimate so report_usage() can forward it
    # to the billing quality gate (otherwise savings are never billed).
    session.last_quality = data.get("quality_proxy")

    # Use the server's authoritative counts for BOTH baseline and optimized so
    # report_usage compares like-for-like. The client-side baseline counted only
    # the messages (not prior_context), while optimized comes from the server and
    # includes pruned context — mixing them made multi-hop calls look like a loss
    # (compressed >= baseline) and get dropped from billing.
    server_baseline = data.get("baseline_tokens")
    if server_baseline:
        baseline_tokens = int(server_baseline)
    compressed_tokens = data.get("optimized_tokens", count_messages_tokens(out_messages))
    return out_messages, baseline_tokens, compressed_tokens


def report_usage(
    provider: str,
    model: str,
    baseline_tokens: int,
    compressed_tokens: int,
    session: BrevitasSession,
    pipeline: str = "",
    agent: str = "",
    run_id: str = "",
) -> None:
    """Report usage to Brevitas for billing. Fire-and-forget."""
    cfg = _cfg()
    if not cfg.get("api_key") or baseline_tokens <= compressed_tokens:
        return
    try:
        httpx.post(
            f"{cfg['base_url']}/v1/usage",
            headers={"X-API-Key": cfg["api_key"]},
            json={
                "provider": provider,
                "model": model,
                "baseline_tokens": baseline_tokens,
                "compressed_tokens": compressed_tokens,
                "session_id": session.session_id,
                "pipeline": pipeline,
                "agent": agent,
                "run_id": run_id,
                "quality_score": session.last_quality,
            },
            timeout=5,
        )
    except Exception:
        pass  # billing reporting is best-effort; never break the user's pipeline
