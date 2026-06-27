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
) -> tuple[list[dict], int, int]:
    """
    Compress a messages list via the Brevitas API.
    Returns (compressed_messages, baseline_tokens, compressed_tokens).
    If compression fails or is disabled, returns the original messages unchanged.
    """
    cfg = _cfg()
    if not cfg.get("enabled") or not cfg.get("api_key"):
        baseline = count_messages_tokens(messages)
        return messages, baseline, baseline

    # Extract text from messages for compression (keep roles/structure intact)
    texts = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            texts.append(" ".join(parts))

    baseline_tokens = count_messages_tokens(messages)
    prior = session.prior_context()

    try:
        resp = httpx.post(
            f"{cfg['base_url']}/v1/compress",
            headers={"X-API-Key": cfg["api_key"]},
            json={
                "task": task or (texts[0][:200] if texts else ""),
                "messages": texts,
                "prior_context": prior,
                "complexity": complexity,
                "compression_level": compression_level,
                "prune_budget": prune_budget,
            },
            timeout=cfg.get("timeout", 30),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return messages, baseline_tokens, baseline_tokens

    compressed_texts = data.get("compressed_messages", texts)
    pruned_context   = data.get("pruned_context", [])

    # Rebuild messages with compressed content, preserving roles
    out_messages: list[dict] = []
    for i, m in enumerate(messages):
        new_m = dict(m)
        if i < len(compressed_texts):
            if isinstance(m.get("content"), list):
                new_m["content"] = [{"type": "text", "text": compressed_texts[i]}]
            else:
                new_m["content"] = compressed_texts[i]
        out_messages.append(new_m)

    compressed_tokens = data.get("optimized_tokens", count_messages_tokens(out_messages))
    return out_messages, baseline_tokens, compressed_tokens


def report_usage(
    provider: str,
    model: str,
    baseline_tokens: int,
    compressed_tokens: int,
    session: BrevitasSession,
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
            },
            timeout=5,
        )
    except Exception:
        pass  # billing reporting is best-effort; never break the user's pipeline
