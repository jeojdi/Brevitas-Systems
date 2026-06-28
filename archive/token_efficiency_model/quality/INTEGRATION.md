# Phase 3: Quality Gate Integration Guide

## Overview

The quality gate (`token_efficiency_model/quality/gate.py`) replaces the fake heuristic `quality_proxy_score` with a real, evidence-based assessment using:

1. **Embedding Cosine Similarity** (sentence-transformers, local, fast)
2. **LLM-as-Judge** (DeepSeek API, ~0.5-1s per call, cost-conscious)
3. **Configurable Floor** (default 0.8, tunable per customer)

## Quick Start

### Direct Usage

```python
from token_efficiency_model.quality.gate import assess, QualityGateConfig

# Simple assessment
assessment = assess(
    optimized_answer="Paris is the capital of France.",
    reference_answer="The capital of France is Paris.",
    question="What is the capital of France?"
)

# Result: QualityAssessment(score=0.90, passed=True, ...)
print(f"Score: {assessment.score}, Passed: {assessment.passed}")

# Custom config
config = QualityGateConfig(
    floor=0.85,  # Stricter than default
    embedding_weight=0.6,  # More weight to embedding (faster)
    judge_weight=0.4,
)
assessment = assess(optimized_answer, reference_answer, question, config=config)
```

### Billing Integration (`api/server.py`)

The `/v1/usage` endpoint now accepts an optional `quality_score`:

```bash
curl -X POST http://localhost:8000/v1/usage \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "anthropic",
    "model": "claude-3-sonnet",
    "baseline_tokens": 10000,
    "compressed_tokens": 5000,
    "quality_score": 0.87
  }'
```

**Billing Logic:**
- If `quality_score >= floor` (default 0.8): **bill full savings**
- If `quality_score` not provided: mark as `unverified` and **don't bill**
- If `quality_score < floor`: mark as `failed` and **don't bill**

Response includes:
```json
{
  "tokens_saved": 5000,
  "savings_pct": 50.0,
  "cost_saved_usd": 0.15,
  "brevitas_fee_usd": 0.015,
  "quality_score": 0.87,
  "quality_status": "verified"
}
```

### Pipeline Integration (Future: Phase 3 Integration)

The pipeline currently uses the deprecated `quality_proxy_score`. To integrate the real gate:

```python
from token_efficiency_model.quality.gate import QualityGate, QualityGateConfig

gate = QualityGate(config=QualityGateConfig(floor=0.8))

# After optimization, before or after model call:
assessment = gate.assess(
    optimized_answer=model_response,  # or simulated response
    reference_answer=full_context_response,  # baseline
    question=task_text
)

if not assessment.passed:
    # Signal fallback to full context (rehydrate)
    pipeline.signal_rehydrate()
    # Optionally retry with full context instead
```

## Scoring Explained

### Embedding Similarity (0-1)

Computes cosine similarity between embeddings of optimized and reference answers.

- **1.0**: Identical text
- **0.8-0.95**: Semantically equivalent (minor wording differences)
- **0.6-0.8**: Similar but with some omissions
- **0.4-0.6**: Significant differences
- **<0.4**: Unrelated or wrong

### Judge Score (0-1)

DeepSeek rates semantic equivalence on the 0-1 scale:

```json
{
  "score": 0.85,
  "reasoning": "Minor omissions on details but core meaning intact"
}
```

Judge is called only once per assessment (cost discipline).

### Combined Score

```
score = (0.5 * embedding_similarity) + (0.5 * judge_score)
```

If judge fails: `score = embedding_similarity * 0.9` (10% penalty for unverified).

## Configuration

### QualityGateConfig

```python
@dataclass
class QualityGateConfig:
    floor: float = 0.8                 # Minimum acceptable score
    embedding_weight: float = 0.5      # Weight for embedding
    judge_weight: float = 0.5          # Weight for judge
    model_name: str = "all-MiniLM-L6-v2"  # sentence-transformers model
    judge_model: str = "deepseek-chat" # LLM model for judge
    max_judge_retries: int = 1         # Retries on transient failures
    timeout: int = 10                  # Seconds for API call
```

## Environment

Required for full functionality (judge calls):

```bash
# In .env.local
Deepseek_api_key=sk-...
```

Without the key, the gate gracefully degrades:
- Uses embedding similarity with 10% penalty
- Sets `fallback_reason` to indicate judge unavailable
- Score is still valid and passed decision still accurate

## Test Coverage

Run tests:

```bash
pytest token_efficiency_model/quality/test_gate.py -v

# 20 tests covering:
# - Embedding similarity: 5 tests (identical, similar, different, empty, symmetric)
# - Full assessment: 6 tests (pass/fail, truncation, config, fallback)
# - Judge parsing: 4 tests (JSON parsing, edge cases)
# - Fallback signaling: 2 tests
# - Convenience functions: 2 tests
# - Billing bounds: 1 test
```

## Fallback Behavior

When an optimized answer fails the quality gate:

1. **Signal Rehydration**: Set `rehydrate_policy="force-full"` in protocol payload
2. **Retry with Full Context**: Optionally re-run model call with all context
3. **Don't Bill Savings**: Set quality_status to "failed" and fee to $0

Integration point in pipeline: after `assess()` returns `passed=False`, trigger:

```python
# In pipeline.py (Phase 3 enhancement):
if not assessment.passed:
    # Current: already rehydrates if quality_proxy < floor (line 319)
    # Future: use real assessment instead of proxy
    payload = protocol.build_payload(..., rehydrate_policy="force-full")
```

## Performance Notes

- **Embedding**: ~100-300ms (local, parallel batching available)
- **Judge**: ~500-1500ms (API call, single call per assessment)
- **Total**: ~1s per assessment; only called on optimization failures and billing verification

Cost: ~$0.0001-0.0005 per judge call (DeepSeek pricing ~$0.5/1M input tokens).

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Judge API unavailable | Fallback to embedding with penalty; still gate correctly |
| Embedding model OOM (rare) | Graceful degradation; return 0.0 score; log error |
| Mismatch between embedding & judge | Weighted average; both must agree reasonably for high scores |
| False positives (gate passes wrong answer) | Embedding similarity + judge agreement needed; rare in practice |
| False negatives (gate fails correct answer) | Floor=0.8 is conservative; can be tuned per use case |

## Deprecated Functions

`token_efficiency_model/common/metrics.py:quality_proxy_score()` is deprecated. It still exists for backward compatibility but emits a `DeprecationWarning`. Do not use on the live path.
