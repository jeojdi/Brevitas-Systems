# Brevitas — provider-cache optimization and metering for LLM agents

Brevitas is middleware that sits between your code and the model providers
(Anthropic, OpenAI, DeepSeek, Groq). Its default request path preserves prompt
content while measuring provider-native cache reads and writes. Optional retrieval,
compression, message reordering, and fuzzy response reuse can reduce provider work,
but can affect behavior and are disabled until explicitly enabled.

- **Content-preserving default.** Requests pass through unchanged except for explicitly
  enabled provider cache metadata. Provider caching can lower cost without lowering the
  provider's token count.
- **Quality-affecting levers fail closed.** Retrieval, LLMLingua, reordering, and fuzzy
  semantic response reuse require explicit operator opt-in and an untripped tenant gate.
- **Mechanism-separated evidence.** Reports distinguish provider input tokens avoided,
  native-cache discount, model calls avoided, transport bytes avoided, and measured
  Brevitas lift from an isolated control arm.
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
# use `client` exactly as before — requests are metered and safe cache routing is applied
```

`brevitas apply --write` can insert that wrap for you (shows a diff and asks first).

## What it does per request

A router measures provider prefix-cache behavior and preserves stable prompt prefixes.
OpenAI-compatible providers normally cache those prefixes automatically. For GPT-5.6,
Brevitas can add a tenant-scoped `prompt_cache_key`; billable explicit breakpoints require
`BREVITAS_OPENAI_CACHE_BREAKPOINTS=1`. Brevitas-owned Anthropic cache writes require
`BREVITAS_ANTHROPIC_CACHE=1`, because a write has a premium and no online router can prove
that a future read will occur. Caller-owned cache policy is always preserved.

Quality-affecting features are separately opt-in:

- `BREVITAS_RETRIEVAL_ENABLED=1` can omit context.
- `BREVITAS_COMPRESS_LOSSY=1` can rewrite context.
- `BREVITAS_MESSAGE_REORDER=1` can change conversational ordering.
- `BREVITAS_SEMANTIC_CACHE=1` can reuse a response for a non-identical prompt.

The byte-identical exact response cache is separate and remains available by default;
it skips a model call by replaying a prior complete response. That is reported as a
**call avoided**, not as prompt compression or a blanket losslessness claim.

## Evidence and benchmarks

Historical benchmark percentages in this repository are not product claims. Provider
cache discounts are not Brevitas-incremental savings unless an isolated control arm proves
the difference. New benchmark output must report randomized paired control/treatment runs,
isolated cache namespaces, fixed transcripts, cold and warm results, repeated trials, and
confidence intervals. Without that control evidence, the dashboard shows the provider's
native cache discount but leaves “Brevitas vs control” unmeasured.

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

The gateway natively proxies Anthropic Messages plus OpenAI Responses, Chat Completions,
Completions, and Embeddings (including compatible providers). Gemini is **not** currently
a native wrapper or proxy integration. `report_receipt()` can normalize Gemini SDK
`usage_metadata` objects for accounting—including cached, candidate, and thinking tokens—
but it does not optimize Gemini requests or establish Brevitas-attributable savings:

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

Active development on `main`. The maintained test suites cover the provider proxy,
tenant isolation, receipt accounting, cache safety, and quality gates. Provider support
is described above; no unsupported provider or benchmark percentage is implied.
