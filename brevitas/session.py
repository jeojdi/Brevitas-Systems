"""
Multi-hop session tracking.

Keeps a running record of every agent response so the next hop
can reference prior content and benefit from cross-hop deduplication.
"""
import secrets
import threading
import time
from collections import deque
from typing import Callable

from .resource_bounds import (
    MAX_CONTENT_RETENTION_S,
    ResourceBounds,
    ResourceLimitExceeded,
    clamp_int,
    utf8_size,
)


class BrevitasSession:
    def __init__(
        self,
        session_id: str = "",
        *,
        prior_ttl_s: int | None = None,
        max_prior_items: int | None = None,
        max_prior_bytes: int | None = None,
        max_prior_item_bytes: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        bounds = ResourceBounds.from_env()
        self.session_id = session_id or ("sess_" + secrets.token_urlsafe(12))
        if utf8_size(self.session_id) > 256:
            raise ResourceLimitExceeded("session_id exceeds 256 bytes")
        self.prior_ttl_s = clamp_int(
            bounds.session_content_ttl_s if prior_ttl_s is None else prior_ttl_s,
            minimum=1, maximum=MAX_CONTENT_RETENTION_S, name="session prior ttl",
        )
        self.max_prior_items = clamp_int(
            bounds.session_max_items if max_prior_items is None else max_prior_items,
            minimum=1, maximum=2_000, name="session max prior items",
        )
        self.max_prior_bytes = clamp_int(
            bounds.session_max_bytes if max_prior_bytes is None else max_prior_bytes,
            minimum=1, maximum=16 * 1024 * 1024, name="session max prior bytes",
        )
        self.max_prior_item_bytes = min(
            self.max_prior_bytes,
            clamp_int(
                bounds.session_max_item_bytes
                if max_prior_item_bytes is None else max_prior_item_bytes,
                minimum=1, maximum=4 * 1024 * 1024,
                name="session max prior item bytes",
            ),
        )
        self._clock = clock
        self._lock = threading.RLock()
        self._prior_content: deque[tuple[str, int, float]] = deque()
        self._prior_bytes = 0
        self.hop_count: int = 0
        # Quality of the most recent compression (from /v1/compress), forwarded
        # to /v1/usage so the billing quality gate has a signal to act on.
        self.last_quality: float | None = None

    def record_response(self, text: str) -> None:
        """Call after each agent response to build cross-hop context."""
        if not text:
            return
        if not isinstance(text, str):
            raise TypeError("session content must be text")
        size = utf8_size(text)
        if size > self.max_prior_item_bytes:
            raise ResourceLimitExceeded(
                f"session content exceeds {self.max_prior_item_bytes} bytes"
            )
        with self._lock:
            now = self._clock()
            self._cleanup_locked(now)
            while self._prior_content and (
                len(self._prior_content) >= self.max_prior_items
                or self._prior_bytes + size > self.max_prior_bytes
            ):
                _, old_size, _ = self._prior_content.popleft()
                self._prior_bytes -= old_size
            self._prior_content.append((text, size, now + self.prior_ttl_s))
            self._prior_bytes += size

    def _cleanup_locked(self, now: float) -> int:
        removed = 0
        while self._prior_content and self._prior_content[0][2] <= now:
            _, size, _ = self._prior_content.popleft()
            self._prior_bytes -= size
            removed += 1
        return removed

    def cleanup(self) -> int:
        with self._lock:
            return self._cleanup_locked(self._clock())

    def prior_context(self) -> list[str]:
        """Return all prior agent outputs as context for the next compression call."""
        with self._lock:
            self._cleanup_locked(self._clock())
            return [text for text, _, _ in self._prior_content]

    @property
    def retained_bytes(self) -> int:
        """Content-aware registry sizing hook; never returns the retained text."""
        with self._lock:
            self._cleanup_locked(self._clock())
            return self._prior_bytes

    def advance(self) -> None:
        with self._lock:
            self.hop_count = min(1_000_000_000, self.hop_count + 1)

    def reset(self) -> None:
        with self._lock:
            self._prior_content.clear()
            self._prior_bytes = 0
            self.hop_count = 0
            self.last_quality = None
