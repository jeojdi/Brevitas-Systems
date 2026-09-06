"""Premise attack. Offline only. No provider calls.

Q1. Is the gap to COMPETENT really the ROI GATE (marking at all), or is it
    marker PLACEMENT? Force the gate open and see whether BREVITAS lands
    exactly on COMPETENT. If it does, placement is worth 0 and the plan is
    not buying placement.
Q2. What does BREVITAS do to a customer who ALREADY marks (the COMPETENT
    body arriving at our door)? That is the production shape the plan says
    it is "converging on".
Q3. What is the BILLABLE delta, not the cost delta?
"""
import copy, os, sys
sys.path.insert(0, os.path.abspath("."))

import scripts.match_engineer_bench as B
from token_efficiency_model.lossless.engine import optimize_request
from token_efficiency_model.lossless.router import BrevitasRouter
from token_efficiency_model.lossless.provider_cache import count_cache_control

MODEL, SYS, DOC = B.MODEL, B.SYS, B.DOC


def build():
    w = {}
    hist, reqs = [], []
    for t in range(10):
        hist.append({"role": "user", "content": f"Turn {t}: please continue."})
        snap = copy.deepcopy(hist)
        reqs.append(("sess", lambda s=snap: {"model": MODEL, "system": SYS,
                                             "messages": copy.deepcopy(s)}))
        hist.append({"role": "assistant", "content": f"Done {t}."})
    w["W1"] = reqs
    w["W2"] = [(f"user-{i}", lambda i=i: {"model": MODEL, "system": SYS,
               "messages": [{"role": "user", "content": f"User {i}: where is my order?"}]})
               for i in range(20)]
    AG = ["planner", "coder", "reviewer", "tester", "documenter"]
    w["W3"] = [(ag, lambda ag=ag: {"model": MODEL, "messages": [
        {"role": "system", "content": f"You are the {ag}. Follow {ag} conventions."},
        {"role": "user", "content": DOC},
        {"role": "user", "content": f"Do the {ag} job."}]})
        for ag in AG for _ in range(2)]
    return w


def price_arm(reqs, mode):
    """mode: naive | competent | brevitas | forced | brevitas_on_competent"""
    cache, router, now, total = B.Cache(), BrevitasRouter(), 1_000_000.0, 0.0
    owners = []
    if mode == "forced":
        router.cache_write_allowed = lambda sid, ttl="": (True, "mark_by_default")
    for sid, make in reqs:
        body = make()
        if mode == "competent":
            body = B.competent(body)
        elif mode == "brevitas_on_competent":
            body = B.competent(body)
            m = optimize_request(body, "anthropic", router, session_id=sid,
                                 tenant_key="acme")
            owners.append(m.get("cache_control_owner"))
        elif mode in ("brevitas", "forced"):
            m = optimize_request(body, "anthropic", router, session_id=sid,
                                 tenant_key="acme")
            owners.append(m.get("cache_control_owner"))
        total += cache.price(body, now)
        now += 20.0
    return total, cache.reads, cache.writes, owners


W = build()
MODES = ["naive", "competent", "brevitas", "forced", "brevitas_on_competent"]
print(f"{'':22}" + "".join(f"{m:>26}" for m in MODES))
results = {}
for name, reqs in W.items():
    row = f"{name:22}"
    for m in MODES:
        tot, rd, wr, own = price_arm(reqs, m)
        results[(name, m)] = (tot, rd, wr, own)
        row += f"{tot:>16,.0f} {rd:>3}/{wr:<5}"
    print(row)

print("\n--- Q1: does forcing the gate open reproduce COMPETENT exactly? ---")
for name in W:
    c = results[(name, "competent")]
    f = results[(name, "forced")]
    print(f"  {name}: COMPETENT {c[0]:,.0f} ({c[1]}/{c[2]})   FORCED {f[0]:,.0f} "
          f"({f[1]}/{f[2]})   delta {f[0]-c[0]:+,.0f} = "
          f"{(f[0]-c[0])/c[0]*100:+.2f}%")

print("\n--- Q2: what happens to a customer who already marks? ---")
for name in W:
    c = results[(name, "competent")]
    b = results[(name, "brevitas_on_competent")]
    own = b[3]
    print(f"  {name}: COMPETENT alone {c[0]:,.0f}  ->  through Brevitas {b[0]:,.0f} "
          f"({(b[0]-c[0])/c[0]*100:+.2f}%)  owners={set(own)}")

print("\n--- Q3: billable base (savings vs the body the customer actually sends) ---")
print("    fee = 25% of verified savings; NAIVE customer = the only billable one")
for name in W:
    n = results[(name, "naive")][0]
    b = results[(name, "brevitas")][0]
    f = results[(name, "forced")][0]
    print(f"  {name}: savings today {n-b:>10,.0f}   after plan {n-f:>10,.0f}   "
          f"billable uplift {(n-f)-(n-b):>+10,.0f}  "
          f"({((n-f)-(n-b))/(n-b)*100:+.1f}% more fee)" if n - b else f"  {name}: zero base")
