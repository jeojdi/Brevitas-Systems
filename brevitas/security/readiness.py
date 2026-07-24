"""Bounded, single-flight active readiness for an envelope cipher's KMS."""
from __future__ import annotations

import asyncio
import concurrent.futures
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .envelope import EnvelopeCipher


@dataclass(frozen=True)
class KMSReadinessResult:
    """Content-free readiness evidence for one process-local KMS probe."""

    ready: bool
    fresh: bool


class KMSReadinessMonitor:
    """Cache successful probes briefly and bound concurrent/hung SDK calls.

    A dedicated one-thread executor prevents readiness traffic from creating an
    unbounded wave of threads when a provider SDK call stalls. A timed-out call
    remains the sole in-flight probe and cannot publish a late success when its
    operation duration exceeded the configured deadline.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        executor: concurrent.futures.Executor | None = None,
    ) -> None:
        self._clock = clock
        self._executor = executor or concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="brevitas-kms-readiness"
        )
        self._owns_executor = executor is None
        self._lock = threading.Lock()
        self._cipher: EnvelopeCipher | None = None
        self._generation = 0
        self._inflight: concurrent.futures.Future[bool] | None = None
        self._last_result = False
        self._last_checked_at: float | None = None

    @staticmethod
    def _valid_bound(value: float) -> bool:
        return math.isfinite(value) and value > 0

    def reset(self) -> None:
        """Invalidate evidence after adapter/key reconfiguration."""
        with self._lock:
            self._generation += 1
            self._cipher = None
            self._inflight = None
            self._last_result = False
            self._last_checked_at = None

    def close(self) -> None:
        """Release a test/local monitor without waiting on an unhealthy SDK."""
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _run_probe(self, cipher: EnvelopeCipher, timeout_seconds: float) -> bool:
        started = self._clock()
        try:
            cipher.probe_kms()
            passed = True
        except Exception:
            passed = False
        finished = self._clock()
        elapsed = finished - started
        return bool(
            passed
            and math.isfinite(elapsed)
            and 0 <= elapsed <= timeout_seconds
        )

    def _publish(
        self,
        future: concurrent.futures.Future[bool],
        cipher: EnvelopeCipher,
        generation: int,
    ) -> None:
        try:
            result = future.result() is True
        except Exception:
            result = False
        checked_at = self._clock()
        with self._lock:
            if (
                generation != self._generation
                or cipher is not self._cipher
                or future is not self._inflight
            ):
                return
            self._last_result = result
            self._last_checked_at = checked_at
            self._inflight = None

    def _snapshot(self, cipher: EnvelopeCipher, max_age_seconds: float) -> KMSReadinessResult:
        now = self._clock()
        with self._lock:
            if cipher is not self._cipher or self._last_checked_at is None:
                return KMSReadinessResult(ready=False, fresh=False)
            age = now - self._last_checked_at
            fresh = bool(
                self._last_result
                and math.isfinite(age)
                and 0 <= age <= max_age_seconds
            )
            return KMSReadinessResult(ready=fresh, fresh=fresh)

    async def check(
        self,
        cipher: EnvelopeCipher,
        *,
        timeout_seconds: float,
        max_age_seconds: float,
    ) -> KMSReadinessResult:
        """Return fail-closed evidence, actively probing when success is stale."""
        if (
            not self._valid_bound(timeout_seconds)
            or not self._valid_bound(max_age_seconds)
        ):
            return KMSReadinessResult(ready=False, fresh=False)

        cached = self._snapshot(cipher, max_age_seconds)
        if cached.ready:
            return cached

        publish: tuple[
            concurrent.futures.Future[bool], EnvelopeCipher, int
        ] | None = None
        with self._lock:
            if cipher is not self._cipher:
                self._generation += 1
                self._cipher = cipher
                self._inflight = None
                self._last_result = False
                self._last_checked_at = None
            future = self._inflight
            if future is None:
                generation = self._generation
                future = self._executor.submit(
                    self._run_probe, cipher, timeout_seconds
                )
                self._inflight = future
                publish = (future, cipher, generation)

        # Register outside the state lock: Future invokes callbacks immediately
        # when a very fast local/test probe has already completed.
        if publish is not None:
            pending, pending_cipher, pending_generation = publish
            pending.add_done_callback(
                lambda completed: self._publish(
                    completed, pending_cipher, pending_generation
                )
            )

        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)),
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except (Exception, asyncio.TimeoutError):
            return KMSReadinessResult(ready=False, fresh=False)
        return self._snapshot(cipher, max_age_seconds)


__all__ = ["KMSReadinessMonitor", "KMSReadinessResult"]
