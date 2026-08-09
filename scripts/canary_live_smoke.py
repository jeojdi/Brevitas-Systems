#!/usr/bin/env python3
"""One real TTL-canary write/probe pair per provider, against live endpoints.

MANUAL ONLY. pytest never references this file, the worker never calls it, and
it refuses to do anything unless BREVITAS_CANARY_LIVE_SMOKE=1 is set explicitly.
It exists because everything else about the canary is mocked, and a transport
that is only ever exercised against a mock is a transport nobody has tested.

It spends Brevitas's own probe keys (BREVITAS_PROBE_ANTHROPIC_KEY /
BREVITAS_PROBE_DEEPSEEK_KEY) against Brevitas's own accounts, with the daily
ledger cap pinned to $0.05 per provider, and hard-asserts that the total booked
across both providers came in under $0.10 before it exits.

    BREVITAS_CANARY_LIVE_SMOKE=1 .venv/bin/python scripts/canary_live_smoke.py

The gap is 30 seconds rather than a ladder rung, so a run takes about a minute
and the observation it records is a short-gap one -- almost certainly 'warm' on
both providers. The point is to prove the request shape, the receipt parse and
the ledger arithmetic work end to end, not to measure a TTL.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GAP_SECONDS = 30.0
PER_PROVIDER_CAP_USD = 0.05
TOTAL_CEILING_USD = 0.10


async def main() -> int:
    if os.getenv("BREVITAS_CANARY_LIVE_SMOKE", "") != "1":
        print("refusing to spend: set BREVITAS_CANARY_LIVE_SMOKE=1 to run")
        return 2
    # Imported here so the guard above runs before the worker module (and its
    # store, its provider pool and its metrics) is constructed.
    from brevitas.receipts import calculate_costs, normalize_usage

    from api import worker
    from api.store import warm_model_class

    booked = 0.0
    for provider in worker._CANARY_PROVIDERS:
        key = worker._canary_key(provider)
        if not key:
            print(f"{provider}: no probe key in the environment, skipped")
            continue
        model = worker._canary_model(provider)
        day = datetime.now(timezone.utc).date().isoformat()
        ttl_seconds, ttl_tier = worker._CANARY_TTL[provider]
        seed = f"smoke:{provider}:{int(time.time())}"
        prefix, prefix_tokens = worker._canary_prefix(provider, seed)
        estimate = worker._canary_estimate_usd(provider, model, prefix_tokens)
        if estimate is None:
            print(f"{provider}: {model} is unpriced, skipped")
            continue

        for leg in ("write", "probe"):
            reservation = await asyncio.to_thread(
                worker._store.warm_canary_reserve, provider, day, estimate,
                PER_PROVIDER_CAP_USD)
            if not reservation.get("allowed"):
                print(f"{provider} {leg}: daily cap denied the reservation")
                break
            status, data = await worker._canary_send(provider, model, prefix)
            receipt = normalize_usage(data.get("usage"), provider)
            costs = calculate_costs(provider, model, receipt.input_tokens, receipt)
            actual = (float(costs.get("actual_cost_usd") or 0.0)
                      if str(costs.get("pricing_status")) == "priced" else estimate)
            await asyncio.to_thread(worker._store.warm_canary_settle, provider,
                                    day, estimate, actual)
            booked += actual
            outcome = worker._warm_ttl_outcome(receipt)
            print(f"{provider} {leg}: status={status} tokens={receipt.input_tokens} "
                  f"cached={getattr(receipt, 'cached_input_tokens', 0)} "
                  f"write={getattr(receipt, 'cache_write_tokens', 0)} "
                  f"outcome={outcome} cost=${actual:.6f}")
            if leg == "write":
                print(f"  waiting {GAP_SECONDS:.0f}s before the probe leg")
                await asyncio.sleep(GAP_SECONDS)
            elif outcome:
                await asyncio.to_thread(
                    worker._store.warm_ttl_observe, provider,
                    warm_model_class(model), ttl_tier, GAP_SECONDS, outcome,
                    "canary")
                print(f"  recorded warm_ttl_observations({outcome}, canary)")

    print(f"total booked: ${booked:.6f}")
    assert booked < TOTAL_CEILING_USD, (
        f"live smoke booked ${booked:.6f}, over the ${TOTAL_CEILING_USD} ceiling")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
