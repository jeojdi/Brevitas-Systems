# Brevitas Pivot Strategy

_Strategy memo — informed by a fact-checked deep-research pass (Aug 2026). Every non-obvious
claim below was adversarially verified against primary sources; citations inline._

## Bottom line up front

**Reject "Redis for AI" as the literal framing — but the pivot instinct is right.** The
research found a clean, defensible wedge that is *already ours* and that no competitor pairs
together. Lead with that, and treat routing/caching as features underneath it, not the headline.

> **Recommended positioning: the _verified_ cost & savings control plane for AI.**
> Not "we cache your AI" (crowded, cloneable) — **"we're the only gateway that can _prove_ what
> it saved you, and only bills when it did."**

---

## 1. Why "Redis for AI" fails the pressure-test

**The technical crux kills the literal version.** "Redis for AI" implies owning a KV/state
store. But **cross-request KV-cache reuse is not reachable from an API proxy** — it requires
access to the inference engine's GPU paged memory (vLLM/SGLang). LMCache, Mooncake, and
CacheBlend all *attach to a self-hosted engine*; hosted provider APIs (Anthropic, OpenAI,
Bedrock) never expose KV tensors. A proxy therefore *structurally cannot* own the "Redis for AI"
primitive. Verified 3-0.
- https://arxiv.org/pdf/2510.09665
- https://docs.lmcache.ai/

**The name is also occupied — by Redis.** Redis shipped **LangCache** (semantic caching,
April 2025) *and* an **Agent Memory Server**. Competing for the "Redis for AI" slot means
fighting the incumbent on its own turf — and datastore moats there are provably fragile
(Valkey / DragonflyDB cloned Redis within a release cycle).
- https://redis.io/blog/spring-release-2025/

**The funded framing is "AI gateway / control plane," not "Redis for AI."** Cloudflare AI
Gateway does 350+ model dynamic routing at the edge; Portkey raised a **$15M Series A**
positioning as "the unified control plane." That lane is where the money and the language
already are.
- https://blog.cloudflare.com/ai-gateway-aug-2025-refresh/
- https://portkey.ai/blog/series-a-funding/

---

## 2. The wedge that's actually hard to copy

Everyone can cache. Everyone can route (RouteLLM / FrugalGPT are open and commoditizing).
**Almost nobody can _prove savings_ rigorously, and nobody ties billing to a quality gate.**
Our existing stack already does both:

- **Mechanism-separated, control-arm-verified savings** (input tokens avoided vs. native-cache
  discount vs. calls avoided vs. measured lift from an isolated control arm).
- **mSPRT quality gate that auto-stops billing** when a lever's quality drops.

That combination is the moat. It compounds three ways a caching proxy can't:
1. **Trust** — we're the neutral scorekeeper of AI spend.
2. **Data network effect** — routing/cache policies learned from real cross-tenant traffic get
   better as we grow.
3. **Switching cost** — once we're the system of record for AI cost/savings, ripping us out
   means losing the audit trail.

This is the Datadog / Cloudflare playbook (become the measurement layer), not the Redis playbook
(own a datastore).

**Near-term proof point:** VS Code silently drops Anthropic prompt-cache breakpoints through
OpenAI-compatible proxies (`cached_tokens=0`). Provider prompt-caching *is* proxy-ownable, tools
are getting it wrong, and we can *measure* the difference. "We recovered the cache savings your
tools are silently losing — here's the receipt" is a demo we can ship now.
- https://github.com/microsoft/vscode/issues/312940

---

## 3. Techniques: defensibility × time-to-ship

```
                 HARD TO SHIP
                      │
   Cross-model        │   Traffic-learned routing
   cascades (quality  │   policies + verified-savings
   attribution hard)  │   feedback loop   ★ BUILD THE MOAT
                      │
 LOW ─────────────────┼───────────────────── HIGH DEFENSIBILITY
                      │
   Semantic cache     │   Prompt-cache "recovery" +
   (Redis owns it,    │   cross-provider cache mgmt
   false-hit risk)    │   Per-agent savings ledger  ★ SHIP FIRST
                      │
                 EASY TO SHIP
```

- **★ Ship first (high-defensibility, easy):** cross-provider prompt-cache management + a
  **per-agent / per-pipeline savings ledger**. Closes the `usage_log` gap, feeds the moat,
  demoable in days.
- **★ Build the moat (high-defensibility, harder):** cross-model **routing whose policy is
  trained on our traffic and validated by our control-arm** — so the router itself carries a
  verified-savings guarantee. RouteLLM shows >2× cost cuts; FrugalGPT reaches up to ~98% of
  GPT-4 at a fraction of cost — *but those are commodity; our verification wrapper is not.*
  - https://arxiv.org/abs/2406.18665
  - https://arxiv.org/abs/2305.05176
- **Commodity (do, don't lead with):** semantic cache (Redis owns the lane; false-hit risk),
  prompt compression — LLMLingua-2 is solid (~2–5× at low loss) but 500x-style ratios degrade.
  - https://arxiv.org/abs/2403.12968
- **Don't build:** literal KV-cache reuse/offload — needs self-hosted inference; out of reach at
  the proxy layer.

**Sequenced roadmap:**
1. Per-agent verified-savings ledger + prompt-cache recovery.
2. Cross-model routing *gated by* our verified-savings + mSPRT engine.
3. "Savings SLA" / audit-report product for finance buyers.

---

## 4. Two narratives

**Investor one-liner:** _"Every company's AI bill is exploding and unaccountable. Brevitas is the
control plane that routes, caches, and — uniquely — verifies what it saved, billing only on
proven savings. We become the neutral system of record for AI spend: a trust + data moat in a
lane (AI gateway) that's raising Series As, differentiated by the one thing gateways can't do —
prove ROI."_

**User / technical one-liner:** _"Point your base URL at Brevitas. It manages provider caches,
routes to the cheapest model that passes a live quality gate, and shows a per-agent receipt of
exactly what it saved — verified against a control arm, not estimated. You only pay a cut of
savings we can prove."_

---

## 5. Risks & kill-criteria

1. **Percentage-of-savings pricing is fragile** — outcome-based pricing is contested in
   enterprise AI, and our own design already settles cache-replay savings at $0. It is *also*
   our best story (aligned incentives).
   - **Kill-criterion:** if verified savings can't clear a floor that covers CAC, add a flat
     platform fee and make "verified savings" the _feature_, not the whole revenue model.
   - https://www.forbes.com/sites/parloa/2026/01/06/outcome-based-pricing-the-most-expensive-myth-in-enterprise-ai/
   - https://www.bvp.com/atlas/the-ai-pricing-and-monetization-playbook
2. **Crowded gateway lane** (Portkey, Cloudflare, Helicone, LiteLLM).
   - **Kill-criterion:** if "verified savings" doesn't win head-to-head demos vs. a gateway that
     just shows a dashboard, the measurement moat isn't real to buyers.
3. **Routing commoditizes.** Don't sell "we route." Sell "we route *and prove it worked.*" If the
   verification layer is ever cloned, re-anchor on the billing-trust + data flywheel.

_Honesty note: a "cluster-route-escalate cascade retains 97–99% of frontier accuracy" claim was
**refuted** (1-2 in verification) — do not cite it._

---

## Method & confidence

Deep-research pass: 5 search angles, 27 sources fetched, 126 claims extracted, top 25 verified by
3-vote adversarial check (24 confirmed, 1 killed). The load-bearing claim — that cross-request KV
reuse is unreachable from a proxy — verified 3-0 against primary sources. Routing headline numbers
are best-case per-task ceilings on older models; treat as directional, not guaranteed. Anchored to
August 2026.
