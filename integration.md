# Brevitas Integration Guide

Brevitas optimizes multi-agent pipelines by reducing token usage across every turn — without changing your agents or prompts. There are two ways to integrate it: the **Python SDK** (pip package) for in-process use, and the **REST API** for language-agnostic or server-side use.

To get an API key, [contact us](mailto:contact@brevitas.systems).

---

## Python SDK

### Install

```bash
pip install brevitas-systems
```

### Authenticate

Set your API key as an environment variable (recommended) or pass it directly:

```bash
export BREVITAS_API_KEY=bvt_your_key_here
```

```python
# or configure in code
from brevitas import configure
configure(api_key="bvt_your_key_here")
```

### Basic usage

```python
from brevitas import optimize
from my_pipeline import architect, builder, reviewer

pipeline = optimize([architect, builder, reviewer])
result = pipeline.run("Build a REST API with auth and rate limiting")

# ↳ 59% fewer tokens. 47% lower cost. 99% quality parity.
print(result.model_response)
print(f"{result.savings_pct:.0f}% tokens saved")
```

`optimize()` accepts any list of callables. Each agent receives the previous agent's output as its input. Brevitas compresses, prunes, and routes between turns — your agent code is unchanged.

### `optimize()` parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `agents` | `list` | required | Agent callables. Each receives the prior agent's output. |
| `api_key` | `str` | env var | Your `bvt_` prefixed Brevitas key. |
| `quality_floor` | `float` | `0.98` | Minimum quality score (0–1) before compression stops. |
| `savings_target` | `float` | `59.0` | Token savings % to target per turn. |
| `compression_level` | `int` | `2` | Message compression aggressiveness (1–3). |
| `prune_budget` | `int` | `5` | Max context chunks retained per turn. |
| `protocol_mode` | `str` | `"compact"` | Wire format: `"compact"` or `"verbose"`. |
| `delta_mode` | `str` | `"on"` | Send only changes between turns: `"on"` or `"off"`. |

### `pipeline.run()` parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `task` | `str` | required | Task description / prompt. |
| `incoming_messages` | `list[str]` | `[]` | Additional messages to include in this turn. |
| `complexity` | `float` | `0.5` | Task complexity hint (0–1). Higher values retain more context. |
| `urgency` | `float` | `0.5` | Urgency hint (0–1). Higher values favor recency over breadth. |
| `task_id` | `str` | `"brevitas-task"` | Stable ID for delta caching across turns. |

### `PipelineResult` fields

```python
result.model_response      # str   — concatenated agent outputs
result.savings_pct         # float — % tokens saved vs. baseline
result.baseline_tokens     # int   — unoptimized token count
result.optimized_tokens    # int   — actual token count sent
result.quality_proxy       # float — estimated quality retention (0–1)
result.routed_model        # str   — model the router selected
result.debug               # dict  — compression, sampling, pruning internals
```

### Multi-turn example

`pipeline.run()` is stateful — context from each call is automatically retained and pruned for the next.

```python
pipeline = optimize([architect, builder, reviewer])

r1 = pipeline.run("Design the database schema")
r2 = pipeline.run("Now implement the API endpoints")
r3 = pipeline.run("Write tests for the auth layer")
# Each turn reuses compressed context from the previous turns.
```

### Using with LangChain / custom agent objects

Any object with a `run()` or `invoke()` method works as an agent:

```python
from langchain.agents import AgentExecutor
from brevitas import optimize

pipeline = optimize([agent_executor_1, agent_executor_2])
result = pipeline.run("Summarize Q3 earnings and flag risks")
```

---

## REST API

The Brevitas REST API exposes the same optimization engine over HTTP. Authenticate all requests with your API key in the `X-API-Key` header.

```
Base URL: https://api.brevitas.systems
```

### Authentication

```bash
curl https://api.brevitas.systems/v1/health \
  -H "X-API-Key: bvt_your_key_here"
```

All endpoints except `/v1/health` and `/v1/providers` require `X-API-Key`.

---

### Endpoints

#### `POST /v1/compress`

Compress a list of messages and prune context for the next agent turn.

**Rate limit:** 60 requests / minute

**Request body**

```json
{
  "messages":          ["<agent message 1>", "<agent message 2>"],
  "prior_context":     ["<context chunk 1>", "<context chunk 2>"],
  "task":              "optional task description for better routing",
  "complexity":        0.5,
  "urgency":           0.5,
  "compression_level": 2,
  "prune_budget":      5,
  "delta_mode":        "off",
  "wire_mode":         "json"
}
```

| Field | Type | Default | Constraints |
|---|---|---|---|
| `messages` | `string[]` | required | max 100 items, each ≤ 50,000 chars |
| `prior_context` | `string[]` | `[]` | max 200 items, each ≤ 50,000 chars |
| `task` | `string` | `""` | max 2,000 chars |
| `complexity` | `float` | `0.5` | 0.0 – 1.0 |
| `urgency` | `float` | `0.5` | 0.0 – 1.0 |
| `compression_level` | `int` | `2` | 1 – 3 |
| `prune_budget` | `int` | `5` | 1 – 50 |
| `delta_mode` | `string` | `"off"` | `"on"` or `"off"` |
| `wire_mode` | `string` | `"json"` | `"json"` or `"msgpack"` |

**Response**

```json
{
  "compressed_messages": ["..."],
  "pruned_context":      ["..."],
  "baseline_tokens":     412,
  "optimized_tokens":    171,
  "savings_pct":         58.5,
  "quality_proxy":       0.9921,
  "routed_model_hint":   "llama3.2",
  "model_response":      "...",
  "state_id":            "abc123"
}
```

**Example**

```bash
curl -X POST https://api.brevitas.systems/v1/compress \
  -H "X-API-Key: bvt_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": ["Agent A finished the plan. Here are the steps: ..."],
    "prior_context": ["User wants a FastAPI app", "Auth is JWT-based"],
    "task": "implement the endpoints",
    "complexity": 0.7
  }'
```

---

#### `GET /v1/stats`

Usage statistics for the authenticated key.

**Rate limit:** 120 requests / minute

**Response**

```json
{
  "total_calls":           142,
  "total_tokens_saved":    58210,
  "avg_savings_pct":       57.3,
  "avg_quality_proxy":     0.9918,
  "total_baseline_tokens": 98400,
  "total_optimized_tokens": 42190,
  "history": [
    {
      "timestamp":        "2026-06-13T18:42:00Z",
      "baseline_tokens":  412,
      "optimized_tokens": 171,
      "savings_pct":      58.5,
      "quality_proxy":    0.9921
    }
  ]
}
```

---

#### `PUT /v1/provider`

Configure the model provider used to run tasks through your pipeline.

**Rate limit:** 30 requests / minute

**Supported providers**

| Provider | Models |
|---|---|
| `ollama` | `llama3.2`, `llama3.1`, `mistral`, `gemma3`, `phi4`, `qwen2.5` |
| `anthropic` | `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001` |
| `openai` | `gpt-4o`, `gpt-4o-mini`, `o3-mini` |
| `grok` | `grok-3`, `grok-3-mini` |
| `deepseek` | `deepseek-chat`, `deepseek-reasoner` |

**Request body**

```json
{
  "provider":         "anthropic",
  "provider_api_key": "sk-ant-...",
  "model":            "claude-sonnet-4-6"
}
```

`provider_api_key` is not required for `ollama`.

**Example**

```bash
curl -X PUT https://api.brevitas.systems/v1/provider \
  -H "X-API-Key: bvt_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{"provider": "openai", "provider_api_key": "sk-...", "model": "gpt-4o-mini"}'
```

---

#### `GET /v1/provider`

Get the currently configured provider for the authenticated key. The provider API key is masked.

---

#### `GET /v1/providers`

List all supported providers and their available models. No authentication required.

---

#### `GET /v1/health`

```json
{ "status": "ok" }
```

No authentication required. Use for uptime checks.

---

### Error responses

| Status | Meaning |
|---|---|
| `401` | Missing or invalid `X-API-Key` |
| `400` | Validation error (see `detail` field) |
| `413` | Request body exceeds 2 MB |
| `429` | Rate limit exceeded |

---

## End-to-end integration example

This example shows a full three-agent pipeline using the Python SDK with an Anthropic backend configured via the API.

```python
import os
import requests
from brevitas import optimize

BREVITAS_KEY = os.environ["BREVITAS_API_KEY"]

# 1. Configure your model provider once (or via dashboard)
requests.put(
    "https://api.brevitas.systems/v1/provider",
    headers={"X-API-Key": BREVITAS_KEY},
    json={
        "provider":         "anthropic",
        "provider_api_key": os.environ["ANTHROPIC_API_KEY"],
        "model":            "claude-sonnet-4-6",
    },
)

# 2. Define your agents (plain callables)
def architect(task: str) -> str:
    # your agent logic here
    return f"Architecture plan for: {task}"

def builder(plan: str) -> str:
    return f"Implementation of: {plan}"

def reviewer(code: str) -> str:
    return f"Review complete. Issues found: none. {code[:40]}..."

# 3. Wrap with Brevitas
pipeline = optimize([architect, builder, reviewer])

# 4. Run
result = pipeline.run(
    "Build a rate-limited REST API with JWT auth",
    complexity=0.8,
    urgency=0.4,
)

print(result.model_response)
print(f"Saved {result.savings_pct:.0f}% of tokens this turn")

# 5. Check cumulative usage
stats = requests.get(
    "https://api.brevitas.systems/v1/stats",
    headers={"X-API-Key": BREVITAS_KEY},
).json()
print(f"Total tokens saved: {stats['total_tokens_saved']:,}")
```
