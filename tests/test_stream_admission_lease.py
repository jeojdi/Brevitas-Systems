import asyncio
from types import SimpleNamespace

import pytest

import api.server as server


class _Lease:
    def __init__(self, *renewals):
        self._limiter = SimpleNamespace(policy=SimpleNamespace(lease_seconds=60))
        self._renewals = list(renewals)
        self.renewals = 0

    async def renew(self):
        self.renewals += 1
        renewal = self._renewals.pop(0)
        if isinstance(renewal, BaseException):
            raise renewal
        return renewal


@pytest.mark.parametrize("renewal", [False, TimeoutError("redis unavailable")])
def test_slow_stream_is_canceled_when_renewal_cannot_prove_ownership(
    monkeypatch, renewal,
):
    monkeypatch.setattr(server, "_admission_renewal_interval", lambda _lease: 0.001)

    async def exercise():
        cancellation = server.threading.Event()
        stream_closed = asyncio.Event()
        keep_working = asyncio.Event()
        work_after_loss = []
        releases = 0

        async def slow_stream():
            try:
                yield b"first"
                await keep_working.wait()
                work_after_loss.append(True)
                yield b"must-not-be-exposed"
            finally:
                stream_closed.set()

        async def release():
            nonlocal releases
            releases += 1

        lease = _Lease(True, renewal)
        chunks = [
            chunk async for chunk in server._lease_guarded_body_iterator(
                slow_stream(), lease, release, cancellation,
            )
        ]
        assert chunks == [b"first"]
        assert cancellation.is_set()
        assert stream_closed.is_set()
        assert work_after_loss == []
        assert lease.renewals == 2
        assert releases == 1

    asyncio.run(exercise())
