# Brevitas — drop-in token savings for your LLM agents

Brevitas is middleware that sits between your code and the model providers
(Anthropic, OpenAI, DeepSeek, Groq) and **cuts your token bill losslessly** —
caching, retrieval and cost-aware routing are applied automatically, and every
optimization fails safe to sending your request untouched.

- **Lossless first.** No answer degradation from the caching/retrieval path; the
  optional lossy compressor is off by default and gated behind a quality check.
- **Honest savings.** Cost is computed from the provider's real usage fields
  (including cached-token discounts), not estimates.
- **Two ways in**, both drop-in: a zero-code proxy, or a one-line client wrap.

Site: https://brevitassystems.com

## Install

```bash
pip install brevitas-systems            # core
pip install "brevitas-systems[all]"     # + retrieval embeddings, llmlingua, provider SDKs
```

## Quick start

### 1. See where you'd save (no changes made)

```bash
brevitas init            # scans your workspace, finds every LLM call site,
                         # checks which provider keys you have, shows next steps
brevitas init --ai       # add an LLM pass for tricky/dynamic call sites
```

Your API keys stay in **your** environment / `.env` — Brevitas never receives them
in the self-hosted flow.

### 2a. Zero-code proxy — no code changes

```bash
brevitas start                         # starts the local proxy on :4242
export ANTHROPIC_BASE_URL=http://localhost:4242
export OPENAI_BASE_URL=http://localhost:4242/openai   # also routes DeepSeek/Groq by model
```

Your existing SDK code now runs through Brevitas unchanged.

### 2b. One-line wrap — per client

```python
import openai, brevitas
client = brevitas.wrap(openai.OpenAI())      # or anthropic.Anthropic()
# use `client` exactly as before — savings applied automatically
```

`brevitas apply --write` can insert that wrap for you (shows a diff and asks first).

## What it does per request

A router estimates, in **cache-adjusted dollars**, whether to lean on the provider's
prefix cache, retrieve only the relevant context, or pass through — using
longest-common-prefix matching (the rule providers actually cache by) and the real
observed cache-hit rate. Retrieval uses an append-only layout so its context stays
cache-stable across turns. Anthropic cache breakpoints are placed automatically.

## Measured savings (real APIs, lossless)

| Workload | Provider | Input savings | Total savings |
|---|---|---|---|
| Multi-turn Q&A over a doc / coding agent | Anthropic (Haiku) | ~88% (warm turns) | ~82% |
| Same | DeepSeek | ~73% | ~70% |
| Same | OpenAI (gpt-4o-mini) | ~49% | ~48% |
| ai-hedge-fund style 6-analyst pipeline | DeepSeek | — | ~30% |
| crewAI marketing 5-agent pipeline | DeepSeek | — | ~5%* |

\* Multi-agent pipelines where **each agent has a distinct system prompt** benefit
less from prefix caching (the shared context sits behind the differing prefix). The
big wins are in repeated-context patterns (chatbots, coding agents, doc analysis,
single-persona multi-turn). Turn 1 on Anthropic shows a small *negative* due to the
cache-write premium, repaid within one warm turn.

Numbers are from `benchmarks/live_e2e.py` and `benchmarks/oss_ab.py` (real DeepSeek /
OpenAI / Anthropic calls) — reproduce them yourself with your keys in `.env.local`.

## Billing (if you use the hosted metering)

Brevitas bills a percentage of **verified** savings only. Savings are checked by an
always-valid sequential quality gate (mSPRT) on an audited sample; if a lever's quality
drops, billing for it stops automatically. Every call is logged with the provider's
usage receipt and an idempotency key.

## Status

Active development on `algo/wave-a`. Core levers (caching, retrieval, cost-aware
router, billing gate) are implemented, tested (250+ tests), and live-verified on all
three providers.
