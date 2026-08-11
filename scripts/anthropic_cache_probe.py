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
    def __init__(self, cap_usd: float = SPEND_CAP_USD) -> None:
        self.calls: list[Call] = []
        self.spend_usd = 0.0
        # Per-run cap. Defaults to the module cap so the original probes are
        # byte-for-byte unchanged in behaviour; E3 passes a tighter one.
        self.cap_usd = float(cap_usd)

    def guard(self, projected_usd: float) -> None:
        if self.spend_usd + projected_usd > self.cap_usd:
            raise RuntimeError(
                f"aborting: ${self.spend_usd:.6f} spent and the next call is "
                f"projected at ${projected_usd:.6f}, which would cross the "
                f"${self.cap_usd:.2f} cap")

    def record(self, call: Call) -> None:
        self.calls.append(call)
        self.spend_usd += call.spend_usd
        tag = f"{call.probe}/{call.label}"
        if call.status == 200:
            print(f"    {tag}: prefix={call.prefix_tokens} "
                  f"read={call.cache_read} write={call.cache_write} "
                  f"fresh={call.fresh_input} spend=${call.spend_usd:.6f}  "
                  f"[running ${self.spend_usd:.6f}/${self.cap_usd:.2f}]",
                  flush=True)
        else:
            print(f"    {tag}: status={call.status} {call.error[:160]}  "
                  f"[running ${self.spend_usd:.6f}/${self.cap_usd:.2f}]",
                  flush=True)
        if self.spend_usd > self.cap_usd:
            raise RuntimeError(
                f"aborting: cumulative spend ${self.spend_usd:.6f} crossed the "
                f"${self.cap_usd:.2f} cap")


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
# E3 -- determinism cost + bounded 1h-tier mechanics
# --------------------------------------------------------------------------

E3_CAP_USD = 0.50
# E3a: two cacheable system blocks, breakpoints at ~5k and ~10k cumulative tokens.
# Both are above haiku's proven 4096 floor, so both regions really cache.
E3A_BLOCK_TOKENS = 5_000
# E3b: one block per arm, comfortably above the floor. Kept smaller than E3a
# because four arms x three calls each is where the money actually goes.
E3B_TOKENS = 6_000
# Late read for every E3b arm. The default cliff is measured at (300, 330]s, and
# the intervening read lands at ~45s, so a 5-minute clock started by that read
# expires by ~375s at the latest. 445s leaves ~70s of margin on the far side.
E3B_MID_READ_S = 45.0
E3B_LATE_READ_S = 445.0

# A realistic stray timestamp -- the exact thing the brief says destroys a prefix.
def _volatile_token() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _words_for(key: str, target_tokens: int) -> int:
    """Calibrate words->tokens once against the live counter, so the blocks land
    on real token targets instead of guessed ones."""
    cal_words = 1_000
    cal = count_tokens(key, [{"type": "text", "text": build_prefix("e3cal", cal_words)}])
    tpw = (cal / cal_words) if cal > 0 else 1.35
    return max(1, round(target_tokens / tpw))


def _e3a_system(base1: str, base2: str, inject: str, where: str) -> list:
    """Two breakpointed system blocks, optionally with a volatile token injected.

    where='none'  clean baseline
    where='top'   position 0 of block 1 -- ahead of BOTH breakpoints
    where='mid'   halfway through block 2 -- after breakpoint 1, before breakpoint 2
    (the 'tail' case is not a system-block edit at all: it goes in the user turn,
     below the last breakpoint, and so cannot touch any cached region)
    """
    b1, b2 = base1, base2
    if where == "top":
        b1 = f"{inject}\n\n{base1}"
    elif where == "mid":
        half = len(base2) // 2
        b2 = f"{base2[:half]}\n\n{inject}\n\n{base2[half:]}"
    return [
        {"type": "text", "text": b1, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": b2, "cache_control": {"type": "ephemeral"}},
    ]


def probe_volatile_token(key: str, ledger: Ledger) -> list[Call]:
    """E3a: what one stray timestamp costs, as a function of WHERE it sits.

    Writes a 2-breakpoint ~10k-token prefix, reads it warm to prove a HIT, then
    re-sends the same prefix three times with a timestamp injected above the
    first breakpoint, between the breakpoints, and below the last breakpoint.
    A closing clean read is the validity sentinel: if it misses, the TTL expired
    mid-sequence and the whole sub-experiment is contaminated.
    """
    print("\n=== E3a  COST OF ONE VOLATILE TOKEN ===", flush=True)
    words = _words_for(key, E3A_BLOCK_TOKENS)
    base1 = build_prefix("e3a_b1", words)
    base2 = build_prefix("e3a_b2", words)
    clean = _e3a_system(base1, base2, "", "none")
    n1 = count_tokens(key, [clean[0]])
    n_all = count_tokens(key, clean)
    print(f"  sized: block1={n1} tok, block1+block2={n_all} tok "
          f"(haiku floor 4096; both breakpoint regions clear it)", flush=True)

    calls: list[Call] = []
    t0 = time.monotonic()

    def fire(label: str, system: list, user: str, note: str) -> Call:
        body = {"model": MODEL, "max_tokens": 8, "system": system,
                "messages": [{"role": "user", "content": user}]}
        c = send(ledger, key, "E3a", label, body,
                 at_s=round(time.monotonic() - t0, 2),
                 projected_tokens=int(E3A_BLOCK_TOKENS * 2.4))
        c.note = note
        calls.append(c)
        return c

    ask = "Reply with the single word OK."
    fire("1-cold-write", clean, ask, "cold write of the clean 2-breakpoint prefix")
    fire("2-clean-read", clean, ask, "warm read; must HIT before any injection means anything")

    ts = _volatile_token()
    tok_top = count_tokens(key, _e3a_system(base1, base2, ts, "top"))
    fire("3-inject-TOP", _e3a_system(base1, base2, ts, "top"), ask,
         f"timestamp {ts!r} at position 0 (above breakpoint 1); "
         f"prefix now {tok_top} tok vs clean {n_all}")
    tok_mid = count_tokens(key, _e3a_system(base1, base2, ts, "mid"))
    fire("4-inject-MID", _e3a_system(base1, base2, ts, "mid"), ask,
         f"timestamp {ts!r} halfway through block 2 (between breakpoints); "
         f"prefix now {tok_mid} tok vs clean {n_all}")
    fire("5-inject-TAIL", clean, f"[{ts}] {ask}",
         f"timestamp {ts!r} in the user turn, BELOW the last breakpoint")
    fire("6-sentinel", clean, ask,
         "clean re-read; a MISS here means TTL expired and E3a is contaminated")
    return calls


def probe_1h_bounded(key: str, ledger: Ledger) -> list[Call]:
    """E3b (BOUNDED): does the ttl on a READ govern the entry's refreshed clock?

    The definitive 1h refresh test needs >60 minutes of wall clock and cannot be
    run here. What IS decidable in ~7.5 minutes is the mechanism underneath it:
    when a read refreshes an entry, does the refreshed lifetime come from the
    ttl requested on THAT read, or from the tier the entry was written in?

    2x2, all four arms written at t=0, all four read late at t=+445s (well past
    the measured (300,330]s default cliff):

      H1  write ttl=1h  -> read at +45s with NO ttl   -> read at +445s
      H2  write ttl=1h  ->        (no mid read)       -> read at +445s   [control]
      F1  write 5m      -> read at +45s with ttl=1h   -> read at +445s
      F2  write 5m      -> read at +45s with NO ttl   -> read at +445s   [control]

    H1 vs H2 asks whether a mis-tiered keep-alive ping DEMOTES a 1h entry to a
    5-minute clock (if it does, H1 dies by ~375s and misses while H2 hits).
    F1 vs F2 asks whether ttl=1h on a read PROMOTES a 5m entry (if it does, F1
    hits at 445s while F2 -- refreshed at 45s on a 5m clock -- misses).
    """
    print("\n=== E3b  1h TIER, BOUNDED (see caveats: NOT the >60min refresh test) ===",
          flush=True)
    words = _words_for(key, E3B_TOKENS)
    arms = {name: build_prefix(f"e3b_{name}", words) for name in ("H1", "H2", "F1", "F2")}
    sized = count_tokens(key, [{"type": "text", "text": arms["H1"]}])
    print(f"  sized: each arm {sized} tok (floor 4096)", flush=True)
    ask = "Reply with the single word OK."
    calls: list[Call] = []
    t0 = time.monotonic()

    def fire(arm: str, label: str, ttl: str | None, note: str) -> Call:
        c = sys_call(key, ledger, "E3b", f"{arm}/{label}", arms[arm], ask,
                     at_s=round(time.monotonic() - t0, 2), ttl=ttl)
        c.note = note
        calls.append(c)
        return c

    print("t=0  writes (H1/H2 on the 1h tier at 2.0x, F1/F2 on the default 5m tier at 1.25x)",
          flush=True)
    fire("H1", "write(ttl=1h)", "1h", "1h write; will get a MIS-TIERED read at +45s")
    fire("H2", "write(ttl=1h)", "1h", "1h write; CONTROL, untouched until the late read")
    fire("F1", "write(5m)", None, "default-tier write; will get a ttl=1h read at +45s")
    fire("F2", "write(5m)", None, "default-tier write; CONTROL, plain read at +45s")

    sleep_until(t0, E3B_MID_READ_S)
    fire("H1", "read+45s(NO ttl)", None,
         "mis-tiered keep-alive: does omitting ttl demote the 1h entry to a 5m clock?")
    fire("F1", "read+45s(ttl=1h)", "1h",
         "promotion probe: does ttl=1h on a read upgrade a 5m entry's clock?")
    fire("F2", "read+45s(NO ttl)", None,
         "control for F1: identical call except the requested ttl")

    sleep_until(t0, E3B_LATE_READ_S)
    fire("H1", "read+445s(ttl=1h)", "1h", "HIT => the mis-tiered read did not demote H1")
    fire("H2", "read+445s(ttl=1h)", "1h", "control; a MISS here contaminates the whole arm set")
    fire("F1", "read+445s(ttl=1h)", "1h", "HIT => the ttl=1h read promoted F1 past the 5m clock")
    fire("F2", "read+445s(NO ttl)", None, "control; MISS expected on a refreshed 5m clock")
    return calls


def main_e3() -> int:
    """E3 entry point. Separate from main() so the original probes stay runnable
    and untouched. Hard cap $0.50, projected per-call BEFORE sending."""
    if os.getenv(ACK_ENV, "") != "1":
        print(f"refusing to spend: set {ACK_ENV}=1 to run this probe")
        return 2
    key = load_probe_key()
    if not key:
        print(f"missing probe key {PROBE_KEY_NAME} in {ENV_FILE} or env")
        return 2
    print(f"E3 determinism/1h probe: model={MODEL} cap=${E3_CAP_USD:.2f} "
          f"seed={PREFIX_SEED}", flush=True)
    ledger = Ledger(cap_usd=E3_CAP_USD)
    try:
        probe_volatile_token(key, ledger)
        print(f"\n--- E3a done, running spend ${ledger.spend_usd:.6f} ---", flush=True)
        probe_1h_bounded(key, ledger)
    except RuntimeError as exc:
        print(f"\nSPEND GUARD: {exc}", flush=True)
        dump(ledger)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
        dump(ledger)
        return 1
    dump(ledger)
    print(f"\nTOTAL LIVE SPEND ${ledger.spend_usd:.6f}  (cap ${E3_CAP_USD:.2f})", flush=True)
    assert ledger.spend_usd < E3_CAP_USD, (
        f"probe spent ${ledger.spend_usd:.6f}, over the ${E3_CAP_USD:.2f} cap")
    return 0


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def dump(ledger: Ledger) -> None:
    scratch = os.environ.get("BREVITAS_PROBE_OUT") or os.path.join(
        REPO_ROOT, "anthropic_cache_probe_receipts.json")
    payload = {
        "model": MODEL, "prices": PRICES, "spend_usd": ledger.spend_usd,
        "cap_usd": ledger.cap_usd, "write_mult_1h": WRITE_MULT_1H,
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
    # `... anthropic_cache_probe.py e3` runs only the E3 probes under their own
    # $0.50 cap. No argument = the original P1-P5 run, unchanged.
    if len(sys.argv) > 1 and sys.argv[1] == "e3":
        raise SystemExit(main_e3())
    raise SystemExit(main())
