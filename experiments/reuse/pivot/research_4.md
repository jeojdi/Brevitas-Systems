# The pattern

## 1. These are one idea, not eight

Seven of the eight have an identical product section. Strip the framing and each reads: *we record a content-addressed operation graph — every op, its input hashes, upstream edges, output hash, metered tokens — and then* [gate a PR / diff a model swap / bill a customer / sign a receipt / replay a step / skip a rerun]. The bracket is the only variable. Candidate 3 substitutes the store for the graph; candidate 2 substitutes the replay harness. Candidate 5 is the only genuine outlier — it comes from a measurement, not from the artifact.

The diagnostic is the buyer list. Head of AI Engineering, platform lead, FinOps owner, Director of DevProd, GRC director, Head of Model Risk, VP Product, BizOps lead, founding engineer at a vertical startup. Nine buyers, one artifact. **When the buyer is the free variable and the product is the constant, that is asset placement, not company building.**

A second tell: the same number is load-bearing in opposite directions across the set. The 20.0% zero-perturbation self-disagreement is the kill shot in #8 and the moat in #1 — inside the same documents. The +0.0% tracker result is quoted as a fatal concession in #1 and #4 and then silently reused as the differentiator in #6 and #7. A strategy derived from a problem does not need the same fact to mean two things. A strategy reverse-engineered from an inventory does.

## 2. Which are sunk-cost reasoning

All of them except #5, and the proposals mostly say so themselves:

- #6: *"the reason this candidate is attractive is that the team already built the graph, and sunk assets are the worst reason to pick a market."*
- #7: the graph is *"a byproduct nobody costed. There is no customer who asked for it."*
- #1: its own kill shot is *"the same fact that killed the caching pitch, wearing different clothes."*
- #4: concedes the wedge should ship the naive changed-file baseline, not the tracker — the pilot deliverable contains none of the company's technology.

The sharpest evidence is that the transitive tracker measured +0.0%, and was then nominated as the differentiator four more times. In #6 it is not merely inert but degenerate: insurance underwriting and Reg B adverse-action decisions are leaf-independent, so the `upstream` array — the one field that made the artifact interesting — is empty in the exact wedge chosen. The same asset failed the same test five times and was re-entered each time under a new buyer.

And the asset being protected is smaller than the deliberation implies. Direct inspection in the #6 kill: ~45 simulator-generated graph files, `tokens: {}` empty, `wall_ms: 0`, no actor, no timestamp, no signature. There is no production collector. This is a benchmark harness, not an installed base. The switching cost being weighed here is close to zero.

The other systematic error, present in all eight: **"unserved" was treated as a synonym for "valuable."** Every proposal located a real gap. Nobody sells change-impact selection for agent evals; nobody publishes a workload noise floor; nobody single-flights fan-out; nobody costs tool calls; nobody signs an agent lineage record. All true. All worth nothing. In a market this crowded and this well-funded, an unfilled gap is much stronger evidence that the gap is not worth filling than that four funded incumbents missed it. Eight for eight.

## 3. Would a founder without this codebase pick any of these?

**No. Not one.** Say it plainly and stop ranking.

The structural reason is that all eight sit at a layer of the stack that three separate parties are each paid to give away:

- The **framework vendor** ships the primitive free to sell the framework — LangGraph time-travel and `CachePolicy`, Dagster data versions, Inngest step memoization, Temporal, Cloudflare Workflows.
- The **hyperscaler** ships it free as a platform control — CloudTrail hash-chained signed digests (2015), S3 Object Lock with a Cohasset letter, Purview.
- The **model provider** closes the gap on a quarterly cadence — free `count_tokens` with a published cross-model diff recipe, `/claude-api migrate`, `max_tokens: 0` pre-warm, session budgets, cache diagnostics.

And above that, the **observability incumbent** already holds the traces, the CI hook, the dataset pipeline and the relationship, with the wedge frequently already shipped: Braintrust trials plus hill climbing, Revenium per-customer margin including tool calls, Vantage's published Token Cost Allocation Specification, Datadog Test Impact Analysis.

The graveyard confirms it independently of any incumbent argument. Earthly shut Satellites down and said the reason was competing with its own free tier. Gentrace liquidated to MIT. Replay.io, DBOS and Lucidic each pivoted off time-travel debugging toward QA, reliability and optimization. AgentOps has run the literal "time travel debugging" pitch at $40/mo since Aug 2024 with no Series A. Better-funded teams already ran these experiments and the results are on the record.

Note also that the two candidates which genuinely escape the sunk-cost pattern — #3 and #5 — still died, and died on *measured size* rather than on competition. #5 in particular was the internal "best remaining lead," repeatedly nominated by other kill reports, and when someone finally measured it on 2.6B real billed tokens it came back at 0.10–0.34% of a bill, below the 0.137% cold-share ceiling that killed the original thesis. **The queue is empty. There is no ninth candidate hiding behind these eight.**

## 4. The strongest argument for walking away from the space

Not "the ideas are mediocre." This: **four independent quantities, measured honestly by your own instruments, all came back within noise of zero.** Cold share 0.36% of tokens. Transitive tracking +0.0%. Fan-out coordination 0.10–0.34%. Perfect exact-key reuse ceiling 0.137%. Those are not positioning failures — they are the layer telling you it carries no economic rent. Rent accrues where something is scarce. Nothing in agent-execution plumbing is scarce: the recorder is becoming an OTel semantic convention, the store is S3 plus a hash, the DAG walk is a weekend, the replay primitive is OSS, and the identity function is `CachePolicy.key_func`.

The corollary is that the buyer is wrong too, not just the product. Every candidate sells infrastructure to the AI engineer or the platform lead. That budget is small, already spent, floored at zero by four subsidizing parties, and shrinking as capability migrates into the SDKs. You have no distribution advantage, no data advantage and no relationship advantage there — and you have now measured, at real expense, that you have no technology advantage either. That is a complete absence of every reason to win.

Walk away from the layer, not just the thesis.

## What to carry forward, and what not to

Carry the muscle, not the artifact. The consistent output of this entire exercise was never a product — it was **numbers nobody else publishes**: 6.52B tokens of field cache behavior, the fan-out probe with controlled skew, the 24-workload change-rate sweep, the 12-gateway audit, the burst analysis on 2.6B billed tokens, the shadow-comparator self-disagreement floor. You killed your own thesis with better instrumentation than any vendor in the category ships. That is a real and unusual capability.

But it is credibility and content, not revenue, and dressing it up as pivot #9 would be the exact error this memo is naming. Publish it, take the standing, and let it open doors to a problem chosen on other grounds.

Three rules that would have killed all eight in a day, for next time:

1. **If the primitive ships free in the framework the customer already runs, stop.** This alone kills 8/8.
2. **Size the prize before designing the product.** Every candidate that reached a number reached a number under 1% of a bill. That number was cheap to get and always available first.
3. **If you cannot name the buyer and their pain without first naming your asset, it is not a business.** Test the next thesis by trying to state it without mentioning anything in this repo. If it survives that, it is real.

The next thesis should be selected by finding someone with a budget and a bleeding problem, before you know what you would build for them. Anything selected the other way around will produce a ninth document that looks exactly like these eight.