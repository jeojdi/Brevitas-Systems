#!/usr/bin/env python3
"""Empirical cache-behavior probe against DeepSeek's live OpenAI-compatible API.

MANUAL ONLY, AND IT SPENDS REAL MONEY. pytest never imports this file, the
worker never calls it, and it refuses to do anything unless
BREVITAS_DEEPSEEK_PROBE=1 is set explicitly. It loads ONLY the
BREVITAS_PROBE_DEEPSEEK_KEY out of the repo-root .env.local (Brevitas's own
probe account) and hard-asserts that cumulative list-price spend stays under
$2.00, aborting the instant a projected call would cross the cap.

WHY DEEPSEEK IS THE HIGH-VALUE TARGET
-------------------------------------
DeepSeek publishes almost NO cache documentation: caching is "automatic" with
no directives, there is no stated minimum cacheable prefix, and the TTL is given
only as the prose "a few hours to a few days" (kv_cache guide). Every number we
measure here is therefore proprietary -- it exists nowhere in their docs -- which
is exactly the datum Brevitas's measurement lever is built to own.

WHAT THIS IS
------------
Behavioral CHARACTERIZATION of DeepSeek's automatic prompt cache with our own
key and our own money. Every request is byte-identical, well-formed, and small.
We measure the provider's OWN usage receipt (prompt_cache_hit_tokens vs
prompt_cache_miss_tokens) as the evidence for every claim.

WHAT THIS IS NOT
----------------
No attempt is made to read or infer another tenant's data, to exploit any
cross-user cache-sharing side channel, to probe or evade rate limits, or to send
malformed/abusive traffic. Synthetic prefixes only, from a seeded word list,
never customer data. If a probe idea edges toward attacking DeepSeek's
infrastructure or other tenants, it is SKIPPED, not run. In particular there is
NO cross-key/cross-tenant scoping test: we hold exactly one probe key, and the
scoping probe only confirms same-key reuse -- cross-key isolation is out of scope
by guardrail, not merely unimplemented.

THE PROBES
----------
FAST (run first, print before spending on the slow sweep):
  P1  IS IT AUTOMATIC     -- write a prefix (miss), immediately resend identical;
                            expect prompt_cache_hit_tokens>0 with NO cache directive.
  P2  MIN CACHEABLE PREFIX-- sweep 64/128/256/512/1024/2048 target tokens; find the
                            smallest prefix that produces a hit on immediate resend.
                            DeepSeek claims 64-token storage units -- does caching
                            kick in near 64, or is there a higher floor?
  P6  SCOPING             -- two identical requests on OUR key hit; confirms the
                            cache is account/key-scoped (cross-key SKIPPED, above).
  P5  PEAK-HOUR PRICING   -- documentation + local-clock + receipt inspection: is
                            DeepSeek's announced 2x peak-hour surcharge live right
                            now, and does the receipt expose any per-call price?
SLOW (real wall-clock waits, one shared timeline, kept under ~18 min):
  P3  TTL CLIFF (bounded) -- three fresh prefixes written at t=0, each read EXACTLY
                            ONCE at 60/300/900 s. DeepSeek publishes NO TTL. A prior
                            Brevitas probe found the cache alive >55 min, so we do
                            NOT hunt the far edge; we BOUND it. Still a hit at 900 s
                            => "TTL > 15 min in our window, consistent with the
                            hours-long prior probe and the docs' 'hours to days'."
  P4  REFRESH-ON-READ     -- only meaningful if TTL is short. It is not, so this is
                            a note keyed off P3's 300 s hit (cache alive at 5 min);
                            no extra spend.

    BREVITAS_DEEPSEEK_PROBE=1 .venv/bin/python scripts/deepseek_cache_probe.py

Writes a machine-readable receipt dump to the scratchpad (or CWD) on completion
AND on a spend-guard abort, so partial data survives an early stop.
"""
from __future__ import annotations

import datetime
import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# --------------------------------------------------------------------------
# guard rails
# --------------------------------------------------------------------------

ACK_ENV = "BREVITAS_DEEPSEEK_PROBE"
SPEND_CAP_USD = 2.00

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env.local")
# The ONLY key this script is allowed to read out of .env.local. Anything else in
# that file (production creds, service-role keys, Stripe) is never parsed.
ALLOWED_ENV_PREFIX = "BREVITAS_PROBE_"
PROBE_KEY_NAME = "BREVITAS_PROBE_DEEPSEEK_KEY"

# deepseek-chat is the model named in the task and the row in
# brevitas/receipts.py MODEL_PRICES. Prices are $/Mtok, copied verbatim from that
# row and kept as literals so a mid-run repricing cannot perturb the cap
# arithmetic. DeepSeek caches AUTOMATICALLY and write-free, so there is no cache
# write rate: a miss is billed at the full input rate, a hit at the cached rate.
MODEL = "deepseek-chat"
PRICES = {"input": 0.14, "cached": 0.0028, "output": 0.28}

BASE_URL = "https://api.deepseek.com/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"

PREFIX_SEED = 20260810
# tok/word for this vocabulary lands ~1.3; refined from P1's first real receipt.
DEFAULT_TPW = 1.33

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
    def cache_hit(self) -> int:
        return int(self.usage.get("prompt_cache_hit_tokens") or 0)

    @property
    def cache_miss(self) -> int:
        # DeepSeek reports miss explicitly; fall back to prompt_tokens - hit.
        miss = self.usage.get("prompt_cache_miss_tokens")
        if miss is not None:
            return int(miss or 0)
        return max(0, int(self.usage.get("prompt_tokens") or 0) - self.cache_hit)

    @property
    def output_tokens(self) -> int:
        return int(self.usage.get("completion_tokens") or 0)

    @property
    def prompt_tokens(self) -> int:
        pt = self.usage.get("prompt_tokens")
        if pt is not None:
            return int(pt or 0)
        return self.cache_hit + self.cache_miss


def load_probe_key() -> str | None:
    """Read ONLY BREVITAS_PROBE_DEEPSEEK_KEY out of .env.local. Env wins."""
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
    out = [rng.choice(_VOCAB) for _ in range(max(1, words))]
    paras = [f"Synthetic cache-probe corpus {tag}. Fixed word list and seed; "
             f"contains no customer data of any kind."]
    for start in range(0, len(out), 60):
        paras.append(" ".join(out[start:start + 60]) + ".")
    return "\n\n".join(paras)


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def _post(url: str, key: str, body: dict, timeout: float = 120.0):
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("authorization", f"Bearer {key}")
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


# --------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------

def price(usage: dict) -> float:
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = max(0, int(usage.get("prompt_tokens") or 0) - hit)
    miss = int(miss or 0)
    out = int(usage.get("completion_tokens") or 0)
    return (miss * PRICES["input"] + hit * PRICES["cached"]
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
            print(f"    {tag}: prompt={call.prompt_tokens} "
                  f"hit={call.cache_hit} miss={call.cache_miss} "
                  f"out={call.output_tokens} spend=${call.spend_usd:.6f}  "
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


def chat_call(ledger: Ledger, key: str, probe: str, label: str, prefix: str,
              at_s: float = 0.0, projected_tokens: int = 9000,
              retry: bool = True) -> Call:
    """One live /chat/completions call, priced, capped, recorded. Retries once.

    No cache directives: DeepSeek caches automatically. The prefix is a system
    message; a tiny fixed user turn keeps the request byte-identical across legs
    so an immediate resend is a pure prefix match.
    """
    body = {
        "model": MODEL,
        "max_tokens": 6,
        "temperature": 0.0,
        "stream": False,
        "messages": [
            {"role": "system", "content": prefix},
            {"role": "user", "content": "Reply with the single word OK."},
        ],
    }
    call = Call(probe=probe, label=label, at_s=at_s)
    # Reserve against a cold miss of the whole prefix -- the worst case.
    ledger.guard(projected_tokens * PRICES["input"] / 1e6)
    for attempt in range(2 if retry else 1):
        try:
            status, data = _post(CHAT_URL, key, body)
            usage = dict(data.get("usage") or {})
        except Exception as exc:  # transport-level
            status, data, usage = 0, {"error": {"message": repr(exc)}}, {}
        if status == 200 and usage:
            call.status, call.usage = status, usage
            call.spend_usd = price(usage)
            break
        call.status = status
        call.error = str((data.get("error") or {}).get("message") or data)[:300]
        if attempt == 0 and retry:
            print(f"    ! {probe}/{label} status={status}: {call.error[:120]} "
                  f"-- retrying once", flush=True)
            time.sleep(3.0)
    ledger.record(call)
    return call


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

def probe_automatic(key: str, ledger: Ledger) -> tuple[list[Call], float]:
    """P1: write a prefix (miss), immediately resend identical (expect a hit),
    with NO cache directive of any kind. Also calibrates tok/word for P2."""
    print("\n=== P1  IS IT AUTOMATIC (fast) ===", flush=True)
    words = 1_500
    prefix = build_prefix("auto", words)
    c_write = chat_call(ledger, key, "P1", "write(miss)", prefix)
    c_read = chat_call(ledger, key, "P1", "resend(expect-hit)", prefix)
    tpw = DEFAULT_TPW
    if c_write.status == 200 and c_write.prompt_tokens > 0:
        tpw = c_write.prompt_tokens / words
    print(f"  calibration: {words} words -> {c_write.prompt_tokens} prompt "
          f"tokens ({tpw:.3f} tok/word)", flush=True)
    verdict = "AUTOMATIC (hit with no directive)" if c_read.cache_hit > 0 \
        else "NO HIT on immediate resend"
    print(f"  verdict: {verdict}", flush=True)
    return [c_write, c_read], tpw


def probe_min_prefix(key: str, ledger: Ledger, tpw: float) -> list[Call]:
    """P2: sweep target prefix sizes; find the smallest that hits on resend.

    Each size is a distinct fresh prefix, written once then immediately resent.
    prompt_cache_hit_tokens > 0 on the resend => that size caches. The smallest
    such measured prompt_tokens is the empirical floor. No wall-clock waits.
    """
    print("\n=== P2  MIN CACHEABLE PREFIX (fast) ===", flush=True)
    print(f"  using {tpw:.3f} tok/word from P1 calibration", flush=True)
    targets = [64, 128, 256, 512, 1024, 2048]
    calls: list[Call] = []
    for tgt in targets:
        words = max(1, round(tgt / tpw))
        prefix = build_prefix(f"min{tgt}", words)
        c_w = chat_call(ledger, key, "P2", f"~{tgt}tok/write", prefix,
                        projected_tokens=max(3000, tgt * 3))
        c_r = chat_call(ledger, key, "P2", f"~{tgt}tok/resend", prefix,
                        projected_tokens=max(3000, tgt * 3))
        cached = c_r.cache_hit > 0
        c_r.note = (f"target={tgt} words={words} "
                    f"measured_prompt={c_w.prompt_tokens} "
                    f"hit_on_resend={c_r.cache_hit} cached={cached}")
        print(f"    -> target~{tgt}: measured_prompt={c_w.prompt_tokens} "
              f"hit_on_resend={c_r.cache_hit} => "
              f"{'CACHED' if cached else 'not cached'}", flush=True)
        calls.extend([c_w, c_r])
    return calls


def probe_scoping(key: str, ledger: Ledger) -> list[Call]:
    """P6: two identical requests on OUR key -> the second hits, confirming the
    cache is account/key-scoped. Cross-key isolation is intentionally NOT tested
    (single probe key; cross-tenant probing is out of scope by guardrail)."""
    print("\n=== P6  SCOPING (fast; same-key only) ===", flush=True)
    prefix = build_prefix("scope", 1_500)
    c1 = chat_call(ledger, key, "P6", "req1(miss)", prefix)
    c2 = chat_call(ledger, key, "P6", "req2(expect-hit)", prefix)
    same_key_shares = c2.cache_hit > 0
    c2.note = ("same-key reuse hit; cross-key isolation NOT tested "
               "(out of scope: single probe key, no cross-tenant probing)")
    print(f"  same-key cache sharing: "
          f"{'YES (account-scoped)' if same_key_shares else 'no hit'}", flush=True)
    return [c1, c2]


def probe_peak_pricing(ledger: Ledger, sample: Call | None) -> dict:
    """P5: documentation + local-clock + receipt inspection. DeepSeek's receipt
    carries NO per-call price, so peak vs standard cannot be read off a receipt
    -- only the token counts are returned. We report the current Beijing clock,
    whether we sit inside the rumored peak windows, and confirm the receipt has
    no price field. Minimal/zero extra spend (reuses an existing receipt)."""
    print("\n=== P5  PEAK-HOUR PRICING (doc + clock + receipt) ===", flush=True)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    bj = now_utc
    if ZoneInfo is not None:
        try:
            bj = now_utc.astimezone(ZoneInfo("Asia/Shanghai"))
        except Exception:
            bj = now_utc + datetime.timedelta(hours=8)
    else:
        bj = now_utc + datetime.timedelta(hours=8)
    # Rumored V4 peak windows (Beijing): ~09:00-12:00 and ~14:00-18:00.
    h = bj.hour + bj.minute / 60.0
    in_peak = (9.0 <= h < 12.0) or (14.0 <= h < 18.0)
    receipt_has_price = False
    price_fields: list[str] = []
    if sample and sample.usage:
        for field_name in ("cost", "price", "amount", "usd", "billing"):
            if field_name in sample.usage:
                receipt_has_price = True
                price_fields.append(field_name)
    print(f"  UTC now:     {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC", flush=True)
    print(f"  Beijing now: {bj.strftime('%Y-%m-%d %H:%M:%S')} (h={h:.2f})", flush=True)
    print(f"  inside rumored peak window (09-12 / 14-18 Beijing): {in_peak}",
          flush=True)
    print(f"  receipt exposes a per-call price field: {receipt_has_price} "
          f"{price_fields}", flush=True)
    return {
        "utc": now_utc.isoformat(),
        "beijing": bj.isoformat(),
        "beijing_hour": round(h, 3),
        "inside_rumored_peak_window": in_peak,
        "receipt_has_price_field": receipt_has_price,
        "price_fields_seen": price_fields,
    }


# --------------------------------------------------------------------------
# SLOW probe (one shared timeline, <=18 min)
# --------------------------------------------------------------------------

TTL_GAPS = [60, 300, 900]  # 1 / 5 / 15 minutes


def run_slow(key: str, ledger: Ledger) -> list[Call]:
    print("\n=== P3  TTL CLIFF (bounded, slow) / P4 refresh note ===", flush=True)
    print("  timeline ~15 min; three fresh prefixes written at t=0, each read "
          "once at 60/300/900 s", flush=True)
    fan = {g: build_prefix(f"ttl{g}", 1_500) for g in TTL_GAPS}
    calls: list[Call] = []

    t0 = time.monotonic()
    print("t=0  writes (misses)", flush=True)
    for g in TTL_GAPS:
        c = chat_call(ledger, key, "P3", f"ttl{g}@write", fan[g], 0.0)
        c.note = f"write for gap={g}s"
        calls.append(c)

    for g in TTL_GAPS:
        sleep_until(t0, float(g))
        c = chat_call(ledger, key, "P3", f"ttl{g}@read(gap={g}s)", fan[g], float(g))
        hit = c.cache_hit > 0
        c.note = f"gap={g}s hit={c.cache_hit} miss={c.cache_miss} alive={hit}"
        print(f"    -> gap={g}s: hit={c.cache_hit} miss={c.cache_miss} => "
              f"{'ALIVE' if hit else 'EXPIRED'}", flush=True)
        calls.append(c)
    return calls


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def dump(ledger: Ledger, extra: dict | None = None) -> None:
    scratch = os.environ.get("BREVITAS_PROBE_OUT") or os.path.join(
        REPO_ROOT, "deepseek_cache_probe_receipts.json")
    payload = {
        "model": MODEL, "prices": PRICES, "spend_usd": ledger.spend_usd,
        "cap_usd": SPEND_CAP_USD, "extra": extra or {},
        "calls": [
            {"probe": c.probe, "label": c.label, "at_s": c.at_s,
             "status": c.status, "usage": c.usage, "spend_usd": c.spend_usd,
             "prompt_tokens": c.prompt_tokens, "cache_hit": c.cache_hit,
             "cache_miss": c.cache_miss, "output_tokens": c.output_tokens,
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
    print(f"deepseek cache probe: model={MODEL} cap=${SPEND_CAP_USD:.2f} "
          f"seed={PREFIX_SEED}", flush=True)

    ledger = Ledger()
    peak: dict = {}
    try:
        # FAST first, so we keep data even if budget/time runs short.
        auto_calls, tpw = probe_automatic(key, ledger)
        probe_min_prefix(key, ledger, tpw)
        probe_scoping(key, ledger)
        sample = next((c for c in ledger.calls if c.status == 200), None)
        peak = probe_peak_pricing(ledger, sample)
        print(f"\n--- fast probes done, running spend ${ledger.spend_usd:.6f} ---",
              flush=True)
        run_slow(key, ledger)
    except RuntimeError as exc:
        print(f"\nSPEND GUARD: {exc}", flush=True)
        dump(ledger, {"peak_pricing": peak})
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
        dump(ledger, {"peak_pricing": peak})
        return 1

    dump(ledger, {"peak_pricing": peak})
    print(f"\nTOTAL LIVE SPEND ${ledger.spend_usd:.6f}  (cap ${SPEND_CAP_USD:.2f})",
          flush=True)
    assert ledger.spend_usd < SPEND_CAP_USD, (
        f"probe spent ${ledger.spend_usd:.6f}, over the ${SPEND_CAP_USD:.2f} cap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
