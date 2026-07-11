"""
Registry of what counts as an "LLM client" for the scanner.

This is the single source of truth the detector consults to decide whether a
constructor call (e.g. ``anthropic.Anthropic(...)``) or a method call
(e.g. ``client.messages.create(...)``) is an LLM API surface that Brevitas can
sit in front of. Adding support for a new provider should be a matter of
appending a :class:`ClientSpec` here — the detector and codemod read this
table and need no further changes.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClientSpec:
    """Describes one provider's client surface.

    Attributes:
        provider:       Canonical provider name used in reports and billing.
        module:         Import module the classes live in (``import openai``).
        sync_classes:   Constructor names that produce a *synchronous* client.
        async_classes:  Constructor names that produce an *asynchronous* client.
                        These are detected but never auto-wrapped — the current
                        wrappers are synchronous only.
        call_paths:     Attribute chains, relative to a client instance, that
                        represent an actual model call (used to confirm a client
                        is exercised and to count pipeline hops).
    """

    provider: str
    module: str
    sync_classes: tuple[str, ...]
    async_classes: tuple[str, ...]
    call_paths: tuple[tuple[str, ...], ...]


REGISTRY: tuple[ClientSpec, ...] = (
    ClientSpec(
        provider="anthropic",
        module="anthropic",
        sync_classes=("Anthropic", "AnthropicBedrock", "AnthropicVertex"),
        async_classes=("AsyncAnthropic", "AsyncAnthropicBedrock", "AsyncAnthropicVertex"),
        call_paths=(("messages", "create"), ("messages", "stream")),
    ),
    ClientSpec(
        provider="openai",
        module="openai",
        sync_classes=("OpenAI", "AzureOpenAI"),
        async_classes=("AsyncOpenAI", "AsyncAzureOpenAI"),
        call_paths=(("chat", "completions", "create"), ("responses", "create"),
                    ("embeddings", "create"), ("completions", "create")),
    ),
)


# ── Lookup helpers ────────────────────────────────────────────────────────────

# class name -> (provider, is_async). Built once at import time.
_CLASS_INDEX: dict[str, tuple[str, bool]] = {}
# module name -> ClientSpec, for resolving ``module.ClassName`` access.
_MODULE_INDEX: dict[str, ClientSpec] = {}
# (provider, is_async) lookup for a class within a known module.
for _spec in REGISTRY:
    _MODULE_INDEX[_spec.module] = _spec
    for _cls in _spec.sync_classes:
        _CLASS_INDEX[_cls] = (_spec.provider, False)
    for _cls in _spec.async_classes:
        _CLASS_INDEX[_cls] = (_spec.provider, True)


def classify_class(name: str) -> tuple[str, bool] | None:
    """Return ``(provider, is_async)`` for a bare class name, or ``None``."""
    return _CLASS_INDEX.get(name)


def classify_module_attr(module: str, attr: str) -> tuple[str, bool] | None:
    """Return ``(provider, is_async)`` for ``module.attr`` access, or ``None``."""
    spec = _MODULE_INDEX.get(module)
    if spec is None:
        return None
    if attr in spec.sync_classes:
        return spec.provider, False
    if attr in spec.async_classes:
        return spec.provider, True
    return None


def known_modules() -> frozenset[str]:
    return frozenset(_MODULE_INDEX)


def call_tails() -> frozenset[str]:
    """Final attribute names that indicate a model call (``create``/``stream``)."""
    return frozenset(path[-1] for spec in REGISTRY for path in spec.call_paths)
