"""cacheledger — what is prompt caching actually doing to your bill?

Every provider bills cache reads, cache writes and fresh input at different
rates and hands you the counts in every response. Almost nobody adds them up.
This reads your own receipts and answers four questions that a dashboard cannot:

  1. REALIZED HIT RATE, token-weighted -- not request-weighted, which flatters
     you, because the requests that miss are the expensive ones.
  2. THE COUNTERFACTUAL -- what the same traffic would have cost with caching
     off. This is the only honest form of "caching saved us X".
  3. WASTED WRITE PREMIUM -- writes you paid the 1.25x/2x premium on and never
     read back. Marking a prefix that is never reused costs MORE than not
     caching it. This is the single most actionable number here and no vendor
     reports it.
  4. THE TTL CLIFF, in situ -- prefix recovery as a function of the gap since
     your last request, which is what decides whether the longer TTL tier is
     worth its higher write multiplier for YOUR arrival pattern.

WHAT IT REFUSES TO DO, and why each refusal is load-bearing:

  * IT WILL NOT GUESS PRICES. `--rates` is required. Rates differ per model,
    per tier and per provider, they change, and a wrong rate card turns every
    dollar figure into fiction while looking exactly as authoritative. Token
    counts are the evidence; dollars are a derived figure under a rate card you
    supplied and which is echoed in the output.
  * IT WILL NOT INFER A SESSION BOUNDARY. Cache locality is a property of who
    shares a prefix. If your receipts carry no session id, say so with
    `--single-session` rather than letting the tool invent one.
  * IT DEDUPES ON REQUEST ID and says how many it dropped. Agent transcripts
    replay the same turn, and each replay carries the same provider receipt.
  * IT WILL NOT REPORT A HIT RATE OVER TOO FEW TOKENS. Below --min-tokens the
    ratio is noise and is refused rather than printed with a caveat nobody
    reads.

Exit codes:  0 ok  |  1 a threshold you set was breached  |  3 refused to measure
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime

SCHEMA_DOC = """Required per-receipt fields (this is Anthropic's own usage shape,
so an Anthropic user can pipe raw logs straight in):

  {"ts": "2026-08-27T04:00:00Z",        # ISO 8601, required for the TTL cliff
   "session": "abc123",                 # or pass --single-session
   "request_id": "req_01ABC",           # optional; used to drop replayed turns
   "model": "claude-haiku-4-5",
   "input_tokens": 12,                  # fresh, uncached
   "cache_read_input_tokens": 48000,
   "cache_creation_input_tokens": 0,
   "output_tokens": 300,
   "cache_creation": {                  # optional; enables the tier split
     "ephemeral_5m_input_tokens": 0,
     "ephemeral_1h_input_tokens": 0}}
"""


class Refuse(RuntimeError):
    """Anything the tool will not guess. Always exits 3."""


@dataclass
class Receipt:
    ts: float | None
    request_id: str
    session: str
    model: str
    inp: int
    read: int
    create: int
    create_5m: int
    create_1h: int
    out: int

    @property
    def prefix(self) -> int:
        """Prompt-side tokens only. Output is billed separately and mixing them
        into a 'hit rate' is the most common way this number gets inflated."""
        return self.inp + self.read + self.create


def _ts(s) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:                                              # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Adapters. Each one maps a real log format onto Receipt and nothing else --
# no arithmetic happens here, so a broken adapter cannot quietly change a result.
# --------------------------------------------------------------------------

def from_anthropic(d: dict, single: str | None) -> Receipt | None:
    u = d.get("usage") or d
    cc = u.get("cache_creation") or {}
    r = Receipt(
        ts=_ts(d.get("ts") or d.get("timestamp")),
        request_id=str(d.get("request_id") or d.get("requestId") or ""),
        session=str(d.get("session") or single or ""),
        model=str(d.get("model") or "unknown"),
        inp=int(u.get("input_tokens") or 0),
        read=int(u.get("cache_read_input_tokens") or 0),
        create=int(u.get("cache_creation_input_tokens") or 0),
        create_5m=int(cc.get("ephemeral_5m_input_tokens") or 0),
        create_1h=int(cc.get("ephemeral_1h_input_tokens") or 0),
        out=int(u.get("output_tokens") or 0),
    )
    return r if r.prefix else None


def from_openai(d: dict, single: str | None) -> Receipt | None:
    """OpenAI reports cached_tokens as a SUBSET of prompt_tokens, not alongside
    it. Adding them the Anthropic way double-counts the prefix and inflates the
    hit rate; this is the single most likely mistake in a cross-provider tool."""
    u = d.get("usage") or d
    prompt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    if cached > prompt:
        raise Refuse(
            f"cached_tokens ({cached}) exceeds prompt_tokens ({prompt}). For "
            f"OpenAI, cached_tokens is a SUBSET of prompt_tokens; this looks "
            f"like Anthropic-shaped data fed through --from openai.")
    r = Receipt(
        ts=_ts(d.get("ts") or d.get("created")),
        request_id=str(d.get("request_id") or d.get("id") or ""),
        session=str(d.get("session") or single or ""),
        model=str(d.get("model") or "unknown"),
        inp=prompt - cached,
        read=cached,
        create=0,          # OpenAI does not bill a separate write
        create_5m=0, create_1h=0,
        out=int(u.get("completion_tokens") or u.get("output_tokens") or 0),
    )
    return r if r.prefix else None


def from_claude_code(d: dict, single: str | None) -> Receipt | None:
    if d.get("type") != "assistant":
        return None
    m = d.get("message") or {}
    if not (m.get("usage")):
        return None
    return from_anthropic(
        {"ts": d.get("timestamp"), "session": d.get("sessionId") or single,
         "request_id": d.get("requestId"),
         "model": m.get("model"), "usage": m["usage"]}, single)


ADAPTERS = {"anthropic": from_anthropic, "openai": from_openai,
            "claude-code": from_claude_code}


def load(paths: list[str], fmt: str, single: str | None) -> list[Receipt]:
    fn = ADAPTERS[fmt]
    out, bad = [], 0
    for p in paths:
        with open(p, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:                                  # noqa: BLE001
                    bad += 1
                    continue
                r = fn(d, single)
                if r:
                    out.append(r)
    # DEDUPE ON REQUEST ID.
    # Found by cross-checking against an independent implementation on the same
    # corpus: the two disagreed by 2.1x on absolute tokens while agreeing to
    # 0.5pp on the hit rate. Agent transcripts write the same assistant turn
    # more than once (streaming, resume, replay), and each copy carries the SAME
    # provider receipt -- so a naive reader bills you twice for one request.
    # Ratios survive duplication; dollars do not, and dollars are the output.
    if any(r.request_id for r in out):
        seen, deduped, dupes = set(), [], 0
        for r in out:
            if r.request_id and r.request_id in seen:
                dupes += 1
                continue
            if r.request_id:
                seen.add(r.request_id)
            deduped.append(r)
        if dupes:
            print(f"  note: dropped {dupes:,} duplicate receipt(s) sharing a "
                  f"request id with an earlier one ({dupes / len(out):.1%} of "
                  f"the input)", file=sys.stderr)
        out = deduped
    if bad and bad > len(out):
        raise Refuse(
            f"{bad} unparseable lines against {len(out)} usable receipts. That "
            f"is not a log in this format. Expected JSONL.\n\n{SCHEMA_DOC}")
    return out


# --------------------------------------------------------------------------
# The arithmetic. Every quantity below is a ratio of token counts that came
# from the provider's own receipt; nothing is modelled or simulated.
# --------------------------------------------------------------------------

def analyse(rs: list[Receipt], rates: dict, min_tokens: int) -> dict:
    if not rs:
        raise Refuse("no usable receipts found")
    prefix = sum(r.prefix for r in rs)
    if prefix < min_tokens:
        raise Refuse(
            f"only {prefix:,} prompt tokens across {len(rs)} receipts; below "
            f"--min-tokens {min_tokens:,} the hit rate is noise. Refusing "
            f"rather than printing a ratio with a caveat.")
    read = sum(r.read for r in rs)
    create = sum(r.create for r in rs)
    fresh = sum(r.inp for r in rs)

    def price(model: str, kind: str) -> float:
        m = rates.get(model) or rates.get("default")
        if m is None or kind not in m:
            raise Refuse(
                f"no {kind!r} rate for model {model!r} in the rate card. Add it, "
                f"or add a 'default' entry. Prices are not guessed: a wrong rate "
                f"makes every dollar below fiction while looking authoritative.")
        return float(m[kind]) / 1e6

    billed = sum(r.read * price(r.model, "read")
                 + r.create_5m * price(r.model, "write_5m")
                 + r.create_1h * price(r.model, "write_1h")
                 + (r.create - r.create_5m - r.create_1h) * price(r.model, "write_5m")
                 + r.inp * price(r.model, "input") for r in rs)
    nocache = sum(r.prefix * price(r.model, "input") for r in rs)

    # WASTED WRITE PREMIUM. A write is only worth its premium if something reads
    # it back. Per session, tokens written beyond what was ever read are premium
    # paid for nothing -- and at a 1.25x/2x multiplier that is strictly worse
    # than not caching. Computed per session because a read in someone else's
    # session cannot recover your write.
    by_sess: dict[str, list[Receipt]] = {}
    for r in rs:
        by_sess.setdefault(r.session, []).append(r)
    wasted_tokens, wasted_cost, n_wasteful = 0, 0.0, 0
    for sid, rows in by_sess.items():
        w = sum(x.create for x in rows)
        rd = sum(x.read for x in rows)
        if w > rd:
            excess = w - rd
            wasted_tokens += excess
            n_wasteful += 1
            # the PREMIUM only, not the whole write: you would have paid input
            # rate for these tokens anyway.
            mdl = rows[0].model
            wasted_cost += excess * (price(mdl, "write_5m") - price(mdl, "input"))

    # TTL CLIFF. Consecutive requests in a session: did the prefix survive the
    # gap? A request that reads ~0 after a long gap re-paid for the whole prefix.
    buckets = [(0, 300), (300, 1800), (1800, 3600), (3600, 7200), (7200, 10**9)]
    cliff = []
    for lo, hi in buckets:
        pairs, lost = 0, 0
        for rows in by_sess.values():
            rows = sorted([x for x in rows if x.ts is not None], key=lambda x: x.ts)
            for a, b in zip(rows, rows[1:]):
                gap = b.ts - a.ts
                if not (lo <= gap < hi):
                    continue
                pairs += 1
                if b.read == 0 and b.create > 0:
                    lost += 1
        if pairs:
            cliff.append({"gap_lo_s": lo, "gap_hi_s": hi, "pairs": pairs,
                          "lost": lost, "loss_rate": lost / pairs})

    gaps = []
    for rows in by_sess.values():
        rows = sorted([x for x in rows if x.ts is not None], key=lambda x: x.ts)
        gaps += [b.ts - a.ts for a, b in zip(rows, rows[1:])]

    return {
        "_rate_card": rates,
        "_provenance": "every count is read from a provider receipt; dollars are "
                       "derived under the rate card echoed above",
        "n_receipts": len(rs), "n_sessions": len(by_sess),
        "prompt_tokens": prefix, "cache_read_tokens": read,
        "cache_write_tokens": create, "fresh_input_tokens": fresh,
        "hit_rate": read / prefix,
        "billed_usd": billed, "no_cache_counterfactual_usd": nocache,
        "saving_usd": nocache - billed,
        "saving_pct": (nocache - billed) / nocache if nocache else 0.0,
        "wasted_write_premium_usd": wasted_cost,
        "wasted_write_tokens": wasted_tokens,
        "sessions_writing_more_than_they_read": n_wasteful,
        "tier_1h_share_of_writes": (sum(r.create_1h for r in rs) / create) if create else None,
        "median_gap_s": statistics.median(gaps) if gaps else None,
        "ttl_cliff": cliff,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("logs", nargs="+", help="JSONL receipt files")
    ap.add_argument("--from", dest="fmt", required=True, choices=sorted(ADAPTERS),
                    help="log format. 'anthropic' is the native schema; "
                         "--schema prints it")
    ap.add_argument("--rates", required=True,
                    help="JSON rate card, $/Mtok, keyed by model (or 'default') "
                         "with keys input/read/write_5m/write_1h. REQUIRED: a "
                         "wrong price makes every dollar here fiction.")
    ap.add_argument("--single-session", default=None,
                    help="name to use when receipts carry no session id")
    ap.add_argument("--min-tokens", type=int, default=1_000_000)
    ap.add_argument("--min-hit-rate", type=float, default=None,
                    help="exit 1 if the realized hit rate falls below this")
    ap.add_argument("--max-wasted-usd", type=float, default=None,
                    help="exit 1 if wasted write premium exceeds this")
    ap.add_argument("--json", default=None)
    ap.add_argument("--schema", action="store_true")
    a = ap.parse_args()

    if a.schema:
        print(SCHEMA_DOC)
        return 0
    try:
        rates = json.load(open(a.rates))
        rs = load(a.logs, a.fmt, a.single_session)
        if not a.single_session and sum(1 for r in rs if not r.session) > len(rs) // 2:
            raise Refuse(
                "most receipts carry no session id. Cache locality is a property "
                "of who shares a prefix, so this is not inferred -- pass "
                "--single-session NAME if they genuinely are one session.")
        res = analyse(rs, rates, a.min_tokens)
    except Refuse as e:
        print(f"cacheledger: REFUSING to measure — {e}", file=sys.stderr)
        return 3
    except FileNotFoundError as e:
        print(f"cacheledger: REFUSING to measure — {e}", file=sys.stderr)
        return 3

    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=1)
    print(f"  receipts           {res['n_receipts']:,} in {res['n_sessions']:,} session(s)")
    print(f"  prompt tokens      {res['prompt_tokens']:,}")
    print(f"    cache read       {res['cache_read_tokens']:,}  "
          f"({res['hit_rate']:.2%} realized hit rate, token-weighted)")
    print(f"    cache write      {res['cache_write_tokens']:,}")
    print(f"    fresh input      {res['fresh_input_tokens']:,}")
    if res["tier_1h_share_of_writes"] is not None:
        print(f"  1h tier share      {res['tier_1h_share_of_writes']:.1%} of writes")
    print(f"  billed             ${res['billed_usd']:,.2f}")
    print(f"  without caching    ${res['no_cache_counterfactual_usd']:,.2f}"
          f"   -> saved {res['saving_pct']:.1%} (${res['saving_usd']:,.2f})")
    print(f"  WASTED WRITE       ${res['wasted_write_premium_usd']:,.2f} premium on "
          f"{res['wasted_write_tokens']:,} tokens never read back, in "
          f"{res['sessions_writing_more_than_they_read']} session(s)")
    if res["ttl_cliff"]:
        print("  TTL cliff          gap(s)          pairs   lost   loss rate")
        for c in res["ttl_cliff"]:
            hi = "inf" if c["gap_hi_s"] > 10**8 else f"{c['gap_hi_s']}"
            print(f"                     {c['gap_lo_s']:>7}-{hi:<8} "
                  f"{c['pairs']:>6} {c['lost']:>6}   {c['loss_rate']:>7.1%}")

    rc = 0
    if a.min_hit_rate is not None and res["hit_rate"] < a.min_hit_rate:
        print(f"  FAIL hit rate {res['hit_rate']:.2%} < --min-hit-rate "
              f"{a.min_hit_rate:.2%}", file=sys.stderr)
        rc = 1
    if a.max_wasted_usd is not None and res["wasted_write_premium_usd"] > a.max_wasted_usd:
        print(f"  FAIL wasted write premium ${res['wasted_write_premium_usd']:,.2f} "
              f"> --max-wasted-usd ${a.max_wasted_usd:,.2f}", file=sys.stderr)
        rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
