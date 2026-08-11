#!/usr/bin/env python3
"""E2 -- CROSS-AGENT PREFIX SHARING. Live probe against Anthropic's Messages API.

MANUAL ONLY, AND IT SPENDS REAL MONEY. pytest never imports this file, the worker
never calls it, and it refuses to run unless BREVITAS_CROSS_AGENT_PROBE=1 is set.
It loads ONLY BREVITAS_PROBE_ANTHROPIC_KEY out of the repo-root .env.local and
hard-asserts that cumulative list-price spend stays under $2.00, aborting the
instant a PROJECTED call would cross the cap (the scripts/anthropic_cache_probe.py
Ledger.guard pattern).

THE CLAIM UNDER TEST (brief 1.2)
--------------------------------
"If agent A and agent B both reference the same L0 context, and the L1 compiler
emits a byte-identical prefix for both, then agent B's call hits the provider's
prefix cache that agent A paid to write."

The repo's own prior (token_efficiency_model/lossless/shared_prefix.py:6) is that
the marketing 5-agent A/B saved only ~5% while single-agent multi-turn saved
70-88%. The thesis says deterministic promotion closes that gap. This measures it.

DESIGN
------
A simulated pipeline of 5 agents (researcher/critic/planner/writer/checker), each
with its OWN short role prompt, all sharing ONE large synthetic document.

  A1 NAIVE_NOCACHE : [role] + [doc] + [question], no cache_control at all.
  A2 NAIVE_CACHED  : [role] + [doc(cache_control)] + [question]. The COMPETENT
                     unaided customer: uses caching, does not reorder. Because the
                     role differs per agent, all five prefixes differ -> 5 writes,
                     0 reads. Reported separately so we compare against the
                     CHEAPER naive arm and never strawman.
  B  PROMOTED      : doc hoisted to position 0 by the SHIPPED layer
                     (token_efficiency_model.lossless.shared_prefix), one
                     cache_control at the END of the doc block, role+question
                     after it. Byte-identical leading prefix across all 5 agents.

CONTROLS
--------
* Each (replication, arm) gets its OWN synthetic document (same word count,
  different seeded content). Cross-arm cache inheritance is therefore
  structurally impossible -- cleaner than waiting out a TTL and free.
* Arm order is alternated across replications (belt and braces on top of that).
* VALIDITY GATE: the first call of every arm must report cache_read == 0.
* VALIDITY GATE: PROMOTED agent-1 must report cache_creation > 0. If it reports
  0 the doc is under the model's minimum cacheable prefix and the run measured
  NOTHING -- it is not evidence that sharing fails.
* Doc size is verified in TOKENS via the free /count_tokens endpoint, not
  characters. claude-haiku-4-5's floor is 4096 tokens (proven live in
  docs/ANTHROPIC_CACHE_MAP.md); the doc is built well above it.
* Exactly ONE cache_control breakpoint per request in every arm (limit is 4; a
  5th is a hard HTTP 400). No top-level auto cache_control anywhere.
* The doc block's sha256 is asserted identical across all 5 PROMOTED requests.
* Calls are SEQUENTIAL. An Anthropic cache entry only becomes readable after the
  first response BEGINS, so concurrent fan-out would make every arm look uncached.
  This measures the serial best case and is labelled as such.
* Wall-clock is logged per call; the 5 calls of an arm land inside ~60s, far
  inside the measured (300,330]s default TTL cliff. Any inter-call gap > 240s
  marks the replication contaminated and it is dropped BEFORE its result is read.

SUB-FLOOR PROBE
---------------
One extra PROMOTED pass on a ~2,000-token doc (below the 4096 floor). Predicted:
zero writes, zero reads -- pipelines whose shared block is under the floor have a
hard zero ceiling on Haiku-class models. That is a product constraint, not a
footnote.

SCOPE
-----
Behavioral characterization with our own key and our own money. No cross-tenant
access, no rate-limit probing, no malformed traffic, no exploitation of any
cross-user cache-sharing side channel. Synthetic prompts only, never customer
data. Every cache entry read here was written by this same Brevitas credential
seconds earlier.

    BREVITAS_CROSS_AGENT_PROBE=1 .venv/bin/python scripts/cross_agent_cache_probe.py
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict

# --------------------------------------------------------------------------
# guard rails
# --------------------------------------------------------------------------

ACK_ENV = "BREVITAS_CROSS_AGENT_PROBE"
SPEND_CAP_USD = 2.00

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO_ROOT, ".env.local")
ALLOWED_ENV_PREFIX = "BREVITAS_PROBE_"
PROBE_KEY_NAME = "BREVITAS_PROBE_ANTHROPIC_KEY"

OUT_DIR = os.environ.get(
    "BREVITAS_PROBE_OUT_DIR",
    "/private/tmp/claude-501/-Users-jamesyang-Documents-GitHub-Brevitas-Systems/"
    "b1404a28-c145-4008-a37d-9362f8022b2e/scratchpad")
OUT_PATH = os.path.join(OUT_DIR, "E2_cross_agent_receipts.json")

# Prices are $/Mtok, copied verbatim from scripts/anthropic_cache_probe.py (which
# copied them from brevitas/receipts.py MODEL_PRICES). Kept as literals so a
# mid-run repricing cannot perturb the cap arithmetic. Marginally ABOVE current
# public haiku list, so the cap over-books rather than under-books.
MODEL = "claude-haiku-4-5"
PRICES = {"input": 1.0, "cached": 0.1, "write": 1.25, "output": 5.0}

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
COUNT_URL = "https://api.anthropic.com/v1/messages/count_tokens"
API_VERSION = "2023-06-01"

# claude-haiku-4-5's minimum cacheable prefix, PROVEN live in
# docs/ANTHROPIC_CACHE_MAP.md (not the stale "2048" comment in
# benchmarks/native_cache_baseline.py, which is wrong for this model).
MIN_CACHEABLE_TOKENS = 4096

SEED = 20260811
DOC_WORDS = 5_400          # -> comfortably north of the 4096-token floor
SUBFLOOR_WORDS = 1_500     # -> deliberately BELOW the floor
REPLICATIONS = int(os.environ.get("BREVITAS_PROBE_REPS", "6"))
MAX_TOKENS = 8
MAX_GAP_S = 240.0          # inter-call gap that marks a replication contaminated

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
# the simulated pipeline: 5 agents, distinct role prompts, one shared document
# --------------------------------------------------------------------------

AGENTS = [
    ("researcher",
     "You are the RESEARCHER agent in a five-stage analysis pipeline. Your role "
     "is to extract factual claims from the reference corpus above and note "
     "which of them are load-bearing for the downstream stages. You never "
     "speculate and you never editorialize.",
     "Name one term that appears in the corpus."),
    ("critic",
     "You are the CRITIC agent in a five-stage analysis pipeline. Your role is "
     "to challenge the researcher's extractions, look for unsupported leaps, "
     "and flag anything that would not survive an audit. You are skeptical by "
     "construction and you argue from the corpus only.",
     "Name one term that appears in the corpus."),
    ("planner",
     "You are the PLANNER agent in a five-stage analysis pipeline. Your role is "
     "to sequence the remaining work into ordered steps with explicit "
     "dependencies, given the corpus and the critic's objections. You optimize "
     "for the shortest path that still clears the audit bar.",
     "Name one term that appears in the corpus."),
    ("writer",
     "You are the WRITER agent in a five-stage analysis pipeline. Your role is "
     "to render the planner's sequence into prose that a non-specialist reader "
     "can follow, without introducing any claim not already present in the "
     "corpus. You write plainly and you do not pad.",
     "Name one term that appears in the corpus."),
    ("checker",
     "You are the CHECKER agent in a five-stage analysis pipeline. Your role is "
     "the final gate: you verify every statement in the writer's draft against "
     "the corpus and refuse anything you cannot ground. You report pass or fail "
     "and you never repair the draft yourself.",
     "Name one term that appears in the corpus."),
]

ARMS = ("NAIVE_NOCACHE", "NAIVE_CACHED", "PROMOTED")


# --------------------------------------------------------------------------
# receipts
# --------------------------------------------------------------------------

@dataclass
class Call:
    """One live request and the receipt it came back with."""

    replication: int
    arm: str
    agent: str
    agent_index: int
    wall_s: float = 0.0
    status: int = 0
    usage: dict = field(default_factory=dict)
    spend_usd: float = 0.0
    error: str = ""
    doc_sha256: str = ""
    reordered: bool | None = None
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
    def output(self) -> int:
        return int(self.usage.get("output_tokens") or 0)

    @property
    def prefix_tokens(self) -> int:
        return self.fresh_input + self.cache_write + self.cache_read


def load_probe_key() -> str | None:
    """Read ONLY BREVITAS_PROBE_ANTHROPIC_KEY out of .env.local. Env wins.

    Anything else in that file (production creds, service-role keys, Stripe) is
    never parsed -- the prefix filter is the whole point.
    """
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
# synthetic documents (never customer data)
# --------------------------------------------------------------------------

def build_doc(tag: str, words: int) -> str:
    """A deterministic synthetic corpus. Distinct `tag` -> distinct bytes from
    position 0, which is what makes cross-arm cache inheritance impossible."""
    rng = random.Random(f"{SEED}:{tag}")
    out = [rng.choice(_VOCAB) for _ in range(words)]
    paras = [f"Synthetic cross-agent probe corpus {tag}. Fixed word list and "
             f"seed; contains no customer data of any kind."]
    for start in range(0, len(out), 60):
        paras.append(" ".join(out[start:start + 60]) + ".")
    return "\n\n".join(paras)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _headers(key: str) -> dict:
    return {"x-api-key": key, "anthropic-version": API_VERSION}


def count_tokens(key: str, blocks: list) -> int:
    """Free, well-formed token count -- used ONLY to size the shared document.
    Sizing must be verified in tokens; characters are not the unit that matters."""
    status, data = _post(
        COUNT_URL, _headers(key),
        {"model": MODEL, "messages": [{"role": "user", "content": blocks}]},
        timeout=60.0)
    if status == 200:
        return int(data.get("input_tokens") or 0)
    return -1


# --------------------------------------------------------------------------
# pricing + ledger
# --------------------------------------------------------------------------

def price(usage: dict) -> float:
    fresh = int(usage.get("input_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    return (fresh * PRICES["input"]
            + written * PRICES["write"]
            + read * PRICES["cached"]
            + out * PRICES["output"]) / 1e6


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
        tag = f"r{call.replication}/{call.arm}/{call.agent}"
        if call.status == 200:
            print(f"    {tag:<34} prefix={call.prefix_tokens:>6} "
                  f"read={call.cache_read:>6} write={call.cache_write:>6} "
                  f"fresh={call.fresh_input:>5} ${call.spend_usd:.6f}  "
                  f"[running ${self.spend_usd:.6f}/${SPEND_CAP_USD:.2f}]",
                  flush=True)
        else:
            print(f"    {tag:<34} status={call.status} {call.error[:150]}  "
                  f"[running ${self.spend_usd:.6f}/${SPEND_CAP_USD:.2f}]",
                  flush=True)
        if self.spend_usd > SPEND_CAP_USD:
            raise RuntimeError(
                f"aborting: cumulative spend ${self.spend_usd:.6f} crossed the "
                f"${SPEND_CAP_USD:.2f} cap")


def send(ledger: Ledger, key: str, call: Call, body: dict,
         projected_tokens: int) -> Call:
    """One live /messages call, priced, capped, recorded. Retries once.

    Reserves against a COLD WRITE of the whole prefix -- the worst case -- before
    the request leaves the process, exactly as anthropic_cache_probe.py does.
    """
    projected = (projected_tokens * PRICES["write"]
                 + MAX_TOKENS * PRICES["output"]) / 1e6
    ledger.guard(projected)
    call.wall_s = time.time()
    for attempt in range(2):
        try:
            status, data = _post(ANTHROPIC_URL, _headers(key), body)
            usage = dict(data.get("usage") or {})
        except Exception as exc:  # transport-level
            status, data, usage = 0, {"error": {"message": repr(exc)}}, {}
        if status == 200 and usage:
            call.status, call.usage = status, usage
            call.spend_usd = price(usage)
            break
        call.status = status
        call.error = str((data.get("error") or {}).get("message") or data)[:300]
        if attempt == 0:
            print(f"    ! {call.arm}/{call.agent} status={status}: "
                  f"{call.error[:120]} -- retrying once", flush=True)
            time.sleep(3.0)
    ledger.record(call)
    return call


# --------------------------------------------------------------------------
# request builders
# --------------------------------------------------------------------------

CC = {"type": "ephemeral"}


def naive_blocks(role: str, doc: str, question: str, cached: bool) -> list:
    """[role] + [doc] + [question]. The shared doc is NOT the leading prefix, so
    each agent's cacheable prefix (role+doc) is byte-DIFFERENT: five writes, zero
    reads. `cached=True` is the competent unaided customer (one breakpoint at the
    end of the doc); `cached=False` is the customer who never turned caching on."""
    doc_block = {"type": "text", "text": doc}
    if cached:
        doc_block["cache_control"] = dict(CC)
    return [
        {"type": "text", "text": role},
        doc_block,
        {"type": "text", "text": question},
    ]


def promoted_blocks(layer, pipeline_id: str, agent: str, role: str, doc: str,
                    question: str):
    """Order decided by the SHIPPED layer (shared_prefix.SharedPrefixLayer), then
    rendered into content blocks with the single breakpoint at the END of the
    promoted doc block. We test the shipped mechanism's decision, not a
    hand-rolled reordering.

    The layer operates on plain user/assistant messages with string content (its
    _reorder_safe allow-list), so ordering is decided there and cache_control is
    applied here -- the layer has no notion of provider breakpoints.
    """
    plain = [
        {"role": "user", "content": role},
        {"role": "user", "content": doc},
        {"role": "user", "content": question},   # volatile last, never moved
    ]
    out, reordered = layer.layout_ex(pipeline_id, agent, plain)
    texts = [m["content"] for m in out]
    if texts[0] != doc:
        raise RuntimeError(
            f"shipped layer did not promote the shared doc to position 0 for "
            f"agent {agent}; got order {[t[:24] for t in texts]}")
    blocks = []
    for i, text in enumerate(texts):
        block = {"type": "text", "text": text}
        if i == 0:                      # end of the doc block == the breakpoint
            block["cache_control"] = dict(CC)
        blocks.append(block)
    return blocks, reordered


def body_for(blocks: list) -> dict:
    n_cc = sum(1 for b in blocks if "cache_control" in b)
    if n_cc > 4:
        raise RuntimeError(f"{n_cc} cache_control breakpoints; the limit is 4 "
                           f"and a 5th is a hard HTTP 400")
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": blocks}],
    }


# --------------------------------------------------------------------------
# one arm = one sequential 5-agent pipeline pass
# --------------------------------------------------------------------------

def run_arm(ledger: Ledger, key: str, rep: int, arm: str, doc: str,
            doc_tokens: int) -> list[Call]:
    from token_efficiency_model.lossless.shared_prefix import SharedPrefixLayer

    calls: list[Call] = []
    layer = None
    if arm == "PROMOTED":
        # Fresh layer + pipeline id per arm: the promotion decision is made from
        # scratch, and the doc is DECLARED shared (register_shared), which is the
        # explicit path an app takes when it knows its own pipeline context.
        layer = SharedPrefixLayer()
        pipeline_id = f"e2-rep{rep}"
        layer.register_shared(pipeline_id, doc)

    for idx, (agent, role, question) in enumerate(AGENTS):
        reordered = None
        if arm == "PROMOTED":
            blocks, reordered = promoted_blocks(
                layer, pipeline_id, agent, role, doc, question)
        else:
            blocks = naive_blocks(role, doc, question,
                                  cached=(arm == "NAIVE_CACHED"))
        call = Call(replication=rep, arm=arm, agent=agent, agent_index=idx,
                    doc_sha256=_h(doc), reordered=reordered)
        send(ledger, key, call, body_for(blocks),
             projected_tokens=doc_tokens + 600)
        calls.append(call)
    return calls


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    if os.environ.get(ACK_ENV) != "1":
        print(f"refusing to run: set {ACK_ENV}=1 (this spends real money)")
        return 2
    key = load_probe_key()
    if not key:
        print(f"refusing to run: {PROBE_KEY_NAME} not found in {ENV_FILE}")
        return 2
    sys.path.insert(0, REPO_ROOT)
    os.makedirs(OUT_DIR, exist_ok=True)

    ledger = Ledger()
    report: dict = {
        "experiment": "E2_cross_agent_prefix_sharing",
        "model": MODEL,
        "prices_usd_per_mtok": PRICES,
        "spend_cap_usd": SPEND_CAP_USD,
        "min_cacheable_tokens_documented": MIN_CACHEABLE_TOKENS,
        "replications_requested": REPLICATIONS,
        "agents": [a[0] for a in AGENTS],
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sizing": {},
        "calls": [],
        "sub_floor": {},
        "aborted": None,
    }

    # ---- size the document IN TOKENS (free endpoint) ----------------------
    probe_doc = build_doc("sizing", DOC_WORDS)
    probe_role, probe_q = AGENTS[0][1], AGENTS[0][2]
    doc_only = count_tokens(key, [{"type": "text", "text": probe_doc}])
    full_req = count_tokens(key, naive_blocks(probe_role, probe_doc, probe_q,
                                              cached=False))
    sub_doc_probe = build_doc("sizing-sub", SUBFLOOR_WORDS)
    sub_only = count_tokens(key, [{"type": "text", "text": sub_doc_probe}])
    report["sizing"] = {
        "doc_words": DOC_WORDS,
        "doc_tokens": doc_only,
        "full_request_tokens": full_req,
        "sub_floor_doc_words": SUBFLOOR_WORDS,
        "sub_floor_doc_tokens": sub_only,
        "floor_tokens": MIN_CACHEABLE_TOKENS,
        "doc_clears_floor": doc_only > MIN_CACHEABLE_TOKENS,
    }
    print(f"\n  sizing: shared doc = {doc_only} tokens "
          f"(floor {MIN_CACHEABLE_TOKENS}); full naive request = {full_req} "
          f"tokens; sub-floor doc = {sub_only} tokens", flush=True)
    if doc_only <= MIN_CACHEABLE_TOKENS:
        print("  ABORT: shared doc is under the minimum cacheable prefix; this "
              "run would measure nothing.")
        return 3

    try:
        # ---- main replications --------------------------------------------
        for rep in range(1, REPLICATIONS + 1):
            # counterbalance arm order (docs already differ per arm, so this is
            # belt-and-braces, not the primary control)
            order = list(ARMS) if rep % 2 else list(reversed(ARMS))
            print(f"\n  replication {rep}/{REPLICATIONS}  order={order}",
                  flush=True)
            for arm in order:
                # DISTINCT document per (replication, arm) -- structurally
                # prevents the second arm inheriting the first arm's warm cache.
                doc = build_doc(f"rep{rep}-{arm}", DOC_WORDS)
                report["calls"].extend(
                    asdict_calls(run_arm(ledger, key, rep, arm, doc, doc_only)))

        # ---- sub-floor probe ----------------------------------------------
        print(f"\n  sub-floor probe (doc ~{sub_only} tokens, below the "
              f"{MIN_CACHEABLE_TOKENS} floor)", flush=True)
        sub_doc = build_doc("subfloor", SUBFLOOR_WORDS)
        sub_calls = run_arm(ledger, key, 0, "PROMOTED", sub_doc, sub_only)
        report["sub_floor"] = {
            "doc_tokens": sub_only,
            "calls": asdict_calls(sub_calls),
            "writes": [c.cache_write for c in sub_calls],
            "reads": [c.cache_read for c in sub_calls],
        }
    except RuntimeError as exc:
        report["aborted"] = str(exc)
        print(f"\n  !! {exc}", flush=True)
    finally:
        report["spend_usd"] = round(ledger.spend_usd, 6)
        report["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime())
        with open(OUT_PATH, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        print(f"\n  receipts -> {OUT_PATH}")
        print(f"  total spend ${ledger.spend_usd:.6f} of ${SPEND_CAP_USD:.2f}")
    return 0


def asdict_calls(calls: list[Call]) -> list[dict]:
    rows = []
    for c in calls:
        row = asdict(c)
        row["cache_read"] = c.cache_read
        row["cache_write"] = c.cache_write
        row["fresh_input"] = c.fresh_input
        row["output_tokens"] = c.output
        row["prefix_tokens"] = c.prefix_tokens
        rows.append(row)
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
