#!/usr/bin/env python3
"""Field measurement of prompt-cache behaviour from real coding-agent traffic.

Reads Claude Code session transcripts, which carry the provider's OWN usage
receipt for every assistant turn:

    {"input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
     "output_tokens", "cache_creation": {"ephemeral_5m_input_tokens",
                                         "ephemeral_1h_input_tokens"}}

Nothing here is synthetic and nothing is simulated: every token count below was
billed by Anthropic against a real session. This is the "field f" measurement
that `docs/DOES_AI_NATIVE_REDIS_WORK.md` L0 says is a query away, and the real
arrival distribution that `scripts/warm_replay_sim.py` currently substitutes
`random.Random` for.

Outputs, per workload and pooled:
  1. Realized hit rate  = read / (read + create + fresh_input)   [token-weighted]
  2. Mechanism split    = within-session continuation vs cross-session cold open
  3. TTL cliff in situ  = P(next request is a re-create | inter-request gap)
  4. Cliff cost         = dollars re-paid to recreate prefixes that had expired
  5. Tier usage         = 5m vs 1h ephemeral writes, as chosen by a shipped agent

Usage:
    .venv/bin/python scripts/field_cache_measure.py [--roots DIR ...] [--json OUT]

Read-only. Touches no network and no API key.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

# Anthropic list price, $/Mtok. Sonnet/Opus-class rates are model dependent; we
# report token counts as primary evidence and dollars only as a derived figure
# under an explicitly stated rate card, because no invoice has been reconciled.
PRICE = {
    "input": 3.00,
    "cache_read": 0.30,      # 0.10x
    "cache_write_5m": 3.75,  # 1.25x
    "cache_write_1h": 6.00,  # 2.00x
    "output": 15.00,
}
RATE_CARD_NOTE = (
    "Sonnet-class list price; dollars are rate-card arithmetic over vendor-reported "
    "counters and are NOT reconciled against a settled invoice."
)


@dataclass
class Turn:
    session: str
    project: str
    ts: float
    model: str
    request_id: str
    inp: int
    read: int
    create: int
    create_5m: int
    create_1h: int
    out: int

    @property
    def prefix_tokens(self) -> int:
        """Total prompt tokens the provider saw for this request."""
        return self.inp + self.read + self.create

    @property
    def is_hit(self) -> bool:
        return self.read > 0


@dataclass
class Session:
    session: str
    project: str
    turns: list = field(default_factory=list)


def _ts(s: str) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def load_turns(roots: list[str]) -> list[Session]:
    sessions: dict[str, Session] = {}
    for root in roots:
        for path in sorted(glob.glob(os.path.join(root, "*", "*.jsonl"))):
            # subagent transcripts are a different workload (fan-out children);
            # keep them out of the main-loop population and note the exclusion.
            if os.sep + "subagents" + os.sep in path:
                continue
            project = os.path.basename(os.path.dirname(path))
            sid = os.path.splitext(os.path.basename(path))[0]
            try:
                fh = open(path, "r", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    m = d.get("message") or {}
                    u = m.get("usage") or {}
                    if not u:
                        continue
                    cc = u.get("cache_creation") or {}
                    t = Turn(
                        session=sid,
                        project=project,
                        ts=_ts(d.get("timestamp") or ""),
                        model=str(m.get("model") or "unknown"),
                        request_id=str(d.get("requestId") or ""),
                        inp=int(u.get("input_tokens") or 0),
                        read=int(u.get("cache_read_input_tokens") or 0),
                        create=int(u.get("cache_creation_input_tokens") or 0),
                        create_5m=int(cc.get("ephemeral_5m_input_tokens") or 0),
                        create_1h=int(cc.get("ephemeral_1h_input_tokens") or 0),
                        out=int(u.get("output_tokens") or 0),
                    )
                    if t.prefix_tokens == 0:
                        continue
                    sessions.setdefault(sid, Session(sid, project)).turns.append(t)

    out = []
    for s in sessions.values():
        # One assistant record per streamed message; dedupe by requestId so a
        # retried/continued request is not double counted.
        seen: set[str] = set()
        uniq = []
        for t in sorted(s.turns, key=lambda x: x.ts):
            key = t.request_id or f"{t.ts}:{t.prefix_tokens}"
            if key in seen:
                continue
            seen.add(key)
            uniq.append(t)
        s.turns = uniq
        if s.turns:
            out.append(s)
    return out


def hit_rate(turns: list[Turn]) -> dict:
    read = sum(t.read for t in turns)
    create = sum(t.create for t in turns)
    fresh = sum(t.inp for t in turns)
    total = read + create + fresh
    return {
        "prompt_tokens_total": total,
        "cache_read": read,
        "cache_create": create,
        "fresh_input": fresh,
        # token-weighted realized hit rate: the fraction of prompt tokens that
        # were billed at the 0.10x read rate.
        "realized_hit_rate": (read / total) if total else 0.0,
    }


def cost(turns: list[Turn]) -> dict:
    m = 1e-6
    c_read = sum(t.read for t in turns) * PRICE["cache_read"] * m
    c_w5 = sum(t.create_5m for t in turns) * PRICE["cache_write_5m"] * m
    c_w1 = sum(t.create_1h for t in turns) * PRICE["cache_write_1h"] * m
    # writes not attributed to a tier fall back to the 5m rate
    untier = sum(max(0, t.create - t.create_5m - t.create_1h) for t in turns)
    c_wu = untier * PRICE["cache_write_5m"] * m
    c_in = sum(t.inp for t in turns) * PRICE["input"] * m
    c_out = sum(t.out for t in turns) * PRICE["output"] * m
    billed = c_read + c_w5 + c_w1 + c_wu + c_in + c_out
    # counterfactual: same traffic with no caching at all — every prompt token
    # billed at the full input rate.
    c_nocache = sum(t.prefix_tokens for t in turns) * PRICE["input"] * m + c_out
    return {
        "billed_usd": billed,
        "nocache_usd": c_nocache,
        "saving_usd": c_nocache - billed,
        "saving_pct": ((c_nocache - billed) / c_nocache * 100.0) if c_nocache else 0.0,
        "write_usd": c_w5 + c_w1 + c_wu,
        "read_usd": c_read,
    }


# Gap buckets straddling the measured 5m cliff (300-330s) and the 1h tier.
GAP_BUCKETS = [
    (0, 30), (30, 60), (60, 120), (120, 240), (240, 300),
    (300, 330), (330, 420), (420, 600), (600, 1200), (1200, 1800),
    (1800, 3600), (3600, 7200), (7200, 86400), (86400, math.inf),
]


def ttl_cliff(sessions: list[Session]) -> dict:
    """Field observation of the TTL cliff, via prefix RECOVERY.

    In a growing agent conversation every request writes new cache (the latest
    turn) while reading the older prefix back, so `cache_creation > 0` is NOT a
    miss indicator -- it is the steady state. The signal that actually tracks
    expiry is how much of the PREVIOUS request's prompt the current one managed
    to read back:

        recovery = cache_read_input_tokens(cur) / prompt_tokens(prev)

    ~1.0 means the prefix survived the gap; ~0.0 means it had to be re-paid at
    the write rate. We report the recovery distribution and the outright-loss
    rate (recovery < 0.05) per gap bucket.

    Caveat stated up front: this is observational, not a controlled probe.
    Recovery can also drop because the agent edited earlier context (a cache
    invalidation rather than an expiry), so the loss rate is an UPPER bound on
    expiry. It is nonetheless a direct field measurement of what the cliff costs
    a real workload, which no controlled probe can supply.
    """
    buckets = {f"{a}-{b}": {"pairs": 0, "lost": 0, "recoveries": [],
                            "reprice_tokens": 0}
               for a, b in GAP_BUCKETS}

    def bucket_of(g: float) -> str | None:
        for a, b in GAP_BUCKETS:
            if a <= g < b:
                return f"{a}-{b}"
        return None

    for s in sessions:
        for prev, cur in zip(s.turns, s.turns[1:]):
            if prev.ts <= 0 or cur.ts <= 0:
                continue
            gap = cur.ts - prev.ts
            if gap < 0 or prev.prefix_tokens <= 0:
                continue
            key = bucket_of(gap)
            if key is None:
                continue
            b = buckets[key]
            b["pairs"] += 1
            rec = min(1.0, cur.read / prev.prefix_tokens)
            b["recoveries"].append(rec)
            if rec < 0.05:
                b["lost"] += 1
                # tokens the workload had already paid to cache and re-paid
                b["reprice_tokens"] += prev.prefix_tokens

    for k, b in buckets.items():
        recs = b.pop("recoveries")
        b["loss_rate"] = (b["lost"] / b["pairs"]) if b["pairs"] else None
        b["median_recovery"] = statistics.median(recs) if recs else None
        b["mean_recovery"] = (sum(recs) / len(recs)) if recs else None
    return buckets


def mechanism_split(sessions: list[Session]) -> dict:
    """Split cache reads into within-session continuation vs session-opening.

    The first billed request of a session cannot be reading a prefix this
    session wrote, so any read on it came from OTHER traffic sharing the
    credential -- the cross-session / cross-agent mechanism. Every later read is
    within-session growing-prefix continuation.
    """
    first_read = first_total = 0
    later_read = later_total = 0
    sessions_with_cold_read = 0
    for s in sessions:
        if not s.turns:
            continue
        f = s.turns[0]
        first_read += f.read
        first_total += f.prefix_tokens
        if f.read > 0:
            sessions_with_cold_read += 1
        for t in s.turns[1:]:
            later_read += t.read
            later_total += t.prefix_tokens
    all_read = first_read + later_read
    return {
        "sessions": len(sessions),
        "sessions_with_cold_open_read": sessions_with_cold_read,
        "cold_open_read_tokens": first_read,
        "cold_open_prompt_tokens": first_total,
        "cold_open_hit_rate": (first_read / first_total) if first_total else 0.0,
        "continuation_read_tokens": later_read,
        "continuation_prompt_tokens": later_total,
        "continuation_hit_rate": (later_read / later_total) if later_total else 0.0,
        # This is the headline decomposition: what share of ALL cache value came
        # from the cross-session mechanism the savings literature markets?
        "share_of_reads_from_cold_open": (first_read / all_read) if all_read else 0.0,
    }


def tier_usage(turns: list[Turn]) -> dict:
    w5 = sum(t.create_5m for t in turns)
    w1 = sum(t.create_1h for t in turns)
    tot = w5 + w1
    return {
        "write_5m_tokens": w5,
        "write_1h_tokens": w1,
        "share_1h": (w1 / tot) if tot else 0.0,
    }


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - k) + xs[hi] * (k - lo)


def gap_distribution(sessions: list[Session]) -> dict:
    gaps = []
    for s in sessions:
        for prev, cur in zip(s.turns, s.turns[1:]):
            if prev.ts > 0 and cur.ts > 0 and cur.ts >= prev.ts:
                gaps.append(cur.ts - prev.ts)
    if not gaps:
        return {}
    return {
        "n_gaps": len(gaps),
        "median_s": statistics.median(gaps),
        "p75_s": pct(gaps, 0.75),
        "p90_s": pct(gaps, 0.90),
        "p99_s": pct(gaps, 0.99),
        # The three regions that decide whether warming can ever pay:
        "P_gap_le_300s": sum(1 for g in gaps if g <= 300) / len(gaps),
        "P_gap_300_3600s": sum(1 for g in gaps if 300 < g <= 3600) / len(gaps),
        "P_gap_gt_3600s": sum(1 for g in gaps if g > 3600) / len(gaps),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="*", default=[
        os.path.expanduser("~/.claude/projects"),
    ])
    ap.add_argument("--json", default=None, help="write full result as JSON")
    args = ap.parse_args()

    sessions = load_turns(args.roots)
    turns = [t for s in sessions for t in s.turns]
    if not turns:
        print("no billed turns found")
        return 1

    by_model = defaultdict(list)
    for t in turns:
        by_model[t.model].append(t)

    result = {
        "rate_card_note": RATE_CARD_NOTE,
        "n_sessions": len(sessions),
        "n_billed_requests": len(turns),
        "pooled": {
            **hit_rate(turns),
            **cost(turns),
            **tier_usage(turns),
        },
        "mechanism_split": mechanism_split(sessions),
        "gap_distribution": gap_distribution(sessions),
        "ttl_cliff": ttl_cliff(sessions),
        "by_model": {
            m: {**hit_rate(ts), **cost(ts), **tier_usage(ts), "n_requests": len(ts)}
            for m, ts in sorted(by_model.items(), key=lambda kv: -len(kv[1]))
        },
        "per_session_hit_rate": {
            "median": statistics.median(
                [hit_rate(s.turns)["realized_hit_rate"] for s in sessions]),
            "p10": pct([hit_rate(s.turns)["realized_hit_rate"] for s in sessions], 0.10),
            "p90": pct([hit_rate(s.turns)["realized_hit_rate"] for s in sessions], 0.90),
        },
    }

    p = result["pooled"]
    ms = result["mechanism_split"]
    gd = result["gap_distribution"]
    print(f"sessions={result['n_sessions']}  billed_requests={result['n_billed_requests']}")
    print(f"prompt tokens         {p['prompt_tokens_total']:,}")
    print(f"  cache_read          {p['cache_read']:,}  ({p['realized_hit_rate']*100:.2f}% realized hit rate)")
    print(f"  cache_create        {p['cache_create']:,}")
    print(f"  fresh input         {p['fresh_input']:,}")
    print(f"1h tier share of writes {p['share_1h']*100:.1f}%")
    print(f"billed ${p['billed_usd']:.2f} vs no-cache ${p['nocache_usd']:.2f}"
          f"  -> caching saved {p['saving_pct']:.1f}% (${p['saving_usd']:.2f})")
    print()
    print("MECHANISM SPLIT")
    print(f"  continuation (within-session) hit rate {ms['continuation_hit_rate']*100:.2f}%")
    print(f"  cold-open (cross-session)     hit rate {ms['cold_open_hit_rate']*100:.2f}%")
    print(f"  share of ALL reads from cold open      {ms['share_of_reads_from_cold_open']*100:.2f}%")
    print(f"  sessions whose first request read cache: "
          f"{ms['sessions_with_cold_open_read']}/{ms['sessions']}")
    print()
    if gd:
        print("INTER-REQUEST GAP (the distribution warming economics turns on)")
        print(f"  median {gd['median_s']:.1f}s  p90 {gd['p90_s']:.1f}s  p99 {gd['p99_s']:.1f}s")
        print(f"  P(gap<=300s)={gd['P_gap_le_300s']*100:.1f}%  "
              f"P(300s<gap<=1h)={gd['P_gap_300_3600s']*100:.1f}%  "
              f"P(gap>1h)={gd['P_gap_gt_3600s']*100:.1f}%")
    print()
    print("TTL CLIFF IN SITU (prefix recovery by inter-request gap)")
    print(f"  {'gap (s)':>14}  {'pairs':>6}  {'med recov':>9}  {'lost':>5}  {'loss rate':>9}")
    for k, b in result["ttl_cliff"].items():
        if b["pairs"] == 0:
            continue
        print(f"  {k:>14}  {b['pairs']:>6}  {b['median_recovery']*100:>8.1f}%  "
              f"{b['lost']:>5}  {b['loss_rate']*100:>8.1f}%")
    reprice = sum(b["reprice_tokens"] for b in result["ttl_cliff"].values())
    print(f"  tokens re-paid after a lost prefix: {reprice:,} "
          f"(${reprice*PRICE['cache_write_5m']*1e-6:.2f} at the 5m write rate)")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(result, fh, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
