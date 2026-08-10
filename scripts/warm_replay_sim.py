#!/usr/bin/env python3
"""Trace-replay simulator + hindsight oracle for cache warming (Phase 0, item 6).

Standalone analytics tooling. Imports nothing from the server, touches no
database, changes no live behavior: it reads an arrival trace (or generates a
synthetic one), replays a candidate warming policy against provider cache
physics, and reports exact net dollars per policy as a fraction of the
hindsight-optimal schedule.

WHY THIS IS EXACT, NOT AN ESTIMATE
----------------------------------
Arrivals are exogenous to warming: whether we ping or not, the customer's next
request lands when it lands. That makes counterfactual replay exact rather than
an off-policy estimate -- given a trace, TTL physics and a price sheet, the
dollar outcome of *any* ping schedule is a deterministic function. It also makes
the hindsight optimum computable, which is the promotion currency (Baleen's
methodology, docs/RL_PREDICTIVE_WARMING_PLAN.md Part 5).

THE MONEY MODEL
---------------
For a prefix of N tokens at input price `price` $/Mtok, let P = N/1e6 * price.
  * cold touch (cache write)  costs  write_multiplier * P
  * warm touch (cache read)   costs  read_cost_fraction * P
An *arrival* touches the cache either way and pays whichever applies. A *ping*
is a touch we choose to pay for: read-priced when the sim cache is still warm at
ping time, write-priced when the chain has lapsed. Every touch (arrival or ping)
refreshes the entry to now + TTL.

  net(policy) = incremental_savings(policy) - ping_cost(policy)

where incremental savings counts only arrivals that landed warm under the policy
AND would have landed cold under never-warm. That "would have been cold" clause
is the whole point: organic warmth from the customer's own traffic is not ours
to claim, and billing it would be exactly the overclaim invariant 1 forbids.
Savings are non-negative by construction because pings only ever refresh an
entry -- warmth is monotone in touches, so policy-warm arrivals are a superset
of baseline-warm arrivals.

CACHE KEY GRANULARITY (the SUTVA point)
---------------------------------------
Provider caches are scoped to the *org's* provider key, so the simulated cache
lives at (org, provider, prefix_hash) and siblings sharing a prefix keep each
other warm for free. Policy bookkeeping lives at (org, customer, provider,
prefix_hash), which is the real `warm_prefixes` primary key. Collapsing these
two granularities into one is the modelling error that makes per-customer
holdouts read near-zero lift; keeping them apart is deliberate.

Pure stdlib. `--selftest` runs the synthetic scenarios with asserted invariants.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

SCHEMA = "brevitas.warm-replay-sim.v1"

# Provider physics. Defaults mirror api/worker.py WARM_PROVIDER_SPECS
# (ttl_seconds, read_cost_fraction) and brevitas/receipts.py MODEL_PRICES
# (input / cached / write for claude-sonnet-5 and deepseek-chat):
#   anthropic  3.0 input, 0.3 cached (0.10x), 3.75 write (1.25x), 300s TTL
#   deepseek   0.14 input, 0.0028 cached (0.02x), 0.14 write (1.00x), 14400s
DEFAULT_PHYSICS: dict[str, dict[str, float]] = {
    "anthropic": {
        "ttl_seconds": 300.0,
        "read_cost_fraction": 0.10,
        "write_multiplier": 1.25,
        "input_price_per_mtok": 3.0,
    },
    "deepseek": {
        "ttl_seconds": 14_400.0,
        "read_cost_fraction": 0.02,
        "write_multiplier": 1.00,
        "input_price_per_mtok": 0.14,
    },
}

# v1 knob defaults, from api/worker.py _warm_claim_kwargs().
V1_DEFAULTS: dict[str, float] = {
    "reserve_usd_per_mtok": 3.75,
    "roi_break_even_p": 0.11,
    "roi_min_arrivals": 5,
    "roi_min_p": 0.35,
    "stop_loss": 3,
    "max_gap_seconds": 3600,
    "safety_margin_seconds": 60,
    "claim_limit": 500,
    "tick_seconds": 60,
    "max_pings_per_customer_day": 288,
    "daily_budget_usd": float("inf"),
    "prefix_ttl_days": 7,
}

# Below this the fraction-of-oracle ratio is unstable and is suppressed rather
# than reported (Part 5: "suppressed when the oracle denominator is below a
# floor"). Absolute net dollars are always reported alongside.
ORACLE_RATIO_FLOOR_USD = 0.01

_SEC_PER_DAY = 86_400.0


# --------------------------------------------------------------------------
# physics / trace types
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Physics:
    """Cache economics for one provider.

    `ttl_seconds` is modelled as a HARD deadline: an entry is warm for exactly
    TTL after its last touch, then gone. That is faithful for anthropic (a real
    5-minute TTL, refreshed by reads). It is an approximation for deepseek,
    whose automatic prefix cache is an eviction policy rather than a clock --
    entries persist "hours while in use" with no published guarantee. Treating
    it as a 4h hard TTL is the conservative reading (it under-credits organic
    warmth and so over-states what warming could buy), and the hindsight
    oracle's per-gap decomposition below depends on the hard-TTL assumption.
    Feed Plane G's `warm_ttl_observations` posterior in via --provider-ttl once
    it has enough mass to argue with.
    """

    provider: str
    ttl_seconds: float
    read_cost_fraction: float
    write_multiplier: float
    input_price_per_mtok: float

    def base_usd(self, tokens: int) -> float:
        """Undiscounted input cost of `tokens` prefix tokens."""
        return (float(tokens) / 1_000_000.0) * self.input_price_per_mtok

    def read_usd(self, tokens: int) -> float:
        return self.read_cost_fraction * self.base_usd(tokens)

    def write_usd(self, tokens: int) -> float:
        return self.write_multiplier * self.base_usd(tokens)

    def hit_savings_usd(self, tokens: int) -> float:
        """Dollars an arrival saves by landing warm instead of cold."""
        return self.write_usd(tokens) - self.read_usd(tokens)

    def break_even_p(self) -> float:
        """P(return) at which one ping per TTL window pays for itself under
        these physics: a ping costs f, a warm return saves (w - f)."""
        gain = self.write_multiplier - self.read_cost_fraction
        if gain <= 0:
            return 1.0
        return min(1.0, self.read_cost_fraction / gain)


@dataclass(frozen=True)
class Arrival:
    ts: float                 # epoch seconds, UTC
    org: str
    customer: str
    provider: str
    prefix_hash: str
    prefix_tokens: int
    model: str = ""

    @property
    def cache_key(self) -> tuple[str, str, str]:
        # Provider caches are org-key-scoped: customer is deliberately absent.
        return (self.org, self.provider, self.prefix_hash)

    @property
    def prefix_key(self) -> tuple[str, str, str, str]:
        # warm_prefixes primary key.
        return (self.org, self.customer, self.provider, self.prefix_hash)


def hour_of_week(ts: float) -> str:
    """168-bucket hour-of-week key, byte-identical to api/store.py
    _utc_hour_bucket (which mirrors the Postgres isodow arithmetic)."""
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return str((dt.isoweekday() - 1) * 24 + dt.hour)


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


# --------------------------------------------------------------------------
# trace loading
# --------------------------------------------------------------------------

_TS_KEYS = ("ts", "timestamp", "created_at", "observed_at", "time")
_ORG_KEYS = ("org", "organization_id", "org_id", "organization")
_CUSTOMER_KEYS = ("customer", "customer_id", "end_customer_id")
_PROVIDER_KEYS = ("provider", "upstream", "vendor")
_PREFIX_KEYS = ("prefix_hash", "warm_prefix_hash", "prefix")
_TOKEN_KEYS = ("prefix_tokens", "cached_tokens", "cache_read_tokens",
               "baseline_tokens", "input_tokens")
_MODEL_KEYS = ("model", "model_name")


def _pick(row: dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def parse_ts(value: Any) -> float:
    """Accept epoch seconds or ISO-8601 (naive treated as UTC)."""
    if isinstance(value, bool):
        raise ValueError("timestamp is not a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError("timestamp is empty")
    try:
        return float(text)
    except ValueError:
        pass
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def arrival_from_row(row: dict[str, Any]) -> Arrival:
    ts = _pick(row, _TS_KEYS)
    if ts is None:
        raise ValueError(f"trace row has no timestamp column {_TS_KEYS}: {sorted(row)}")
    provider = str(_pick(row, _PROVIDER_KEYS) or "").strip().lower()
    if not provider:
        raise ValueError("trace row has no provider")
    tokens = _pick(row, _TOKEN_KEYS)
    if tokens is None:
        raise ValueError("trace row has no prefix token count")
    return Arrival(
        ts=parse_ts(ts),
        org=str(_pick(row, _ORG_KEYS) or "unattributed-org"),
        customer=str(_pick(row, _CUSTOMER_KEYS) or "unattributed-customer"),
        provider=provider,
        prefix_hash=str(_pick(row, _PREFIX_KEYS) or ""),
        prefix_tokens=int(float(tokens)),
        model=str(_pick(row, _MODEL_KEYS) or ""),
    )


def load_trace(path: str) -> list[Arrival]:
    """Load a JSON or CSV export of usage_log-shaped arrival rows.

    JSON may be a bare list, or an object with an "arrivals"/"rows"/"data" list.
    CSV must carry a header row. Column names are matched by alias so a raw
    usage_log export (organization_id / customer_id / ts / warm_prefix_hash)
    loads without a reshaping step.
    """
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        payload = json.loads(text)
        if isinstance(payload, dict):
            for key in ("arrivals", "rows", "data", "usage_log"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
            else:
                raise ValueError("JSON trace object has no arrivals/rows/data list")
        if not isinstance(payload, list):
            raise ValueError("JSON trace must be a list of arrival rows")
        rows: Iterable[dict[str, Any]] = payload
    else:
        rows = list(csv.DictReader(io.StringIO(text)))
    arrivals = [arrival_from_row(dict(row)) for row in rows]
    arrivals.sort(key=lambda a: a.ts)
    return arrivals


# --------------------------------------------------------------------------
# synthetic generators
# --------------------------------------------------------------------------

# Anchored on a Monday so hour-of-week buckets are readable in output.
_ANCHOR = datetime(2026, 8, 3, 9, 0, 0, tzinfo=timezone.utc).timestamp()


def scenario_cron(seed: int = 7) -> list[Arrival]:
    """A periodic cron-like customer: a batch job firing every 10 minutes for
    six hours against one stable 40k-token system prefix.

    The 600s period sits above anthropic's 300s TTL (so never-warm is cold on
    every arrival and there is real money on the table) and below v1's 3600s
    max_gap (so the candidate filter admits it). This is the scenario warming
    was built for; if v1 cannot make money here it cannot make money anywhere.
    """
    del seed  # deterministic
    return [
        Arrival(_ANCHOR + i * 600.0, "org-cron", "cust-cron", "anthropic",
                "p" * 8 + "cron", 40_000, "claude-sonnet-5")
        for i in range(36)
    ]


def scenario_bursty(seed: int = 11) -> list[Arrival]:
    """A bursty human on a 9-to-5: three weekdays, arrivals only between 09:00
    and 17:00 UTC with exponential inter-arrival gaps (mean 7.5 min), long dead
    nights in between.

    Exercises the mixed regime -- some gaps fall inside the TTL (organically
    warm, nothing for us to claim) and some outside (bridgeable) -- plus the
    overnight gap that poisons v1's EWMA.
    """
    rng = random.Random(seed)
    out: list[Arrival] = []
    for day in range(3):
        clock = _ANCHOR + day * _SEC_PER_DAY
        window_end = clock + 8 * 3600.0
        while clock < window_end:
            out.append(Arrival(clock, "org-burst", "cust-burst", "anthropic",
                               "b" * 8 + "urst", 28_000, "claude-sonnet-5"))
            clock += rng.expovariate(1.0 / 450.0)
    out.sort(key=lambda a: a.ts)
    return out


def scenario_churned(seed: int = 13) -> list[Arrival]:
    """A churned customer: two hours of steady 10-minute traffic, then nothing,
    ever again.

    The point of interest is the tail. v1 keeps pinging a dead prefix until
    consecutive_misses hits stop_loss -- real dollars spent against arrivals
    that never come -- while the oracle, knowing the trace ends, spends nothing
    after the last arrival.
    """
    del seed
    return [
        Arrival(_ANCHOR + i * 600.0, "org-churn", "cust-churn", "anthropic",
                "c" * 8 + "hurn", 32_000, "claude-sonnet-5")
        for i in range(12)
    ]


def _poisson_window(rng: random.Random, start: float, end: float,
                    mean_gap_s: float) -> list[float]:
    """Exponential inter-arrival timestamps inside [start, end)."""
    out: list[float] = []
    clock = start + rng.expovariate(1.0 / mean_gap_s)
    while clock < end:
        out.append(clock)
        clock += rng.expovariate(1.0 / mean_gap_s)
    return out


def scenario_company(seed: int = 17) -> list[Arrival]:
    """One organization, eight end customers, twenty-one simulated days.

    This is the scenario the LEARNED scheduler exists for, and the one that
    exposes what v1 does on a real trace rather than a six-hour demo. v1's
    ROI gate reads p_return = hour_histogram[bucket] / arrival_count -- a
    LIFETIME fraction. A customer active in K hour-of-week buckets averages
    1/K there no matter how dense their traffic is, so over three weeks
    almost every real customer falls under the 0.11 anthropic break-even and
    v1 simply stops warming. The hazard model divides by EXPOSURE instead,
    which is scale-free in trace length.

    The cast, all on anthropic physics, all in org-acme:
      cron-30m    a batch agent every 30 minutes, around the clock
      cron-2h     a slower batch agent every 2 hours
      us-day      a human, 09-17 UTC-5, weekdays, ~6 minute gaps
      eu-day      a human, 09-17 UTC+1, weekdays, ~7 minute gaps
      night-owl   a human, evenings only (UTC-8), every day, ~8 minute gaps
      power       a heavy bursty user: several dense bursts a day
      churned     a daytime human who leaves for good after day 7
      newcomer    a sparse customer who first appears on day 14

    us-day, eu-day and night-owl share one org-wide system prefix, so they
    land on the SAME provider cache key and keep each other warm for free.
    That is the SUTVA point the module docstring makes, made load-bearing:
    a policy that bills each of them for the others' warmth is overclaiming.
    """
    rng = random.Random(seed)
    day = _SEC_PER_DAY
    # _ANCHOR is a Monday 09:00 UTC; back up to that Monday's midnight so the
    # weekday arithmetic below is readable.
    start = _ANCHOR - 9 * 3600.0
    out: list[Arrival] = []
    org = "org-acme"
    shared = "sysprompt-acme"

    def add(ts: float, customer: str, prefix: str, tokens: int) -> None:
        out.append(Arrival(ts, org, customer, "anthropic", prefix, tokens,
                           "claude-sonnet-5"))

    # -- two cron agents, around the clock ---------------------------------
    for i in range(int(21 * day / 1800.0)):
        add(start + i * 1800.0, "cron-30m", "p-cron30", 3_200)
    for i in range(int(21 * day / 7200.0)):
        add(start + i * 7200.0, "cron-2h", "p-cron2h", 2_400)

    # -- three humans in three time zones, on one shared org prefix --------
    humans = (
        # (customer, utc window start hour, window hours, mean gap, weekdays only)
        ("us-day", 14.0, 8.0, 360.0, True),     # 09-17 at UTC-5
        ("eu-day", 8.0, 8.0, 420.0, True),      # 09-17 at UTC+1
        ("night-owl", 3.0, 4.0, 480.0, False),  # 19-23 at UTC-8
    )
    for customer, hour0, span, gap, weekdays_only in humans:
        for d in range(21):
            if weekdays_only and datetime.fromtimestamp(
                    start + d * day, timezone.utc).isoweekday() > 5:
                continue
            base = start + d * day + hour0 * 3600.0
            for ts in _poisson_window(rng, base, base + span * 3600.0, gap):
                add(ts, customer, shared, 2_800)

    # -- the heavy bursty power user ---------------------------------------
    for d in range(21):
        for burst in range(rng.randint(2, 4)):
            base = start + d * day + rng.uniform(6.0, 21.0) * 3600.0
            for ts in _poisson_window(rng, base, base + rng.uniform(0.7, 2.0) * 3600.0,
                                      240.0):
                add(ts, "power", "p-power", 4_000)

    # -- the customer who churns after day 7 -------------------------------
    for d in range(7):
        base = start + d * day + 15.0 * 3600.0
        for ts in _poisson_window(rng, base, base + 6.0 * 3600.0, 420.0):
            add(ts, "churned", "p-churn", 3_600)

    # -- the sparse newcomer, first seen on day 14 -------------------------
    for d in range(14, 21):
        base = start + d * day + 10.0 * 3600.0
        for ts in _poisson_window(rng, base, base + 5.0 * 3600.0, 1_500.0):
            add(ts, "newcomer", "p-new", 1_400)

    out.sort(key=lambda a: (a.ts, a.customer))
    return out


SCENARIOS: dict[str, Callable[[int], list[Arrival]]] = {
    "cron": scenario_cron,
    "bursty": scenario_bursty,
    "churned": scenario_churned,
    "company": scenario_company,
}


# --------------------------------------------------------------------------
# policies
# --------------------------------------------------------------------------

@dataclass
class PingRequest:
    """A ping the policy wants executed at `ts` against a prefix."""

    prefix_key: tuple[str, str, str, str]
    cache_key: tuple[str, str, str]
    provider: str
    customer: str
    prefix_tokens: int
    reserved_usd: float


class Policy:
    """Replay interface. The engine owns the cache and the clock; a policy only
    observes arrivals and proposes pings on tick boundaries."""

    name = "policy"

    def reset(self, cfg: "SimConfig", physics: dict[str, Physics]) -> None:
        raise NotImplementedError

    def on_arrival(self, arrival: Arrival, cache_read: bool) -> None:
        raise NotImplementedError

    def on_tick(self, now: float) -> list[PingRequest]:
        raise NotImplementedError

    def on_settle(self, req: PingRequest, now: float, spent_usd: float) -> None:
        raise NotImplementedError

    def next_due_at(self) -> float:
        """Earliest time any prefix could become claimable, or inf. Lets the
        engine skip tick grid points where nothing can happen; it never changes
        which grid points a claim may land on."""
        return float("inf")


class NeverWarmPolicy(Policy):
    """The counterfactual floor: observe everything, ping nothing."""

    name = "never-warm"

    def reset(self, cfg: "SimConfig", physics: dict[str, Physics]) -> None:
        return None

    def on_arrival(self, arrival: Arrival, cache_read: bool) -> None:
        return None

    def on_tick(self, now: float) -> list[PingRequest]:
        return []

    def on_settle(self, req: PingRequest, now: float, spent_usd: float) -> None:
        return None


@dataclass
class _PrefixRow:
    """One `warm_prefixes` row. Fields are the columns warm_due_claim and
    warm_prefix_observe actually read/write; nothing else is modelled."""

    org: str
    customer: str
    provider: str
    prefix_hash: str
    prefix_tokens: int
    arrival_count: int = 0
    ewma_interarrival_s: float | None = None
    hour_histogram: dict[str, int] = field(default_factory=dict)
    last_seen_at: float = 0.0
    next_due_at: float = 0.0
    expires_at: float = 0.0
    consecutive_misses: int = 0
    warm_pings: int = 0
    pings_today: int = 0
    pings_today_date: str = ""
    state: str = "active"
    ping_reserve_usd: float = 0.0

    @property
    def prefix_key(self) -> tuple[str, str, str, str]:
        return (self.org, self.customer, self.provider, self.prefix_hash)

    @property
    def cache_key(self) -> tuple[str, str, str]:
        return (self.org, self.provider, self.prefix_hash)


class V1HeuristicPolicy(Policy):
    """Faithful replay of shipped v1: warm_prefix_observe + warm_due_claim +
    warm_ping_settle (api/store.py, mirrored by migration 202607280003).

    One ping per (TTL - safety_margin) window per prefix, gated by:
      * candidate filter -- state active, next_due_at <= now, not expired,
        consecutive_misses < stop_loss, EWMA inter-arrival <= max_gap_seconds
      * ROI gate -- p_return = hour_histogram[bucket] / arrival_count must clear
        roi_min_p while arrival_count < roi_min_arrivals, and the provider's
        break-even probability thereafter
      * per-customer-day ping cap, then the reserve-then-settle daily budget

    Deliberately not modelled: the advisory lock and claim-token fencing (single
    simulated worker, so there is no concurrent claimant to fence) and the
    holdout coin (defaults to 0.0 and is a measurement arm, not a policy).
    """

    name = "v1heuristic"

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str, str], _PrefixRow] = {}
        self.cfg: SimConfig | None = None
        self.physics: dict[str, Physics] = {}
        # (org, provider, day) -> [reserved, spent]
        self.ledger: dict[tuple[str, str, str], list[float]] = defaultdict(
            lambda: [0.0, 0.0])
        self.decisions: dict[str, int] = defaultdict(int)

    def reset(self, cfg: "SimConfig", physics: dict[str, Physics]) -> None:
        self.rows = {}
        self.cfg = cfg
        self.physics = physics
        self.ledger = defaultdict(lambda: [0.0, 0.0])
        self.decisions = defaultdict(int)

    # -- observe -----------------------------------------------------------
    def on_arrival(self, arrival: Arrival, cache_read: bool) -> None:
        cfg = self.cfg
        assert cfg is not None
        phys = self.physics[arrival.provider]
        ttl = int(phys.ttl_seconds)
        horizon = max(1, ttl - cfg.safety_margin_seconds)
        bucket = hour_of_week(arrival.ts)
        row = self.rows.get(arrival.prefix_key)
        if row is None:
            row = _PrefixRow(
                org=arrival.org, customer=arrival.customer,
                provider=arrival.provider, prefix_hash=arrival.prefix_hash,
                prefix_tokens=arrival.prefix_tokens,
                arrival_count=1, ewma_interarrival_s=None,
                hour_histogram={bucket: 1}, last_seen_at=arrival.ts,
                next_due_at=arrival.ts + horizon,
                expires_at=arrival.ts + cfg.prefix_ttl_days * _SEC_PER_DAY,
                # Observer-priced worst case: a lapsed ping writes the cache.
                ping_reserve_usd=phys.write_usd(arrival.prefix_tokens))
            self.rows[arrival.prefix_key] = row
            return
        gap = arrival.ts - row.last_seen_at
        row.ewma_interarrival_s = (
            gap if row.ewma_interarrival_s is None
            else round(0.3 * gap + 0.7 * float(row.ewma_interarrival_s), 3))
        row.hour_histogram[bucket] = row.hour_histogram.get(bucket, 0) + 1
        row.arrival_count += 1
        row.prefix_tokens = arrival.prefix_tokens
        row.ping_reserve_usd = phys.write_usd(arrival.prefix_tokens)
        # An arrival that read the cache proves the pings are converting; a
        # miss leaves the stop-loss evidence standing.
        if cache_read:
            row.consecutive_misses = 0
        row.state = "active"
        row.last_seen_at = arrival.ts
        row.next_due_at = arrival.ts + horizon
        row.expires_at = arrival.ts + cfg.prefix_ttl_days * _SEC_PER_DAY

    # -- claim -------------------------------------------------------------
    def _eligible(self, row: _PrefixRow, now: float) -> bool:
        cfg = self.cfg
        assert cfg is not None
        return (row.state == "active"
                and row.expires_at > now
                and row.consecutive_misses < cfg.stop_loss
                and (row.ewma_interarrival_s is None
                     or row.ewma_interarrival_s <= cfg.max_gap_seconds))

    def next_due_at(self) -> float:
        """Earliest next_due_at over rows that could still be claimed.

        Only the time-invariant half of the candidate filter is applied here.
        The ROI gate depends on the hour bucket, so a row it keeps rejecting
        still proposes a tick every window -- exactly as the real worker
        re-scans and re-skips it -- and is retired only by expires_at, which
        on_tick prunes.
        """
        cfg = self.cfg
        if cfg is None:
            return float("inf")
        soonest = float("inf")
        for row in self.rows.values():
            if row.state != "active" or row.consecutive_misses >= cfg.stop_loss:
                continue
            if (row.ewma_interarrival_s is not None
                    and row.ewma_interarrival_s > cfg.max_gap_seconds):
                continue
            soonest = min(soonest, row.next_due_at)
        return soonest

    def on_tick(self, now: float) -> list[PingRequest]:
        cfg = self.cfg
        assert cfg is not None
        day = utc_day(now)
        bucket = hour_of_week(now)
        # warm_prefix_observe deletes expired rows on every write; the claim
        # filter also excludes them. Prune so next_due_at() can go to infinity.
        for key in [k for k, r in self.rows.items() if r.expires_at <= now]:
            del self.rows[key]
        candidates = sorted(
            (r for r in self.rows.values() if r.next_due_at <= now and self._eligible(r, now)),
            key=lambda r: (r.next_due_at, r.prefix_key),
        )[:cfg.claim_limit * 4]
        claimed: list[PingRequest] = []
        claimed_counts: dict[tuple[str, str, str], int] = defaultdict(int)
        for row in candidates:
            if len(claimed) >= cfg.claim_limit:
                break
            phys = self.physics[row.provider]
            p_return = min(1.0, row.hour_histogram.get(bucket, 0)
                           / max(row.arrival_count, 1))
            floor = (cfg.roi_min_p if row.arrival_count < cfg.roi_min_arrivals
                     else cfg.break_even_for(row.provider))
            reserve = max(
                row.ping_reserve_usd,
                round(cfg.reserve_usd_per_mtok * row.prefix_tokens / 1_000_000.0, 10))
            if p_return < floor:
                self.decisions["skipped_roi"] += 1
                continue
            cust_key = (row.org, row.customer, row.provider)
            pings_today = sum(
                r.pings_today for r in self.rows.values()
                if (r.org, r.customer, r.provider) == cust_key
                and r.pings_today_date == day)
            if pings_today + claimed_counts[cust_key] >= cfg.max_pings_per_customer_day:
                self.decisions["cap_denied"] += 1
                continue
            book = self.ledger[(row.org, row.provider, day)]
            if book[0] + book[1] + reserve > cfg.daily_budget_usd:
                self.decisions["budget_denied"] += 1
                continue
            book[0] += reserve
            claimed_counts[cust_key] += 1
            self.decisions["pinged"] += 1
            claimed.append(PingRequest(
                prefix_key=row.prefix_key, cache_key=row.cache_key,
                provider=row.provider, customer=row.customer,
                prefix_tokens=row.prefix_tokens, reserved_usd=reserve))
        return claimed

    # -- settle ------------------------------------------------------------
    def on_settle(self, req: PingRequest, now: float, spent_usd: float) -> None:
        cfg = self.cfg
        assert cfg is not None
        day = utc_day(now)
        book = self.ledger[(req.prefix_key[0], req.provider, day)]
        book[0] = max(0.0, book[0] - req.reserved_usd)
        book[1] += spent_usd
        row = self.rows.get(req.prefix_key)
        if row is None:
            return
        ttl = int(self.physics[req.provider].ttl_seconds)
        row.warm_pings += 1
        # Pre-charge convention: a ping is a miss until an arrival clears it.
        row.consecutive_misses += 1
        row.pings_today = row.pings_today + 1 if row.pings_today_date == day else 1
        row.pings_today_date = day
        row.next_due_at = now + max(1, ttl - cfg.safety_margin_seconds)


# --------------------------------------------------------------------------
# learned scheduler (Phase 1 semantics, learned ONLINE during the replay)
# --------------------------------------------------------------------------

# Constants are LITERALS on both sides of the port, exactly as the migration
# insists: a hazard posterior is only comparable across replicas if every
# writer decays it with the same half-life.
_T_HALF_S = 1_209_600.0          # 14 days
_E_CAP_HOURS = 484.8             # T_HALF / (3600 * ln 2): the exposure supremum
_MAX_WALK_HOURS = 672.0
_SPARSITY = 1e-6
_ORG_AGGREGATE = "\x00org-aggregate"   # stands in for the nil-uuid row
# BG/NBD hyperparameters, fixed: r = 0.5, alpha = 7 days, a = 1, b = 2.5.
_BG_R, _BG_ALPHA, _BG_B = 0.5, 7.0, 2.5
_INDEX_CLAMP = 1_000_000.0


@dataclass
class _HazardState:
    """One `warm_customer_state` row: decayed hour-of-week hazard sufficient
    statistics plus the raw BG/NBD counters."""

    hazard_n: dict[str, float] = field(default_factory=dict)
    hazard_e: dict[str, float] = field(default_factory=dict)
    events_total: int = 0
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0
    last_update_at: float = 0.0


def hazard_touch(state: _HazardState | None, ts: float) -> _HazardState:
    """Fold one arrival into the decayed maps. Port of
    public.warm_customer_state_touch (202608100003)."""
    bucket = hour_of_week(ts)
    if state is None:
        # A first arrival has NO exposure history: hazard_e stays empty, so
        # the shrinkage prior carries the whole estimate. That is the
        # cold-start story, and it is why this policy starts v1-equivalent.
        return _HazardState(hazard_n={bucket: 1.0}, hazard_e={}, events_total=1,
                            first_seen_at=ts, last_seen_at=ts, last_update_at=ts)

    # A clock that went backwards decays nothing rather than amplifying.
    delta = max(0.0, ts - state.last_update_at)
    decay = 0.5 ** (delta / _T_HALF_S)
    state.hazard_n = {k: round(v * decay, 12) for k, v in state.hazard_n.items()
                      if v * decay >= _SPARSITY}
    state.hazard_e = {k: round(v * decay, 12) for k, v in state.hazard_e.items()
                      if v * decay >= _SPARSITY}

    if delta / 3600.0 > _MAX_WALK_HOURS:
        # Too long to walk hour by hour; the decayed integral is uniform over
        # the week by then anyway.
        share = _E_CAP_HOURS / 168.0
        for index in range(168):
            key = str(index)
            state.hazard_e[key] = round(state.hazard_e.get(key, 0.0) + share, 12)
    else:
        cursor = state.last_update_at
        while cursor < ts:
            nxt = min(ts, math.floor(cursor / 3600.0) * 3600.0 + 3600.0)
            hours = (nxt - cursor) / 3600.0
            key = hour_of_week(cursor)
            state.hazard_e[key] = round(state.hazard_e.get(key, 0.0) + hours, 12)
            cursor = nxt

    state.hazard_n[bucket] = round(state.hazard_n.get(bucket, 0.0) + 1.0, 12)
    state.events_total += 1
    state.last_seen_at = ts
    state.last_update_at = ts
    return state


def hazard_rate(state: _HazardState, org_state: _HazardState | None,
                bucket: str) -> float:
    """h, arrivals per hour in `bucket`, shrunk customer -> org -> global.

    0.25 / 42.0 = 1/168, one arrival per week per bucket-hour: the global
    level, a CONSTANT on purpose, content-free and identical for every
    tenant so no cross-organization behaviour is ever pooled. 8 pseudo-
    exposure-hours of organization-prior weight sit between the two.
    """
    org_n = (org_state.hazard_n.get(bucket, 0.0) if org_state else 0.0)
    org_e = (org_state.hazard_e.get(bucket, 0.0) if org_state else 0.0)
    h_org = (org_n + 0.25) / (org_e + 42.0)
    return ((state.hazard_n.get(bucket, 0.0) + 8.0 * h_org)
            / (state.hazard_e.get(bucket, 0.0) + 8.0))


def hazard_p_return(rate: float, ttl_seconds: float) -> float:
    """1 - exp(-h * TTL), the single-bucket approximation: a TTL window that
    spans more than one hour-of-week bucket is priced entirely at the bucket
    it starts in (Phase 2 integrates across the window)."""
    return min(1.0, max(0.0, 1.0 - math.exp(
        max(-50.0, -rate * ttl_seconds / 3600.0))))


def bg_nbd_p_alive(state: _HazardState, now: float) -> float:
    """P(the customer is still alive), BG/NBD with fixed hyperparameters.

    The more arrivals a customer made, the less forgiving a silence is: the
    ratio ((alpha + T)/(alpha + t_x)) raised to (r + x) is the likelihood
    that a customer this active would have gone this quiet by chance.
    """
    x = state.events_total
    t_x = max(0.0, (state.last_seen_at - state.first_seen_at) / 86_400.0)
    t_cap = max(t_x, (now - state.first_seen_at) / 86_400.0)
    z = (math.log(1.0 / (_BG_B + max(x - 1, 0)))
         + min(50.0, _BG_R + x) * math.log((_BG_ALPHA + t_cap)
                                           / (_BG_ALPHA + t_x)))
    return max(0.01, min(1.0, 1.0 / (1.0 + math.exp(min(50.0, max(-50.0, z))))))


def chain_length(rate: float, now: float, last_seen_at: float,
                 ttl_seconds: float, safety_margin_s: float,
                 break_even: float) -> int:
    """n_chain: how many FURTHER keep-alives this ping commits the org to
    before the session's expected next arrival.

    The truncation at I_max = tau * (1/f - 1) is what makes "never warm a
    dead session" arithmetic rather than policy: past it the chain alone
    drives index = p_eff/b - 1 - n_chain below zero for every p <= 1.
    """
    chain_age = max(0.0, now - last_seen_at)
    gap = min(2_592_000.0, max(0.0, 3600.0 / max(rate, 1e-6) - chain_age))
    tau = max(1.0, ttl_seconds - safety_margin_s)
    read_fraction = break_even / (1.0 + break_even)
    imax = tau * max(0.0, 1.0 / max(read_fraction, 1e-9) - 1.0)
    return max(0, math.ceil(min(gap, imax) / tau) - 1)


def dollar_index(p_eff: float, break_even: float, n_chain: int) -> float:
    """index = (p_eff*v_hit - n_chain*c_belief - c_belief) / c_belief
             = p_eff / b - 1 - n_chain

    The prefix dollar value cancels (v_hit/c_belief = (1-f)/f = 1/b), which
    is what makes the index comparable across providers and prefix sizes.
    Mirror of api/store.py warm_index_components and of migration
    202608100002's index block.
    """
    if break_even <= 1e-9:
        # A provider whose reads are free: every ping pays for itself.
        return _INDEX_CLAMP
    return min(_INDEX_CLAMP, max(-_INDEX_CLAMP,
                                 p_eff / break_even - 1 - n_chain))


class LearnedIndexPolicy(V1HeuristicPolicy):
    """Phase 1's learned scheduler, learned ONLINE as the replay advances.

    Deliberately a subclass: the observation path (warm_prefixes rows, EWMA,
    next_due_at, expiry, the reserve) is v1's, byte for byte. What changes is
    what p_return MEANS and what gates on it -- exactly the shape of the
    shipped flag pair (p_hazard_v2 + p_index_enabled in 202608100003), where
    hazard_v2 is an EXTENSION of the index policy and inert without it.

    Per arrival it folds the customer's (and the organization aggregate's)
    decayed hour-of-week hazard statistics forward, then at claim time:

        h        = shrunk hazard rate for this hour-of-week bucket
        p_return = 1 - exp(-h * TTL)              (replaces v1's histogram)
        p_alive  = BG/NBD frequency / recency / age
        n_chain  = keep-alives committed before the expected next arrival
        index    = p_return * p_alive / b - 1 - n_chain

    Admission is index > 0, after the same ROI floor v1 applies (now against
    the hazard p_return), with the per-org daily budget spent greedily by
    index descending. The stop-loss predicate is BYPASSED, exactly as the
    migration specifies -- p_alive and the chain term replace it.

    IT STARTS AT FLAT PRIORS BY CONSTRUCTION. A customer with no state has
    empty hazard maps, so h collapses to the uniform global prior, p_alive is
    1 and n_chain is 0: the flat-prior form the migration proved equal to v1.

    NOT MODELLED: the pacing dual (lambda). At the default unbounded daily
    budget lambda is pinned at 0 for every candidate, so omitting it is
    outcome-identical here; it would matter only under a binding budget.
    """

    name = "learned-index"

    def __init__(self) -> None:
        super().__init__()
        self.state: dict[tuple[str, str, str], _HazardState] = {}
        self.last_index: dict[tuple[str, str, str, str], float] = {}

    def reset(self, cfg: "SimConfig", physics: dict[str, Physics]) -> None:
        super().reset(cfg, physics)
        self.state = {}
        self.last_index = {}

    # -- observe -----------------------------------------------------------
    def on_arrival(self, arrival: Arrival, cache_read: bool) -> None:
        super().on_arrival(arrival, cache_read)
        for subject in (arrival.customer, _ORG_AGGREGATE):
            key = (arrival.org, subject, arrival.provider)
            self.state[key] = hazard_touch(self.state.get(key), arrival.ts)

    # -- score -------------------------------------------------------------
    def _score(self, row: _PrefixRow, now: float) -> tuple[float, float, float, int]:
        """(p_return, p_alive, index, n_chain) for one candidate row."""
        cfg = self.cfg
        assert cfg is not None
        phys = self.physics[row.provider]
        bucket = hour_of_week(now)
        break_even = cfg.break_even_for(row.provider)
        state = self.state.get((row.org, row.customer, row.provider))
        if state is None:
            # No state row: the 202608100002 defaults, which ARE v1.
            p_return = min(1.0, row.hour_histogram.get(bucket, 0)
                           / max(row.arrival_count, 1))
            index = dollar_index(p_return, break_even, 0)
            return p_return, 1.0, index, 0
        org_state = self.state.get((row.org, _ORG_AGGREGATE, row.provider))
        rate = hazard_rate(state, org_state, bucket)
        p_return = hazard_p_return(rate, phys.ttl_seconds)
        p_alive = bg_nbd_p_alive(state, now)
        n_chain = chain_length(rate, now, state.last_seen_at, phys.ttl_seconds,
                               cfg.safety_margin_seconds, break_even)
        # organic_multiplier is pinned at 1.0 in Phase 1: until the (org,
        # prefix) control arm produces per-(provider, hour) organic baselines
        # there is no unbiased estimator for it. Pinning it can only OVERSTATE
        # the index, and the index gates spending, never billing.
        index = dollar_index(p_return * p_alive * 1.0, break_even, n_chain)
        return p_return, p_alive, index, n_chain

    # -- claim -------------------------------------------------------------
    def _eligible(self, row: _PrefixRow, now: float) -> bool:
        """v1's candidate filter MINUS the stop-loss predicate, which
        202608100002 bypasses when the hazard and index flags are both on."""
        cfg = self.cfg
        assert cfg is not None
        return (row.state == "active"
                and row.expires_at > now
                and (row.ewma_interarrival_s is None
                     or row.ewma_interarrival_s <= cfg.max_gap_seconds))

    def next_due_at(self) -> float:
        cfg = self.cfg
        if cfg is None:
            return float("inf")
        soonest = float("inf")
        for row in self.rows.values():
            if row.state != "active":
                continue
            if (row.ewma_interarrival_s is not None
                    and row.ewma_interarrival_s > cfg.max_gap_seconds):
                continue
            soonest = min(soonest, row.next_due_at)
        return soonest

    def on_tick(self, now: float) -> list[PingRequest]:
        cfg = self.cfg
        assert cfg is not None
        day = utc_day(now)
        for key in [k for k, r in self.rows.items() if r.expires_at <= now]:
            del self.rows[key]

        scored: list[tuple[float, _PrefixRow, float]] = []
        for row in self.rows.values():
            if row.next_due_at > now or not self._eligible(row, now):
                continue
            p_return, _p_alive, index, _chain = self._score(row, now)
            self.last_index[row.prefix_key] = index
            scored.append((index, row, p_return))
        # Ordered by the index itself, descending: value per belief-dollar,
        # which is already the right ranking under a binding budget.
        scored.sort(key=lambda item: (-item[0], item[1].next_due_at,
                                      item[1].prefix_key))

        claimed: list[PingRequest] = []
        claimed_counts: dict[tuple[str, str, str], int] = defaultdict(int)
        for index, row, p_return in scored[:cfg.claim_limit * 4]:
            if len(claimed) >= cfg.claim_limit:
                break
            floor = (cfg.roi_min_p if row.arrival_count < cfg.roi_min_arrivals
                     else cfg.break_even_for(row.provider))
            if p_return < floor:
                self.decisions["skipped_roi"] += 1
                continue
            if index <= 0.0:
                self.decisions["skipped_index"] += 1
                continue
            reserve = max(
                row.ping_reserve_usd,
                round(cfg.reserve_usd_per_mtok * row.prefix_tokens / 1_000_000.0, 10))
            cust_key = (row.org, row.customer, row.provider)
            pings_today = sum(
                r.pings_today for r in self.rows.values()
                if (r.org, r.customer, r.provider) == cust_key
                and r.pings_today_date == day)
            if pings_today + claimed_counts[cust_key] >= cfg.max_pings_per_customer_day:
                self.decisions["cap_denied"] += 1
                continue
            book = self.ledger[(row.org, row.provider, day)]
            if book[0] + book[1] + reserve > cfg.daily_budget_usd:
                self.decisions["budget_denied"] += 1
                continue
            book[0] += reserve
            claimed_counts[cust_key] += 1
            self.decisions["pinged"] += 1
            claimed.append(PingRequest(
                prefix_key=row.prefix_key, cache_key=row.cache_key,
                provider=row.provider, customer=row.customer,
                prefix_tokens=row.prefix_tokens, reserved_usd=reserve))
        return claimed

    # -- introspection -----------------------------------------------------
    def summary(self, now: float) -> dict[str, dict[str, Any]]:
        """What the model actually learned, per customer, at `now`."""
        out: dict[str, dict[str, Any]] = {}
        for (org, customer, provider), state in sorted(self.state.items()):
            if customer == _ORG_AGGREGATE:
                continue
            org_state = self.state.get((org, _ORG_AGGREGATE, provider))
            rates = {
                bucket: hazard_rate(state, org_state, bucket)
                for bucket in sorted(state.hazard_n, key=lambda b: int(b))
            }
            top = sorted(rates.items(), key=lambda kv: (-kv[1], int(kv[0])))[:3]
            peak = top[0][1] if top else 0.0
            out[_customer_label(org, customer)] = {
                "top_hazard_hours": [
                    {"hour_of_week": int(bucket),
                     "weekday": ("Mon Tue Wed Thu Fri Sat Sun".split()
                                 [int(bucket) // 24]),
                     "utc_hour": int(bucket) % 24,
                     "arrivals_per_hour": round(rate, 4)}
                    for bucket, rate in top
                ],
                "detected_period_s": (round(3600.0 / peak, 1) if peak > 0 else None),
                "p_alive_end": round(bg_nbd_p_alive(state, now), 4),
                "events_total": state.events_total,
                "buckets_with_mass": len(state.hazard_n),
                "quiet_days": round(max(0.0, now - state.last_seen_at) / 86_400.0, 2),
            }
        return out


class LearnedIndexStopLossPolicy(LearnedIndexPolicy):
    """learned-index with ONE line changed: the v1 stop-loss is not bypassed.

    Not a shipped policy -- a counterfactual, so the cost of that single
    documented design decision is a number rather than an argument.
    202608100002 satisfies the stop-loss predicate unconditionally when the
    hazard and index flags are both on, on the theory that p_alive and the
    chain term subsume it. This arm holds everything else fixed and asks what
    that theory is worth.
    """

    name = "learned-index-stoploss"

    def _eligible(self, row: _PrefixRow, now: float) -> bool:
        cfg = self.cfg
        assert cfg is not None
        return (super()._eligible(row, now)
                and row.consecutive_misses < cfg.stop_loss)

    def next_due_at(self) -> float:
        cfg = self.cfg
        if cfg is None:
            return float("inf")
        soonest = float("inf")
        for row in self.rows.values():
            if row.state != "active" or row.consecutive_misses >= cfg.stop_loss:
                continue
            if (row.ewma_interarrival_s is not None
                    and row.ewma_interarrival_s > cfg.max_gap_seconds):
                continue
            soonest = min(soonest, row.next_due_at)
        return soonest


POLICIES: dict[str, Callable[[], Policy]] = {
    "v1heuristic": V1HeuristicPolicy,
    "never-warm": NeverWarmPolicy,
    "learned-index": LearnedIndexPolicy,
    "learned-index-stoploss": LearnedIndexStopLossPolicy,
}


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------

@dataclass
class SimConfig:
    physics: dict[str, Physics]
    tick_seconds: int = int(V1_DEFAULTS["tick_seconds"])
    safety_margin_seconds: int = int(V1_DEFAULTS["safety_margin_seconds"])
    stop_loss: int = int(V1_DEFAULTS["stop_loss"])
    max_gap_seconds: int = int(V1_DEFAULTS["max_gap_seconds"])
    roi_min_arrivals: int = int(V1_DEFAULTS["roi_min_arrivals"])
    roi_min_p: float = V1_DEFAULTS["roi_min_p"]
    roi_break_even_p: float = V1_DEFAULTS["roi_break_even_p"]
    reserve_usd_per_mtok: float = V1_DEFAULTS["reserve_usd_per_mtok"]
    claim_limit: int = int(V1_DEFAULTS["claim_limit"])
    max_pings_per_customer_day: int = int(V1_DEFAULTS["max_pings_per_customer_day"])
    daily_budget_usd: float = V1_DEFAULTS["daily_budget_usd"]
    prefix_ttl_days: int = int(V1_DEFAULTS["prefix_ttl_days"])
    max_ticks: int = 2_000_000

    def break_even_for(self, provider: str) -> float:
        """Mirror of api/worker.py _warm_break_even_by_provider: the scalar env
        knob stays anthropic's calibrated fallback, every other provider derives
        its break-even from its own read-cost fraction."""
        if provider == "anthropic":
            return self.roi_break_even_p
        phys = self.physics.get(provider)
        if phys is None:
            return self.roi_break_even_p
        frac = phys.read_cost_fraction
        return min(1.0, round(frac / max(1.0 - frac, 1e-9), 6))


@dataclass
class CustomerStats:
    arrivals: int = 0
    warm_arrivals: int = 0
    incremental_warm_arrivals: int = 0
    savings_usd: float = 0.0
    ping_cost_usd: float = 0.0
    pings: int = 0
    pings_after_last_arrival: int = 0

    @property
    def net_usd(self) -> float:
        return self.savings_usd - self.ping_cost_usd


@dataclass
class ReplayResult:
    policy: str
    warm_flags: list[bool]
    ping_cost_usd: float = 0.0
    pings: int = 0
    pings_after_last_arrival: int = 0
    savings_usd: float = 0.0
    ticks: int = 0
    per_customer: dict[str, CustomerStats] = field(default_factory=dict)
    decisions: dict[str, int] = field(default_factory=dict)
    # Timestamped money, so a report can slice a warmup window off the front
    # without re-running the replay. (ts, customer_label, usd).
    savings_events: list[tuple[float, str, float]] = field(default_factory=list)
    ping_events: list[tuple[float, str, float]] = field(default_factory=list)
    # Whatever the policy learned, if it learned anything.
    learned: dict[str, Any] = field(default_factory=dict)

    @property
    def net_usd(self) -> float:
        return self.savings_usd - self.ping_cost_usd

    def window(self, start_ts: float) -> tuple[float, float, dict[str, tuple[float, float]]]:
        """(savings, ping cost, per-customer (savings, cost)) at ts >= start."""
        savings = 0.0
        cost = 0.0
        per: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for ts, label, usd in self.savings_events:
            if ts >= start_ts:
                savings += usd
                per[label][0] += usd
        for ts, label, usd in self.ping_events:
            if ts >= start_ts:
                cost += usd
                per[label][1] += usd
        return savings, cost, {k: (v[0], v[1]) for k, v in per.items()}


def _customer_label(org: str, customer: str) -> str:
    return f"{org}/{customer}"


def replay(arrivals: Sequence[Arrival], cfg: SimConfig, policy: Policy,
           baseline_warm: Sequence[bool] | None = None) -> ReplayResult:
    """Replay `arrivals` under `policy`, returning exact dollars.

    `baseline_warm` is the never-warm warm/cold verdict per arrival index. When
    given, savings are the incremental ones only; when omitted (the baseline
    pass itself) savings are zero by definition.
    """
    policy.reset(cfg, cfg.physics)
    expiry: dict[tuple[str, str, str], float] = {}
    warm_flags: list[bool] = []
    result = ReplayResult(policy=policy.name, warm_flags=warm_flags)
    per_customer: dict[str, CustomerStats] = defaultdict(CustomerStats)

    last_arrival_ts = arrivals[-1].ts if arrivals else 0.0
    tick = float(cfg.tick_seconds)
    # Grid origin: the first arrival, so tick alignment is reproducible.
    origin = arrivals[0].ts if arrivals else 0.0
    grid_k = 0
    # Nothing can happen after every prefix row has expired.
    hard_stop = last_arrival_ts + cfg.prefix_ttl_days * _SEC_PER_DAY + tick

    index = 0
    total = len(arrivals)
    while True:
        if result.ticks > cfg.max_ticks:
            raise RuntimeError("simulation exceeded max_ticks; coarsen --tick-seconds")
        next_arrival_ts = arrivals[index].ts if index < total else float("inf")
        due = policy.next_due_at()
        if due == float("inf") or due > hard_stop:
            tick_ts = float("inf")
        else:
            k = max(grid_k, math.ceil((due - origin) / tick - 1e-9))
            tick_ts = origin + k * tick
            if tick_ts > hard_stop:
                tick_ts = float("inf")
        if next_arrival_ts == float("inf") and tick_ts == float("inf"):
            break

        if next_arrival_ts <= tick_ts:
            # Arrivals win ties: a request landing exactly on a tick boundary
            # has already refreshed the entry the worker would have pinged.
            arrival = arrivals[index]
            index += 1
            phys = cfg.physics.get(arrival.provider)
            if phys is None:
                raise KeyError(
                    f"no physics configured for provider {arrival.provider!r}; "
                    f"known: {','.join(sorted(cfg.physics))}. Add one with "
                    f"--provider-ttl/--read-fraction/--write-multiplier/--input-price")
            cache_read = expiry.get(arrival.cache_key, float("-inf")) > arrival.ts
            expiry[arrival.cache_key] = arrival.ts + phys.ttl_seconds
            warm_flags.append(cache_read)
            label = _customer_label(arrival.org, arrival.customer)
            stats = per_customer[label]
            stats.arrivals += 1
            stats.warm_arrivals += int(cache_read)
            if baseline_warm is not None:
                base = baseline_warm[len(warm_flags) - 1]
                if cache_read and not base:
                    gain = phys.hit_savings_usd(arrival.prefix_tokens)
                    stats.incremental_warm_arrivals += 1
                    stats.savings_usd += gain
                    result.savings_usd += gain
                    result.savings_events.append((arrival.ts, label, gain))
                elif base and not cache_read:
                    # Impossible: pings only add touches, and warmth is monotone
                    # in touches. A violation means the engine lost a refresh.
                    raise AssertionError(
                        "policy turned a baseline-warm arrival cold; "
                        "cache accounting is broken")
            policy.on_arrival(arrival, cache_read)
            continue

        # Tick.
        grid_k = int(round((tick_ts - origin) / tick)) + 1
        result.ticks += 1
        for req in policy.on_tick(tick_ts):
            phys = cfg.physics[req.provider]
            warm_at_ping = expiry.get(req.cache_key, float("-inf")) > tick_ts
            spent = (phys.read_usd(req.prefix_tokens) if warm_at_ping
                     else phys.write_usd(req.prefix_tokens))
            expiry[req.cache_key] = tick_ts + phys.ttl_seconds
            result.ping_cost_usd += spent
            result.pings += 1
            label = _customer_label(req.prefix_key[0], req.customer)
            stats = per_customer[label]
            stats.ping_cost_usd += spent
            stats.pings += 1
            result.ping_events.append((tick_ts, label, spent))
            if tick_ts > last_arrival_ts:
                result.pings_after_last_arrival += 1
                stats.pings_after_last_arrival += 1
            policy.on_settle(req, tick_ts, spent)

    result.per_customer = dict(per_customer)
    result.decisions = dict(getattr(policy, "decisions", {}) or {})
    summarize = getattr(policy, "summary", None)
    if callable(summarize):
        # Read the posterior as of the last arrival, not as of the last tick:
        # "P(alive) at the end" means at the end of the observed trace.
        result.learned = summarize(last_arrival_ts)
    return result


# --------------------------------------------------------------------------
# hindsight oracle
# --------------------------------------------------------------------------

@dataclass
class OracleResult:
    net_usd: float = 0.0
    savings_usd: float = 0.0
    ping_cost_usd: float = 0.0
    pings: int = 0
    bridged_gaps: int = 0
    declined_gaps: int = 0
    per_customer: dict[str, CustomerStats] = field(default_factory=dict)
    # (arrival ts, customer label, gain usd, chain cost usd) per bridged gap.
    bridge_events: list[tuple[float, str, float, float]] = field(default_factory=list)

    def window(self, start_ts: float) -> tuple[float, dict[str, float]]:
        """(net usd, per-customer net usd) over gaps CLOSING at ts >= start."""
        net = 0.0
        per: dict[str, float] = defaultdict(float)
        for ts, label, gain, cost in self.bridge_events:
            if ts >= start_ts:
                net += gain - cost
                per[label] += gain - cost
        return net, dict(per)


def hindsight_oracle(arrivals: Sequence[Arrival], cfg: SimConfig) -> OracleResult:
    """The best any ping schedule could have done, knowing the whole trace.

    For a hard-TTL provider the backward pass collapses to a per-gap decision,
    and the collapse is exact rather than a heuristic:

      * every arrival touches the cache itself, so the state at the start of any
        inter-arrival gap is fixed at "warm until t_i + TTL" no matter what was
        done earlier -- the gaps are independent subproblems;
      * within a gap, a partial ping chain buys nothing (the arrival still lands
        cold) and a lapsed, write-priced restart costs w*P against a maximum
        gain of (w - f)*P, so it can never pay. Only the minimal *unbroken*
        chain is ever a candidate;
      * that chain needs k = ceil(g / TTL) - 1 pings, each landing on a still-warm
        entry and therefore read-priced at f*P.

    So: bridge the gap iff (w - f) * P_arrival > k * f * P_ping. Pings after the
    last arrival, and pings before the first, are never taken -- there is no
    arrival to serve.
    """
    by_cache: dict[tuple[str, str, str], list[Arrival]] = defaultdict(list)
    for arrival in arrivals:
        by_cache[arrival.cache_key].append(arrival)
    out = OracleResult()
    per_customer: dict[str, CustomerStats] = defaultdict(CustomerStats)
    for cache_key, stream in by_cache.items():
        stream.sort(key=lambda a: a.ts)
        phys = cfg.physics.get(cache_key[1])
        if phys is None:
            raise KeyError(f"no physics configured for provider {cache_key[1]!r}")
        ttl = phys.ttl_seconds
        for prev, nxt in zip(stream, stream[1:]):
            gap = nxt.ts - prev.ts
            label = _customer_label(nxt.org, nxt.customer)
            stats = per_customer[label]
            stats.arrivals += 1
            if gap <= ttl:
                # Organically warm. Nothing to buy, nothing to claim.
                stats.warm_arrivals += 1
                continue
            pings = max(0, math.ceil(gap / ttl - 1.0 - 1e-9))
            gain = phys.hit_savings_usd(nxt.prefix_tokens)
            # The chain re-pings the payload we last observed, which is the
            # prefix carried by the arrival that opened the gap.
            cost = pings * phys.read_usd(prev.prefix_tokens)
            if gain > cost:
                out.bridged_gaps += 1
                out.savings_usd += gain
                out.ping_cost_usd += cost
                out.pings += pings
                stats.warm_arrivals += 1
                stats.incremental_warm_arrivals += 1
                stats.savings_usd += gain
                stats.ping_cost_usd += cost
                stats.pings += pings
                out.bridge_events.append((nxt.ts, label, gain, cost))
            else:
                out.declined_gaps += 1
    out.net_usd = out.savings_usd - out.ping_cost_usd
    out.per_customer = dict(per_customer)
    return out


def oracle_fraction(net_usd: float, oracle_net_usd: float) -> float | None:
    """Fraction-of-oracle net dollars, or None when the denominator is too
    small for the ratio to mean anything."""
    if oracle_net_usd < ORACLE_RATIO_FLOOR_USD:
        return None
    return net_usd / oracle_net_usd


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def _usd(value: float) -> str:
    return f"${value:,.6f}"


def _frac(value: float | None) -> str:
    if value is None:
        return f"n/a (oracle < {_usd(ORACLE_RATIO_FLOOR_USD)})"
    return f"{value * 100:.1f}%"


def evaluate(arrivals: Sequence[Arrival], cfg: SimConfig,
             policy_names: Sequence[str]) -> dict[str, Any]:
    if not arrivals:
        raise ValueError("trace is empty")
    baseline = replay(arrivals, cfg, NeverWarmPolicy())
    oracle = hindsight_oracle(arrivals, cfg)
    results: list[tuple[str, ReplayResult]] = []
    for name in policy_names:
        factory = POLICIES.get(name)
        if factory is None:
            raise KeyError(f"unknown policy {name!r}; known: {sorted(POLICIES)}")
        results.append((name, replay(arrivals, cfg, factory(), baseline.warm_flags)))

    span = arrivals[-1].ts - arrivals[0].ts
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "trace": {
            "arrivals": len(arrivals),
            "span_hours": round(span / 3600.0, 3),
            "start": datetime.fromtimestamp(arrivals[0].ts, timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(arrivals[-1].ts, timezone.utc).isoformat(),
            "customers": sorted({_customer_label(a.org, a.customer) for a in arrivals}),
            "providers": sorted({a.provider for a in arrivals}),
            "cache_keys": len({a.cache_key for a in arrivals}),
            "baseline_warm_arrivals": sum(baseline.warm_flags),
        },
        "oracle": {
            "net_usd": oracle.net_usd,
            "savings_usd": oracle.savings_usd,
            "ping_cost_usd": oracle.ping_cost_usd,
            "pings": oracle.pings,
            "bridged_gaps": oracle.bridged_gaps,
            "declined_gaps": oracle.declined_gaps,
            "per_customer": {
                label: {
                    "net_usd": st.net_usd, "savings_usd": st.savings_usd,
                    "ping_cost_usd": st.ping_cost_usd, "pings": st.pings,
                } for label, st in sorted(oracle.per_customer.items())
            },
        },
        "policies": [],
    }
    for name, res in results:
        payload["policies"].append({
            "policy": name,
            "net_usd": res.net_usd,
            "savings_usd": res.savings_usd,
            "ping_cost_usd": res.ping_cost_usd,
            "pings": res.pings,
            "pings_after_last_arrival": res.pings_after_last_arrival,
            "warm_arrivals": sum(res.warm_flags),
            "incremental_warm_arrivals": sum(
                st.incremental_warm_arrivals for st in res.per_customer.values()),
            "ticks": res.ticks,
            "decisions": res.decisions,
            "oracle_fraction": oracle_fraction(res.net_usd, oracle.net_usd),
            "per_customer": {
                label: {
                    "arrivals": st.arrivals,
                    "warm_arrivals": st.warm_arrivals,
                    "incremental_warm_arrivals": st.incremental_warm_arrivals,
                    "net_usd": st.net_usd,
                    "savings_usd": st.savings_usd,
                    "ping_cost_usd": st.ping_cost_usd,
                    "pings": st.pings,
                    "pings_after_last_arrival": st.pings_after_last_arrival,
                    "oracle_fraction": oracle_fraction(
                        st.net_usd,
                        oracle.per_customer.get(label, CustomerStats()).net_usd),
                } for label, st in sorted(res.per_customer.items())
            },
        })
    payload["_objects"] = {"baseline": baseline, "oracle": oracle,
                           "results": results, "start_ts": arrivals[0].ts,
                           "end_ts": arrivals[-1].ts, "arrivals": arrivals}
    return payload


def render(payload: dict[str, Any], title: str = "") -> str:
    lines: list[str] = []
    trace = payload["trace"]
    head = title or "trace"
    lines.append("=" * 78)
    lines.append(f"{head}: {trace['arrivals']} arrivals over {trace['span_hours']}h, "
                 f"{trace['cache_keys']} cache key(s), providers "
                 f"{','.join(trace['providers'])}")
    lines.append(f"  window {trace['start']} .. {trace['end']}")
    lines.append(f"  never-warm baseline: {trace['baseline_warm_arrivals']}"
                 f"/{trace['arrivals']} arrivals organically warm")
    orc = payload["oracle"]
    lines.append("-" * 78)
    lines.append(f"  HINDSIGHT ORACLE   net {_usd(orc['net_usd'])}"
                 f"  (savings {_usd(orc['savings_usd'])}"
                 f" - pings {_usd(orc['ping_cost_usd'])} over {orc['pings']} pings;"
                 f" bridged {orc['bridged_gaps']} gaps, declined {orc['declined_gaps']})")
    lines.append("-" * 78)
    for pol in payload["policies"]:
        lines.append(f"  {pol['policy']:<14} net {_usd(pol['net_usd']):>16}"
                     f"   of-oracle {_frac(pol['oracle_fraction'])}")
        lines.append(f"      savings {_usd(pol['savings_usd'])}"
                     f" on {pol['incremental_warm_arrivals']} incremental warm"
                     f" arrivals ({pol['warm_arrivals']} warm total)")
        lines.append(f"      ping cost {_usd(pol['ping_cost_usd'])}"
                     f" over {pol['pings']} pings"
                     f" ({pol['pings_after_last_arrival']} after the last arrival)")
        if pol["decisions"]:
            detail = "  ".join(f"{k}={v}" for k, v in sorted(pol["decisions"].items()))
            lines.append(f"      claim decisions: {detail}   ticks={pol['ticks']}")
        for label, st in pol["per_customer"].items():
            lines.append(
                f"        - {label:<22} net {_usd(st['net_usd']):>14}"
                f"  of-oracle {_frac(st['oracle_fraction'])}"
                f"  [{st['incremental_warm_arrivals']}/{st['arrivals']} incr warm,"
                f" {st['pings']} pings]")
    lines.append("=" * 78)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# learning report
# --------------------------------------------------------------------------

WARMUP_DAYS = 7


def render_learning(payload: dict[str, Any], title: str = "",
                    warmup_days: int = WARMUP_DAYS) -> str:
    """Warmup window (days 1..N) versus evaluation window (days N+1..end).

    Everything below is EVALUATION-WINDOW money. The warmup window is where
    the online model builds its hazard mass; scoring a learner on the period
    it was learning in flatters it, so that period is reported separately and
    then excluded.
    """
    objs = payload["_objects"]
    baseline: ReplayResult = objs["baseline"]
    oracle: OracleResult = objs["oracle"]
    results: list[tuple[str, ReplayResult]] = objs["results"]
    trace = payload["trace"]

    start_ts = objs["start_ts"]
    end_ts = objs["end_ts"]
    eval_start = start_ts + warmup_days * _SEC_PER_DAY
    total_days = (end_ts - start_ts) / _SEC_PER_DAY

    lines: list[str] = []
    lines.append("=" * 92)
    lines.append(f"LEARNING REPORT -- {title or 'trace'}")
    lines.append("=" * 92)
    lines.append(f"  {trace['arrivals']:,} arrivals, {len(trace['customers'])} customers, "
                 f"{trace['cache_keys']} cache key(s), {total_days:.1f} simulated days")
    lines.append(f"  warmup    days 1-{warmup_days}   "
                 f"{datetime.fromtimestamp(start_ts, timezone.utc).date()} .. "
                 f"{datetime.fromtimestamp(eval_start, timezone.utc).date()}"
                 f"   (model builds hazard mass; excluded from every number below)")
    lines.append(f"  EVALUATION days {warmup_days + 1}-{total_days:.0f}  "
                 f"{datetime.fromtimestamp(eval_start, timezone.utc).date()} .. "
                 f"{datetime.fromtimestamp(end_ts, timezone.utc).date()}")
    lines.append("")

    oracle_eval, oracle_per = oracle.window(eval_start)
    lines.append("-" * 92)
    lines.append("EVALUATION WINDOW -- net dollars and fraction of the hindsight optimum")
    lines.append("-" * 92)
    lines.append(f"  {'policy':<24}{'net $':>14}{'savings $':>14}"
                 f"{'ping cost $':>14}{'pings':>9}{'of oracle':>13}")
    rows: list[tuple[str, float, float, float, int]] = [
        ("never-warm", 0.0, 0.0, 0.0, 0)]
    windows: dict[str, tuple[float, float, dict[str, tuple[float, float]]]] = {}
    for name, res in results:
        savings, cost, per = res.window(eval_start)
        windows[name] = (savings, cost, per)
        pings = sum(1 for ts, _l, _u in res.ping_events if ts >= eval_start)
        if name == "never-warm":
            rows[0] = (name, savings - cost, savings, cost, pings)
        else:
            rows.append((name, savings - cost, savings, cost, pings))
    for name, net, savings, cost, pings in rows:
        frac = oracle_fraction(net, oracle_eval)
        lines.append(f"  {name:<24}{net:>14.6f}{savings:>14.6f}"
                     f"{cost:>14.6f}{pings:>9,}{_frac(frac):>13}")
    oracle_pings = sum(1 for ts, _l, _g, _c in oracle.bridge_events if ts >= eval_start)
    lines.append(f"  {'oracle':<24}{oracle_eval:>14.6f}{'':>14}{'':>14}"
                 f"{oracle_pings:>9,}{'100.0%':>13}"
                 f"   <- hindsight bound, {oracle_pings} gaps bridged")
    lines.append("")

    learned_name = "learned-index"
    learned_res = dict(results).get(learned_name)
    v1_res = dict(results).get("v1heuristic")
    if learned_res is None:
        lines.append("  (learned-index not among the replayed policies)")
        lines.append("=" * 92)
        return "\n".join(lines)

    lines.append("-" * 92)
    lines.append("WHAT learned-index LEARNED, PER CUSTOMER (evaluation window money)")
    lines.append("-" * 92)
    _s, _c, learned_per = windows[learned_name]
    v1_per = windows.get("v1heuristic", (0.0, 0.0, {}))[2]
    for label in sorted(trace["customers"]):
        info = learned_res.learned.get(label, {})
        l_sav, l_cost = learned_per.get(label, (0.0, 0.0))
        v_sav, v_cost = v1_per.get(label, (0.0, 0.0))
        l_net, v_net = l_sav - l_cost, v_sav - v_cost
        pings = sum(1 for ts, lab, _u in learned_res.ping_events
                    if ts >= eval_start and lab == label)
        hits = sum(1 for ts, lab, _u in learned_res.savings_events
                   if ts >= eval_start and lab == label)
        top = info.get("top_hazard_hours") or []
        top_text = ", ".join(
            f"{h['weekday']} {h['utc_hour']:02d}:00Z ({h['arrivals_per_hour']:.2f}/h)"
            for h in top) or "(no mass)"
        period = info.get("detected_period_s")
        period_text = f"{period / 60.0:.1f} min" if period else "n/a"
        lines.append(f"  {label}")
        lines.append(f"      top hazard hours : {top_text}")
        lines.append(f"      peak-bucket period: {period_text}"
                     f"   P(alive) at end: {info.get('p_alive_end', 1.0):.3f}"
                     f"   quiet {info.get('quiet_days', 0.0):.1f}d"
                     f"   buckets w/ mass {info.get('buckets_with_mass', 0)}")
        lines.append(f"      pings {pings:>5,}   warm hits {hits:>5,}"
                     f"   net {_usd(l_net):>14}"
                     f"   vs v1 {_usd(v_net):>14}"
                     f"   delta {_usd(l_net - v_net):>14}")
    # -- where the pings actually went ------------------------------------
    lines.append("")
    lines.append("-" * 92)
    lines.append("WHERE learned-index SPENT IT -- pings by how long the customer "
                 "was ALREADY silent")
    lines.append("-" * 92)
    # Merge the real arrival stream with the ping stream so every ping can be
    # labelled with how long that customer had already been quiet.
    last_seen: dict[str, float] = {}
    stream: list[tuple[float, int, str, float]] = [
        (a.ts, 0, _customer_label(a.org, a.customer), 0.0)
        for a in objs["arrivals"]
    ]
    stream += [(ts, 1, label, usd) for ts, label, usd in learned_res.ping_events]
    stream.sort(key=lambda item: (item[0], item[1]))
    edges = ((300.0, "< 5m"), (900.0, "5-15m"), (3_600.0, "15-60m"),
             (21_600.0, "1-6h"), (float("inf"), "> 6h"))
    hist: dict[str, list[float]] = {name: [0, 0.0] for _e, name in edges}
    for ts, kind, label, usd in stream:
        if kind == 0:
            last_seen[label] = ts
            continue
        if ts < eval_start:
            continue
        idle = ts - last_seen.get(label, ts)
        for edge, name in edges:
            if idle < edge:
                hist[name][0] += 1
                hist[name][1] += usd
                break
    total_pings = sum(int(v[0]) for v in hist.values())
    total_cost = sum(v[1] for v in hist.values())
    for _edge, name in edges:
        count, cost = hist[name]
        lines.append(f"    silent {name:<8} {int(count):>6,} pings "
                     f"({int(count) / max(total_pings, 1) * 100:5.1f}%)"
                     f"   ${cost:>9.4f} ({cost / max(total_cost, 1e-9) * 100:5.1f}% of spend)")
    reads = [u for ts, _l, u in learned_res.ping_events if ts >= eval_start]
    write_priced = sum(1 for u in reads if u > 0.003)
    lines.append(f"    write-priced pings: {write_priced:,} of {len(reads):,} "
                 f"({write_priced / max(len(reads), 1) * 100:.1f}%) -- a ping that "
                 f"lands on an entry that already lapsed pays the full write premium")

    lines.append("")
    lines.append("-" * 92)
    l_net_total = windows[learned_name][0] - windows[learned_name][1]
    v_net_total = (windows.get("v1heuristic", (0.0, 0.0, {}))[0]
                   - windows.get("v1heuristic", (0.0, 0.0, {}))[1])
    lines.append(f"  learned-index net {_usd(l_net_total)}"
                 f"   v1heuristic net {_usd(v_net_total)}"
                 f"   delta {_usd(l_net_total - v_net_total)}")
    if learned_res.decisions:
        lines.append("  learned-index claim decisions (whole run): "
                     + "  ".join(f"{k}={v}" for k, v in
                                 sorted(learned_res.decisions.items())))
    if v1_res is not None and v1_res.decisions:
        lines.append("  v1heuristic  claim decisions (whole run): "
                     + "  ".join(f"{k}={v}" for k, v in
                                 sorted(v1_res.decisions.items())))
    lines.append("=" * 92)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def selftest(cfg_factory: Callable[[], SimConfig]) -> int:
    """Run the synthetic scenarios and assert the invariants that must hold for
    any policy, plus the scenario-specific economics the simulator exists to
    expose."""
    checks = 0
    for scenario in ("cron", "bursty", "churned"):
        arrivals = SCENARIOS[scenario](0)
        cfg = cfg_factory()
        payload = evaluate(arrivals, cfg, ["never-warm", "v1heuristic"])
        print(render(payload, title=f"selftest scenario '{scenario}'"))
        orc = payload["oracle"]["net_usd"]
        by_name = {p["policy"]: p for p in payload["policies"]}
        never = by_name["never-warm"]
        v1 = by_name["v1heuristic"]

        # -- universal invariants ------------------------------------------
        _assert(orc >= -1e-12,
                f"[{scenario}] oracle net must be non-negative, got {orc}")
        checks += 1
        for pol in payload["policies"]:
            _assert(pol["net_usd"] <= orc + 1e-9,
                    f"[{scenario}] {pol['policy']} net {pol['net_usd']} beats "
                    f"hindsight oracle {orc}")
            checks += 1
            _assert(pol["savings_usd"] >= -1e-12,
                    f"[{scenario}] {pol['policy']} has negative savings")
            checks += 1
        _assert(abs(never["net_usd"]) < 1e-12,
                f"[{scenario}] never-warm net must be exactly zero, got "
                f"{never['net_usd']}")
        checks += 1
        _assert(never["pings"] == 0, f"[{scenario}] never-warm pinged")
        checks += 1
        if orc > ORACLE_RATIO_FLOOR_USD:
            _assert(never["oracle_fraction"] == 0.0,
                    f"[{scenario}] never-warm oracle fraction must be 0 when the "
                    f"oracle is positive, got {never['oracle_fraction']}")
            checks += 1
        else:
            _assert(never["oracle_fraction"] is None,
                    f"[{scenario}] tiny-oracle ratio must be suppressed")
            checks += 1
        # Ping economics: every ping is priced at read or write, never free.
        _assert((v1["ping_cost_usd"] > 0) == (v1["pings"] > 0),
                f"[{scenario}] v1 ping count and ping cost disagree")
        checks += 1
        # The oracle never spends against arrivals that do not exist.
        _assert(payload["oracle"]["pings"] == 0 or orc > 0,
                f"[{scenario}] oracle bought pings that did not pay")
        checks += 1

        # -- scenario economics --------------------------------------------
        if scenario == "cron":
            # The scenario warming was built for: v1 must be strictly between
            # doing nothing and doing it perfectly.
            _assert(orc > ORACLE_RATIO_FLOOR_USD,
                    f"[cron] oracle should find real money, got {orc}")
            checks += 1
            _assert(never["net_usd"] < v1["net_usd"] < orc,
                    f"[cron] v1 must sit strictly between never-warm "
                    f"({never['net_usd']}) and oracle ({orc}), got {v1['net_usd']}")
            checks += 1
            _assert(0.0 < v1["oracle_fraction"] < 1.0,
                    f"[cron] v1 oracle fraction out of range: "
                    f"{v1['oracle_fraction']}")
            checks += 1
            # v1 pays the safety margin: one ping per (TTL - 60) window is
            # strictly more pings than the oracle's minimal chain.
            _assert(v1["pings"] > payload["oracle"]["pings"],
                    "[cron] v1 should spend more pings than the minimal chain")
            checks += 1
        if scenario == "churned":
            # The tail is the finding: v1 keeps paying after the customer is
            # gone, until consecutive_misses trips the stop-loss.
            _assert(v1["pings_after_last_arrival"] > 0,
                    "[churned] v1 should burn stop-loss pings past the last arrival")
            checks += 1
            _assert(v1["pings_after_last_arrival"] <= cfg.stop_loss,
                    f"[churned] stop-loss must bound the dead tail at "
                    f"{cfg.stop_loss}, got {v1['pings_after_last_arrival']}")
            checks += 1
            _assert(payload["oracle"]["pings"] >= 0 and orc >= 0, "[churned] oracle sane")
            checks += 1

        # -- monotonicity: warmth is monotone in touches --------------------
        baseline = payload["_objects"]["baseline"]
        for name, res in payload["_objects"]["results"]:
            for i, (base, got) in enumerate(zip(baseline.warm_flags, res.warm_flags)):
                _assert(got or not base,
                        f"[{scenario}] {name} lost a baseline-warm arrival at {i}")
            checks += 1

    # -- oracle correctness on a hand-computable case -----------------------
    cfg = cfg_factory()
    phys = cfg.physics["anthropic"]
    tokens = 1_000_000
    base = phys.base_usd(tokens)                    # $3.00
    two = [Arrival(_ANCHOR, "o", "c", "anthropic", "h", tokens),
           Arrival(_ANCHOR + 600.0, "o", "c", "anthropic", "h", tokens)]
    orc2 = hindsight_oracle(two, cfg)
    # gap 600 over a 300s TTL -> one bridging ping at 0.10x, gain 1.15x.
    _assert(orc2.pings == 1, f"expected a 1-ping bridge, got {orc2.pings}")
    _assert(abs(orc2.savings_usd - 1.15 * base) < 1e-9,
            f"gain should be (w-f)*P, got {orc2.savings_usd}")
    _assert(abs(orc2.ping_cost_usd - 0.10 * base) < 1e-9,
            f"chain should be read-priced, got {orc2.ping_cost_usd}")
    checks += 3
    # A gap so wide the chain costs more than the hit is worth: 1.15 < k*0.10
    # once k >= 12, i.e. gap > 12 * TTL.
    wide = [Arrival(_ANCHOR, "o", "c", "anthropic", "h", tokens),
            Arrival(_ANCHOR + 300.0 * 14, "o", "c", "anthropic", "h", tokens)]
    orc3 = hindsight_oracle(wide, cfg)
    _assert(orc3.pings == 0 and orc3.net_usd == 0.0,
            f"oracle must decline an unaffordable chain, got {orc3}")
    checks += 1
    # deepseek's 4h TTL swallows the same gap whole: nothing to buy.
    ds_cfg = cfg_factory()
    ds = [Arrival(_ANCHOR, "o", "c", "deepseek", "h", tokens, "deepseek-chat"),
          Arrival(_ANCHOR + 600.0, "o", "c", "deepseek", "h", tokens, "deepseek-chat")]
    orc4 = hindsight_oracle(ds, ds_cfg)
    _assert(orc4.pings == 0 and orc4.net_usd == 0.0,
            f"deepseek gap is inside TTL, oracle should spend nothing: {orc4}")
    checks += 1
    # Break-even parity with api/worker.py _warm_break_even_by_provider.
    _assert(abs(ds_cfg.break_even_for("deepseek") - 0.020408) < 1e-6,
            f"deepseek break-even drifted: {ds_cfg.break_even_for('deepseek')}")
    _assert(ds_cfg.break_even_for("anthropic") == V1_DEFAULTS["roi_break_even_p"],
            "anthropic break-even must stay the calibrated scalar")
    checks += 2

    # -- the learned scheduler on the company eval window -------------------
    company = SCENARIOS["company"](0)
    cfg = cfg_factory()
    payload = evaluate(company, cfg, ["never-warm", "v1heuristic", "learned-index",
                                      "learned-index-stoploss"])
    print(render_learning(payload, title="selftest scenario 'company'"))
    objs = payload["_objects"]
    eval_start = objs["start_ts"] + WARMUP_DAYS * _SEC_PER_DAY
    by_policy = dict(objs["results"])
    oracle_eval, _per = objs["oracle"].window(eval_start)

    def _eval_net(name: str) -> float:
        savings, cost, _ = by_policy[name].window(eval_start)
        return savings - cost

    learned_net = _eval_net("learned-index")
    v1_net = _eval_net("v1heuristic")
    never_net = _eval_net("never-warm")

    _assert(abs(never_net) < 1e-12,
            f"[company] never-warm eval net must be zero, got {never_net}")
    checks += 1

    # THE HEADLINE COMPARISON, REPORTED RATHER THAN ASSERTED.
    #
    # This harness was asked to assert learned-index >= v1heuristic on the
    # company evaluation window. It does not, and encoding that inequality as
    # an assertion would bake a claim the trace refutes into the test suite --
    # the next person to run --selftest would read a green bar as evidence for
    # something this simulator actually measures as false. So the comparison
    # is computed, printed and left falsifiable, while the invariants that ARE
    # unconditional stay fatal below.
    stoploss_net = _eval_net("learned-index-stoploss") if (
        "learned-index-stoploss" in by_policy) else None
    print("\n" + "!" * 78)
    print("HEADLINE FINDING (reported, not asserted -- see selftest source)")
    print(f"  company eval window   v1heuristic   net {_usd(v1_net)}")
    print(f"                        learned-index net {_usd(learned_net)}")
    if stoploss_net is not None:
        print(f"                        +stop-loss    net {_usd(stoploss_net)}")
    print(f"                        oracle        net {_usd(oracle_eval)}")
    verdict = ("HOLDS" if learned_net >= v1_net - 1e-12 else "DOES NOT HOLD")
    print(f"  learned-index >= v1heuristic : {verdict}")
    print("!" * 78 + "\n")

    # And neither may beat hindsight, which is the whole point of having one.
    for name, net in (("learned-index", learned_net), ("v1heuristic", v1_net),
                      ("learned-index-stoploss", _eval_net("learned-index-stoploss"))):
        _assert(net <= oracle_eval + 1e-9,
                f"[company] {name} eval net {net} beats the hindsight oracle "
                f"{oracle_eval}")
        checks += 1
    _assert(oracle_eval >= -1e-12,
            f"[company] oracle eval net must be non-negative, got {oracle_eval}")
    checks += 1
    # Warmth stays monotone in touches for the learned policy too.
    base_flags = objs["baseline"].warm_flags
    for i, (base, got) in enumerate(zip(base_flags,
                                        by_policy["learned-index"].warm_flags)):
        _assert(got or not base,
                f"[company] learned-index lost a baseline-warm arrival at {i}")
    checks += 1
    # Flat priors are v1 by construction: a customer the model has never seen
    # must score exactly the v1 histogram, p_alive 1, n_chain 0.
    flat = LearnedIndexPolicy()
    flat.reset(cfg_factory(), cfg.physics)
    row = _PrefixRow(org="o", customer="c", provider="anthropic",
                     prefix_hash="h", prefix_tokens=1000, arrival_count=10,
                     hour_histogram={hour_of_week(_ANCHOR): 5})
    p_ret, p_alive, index, n_chain = flat._score(row, _ANCHOR)
    _assert(abs(p_ret - 0.5) < 1e-12 and p_alive == 1.0 and n_chain == 0,
            f"[flat-prior] cold start must be v1-equivalent, got "
            f"p_return={p_ret} p_alive={p_alive} n_chain={n_chain}")
    _assert(abs(index - (0.5 / V1_DEFAULTS["roi_break_even_p"] - 1)) < 1e-9,
            f"[flat-prior] index must be p/b - 1 at flat priors, got {index}")
    checks += 2
    # The dollar index is the store's, algebraically: index = p/b - 1 - n.
    from_store = 0.4 / 0.11 - 1 - 3
    _assert(abs(dollar_index(0.4, 0.11, 3) - from_store) < 1e-12,
            "dollar_index drifted from p_eff/b - 1 - n_chain")
    checks += 1
    # A dead session is refused by arithmetic, not policy: silence drives
    # P(alive) down and the chain term up until the index cannot clear zero.
    dead = _HazardState(hazard_n={"0": 6.0}, hazard_e={"0": 3.0}, events_total=200,
                        first_seen_at=_ANCHOR - 20 * _SEC_PER_DAY,
                        last_seen_at=_ANCHOR - 10 * _SEC_PER_DAY,
                        last_update_at=_ANCHOR - 10 * _SEC_PER_DAY)
    _assert(bg_nbd_p_alive(dead, _ANCHOR) < 0.5,
            "[p_alive] ten silent days after 200 arrivals should read as churn")
    checks += 1

    print(f"\nselftest OK: {checks} invariants asserted across "
          f"{len(SCENARIOS)} synthetic scenarios")
    return 0


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def _kv_floats(pairs: Sequence[str] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in pairs or ():
        if "=" not in item:
            raise ValueError(f"expected provider=value, got {item!r}")
        key, _, value = item.partition("=")
        out[key.strip().lower()] = float(value)
    return out


def build_physics(args: argparse.Namespace) -> dict[str, Physics]:
    spec: dict[str, dict[str, float]] = {
        name: dict(values) for name, values in DEFAULT_PHYSICS.items()
    }
    overrides = (
        ("ttl_seconds", _kv_floats(args.provider_ttl)),
        ("read_cost_fraction", _kv_floats(args.read_fraction)),
        ("write_multiplier", _kv_floats(args.write_multiplier)),
        ("input_price_per_mtok", _kv_floats(args.input_price)),
    )
    for field_name, mapping in overrides:
        for provider, value in mapping.items():
            spec.setdefault(provider, dict(DEFAULT_PHYSICS["anthropic"]))[field_name] = value
    physics: dict[str, Physics] = {}
    for provider, values in spec.items():
        if values["ttl_seconds"] <= 0:
            raise ValueError(f"{provider}: ttl_seconds must be positive")
        if not 0.0 <= values["read_cost_fraction"] <= 1.0:
            raise ValueError(f"{provider}: read_cost_fraction must be in [0,1]")
        if values["write_multiplier"] < values["read_cost_fraction"]:
            raise ValueError(
                f"{provider}: write_multiplier below read_cost_fraction leaves "
                "warming with nothing to save")
        physics[provider] = Physics(provider=provider, **values)
    return physics


def build_config(args: argparse.Namespace) -> SimConfig:
    return SimConfig(
        physics=build_physics(args),
        tick_seconds=args.tick_seconds,
        safety_margin_seconds=args.safety_margin_seconds,
        stop_loss=args.stop_loss,
        max_gap_seconds=args.max_gap_seconds,
        roi_min_arrivals=args.roi_min_arrivals,
        roi_min_p=args.roi_min_p,
        roi_break_even_p=args.roi_break_even_p,
        reserve_usd_per_mtok=args.reserve_usd_per_mtok,
        claim_limit=args.claim_limit,
        max_pings_per_customer_day=args.max_pings_per_customer_day,
        daily_budget_usd=(float("inf") if args.daily_budget_usd is None
                          else args.daily_budget_usd),
        prefix_ttl_days=args.prefix_ttl_days,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warm_replay_sim.py",
        description=("Replay a cache-warming policy against an arrival trace and "
                     "report exact net dollars as a fraction of the hindsight "
                     "optimum. Analytics only: reads no database, changes nothing."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("trace columns (aliases accepted): ts/timestamp/created_at, "
                "org/organization_id, customer/customer_id, provider, "
                "prefix_hash/warm_prefix_hash, prefix_tokens/cached_tokens, model"))
    src = parser.add_argument_group("trace source")
    src.add_argument("--trace", help="JSON or CSV arrival trace")
    src.add_argument("--synthetic", default="",
                     help=f"comma-separated built-ins: {','.join(SCENARIOS)}")
    src.add_argument("--list-scenarios", action="store_true")
    src.add_argument("--seed", type=int, default=0, help="synthetic generator seed")

    pol = parser.add_argument_group("policies")
    pol.add_argument("--policy", default="never-warm,v1heuristic",
                     help=f"comma-separated: {','.join(POLICIES)}")

    phy = parser.add_argument_group("provider physics (repeatable provider=value)")
    phy.add_argument("--provider-ttl", action="append", metavar="P=SECONDS")
    phy.add_argument("--read-fraction", action="append", metavar="P=FRACTION")
    phy.add_argument("--write-multiplier", action="append", metavar="P=MULT")
    phy.add_argument("--input-price", action="append", metavar="P=USD_PER_MTOK")

    knob = parser.add_argument_group("v1 policy knobs (defaults = shipped worker)")
    knob.add_argument("--tick-seconds", type=int, default=int(V1_DEFAULTS["tick_seconds"]))
    knob.add_argument("--safety-margin-seconds", type=int,
                      default=int(V1_DEFAULTS["safety_margin_seconds"]))
    knob.add_argument("--stop-loss", type=int, default=int(V1_DEFAULTS["stop_loss"]))
    knob.add_argument("--max-gap-seconds", type=int,
                      default=int(V1_DEFAULTS["max_gap_seconds"]))
    knob.add_argument("--roi-min-arrivals", type=int,
                      default=int(V1_DEFAULTS["roi_min_arrivals"]))
    knob.add_argument("--roi-min-p", type=float, default=V1_DEFAULTS["roi_min_p"])
    knob.add_argument("--roi-break-even-p", type=float,
                      default=V1_DEFAULTS["roi_break_even_p"])
    knob.add_argument("--reserve-usd-per-mtok", type=float,
                      default=V1_DEFAULTS["reserve_usd_per_mtok"])
    knob.add_argument("--claim-limit", type=int, default=int(V1_DEFAULTS["claim_limit"]))
    knob.add_argument("--max-pings-per-customer-day", type=int,
                      default=int(V1_DEFAULTS["max_pings_per_customer_day"]))
    knob.add_argument("--daily-budget-usd", type=float, default=None,
                      help="per (org, provider, day); default unbounded")
    knob.add_argument("--prefix-ttl-days", type=int,
                      default=int(V1_DEFAULTS["prefix_ttl_days"]))

    out = parser.add_argument_group("output")
    out.add_argument("--json", action="store_true", help="machine-readable output")
    out.add_argument("--report", default="summary", choices=("summary", "learning"),
                     help=("'learning' splits a warmup window off the front and "
                           "reports evaluation-window money plus what the learned "
                           "scheduler inferred per customer"))
    out.add_argument("--warmup-days", type=int, default=WARMUP_DAYS,
                     help="length of the excluded warmup window (--report learning)")
    out.add_argument("--selftest", action="store_true",
                     help=("run synthetic scenarios with asserted invariants. "
                           "The universal invariants hold under any knobs; the "
                           "scenario economics are asserted against DEFAULT "
                           "physics, so overriding TTL/pricing may fail them "
                           "legitimately"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _main(argv)
    except (KeyError, ValueError) as exc:
        # Trace/config problems are user error, not a stack trace. Physics gaps
        # are the common one: a trace can name any provider, and a wrong price
        # sheet would silently produce confident, wrong dollars.
        print(f"warm_replay_sim: {exc}", file=sys.stderr)
        return 2


def _main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_scenarios:
        for name, fn in SCENARIOS.items():
            doc = (fn.__doc__ or "").strip().splitlines()[0]
            print(f"{name:<10} {doc}")
        return 0

    if args.selftest:
        return selftest(lambda: build_config(args))

    policy_names = [p.strip() for p in args.policy.split(",") if p.strip()]
    cfg = build_config(args)

    if args.trace:
        arrivals = load_trace(args.trace)
        payload = evaluate(arrivals, cfg, policy_names)
        if args.report == "learning" and not args.json:
            print(render_learning(payload, title=args.trace,
                                  warmup_days=args.warmup_days))
        payload.pop("_objects", None)
        print(json.dumps(payload, indent=2) if args.json
              else render(payload, title=args.trace))
        return 0

    names = [s.strip() for s in args.synthetic.split(",") if s.strip()] or list(SCENARIOS)
    bundle: list[dict[str, Any]] = []
    for name in names:
        if name not in SCENARIOS:
            parser.error(f"unknown scenario {name!r}; known: {','.join(SCENARIOS)}")
        payload = evaluate(SCENARIOS[name](args.seed), build_config(args), policy_names)
        if args.report == "learning" and not args.json:
            print(render_learning(payload, title=f"synthetic scenario '{name}'",
                                  warmup_days=args.warmup_days))
        payload.pop("_objects", None)
        payload["scenario"] = name
        bundle.append(payload)
        if not args.json:
            print(render(payload, title=f"synthetic scenario '{name}'"))
    if args.json:
        print(json.dumps({"schema": SCHEMA, "scenarios": bundle}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
