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
import os
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from token_efficiency_model.lossless.provider_cache import (
    _content_tokens,
    anthropic_min_tokens,
    count_tokens,
    tokenizer_exact,
)

# Headers the cached prefix varies on. anthropic-beta rides here because the 1h
# TTL tier only exists under its beta flag — replaying without it would address a
# different cache entry. Credentials are NEVER part of the payload or the hash.
_VARY_HEADERS = ("anthropic-version", "anthropic-beta")

_TTL_SECONDS = {"1h": 3600, "": 300}

# Automatic-prefix-cache providers extraction supports, from the 2026-07 provider
# capability research. A provider earns a row ONLY where a keep-alive ping could
# even in principle pay: the cached-read price must be a deep discount (one warm
# return buys back many pings) AND the provider must at least describe a lifetime
# that staying in use extends. That is a CAPTURE screen, not a verdict that
# warming pays — both rows below currently fail the API enablement gate on
# measurement (openai: refresh-on-read undocumented; deepseek: pings measured net
# NEGATIVE, see the floors note further down and api/server.py
# _WARM_INACTIVE_REASONS). Everything else is measurement-only BY DESIGN —
# extraction returns None and the warming worker never sees it:
#   groq / fireworks   cached reads bill 0.50x input: a keep-alive ping costs
#                      exactly what a warm return saves — warming can NEVER pay;
#   perplexity         no cached-input discount exists at all — nothing to warm;
#   mistral / xai /    a real discount exists, but TTL and refresh-on-read are
#   together /         undocumented, so pings would be speculative spend against
#   openrouter         an unknown eviction policy (openrouter adds unverifiable
#                      pass-through routing on top).
#
# min_prefix_tokens is the provider's MINIMUM CACHEABLE PREFIX — below it the
# provider silently caches nothing (no error, just a full-price miss), so a ping
# has no entry to keep alive and is pure spend:
#   openai    1024, OpenAI's own documented automatic-cache floor.
#   deepseek  128, MEASURED — DeepSeek publishes no floor at all. It is NOT 64:
#             a 201-token prompt cached exactly 128 and stranded 73
#             (hit=128, miss=73); a 64-token block would have cached 192 and
#             stranded 9, so the block size — and therefore the one-block floor —
#             is 128 (docs/DEEPSEEK_CACHE_MAP.md, "Block size RESOLVED" + P2).
#             Corroborating: a 98-token prompt caches nothing, the first hit
#             appears at a 163-token prompt as exactly 128, and every hit ever
#             observed is a multiple of 128 (128/256/512/896/1792/1920). A prefix
#             of 64..127 tokens can therefore NEVER form a block, which is what
#             the old 64 authorized: captures that provably could not warm.
# These floors gate CAPTURE ONLY — whether a prefix is even shaped like something
# a ping could keep alive. Whether a customer may turn warming ON for a provider
# is a separate policy gate (api/server.py) and is decided on different evidence:
# DeepSeek's cache is automatic, write-free and still warm at 900 s untouched, so
# an idle prefix stays warm for free and a keep-alive ping is near-pure cost (a
# live n=36 A/B against native caching measured -1.27% incremental savings), which
# is ample reason for that policy to refuse DeepSeek enablement outright. The two
# gates are independent: a provider can be correctly captured and measured here
# while being correctly refused enablement there, and a floor of 64 was wrong for
# either purpose.
_AUTO_WARMABLE = {
    "openai":   {"min_prefix_tokens": 1024},
    "deepseek": {"min_prefix_tokens": 128},
}


def provider_warmable(provider: str) -> bool:
    """True when the provider's cache is shaped so a keep-alive ping COULD pay
    (Anthropic's marker cache, or a deep-discount automatic cache) — the
    capture/observation screen only, NOT the spend decision. Whether warming may
    actually be enabled is api/server.py's measurement-driven policy gate
    (_WARM_ACTIVE_PROVIDERS, anthropic-only: DeepSeek pings measured -1.27%
    incremental at n=36). Everything else stays measurement-only (see
    _AUTO_WARMABLE)."""
    return provider == "anthropic" or provider in _AUTO_WARMABLE


def _auto_ttl_seconds(provider: str, model: str) -> int:
    """Assumed cache lifetime for automatic-prefix providers. Deliberately the
    documented FLOOR, never an optimistic estimate — overestimating TTL means the
    entry evicts before the ping and the spend buys nothing."""
    if provider == "deepseek":
        # 4h = the low end of the only lifetime DeepSeek publishes ("a few hours to
        # a few days", KV-cache guide) — prose, so this is the floor of the
        # documented range, not a measured expiry. Our receipts put a hard MEASURED
        # floor of 900 s under it: an untouched entry was still fully warm at
        # 60/300/900 s (docs/DEEPSEEK_CACHE_MAP.md P3), and a prior probe saw
        # >55 min. The cliff was never found, so do NOT retune this down to 900:
        # 900 s is a lower bound on a LIVE entry, not an eviction time, and
        # assuming it would schedule ~16x more pings on the one provider whose
        # cache is already free and long-lived (i.e. where each extra ping is
        # closest to pure cost).
        return 14400
    if (model or "").lower().startswith("gpt-5.6"):
        return 1800                # explicit-mode "30m" is a documented minimum lifetime
    return 300                     # pre-5.6 automatic: 5-10min inactivity eviction floor


# --------------------------------------------------------------------------- #
# Chain-hash prefix keying (Phase 1.5, TASK A)
#
# prefix_hash is a sha256 of the WHOLE payload, which is exactly what makes it
# useless for containment: two conversations sharing 40k tokens of system prompt
# hash to two unrelated 64-hex strings. The chain is a SECOND, PARALLEL artifact
# -- vLLM-style block chaining -- where a shared leading prefix produces a shared
# leading DIGEST SEQUENCE, so containment survives without storing any content.
#
# It changes nothing about prefix_hash: the existing canonicalization (with its
# default=str escape hatch) is untouched byte-for-byte, and the chain uses a
# STRICT canonicalization of its own so a non-serializable element quarantines
# the chain (chain is None) rather than silently hashing a memory address into a
# structural key that other rows are meant to match.
# --------------------------------------------------------------------------- #
_CHAIN_SCHEME = 1
_CHAIN_BLOCK_BYTES = 4096
_CHAIN_MAX_BLOCKS = 300        # hard extraction cap; beyond -> truncated=True
_CHAIN_MAX_PATH_BLOCKS = 253   # ltree label budget: 2 header + 1 root + 253

# Cache-identity extras: request fields that fork the PROVIDER'S cache entry
# without appearing in the prefix elements. Two requests with an identical
# element sequence but a different tool_choice (anthropic) or prompt_cache_key
# (automatic providers) address different entries, so they must not share a
# chain root. Values are read post-injection -- whatever is on the body at
# extraction time is what the provider will see.
_CHAIN_EXTRAS = {
    "anthropic": ("tool_choice", "thinking", "speed", "output_config"),
    "openai": ("prompt_cache_key", "prompt_cache_options"),
    "deepseek": ("prompt_cache_key", "prompt_cache_options"),
}


@dataclass
class PrefixChain:
    """Block-chained structural key for one warm prefix.

    digests are UNSALTED: brevitas/ is the customer-installed package and must
    never hold the per-organization salt. The hosted observation boundary
    (api/server.py::_hosted_warm_observe) HMACs each u_i into the pseudonymous
    node digest that is actually stored.
    """
    scheme: int                 # _CHAIN_SCHEME
    digests: list[str]          # u_0..u_n, 64-hex each; u_0 is the seed node
    block_tokens: list[int]     # len n (per complete block; telescoping)
    block_elements: list[int]   # elements consumed by each block
    token_cum: list[int]        # cumulative tokens through block i (len n)
    tail_tokens: int            # prefix_tokens beyond the last complete block
    truncated: bool


@dataclass
class WarmPrefix:
    """Everything a warming worker needs to replay one cached prefix byte-identical."""
    provider: str
    model: str
    prefix_hash: str
    prefix_tokens: int
    provider_ttl_seconds: int
    payload: dict[str, Any]
    # Additive and optional: every existing constructor call keeps working, and
    # a None chain means "no structural key for this observation", never an error.
    chain: PrefixChain | None = None


def _chain_enabled() -> bool:
    """Observation, so default ON -- but a kill switch exists because the chain
    is the one part of extraction that walks every element a second time."""
    return os.getenv("BREVITAS_WARM_CHAIN", "1").strip().lower() not in {
        "0", "false", "no"}


def _canon_strict(obj: Any) -> bytes:
    """Canonical JSON with NO default= escape hatch.

    json.dumps(default=str) turns an un-encodable object into its repr, which
    for most objects embeds a memory address: the same request would then hash
    differently on every process, and two different requests could collide. The
    existing prefix_hash tolerates that (it is a whole-payload identity and the
    payload came off the wire as JSON anyway); a STRUCTURAL key that other rows
    are matched against cannot. TypeError propagates and the chain is dropped.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _chain_elements(provider: str, captured: dict[str, Any]) -> list[Any]:
    """The element sequence, in _capture_prefix's own serialization order:
    tools, then system (anthropic only; a str system is ONE element, a list
    contributes each block), then the captured message prefix."""
    elements: list[Any] = list(captured.get("tools") or [])
    if provider == "anthropic":
        sysv = captured.get("system")
        if isinstance(sysv, str):
            elements.append(sysv)
        elif isinstance(sysv, list):
            elements.extend(sysv)
    elements.extend(captured.get("messages_prefix") or [])
    return elements


def build_prefix_chain(body: dict, provider: str, model: str,
                       captured: dict, payload: dict, *,
                       prefix_tokens: int = 0) -> Optional[PrefixChain]:
    """Chain-hash the captured prefix, or None when no chain may be emitted.

    None -- never a partial or approximate chain -- when the kill switch is off,
    when tiktoken did not load (a heuristic tokenizer would write mis-weighted
    node rows, and wrong weights are worse than absent ones), or on ANY
    exception: the WarmPrefix still goes out, it simply carries no structural
    key. Library code, so nothing is logged from here.
    """
    if not _chain_enabled() or not tokenizer_exact():
        return None
    try:
        return _build_prefix_chain(body, provider, model, captured, payload,
                                   prefix_tokens=prefix_tokens)
    except Exception:
        return None


def _build_prefix_chain(body: dict, provider: str, model: str,
                        captured: dict, payload: dict, *,
                        prefix_tokens: int = 0) -> PrefixChain:
    """u_0 = sha256("BXP\\0" || canon(seed));
       u_i = sha256(u_{i-1} || u32be(i) || block_bytes_i).

    Blocks are ELEMENT-ALIGNED and BYTE-BUDGETED: a block closes at the first
    element boundary at or past 4096 framed bytes, so two requests sharing a
    leading element sequence partition identically and their digest sequences
    share the same prefix. Trailing elements that never reach the budget are the
    tail: no digest (a partial block would hash differently the moment the
    conversation grows), only tail_tokens.
    """
    seed = {
        "s": _CHAIN_SCHEME,
        "p": provider,
        "m": model,
        "t": payload.get("ttl", ""),     # anthropic tier; "" for auto providers
        "v": payload.get("vary", {}),    # anthropic _vary_headers; {} for auto
        "x": {key: body[key] for key in _CHAIN_EXTRAS.get(provider, ())
              if isinstance(body, dict) and key in body},
    }
    digests = [hashlib.sha256(b"BXP\x00" + _canon_strict(seed)).hexdigest()]
    block_tokens: list[int] = []
    block_elements: list[int] = []
    token_cum: list[int] = []
    truncated = False

    previous = bytes.fromhex(digests[0])
    pending = b""                    # framed bytes of the open block
    pending_text = ""                # its canonical text, for the token count
    consumed_text = ""               # canonical text of every CLOSED block
    pending_elements = 0
    for element in _chain_elements(provider, captured):
        canonical = _canon_strict(element)
        pending += len(canonical).to_bytes(4, "big") + canonical
        pending_text += canonical.decode("utf-8")
        pending_elements += 1
        if len(pending) < _CHAIN_BLOCK_BYTES:
            continue
        index = len(digests)         # 1-based block index, == len(digests) here
        digest = hashlib.sha256(
            previous + index.to_bytes(4, "big") + pending).hexdigest()
        digests.append(digest)
        previous = bytes.fromhex(digest)
        consumed_text += pending_text
        # Telescoping, and deliberately not per-block counting: tokenizers are
        # not additive across a concatenation boundary, so counting each block
        # in isolation would not sum to the whole. Counting the cumulative text
        # and differencing does, exactly.
        cumulative = count_tokens(consumed_text)
        block_tokens.append(cumulative - (token_cum[-1] if token_cum else 0))
        token_cum.append(cumulative)
        block_elements.append(pending_elements)
        pending = b""
        pending_text = ""
        pending_elements = 0
        if len(digests) - 1 >= _CHAIN_MAX_BLOCKS:
            truncated = True
            break
    return PrefixChain(
        scheme=_CHAIN_SCHEME,
        digests=digests,
        block_tokens=block_tokens,
        block_elements=block_elements,
        token_cum=token_cum,
        # The WarmPrefix's own prefix_tokens is authoritative (anthropic takes
        # it from the engine's meta), so the tail is what that count has beyond
        # the last closed block rather than a second, disagreeing measurement.
        tail_tokens=max(0, int(prefix_tokens) - (token_cum[-1] if token_cum else 0)),
        truncated=truncated,
    )


_warm_observer: Callable | None = None


def set_warm_observer(callback: Callable | None) -> None:
    """Install the hosted API's warm-prefix sink; local proxies leave it unset.

    Called as `(organization_id, customer_id, prefix, cache_read)`, or with a
    fifth `request_id` -- the metering id of the receipt this same request
    wrote -- when the callable's signature accepts one. Four-argument sinks
    keep working unchanged; the proxy inspects rather than probes, because a
    TypeError inside the delivery task is swallowed and would silently disable
    observation entirely.
    """
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
        chain=build_prefix_chain(body, provider, model, captured, payload,
                                 prefix_tokens=prefix_tokens),
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
        chain=build_prefix_chain(body, provider, model, captured, payload,
                                 prefix_tokens=prefix_tokens),
    )
