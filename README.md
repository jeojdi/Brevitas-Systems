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

## Cloud usage tracking

See [Account and company onboarding](docs/ONBOARDING.md) for the individual,
employee-invitation, workspace-switching, and enterprise-customer flows.

For a SaaS integration, the SaaS company holds one Brevitas service key per environment. Each
request from its backend includes an exact, stable `X-Brevitas-Customer-ID` from its own database.
End customers do not install BVX and do not receive Brevitas keys. Existing customers may be
bulk-imported by stable ID or are created automatically on first traffic; identity assignment is
never semantic or fuzzy.

AgentMap-discovered backend services, workers, Claude Code, Codex, and custom clients all
write the same content-free receipt:

`account → project → environment → source/agent → provider → model → operation`

```bash
export BREVITAS_API_KEY=bvt_...
export BREVITAS_PROJECT=billing-app
export BREVITAS_ENVIRONMENT=production
export BREVITAS_SOURCE=api-worker
```

The hosted gateway accepts `X-Brevitas-Key` plus the equivalent `X-Brevitas-*` metadata
headers. Provider keys use their normal `Authorization` or `X-Api-Key` header. Unknown
models retain token totals and are shown as **Unpriced** rather than receiving a guessed
price.

The gateway meters Anthropic Messages plus OpenAI Responses, Chat Completions, Completions,
and Embeddings (including AgentMap's OpenAI-compatible providers). Native Gemini/Vertex,
Bedrock, Cohere, Replicate, Hugging Face, Ollama, LiteLLM, and framework calls submit the
numeric receipt through the same adapter—no model content is sent:

```python
import brevitas

brevitas.report_receipt(
    "google_gemini", "your-model", baseline_tokens=1200,
    usage=response.usage_metadata,
    operation="generate_content",
)
```

For Codex, export `OPENAI_API_KEY` (the customer's provider key), `BREVITAS_API_KEY`,
`BREVITAS_REPO`, and `BREVITAS_CLIENT=codex`, then add this to `~/.codex/config.toml`:

```toml
model_provider = "brevitas"
model = "YOUR_OPENAI_MODEL"

[model_providers.brevitas]
name = "Brevitas"
base_url = "https://brevitassystems.com/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
env_http_headers = { "X-Brevitas-Key" = "BREVITAS_API_KEY", "X-Brevitas-Repo" = "BREVITAS_REPO", "X-Brevitas-Client" = "BREVITAS_CLIENT" }
```

For Claude Code:

```bash
export ANTHROPIC_BASE_URL="https://brevitassystems.com"
export BREVITAS_CLIENT="claude-code"
export ANTHROPIC_CUSTOM_HEADERS="X-Brevitas-Key: ${BREVITAS_API_KEY}
X-Brevitas-Repo: ${BREVITAS_REPO}
X-Brevitas-Client: ${BREVITAS_CLIENT}"
```

These follow the supported [Codex custom-provider configuration](https://developers.openai.com/codex/config-advanced/)
and [Claude Code environment variables](https://code.claude.com/docs/en/env-vars).

The Supabase `usage_log` stores numeric categories and labels only—never prompts, responses,
code, absolute paths, Git remotes, or raw provider receipts. A hosted proxy necessarily sees
request and response bytes in transit; use the SDK/direct receipt path when that is not acceptable.

## Status

Active development on `algo/wave-a`. Core levers (caching, retrieval, cost-aware
router, billing gate) are implemented, tested (250+ tests), and live-verified on all
three providers.
