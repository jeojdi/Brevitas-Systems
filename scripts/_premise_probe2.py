"""Does the plan's safety bound actually bound anything on the plan's OWN
adversarial workloads? Offline only.

W4  : 20 one-shot sessions, each with a UNIQUE ~9.6k prefix.
W3C : 5 agents, ONE pass each, per-agent text in body["system"].

The plan's Step 1 keys the negative-ROI ledger per PREFIX DIGEST and caps at 2
writes-without-reads. If every digest is new, the cap can never bind.
The plan predicts W4 ~246,203 at 0rd/2wr. Check it.
"""
import copy, os, sys
sys.path.insert(0, os.path.abspath("."))

import scripts.match_engineer_bench as B
from token_efficiency_model.lossless.engine import optimize_request
from token_efficiency_model.lossless.router import BrevitasRouter

MODEL = B.MODEL
UNIT = "The service must validate every inbound field.\n"

# W4: 20 unique one-shot prefixes
W4 = [(f"one-{i}", lambda i=i: {"model": MODEL,
       "system": f"TENANT {i} PRIVATE HANDBOOK\n" + (f"[{i}] {UNIT}" * 1200),
       "messages": [{"role": "user", "content": f"Question {i}?"}]})
      for i in range(20)]

# W3C: five agents, ONE pass each, per-agent text in body["system"]
AG = ["planner", "coder", "reviewer", "tester", "documenter"]
W3C = [(ag, lambda ag=ag: {"model": MODEL,
        "system": f"You are the {ag}. Follow {ag} conventions.",
        "messages": [{"role": "user", "content": B.DOC},
                     {"role": "user", "content": f"Do the {ag} job."}]})
       for ag in AG]


def price(reqs, mode):
    cache, router, now, total = B.Cache(), BrevitasRouter(), 1_000_000.0, 0.0
    if mode == "forced":          # simulates mark_by_default with NO cooldown binding
        router.cache_write_allowed = lambda sid, ttl="": (True, "mark_by_default")
    if mode == "forced_cap2":     # digest-scoped ledger, cap 2 writes-without-reads
        seen = {}
        def gate(sid, ttl="", _r=router):
            key = getattr(_r, "_probe_digest", "")
            if seen.get(key, 0) >= 2:
                return False, "speculation_cooldown"
            return True, "mark_by_default"
        router.cache_write_allowed = gate
    for sid, make in reqs:
        body = make()
        if mode == "competent":
            body = B.competent(body)
        elif mode.startswith("forced") or mode == "brevitas":
            if mode == "forced_cap2":
                # digest = the tenant-scoped prefix chain the plan reuses (router.py:309)
                import hashlib
                blocks = B._blocks(body)
                h = hashlib.sha256()
                for t, _ in blocks[:4]:
                    h.update(hashlib.sha256(t.encode()).hexdigest().encode())
                router._probe_digest = h.hexdigest()
            optimize_request(body, "anthropic", router, session_id=sid, tenant_key="acme")
        before_r, before_w = cache.reads, cache.writes
        total += cache.price(body, now)
        if mode == "forced_cap2" and cache.writes > before_w and cache.reads == before_r:
            seen[router._probe_digest] = seen.get(router._probe_digest, 0) + 1
        elif mode == "forced_cap2" and cache.reads > before_r:
            seen[router._probe_digest] = 0
        now += 20.0
    return total, cache.reads, cache.writes


for name, reqs in (("W4  20 unique one-shot prefixes", W4),
                   ("W3C 5 agents x1, per-agent system", W3C)):
    print(f"\n{name}")
    base = None
    for m in ("naive", "competent", "brevitas", "forced", "forced_cap2"):
        t, rd, wr = price(reqs, m)
        if m == "naive":
            base = t
        print(f"  {m:<12} {t:>12,.0f}  vs NAIVE {(t-base)/base*100:>+7.1f}%   {rd:>3}rd/{wr:<3}wr")
