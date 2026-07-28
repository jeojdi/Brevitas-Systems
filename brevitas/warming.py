"""Warm-prefix observation for predictive provider-cache warming.

extract_warm_prefix() snapshots the cacheable prefix of a POST-optimization
request so a worker can later replay it byte-identical as a keep-alive ping
before the provider TTL lapses. Two shapes exist:

- Anthropic (explicit markers): everything through the LAST cache_control
  marker, walking segments in the order Anthropic serializes (and
  apply_anthropic_cache marks) them: tools, system, messages.
- Automatic-prefix-cache providers (DeepSeek; OpenAI-compat generally): no
  markers exist — the prefix is the stable leading portion of the conversation,
  everything before the final (volatile) turn, plus tools and any caller/Brevitas
  prompt_cache_key, with the upstream endpoint recorded so the worker pings the
  provider this request actually used.

Providers where the keep-alive math cannot work — no cache at all, or a discount
too shallow for a ping to ever pay for itself — are NOT extractable and get
measurement only (see _AUTO_WARMABLE). extract_warm_prefix returns None for them.

The module also hosts the warm-observer callback slot (set_warm_observer),
mirroring proxy.set_usage_reporter: the hosted API installs a sink at startup,
so brevitas/ never imports server code.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from token_efficiency_model.lossless.provider_cache import (
    _content_tokens,
    anthropic_min_tokens,
    count_tokens,
)

# Headers the cached prefix varies on. anthropic-beta rides here because the 1h
# TTL tier only exists under its beta flag — replaying without it would address a
# different cache entry. Credentials are NEVER part of the payload or the hash.
_VARY_HEADERS = ("anthropic-version", "anthropic-beta")

_TTL_SECONDS = {"1h": 3600, "": 300}

# Automatic-prefix-cache providers extraction supports, from the 2026-07 provider
# capability research. A provider earns a row ONLY where warming can provably net
# positive: the cached-read price must be a deep discount (one warm return buys
# back many pings) AND the provider must document a TTL that reads/pings extend.
# Everything else is measurement-only BY DESIGN — extraction returns None and the
# warming worker never sees it:
#   groq / fireworks   cached reads bill 0.50x input: a keep-alive ping costs
#                      exactly what a warm return saves — warming can NEVER pay;
#   perplexity         no cached-input discount exists at all — nothing to warm;
#   mistral / xai /    a real discount exists, but TTL and refresh-on-read are
#   together /         undocumented, so pings would be speculative spend against
#   openrouter         an unknown eviction policy (openrouter adds unverifiable
#                      pass-through routing on top).
_AUTO_WARMABLE = {
    "openai":   {"min_prefix_tokens": 1024},
    "deepseek": {"min_prefix_tokens": 64},
}


def provider_warmable(provider: str) -> bool:
    """True when warming pings can mathematically pay for themselves on this
    provider. Everything else stays measurement-only (see _AUTO_WARMABLE)."""
    return provider == "anthropic" or provider in _AUTO_WARMABLE


def _auto_ttl_seconds(provider: str, model: str) -> int:
    """Assumed cache lifetime for automatic-prefix providers. Deliberately the
    documented FLOOR, never an optimistic estimate — overestimating TTL means the
    entry evicts before the ping and the spend buys nothing."""
    if provider == "deepseek":
        return 14400               # hours-scale persistence while a prefix stays in use
    if (model or "").lower().startswith("gpt-5.6"):
        return 1800                # explicit-mode "30m" is a documented minimum lifetime
    return 300                     # pre-5.6 automatic: 5-10min inactivity eviction floor


@dataclass
class WarmPrefix:
    """Everything a warming worker needs to replay one cached prefix byte-identical."""
    provider: str
    model: str
    prefix_hash: str
    prefix_tokens: int
    provider_ttl_seconds: int
    payload: dict[str, Any]


_warm_observer: Callable | None = None


def set_warm_observer(callback: Callable | None) -> None:
    """Install the hosted API's warm-prefix sink; local proxies leave it unset."""
    global _warm_observer
    _warm_observer = callback


def get_warm_observer() -> Callable | None:
    return _warm_observer


def _vary_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    vary: dict[str, str] = {}
    for name in _VARY_HEADERS:
        for key, value in (headers or {}).items():
            if key.lower() == name and value:
                vary[name] = value
                break
    return vary


def _last_marker(body: dict) -> tuple[tuple, dict] | None:
    """Position and marker of the LAST cache_control block marker in serialization
    order: tools, then system blocks, then message content blocks. A request-wide
    top-level cache_control has no block boundary, so it is intentionally not found."""
    found: tuple[tuple, dict] | None = None
    tools = body.get("tools")
    if isinstance(tools, list):
        for i, tool in enumerate(tools):
            if isinstance(tool, dict) and isinstance(tool.get("cache_control"), dict):
                found = (("tools", i), tool["cache_control"])
    sysv = body.get("system")
    if isinstance(sysv, list):
        for i, block in enumerate(sysv):
            if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
                found = (("system", i), block["cache_control"])
    for mi, msg in enumerate(body.get("messages") or []):
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for bi, block in enumerate(content):
                if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
                    found = (("message", mi, bi), block["cache_control"])
    return found


def _capture_prefix(body: dict, position: tuple) -> dict[str, Any]:
    """Deep-copied segments through the marked block. Markers — and therefore their
    positions and per-marker ttl — ride inside the copied blocks unchanged, so the
    payload alone is enough for a byte-identical replay of the cached prefix."""
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    sysv = body.get("system")
    kind = position[0]
    if kind == "tools":
        return {"tools": deepcopy(tools[: position[1] + 1]),
                "system": None, "messages_prefix": []}
    if kind == "system":
        return {"tools": deepcopy(tools),
                "system": deepcopy(sysv[: position[1] + 1]),
                "messages_prefix": []}
    _, mi, bi = position
    messages = body.get("messages") or []
    prefix_messages = deepcopy(messages[:mi])
    marked = deepcopy(messages[mi])
    marked["content"] = marked["content"][: bi + 1]
    prefix_messages.append(marked)
    return {"tools": deepcopy(tools), "system": deepcopy(sysv),
            "messages_prefix": prefix_messages}


def _local_prefix_tokens(captured: dict[str, Any]) -> int:
    """Local re-count for caller-owned markers, mirroring apply_anthropic_cache's
    per-segment arithmetic (tools via str(), system blocks by text, messages by content)."""
    total = sum(count_tokens(str(t)) for t in captured["tools"] or [])
    sysv = captured["system"]
    if isinstance(sysv, str):
        total += _content_tokens(sysv)
    elif isinstance(sysv, list):
        total += sum(count_tokens(b.get("text", "")) for b in sysv
                     if isinstance(b, dict) and b.get("type") == "text")
    for msg in captured["messages_prefix"]:
        total += _content_tokens(msg.get("content") if isinstance(msg, dict) else None)
    return total


def _auto_prefix_tokens(captured: dict[str, Any]) -> int:
    """Local stable-prefix count for automatic-cache providers, mirroring
    apply_openai_cache's canonical-JSON stable-view arithmetic so the gate agrees
    with the engine's notion of the stable context."""
    view: dict[str, Any] = {"messages": captured["messages_prefix"]}
    if captured["tools"]:
        view["tools"] = captured["tools"]
    return count_tokens(json.dumps(view, sort_keys=True, separators=(",", ":"), default=str))


def _extract_auto_prefix(body: dict, provider: str, model: str,
                         upstream: str) -> Optional[WarmPrefix]:
    """Automatic-prefix-cache shape (OpenAI-compat): the cacheable prefix is the
    stable leading portion — every message before the final (volatile) turn, plus
    tools — captured for byte-identical replay. prompt_cache_key/_options ride in
    the payload when present (the replay must address the same cache entry), and
    the upstream endpoint rides along so the worker pings the provider this
    request actually used. Credentials are NEVER part of the payload or the hash."""
    caps = _AUTO_WARMABLE.get(provider)
    if caps is None:
        return None                # no cache, or a discount pings can never beat
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) < 2:
        return None                # no stable turn ahead of the volatile one
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    captured = {"tools": deepcopy(tools), "messages_prefix": deepcopy(messages[:-1])}
    prefix_tokens = _auto_prefix_tokens(captured)
    if prefix_tokens < caps["min_prefix_tokens"]:
        return None
    payload: dict[str, Any] = {
        "model": model,
        "tools": captured["tools"],
        "messages_prefix": captured["messages_prefix"],
        "upstream": upstream,      # exact chat endpoint this request used
    }
    for field in ("prompt_cache_key", "prompt_cache_options"):
        value = body.get(field)
        if value:
            payload[field] = deepcopy(value)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return WarmPrefix(
        provider=provider,
        model=model,
        prefix_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        prefix_tokens=prefix_tokens,
        provider_ttl_seconds=_auto_ttl_seconds(provider, model),
        payload=payload,
    )


def extract_warm_prefix(body: dict, provider: str, model: str,
                        headers: Mapping[str, str] | None,
                        meta: dict, upstream: str = "") -> Optional[WarmPrefix]:
    """Snapshot the cacheable prefix of one optimized request, or None when there is
    nothing worth warming: a provider where the warming math cannot work (see
    _AUTO_WARMABLE), no markers (Anthropic), no stable leading turns (automatic
    providers), or below the provider minimum (where the provider silently does
    not cache, so pings would buy nothing)."""
    if not isinstance(body, dict):
        return None
    meta = meta or {}
    if provider != "anthropic":
        return _extract_auto_prefix(body, provider, model, upstream)
    found = _last_marker(body)
    if found is None:
        return None
    position, marker = found
    captured = _capture_prefix(body, position)
    if meta.get("cache_control_owner") == "caller":
        # Caller-owned plans never went through apply_anthropic_cache, so meta carries
        # no token count — re-count the captured prefix with the local counter.
        prefix_tokens = _local_prefix_tokens(captured)
    else:
        prefix_tokens = int(meta.get("cached_prefix_tokens", 0) or 0)
    if prefix_tokens < anthropic_min_tokens(model):
        return None
    ttl = str(marker.get("ttl") or "")
    payload = {
        "model": model,
        "tools": captured["tools"],
        "system": captured["system"],
        "messages_prefix": captured["messages_prefix"],
        "ttl": ttl,                    # "" = default 5m tier; "1h" = long-TTL tier
        "vary": _vary_headers(headers),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return WarmPrefix(
        provider=provider,
        model=model,
        prefix_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        prefix_tokens=prefix_tokens,
        provider_ttl_seconds=_TTL_SECONDS.get(ttl, 300),
        payload=payload,
    )
