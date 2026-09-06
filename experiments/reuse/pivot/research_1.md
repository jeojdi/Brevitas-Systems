# Model drift as a business: research findings

## How fast does the churn actually run

Provider deprecation is now a scheduled, involuntary, recurring project, not an occasional event. All of the following is from live vendor documentation read today (2026-08-18), not memory.

**Anthropic** ([model deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations)) commits to "at least 60 days' notice before model retirement for publicly released models." The last four retirements came in at 62, 60, 62 and 61 days of notice — the earlier ones (Opus 3, Sonnet 3.7) ran 189 and 114 days. **Notice is compressing to the contractual floor.** Service life is roughly 12 months: Opus 4.1 shipped 2025-08-05 and was dead 2026-08-05, to the day. Sonnet 4 and Opus 4 lived 13 months. Meanwhile the frontier ID cadence is about six weeks — opus-4-5 (Nov 2025), 4-6 (Feb 2026), 4-7 (Apr 2026), 4-8 (May 2026), fable-5 (Jun 2026), opus-5 (Jul 2026). Three more forced migrations are already dated in the next three months (sonnet-4-5 "not sooner than" 2026-09-29, haiku-4-5 2026-10-15, opus-4-5 2026-11-24).

**OpenAI** ([deprecations](https://developers.openai.com/api/docs/deprecations)) is more generous on paper — "at least 6 months" for GA models, 3 for specialised variants, and explicitly "much shorter notice, such as 2 weeks" for previews — but the volume is higher and it retires *products*, not just models. Live right now: the entire GPT-5 snapshot family (`gpt-5-2025-08-07`, `o3-2025-04-16`, `gpt-5-pro-2025-10-06`) shuts down 2026-12-11; the legacy snapshot sweep (`gpt-4o-2024-05-13`, `gpt-4-turbo`, `o1`, `o3-mini`) on 2026-10-23; the Assistants API on **2026-08-26, eight days from now**; reusable prompts, Agent Builder, and OpenAI's own Evals platform on 2026-11-30.

**Google** ([Gemini models doc](https://ai.google.dev/gemini-api/docs/models)) has the shortest window and the most dangerous default: preview models are "deprecated with at least 2 weeks notice," and for `-latest` aliases, "a 2-week notice will be provided through email before the version behind latest is changed." Two weeks, by email, for a silent behaviour swap.

**Azure/Microsoft Foundry** ([lifecycle policy](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/model-retirements)) is the one that changes models underneath you by design. GA models get an 18-month life with the retirement date set programmatically at launch — "there's no separate announcement" — and ≥60 days of active notice. But Global Standard / Standard deployments are **auto-upgraded** on a rolling region-by-region basis, controlled by a `versionUpgradeOption` property most teams never touch; preview deployments are **force-upgraded** with 30 days notice and "no option to remain on a retiring preview model." Anthropic, DeepSeek, Fireworks and Mistral models on Foundry get 12 months, not 18.

## What changes silently

This is the part that turned out to be better-evidenced than I expected, because the vendors now admit it in writing.

Anthropic's [model IDs and versioning](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions) page — which exists because an HN post on 2026-04-15 ("Anthropic no longer allows you to fix to specific model version," 27 points) claimed the dateless 4.6-generation IDs were evergreen pointers — states the pin guarantee and then immediately qualifies it:

> "Model weights are fixed for a given ID, but the serving infrastructure around the model can change over time. This infrastructure includes components such as the request router, safety classifiers, and sampling logic. Occasionally, infrastructure updates produce minor differences in observable behavior even when the model ID and weights have not changed."

That is a vendor admission of the exact failure mode: same ID, different behaviour, no notice, no version number to point at. The HN poster's specific claim (that pinning is impossible) is **refuted** by the doc — the dateless ID *is* a pinned snapshot — but the fact that Anthropic had to write a page to say so is itself the signal.

The empirical backing is strong. Anthropic's own [postmortem of three recent issues](https://www.anthropic.com/engineering/a-postmortem-of-three-recent-issues) documents three overlapping infrastructure bugs between 2025-08-05 and 2025-09-18: a context-window routing error that at peak affected **16% of Sonnet 4 requests** and touched **~30% of Claude Code users**; TPU output corruption emitting Thai and Chinese tokens into English responses; and an XLA:TPU approximate-top-k bug. Crucially, Anthropic says its own evaluation process "simply didn't capture the degradation users were reporting" — the vendor, with full access, could not detect it with benchmarks.

On the alias side, a detailed single-source practitioner report ([HN 47271099](https://news.ycombinator.com/item?id=47271099), 2026-03-06, 2 points, 0 comments) describes `gemini-flash-latest` being repointed to `gemini-3-flash-preview` on 2026-01-21, a model that does not support Search grounding. "The API never returned an error. HTTP 200, valid JSON, finish reason: STOP. The only thing missing was groundingMetadata." Roughly one month of ungrounded, hallucinated part specs accumulated in a B2B parts database before anyone noticed; then 16 hours and 63 commits chasing a bug that was not in their code. Treat this as one unverified account — but it is specific, dated, falsifiable, and matches Google's documented 2-week-email policy exactly.

The academic record is thinner than the folklore suggests but consistent. Chen, Zaharia and Zou's [How Is ChatGPT's Behavior Changing over Time?](https://arxiv.org/abs/2307.09009) remains the canonical measurement: GPT-4 prime-identification accuracy fell from 84% (March 2023) to 51% (June 2023), with degraded chain-of-thought responsiveness and instruction-following. [LLM Output Drift in Financial Workflows](https://arxiv.org/abs/2511.07585) (Nov 2025, 480 runs) found the harder problem: at T=0.0, small models were 100% output-consistent while GPT-OSS-120B managed **12.5%** (95% CI 3.5–36.0%), with RAG task drift of 25–75%.

Migration is also not just a string swap. Anthropic's [migration guide](https://platform.claude.com/docs/en/about-claude/models/migration-guide) lists, across four generations in ten months: assistant prefill removed (400 error); `temperature`, `top_p` and `top_k` rejected with a 400 on 4.7 and later; extended thinking with `budget_tokens` removed; thinking content omitted by default; thinking silently *on* by default from Opus 5; effort levels recalibrated with a new `max` tier; a new tokenizer from Opus 4.7 producing **~30% more tokens for identical text** (independently corroborated by a practitioner measurement of 20–30% higher cost per Claude Code session); Fable 5 at 2× the price of Opus 5 and unavailable under ZDR.

Note the second-order consequence, which I have not seen anyone state: **you can no longer set temperature=0 on Anthropic's frontier models.** Exact-output matching as a drift detector is dead there by provider fiat.

## What tooling exists, and who is selling it

Everything funded frames the diff as *"you changed your app."* Nothing funded frames it as *"the provider changed under you."*

[LangSmith](https://docs.langchain.com/langsmith/evaluation-concepts) sells "Regression testing: ensure new versions don't degrade quality" — versions of *your* application, on *your* dataset. [Braintrust](https://www.braintrust.dev/pricing) ($0 / $249/mo / custom) sells Observe, Evaluate, Discover, with no cross-model-version framing on the pricing page. Arize Phoenix sells "rerun them through different versions of your application." Datadog LLM Observability offers outlier detection on duration and error rate — operational metrics, not behaviour.

The provider-changed framing exists only as unfunded hobbyware. On HN, "Seismograph — open-source early warning for silent LLM API drift" (1 point, 0 comments), "Continuum — GitHub Action that detects LLM drift in CI" (1 point), "PromptDrifter — catch LLM prompt drift before it breaks prod" (3 points), "DriftProof" (1 point). Every single one is in the noise floor.

And the commercial layer is being absorbed and given away. **Humanloop was acquired by Anthropic** (announced 2025-08-13) and sunset its platform on 2025-09-08. **OpenAI acquired Promptfoo** on 2026-03-09 — Promptfoo having raised an $18.4M Series A in Sept 2025 positioned not as evals but as "the definitive AI security stack." Three months later OpenAI deprecated its own Evals platform and named Promptfoo, a free open-source CLI, as the migration path. Promptfoo Community is "Free Forever" with 24.3k GitHub stars.

This is the same shape as the caching wedge: the capability is real, the buyers are real, and the platform vendors are shipping it for free as a retention feature.

## Is the pain felt and budgeted

Felt: yes, clearly. [Ask HN: How are people doing AI evals these days?](https://news.ycombinator.com/item?id=47319587) (2026-03-10, 30 points, 43 comments) is the best sentiment sample I found. Representative: one commenter notes teams "spend hours on human review in eval pipelines, accumulating time with each new model release," and that eval pipelines remain "much less standardized compared to traditional software monitoring." Another: "people complaining that a new model comes out, it's amazing for a few weeks, then they nerf it." Another runs ~1000 prompts per hypothesis, "hours of grinding." Another: "the space moves too fast to justify major investments" in eval tooling. The Opus 4.7 launch thread carried 1,452 comments including reports of integration breakage from reasoning-summary format changes.

Budgeted: yes, but the budget line is called **evals**, and it is already contested, already commoditized, and already being bought by the model providers themselves. "Drift detection" is not a line item anyone I found is buying separately.

## What this means for the surviving asset

The content-addressed (exact inputs → exact output) record is a genuinely good fit for two of the four legs a drift product needs, and a measured non-fit for the hardest one.

It gives you the **replayable input closure** — the exact bytes, including upstream artifact hashes — which is precisely what eval platforms do *not* store (they store sampled, often truncated or redacted traces). It gives you the **prior output** to compare against, at full production coverage rather than a 200-row golden set. The transitive dependency graph, measured worthless for cost, is actually the right shape for **attribution**: given a behaviour change in a DAG, it tells you which node's output hash moved first. That is a real reuse of a dead asset.

What it does not give you is **detection**, and the experiment already measured why: the shadow comparator disagreed with itself 14.3% of the time and 20.0% at zero perturbation. That is not a bug in the comparator — it is the provider's non-determinism floor, corroborated externally by the 12.5% consistency figure at T=0.0, and now made structural by Anthropic rejecting `temperature` outright on 4.7+. Any product here has to be a statistical comparator over N samples with a judge, not an exact-match store. Content addressing buys the replay and the attribution; it does not buy the signal.

The cheap substitute is the thing everyone already does: a golden dataset plus LLM-as-judge, re-run at migration time. That needs no content addressing at all. So the differentiated claim would have to rest on *coverage* (replay real production traffic, not a curated set) and *attribution* (which upstream operation moved), not on detection. I found no evidence that anyone is buying either.

---

## Bottom line

**Established (verified live against primary sources today):**
- Forced-migration cadence is real and dated. Anthropic: ~12-month model life, notice compressed to the 60-day contractual floor on the last four retirements, ~6-week frontier ID cadence, three more retirements dated in the next three months. OpenAI: 6-month GA floor, 2 weeks for previews, Assistants API dies 2026-08-26. Google: 2 weeks for previews *and* for repointing `-latest`. Azure: 18-month GA life with auto-upgrade on by default for Standard deployments and no-opt-out force-upgrade for previews.
- Same-ID behaviour change is vendor-admitted, not folklore. Anthropic's versioning doc names router, safety classifiers and sampling logic as things that change under a pinned ID and produce observable behavioural differences.
- Silent degradation has happened at scale and was undetectable by the vendor's own benchmarks: 16% of Sonnet 4 requests at peak, ~30% of Claude Code users, six weeks, three overlapping bugs.
- Migration is a real engineering project, not a string swap: prefill removed, sampling params 400ing, thinking defaults inverted twice, effort recalibrated, tokenizer +30% tokens, Fable 5 at 2× price and ZDR-incompatible — all within ten months.
- Exact-output matching is not a viable drift detector. Provider non-determinism is measured at 12.5% consistency at T=0.0 on a 120B model, the experiment's own comparator self-disagreed 20.0% at zero perturbation, and Anthropic now rejects temperature=0 on frontier models.
- No funded vendor sells "the provider changed under you." LangSmith, Braintrust, Arize and Datadog all frame the diff as *your app changed*. The provider-changed framing exists only as 1–3 point GitHub hobby projects.
- The eval layer is being absorbed by the model providers and given away free: Humanloop → Anthropic (2025-08), Promptfoo → OpenAI (2026-03), OpenAI's own paid Evals product killed 2026-11-30 with a free OSS tool named as the replacement.

**Contested:**
- Whether "no funded vendor sells this" is an unserved gap or a demonstrated absence of demand. The funded players have every capability needed to add provider-drift detection cheaply and have not — which reads more like *nobody asks* than *nobody can*.
- Whether the pain is budgeted as its own line or absorbed into an existing evals/observability line. Practitioner reports describe hours of human review per model release, but no one describes a purchase order for drift.
- The gemini-flash-latest grounding incident is a single self-published account with 2 points and no corroboration. Detailed and falsifiable, but uncorroborated, and it contains at least one internally odd date reference. Treat as illustrative, not as evidence of frequency.
- The HN "cannot pin Anthropic versions" claim is refuted by Anthropic's docs on the facts, but the confusion it represents is real enough that Anthropic wrote a page to correct it.

**Unknown:**
- Actual dollar spend on LLM eval/observability tooling, and its growth rate. I could not verify any market-size figure — the session's web-search budget was exhausted before I could, and I will not quote one from memory.
- How many teams are on `-latest` aliases or Azure auto-upgrade defaults in production, i.e. the true exposed population. Nobody publishes this.
- Whether coverage (replay all production traffic) and attribution (which upstream op moved) are worth paying for over a golden set plus a judge. No evidence either way; this is the load-bearing unknown for any product decision here.
- What the false-positive rate of a statistical comparator would be at production volume, and whether it lands below the threshold where an on-call engineer stops trusting it. The experiment's 14.3%/20.0% self-disagreement suggests this is the pivotal engineering risk, and it is unmeasured at N-sample granularity.

**Sources:** [OpenAI deprecations](https://developers.openai.com/api/docs/deprecations) · [Anthropic model deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations) · [Anthropic model IDs and versioning](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions) · [Anthropic models overview](https://platform.claude.com/docs/en/about-claude/models/overview) · [Anthropic migration guide](https://platform.claude.com/docs/en/about-claude/models/migration-guide) · [Anthropic postmortem of three recent issues](https://www.anthropic.com/engineering/a-postmortem-of-three-recent-issues) · [Azure/Foundry model lifecycle](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/model-retirements) · [Gemini API models](https://ai.google.dev/gemini-api/docs/models) · [Gemini API changelog](https://ai.google.dev/gemini-api/docs/changelog) · [LangSmith evaluation concepts](https://docs.langchain.com/langsmith/evaluation-concepts) · [Braintrust pricing](https://www.braintrust.dev/pricing) · [Promptfoo pricing](https://www.promptfoo.dev/pricing/) · [Promptfoo joining OpenAI](https://www.promptfoo.dev/blog/promptfoo-joining-openai/) · [Chen, Zaharia, Zou 2023](https://arxiv.org/abs/2307.09009) · [LLM Output Drift in Financial Workflows](https://arxiv.org/abs/2511.07585) · [Ask HN: How are people doing AI evals these days?](https://news.ycombinator.com/item?id=47319587) · [HN: gemini-flash-latest silently broke Search grounding](https://news.ycombinator.com/item?id=47271099) · [HN: Anthropic no longer allows you to fix to specific model version](https://news.ycombinator.com/item?id=47775389)