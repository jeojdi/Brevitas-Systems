# Marketing Agency: 7-Agent Campaign Orchestrator

A realistic multi-agent campaign planning workflow that demonstrates Brevitas per-agent token-savings tracking.

## Overview

The marketing agency orchestrates a **7-agent sequential DAG** for campaign planning:

1. **Intake** — Parse client brief into structured goals
2. **Researcher** — Market, competitor, and audience analysis
3. **Strategist** — Channel and messaging strategy
4. **Copywriter** — Ad, email, and social copy variants
5. **SEO Optimizer** — Keywords and on-page optimization
6. **Editor** — QA review and brand alignment
7. **Reporter** — Final campaign brief assembly

All calls route through the Brevitas SDK with automatic per-agent and per-pipeline token tracking.

## Setup

### Install dependencies

```bash
pip install openai  # For DeepSeek provider
```

### Get a DeepSeek API key (optional)

For real DeepSeek runs, obtain an API key from [api.deepseek.com](https://api.deepseek.com).

Set the environment variable:

```bash
export DEEPSEEK_API_KEY=sk-...
```

Both uppercase and mixed-case variants are supported:
- `DEEPSEEK_API_KEY` (primary)
- `Deepseek_api_key` (fallback)

## Running the Campaign

### With Mock Provider (Deterministic, No API Key Required)

```bash
python -m examples.marketing_agency.run
```

Output:
```
🚀 Starting campaign with MOCK provider...

Run ID: 550e8400-e29b-41d4-a716-446655440000

[1/7] INTAKE AGENT: Parsing brief...
✓ Brief processed
...
[7/7] REPORTER AGENT: Final brief...
✓ Campaign brief assembled

PER-AGENT SAVINGS BREAKDOWN
Agent                Calls      Tokens Saved    Savings %    Cost Saved
──────────────────────────────────────────────────────────────────────
intake               1          150             12.5%        $0.45
researcher           1          2400            35.2%        $7.20
strategist           1          1800            28.9%        $5.40
copywriter           1          900             15.3%        $2.70
seo_optimizer        1          1100            22.1%        $3.30
editor               1          650             18.7%        $1.95
reporter             1          1450            26.8%        $4.35
──────────────────────────────────────────────────────────────────────
TOTAL                7          8450            22.3%        $25.35

✓ Campaign complete! All 7 agents tracked and attributed to pipeline='campaign-launch'
```

### With Real DeepSeek Provider

```bash
export BREVITAS_AGENCY_PROVIDER=deepseek
python -m examples.marketing_agency.run
```

The orchestrator will:
1. Call the real DeepSeek API for each agent
2. Route all calls through Brevitas for token tracking
3. Demonstrate per-agent savings from compression/caching
4. Print the final campaign brief and savings breakdown

## File Structure

```
examples/marketing_agency/
├── __init__.py          # Package marker
├── orchestrator.py      # 7-agent DAG orchestrator with Brevitas tracking
├── provider.py          # Provider abstraction (mock and deepseek)
├── run.py              # Campaign execution entry point
└── README.md           # This file
```

## How It Works

### Label Propagation

Each campaign execution uses Brevitas labels:

```python
with brevitas.start_run(pipeline="campaign-launch") as run:
    agency = MarketingAgency()
    # Inside the context, each agent call is automatically labeled with:
    # - pipeline = "campaign-launch"
    # - run_id = <unique run identifier>
    # - agent = <role name> (set by the agent context manager)
```

### Mock Provider

For CI testing and deterministic behavior without API costs:

- Returns canned realistic responses per agent type
- Costs zero API tokens
- Allows testing the orchestrator logic and Brevitas tracking

### DeepSeek Provider

For real multi-agent workflows:

- Uses OpenAI-compatible client pointed at `https://api.deepseek.com/v1`
- Supports both `deepseek-chat` and `deepseek-reasoner` models
- Enables live validation of per-agent token savings
- Requires valid `DEEPSEEK_API_KEY`

## Brevitas Integration

All agent calls use Brevitas SDK wrapping:

```python
def _call_agent(self, agent_name: str, model: str, system_prompt: str, user_input: str) -> str:
    with brevitas.agent(agent_name):
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]
        response_text = self.provider.chat(model, messages, temperature=0.7)
        return response_text
```

The `brevitas.agent()` context manager:
1. Sets the agent label in a contextvar
2. Propagates the label through the call stack
3. Ensures the call is recorded with `agent=<role>` in the database

## Reconciliation

Per the token-savings tracking design:

- **Agent level**: Each agent's call is recorded with savings attributed to it
- **Pipeline level**: Sum of agent savings = pipeline total
- **Account level**: Sum of pipeline savings = account total

Query the `/v1/stats/agents?pipeline=campaign-launch` endpoint to verify:

```json
{
  "by_agent": [
    {
      "agent": "intake",
      "calls": 1,
      "tokens_saved": 150,
      "savings_pct": 12.5,
      "cost_saved_usd": 0.45
    },
    ...
  ],
  "pipeline_total": {
    "calls": 7,
    "tokens_saved": 8450,
    "savings_pct": 22.3,
    "cost_saved_usd": 25.35
  }
}
```

## Testing

Run integration tests:

```bash
pytest tests/test_phase_d_marketing_agency.py -v
```

Tests cover:
- Orchestrator initialization
- All 7 agents execute successfully
- Context sharing between agents
- Mock provider determinism
- Brevitas label tracking
- Provider factory
- Full campaign with tracking

## Next Steps

Phase E will integrate this into CI/CD:
- Run mock provider in required CI checks
- Optional real DeepSeek job when `DEEPSEEK_API_KEY` secret is set
- Assert per-agent tracking and reconciliation invariants

Phase F will mirror labels to Supabase for analytics and billing.
