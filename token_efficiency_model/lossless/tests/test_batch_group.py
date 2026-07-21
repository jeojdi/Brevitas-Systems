"""Batch prefix grouping (pathfinder gate, CR1) — scheduling semantics.
Deterministic; no network. asyncio scenarios driven via asyncio.run()."""
from __future__ import annotations

import asyncio

from token_efficiency_model.lossless.batch_group import BatchGroupGate

BIG = "shared corpus for the whole burst " * 300      # >> min_chars


def _body(question: str) -> dict:
    return {"messages": [{"role": "user", "content": BIG},
                         {"role": "user", "content": question}]}


def test_signature_same_prefix_same_sig_and_small_is_none():
    g = BatchGroupGate()
    s1 = g.signature(_body("q1"))
    s2 = g.signature(_body("q2 totally different question"))
    assert s1 and s1 == s2, "volatile FINAL message must not affect the signature"
    other = {"messages": [{"role": "user", "content": BIG + "x"},
                          {"role": "user", "content": "q"}]}
    assert g.signature(other) != s1, "different stable prefix -> different signature"
    assert g.signature(_body("q1"), namespace="tenant-a") != g.signature(
        _body("q1"), namespace="tenant-b"
    ), "identical prefixes from different tenants must never coordinate"
    assert g.signature({"messages": [{"role": "user", "content": "tiny"},
                                     {"role": "user", "content": "q"}]}) is None


def test_siblings_wait_for_pathfinder_then_run_free():
    async def scenario():
        g = BatchGroupGate(max_wait=5.0)
        sig = g.signature(_body("q"))
        order = []

        role0, _ = await g.acquire(sig)
        assert role0 == "pathfinder"

        async def sibling(name):
            role, waited = await g.acquire(sig)
            order.append((name, role))
            return waited

        tasks = [asyncio.create_task(sibling(f"s{i}")) for i in range(3)]
        await asyncio.sleep(0.05)
        assert order == [], "siblings must be HELD while pathfinder is in flight"
        g.release(sig, warm_ttl=60)                    # pathfinder prefill done
        waits = await asyncio.gather(*tasks)
        assert len(order) == 3 and all(r == "grouped" for _, r in order)
        assert all(w >= 0.04 for w in waits), "siblings actually waited"
        # signature now WARM: no holds, no new pathfinder
        role, waited = await g.acquire(sig)
        assert role == "free" and waited == 0.0
    asyncio.run(scenario())


def test_timeout_fails_open():
    async def scenario():
        g = BatchGroupGate(max_wait=0.1)
        sig = g.signature(_body("q"))
        role0, _ = await g.acquire(sig)
        assert role0 == "pathfinder"                   # never released (crash sim)
        role, waited = await g.acquire(sig)
        assert role == "grouped" and 0.09 <= waited < 1.0, \
            "sibling must proceed after max_wait even if pathfinder never releases"
    asyncio.run(scenario())


def test_warm_expiry_elects_new_pathfinder():
    async def scenario():
        g = BatchGroupGate()
        sig = g.signature(_body("q"))
        role, _ = await g.acquire(sig)
        g.release(sig, warm_ttl=0.01)                  # warm window expires immediately
        await asyncio.sleep(0.02)
        role2, _ = await g.acquire(sig)
        assert role2 == "pathfinder", "expired warm window -> next request re-warms"
        g.release(sig)
    asyncio.run(scenario())


def test_release_idempotent():
    async def scenario():
        g = BatchGroupGate()
        sig = g.signature(_body("q"))
        await g.acquire(sig)
        g.release(sig)
        g.release(sig)                                 # double release: harmless
    asyncio.run(scenario())


def test_signature_covers_stable_blocks_in_final_message():
    # Anthropic alternating-role constraint: big doc + question live in ONE message
    g = BatchGroupGate()
    def body(q):
        return {"messages": [{"role": "user",
                              "content": [{"type": "text", "text": BIG},
                                          {"type": "text", "text": q}]}]}
    s1, s2 = g.signature(body("q1")), g.signature(body("q2 different"))
    assert s1 and s1 == s2, "shared doc block must define the signature"
    other = {"messages": [{"role": "user",
                           "content": [{"type": "text", "text": BIG + "y"},
                                       {"type": "text", "text": "q"}]}]}
    assert g.signature(other) != s1
