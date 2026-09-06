"""Does the SHIPPED engine save money, as of right now?

Offline. No provider calls, no spend. Every arm is priced by the SAME model of the
Anthropic cache, so the comparison is apples-to-apples:

    marked prefix already cached & unexpired -> read  at 0.10x, TTL refreshed
    marked prefix not cached                 -> write at 1.25x, stored with TTL
    everything not under a marker            -> 1.00x
    no markers at all                        -> every token 1.00x  (Anthropic does
                                                not cache automatically)

Costs are in units of "one uncached input token", so percentages are invariant to the
model's actual $/MTok. The cache is keyed by prefix CONTENT and shared across sessions,
which is how the provider actually behaves.

Three arms:
  NAIVE      customer sends nothing special                       (does-nothing baseline)
  COMPETENT  customer marks the last stable block themselves      (Anthropic's own
             documented pattern -- this is the honest baseline for a customer who
             already knows what they are doing; it is what E1 measured against)
  BREVITAS   the shipped optimize_request()
"""
import copy, hashlib, os, sys, time
sys.path.insert(0, os.path.abspath("."))

from token_efficiency_model.lossless.engine import optimize_request
from token_efficiency_model.lossless.router import BrevitasRouter
from token_efficiency_model.lossless.provider_cache import (
    count_tokens, anthropic_min_tokens)

MODEL = "claude-haiku-4-5"          # the model every live probe in docs/ used
FLOOR = anthropic_min_tokens(MODEL)
READ, WRITE, TTL_5M = 0.10, 1.25, 300.0


def _blocks(body):
    """Flatten a request to [(text, has_cache_control)] in wire order.
    Anthropic's wire order is system-then-messages, which is why a per-agent system
    block in front of shared content caps the shareable prefix at ~0."""
    out = []
    sysv = body.get("system")
    if isinstance(sysv, str) and sysv:
        out.append((sysv, False))
    elif isinstance(sysv, list):
        for b in sysv:
            out.append((b.get("text", ""), "cache_control" in b))
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            out.append((c, False))
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    out.append((b.get("text", ""), "cache_control" in b))
    return out


class Cache:
    """Content-keyed, shared across sessions, TTL'd — and INCREMENTAL, which is the
    part that matters: Anthropic matches the LONGEST cached prefix, so an append-only
    conversation reads everything it cached last turn and only writes the delta. A
    model that keys on the exact full prefix instead makes incremental caching look
    impossible and reports a false negative on the flagship workload."""
    def __init__(self):
        self.entries = {}
        self.reads = self.writes = 0

    def price(self, body, now):
        blocks = _blocks(body)
        toks = [count_tokens(t) for t, _ in blocks]
        marked = [i for i, (_, m) in enumerate(blocks) if m]
        if not marked:
            return sum(toks) * 1.0
        deepest = max(marked)
        cum, h, bound_hash = 0, hashlib.sha256(), {}
        for i, (t, _) in enumerate(blocks):
            h.update(t.encode())
            cum += toks[i]
            bound_hash[i] = (h.hexdigest(), cum)
        pref_tokens = bound_hash[deepest][1]
        tail = sum(toks) - pref_tokens
        if pref_tokens < FLOOR:          # provider silently ignores the marker
            return sum(toks) * 1.0
        # longest already-cached boundary at or before the deepest marker
        hit_tokens = 0
        for i in range(deepest, -1, -1):
            key, upto = bound_hash[i]
            exp = self.entries.get(key)
            if exp is not None and exp > now:
                hit_tokens = upto
                self.entries[key] = now + TTL_5M     # refresh-on-use
                break
        self.entries[bound_hash[deepest][0]] = now + TTL_5M
        written = pref_tokens - hit_tokens
        if hit_tokens:
            self.reads += 1
        if written:
            self.writes += 1
        return hit_tokens * READ + written * WRITE + tail * 1.0


def competent(body):
    """What an engineer who has read the Anthropic caching page would actually write:
    mark the shared system block, and mark the last stable message before the volatile
    tail. Marking ONLY the trailing message would be an unfairly weak baseline — with a
    single-turn request there is no such message at all, and the arm would score zero
    for a reason that has nothing to do with the customer's competence."""
    body = copy.deepcopy(body)
    sysv = body.get("system")
    if isinstance(sysv, str) and sysv:
        body["system"] = [{"type": "text", "text": sysv,
                           "cache_control": {"type": "ephemeral"}}]
    msgs = body.get("messages", [])
    # a leading system-role MESSAGE is the same idea expressed in the messages array
    if msgs and msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str):
        msgs[0]["content"] = [{"type": "text", "text": msgs[0]["content"],
                               "cache_control": {"type": "ephemeral"}}]
    if len(msgs) >= 2:
        target = msgs[-2]
        c = target.get("content")
        if isinstance(c, str):
            target["content"] = [{"type": "text", "text": c,
                                  "cache_control": {"type": "ephemeral"}}]
    return body


def run(name, requests, note=""):
    """requests: list of (session_id, body-factory)."""
    arms = {}
    for arm in ("NAIVE", "COMPETENT", "BREVITAS(pre-fix)", "BREVITAS"):
        cache, router, now, total = Cache(), BrevitasRouter(), time.time(), 0.0
        if arm == "BREVITAS(pre-fix)":
            # today's ROI-gate fix, disabled: warm-prefix evidence never qualifies,
            # which is exactly the behaviour at commit 04b6fe3
            router._warm_prefix_window = lambda ttl: -1.0
        for sid, make in requests:
            body = make()
            if arm == "COMPETENT":
                body = competent(body)
            elif arm.startswith("BREVITAS"):
                optimize_request(body, "anthropic", router, session_id=sid,
                                 tenant_key="acme")
            total += cache.price(body, now)
            now += 20.0                  # 20s between calls: inside the 5-min TTL
        arms[arm] = (total, cache.reads, cache.writes)
    base = arms["NAIVE"][0]
    comp = arms["COMPETENT"][0]
    print(f"\n{name}")
    if note:
        print(f"  {note}")
    print(f"  {'arm':<18} {'cost (tok-units)':>18} {'vs NAIVE':>10} {'vs COMPETENT':>14} {'rd/wr':>9}")
    for arm, (tot, rd, wr) in arms.items():
        v1 = f"{(tot - base) / base * 100:+.1f}%" if base else "-"
        v2 = f"{(tot - comp) / comp * 100:+.1f}%" if comp else "-"
        print(f"  {arm:<18} {tot:>18,.0f} {v1:>10} {v2:>14} {rd:>4}/{wr:<4}")
    return arms


SYS = "You are Acme Support.\n" + ("Policy: always cite the handbook section.\n" * 1200)
DOC = "SHARED SPEC\n" + ("The service must validate every inbound field.\n" * 1200)

print(f"model={MODEL}  provider min cacheable prefix={FLOOR} tokens")

# 1. one long agent session
hist = []
reqs = []
for t in range(10):
    hist.append({"role": "user", "content": f"Turn {t}: please continue."})
    snap = copy.deepcopy(hist)
    reqs.append(("sess", lambda s=snap: {"model": MODEL, "system": SYS,
                                         "messages": copy.deepcopy(s)}))
    hist.append({"role": "assistant", "content": f"Done {t}."})
run("1. ONE LONG SESSION — 10 turns, growing history", reqs,
    "the case provider docs are written for")

# 2. many short sessions behind one shared system prompt
reqs = [(f"user-{i}", lambda i=i: {"model": MODEL, "system": SYS,
                                   "messages": [{"role": "user",
                                                 "content": f"User {i}: where is my order?"}]})
        for i in range(20)]
run("2. TWENTY SHORT SESSIONS — one shared system prompt", reqs,
    "a support bot / API product: this is where the ROI-gate bug lived")

# 3. multi-agent pipeline over one shared document
AGENTS = ["planner", "coder", "reviewer", "tester", "documenter"]
reqs = [(f"{ag}", lambda ag=ag: {"model": MODEL,
                                 "messages": [{"role": "system",
                                               "content": f"You are the {ag}. Follow {ag} conventions."},
                                              {"role": "user", "content": DOC},
                                              {"role": "user", "content": f"Do the {ag} job."}]})
        for ag in AGENTS for _ in range(2)]
run("3. FIVE AGENTS x2 PASSES — one shared document", reqs,
    "the AI-Native Redis case: per-agent system prompt sits at position 0")
