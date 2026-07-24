"""Deterministic lifecycle coverage for the public Brevitas SDK pool owner."""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from token_efficiency_model.lossless.dropin import BrevitasClient, BrevitasDropIn


class _SyncClient:
    def __init__(self, *, close_error: bool = False) -> None:
        self.close_calls = 0
        self.close_error = close_error

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error:
            raise RuntimeError("secret-bearing close failure")


class _AsyncClient:
    def __init__(self) -> None:
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1


def _install_provider_factories(monkeypatch):
    created = {"openai": [], "anthropic": []}

    def openai_factory(**_kwargs):
        client = _SyncClient()
        created["openai"].append(client)
        return client

    def anthropic_factory(**_kwargs):
        client = _SyncClient()
        created["anthropic"].append(client)
        return client

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=openai_factory))
    monkeypatch.setitem(
        sys.modules, "anthropic", SimpleNamespace(Anthropic=anthropic_factory))
    return created


def test_route_replacement_closes_prior_before_overwrite_and_final_close_once(monkeypatch):
    created = _install_provider_factories(monkeypatch)
    owner = BrevitasClient(api_key="not-logged")
    assert BrevitasClient is BrevitasDropIn

    first = owner._route_client("openai")
    assert owner._route_client("openai") is first
    assert first.close_calls == 0
    second = owner._route_client("anthropic")
    assert first.close_calls == 1
    assert owner._client is second
    assert created == {"openai": [first], "anthropic": [second]}

    owner.close()
    owner.close()
    assert first.close_calls == 1
    assert second.close_calls == 1
    with pytest.raises(RuntimeError, match="closed"):
        owner._route_client("openai")


def test_route_replacement_close_error_is_safe_and_does_not_block_new_owner(monkeypatch):
    created = _install_provider_factories(monkeypatch)
    owner = BrevitasDropIn(api_key="not-logged")
    prior = _SyncClient(close_error=True)
    prior.__provider__ = "openai"
    owner._client = prior
    owner._client_provider = "openai"

    replacement = owner._route_client("anthropic")
    assert prior.close_calls == 1
    assert replacement is created["anthropic"][0]
    owner.close()
    assert replacement.close_calls == 1


def test_sync_context_preserves_user_exception_and_swallows_close_failure():
    owner = BrevitasDropIn()
    underlying = _SyncClient(close_error=True)

    class UserFailure(Exception):
        pass

    with pytest.raises(UserFailure, match="original failure"):
        with owner:
            owner._client = underlying
            owner._client_provider = "openai"
            raise UserFailure("original failure")
    assert underlying.close_calls == 1
    owner.close()
    assert underlying.close_calls == 1


def test_concurrent_sync_close_is_thread_safe_and_exactly_once():
    owner = BrevitasDropIn()

    class SlowClient(_SyncClient):
        def close(self):
            self.close_calls += 1
            time.sleep(0.02)

    underlying = SlowClient()
    owner._client = underlying
    owner._client_provider = "openai"
    barrier = threading.Barrier(12)

    def close(_):
        barrier.wait()
        owner.close()

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(close, range(12)))
    assert underlying.close_calls == 1


def test_concurrent_route_replacements_leave_no_unclosed_or_double_closed_client(monkeypatch):
    created = _install_provider_factories(monkeypatch)
    owner = BrevitasDropIn(api_key="not-logged")
    barrier = threading.Barrier(10)

    def route(index):
        barrier.wait()
        owner._route_client("openai" if index % 2 else "anthropic")

    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(route, range(10)))
    clients = [*created["openai"], *created["anthropic"]]
    assert clients
    assert sum(client.close_calls == 0 for client in clients) == 1
    assert all(client.close_calls in {0, 1} for client in clients)
    owner.close()
    assert all(client.close_calls == 1 for client in clients)


def test_acquire_adopts_overridden_route_client_that_does_not_cache(monkeypatch):
    owner = BrevitasDropIn()
    underlying = _SyncClient()
    monkeypatch.setattr(owner, "_route_client", lambda _provider: underlying)

    assert owner._acquire_client("openai") is underlying
    assert owner._client is underlying
    assert owner._client_provider == "openai"
    assert owner._active_calls == 1

    owner._release_client()
    owner.close()
    assert underlying.close_calls == 1


def test_sync_close_can_finish_async_only_client_inside_running_loop():
    async def exercise():
        owner = BrevitasDropIn()
        underlying = _AsyncClient()
        owner._client = underlying
        owner._client_provider = "openai"
        owner.close()
        owner.close()
        return underlying

    underlying = asyncio.run(exercise())
    assert underlying.aclose_calls == 1


def test_async_context_prefers_aclose_and_is_idempotent():
    class Both(_SyncClient):
        def __init__(self):
            super().__init__()
            self.aclose_calls = 0

        async def aclose(self):
            self.aclose_calls += 1

    async def exercise():
        owner = BrevitasDropIn()
        underlying = Both()
        async with owner:
            owner._client = underlying
            owner._client_provider = "openai"
        await owner.aclose()
        return underlying

    underlying = asyncio.run(exercise())
    assert underlying.aclose_calls == 1
    assert underlying.close_calls == 0


def test_cancelled_aclose_finishes_cleanup_without_leaking_task():
    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowAsync:
            def __init__(self):
                self.calls = 0

            async def aclose(self):
                started.set()
                await release.wait()
                self.calls += 1

        owner = BrevitasDropIn()
        underlying = SlowAsync()
        owner._client = underlying
        owner._client_provider = "openai"
        closing = asyncio.create_task(owner.aclose())
        await started.wait()
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await owner.aclose()
        pending_cleanup = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name() == "brevitas-client-aclose"
            and not task.done()
        ]
        return underlying, pending_cleanup

    underlying, pending = asyncio.run(exercise())
    assert underlying.calls == 1
    assert pending == []
