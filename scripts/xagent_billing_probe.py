#!/usr/bin/env python3
"""Cross-agent prompt-cache BILLING probe (Anthropic).

Three experiments the existing cross_agent_cache_probe.py does not run, each
targeting a specific referee objection to the "cross-agent sharing works"
result:

  X1 CONCURRENCY FAN-OUT.  The existing result is serial by construction: agent
     1 writes, then agents 2..N read.  Real agent fan-out is CONCURRENT.  If k>1
     callers are each billed cache_creation when they race, the closed-form
     saving identity is wrong in exactly the regime practitioners deploy.
     We fire N callers at one fresh prefix with controlled inter-arrival skew
     and count how many pay the write.

  X2 REUSE-DEPTH CURVE.  The published "+24.12% for caching without reordering"
     is the definitional cost of marking a prefix that is never reused: with one
     call per agent the cached arm pays the 1.25x write premium and collects
     zero reads.  We measure the penalty as a function of reuse depth k and
     locate the sign flip, which is the number a practitioner actually needs.

  X3 TOLERANCE ENVELOPE.  Which perturbations an integrator plausibly introduces
     actually destroy the hit?  Leading whitespace, unicode normalisation, JSON
     key ordering, and single-token insertions at position 0 / middle / tail.

Guardrails (same discipline as the existing probes):
  * runs only when BREVITAS_XAGENT_PROBE=1
  * probe key only, read from .env.local under the BREVITAS_PROBE_ prefix
  * hard list-price spend cap, checked before every call
  * synthetic seeded prefixes, never customer data
  * behavioural characterisation only: our own key reading our own prefixes.
    No cross-tenant access, no rate-limit probing, no malformed traffic.

Usage:
    BREVITAS_XAGENT_PROBE=1 .venv/bin/python scripts/xagent_billing_probe.py \
        --out receipts.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

ACK_ENV = "BREVITAS_XAGENT_PROBE"
PROBE_KEY_NAME = "BREVITAS_PROBE_ANTHROPIC_KEY"
ALLOWED_ENV_PREFIX = "BREVITAS_PROBE_"
ENV_FILE = ".env.local"

BASE_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5"
API_VERSION = "2023-06-01"

SPEND_CAP_USD = 3.00

# $/Mtok, marginally above public haiku list so the cap over-books.
PRICES = {
    "input": 1.00,
    "cache_read": 0.10,
    "cache_write_5m": 1.25,
    "output": 5.00,
}

# claude-haiku-4-5 minimum cacheable prefix is ~4096 tokens; overshoot so every
# arm is unambiguously above the floor.
PREFIX_WORDS = 4200
PREFIX_SEED = 20260814

_VOCAB = [
    "ledger", "prefix", "cache", "token", "receipt", "invoice", "hazard",
    "arrival", "breakpoint", "residency", "quantum", "tier", "settle", "probe",
    "corpus", "window", "policy", "holdout", "canary", "scope", "credential",
    "workspace", "fanout", "reorder", "canonical", "boundary", "eviction",
    "premium", "multiplier", "denominator", "attribution", "counterfactual",
]


# --------------------------------------------------------------------------
# key + transport
# --------------------------------------------------------------------------

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


def _post(key: str, body: dict, timeout: float = 120.0):
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE_URL, data=payload, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", API_VERSION)
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
    except Exception as exc:  # network-level failure
        return 0, {"error": {"message": f"{type(exc).__name__}: {exc}"}}


def build_prefix(tag: str, words: int = PREFIX_WORDS) -> str:
    rng = random.Random(f"{PREFIX_SEED}:{tag}")
    out = [rng.choice(_VOCAB) for _ in range(max(1, words))]
    paras = [f"Synthetic cache-probe corpus {tag}. Fixed word list and seed; "
             f"contains no customer data of any kind."]
    for start in range(0, len(out), 60):
        paras.append(" ".join(out[start:start + 60]) + ".")
    return "\n\n".join(paras)


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------

@dataclass
class Call:
    label: str
    status: int
    usage: dict
    wall_ms: float
    err: str = ""

    @property
    def read(self) -> int:
        return int(self.usage.get("cache_read_input_tokens") or 0)

    @property
    def create(self) -> int:
        return int(self.usage.get("cache_creation_input_tokens") or 0)

    @property
    def fresh(self) -> int:
        return int(self.usage.get("input_tokens") or 0)

    @property
    def out(self) -> int:
        return int(self.usage.get("output_tokens") or 0)

    @property
    def usd(self) -> float:
        m = 1e-6
        return (self.fresh * PRICES["input"]
                + self.read * PRICES["cache_read"]
                + self.create * PRICES["cache_write_5m"]
                + self.out * PRICES["output"]) * m


class Ledger:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.spend = 0.0
        self._lock = threading.Lock()

    def guard(self, projected: float) -> None:
        if self.spend + projected > SPEND_CAP_USD:
            raise SystemExit(
                f"ABORT: projected ${self.spend + projected:.4f} exceeds "
                f"${SPEND_CAP_USD:.2f} cap")

    def record(self, c: Call) -> None:
        with self._lock:
            self.calls.append(c)
            self.spend += c.usd
        if self.spend > SPEND_CAP_USD:
            raise SystemExit(f"ABORT: spend ${self.spend:.4f} over cap")


def call(ledger: Ledger, key: str, label: str, prefix: str,
         question: str = "Reply with the single word: ok.",
         cache: bool = True, max_tokens: int = 8,
         system_extra: str = "") -> Call:
    ledger.guard(0.02)
    block = {"type": "text", "text": prefix}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]
    if system_extra:
        system.append({"type": "text", "text": system_extra})
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": question}],
    }
    t0 = time.time()
    status, resp = _post(key, body)
    wall = (time.time() - t0) * 1000.0
    if status != 200:
        c = Call(label, status, {}, wall, str(resp.get("error", {}).get("message"))[:200])
    else:
        c = Call(label, status, resp.get("usage") or {}, wall)
    ledger.record(c)
    return c


# --------------------------------------------------------------------------
# X1 concurrency fan-out
# --------------------------------------------------------------------------

def x1_concurrency(ledger: Ledger, key: str, n: int = 5,
                   skews_ms=(0, 50, 200, 1000, 5000), reps: int = 2) -> list[dict]:
    """Fire n callers at one FRESH prefix with controlled skew; count writers."""
    rows = []
    for skew in skews_ms:
        for rep in range(reps):
            nonce = uuid.uuid4().hex
            prefix = build_prefix(f"x1-{skew}-{rep}-{nonce}")
            results: list[Call] = []
            lock = threading.Lock()

            def worker(i: int) -> None:
                time.sleep(i * skew / 1000.0)
                c = call(ledger, key, f"x1:skew{skew}:rep{rep}:a{i}", prefix)
                with lock:
                    results.append(c)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            ok = [c for c in results if c.status == 200]
            writers = sum(1 for c in ok if c.create > 0)
            readers = sum(1 for c in ok if c.read > 0)
            rows.append({
                "skew_ms": skew, "rep": rep, "n": n,
                "ok": len(ok),
                "writers": writers,
                "readers": readers,
                "write_tokens": sum(c.create for c in ok),
                "read_tokens": sum(c.read for c in ok),
                "usd": sum(c.usd for c in ok),
            })
            print(f"  X1 skew={skew:>5}ms rep{rep}: {writers} writer(s), "
                  f"{readers} reader(s) of {len(ok)} ok  "
                  f"[${ledger.spend:.4f}/${SPEND_CAP_USD:.2f}]")
            # let the 5m entry age out of the way of the next cell
            time.sleep(1.0)
    return rows


# --------------------------------------------------------------------------
# X2 reuse-depth curve
# --------------------------------------------------------------------------

def x2_reuse_depth(ledger: Ledger, key: str, depths=(1, 2, 3, 5, 10),
                   reps: int = 2) -> list[dict]:
    """Cost of the CACHED arm vs the UNCACHED arm as a function of reuse depth."""
    rows = []
    for k in depths:
        for rep in range(reps):
            out = {}
            for arm_cached in (True, False):
                nonce = uuid.uuid4().hex
                prefix = build_prefix(f"x2-{k}-{rep}-{arm_cached}-{nonce}")
                total = 0.0
                for i in range(k):
                    c = call(ledger, key,
                             f"x2:d{k}:rep{rep}:{'cached' if arm_cached else 'plain'}:{i}",
                             prefix, cache=arm_cached)
                    total += c.usd
                out["cached" if arm_cached else "plain"] = total
            delta = (out["cached"] - out["plain"]) / out["plain"] * 100.0 if out["plain"] else 0.0
            rows.append({"depth": k, "rep": rep,
                         "cached_usd": out["cached"], "plain_usd": out["plain"],
                         "cached_vs_plain_pct": delta})
            print(f"  X2 depth={k:>2} rep{rep}: cached ${out['cached']:.6f} vs "
                  f"plain ${out['plain']:.6f}  -> {delta:+.2f}%  "
                  f"[${ledger.spend:.4f}]")
    return rows


# --------------------------------------------------------------------------
# X3 tolerance envelope
# --------------------------------------------------------------------------

def _one_token_insert(text: str, where: str) -> str:
    parts = text.split(" ")
    tok = "ZZQX"
    if where == "head":
        parts.insert(1, tok)
    elif where == "mid":
        parts.insert(len(parts) // 2, tok)
    else:
        parts.append(tok)
    return " ".join(parts)


def x3_tolerance(ledger: Ledger, key: str, reps: int = 2) -> list[dict]:
    """Write a prefix, then read it back under a perturbation; report recovery."""
    axes = {
        "identical": lambda s: s,
        "leading_space": lambda s: " " + s,
        "trailing_space": lambda s: s + " ",
        "crlf": lambda s: s.replace("\n", "\r\n"),
        "nfd_unicode": lambda s: unicodedata.normalize("NFD", s + " café"),
        "nfc_unicode": lambda s: unicodedata.normalize("NFC", s + " café"),
        "insert_head": lambda s: _one_token_insert(s, "head"),
        "insert_mid": lambda s: _one_token_insert(s, "mid"),
        "insert_tail": lambda s: _one_token_insert(s, "tail"),
        "case_flip_head": lambda s: s[:40].upper() + s[40:],
    }
    rows = []
    for rep in range(reps):
        for name, fn in axes.items():
            nonce = uuid.uuid4().hex
            base = build_prefix(f"x3-{name}-{rep}-{nonce}")
            w = call(ledger, key, f"x3:{name}:rep{rep}:write", base)
            if w.status != 200:
                print(f"  X3 {name}: WRITE FAILED {w.err}")
                continue
            r = call(ledger, key, f"x3:{name}:rep{rep}:read", fn(base))
            written = w.create
            recovery = (r.read / written) if written else 0.0
            rows.append({"axis": name, "rep": rep,
                         "written": written, "read": r.read,
                         "recovery": recovery})
            print(f"  X3 {name:>16}: wrote {written:>6} read {r.read:>6} "
                  f"-> recovery {recovery*100:6.1f}%  [${ledger.spend:.4f}]")
    return rows


# --------------------------------------------------------------------------
# X4 breakpoint granularity
# --------------------------------------------------------------------------

def _seg_call(ledger: Ledger, key: str, label: str, segments: list[str],
              max_tokens: int = 8) -> Call:
    """Send a system prompt split into segments, each its own cache breakpoint.

    Anthropic caps cache_control at 4 breakpoints, so segments[:4] are marked.
    """
    ledger.guard(0.02)
    system = []
    for i, s in enumerate(segments):
        blk = {"type": "text", "text": s}
        if i < 4:
            blk["cache_control"] = {"type": "ephemeral"}
        system.append(blk)
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": "Reply with the single word: ok."}],
    }
    t0 = time.time()
    status, resp = _post(key, body)
    wall = (time.time() - t0) * 1000.0
    if status != 200:
        c = Call(label, status, {}, wall,
                 str(resp.get("error", {}).get("message"))[:200])
    else:
        c = Call(label, status, resp.get("usage") or {}, wall)
    ledger.record(c)
    return c


def x4_breakpoint_granularity(ledger: Ledger, key: str, nseg: int = 4,
                              reps: int = 2) -> list[dict]:
    """Does invalidation quantise to the BREAKPOINT or to the token?

    Split the prefix into nseg equally sized, individually cached segments.
    Write it, then perturb exactly one token inside segment j and re-send.

    If invalidation is breakpoint-granular, perturbing segment j must destroy
    segments j..nseg and preserve segments 1..j-1 exactly, so recovery should
    land on the staircase (j-1)/nseg. If it were token-granular, recovery would
    be near 1 - 1/total_tokens everywhere.
    """
    rows = []
    for rep in range(reps):
        for j in range(nseg + 1):  # j == nseg means "perturb after the last breakpoint"
            nonce = uuid.uuid4().hex
            segs = [build_prefix(f"x4-{rep}-{nonce}-s{i}", PREFIX_WORDS // nseg + 400)
                    for i in range(nseg)]
            w = _seg_call(ledger, key, f"x4:rep{rep}:seg{j}:write", segs)
            if w.status != 200:
                print(f"  X4 seg{j}: WRITE FAILED {w.err}")
                continue
            if j < nseg:
                pert = list(segs)
                pert[j] = _one_token_insert(pert[j], "mid")
            else:
                # perturbation lands in the uncached tail, past every breakpoint
                pert = list(segs) + ["Volatile tail: " + uuid.uuid4().hex]
            r = _seg_call(ledger, key, f"x4:rep{rep}:seg{j}:read", pert)
            written = w.create
            recovery = (r.read / written) if written else 0.0
            where = f"segment {j+1}/{nseg}" if j < nseg else "past last breakpoint"
            rows.append({"rep": rep, "perturbed_segment": j, "nseg": nseg,
                         "written": written, "read": r.read,
                         "recovery": recovery,
                         "predicted_if_breakpoint_granular": j / nseg if j < nseg else 1.0})
            print(f"  X4 perturb {where:>22}: wrote {written:>6} read {r.read:>6} "
                  f"-> recovery {recovery*100:6.1f}%  "
                  f"(breakpoint-granular predicts {(j/nseg if j<nseg else 1.0)*100:.0f}%)"
                  f"  [${ledger.spend:.4f}]")
    return rows


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="xagent_receipts.json")
    ap.add_argument("--only", nargs="*", default=["x1", "x2", "x3", "x4"])
    ap.add_argument("--reps", type=int, default=2)
    args = ap.parse_args()

    if os.environ.get(ACK_ENV) != "1":
        print(f"refusing to run: set {ACK_ENV}=1 to acknowledge live spend "
              f"(cap ${SPEND_CAP_USD:.2f})")
        return 2
    key = load_probe_key()
    if not key:
        print(f"no {PROBE_KEY_NAME} found in env or {ENV_FILE}")
        return 2

    ledger = Ledger()
    result: dict = {"model": MODEL, "cap_usd": SPEND_CAP_USD,
                    "prefix_words": PREFIX_WORDS, "reps": args.reps}

    # sanity: one write/read pair must behave, or the harness is not measuring
    # what it thinks it is.
    nonce = uuid.uuid4().hex
    p = build_prefix(f"sanity-{nonce}")
    w = call(ledger, key, "sanity:write", p)
    if w.status != 200:
        print(f"sanity write failed: {w.status} {w.err}")
        return 1
    r = call(ledger, key, "sanity:read", p)
    print(f"sanity: wrote {w.create} tok, read back {r.read} tok "
          f"(fresh={r.fresh})  [${ledger.spend:.4f}]")
    if w.create <= 0:
        print("ABORT: prefix did not cache -- below floor? cannot proceed.")
        return 1
    result["sanity"] = {"write_create": w.create, "read_read": r.read}

    if "x1" in args.only:
        print("\nX1 concurrency fan-out (how many callers pay the write?)")
        result["x1_concurrency"] = x1_concurrency(ledger, key, reps=args.reps)
    if "x2" in args.only:
        print("\nX2 reuse-depth curve (where does the caching penalty flip?)")
        result["x2_reuse_depth"] = x2_reuse_depth(ledger, key, reps=args.reps)
    if "x3" in args.only:
        print("\nX3 tolerance envelope (which perturbations kill the hit?)")
        result["x3_tolerance"] = x3_tolerance(ledger, key, reps=args.reps)
    if "x4" in args.only:
        print("\nX4 breakpoint granularity (does invalidation quantise to the breakpoint?)")
        result["x4_breakpoint"] = x4_breakpoint_granularity(ledger, key, reps=args.reps)

    result["total_spend_usd"] = ledger.spend
    result["calls"] = [
        {"label": c.label, "status": c.status, "usage": c.usage,
         "wall_ms": round(c.wall_ms, 1), "usd": c.usd, "err": c.err}
        for c in ledger.calls
    ]
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=1)
    print(f"\ntotal live spend ${ledger.spend:.4f} of ${SPEND_CAP_USD:.2f} cap")
    print(f"receipts -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
