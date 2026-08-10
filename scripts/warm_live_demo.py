#!/usr/bin/env python3
"""Live-fire proof that a keep-alive ping bridges a provider cache TTL.

MANUAL ONLY, AND IT SPENDS REAL MONEY. pytest never imports this file, the
worker never calls it, and it refuses to do anything unless
BREVITAS_WARM_LIVE_DEMO=1 is set explicitly. It loads ONLY the
BREVITAS_PROBE_* keys out of the repo-root .env.local (Brevitas's own probe
accounts) and hard-asserts that cumulative list-price spend stays under
$0.75, aborting mid-run the moment a projected call would cross the cap.

THE EXPERIMENT
--------------
Everything the simulator prices is a MODEL of provider behaviour. This is the
measurement that says the model is not fiction. Three prefixes, one timeline,
about nine minutes:

  anthropic, prefix A -- WITH warming
      t=0     write request, cache_control breakpoint on the system prefix
      t=+4m   keep-alive ping (same prefix, a '.' turn, max_tokens=1)
      t=+8m   the customer returns
    The 5-minute TTL has lapsed twice over by t=+8m. If the ping did nothing,
    this lands cold. EXPECT usage.cache_read_input_tokens > 0.

  anthropic, prefix B -- WITHOUT warming (the control)
      t=0     write request
      t=+8m   the customer returns, no ping in between
    EXPECT cache_creation_input_tokens > 0 and cache_read_input_tokens == 0:
    the entry died and the return pays the full write premium again.

  deepseek -- the honest counterpoint
      t=0     write, t=+8m return, NO pings
    DeepSeek's automatic prefix cache is an eviction policy, not a 5-minute
    clock. EXPECT prompt_cache_hit_tokens > 0 anyway -- warming buys nothing
    at this gap, and a vendor who claims otherwise is selling you the
    provider's own cache back.

A and B run on the same timeline so the pair is a controlled comparison
rather than two runs an hour apart, and so the whole demo costs one wait.

PREFIX SIZING IS NOT ARBITRARY. Anthropic's minimum cacheable prefix is
MODEL-DEPENDENT and claude-haiku-4-5's is 4096 tokens -- a shorter prefix
silently never caches and reports cache_creation_input_tokens = 0 with no
error at all. The synthetic prefixes here are built well above that floor
from a seeded word list, so a null result is a real null result rather than
a prefix that was never eligible.

NO CUSTOMER DATA EVER TOUCHES THIS FILE. The prefixes are generated from a
fixed word list and a fixed seed.

    BREVITAS_WARM_LIVE_DEMO=1 .venv/bin/python scripts/warm_live_demo.py
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

ACK_ENV = "BREVITAS_WARM_LIVE_DEMO"
SPEND_CAP_USD = 0.75

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env.local")
# The ONLY keys this script is allowed to read out of .env.local. Anything
# else in that file (production credentials, service-role keys, Stripe) is
# never parsed into this process.
ALLOWED_ENV_PREFIX = "BREVITAS_PROBE_"

# Wall-clock schedule, seconds from t0. The anthropic TTL is 300s, so the
# t=+8m return is two TTLs past the write and the single ping at t=+4m is the
# only thing that can keep the entry alive.
PING_AT_S = 240.0
RETURN_AT_S = 480.0

# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------

# List prices, $/Mtok, copied from brevitas/receipts.py MODEL_PRICES. Kept as
# literals rather than imported so this script stays standalone and cannot be
# perturbed by a repricing that lands mid-demo.
#
# claude-haiku-4-5 is the cheapest PRICED anthropic model in the catalog and
# the one this demo spends on. Its 4096-token cache minimum is the reason
# PREFIX_WORDS is sized the way it is.
ANTHROPIC_MODEL = "claude-haiku-4-5"
ANTHROPIC_PRICES = {"input": 1.0, "cached": 0.1, "write": 1.25, "output": 5.0}
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_PRICES = {"input": 0.14, "cached": 0.0028, "output": 0.28}

# ~5,200 words -> comfortably north of haiku-4-5's 4096-token cache floor,
# while a single write still costs well under a cent.
PREFIX_WORDS = 5_200
PREFIX_SEED = 20260810

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


@dataclass
class Call:
    """One live request and the receipt it came back with."""

    provider: str
    scenario: str
    leg: str
    at_s: float
    status: int = 0
    usage: dict = field(default_factory=dict)
    spend_usd: float = 0.0
    nocache_usd: float = 0.0
    error: str = ""

    @property
    def cached_tokens(self) -> int:
        if self.provider == "anthropic":
            return int(self.usage.get("cache_read_input_tokens") or 0)
        return int(self.usage.get("prompt_cache_hit_tokens") or 0)

    @property
    def written_tokens(self) -> int:
        if self.provider == "anthropic":
            return int(self.usage.get("cache_creation_input_tokens") or 0)
        return int(self.usage.get("prompt_cache_miss_tokens") or 0)

    @property
    def prefix_tokens(self) -> int:
        """Total input tokens the provider says it processed, however priced."""
        if self.provider == "anthropic":
            return (int(self.usage.get("input_tokens") or 0)
                    + self.written_tokens + self.cached_tokens)
        return int(self.usage.get("prompt_tokens") or 0)


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------

def load_probe_keys() -> dict[str, str]:
    """Read ONLY BREVITAS_PROBE_* out of .env.local. Process env wins."""
    found: dict[str, str] = {}
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
                value = value.strip().strip('"').strip("'")
                if value:
                    found[name] = value
    except FileNotFoundError:
        pass
    for name in ("BREVITAS_PROBE_ANTHROPIC_KEY", "BREVITAS_PROBE_DEEPSEEK_KEY"):
        env_value = os.environ.get(name, "").strip()
        if env_value:
            found[name] = env_value
    return found


# --------------------------------------------------------------------------
# synthetic prefixes (never customer data)
# --------------------------------------------------------------------------

def build_prefix(tag: str) -> str:
    """A deterministic, synthetic document. Byte-identical across legs, which
    is the whole requirement -- prompt caching is a PREFIX match and one
    changed byte invalidates everything after it."""
    rng = random.Random(f"{PREFIX_SEED}:{tag}")
    words = [rng.choice(_VOCAB) for _ in range(PREFIX_WORDS)]
    paragraphs: list[str] = [
        f"Synthetic warming corpus {tag}. Generated from a fixed word list "
        f"and a fixed seed; contains no customer data of any kind."
    ]
    for start in range(0, len(words), 60):
        paragraphs.append(" ".join(words[start:start + 60]) + ".")
    return "\n\n".join(paragraphs)


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def _post(url: str, headers: dict[str, str], body: dict,
          timeout: float = 120.0) -> tuple[int, dict]:
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    for key, value in headers.items():
        request.add_header(key, value)
    request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": {"message": raw[:400]}}


def anthropic_call(key: str, prefix: str, turn: str, max_tokens: int
                   ) -> tuple[int, dict]:
    return _post(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
        {
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            # The breakpoint sits on the system block, so the cached prefix is
            # byte-identical across legs and only the user turn varies.
            "system": [{"type": "text", "text": prefix,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": turn}],
        },
    )


def deepseek_call(key: str, prefix: str, turn: str, max_tokens: int
                  ) -> tuple[int, dict]:
    return _post(
        "https://api.deepseek.com/chat/completions",
        {"authorization": f"Bearer {key}"},
        {
            "model": DEEPSEEK_MODEL,
            "max_tokens": max_tokens,
            # DeepSeek caches automatically on the message prefix; there is no
            # cache_control to set and nothing to keep alive by hand.
            "messages": [{"role": "system", "content": prefix},
                         {"role": "user", "content": turn}],
        },
    )


# --------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------

def price_anthropic(usage: dict) -> tuple[float, float]:
    """(actual list-price dollars, dollars the same call would cost uncached)."""
    fresh = int(usage.get("input_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    actual = (fresh * ANTHROPIC_PRICES["input"]
              + written * ANTHROPIC_PRICES["write"]
              + read * ANTHROPIC_PRICES["cached"]
              + out * ANTHROPIC_PRICES["output"]) / 1e6
    nocache = ((fresh + written + read) * ANTHROPIC_PRICES["input"]
               + out * ANTHROPIC_PRICES["output"]) / 1e6
    return actual, nocache


def price_deepseek(usage: dict) -> tuple[float, float]:
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    out = int(usage.get("completion_tokens") or 0)
    total_in = int(usage.get("prompt_tokens") or (hit + miss))
    actual = (miss * DEEPSEEK_PRICES["input"]
              + hit * DEEPSEEK_PRICES["cached"]
              + out * DEEPSEEK_PRICES["output"]) / 1e6
    nocache = (total_in * DEEPSEEK_PRICES["input"]
               + out * DEEPSEEK_PRICES["output"]) / 1e6
    return actual, nocache


# --------------------------------------------------------------------------
# runner
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
        if self.spend_usd > SPEND_CAP_USD:
            raise RuntimeError(
                f"aborting: cumulative spend ${self.spend_usd:.6f} crossed the "
                f"${SPEND_CAP_USD:.2f} cap")


def run_leg(ledger: Ledger, keys: dict[str, str], provider: str, scenario: str,
            leg: str, at_s: float, prefix: str, turn: str, max_tokens: int,
            retry: bool = True) -> Call:
    """One live call, priced, capped and recorded. Retries once on failure."""
    call = Call(provider=provider, scenario=scenario, leg=leg, at_s=at_s)
    # A cold write of this prefix is the worst case; reserve against it.
    approx_tokens = PREFIX_WORDS * 2
    projected = (approx_tokens * (ANTHROPIC_PRICES["write"]
                                  if provider == "anthropic"
                                  else DEEPSEEK_PRICES["input"])) / 1e6
    ledger.guard(projected)

    key = keys["BREVITAS_PROBE_ANTHROPIC_KEY" if provider == "anthropic"
                else "BREVITAS_PROBE_DEEPSEEK_KEY"]
    attempts = 2 if retry else 1
    for attempt in range(attempts):
        try:
            if provider == "anthropic":
                status, data = anthropic_call(key, prefix, turn, max_tokens)
                usage = dict(data.get("usage") or {})
            else:
                status, data = deepseek_call(key, prefix, turn, max_tokens)
                usage = dict(data.get("usage") or {})
        except Exception as exc:  # transport-level
            status, data, usage = 0, {"error": {"message": repr(exc)}}, {}
        if status == 200 and usage:
            call.status, call.usage = status, usage
            call.spend_usd, call.nocache_usd = (
                price_anthropic(usage) if provider == "anthropic"
                else price_deepseek(usage))
            break
        message = str((data.get("error") or {}).get("message") or data)[:300]
        call.status, call.error = status, message
        if attempt + 1 < attempts:
            print(f"    ! {provider}/{scenario}/{leg} failed "
                  f"(status={status}): {message} -- retrying once")
            time.sleep(3.0)
        else:
            print(f"    ! {provider}/{scenario}/{leg} FAILED after retry "
                  f"(status={status}): {message}")
    ledger.record(call)
    if call.status == 200:
        print(f"    {provider}/{scenario}/{leg}: prefix={call.prefix_tokens} "
              f"cache_read={call.cached_tokens} written={call.written_tokens} "
              f"spend=${call.spend_usd:.6f}")
    return call


def sleep_until(t0: float, at_s: float) -> None:
    remaining = (t0 + at_s) - time.monotonic()
    while remaining > 0:
        print(f"  ... {remaining:6.1f}s until t=+{at_s / 60:.0f}m", flush=True)
        time.sleep(min(30.0, remaining))
        remaining = (t0 + at_s) - time.monotonic()


def render(ledger: Ledger) -> str:
    rows: list[tuple[str, str, Call | None, Call | None]] = []
    by_key: dict[tuple[str, str, str], Call] = {
        (c.provider, c.scenario, c.leg): c for c in ledger.calls}
    for provider, scenario in (("anthropic", "A: warmed"),
                               ("anthropic", "B: unwarmed"),
                               ("deepseek", "C: no warming needed")):
        rows.append((provider, scenario,
                     by_key.get((provider, scenario, "write")),
                     by_key.get((provider, scenario, "return"))))

    lines: list[str] = []
    lines.append("=" * 100)
    lines.append("LIVE LEDGER -- real calls, real cents, provider-reported receipts")
    lines.append("=" * 100)
    header = (f"{'provider':<10} {'scenario':<22} {'prefix tok':>10} "
              f"{'cached@return':>14} {'spent':>10} {'no-cache':>10} {'net':>10}")
    lines.append(header)
    lines.append("-" * 100)
    for provider, scenario, write, ret in rows:
        if write is None or ret is None:
            lines.append(f"{provider:<10} {scenario:<22} "
                         f"{'-- leg missing, see failures above --':>60}")
            continue
        legs = [c for c in ledger.calls
                if c.provider == provider and c.scenario == scenario]
        # Spend counts EVERY leg, pings included. The no-cache baseline counts
        # only the customer's own requests -- in a world without a cache there
        # is no keep-alive to pay for -- so net is savings already charged for
        # the ping rather than savings with the cost hidden.
        spent = sum(c.spend_usd for c in legs)
        nocache = sum(c.nocache_usd for c in legs if c.leg != "ping")
        lines.append(
            f"{provider:<10} {scenario:<22} {ret.prefix_tokens:>10,} "
            f"{ret.cached_tokens:>14,} ${spent:>9.6f} "
            f"${nocache:>9.6f} ${nocache - spent:>9.6f}")
    lines.append("-" * 100)
    lines.append(f"{'TOTAL LIVE SPEND':<33} ${ledger.spend_usd:.6f}"
                 f"   (cap ${SPEND_CAP_USD:.2f})")
    lines.append("=" * 100)

    lines.append("")
    lines.append("VERDICT")
    warmed = by_key.get(("anthropic", "A: warmed", "return"))
    control = by_key.get(("anthropic", "B: unwarmed", "return"))
    ping = by_key.get(("anthropic", "A: warmed", "ping"))
    deep = by_key.get(("deepseek", "C: no warming needed", "return"))
    if warmed and control and warmed.status == 200 and control.status == 200:
        if warmed.cached_tokens > 0 and control.cached_tokens == 0:
            lines.append(
                f"  PROVEN. One ${ping.spend_usd if ping else 0:.6f} keep-alive at t=+4m "
                f"carried {warmed.cached_tokens:,} tokens across an 8-minute gap "
                f"that a 5-minute TTL cannot span. The unwarmed control re-wrote "
                f"{control.written_tokens:,} tokens at the 1.25x premium.")
            delta = control.spend_usd - warmed.spend_usd
            lines.append(f"  On the return leg alone the warmed prefix cost "
                         f"${warmed.spend_usd:.6f} against the control's "
                         f"${control.spend_usd:.6f} -- ${delta:.6f} saved for a "
                         f"${ping.spend_usd if ping else 0:.6f} ping.")
        elif warmed.cached_tokens == 0:
            lines.append("  NOT PROVEN: the warmed return read nothing from cache. "
                         "Either the ping did not land, the prefix fell under the "
                         "model's cache minimum, or the TTL is shorter than modelled.")
        else:
            lines.append("  INCONCLUSIVE: the unwarmed control ALSO read from cache, "
                         "so this gap did not actually need warming.")
    else:
        lines.append("  anthropic legs incomplete -- see the failures above.")
    if deep and deep.status == 200:
        if deep.cached_tokens > 0:
            lines.append(
                f"  DeepSeek read {deep.cached_tokens:,} tokens from cache across "
                f"the same 8-minute gap with ZERO pings. Warming this provider at "
                f"this gap would have been pure cost -- the counterpoint is real "
                f"and we report it.")
        else:
            lines.append("  DeepSeek went cold unaided; its cache is shorter-lived "
                         "than assumed at this gap.")
    else:
        lines.append("  deepseek leg incomplete -- see the failures above.")

    lines.append("")
    lines.append("RAW RECEIPTS (provider-reported usage, verbatim)")
    for call in ledger.calls:
        tag = f"{call.provider}/{call.scenario}/{call.leg} @ t=+{call.at_s / 60:.0f}m"
        if call.status == 200:
            lines.append(f"  {tag}: {json.dumps(call.usage, sort_keys=True)}")
        else:
            lines.append(f"  {tag}: FAILED status={call.status} {call.error}")
    return "\n".join(lines)


def main() -> int:
    if os.getenv(ACK_ENV, "") != "1":
        print(f"refusing to spend: set {ACK_ENV}=1 to run this demo")
        return 2
    keys = load_probe_keys()
    missing = [n for n in ("BREVITAS_PROBE_ANTHROPIC_KEY",
                           "BREVITAS_PROBE_DEEPSEEK_KEY") if n not in keys]
    if missing:
        print(f"missing probe keys: {', '.join(missing)}")
        return 2

    prefix_a = build_prefix("A")
    prefix_b = build_prefix("B")
    prefix_d = build_prefix("D")
    print(f"synthetic prefixes built: ~{PREFIX_WORDS:,} words each, seed "
          f"{PREFIX_SEED}, model {ANTHROPIC_MODEL} / {DEEPSEEK_MODEL}")
    print(f"spend cap ${SPEND_CAP_USD:.2f}; timeline is "
          f"t=0 / t=+{PING_AT_S / 60:.0f}m / t=+{RETURN_AT_S / 60:.0f}m\n")

    ledger = Ledger()
    t0 = time.monotonic()
    try:
        print("t=0  writes")
        run_leg(ledger, keys, "anthropic", "A: warmed", "write", 0.0,
                prefix_a, "Reply with the single word OK.", 16)
        run_leg(ledger, keys, "anthropic", "B: unwarmed", "write", 0.0,
                prefix_b, "Reply with the single word OK.", 16)
        run_leg(ledger, keys, "deepseek", "C: no warming needed", "write", 0.0,
                prefix_d, "Reply with the single word OK.", 16)

        sleep_until(t0, PING_AT_S)
        print(f"t=+{PING_AT_S / 60:.0f}m  keep-alive ping on prefix A ONLY")
        run_leg(ledger, keys, "anthropic", "A: warmed", "ping", PING_AT_S,
                prefix_a, ".", 1)

        sleep_until(t0, RETURN_AT_S)
        print(f"t=+{RETURN_AT_S / 60:.0f}m  the customers return")
        run_leg(ledger, keys, "anthropic", "A: warmed", "return", RETURN_AT_S,
                prefix_a, "Reply with the single word DONE.", 16)
        run_leg(ledger, keys, "anthropic", "B: unwarmed", "return", RETURN_AT_S,
                prefix_b, "Reply with the single word DONE.", 16)
        run_leg(ledger, keys, "deepseek", "C: no warming needed", "return",
                RETURN_AT_S, prefix_d, "Reply with the single word DONE.", 16)
    except RuntimeError as exc:
        print(f"\nSPEND GUARD: {exc}")
        print(render(ledger))
        return 1

    print()
    print(render(ledger))
    assert ledger.spend_usd < SPEND_CAP_USD, (
        f"live demo spent ${ledger.spend_usd:.6f}, over the "
        f"${SPEND_CAP_USD:.2f} cap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
