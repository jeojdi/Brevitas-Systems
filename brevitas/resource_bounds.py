"""Shared, finite resource policies for process and durable state.

Every value is positive and capped at a repository-owned absolute maximum.  An
operator may tune within those limits, but cannot accidentally select an
unbounded cache, registry, session, Redis stream, or queued artifact.
"""
from __future__ import annotations

import json
import os
import asyncio
import inspect
import threading
import time
from copy import deepcopy
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Generic, Iterator, Mapping, TypeVar


K = TypeVar("K")
V = TypeVar("V")

ONE_HOUR_S = 3_600
MAX_CONTENT_RETENTION_S = 24 * ONE_HOUR_S


class ResourceBoundError(ValueError):
    """Base class for invalid policies and values that exceed a finite bound."""


class ResourceLimitExceeded(ResourceBoundError):
    """Raised before an oversized value is encrypted, retained, or queued."""


class ResourceConfigurationError(ResourceBoundError):
    """Raised when an environment value is not an integer."""


def _settle_awaitable(result: object) -> None:
    if not inspect.isawaitable(result):
        return

    async def wait() -> None:
        await result  # type: ignore[misc]

    def run() -> None:
        try:
            asyncio.run(wait())
        except (Exception, asyncio.CancelledError):
            pass

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        run()
        return
    thread = threading.Thread(target=run, name="brevitas-resource-finalizer", daemon=False)
    thread.start()
    thread.join()


def safe_close_resource(resource: object) -> None:
    """Close one sync/async owner without leaking tasks or exception content."""
    try:
        close = getattr(resource, "close", None)
        if callable(close):
            _settle_awaitable(close())
            return
        aclose = getattr(resource, "aclose", None)
        if callable(aclose):
            _settle_awaitable(aclose())
    except (Exception, asyncio.CancelledError):
        pass


def _invoke_finalizer(callback: Callable[[V], object], value: V) -> None:
    def invoke() -> None:
        try:
            _settle_awaitable(callback(value))
        except (Exception, asyncio.CancelledError):
            pass

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        invoke()
        return
    # Invoke the callback in the worker too. An async callback may create tasks;
    # they must bind to the worker's temporary loop, never escape on the caller's.
    thread = threading.Thread(
        target=invoke, name="brevitas-resource-finalizer", daemon=False
    )
    thread.start()
    thread.join()


def clamp_int(value: object, *, minimum: int, maximum: int, name: str) -> int:
    """Parse and clamp an integer to an explicitly finite positive interval."""
    if minimum < 1 or maximum < minimum:
        raise ResourceConfigurationError(f"invalid repository bounds for {name}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ResourceConfigurationError(f"{name} must be an integer") from exc
    return min(maximum, max(minimum, parsed))


def env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Read a finite integer environment setting; invalid text fails startup."""
    return clamp_int(os.getenv(name, str(default)), minimum=minimum,
                     maximum=maximum, name=name)


def serialized_size_bytes(value: object) -> int:
    """Return deterministic compact-JSON size without retaining the serialization."""
    try:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True,
                         ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ResourceLimitExceeded("value is not safely serializable") from exc
    return len(raw)


def utf8_size(value: object) -> int:
    return len(str(value).encode("utf-8"))


def require_size(value: object, maximum: int, *, name: str = "value",
                 sizer: Callable[[object], int] = serialized_size_bytes) -> int:
    maximum = clamp_int(maximum, minimum=1, maximum=1_073_741_824,
                        name=f"{name} maximum")
    size = sizer(value)
    if size > maximum:
        raise ResourceLimitExceeded(f"{name} exceeds {maximum} bytes")
    return size


@dataclass(frozen=True)
class ResourceBounds:
    """Repository defaults plus hard ceilings for all shared integration points."""

    request_max_bytes: int = 2 * 1024 * 1024
    request_max_items: int = 512
    semantic_cache_ttl_s: int = ONE_HOUR_S
    semantic_cache_max_entries: int = 10_000
    semantic_cache_max_entry_bytes: int = 1024 * 1024
    semantic_cache_candidate_limit: int = 256
    registry_ttl_s: int = ONE_HOUR_S
    registry_max_entries: int = 10_000
    registry_max_value_bytes: int = 2 * 1024 * 1024
    session_content_ttl_s: int = ONE_HOUR_S
    session_max_items: int = 128
    session_max_bytes: int = 2 * 1024 * 1024
    session_max_item_bytes: int = 256 * 1024
    redis_stream_max_entries: int = 100_000
    redis_stream_ttl_s: int = ONE_HOUR_S
    job_payload_ttl_s: int = ONE_HOUR_S
    job_result_ttl_s: int = ONE_HOUR_S
    job_max_payload_bytes: int = 1024 * 1024
    job_max_result_bytes: int = 2 * 1024 * 1024
    demo_session_ttl_s: int = ONE_HOUR_S
    demo_max_sessions: int = 100
    demo_max_session_bytes: int = 16 * 1024 * 1024
    demo_document_max_bytes: int = 8 * 1024 * 1024
    demo_history_max_items: int = 128
    demo_history_max_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        limits = {
            "request_max_bytes": (1024, 16 * 1024 * 1024),
            "request_max_items": (1, 2_000),
            "semantic_cache_ttl_s": (1, MAX_CONTENT_RETENTION_S),
            "semantic_cache_max_entries": (1, 1_000_000),
            "semantic_cache_max_entry_bytes": (1024, 8 * 1024 * 1024),
            "semantic_cache_candidate_limit": (1, 2_048),
            "registry_ttl_s": (1, MAX_CONTENT_RETENTION_S),
            "registry_max_entries": (1, 100_000),
            "registry_max_value_bytes": (1024, 16 * 1024 * 1024),
            "session_content_ttl_s": (1, MAX_CONTENT_RETENTION_S),
            "session_max_items": (1, 2_000),
            "session_max_bytes": (1024, 16 * 1024 * 1024),
            "session_max_item_bytes": (256, 4 * 1024 * 1024),
            "redis_stream_max_entries": (1, 1_000_000),
            "redis_stream_ttl_s": (1, MAX_CONTENT_RETENTION_S),
            "job_payload_ttl_s": (1, MAX_CONTENT_RETENTION_S),
            "job_result_ttl_s": (1, MAX_CONTENT_RETENTION_S),
            "job_max_payload_bytes": (1024, 8 * 1024 * 1024),
            "job_max_result_bytes": (1024, 16 * 1024 * 1024),
            "demo_session_ttl_s": (1, MAX_CONTENT_RETENTION_S),
            "demo_max_sessions": (1, 1_000),
            "demo_max_session_bytes": (1024, 64 * 1024 * 1024),
            "demo_document_max_bytes": (1024, 32 * 1024 * 1024),
            "demo_history_max_items": (1, 1_000),
            "demo_history_max_bytes": (1024, 16 * 1024 * 1024),
        }
        for name, (minimum, maximum) in limits.items():
            object.__setattr__(
                self, name,
                clamp_int(getattr(self, name), minimum=minimum, maximum=maximum,
                          name=name),
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ResourceBounds":
        source = os.environ if env is None else env

        def read(name: str, default: int, low: int, high: int) -> int:
            return clamp_int(source.get(name, str(default)), minimum=low,
                             maximum=high, name=name)

        return cls(
            request_max_bytes=read("BREVITAS_REQUEST_MAX_BYTES", 2 * 1024 * 1024,
                                   1024, 16 * 1024 * 1024),
            request_max_items=read("BREVITAS_REQUEST_MAX_ITEMS", 512, 1, 2_000),
            semantic_cache_ttl_s=read("BREVITAS_CACHE_TTL_SECONDS", ONE_HOUR_S,
                                      1, MAX_CONTENT_RETENTION_S),
            semantic_cache_max_entries=read("BREVITAS_CACHE_MAX_ENTRIES", 10_000,
                                            1, 1_000_000),
            semantic_cache_max_entry_bytes=read(
                "BREVITAS_CACHE_MAX_ENTRY_BYTES", 1024 * 1024, 1024, 8 * 1024 * 1024),
            semantic_cache_candidate_limit=read(
                "BREVITAS_CACHE_CANDIDATE_LIMIT", 256, 1, 2_048),
            registry_ttl_s=read("BREVITAS_REGISTRY_TTL_SECONDS", ONE_HOUR_S,
                                1, MAX_CONTENT_RETENTION_S),
            registry_max_entries=read("BREVITAS_REGISTRY_MAX_ENTRIES", 10_000,
                                      1, 100_000),
            registry_max_value_bytes=read("BREVITAS_REGISTRY_MAX_VALUE_BYTES",
                                          2 * 1024 * 1024, 1024, 16 * 1024 * 1024),
            session_content_ttl_s=read("BREVITAS_SESSION_TTL_SECONDS", ONE_HOUR_S,
                                       1, MAX_CONTENT_RETENTION_S),
            session_max_items=read("BREVITAS_SESSION_MAX_ITEMS", 128, 1, 2_000),
            session_max_bytes=read("BREVITAS_SESSION_MAX_BYTES", 2 * 1024 * 1024,
                                   1024, 16 * 1024 * 1024),
            session_max_item_bytes=read("BREVITAS_SESSION_MAX_ITEM_BYTES", 256 * 1024,
                                        256, 4 * 1024 * 1024),
            redis_stream_max_entries=read("BREVITAS_REDIS_STREAM_MAX_ENTRIES", 100_000,
                                          1, 1_000_000),
            redis_stream_ttl_s=read("BREVITAS_REDIS_STREAM_TTL_SECONDS", ONE_HOUR_S,
                                    1, MAX_CONTENT_RETENTION_S),
            job_payload_ttl_s=read("BREVITAS_JOB_PAYLOAD_TTL_SECONDS", ONE_HOUR_S,
                                   1, MAX_CONTENT_RETENTION_S),
            job_result_ttl_s=read("BREVITAS_JOB_RESULT_TTL_SECONDS", ONE_HOUR_S,
                                  1, MAX_CONTENT_RETENTION_S),
            job_max_payload_bytes=read("BREVITAS_JOB_MAX_PAYLOAD_BYTES", 1024 * 1024,
                                       1024, 8 * 1024 * 1024),
            job_max_result_bytes=read("BREVITAS_JOB_MAX_RESULT_BYTES", 2 * 1024 * 1024,
                                      1024, 16 * 1024 * 1024),
            demo_session_ttl_s=read("BREVITAS_DEMO_SESSION_TTL_SECONDS", ONE_HOUR_S,
                                    1, MAX_CONTENT_RETENTION_S),
            demo_max_sessions=read("BREVITAS_DEMO_MAX_SESSIONS", 100, 1, 1_000),
            demo_max_session_bytes=read("BREVITAS_DEMO_MAX_SESSION_BYTES",
                                        16 * 1024 * 1024, 1024, 64 * 1024 * 1024),
            demo_document_max_bytes=read("BREVITAS_DEMO_DOCUMENT_MAX_BYTES",
                                         8 * 1024 * 1024, 1024, 32 * 1024 * 1024),
            demo_history_max_items=read("BREVITAS_DEMO_HISTORY_MAX_ITEMS", 128,
                                        1, 1_000),
            demo_history_max_bytes=read("BREVITAS_DEMO_HISTORY_MAX_BYTES",
                                        2 * 1024 * 1024, 1024, 16 * 1024 * 1024),
        )


def extend_bounded_list(values: list[V], additions: list[V], *, max_items: int,
                        max_bytes: int,
                        sizer: Callable[[V], int] = serialized_size_bytes) -> None:
    """Append only pre-sized items, then evict oldest values to finite bounds."""
    item_limit = clamp_int(max_items, minimum=1, maximum=1_000_000,
                           name="list max_items")
    byte_limit = clamp_int(max_bytes, minimum=1, maximum=1_073_741_824,
                           name="list max_bytes")
    addition_sizes = [sizer(value) for value in additions]
    if any(size > byte_limit for size in addition_sizes):
        raise ResourceLimitExceeded(f"list item exceeds {byte_limit} bytes")
    values.extend(additions)
    sizes = [sizer(value) for value in values]
    total = sum(sizes)
    remove = 0
    while remove < len(values) and (
        len(values) - remove > item_limit or total > byte_limit
    ):
        total -= sizes[remove]
        remove += 1
    if remove:
        del values[:remove]


@dataclass
class _Entry(Generic[V]):
    value: V
    expires_at: float
    size: int


class BoundedTTLMap(Generic[K, V]):
    """Thread-safe TTL/LRU map with count, key, value, and aggregate byte limits.

    ``clock`` is injectable so expiry and concurrent eviction tests are exact.
    Least-recently-used entries are evicted first after expired entries.
    """

    def __init__(
        self,
        *,
        ttl_s: int | float,
        max_entries: int,
        max_value_bytes: int,
        max_total_bytes: int | None = None,
        max_key_bytes: int = 512,
        clock: Callable[[], float] = time.monotonic,
        sizer: Callable[[V], int] = serialized_size_bytes,
        copier: Callable[[V], V] = deepcopy,
        snapshotter: Callable[[V], V] | None = None,
        on_remove: Callable[[V], object] | None = None,
        resource_key: Callable[[V], object] | None = None,
    ) -> None:
        self.ttl_s = float(clamp_int(ttl_s, minimum=1,
                                     maximum=MAX_CONTENT_RETENTION_S, name="ttl_s"))
        self.max_entries = clamp_int(max_entries, minimum=1, maximum=1_000_000,
                                     name="max_entries")
        self.max_value_bytes = clamp_int(max_value_bytes, minimum=1,
                                         maximum=1_073_741_824,
                                         name="max_value_bytes")
        default_total = self.max_entries * self.max_value_bytes
        self.max_total_bytes = clamp_int(
            default_total if max_total_bytes is None else max_total_bytes,
            minimum=1, maximum=min(1_073_741_824, default_total),
            name="max_total_bytes",
        )
        self.max_key_bytes = clamp_int(max_key_bytes, minimum=1, maximum=65_536,
                                       name="max_key_bytes")
        self._clock = clock
        self._sizer = sizer
        self._copier = copier
        self._snapshotter = snapshotter or copier
        self._on_remove = on_remove
        self._resource_key = resource_key or (lambda value: value)
        self._entries: OrderedDict[K, _Entry[V]] = OrderedDict()
        self._total_bytes = 0
        self._lock = threading.RLock()
        self._pending_finalizers: set[int] = set()

    def _token(self, value: V) -> int:
        return id(self._resource_key(value))

    def _cleanup_locked(self, now: float) -> list[V]:
        expired = [key for key, entry in self._entries.items()
                   if entry.expires_at <= now]
        removed: list[V] = []
        for key in expired:
            entry = self._entries.pop(key)
            self._total_bytes -= entry.size
            removed.append(entry.value)
        return removed

    def _select_finalizers_locked(self, removed: list[V]) -> list[tuple[int, V]]:
        if self._on_remove is None or not removed:
            return []
        retained = {self._token(entry.value) for entry in self._entries.values()}
        selected: list[tuple[int, V]] = []
        for value in removed:
            token = self._token(value)
            if token in retained or token in self._pending_finalizers:
                continue
            self._pending_finalizers.add(token)
            selected.append((token, value))
        return selected

    def _run_finalizers(self, selected: list[tuple[int, V]]) -> None:
        for token, value in selected:
            try:
                if self._on_remove is not None:
                    _invoke_finalizer(self._on_remove, value)
            except (Exception, asyncio.CancelledError):
                pass
            finally:
                with self._lock:
                    self._pending_finalizers.discard(token)

    def cleanup(self) -> int:
        with self._lock:
            removed = self._cleanup_locked(self._clock())
            selected = self._select_finalizers_locked(removed)
        self._run_finalizers(selected)
        return len(removed)

    def put(self, key: K, value: V, *, ttl_s: int | float | None = None) -> None:
        if utf8_size(key) > self.max_key_bytes:
            raise ResourceLimitExceeded(f"key exceeds {self.max_key_bytes} bytes")
        owned = self._copier(value)
        size = self._sizer(owned)
        if size > self.max_value_bytes or size > self.max_total_bytes:
            raise ResourceLimitExceeded(
                f"value exceeds bounded map limit ({self.max_value_bytes} bytes)"
            )
        duration = self.ttl_s if ttl_s is None else float(clamp_int(
            ttl_s, minimum=1, maximum=int(self.ttl_s), name="entry ttl_s"))
        with self._lock:
            if self._token(owned) in self._pending_finalizers:
                raise ResourceLimitExceeded("resource is being finalized")
            now = self._clock()
            removed = self._cleanup_locked(now)
            old = self._entries.pop(key, None)
            if old is not None:
                self._total_bytes -= old.size
                removed.append(old.value)
            while self._entries and (
                len(self._entries) >= self.max_entries
                or self._total_bytes + size > self.max_total_bytes
            ):
                _, evicted = self._entries.popitem(last=False)
                self._total_bytes -= evicted.size
                removed.append(evicted.value)
            self._entries[key] = _Entry(value=owned, expires_at=now + duration, size=size)
            self._total_bytes += size
            selected = self._select_finalizers_locked(removed)
        self._run_finalizers(selected)

    def get(self, key: K, default: V | None = None) -> V | None:
        with self._lock:
            now = self._clock()
            removed = self._cleanup_locked(now)
            entry = self._entries.get(key)
            if entry is None:
                result = default
            else:
                self._entries.move_to_end(key)
                result = self._snapshotter(entry.value)
            selected = self._select_finalizers_locked(removed)
        self._run_finalizers(selected)
        return result

    def get_or_create(self, key: K, factory: Callable[[], V], *,
                      ttl_s: int | float | None = None) -> V:
        with self._lock:
            now = self._clock()
            removed = self._cleanup_locked(now)
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                result = self._snapshotter(entry.value)
            else:
                if utf8_size(key) > self.max_key_bytes:
                    raise ResourceLimitExceeded(f"key exceeds {self.max_key_bytes} bytes")
                owned = self._copier(factory())
                if self._token(owned) in self._pending_finalizers:
                    raise ResourceLimitExceeded("resource is being finalized")
                size = self._sizer(owned)
                if size > self.max_value_bytes or size > self.max_total_bytes:
                    raise ResourceLimitExceeded(
                        f"value exceeds bounded map limit ({self.max_value_bytes} bytes)"
                    )
                duration = self.ttl_s if ttl_s is None else float(clamp_int(
                    ttl_s, minimum=1, maximum=int(self.ttl_s), name="entry ttl_s"))
                while self._entries and (
                    len(self._entries) >= self.max_entries
                    or self._total_bytes + size > self.max_total_bytes
                ):
                    _, evicted = self._entries.popitem(last=False)
                    self._total_bytes -= evicted.size
                    removed.append(evicted.value)
                self._entries[key] = _Entry(owned, now + duration, size)
                self._total_bytes += size
                result = self._snapshotter(owned)
            selected = self._select_finalizers_locked(removed)
        self._run_finalizers(selected)
        return result

    def mutate(
        self,
        key: K,
        mutator: Callable[[V], V | None],
        *,
        ttl_s: int | float | None = None,
        copier: Callable[[V], V] | None = None,
    ) -> V | None:
        """Copy, mutate, remeasure, then atomically commit one existing value.

        The original remains untouched if copying, mutation, or sizing fails. The
        lock also serializes concurrent updates to the same process registry.
        """
        with self._lock:
            now = self._clock()
            removed = self._cleanup_locked(now)
            original = self._entries.get(key)
            if original is None:
                selected = self._select_finalizers_locked(removed)
                result = None
            else:
                copy_value = copier or self._copier
                candidate = copy_value(original.value)
                replacement = mutator(candidate)
                if replacement is not None:
                    candidate = replacement
                # The mutator may retain its candidate reference. Copy once more so
                # only the map owns the committed object.
                owned_candidate = self._copier(candidate)
                if self._token(owned_candidate) in self._pending_finalizers:
                    raise ResourceLimitExceeded("resource is being finalized")
                size = self._sizer(owned_candidate)
                if size > self.max_value_bytes or size > self.max_total_bytes:
                    raise ResourceLimitExceeded(
                        f"value exceeds bounded map limit ({self.max_value_bytes} bytes)"
                    )
                duration = self.ttl_s if ttl_s is None else float(clamp_int(
                    ttl_s, minimum=1, maximum=int(self.ttl_s), name="entry ttl_s"))

                # Determine all evictions without touching live state. The current key
                # is protected; a rejected candidate therefore has no side effects.
                total = self._total_bytes - original.size + size
                evictions: list[K] = []
                for candidate_key, entry in self._entries.items():
                    if total <= self.max_total_bytes:
                        break
                    if candidate_key == key:
                        continue
                    evictions.append(candidate_key)
                    total -= entry.size
                if total > self.max_total_bytes:
                    raise ResourceLimitExceeded(
                        "mutation cannot fit within aggregate byte limit"
                    )

                for evicted_key in evictions:
                    evicted = self._entries.pop(evicted_key)
                    self._total_bytes -= evicted.size
                    removed.append(evicted.value)
                self._entries.pop(key)
                self._total_bytes -= original.size
                removed.append(original.value)
                self._entries[key] = _Entry(
                    value=owned_candidate, expires_at=now + duration, size=size
                )
                self._total_bytes += size
                result = self._snapshotter(owned_candidate)
                selected = self._select_finalizers_locked(removed)
        self._run_finalizers(selected)
        return result

    def pop(self, key: K, default: V | None = None) -> V | None:
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                result = default
                selected = []
            else:
                self._total_bytes -= entry.size
                result = self._snapshotter(entry.value)
                selected = self._select_finalizers_locked([entry.value])
        self._run_finalizers(selected)
        return result

    def discard(self, key: K) -> bool:
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return False
            self._total_bytes -= entry.size
            selected = self._select_finalizers_locked([entry.value])
        self._run_finalizers(selected)
        return True

    def clear(self) -> None:
        with self._lock:
            removed = [entry.value for entry in self._entries.values()]
            self._entries.clear()
            self._total_bytes = 0
            selected = self._select_finalizers_locked(removed)
        self._run_finalizers(selected)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            removed = self._cleanup_locked(self._clock())
            result = key in self._entries
            selected = self._select_finalizers_locked(removed)
        self._run_finalizers(selected)
        return result

    def __len__(self) -> int:
        with self._lock:
            removed = self._cleanup_locked(self._clock())
            result = len(self._entries)
            selected = self._select_finalizers_locked(removed)
        self._run_finalizers(selected)
        return result

    @property
    def total_bytes(self) -> int:
        with self._lock:
            removed = self._cleanup_locked(self._clock())
            result = self._total_bytes
            selected = self._select_finalizers_locked(removed)
        self._run_finalizers(selected)
        return result

    def items(self) -> Iterator[tuple[K, V]]:
        with self._lock:
            removed = self._cleanup_locked(self._clock())
            snapshot = [
                (key, self._snapshotter(entry.value))
                for key, entry in self._entries.items()
            ]
            selected = self._select_finalizers_locked(removed)
        self._run_finalizers(selected)
        return iter(snapshot)
