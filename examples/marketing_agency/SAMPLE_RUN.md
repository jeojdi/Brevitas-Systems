# Marketing Agency — Live DeepSeek Run (real, measured, billed)

**This is a real run against the DeepSeek API**, recorded in the Brevitas usage store
(`api/brevitas.db`, table `usage_log`) and attributed per agent under
`pipeline = "campaign-launch"`. Numbers below are measured token counts and computed
DeepSeek costs from the Brevitas pipeline — not estimates, not constants.

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

All 7 agents executed against real DeepSeek, each call routed through Brevitas
`/v1/compress` (+ `/v1/usage` for billing) with `pipeline`/`agent`/`run_id` labels.
Context accumulates down the pipeline, so baseline grows hop to hop.

## Per-agent savings + billing (measured)

| Agent          | Baseline tok | Optimized tok | Saved | Cost saved (USD) | Brevitas fee (10%) |
|----------------|-------------:|--------------:|------:|-----------------:|-------------------:|
| intake         |          240 |           188 |    52 |     $0.00001404 |       $0.00000140 |
| researcher     |          776 |           724 |    52 |     $0.00002860 |       $0.00000286 |
| strategist     |        3,519 |         3,062 |   457 |     $0.00025135 |       $0.00002514 |
| copywriter     |        7,014 |         6,487 |   527 |     $0.00014229 |       $0.00001423 |
| seo_optimizer  |        8,784 |         8,624 |   160 |     $0.00004320 |       $0.00000432 |
| editor         |       12,434 |        11,533 |   901 |     $0.00024327 |       $0.00002433 |
| reporter       |       16,677 |        14,145 | 2,532 |     $0.00068364 |       $0.00006836 |
| **TOTAL**      |   **49,444** |    **44,763** | **4,681** | **$0.00140639** |    **$0.00014064** |

**Verified empirically against `usage_log`:**
- Exactly **7 rows** (one per agent — no double-recording).
- All rows `provider = "deepseek"` → correct price table applied.
- `cost_saved_usd > 0` and `brevitas_fee_usd > 0`; fee = exactly 10% of cost saved.
- Σ(per-agent saved) = 4,681 = pipeline total. Attribution reconciles.

The dollar figures are small by design: DeepSeek input pricing is ~$0.27/1M tokens and
these are short prompts. The billing mechanism is real and scales linearly with token
volume and model price.
