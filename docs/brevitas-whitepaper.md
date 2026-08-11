# The Cache Dividend

### How Brevitas recovers the money hiding inside provider-native prompt caching

**Brevitas Systems — White Paper, August 2026**
brevitassystems.com

---

## Executive summary

Every major LLM provider now operates a native caching layer, and every one of them puts the discount on the price sheet: a cached input token is priced somewhere between roughly **half and one-fiftieth** of a fresh one, depending on provider and model. Anthropic prices a Haiku cache read at one-tenth of fresh input. DeepSeek prices a cache hit at one-fiftieth of a miss. You receive that discount if, and only if, your traffic lines up with the cache's rules. Here is the problem: **the rules are not published.** The price is documented; everything that determines whether you actually earn it — what gets cached, how long it lives, how a partial match is quantized, why one request hits and the next misses — is documented in prose at best and not at all at worst. From the outside, the cache is an opaque box that occasionally hands back a discount for reasons no one can see.

Brevitas exists because we refused to accept the box — and because you can characterize a box like that from the outside, without ever prying it open. Over two probe days, **2026-08-09 and 2026-08-10**, we ran a small, deliberate set of controlled request sequences: **41 logged calls** against Anthropic's Messages API and roughly two dozen against DeepSeek's, on our own funded probe keys, using synthetic prompts we generated ourselves, reading nothing but the usage receipt the provider returns on every response. Total live spend: **$0.2336** on Anthropic and **$0.002037** on DeepSeek. About a quarter of a dollar bought a map that neither provider publishes as numbers: where the expiry cliff actually falls, what the minimum cacheable prefix actually is, how a hit is actually quantized, and whether reading an entry resets its clock. Then we built that map into a drop-in gateway that shapes *how* your requests engage the cache, so the discount the provider already advertises actually lands on your invoice.

Three things make this different from every other "reduce your AI bill" pitch:

1. **The default path never touches your content.** On the lossless path — the default — requests pass through byte-for-byte. Same prompts, same models, same outputs. The provider sees exactly the request you sent; it just charges you less for it. Anything that would alter a request is a separate, opt-in feature, named as such, and never on by default.
2. **Every dollar is proven, not estimated.** Each request produces a receipt built from the provider's own usage accounting: what you were charged, what you would have been charged, and the difference.
3. **We only get paid when you save.** Our fee is a percentage of verified savings. If the cache doesn't pay out, neither do we.

---

## 1. The problem: you are paying full price for repetition

Modern AI workloads are overwhelmingly repetitive. An agent re-sends its system prompt, tool definitions, and conversation history on every single turn. A RAG application re-sends the same instructions thousands of times a day. A coding assistant re-transmits nearly identical context on every keystroke-triggered request.

The providers know this. That's why native caching exists: Anthropic's prompt caching prices a Haiku cache read at one-tenth of the normal input rate, and DeepSeek caches automatically and prices a hit at one-fiftieth of a miss. On paper, a heavily agentic workload should be paying a small fraction of list price for the bulk of its input.

In practice, what teams capture varies enormously — and we are not going to quote you a headline percentage, because the honest answer is that **the recoverable share is a property of your traffic and your provider, not of the industry.** Two measurements from our own probes bracket the range. On Anthropic, an untouched cache entry is gone somewhere between 300 and 330 seconds after it was written, so a workload whose requests arrive further apart than that re-pays the full write premium every single time — that gap is real, unclaimed money. On DeepSeek, the opposite: the cache is automatic and was still fully warm at 15 minutes untouched, so a customer with a repeated prefix is *already* getting almost all of the discount. We benchmarked exactly that case and measured Brevitas's incremental benefit over a natively-cached DeepSeek baseline at **−1.27%** across 36 calls. Negative. There was nothing there for us to sell, so we don't sell it.

The only defensible number for your workload is the one your own receipts produce, which is why the gateway measures before it bills, and bills only what the receipts prove.

---

## 2. The black box: a discount with no instructions

Why does the advertised discount fail to materialize? Because almost nothing that governs it is documented, and the little that *is* observable is observable only one receipt at a time — which nobody does.

Consider what a paying customer is actually allowed to know. You can read the price of a cache hit. That is roughly where public knowledge ends. You cannot see:

- **What the cache matched.** Matching is exact-prefix: a single reordered field, a timestamp, or a dynamic value near the top of a request silently breaks the match for everything after it. Which byte broke it? The API will never tell you.
- **How long anything lives.** Lifetimes are given as prose or not at all. Anthropic documents "at least 5 minutes, refreshed on use" and no cliff; DeepSeek documents "usually within a few hours to a few days" and no number. Neither tells you where your traffic's own gaps fall relative to that boundary, and on a short-TTL provider every request that lands on the wrong side of it pays full write-side price for an entry that then dies unused.
- **What the block size is.** Where a cache matches in fixed-size units, a partial prefix is rounded down and the tail bills at full rate — so a 1,015-token prefix may only ever recover 896 of it. On DeepSeek the live block is **128 tokens**, not the 64 that circulates publicly. We only know that because we measured it, and it changes what a given prefix is actually worth.
- **Why you missed.** The usage report says *whether* you hit. Never why you missed, what the miss cost, or what would have made it hit. There is no debugger, no simulator, no support article — no feedback loop at all.

This isn't a gap in the documentation; it is the design. The cache is internal infrastructure that happens to have a price attached, and every provider's own terms reserve the right to change how that infrastructure behaves without notice. Nothing obliges them to tell you when they do.

The result: for the customer, caching behaves like a slot machine. Teams enable it, see erratic results, can't explain them, and move on — leaving the discount on the table indefinitely. Not because they're careless, but because **the information needed to capture it does not exist in anything the provider publishes.** It has to be measured.

---

## 3. What Brevitas does: measuring the box from the outside

If the provider won't tell you how the cache works, there is one honest alternative: characterize it behaviorally. Send ordinary, well-formed traffic on your own key, change one variable at a time, and read what the provider's own usage receipt says came back. No internals are inspected and none need to be — the receipt is a published output of a published API, and it is sufficient.

That is Brevitas's founding work. We built two probe harnesses, one per provider, and ran them on 2026-08-09 and 2026-08-10. Both are ordinary scripts, committed to our repo and re-runnable on demand: `scripts/anthropic_cache_probe.py` (guarded by an explicit env flag and a hard **$5.00** list-price spend cap) and `scripts/deepseek_cache_probe.py` (**$2.00** cap). Hold a prefix constant and sweep the interval between requests, and the expiry window falls out of the receipts. Sweep prefix length through the floor, and the minimum cacheable size and the block quantization fall out too. What we actually spent to learn all of it: **$0.2336** and **$0.002037**.

Some of what those two days produced, each line backed by a provider receipt:

- **Anthropic's default cache dies between 300 s and 330 s** untouched — a 5:00 read hit, a 5:30 read came back as a full cold re-write. The docs say "at least 5 minutes" and stop there.
- **A single read genuinely resets the clock.** A prefix read once at 4:00 was still warm at 8:00; an otherwise identical control prefix, untouched, was cold at 8:00 and re-paid the write premium. The docs assert refresh-on-read; this is what it looks like measured, bridging two full windows.
- **Anthropic's minimum cacheable prefix on Haiku is ~4096 tokens.** This matches Anthropic's own docs — and contradicts the 1024 figure most people are actually using, which is the Sonnet number. A 4094-token prefix caches nothing, silently, with a 200 response.
- **DeepSeek's cache block is 128 tokens**, not the 64 commonly repeated: a 201-token prompt cached exactly 128 and stranded 73. Its floor is one block, and it was still warm at 15 minutes with no keep-alive at all.

**What we did not do, deliberately.** This is a boundary we hold, not a formality:

- No cross-tenant access, and no probing of whether one account's cached prefix is visible to another. We hold one probe key and that test is out of scope by policy, not merely unimplemented.
- No rate-limit probing, no evasion, no malformed or abusive traffic. Every call was well-formed and small.
- No customer data, ever. Every prefix is synthetic, generated from a seeded word list.
- Our own account, our own key, our own money — never a customer's key and never a customer's bill.
- Where a probe idea edged toward stressing a provider's infrastructure or other tenants, it was skipped rather than run.

Some of this exists in neither provider's documentation. The rest exists there only as prose, which a number either confirms or corrects — and knowing *which* is the whole point. The result is a proprietary, checkable, reproducible map of two providers' caches, produced for about a quarter of a dollar and re-derivable for the same. We would rather hand you a map you can audit for pocket change than a claim you have to take on faith.

That map powers the Brevitas gateway. Sitting between your application and the provider, it does three things:

**Cache-aware request shaping.** Brevitas attaches and positions the provider's own cache directives on your traffic so that the stable, repeated portions of your requests reliably qualify for the discount. Your content is untouched; only the cache metadata riding alongside it changes.

**Predictive warming, where the measurement says it pays.** On a provider whose cache expires on a short clock, a keep-alive read issued *inside* the measured TTL holds a hot prefix resident, so a request that would have arrived to a cold cache arrives to a warm one instead. That is the mechanism, and it is the mechanism our measurements support: the TTL cliff is measured, the refresh-on-read behavior is measured, and the keep-alive cadence is set below the measured cliff with a safety margin. What we will not do here is quote you a lift multiple. The number that matters is the *incremental* saving over what your traffic would have earned organically, and separating those two requires a control arm rather than an attribution model — see §4.

The corollary is the part we are proudest of: **we do not warm DeepSeek.** We measured its cache still warm at 15 minutes untouched, then benchmarked our own arm against a natively-cached baseline over 36 calls and came out at **−1.27%** — very slightly *worse* than doing nothing. A keep-alive ping there converts nothing and would bill a customer for a ping. So the policy doesn't schedule one. Warming is provider-specific by measurement, and where the measurement says it doesn't pay, the honest product is to not do it.

**Re-calibration.** Provider cache behavior can change without announcement, so the map has to be treated as perishable. The probe harnesses are committed and re-runnable at the cost of pocket change, and the gateway meters hit rate on live traffic against the receipts — so a provider-side change shows up as a visible shift in the receipts rather than as a bill that quietly reinflates.

---

## 4. Proof, not promises

Cost-saving vendors have an incentive to overstate savings. We removed the incentive structurally.

**Per-request receipts.** Every response that passes through Brevitas is metered against the provider's own usage report. The receipt records the tokens billed at the discounted cache rate, the price you actually paid, and the counterfactual price at full list rate. Savings are the arithmetic difference — derived from provider data, not our estimates.

**A control arm.** Attribution is the hard part of this business, and we would rather solve it than argue about it. When a customer returns inside the TTL and gets a cache read, the receipt looks identical whether our keep-alive kept the entry alive or the customer would have returned anyway — so lift can be asserted from warmed traffic but not measured. The fix is to withhold a random share of the keep-alives that were about to happen and let the difference in cost between arms be the causal effect, whatever the organic rate turns out to be. That holdout is built and instrumented; it ships **off by default** and is enabled per deployment, because it deliberately gives up some savings to buy a real number. Where it is off, we say so, and we bill only the receipt arithmetic above — we do not report a lift figure we did not measure.

**Savings-share billing.** Our fee is a fixed percentage of verified savings. A month where the cache pays out nothing is a month you owe us nothing. We are the only party in this arrangement whose revenue depends on your bill going *down*.

---

## 5. Integration: one line, your keys, your content

Brevitas is a base-URL change:

```python
client = OpenAI(
    base_url="https://api.brevitassystems.com/v1",
    api_key=os.environ["OPENAI_API_KEY"],       # your key, unchanged
    default_headers={
        "X-Brevitas-Key": os.environ["BREVITAS_API_KEY"],
        "X-Brevitas-Customer-ID": "acme",
    },
)
```

The same two headers work with the Anthropic SDK. There is no SDK to adopt, no re-architecture, and no migration risk: remove the base URL and you are back to talking to the provider directly.

Trust properties, by construction:

- **Your API keys stay yours.** Brevitas forwards your provider credentials; it never owns your provider relationship.
- **Content passes through unchanged.** The gateway's default path is content-preserving — what the provider receives is what you sent.
- **A local option exists.** For teams that can't route traffic through a hosted gateway, a zero-code local proxy keeps every byte on your own machine.

---

## 6. What this is worth

Anthropic prices a Haiku cache read at one-tenth of standard input; DeepSeek prices a hit at one-fiftieth of a miss; the major providers discount cached input by roughly half or more depending on model. Those are price-sheet facts, not projections. For workloads dominated by repeated context — agents, copilots, RAG, high-volume extraction — the addressable discount is a recurring line item rather than a one-time optimization.

What we will not do is turn that into an industry-scale number. Aggregate LLM API spend is measured in the tens of billions of dollars annually and growing, and it is easy to multiply that by an assumed recovery rate and print something impressive. We don't have a measured recovery rate across the industry — we have two providers mapped, one benchmark that came back negative, and a settlement path that computes the answer per customer from their own receipts. The recoverable share plausibly ranges from *nothing* (DeepSeek, already automatic and long-lived) to a material fraction of input spend (a short-TTL provider with sporadic, prefix-heavy traffic), and which one you are is an empirical question about your traffic.

That is the actual business: answering that question per customer, with receipts, and taking a share only of what we prove we recovered. A customer for whom the answer is "nothing" pays us nothing — and we would rather tell them that than sell them a number.

---

## 7. Conclusion

The cheapest tokens you'll ever buy are the ones your provider has already agreed to discount. That discount is real and frequently forfeited — not because teams don't want it, but because the machinery that grants it is undocumented. The providers publish the price of a cache hit and little else; the rules that decide whether you get one are visible only in the receipts, one call at a time.

We took the path that was actually available, and it turned out to be the better one: we measured it. Two probe days, a few dozen well-formed calls on our own key, synthetic prompts, and about a quarter of a dollar bought a map of two providers' caches that exists nowhere in their docs — a map any skeptic can re-derive by running the same committed scripts. Brevitas turns that map into a managed, measured source of savings: same prompts, same models, same outputs, smaller invoice, with the evidence attached. And because provider behavior can change without notice, the harnesses stay runnable and we re-run them, so the map doesn't quietly go stale.

**Start with one line of code at [brevitassystems.com](https://brevitassystems.com).** If we don't save you money, you don't pay us.
