# Marketing Agency — Live DeepSeek Run (real, measured)

**This is a real run against the DeepSeek API**, recorded in the Brevitas usage store
(`api/brevitas.db`, table `usage_log`) and attributed per agent under
`pipeline = "campaign-launch"`. Numbers below are measured token counts from the
Brevitas pipeline — not estimates, not constants.

## How it was run (reproducible)

```bash
# 1. start the Brevitas API server
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000

# 2. create a Brevitas key and set the provider to DeepSeek
KEY=$(curl -s -X POST localhost:8000/v1/keys -d '{"name":"agency-live-run"}' | jq -r .api_key)
curl -s -X PUT localhost:8000/v1/provider -H "X-API-Key: $KEY" \
  -d "{\"provider\":\"deepseek\",\"provider_api_key\":\"$DEEPSEEK_API_KEY\",\"model\":\"deepseek-chat\"}"

# 3. run the 7-agent campaign live (takes several minutes — reasoner agents are slow)
BREVITAS_API_KEY=$KEY DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY \
BREVITAS_AGENCY_PROVIDER=deepseek BREVITAS_BASE_URL=http://127.0.0.1:8000 \
python -m examples.marketing_agency.run
```

All 7 agents executed against real DeepSeek (`deepseek-chat` / `deepseek-reasoner`),
each call routed through Brevitas `/v1/compress` with `pipeline`/`agent`/`run_id` labels.

## Per-agent savings (measured)

| Agent          | Model             | Baseline tok | Optimized tok | Saved | Savings % |
|----------------|-------------------|-------------:|--------------:|------:|----------:|
| intake         | deepseek-chat     |          240 |           188 |    52 |     21.7% |
| researcher     | deepseek-reasoner |          727 |           671 |    56 |      7.7% |
| strategist     | deepseek-reasoner |          908 |           511 |   397 |     43.7% |
| copywriter     | deepseek-chat     |          867 |           461 |   406 |     46.8% |
| seo_optimizer  | deepseek-chat     |        2,018 |         1,612 |   406 |     20.1% |
| editor         | deepseek-chat     |        4,054 |         3,815 |   239 |      5.9% |
| reporter       | deepseek-chat     |        6,369 |         5,623 |   746 |     11.7% |
| **TOTAL**      |                   |   **15,183** |    **12,881** | **2,302** | **15.2%** |

**Reconciliation:** Σ(per-agent saved) = 2,302 = pipeline total. ✓ Attribution holds
across all 7 agents under `pipeline="campaign-launch"`.

Context accumulates down the pipeline (reporter sees all prior outputs → 6,369 baseline
tokens), so later agents carry the largest absolute savings.

## Known issues found during this run (open — must fix)

1. **`run.py` response-shape mismatch (cosmetic, misleading).** `run.py` expects the
   stats response as `{"by_agent": [...], "pipeline_total": {...}}`, but
   `GET /v1/stats/agents` returns a bare JSON array. So `run.py` prints "No agent
   statistics recorded" and exits 1 **even though the data was recorded correctly**.
   The numbers above were read directly from the API/DB, not from `run.py`'s summary.

2. **Double-recording.** An agent call can produce two `usage_log` rows — one from the
   server-side `/v1/compress` handler and one from the SDK wrapper's `/v1/usage` report
   (observed for `intake`). This inflates `calls` and can double-count savings.

3. **Provider mislabel -> $0 billing.** The SDK wrapper records `provider="openai"` (and
   some rows have empty provider) for what are DeepSeek calls, so `cost_for_tokens` finds
   no matching DeepSeek price and `cost_saved_usd`/`brevitas_fee_usd` come out **0**.
   Token tracking is correct; the **cost/fee (billing) path is not yet functional** until
   the provider label is fixed to `deepseek`.

**Bottom line:** per-pipeline / per-agent **token** tracking works end-to-end on live
DeepSeek and reconciles. The **dollar billing** half needs the provider-label fix before
the "charge a % of savings" number is real.
