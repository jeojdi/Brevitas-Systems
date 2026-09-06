"""Is COMPETENT actually a careful engineer, or a flattering baseline?

Same pricing model as scripts/match_engineer_bench.py, with three additions:

  1. A FAITHFUL cache model. The original Cache stores ONE entry per call (the
     deepest marker). Anthropic stores an entry at EVERY cache_control breakpoint
     ("earlier breakpoints remain valid read points"), which is the entire reason
     the provider gives you four of them. Both models are run side by side so the
     modelling error is visible rather than assumed away.

  2. A 1h-TTL tier, so arrival gaps past the 5-minute cliff can be priced. 1h
     writes cost 2.0x, reads 0.10x, and a read refreshes the hour
     (docs/ANTHROPIC_CACHE_MAP.md P5).

  3. EXPERT arms:
       EXPERT        byte-identical to the customer's request; textbook-optimal
                     breakpoint placement per Anthropic's own documented patterns.
       EXPERT+1h     EXPERT plus tier selection by observed inter-arrival gap.
       EXPERT+LAYOUT EXPERT plus a request-layout change (shared content first).
                     NOT byte-identical -- the model sees a different prompt.

Offline. No provider calls.
"""
import copy, hashlib, os, sys, time
sys.path.insert(0, os.path.abspath("."))

from token_efficiency_model.lossless.engine import optimize_request
from token_efficiency_model.lossless.router import BrevitasRouter
from token_efficiency_model.lossless.provider_cache import (
    count_tokens, anthropic_min_tokens)

MODEL = "claude-haiku-4-5"
FLOOR = anthropic_min_tokens(MODEL)
READ = 0.10
WRITE_5M, WRITE_1H = 1.25, 2.00
TTL_5M, TTL_1H = 300.0, 3600.0


def _blocks(body):
    """[(text, has_cache_control, ttl)] in wire order: tools -> system -> messages."""
    out = []
    for t in body.get("tools", []) or []:
        cc = t.get("cache_control") if isinstance(t, dict) else None
        out.append((str(t), cc is not None, (cc or {}).get("ttl", "")))
    sysv = body.get("system")
    if isinstance(sysv, str) and sysv:
        out.append((sysv, False, ""))
    elif isinstance(sysv, list):
        for b in sysv:
            cc = b.get("cache_control")
            out.append((b.get("text", ""), cc is not None, (cc or {}).get("ttl", "")))
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            out.append((c, False, ""))
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    cc = b.get("cache_control")
                    out.append((b.get("text", ""), cc is not None, (cc or {}).get("ttl", "")))
    return out


class Cache:
    """faithful=False reproduces match_engineer_bench.py exactly (one entry per call,
    at the deepest marker). faithful=True writes an entry at EVERY breakpoint, which
    is what the provider documents."""

    def __init__(self, faithful=False):
        self.entries = {}
        self.reads = self.writes = 0
        self.faithful = faithful

    def price(self, body, now):
        blocks = _blocks(body)
        toks = [count_tokens(t) for t, _, _ in blocks]
        marked = [i for i, (_, m, _) in enumerate(blocks) if m]
        if not marked:
            return sum(toks) * 1.0
        deepest = max(marked)
        cum, h, bound = 0, hashlib.sha256(), {}
        for i, (t, _, _) in enumerate(blocks):
            h.update(t.encode())
            cum += toks[i]
            bound[i] = (h.hexdigest(), cum)
        pref_tokens = bound[deepest][1]
        tail = sum(toks) - pref_tokens
        if pref_tokens < FLOOR:                 # provider silently ignores the marker
            return sum(toks) * 1.0
        hit_tokens = 0
        for i in range(deepest, -1, -1):
            key, upto = bound[i]
            exp = self.entries.get(key)
            if exp is not None and exp > now:
                hit_tokens = upto
                break
        ttl = blocks[deepest][2]
        life = TTL_1H if ttl == "1h" else TTL_5M
        write_rate = WRITE_1H if ttl == "1h" else WRITE_5M
        # refresh every entry at or before the hit boundary, and store the new ones
        store = marked if self.faithful else [deepest]
        for i in store:
            if bound[i][1] >= FLOOR:
                t_i = blocks[i][2]
                self.entries[bound[i][0]] = now + (TTL_1H if t_i == "1h" else TTL_5M)
        for i in range(deepest, -1, -1):
            if bound[i][1] <= hit_tokens and bound[i][0] in self.entries:
                self.entries[bound[i][0]] = max(self.entries[bound[i][0]], now + life)
        written = pref_tokens - hit_tokens
        if hit_tokens:
            self.reads += 1
        if written:
            self.writes += 1
        return hit_tokens * READ + written * write_rate + tail * 1.0


# --------------------------------------------------------------------------- arms
def _wrap(holder, key, ttl=""):
    cc = {"type": "ephemeral", "ttl": "1h"} if ttl == "1h" else {"type": "ephemeral"}
    v = holder.get(key)
    if isinstance(v, str) and v:
        holder[key] = [{"type": "text", "text": v, "cache_control": cc}]
        return True
    if isinstance(v, list) and v and isinstance(v[-1], dict) and "cache_control" not in v[-1]:
        v[-1]["cache_control"] = cc
        return True
    return False


def competent(body):
    """Unchanged from match_engineer_bench.py -- the incumbent baseline."""
    body = copy.deepcopy(body)
    if isinstance(body.get("system"), str) and body["system"]:
        _wrap(body, "system")
    msgs = body.get("messages", [])
    if msgs and msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str):
        _wrap(msgs[0], "content")
    if len(msgs) >= 2:
        _wrap(msgs[-2], "content")
    return body


def expert(body, ttl=""):
    """A careful engineer who has actually read the caching page.

    Three things COMPETENT does not do, all straight out of Anthropic's docs:

    a) Marks the last content block of the MOST RECENTLY APPENDED turn, not the one
       before it. The docs' multi-turn pattern is msgs[-1]: this turn's volatile tail
       is next turn's stable prefix, so paying 1.25x on a ~10-token delta now buys a
       0.10x read on it every turn after. COMPETENT's msgs[-2] leaves that on the table
       and re-pays full freight for the newest turn forever.
       Only applied when there IS history -- on a stateless single-turn request the
       final block is unique per user and marking it is a pure wasted write.

    b) Spends all four breakpoints as a CASCADE (globally-shared prefix, then
       session-shared, then the newest turn) instead of one. Each is an independent
       read point, so a session whose tail diverges still reads the shared head.

    c) Checks the per-model floor (4096 for claude-haiku-4-5) before marking. A marker
       under the floor is silently inert -- HTTP 200, cache_creation 0.
    """
    body = copy.deepcopy(body)
    if isinstance(body.get("system"), str) and body["system"]:
        _wrap(body, "system", ttl)
    msgs = body.get("messages", [])
    if msgs and msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str):
        _wrap(msgs[0], "content", ttl)

    # cumulative tokens per message so we never mark below the floor
    cum, cums = 0, []
    if isinstance(body.get("system"), list):
        cum += sum(count_tokens(b.get("text", "")) for b in body["system"])
    for m in msgs:
        c = m.get("content")
        cum += count_tokens(c) if isinstance(c, str) else \
            sum(count_tokens(b.get("text", "")) for b in c if isinstance(b, dict))
        cums.append(cum)

    already = sum(1 for h in ([body.get("system")] + [m.get("content") for m in msgs])
                  if isinstance(h, list) and any(isinstance(b, dict) and "cache_control" in b
                                                 for b in h))
    budget = 4 - already
    if budget <= 0 or not msgs:
        return body
    # (a): newest turn, but only for a continuing conversation
    targets = []
    if len(msgs) >= 2 and cums[-1] >= FLOOR:
        targets.append(len(msgs) - 1)
    # (b): cascade back through stable history, widest spacing first
    stable = [i for i in range(len(msgs) - 1) if cums[i] >= FLOOR and i not in targets]
    while stable and len(targets) < budget:
        targets.append(stable.pop())
    for i in sorted(set(targets))[:budget]:
        _wrap(msgs[i], "content", ttl)
    return body


def expert_layout(body, ttl=""):
    """EXPERT plus the layout fix: hoist content that is identical across agents ahead
    of the per-agent instruction, so the shared prefix is shared from token 0.

    NOT byte-identical. Anthropic matches longest-common-prefix from token 0 and the
    wire order is tools -> system -> messages, so a per-agent system block at position
    0 caps the shareable prefix at ~0 no matter where the breakpoints go. The only fix
    is to move the shared block in front -- which changes the prompt the model reads.
    A pipeline author does this at design time; a byte-lossless proxy cannot.
    """
    body = copy.deepcopy(body)
    msgs = body.get("messages", [])
    if len(msgs) >= 3 and msgs[0].get("role") == "system":
        head = msgs.pop(0)                      # per-agent role
        big = max(range(len(msgs)), key=lambda i: count_tokens(str(msgs[i].get("content"))))
        msgs.insert(big + 1, head)              # shared doc now leads
        body["messages"] = msgs
    return expert(body, ttl)


# --------------------------------------------------------------------------- run
ARMS = ["NAIVE", "COMPETENT", "BREVITAS", "EXPERT", "EXPERT+LAYOUT"]


def run(name, requests, note="", spacing=20.0, faithful=False, arms=ARMS):
    out = {}
    for arm in arms:
        cache, router, now, total = Cache(faithful), BrevitasRouter(), time.time(), 0.0
        for sid, make in requests:
            body = make()
            ttl = "1h" if spacing > 300.0 else ""
            if arm == "COMPETENT":
                body = competent(body)
            elif arm == "EXPERT":
                body = expert(body)
            elif arm == "EXPERT+1h":
                body = expert(body, ttl)
            elif arm == "EXPERT+LAYOUT":
                body = expert_layout(body, ttl if arm.endswith("1h") else "")
            elif arm == "BREVITAS":
                optimize_request(body, "anthropic", router, session_id=sid, tenant_key="acme")
            total += cache.price(body, now)
            now += spacing
        out[arm] = (total, cache.reads, cache.writes)
    base, comp = out["NAIVE"][0], out["COMPETENT"][0]
    print(f"\n{name}   [{'faithful' if faithful else 'shipped-bench'} cache model]")
    if note:
        print(f"  {note}")
    print(f"  {'arm':<15}{'cost':>14}{'vs NAIVE':>11}{'vs COMPETENT':>14}{'rd/wr':>9}")
    for arm, (tot, rd, wr) in out.items():
        v1 = f"{(tot-base)/base*100:+.1f}%" if base else "-"
        v2 = f"{(tot-comp)/comp*100:+.1f}%" if comp else "-"
        print(f"  {arm:<15}{tot:>14,.0f}{v1:>11}{v2:>14}{rd:>4}/{wr:<4}")
    return out


SYS = "You are Acme Support.\n" + ("Policy: always cite the handbook section.\n" * 1200)
DOC = "SHARED SPEC\n" + ("The service must validate every inbound field.\n" * 1200)
AGENTS = ["planner", "coder", "reviewer", "tester", "documenter"]


def w1():
    hist, reqs = [], []
    for t in range(10):
        hist.append({"role": "user", "content": f"Turn {t}: please continue."})
        snap = copy.deepcopy(hist)
        reqs.append(("sess", lambda s=snap: {"model": MODEL, "system": SYS,
                                             "messages": copy.deepcopy(s)}))
        hist.append({"role": "assistant", "content": f"Done {t}."})
    return reqs


def w2():
    return [(f"user-{i}", lambda i=i: {"model": MODEL, "system": SYS,
                                       "messages": [{"role": "user",
                                                     "content": f"User {i}: where is my order?"}]})
            for i in range(20)]


def w3():
    return [(ag, lambda ag=ag: {"model": MODEL,
                                "messages": [{"role": "system",
                                              "content": f"You are the {ag}. Follow {ag} conventions."},
                                             {"role": "user", "content": DOC},
                                             {"role": "user", "content": f"Do the {ag} job."}]})
            for ag in AGENTS for _ in range(2)]


print(f"model={MODEL}  floor={FLOOR} tok   read={READ}x  write_5m={WRITE_5M}x  write_1h={WRITE_1H}x")
for faithful in (False, True):
    run("1. ONE LONG SESSION - 10 turns", w1(), "COMPETENT marks msgs[-2]; EXPERT marks msgs[-1]",
        faithful=faithful)
    run("2. TWENTY SHORT SESSIONS - shared system", w2(),
        "single-turn: EXPERT must NOT mark the unique tail", faithful=faithful)
    run("3. FIVE AGENTS x2 - shared document", w3(),
        "per-agent system at position 0 caps the shareable prefix at ~0", faithful=faithful)

print("\n" + "=" * 78)
print("4. SPARSE ARRIVALS - 12 calls, 8 minutes apart (past the 5m cliff)")
print("   the one place a tier choice, not a placement choice, decides the bill")
reqs4 = [(f"cron-{i}", lambda i=i: {"model": MODEL, "system": SYS,
                                    "messages": [{"role": "user", "content": f"Batch {i}: summarise."}]})
         for i in range(12)]
run("4. SPARSE ARRIVALS - 480s apart", reqs4, spacing=480.0, faithful=True,
    arms=["NAIVE", "COMPETENT", "BREVITAS", "EXPERT", "EXPERT+1h"])

print("\n" + "=" * 78)
print("4b. SAME sparse workload, ONE session id -- gives BREVITAS's gap-EWMA a chance")
reqs4b = [("cron", lambda i=i: {"model": MODEL, "system": SYS,
                                "messages": [{"role": "user", "content": f"Batch {i}: summarise."}]})
          for i in range(12)]
run("4b. SPARSE, single session", reqs4b, spacing=480.0, faithful=True,
    arms=["NAIVE", "COMPETENT", "BREVITAS", "EXPERT", "EXPERT+1h"])
