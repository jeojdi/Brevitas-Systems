# Brevitas — technical audit

*What the system actually does, how it differs from prompt-compression tools and
memory layers, and where the current gaps are. Written against the code as of
July 2026 (`backend-enterprise-release`), with file references so claims can be
checked.*

---

## 1. What Brevitas is

Brevitas sits between an application and the model providers (Anthropic, OpenAI,
DeepSeek, Groq). There are two ways onto the path, both requiring no rewrite of
the calling code:

- **Local proxy** (`brevitas/proxy.py`): `brevitas start` runs a proxy on
  `:4242`; the app points `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` at it and the
  existing SDK code runs unchanged.
- **Client wrap** (`brevitas/wrappers/`): `brevitas.wrap(openai.OpenAI())`
  returns a client with the same interface. `brevitas apply --write` can insert
  the wrap automatically after showing a diff; `brevitas init`
  (`brevitas/scanner/`) finds the call sites first.

The important design decision is what happens on that path **by default**:
almost nothing. The default request passes through with its content untouched.
Brevitas reads the provider's own usage accounting on the way back
(`brevitas/receipts.py`) and attributes it (`brevitas/labels.py`). Anything that
could change what the model sees or returns is a separately gated lever.

That inversion — measure first, and treat every content-touching optimization as
a liability to be justified — is the actual architecture. Everything below
follows from it.

## 2. The request path, concretely

**Metering.** `receipts.py` normalizes usage objects from Anthropic, OpenAI
(Responses and Chat Completions shapes), Cohere-style `billed_units`, and
Gemini-style fields into one `TokenReceipt`: fresh input, cached input, cache
writes (with Anthropic's 5-minute vs 1-hour write tiers kept separate, because
they price at 1.25× and 2× respectively and must not be double-counted),
and output. Prompt and response content is not retained — the receipt is numbers
only.

**Attribution.** `labels.py` propagates `pipeline` / `agent` / `run_id` through
Python contextvars, so a multi-agent run gets per-agent cost attribution without
anyone threading IDs by hand. Per-call metadata overrides the contextvar; the
contextvar overrides the empty default.

**Provider cache routing.** The router preserves stable prompt prefixes and
works with each provider's native prefix cache
(`token_efficiency_model/lossless/provider_cache.py`). For Anthropic it can
place `cache_control` breakpoints — only where the cumulative prefix
(tools → system → prior turns) clears the 1024-token minimum, never on the
volatile tail, at most four. It does **not** do this by default
(`BREVITAS_ANTHROPIC_CACHE=1` required), for an economic reason worth stating
plainly: an Anthropic cache write costs a premium, and no online router can
prove a future read will occur. Writing speculatively can lose money.
Caller-owned cache policy is always preserved. Savings are computed from the
provider's own usage fields via `savings_from_usage`, with the turn-1 write
surcharge counted **against** the savings, not hidden.

**Exact response cache.** `semantic_cache.py` Layer 1 is a SHA-256 hash over the
entire request — model ID, system prompt, params, every message. A hit replays a
prior complete response and skips the model call. This is on by default because
it is byte-identical: it can only fire on an exact repeat (retries, agent loops,
parallel agents sharing context). It is reported as a **call avoided** — not as
compression, and not as a losslessness claim about anything else.

## 3. The gated levers

Four things can change model behavior, and all four are off until an operator
sets an env flag **and** the tenant's per-lever quality gate is untripped
(fail-closed on both conditions — see `api/server.py` around lines 661, 3044,
3096):

| Lever | Flag | What it can do wrong |
|---|---|---|
| Retrieval instead of full context | `BREVITAS_RETRIEVAL_ENABLED` | omit a passage the answer needed |
| Lossy compression (LLMLingua) | `BREVITAS_COMPRESS_LOSSY` | rewrite context |
| Message reordering | `BREVITAS_MESSAGE_REORDER` | change conversational order |
| Semantic response reuse | `BREVITAS_SEMANTIC_CACHE` | serve a response to a non-identical prompt |

The semantic layer (Layer 2 of the cache) is deliberately narrow even when
enabled: it only considers prior requests where *everything except the last user
message* is byte-identical — same model, same system prompt, same tools, same
params, same prior turns — and then matches that final message by embedding
similarity. Tool-using, streaming, and high-temperature calls are never cached.
TTL with jitter bounds staleness.

The underlying algorithms are not hand-rolled. Each lever in
`token_efficiency_model/lossless/` is a documented implementation of a published
algorithm with the primary source cited and the paper's own pseudocode extracted
(`docs/levers/ALGORITHMS.md`): Anthropic/OpenAI prompt-caching semantics;
IPFS-style content addressing with LBFS content-defined chunking (13-bit
boundary mask, 48-byte window) for dedup; Myers diff → VCDIFF ops and rsync
rolling checksums for delta transmission; DPR dense retrieval with ColBERTv2
MaxSim and residual compression; and the MIT RLM loop (arXiv:2512.24601) for
contexts that should never enter the window at all. Every reconstruction path
re-hashes what it rebuilt and falls back to the full content on any mismatch.

## 4. How the accounting stays honest

This is the part most likely to be skimmed and it should not be. Brevitas
reports savings **by mechanism**, because the mechanisms are not economically
equivalent:

- **Native-cache discount** — the provider processed the tokens but billed them
  cheaper. Token count unchanged; cost lower.
- **Provider input tokens avoided** — retrieval/compression actually sent fewer
  tokens. Cost and count lower; quality risk nonzero.
- **Model calls avoided** — exact-cache replay. No call happened.
- **Transport bytes avoided** — dedup/delta on the wire. Affects bandwidth and
  latency, not the provider bill.
- **Measured lift** — an isolated control arm (`brevitas/compare.py` runs the
  same question down both paths against the same document, with the provider's
  own caching credited to the *no-Brevitas* side so the comparison isn't
  rigged in our favor).

A tool that collapses these into one "% saved" number is making a claim it
can't support. Keeping them separate is what lets a customer audit the invoice.

The benchmark record follows the same discipline (`docs/levers/BENCHMARKS.md`):
every number is labelled **algorithmic** (deterministic, synthetic inputs,
exact by construction) or **real-model** (real model, real public dataset,
official metric). The real-model result is stated with its cost: on HotpotQA
with DeepSeek, retrieval at k=8 preserves exact-match accuracy (Δ0.0) for ~23%
token savings; k=5 reaches ~55% savings but drops EM by ~7.5 points because
multi-hop questions sometimes lose a needed passage. That frontier is published
rather than smoothed over, and it is why retrieval ships off by default.

## 5. What this is not

**Not a prompt-compression tool.** Compression products (LLMLingua wrappers,
trimmers, summarize-the-history middlewares) mutate the prompt as their core
loop and report a blended savings figure. Brevitas treats mutation as the
exception: the default path is content-preserving, the mutating levers fail
closed per tenant, and a lever that degrades quality trips off rather than
continuing to save money on wrong answers. LLMLingua exists in the stack — as
one gated lever, not as the product.

**Not a memory layer.** Memory products (the Mem0 / Zep / LangMem shape) extract
facts and summaries from conversations, store them, and inject the derived text
into future prompts. That is a content store plus a prompt rewrite — useful for
personalization, but the model now sees text the developer never wrote, and the
vendor now holds conversation content. Brevitas does neither: the metering path
retains no content, telemetry is allowlisted and content-free by construction
(`docs/OBSERVABILITY.md` — no prompts, no bodies, no raw URLs, exceptions
reduced to a class name), and nothing derived is injected into a prompt. The
goal is not to make the model "remember"; it is to stop paying repeatedly for
transmitting and recomputing bytes that haven't changed, while the model sees
exactly what it would have seen anyway.

**Not a semantic cache in the GPTCache sense.** Generic semantic caches match on
"the question sounds similar" and will happily serve one user's answer to a
different model, temperature, or system prompt. Here the fuzzy match is scoped
to a single position in an otherwise byte-identical request, keyed on model ID,
restricted to near-deterministic calls, and gated per tenant.

## 6. Audit findings — gaps and risks

An audit that only lists strengths isn't one.

1. **Wiring gap.** Levers 1, 2, 3, and 5 are validated library modules with
   tests, but are **not yet on the live request path**
   (`docs/levers/README.md`, "Wiring status"). Today the live path carries
   metering, provider cache routing, the exact cache, and the gated retrieval
   endpoint. Marketing claims should track the live path, not the library.
2. **Lever 1 numbers are simulated.** The provider-caching benchmark uses
   documented discount rates, not live traffic (`BENCHMARKS.md` says so, but
   the 70.69% figure travels well and its caveat doesn't). A live-key rerun
   would close this.
3. **Semantic cache scale ceiling.** The nearest-neighbour scan is brute-force
   cosine per context bucket — fine to ~10k rows by its own comment, with the
   pgvector migration (002) written but pending for hosted scale.
4. **Retrieval quality measurement is thin.** One dataset (HotpotQA), one
   answering model (DeepSeek), n=40 for the end-to-end run. Directionally
   solid, statistically light. The recorded plan
   (`benchmarks/levers/RETRIEVAL_QUALITY_PLAN.md`) should be executed before
   retrieval is recommended for accuracy-critical tenants.
5. **Fail-closed depends on gate integrity.** The safety story rests on the
   per-tenant trip gates in `api/server.py`. Those gates deserve their own
   adversarial tests (can a mislabeled request bypass a tripped gate?) —
   the current tests exercise the happy path more than the boundary.

## 7. Summary

The defensible technical position is not "we reduce tokens" — many things
reduce tokens — and not "we are a memory layer," which describes a different
product with a different data-custody posture. It is narrower and stronger:
**the default path changes nothing and measures everything; every
content-touching optimization is a published algorithm behind a per-tenant
fail-closed gate; and savings are reported by mechanism, from provider-reported
usage, with the costs (cache-write premiums, accuracy tradeoffs) counted
against the number.** The near-term work is to close the gap between the
validated library and the live path, and to replace the remaining simulated
numbers with traffic-real ones.
