# Brevitas Integration Guide

Brevitas is middleware between your code and the model providers (Anthropic, OpenAI,
DeepSeek, Groq, plus Azure OpenAI and AWS Bedrock for Claude). You reach it by pointing
your existing SDK at the Brevitas **base URL** and adding two headers — your app, prompts,
and provider keys are otherwise unchanged.

There are two ways in:

- **Hosted gateway (recommended, billable path).** A base-URL change. Requests route
  through Brevitas, which forwards them to the provider with your own provider key,
  measures usage from the provider's receipt, and returns the response unchanged.
- **Local proxy.** A zero-code install that keeps every byte on your machine. Great for a
  private savings dashboard, but its receipts are non-authoritative and **not** billable —
  see [README](README.md#local-proxy--privacy-first-not-on-savings-based-pricing).

This guide covers the hosted gateway.

---

## 1. Connect

```bash
pip install brevitas-systems
brevitas connect
```

`brevitas connect` opens your browser, you approve as a workspace owner or admin, and it
mints an organization service key (`bvt_...`) scoped to your workspace, registers your
tenant id, and prints ready-to-paste snippets. Store the key with `--env-file .env` or
`--store-key` (OS keyring); it is shown once.

You now have three values:

| Value | What it is | Where it goes |
|---|---|---|
| Your **provider key** (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …) | Bills you directly; forwarded upstream unchanged, never stored | the SDK's normal `api_key=` |
| `BREVITAS_API_KEY` (`bvt_...`) | Identifies you to Brevitas | the `X-Brevitas-Key` header |
| `BREVITAS_CUSTOMER_ID` | The tenant to attribute usage to | the `X-Brevitas-Customer-ID` header |

### Using local AI coding tools instead of your own code?

If your traffic comes from tools like Claude Code, Cursor, or Copilot — not code you
control — install the [BVX CLI](README.md) and run one command instead of editing base URLs:

```bash
brew install Brevitas-ai/brevitas/bvx
bvx connect
```

`bvx connect` signs you in, then routes your detected tools through the hosted gateway
(attaching `X-Brevitas-Key` for you), so their usage is metered on the **same billable
path** as the SDK below — with no per-tool base-URL or header edits. `bvx disconnect`
returns them to calling providers directly. It reuses BVX's local proxy as the forwarder,
so keep the background service running (`bvx start`).

---

## 2. Point your client at the gateway

### Option A — one line with `brevitas.hosted()` (recommended)

`brevitas.hosted()` sets the gateway base URL and both headers for you, reading
`BREVITAS_API_KEY` / `BREVITAS_CUSTOMER_ID` from the environment when you don't pass them.

```python
from openai import OpenAI
import brevitas

# OPENAI_API_KEY, BREVITAS_API_KEY, BREVITAS_CUSTOMER_ID in the environment
client = brevitas.hosted(OpenAI(), customer_id="acme")

client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "ping"}],
)
```

Anthropic is identical with the Anthropic SDK:

```python
from anthropic import Anthropic
import brevitas

client = brevitas.hosted(Anthropic(), customer_id="acme")
client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "ping"}],
)
```

`hosted()` returns a configured copy of your client; use it exactly like the original. It
does **not** apply client-side optimization (the gateway does that server-side), so don't
also wrap it with `brevitas.wrap()`.

### Option B — set the base URL and headers yourself

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://api.brevitassystems.com/v1",
    api_key=os.environ["OPENAI_API_KEY"],          # your provider key, forwarded upstream
    default_headers={
        "X-Brevitas-Key": os.environ["BREVITAS_API_KEY"],
        "X-Brevitas-Customer-ID": "acme",
    },
)
```

Node (OpenAI SDK):

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "https://api.brevitassystems.com/v1",
  apiKey: process.env.OPENAI_API_KEY,              // your provider key, forwarded upstream
  defaultHeaders: {
    "X-Brevitas-Key": process.env.BREVITAS_API_KEY,
    "X-Brevitas-Customer-ID": "acme",
  },
});
```

curl:

```bash
curl https://api.brevitassystems.com/v1/chat/completions \
  -H "X-Brevitas-Key: $BREVITAS_API_KEY" \
  -H "X-Brevitas-Customer-ID: acme" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}'
```

---

## 3. Two headers, and the #1 first-call failure

- **`X-Brevitas-Key`** is the only header the gateway authenticates on. It is **not**
  `Authorization` — on the hosted gateway `Authorization` carries *your provider key* and
  is forwarded upstream. Passing the Brevitas key as `api_key=`/`Authorization` returns
  `401 Missing X-Brevitas-Key header`.
- **`X-Brevitas-Customer-ID`** attributes the request to a tenant. An organization service
  key **rejects every proxy call without it**:

  ```
  400  {"detail": "Organization service proxy calls require X-Brevitas-Customer-ID"}
  ```

  This is the single most common reason a first request fails. If you are the tenant, use
  one stable id (e.g. your company slug); if you resell, send each end customer's stable id
  from your own database.

---

## 4. Supported providers

The gateway natively proxies **Anthropic Messages** and **OpenAI** Chat Completions,
Responses, Completions, and Embeddings (including OpenAI-compatible providers such as
DeepSeek and Groq, routed by model name), plus **Azure OpenAI** and **AWS Bedrock**
(Claude). See the [README](README.md) for the Azure and Bedrock lanes and their
constraints. Gemini is not currently a native proxy integration; `report_receipt()` can
still normalize Gemini usage for accounting.

Unknown models are metered but shown as **Unpriced** rather than being charged a guessed
price.

---

## 5. Base URL, without the footgun

- The **gateway** base URL your SDK talks to is `https://api.brevitassystems.com/v1` — the
  SDK appends the rest of the path (`/chat/completions`, `/messages`, …). `brevitas.hosted()`
  builds this for you.
- `BREVITAS_BASE_URL` is a **different** setting — the control-plane origin the SDK uses to
  report receipts — and must be the **bare origin with no `/v1`** (the SDK appends `/v1`
  itself, so a `/v1` suffix yields `/v1/v1` and 404s). Only set it if you self-host.

---

## 6. How pricing works

Brevitas is moving to **credit-based pricing**:

- **Bring your own provider key.** The provider bills you directly for tokens, unchanged.
- **Per-request credits.** Each hosted-gateway request draws a small, flat number of
  Brevitas credits — independent of token volume.
- **Free trial credits** are granted to every new workspace so you can integrate and see
  value before paying.
- **Buy more** via prepaid credit packs (self-serve) or a subscription (larger accounts).

Verified savings are always measured and shown in your dashboard — that is the feature, not
the meter. Billing is rolling out; while it is, no per-request fee is charged. Check your
status any time in the dashboard, or with `brevitas billing-check`. (Implementation detail:
[CREDIT_PRICING_PLAN.md](CREDIT_PRICING_PLAN.md).)

---

## 7. Verify it's working

- Make one call through the gateway, then open the dashboard — your usage should appear.
- `brevitas connect` records non-secret connection metadata (endpoint, org id, key prefix,
  expiry) in `~/.config/brevitas/connection.json`. Service keys expire (default 90 days);
  rotate before then with another `brevitas connect`.

Questions: [contact@brevitas.systems](mailto:contact@brevitas.systems).
