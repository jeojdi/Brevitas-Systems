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
- **Two ways in.** The **hosted gateway** is a base-URL change and is the path that
  produces metered, billable savings. The **local proxy** is a zero-code install that
  keeps every byte on your machine — and, by design, cannot be billed on
  percentage-of-savings.

Site: https://brevitassystems.com

## Install

```bash
pip install brevitas-systems            # core
pip install "brevitas-systems[all]"     # + retrieval embeddings, llmlingua, provider SDKs
```

## Quick start — hosted gateway (recommended)

One command. It opens your browser, you approve as a workspace owner or admin, and
it hands back an organization service key scoped to your workspace.

```bash
brevitas connect
```

Then three lines in your app — no install, no background service, no code changes
beyond the client constructor:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.brevitassystems.com/v1",
    api_key=os.environ["BREVITAS_API_KEY"],
    default_headers={"X-Brevitas-Customer-ID": "acme"},   # required — see below
)
```

```bash
export BREVITAS_API_KEY=bvt_...
export OPENAI_BASE_URL=https://api.brevitassystems.com/v1
export BREVITAS_CUSTOMER_ID=acme
```

Confirm your traffic is actually being metered. `brevitas billing-check` and its
`GET /v1/billing/readiness` endpoint are **designed but not yet shipped**; until they
are, ask us and we will read it out of the usage log for you — the query is in
`docs/ONBOARD_HOSTED_CUSTOMER.md` §3.2.

### `X-Brevitas-Customer-ID` is required on every hosted request

An organization service key **rejects every proxy call without it**:

```
400  {"detail": "Organization service proxy calls require X-Brevitas-Customer-ID"}
```

This is the single most common reason a first request fails. It is deliberate: one
organization key can route traffic for many end customers, and the header is what
says which one. Identity assignment is exact and stable — never semantic, never
fuzzy.

- **You are the tenant** (most integrations): use one stable id such as your company
  slug. `brevitas connect` creates that customer record up front and pins it to the
  key it mints, so a header-less call resolves to it rather than 400ing. The header
  still wins whenever it is present, and we still recommend always sending it.
- **You resell to your own customers**: send each end customer's stable id from your
  own database. `brevitas connect --multi-tenant` leaves the key unpinned so a
  missing header stays a hard 400 — attribution is never guessed from "this account
  only has one customer".

Existing customers can be bulk-imported by stable id (`POST /v1/customers/import`) or
created automatically on first traffic. End customers do not install anything and do
not receive Brevitas keys.

## Local proxy — privacy-first, not on savings-based pricing

Everything stays on your machine. Your provider keys stay in **your** environment or
`.env`; Brevitas never receives them in this flow.

Be aware of the tradeoff: receipts from the local proxy arrive over `POST /v1/usage`
and are recorded **non-authoritative**, because a client-side proxy cannot certify
its own savings. They give you dashboards and accounting. They are **not** eligible
for percentage-of-savings billing — that requires the hosted gateway above.

### 1. See where you'd save (no changes made)

```bash
brevitas init            # scans your workspace, finds every LLM call site,
                         # checks which provider keys you have, shows next steps
brevitas init --ai       # add an LLM pass for tricky/dynamic call sites
```

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

## Billing (hosted gateway only)

Brevitas bills a percentage of **verified** savings only. Savings are checked by an
always-valid sequential quality gate (mSPRT) on an audited sample; if a lever's quality
drops, billing for it stops automatically. Every call is logged with the provider's
usage receipt and an idempotency key.

Three things are true and worth knowing before you pick a path:

- **Only hosted-gateway traffic is billable.** Receipts posted by the local proxy are
  recorded non-authoritative by design, because a client-side proxy cannot certify its
  own savings. Local-proxy usage produces analytics, never an invoice.
- **Billing is off until a human at Brevitas attests your commercial arrangement.**
  The database refuses that write from the application entirely, so no bug and no
  leaked key can turn savings into a charge. Ask us for your attestation state at any
  time — it is not an internal detail, and a self-service view of it is planned.
- **Savings that come only from cache replays currently settle at $0.** Today's
  halting conditions stop any period where zero-spend rows dominate the savings, and a
  cache replay is a zero-spend row by construction. The redesign is pending. We would
  rather say this here than have you find it on an invoice.

Operator-side detail: [Onboarding a hosted (billable) customer](docs/ONBOARD_HOSTED_CUSTOMER.md).

## Cloud usage tracking

See [Account and company onboarding](docs/ONBOARDING.md) for the individual,
employee-invitation, workspace-switching, and enterprise-customer flows.

For a SaaS integration, the SaaS company holds one Brevitas service key per environment
(`brevitas connect` mints one; the dashboard's **Company Administration → service
accounts** is the manual equivalent). Each request from its backend includes an exact,
stable `X-Brevitas-Customer-ID` from its own database — see
[the header rules above](#x-brevitas-customer-id-is-required-on-every-hosted-request),
which are the most common cause of a failed first request. End customers do not install
BVX and do not receive Brevitas keys.

AgentMap-discovered backend services, workers, Claude Code, Codex, and custom clients all
write the same content-free receipt:

`account → project → environment → source/agent → provider → model → operation`

```bash
export BREVITAS_API_KEY=bvt_...
export BREVITAS_PROJECT=billing-app
export BREVITAS_ENVIRONMENT=production
export BREVITAS_SOURCE=api-worker
```

`BREVITAS_BASE_URL` selects the control plane receipts are sent to and defaults to
`https://api.brevitassystems.com`. Set it **only** if you run your own API (self-hosted or
local development) — otherwise a self-hosted deployment reports its usage to the hosted
service. Do not give it a `/v1` suffix: the SDK appends `/v1` itself, so a `/v1` base
produces `/v1/v1` and silently 404s.

A `/v1` suffix is correct for **gateway** base URLs (`ANTHROPIC_BASE_URL`,
`OPENAI_BASE_URL`, the OpenAI SDK's `base_url`) and wrong for `BREVITAS_BASE_URL`. Both
`https://api.brevitassystems.com/v1` and `https://brevitassystems.com/v1` reach the
gateway — the marketing origin rewrites `/v1/*` to the API host — but prefer the direct
`api.` host, which is what `brevitas connect` prints and one fewer hop.

When `BREVITAS_PROJECT` is unset the SDK falls back to your local Git-root folder name so
the dashboard has a project dimension. That folder name is your own material, so
`BREVITAS_PROJECT_AUTO=0` suppresses the fallback and sends nothing.

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
