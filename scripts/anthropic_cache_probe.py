#!/usr/bin/env python3
"""Empirical cache-behavior probe against Anthropic's live Messages API.

MANUAL ONLY, AND IT SPENDS REAL MONEY. pytest never imports this file, the
worker never calls it, and it refuses to do anything unless
BREVITAS_ANTHROPIC_PROBE=1 is set explicitly. It loads ONLY the
BREVITAS_PROBE_ANTHROPIC_KEY out of the repo-root .env.local (Brevitas's own
probe account) and hard-asserts that cumulative list-price spend stays under
$5.00, aborting the instant a projected call would cross the cap.

WHAT THIS IS
------------
Behavioral CHARACTERIZATION of Anthropic's prompt cache with our own key and
our own money. Every request is byte-identical, well-formed, and small. We are
measuring documented-but-opaque timing/pricing behavior -- the TTL cliff,
refresh-on-read, the minimum cacheable prefix, breakpoint handling, and the 1h
tier -- using the provider's OWN usage receipt (cache_read_input_tokens vs
cache_creation_input_tokens) as the evidence for every claim.

WHAT THIS IS NOT
----------------
No attempt is made to read or infer another tenant's data, to exploit the
cross-user cache-sharing side channel, to probe or evade rate limits, or to
send malformed/abusive traffic. Synthetic prefixes only, from a seeded word
list, never customer data. If a probe idea edges toward attacking Anthropic's
infrastructure or other tenants, it is SKIPPED, not run.

THE PROBES
----------
FAST (run first, print before spending on the slow sweep):
  P3  MIN CACHEABLE TOKENS  -- sweep prefix sizes around the documented 4096-tok
                              haiku floor; find where cache_creation first appears.
  P4  BREAKPOINT COUNT      -- 1, 4, then 5 cache_control breakpoints; does 5
                              error (400) or silently cap?
SLOW (real wall-clock waits, one shared ~8-minute timeline):
  P1  TTL CLIFF             -- a fan of prefixes each written at t=0 and read
                              EXACTLY ONCE at a staggered gap, so no read
                              refreshes another. The gap where cache_read flips
                              to cache_creation is the raw TTL. A separate
                              single chain (read every ~90s) shows that reads
                              keep an entry alive indefinitely.
  P2  REFRESH-ON-READ       -- R1: write@0, read@240s, read@480s. R2 (control):
                              write@0, read@480s only. If R1@480 is warm and
                              R2@480 is cold, the t=240 read reset the 5-min clock.
  P5  1h TIER (sample)      -- one write with cache_control ttl:"1h", one read at
                              ~360s (past the 5-min default). Confirms the tier is
                              live and outlives the default clock. Not swept.

PREFIX SIZING IS NOT ARBITRARY. Anthropic's minimum cacheable prefix is
MODEL-DEPENDENT and claude-haiku-4-5's is documented at 4096 tokens -- a shorter
prefix silently never caches and reports cache_creation_input_tokens = 0 with no
error. Cacheable-prefix probes are built well above that floor from a seeded word
list; P3 deliberately sweeps THROUGH it to measure the real boundary.

    BREVITAS_ANTHROPIC_PROBE=1 .venv/bin/python scripts/anthropic_cache_probe.py

Writes a machine-readable receipt dump to the scratchpad (or CWD) on completion
AND on a spend-guard abort, so partial data survives an early stop.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# guard rails
# --------------------------------------------------------------------------

ACK_ENV = "BREVITAS_ANTHROPIC_PROBE"
SPEND_CAP_USD = 5.00

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env.local")
# The ONLY key this script is allowed to read out of .env.local. Anything else
# in that file (production creds, service-role keys, Stripe) is never parsed.
ALLOWED_ENV_PREFIX = "BREVITAS_PROBE_"
PROBE_KEY_NAME = "BREVITAS_PROBE_ANTHROPIC_KEY"

# claude-haiku-4-5 is the cheapest PRICED anthropic model in the catalog. Prices
# are $/Mtok, copied verbatim from brevitas/receipts.py MODEL_PRICES and kept as
# literals so a mid-run repricing cannot perturb the cap arithmetic. These are
# marginally ABOVE the current public haiku list ($0.80/$4.00), so pricing here
# is conservative for the spend cap (it over-books, never under-books).
MODEL = "claude-haiku-4-5"
PRICES = {"input": 1.0, "cached": 0.1, "write": 1.25, "output": 5.0}
# A 1h-tier cache WRITE is priced at 2x base (vs 1.25x for the 5m tier).
WRITE_MULT_1H = 2.0

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
COUNT_URL = "https://api.anthropic.com/v1/messages/count_tokens"
API_VERSION = "2023-06-01"
# 1h cache TTL shipped behind this beta flag; harmless if the tier is now GA.
BETA_1H = "extended-cache-ttl-2025-04-11"

PREFIX_SEED = 20260810
# ~5,200 words -> comfortably north of haiku's 4096-token floor, one write still
# well under a cent. Used by every probe that must actually cache.
BIG_WORDS = 5_200

_VOCAB = (
    "ledger receipt invoice settlement reconcile envelope threshold cadence "
    "throughput latency payload cursor ledgered upstream downstream provider "
    "tenant workspace boundary contract predicate invariant migration rollout "
    "canary probe hazard exposure posterior shrinkage estimator baseline "
    "counterfactual attribution admission budget reservation claim token "
    "prefix suffix bucket window horizon interval schedule dispatcher worker "
    "queue backlog retention checkpoint transcript identifier namespace "
    "quorum replica partition durable idempotent monotonic deterministic "
    "observable measurable auditable reversible bounded amortized "
    "instrument calibrate normalize aggregate summarize verify attest record"
).split()


# --------------------------------------------------------------------------
# receipts
# --------------------------------------------------------------------------

@dataclass
class Call:
    """One live request and the receipt it came back with."""

    probe: str
    label: str
    at_s: float = 0.0
    status: int = 0
    usage: dict = field(default_factory=dict)
    spend_usd: float = 0.0
    error: str = ""
    note: str = ""

    @property
    def cache_read(self) -> int:
        return int(self.usage.get("cache_read_input_tokens") or 0)

    @property
    def cache_write(self) -> int:
        return int(self.usage.get("cache_creation_input_tokens") or 0)

    @property
    def fresh_input(self) -> int:
        return int(self.usage.get("input_tokens") or 0)

    @property
    def prefix_tokens(self) -> int:
        return self.fresh_input + self.cache_write + self.cache_read


def load_probe_key() -> str | None:
    """Read ONLY BREVITAS_PROBE_ANTHROPIC_KEY out of .env.local. Env wins."""
    found = ""
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                name = name.strip()
                if not name.startswith(ALLOWED_ENV_PREFIX):
                    continue
                if name == PROBE_KEY_NAME:
                    found = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return os.environ.get(PROBE_KEY_NAME, "").strip() or found or None


# --------------------------------------------------------------------------
# synthetic prefixes (never customer data)
# --------------------------------------------------------------------------

def build_prefix(tag: str, words: int) -> str:
    """A deterministic, synthetic document, byte-identical across legs -- the
    whole requirement, since prompt caching is a PREFIX match and one changed
    byte invalidates everything after it."""
    rng = random.Random(f"{PREFIX_SEED}:{tag}")
    out = [rng.choice(_VOCAB) for _ in range(words)]
    paras = [f"Synthetic cache-probe corpus {tag}. Fixed word list and seed; "
             f"contains no customer data of any kind."]
    for start in range(0, len(out), 60):
        paras.append(" ".join(out[start:start + 60]) + ".")
    return "\n\n".join(paras)


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def _post(url: str, headers: dict, body: dict, timeout: float = 120.0):
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": {"message": raw[:400]}}


def _headers(key: str, beta: str | None = None) -> dict:
    hdr = {"x-api-key": key, "anthropic-version": API_VERSION}
    if beta:
        hdr["anthropic-beta"] = beta
    return hdr


def count_tokens(key: str, system_blocks: list) -> int:
    """Free, well-formed token count -- used only to SIZE prefixes for P3."""
    status, data = _post(
        COUNT_URL, _headers(key),
        {"model": MODEL, "system": system_blocks,
         "messages": [{"role": "user", "content": "."}]},
        timeout=60.0)
    if status == 200:
        return int(data.get("input_tokens") or 0)
    return -1


# --------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------

def price(usage: dict, write_mult: float = PRICES["write"]) -> float:
    fresh = int(usage.get("input_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    return (fresh * PRICES["input"]
            + written * write_mult
            + read * PRICES["cached"]
            + out * PRICES["output"]) / 1e6


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------

class Ledger:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.spend_usd = 0.0

    def guard(self, projected_usd: float) -> None:
        if self.spend_usd + projected_usd > SPEND_CAP_USD:
            raise RuntimeError(
                f"aborting: ${self.spend_usd:.6f} spent and the next call is "
                f"projected at ${projected_usd:.6f}, which would cross the "
                f"${SPEND_CAP_USD:.2f} cap")

    def record(self, call: Call) -> None:
        self.calls.append(call)
        self.spend_usd += call.spend_usd
        tag = f"{call.probe}/{call.label}"
        if call.status == 200:
            print(f"    {tag}: prefix={call.prefix_tokens} "
                  f"read={call.cache_read} write={call.cache_write} "
                  f"fresh={call.fresh_input} spend=${call.spend_usd:.6f}  "
                  f"[running ${self.spend_usd:.6f}/${SPEND_CAP_USD:.2f}]",
                  flush=True)
        else:
            print(f"    {tag}: status={call.status} {call.error[:160]}  "
                  f"[running ${self.spend_usd:.6f}/${SPEND_CAP_USD:.2f}]",
                  flush=True)
        if self.spend_usd > SPEND_CAP_USD:
            raise RuntimeError(
                f"aborting: cumulative spend ${self.spend_usd:.6f} crossed the "
                f"${SPEND_CAP_USD:.2f} cap")


def send(ledger: Ledger, key: str, probe: str, label: str, body: dict,
         at_s: float = 0.0, write_mult: float = PRICES["write"],
         beta: str | None = None, projected_tokens: int = BIG_WORDS * 2,
         retry: bool = True) -> Call:
    """One live /messages call, priced, capped, recorded. Retries once."""
    call = Call(probe=probe, label=label, at_s=at_s)
    # Reserve against a cold write of the whole prefix -- the worst case.
    projected = projected_tokens * write_mult / 1e6
    ledger.guard(projected)
    for attempt in range(2 if retry else 1):
        try:
            status, data = _post(ANTHROPIC_URL, _headers(key, beta), body)
            usage = dict(data.get("usage") or {})
        except Exception as exc:  # transport-level
            status, data, usage = 0, {"error": {"message": repr(exc)}}, {}
        if status == 200 and usage:
            call.status, call.usage = status, usage
            call.spend_usd = price(usage, write_mult)
            break
        call.status = status
        call.error = str((data.get("error") or {}).get("message") or data)[:300]
        if attempt == 0 and retry:
            print(f"    ! {probe}/{label} status={status}: {call.error[:120]} "
                  f"-- retrying once", flush=True)
            time.sleep(3.0)
    ledger.record(call)
    return call


def sys_call(key: str, ledger: Ledger, probe: str, label: str, prefix: str,
             turn: str, at_s: float = 0.0, ttl: str | None = None) -> Call:
    """Single-breakpoint system-prefix call. ttl='1h' opts into the extended tier."""
    cc = {"type": "ephemeral"}
    if ttl:
        cc["ttl"] = ttl
    body = {
        "model": MODEL,
        "max_tokens": 8,
        "system": [{"type": "text", "text": prefix, "cache_control": cc}],
        "messages": [{"role": "user", "content": turn}],
    }
    return send(ledger, key, probe, label, body, at_s=at_s,
                write_mult=WRITE_MULT_1H if ttl == "1h" else PRICES["write"],
                beta=BETA_1H if ttl == "1h" else None)


# --------------------------------------------------------------------------
# scheduling
# --------------------------------------------------------------------------

def sleep_until(t0: float, at_s: float) -> None:
    remaining = (t0 + at_s) - time.monotonic()
    while remaining > 0:
        print(f"  ... {remaining:6.1f}s until t=+{at_s:.0f}s", flush=True)
        time.sleep(min(20.0, remaining))
        remaining = (t0 + at_s) - time.monotonic()


# --------------------------------------------------------------------------
# FAST probes
# --------------------------------------------------------------------------

def probe_min_tokens(key: str, ledger: Ledger) -> list[Call]:
    """P3: sweep prefix sizes THROUGH the documented 4096-tok haiku floor.

    Each size is a distinct, fresh prefix written exactly once. cache_creation
    appears iff the prefix met the minimum, so the smallest measured-token
    prefix with cache_write > 0 is the empirical floor. No wall-clock waits.
    """
    print("\n=== P3  MIN CACHEABLE TOKENS (fast) ===", flush=True)
    # Calibrate tokens-per-word once so the ladder lands on real token targets.
    cal_words = 1_000
    cal_tokens = count_tokens(
        key, [{"type": "text", "text": build_prefix("cal", cal_words)}])
    tpw = (cal_tokens / cal_words) if cal_tokens > 0 else 1.35
    print(f"  calibration: {cal_words} words -> {cal_tokens} tokens "
          f"({tpw:.3f} tok/word)", flush=True)
    targets = [768, 1024, 1536, 2048, 3072, 3840, 4096, 4352, 4864, 5632]
    calls: list[Call] = []
    for tgt in targets:
        words = max(1, round(tgt / tpw))
        prefix = build_prefix(f"min{tgt}", words)
        measured = count_tokens(key, [{"type": "text", "text": prefix}])
        c = sys_call(key, ledger, "P3", f"~{tgt}tok", prefix,
                     "Reply with the single word OK.")
        c.note = f"target={tgt} words={words} count_tokens={measured}"
        calls.append(c)
    return calls


def probe_breakpoints(key: str, ledger: Ledger) -> list[Call]:
    """P4: 1, 4, then 5 cache_control breakpoints. Does 5 error or silently cap?

    Anthropic documents a maximum of 4 cache_control breakpoints. Splitting one
    large prefix into N cacheable system blocks and requesting N=5 tests whether
    the 5th is a 400 (documented) or silently dropped.
    """
    print("\n=== P4  BREAKPOINT COUNT (fast) ===", flush=True)
    calls: list[Call] = []
    for n in (1, 4, 5):
        per = BIG_WORDS // n
        blocks = []
        for i in range(n):
            text = build_prefix(f"bp{n}_{i}", per)
            blocks.append({"type": "text", "text": text,
                           "cache_control": {"type": "ephemeral"}})
        body = {"model": MODEL, "max_tokens": 8, "system": blocks,
                "messages": [{"role": "user", "content": "Reply OK."}]}
        c = send(ledger, key, "P4", f"{n}-breakpoints", body,
                 projected_tokens=BIG_WORDS * 2)
        c.note = f"{n} cache_control blocks of ~{per} words each"
        calls.append(c)
    return calls


# --------------------------------------------------------------------------
# SLOW probes (one shared timeline)
# --------------------------------------------------------------------------

# Single-read TTL fan: each gap gets its own fresh prefix, written at t=0 and
# read EXACTLY ONCE at the gap, so no read refreshes another. Brackets the
# documented 300s (5-min) default from 4.5 to 7.5 minutes.
TTL_FAN_GAPS = [270, 300, 330, 360, 390, 420, 450]
# Chain reads: every <=90s keeps ONE prefix alive well past a single TTL.
CHAIN_READS = [60, 150, 240, 300, 360, 450]
REFRESH_MID = 240.0     # P2: R1 first read (the refresh under test)
REFRESH_LATE = 480.0    # P2: both R1 and R2 late read
TIER_1H_READ = 360.0    # P5: read past the 5-min default, within the 1h tier


def run_slow(key: str, ledger: Ledger) -> list[Call]:
    print("\n=== SLOW PHASE (P1 TTL cliff / P2 refresh-on-read / P5 1h tier) ===",
          flush=True)
    print("  timeline ~8 min; writes at t=0, then staggered single reads",
          flush=True)
    big = "Reply with the single word OK."

    # Build every prefix up front (distinct tags => distinct cache keys).
    chain = build_prefix("chain", BIG_WORDS)
    fan = {g: build_prefix(f"fan{g}", BIG_WORDS) for g in TTL_FAN_GAPS}
    r1 = build_prefix("refresh1", BIG_WORDS)
    r2 = build_prefix("refresh2", BIG_WORDS)
    tier = build_prefix("tier1h", BIG_WORDS)
    calls: list[Call] = []

    t0 = time.monotonic()
    print("t=0  writes", flush=True)
    calls.append(sys_call(key, ledger, "P1", "chain@write", chain, big, 0.0))
    for g in TTL_FAN_GAPS:
        calls.append(sys_call(key, ledger, "P1", f"fan{g}@write", fan[g], big, 0.0))
    calls.append(sys_call(key, ledger, "P2", "R1@write", r1, big, 0.0))
    calls.append(sys_call(key, ledger, "P2", "R2ctrl@write", r2, big, 0.0))
    calls.append(sys_call(key, ledger, "P5", "1h@write", tier, big, 0.0, ttl="1h"))

    # Assemble the read schedule as a sorted event list.
    events: list[tuple[float, str]] = []
    for g in CHAIN_READS:
        events.append((float(g), "chain"))
    for g in TTL_FAN_GAPS:
        events.append((float(g), f"fan{g}"))
    events.append((REFRESH_MID, "R1mid"))
    events.append((REFRESH_LATE, "R1late"))
    events.append((REFRESH_LATE, "R2late"))
    events.append((TIER_1H_READ, "tier1h"))
    events.sort(key=lambda e: e[0])

    for at_s, kind in events:
        sleep_until(t0, at_s)
        if kind == "chain":
            calls.append(sys_call(key, ledger, "P1", f"chain@read+{at_s:.0f}s",
                                  chain, big, at_s))
        elif kind.startswith("fan"):
            g = int(kind[3:])
            calls.append(sys_call(key, ledger, "P1", f"fan{g}@read(gap={g}s)",
                                  fan[g], big, at_s))
        elif kind == "R1mid":
            calls.append(sys_call(key, ledger, "P2", "R1@read+240s(refresh)",
                                  r1, big, at_s))
        elif kind == "R1late":
            calls.append(sys_call(key, ledger, "P2", "R1@read+480s", r1, big, at_s))
        elif kind == "R2late":
            calls.append(sys_call(key, ledger, "P2", "R2ctrl@read+480s(no-refresh)",
                                  r2, big, at_s))
        elif kind == "tier1h":
            calls.append(sys_call(key, ledger, "P5", "1h@read+360s", tier, big,
                                  at_s, ttl="1h"))
    return calls


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def dump(ledger: Ledger) -> None:
    scratch = os.environ.get("BREVITAS_PROBE_OUT") or os.path.join(
        REPO_ROOT, "anthropic_cache_probe_receipts.json")
    payload = {
        "model": MODEL, "prices": PRICES, "spend_usd": ledger.spend_usd,
        "cap_usd": SPEND_CAP_USD,
        "calls": [
            {"probe": c.probe, "label": c.label, "at_s": c.at_s,
             "status": c.status, "usage": c.usage, "spend_usd": c.spend_usd,
             "prefix_tokens": c.prefix_tokens, "cache_read": c.cache_read,
             "cache_write": c.cache_write, "fresh_input": c.fresh_input,
             "error": c.error, "note": c.note}
            for c in ledger.calls],
    }
    try:
        with open(scratch, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        print(f"\nreceipts dumped -> {scratch}", flush=True)
    except OSError as exc:
        print(f"\ncould not dump receipts: {exc}", flush=True)


def main() -> int:
    if os.getenv(ACK_ENV, "") != "1":
        print(f"refusing to spend: set {ACK_ENV}=1 to run this probe")
        return 2
    key = load_probe_key()
    if not key:
        print(f"missing probe key {PROBE_KEY_NAME} in {ENV_FILE} or env")
        return 2
    print(f"anthropic cache probe: model={MODEL} cap=${SPEND_CAP_USD:.2f} "
          f"seed={PREFIX_SEED}", flush=True)

    ledger = Ledger()
    try:
        # FAST first, so we keep data even if budget/time runs short.
        probe_min_tokens(key, ledger)
        probe_breakpoints(key, ledger)
        print(f"\n--- fast probes done, running spend ${ledger.spend_usd:.6f} ---",
              flush=True)
        run_slow(key, ledger)
    except RuntimeError as exc:
        print(f"\nSPEND GUARD: {exc}", flush=True)
        dump(ledger)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
        dump(ledger)
        return 1

    dump(ledger)
    print(f"\nTOTAL LIVE SPEND ${ledger.spend_usd:.6f}  (cap ${SPEND_CAP_USD:.2f})",
          flush=True)
    assert ledger.spend_usd < SPEND_CAP_USD, (
        f"probe spent ${ledger.spend_usd:.6f}, over the ${SPEND_CAP_USD:.2f} cap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
