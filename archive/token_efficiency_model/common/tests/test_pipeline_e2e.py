"""
End-to-end pipeline tests — verify real compression, savings, and quality.
Run: pytest token_efficiency_model/common/tests/test_pipeline_e2e.py -v
"""
import pytest
from token_efficiency_model.combined_tactics.pipeline import TokenEfficientPipeline
from token_efficiency_model.common.metrics import estimate_tokens, estimate_tokens_many


AGENT_CONVERSATIONS = [
    {
        "task": "Build a Redis-backed rate limiter in Python",
        "messages": [
            "Agent 1 (Planner): The user wants a rate limiter. I have analyzed the requirements. "
            "The rate limiter should use a token bucket algorithm backed by Redis. Each user gets "
            "100 requests per minute. I analyzed the request and we need a token bucket rate limiter "
            "backed by Redis with per-user isolation.",
            "Agent 2 (Architect): Based on the planner's analysis above, we need Redis with GCRA. "
            "The implementation should use Redis to store bucket state per user. Each user gets 100 "
            "tokens per minute. We need GCRA for atomicity. Redis will store the per-user token state. "
            "100 tokens per minute is the agreed-upon limit from the planning phase.",
        ],
        "prior_context": [
            "User is building a Python API gateway service.",
            "User is building a Python API gateway service with FastAPI.",
            "The gateway handles 5000 requests per second at peak load.",
            "The gateway currently handles around 5000 req/s at peak.",
            "Rate limiting is needed to prevent abuse from individual clients.",
            "Rate limiting must be enforced to stop client abuse.",
            "Redis 7.2 is already deployed in the infrastructure.",
            "The team uses Redis 7.2 in their existing infrastructure stack.",
        ],
    },
    {
        "task": "Write a Python sorting function",
        "messages": [
            "Agent 1: I need to write a sort utility. The function should accept a list. "
            "I will implement a sort function. The sort function should sort in ascending order. "
            "The function must return a new sorted list and not modify the input in place.",
            "Agent 2: Building on agent 1's work, I will now implement the sort function. "
            "As described above, the sort function accepts a list and returns a new sorted list. "
            "The sort should be in ascending order as specified. I will use Python's sorted() builtin.",
        ],
        "prior_context": [
            "Project uses Python 3.11.",
            "Project language is Python 3.11.",
            "No external dependencies allowed.",
            "External libraries are not permitted in this codebase.",
        ],
    },
    {
        "task": "Review a database migration for safety",
        "messages": [
            "Agent 1 (Analyst): I reviewed the migration file. The migration adds a NOT NULL column "
            "to a 50M row table. This is a potentially dangerous operation. Adding a NOT NULL column "
            "to a large table requires a table rewrite. The migration adds a NOT NULL column with a "
            "default value which triggers a full table rewrite on PostgreSQL < 11.",
            "Agent 2 (Reviewer): Based on the analyst's review, the migration adds a NOT NULL column. "
            "The table has 50 million rows. A full table rewrite will lock the table. "
            "The migration is potentially dangerous for a 50M row table as described in the analysis.",
        ],
        "prior_context": [
            "Database is PostgreSQL 14.",
            "Running PostgreSQL version 14.",
            "Table: users, rows: ~50 million.",
            "The users table has approximately 50 million rows.",
            "Zero-downtime deployments required.",
            "Deployments must have zero downtime.",
        ],
    },
]


@pytest.fixture(scope="module")
def pipeline():
    return TokenEfficientPipeline(
        model_backend=None,
        quality_floor=0.85,
        savings_target=20.0,
    )


@pytest.mark.parametrize("convo", AGENT_CONVERSATIONS)
def test_compression_reduces_tokens(pipeline, convo):
    """Compressed output must be fewer tokens than baseline."""
    result = pipeline.process_task(
        task_text=convo["task"],
        incoming_messages=convo["messages"],
        prior_context=convo["prior_context"],
        compression_level=2,
        prune_budget=4,
    )
    assert result.baseline_tokens > 0, "Baseline should be non-zero"

    compressed_msgs = result.debug.get("compressed_messages", convo["messages"])
    pruned_ctx      = result.debug.get("pruned_context", convo["prior_context"])
    output_tokens   = estimate_tokens_many(compressed_msgs) + estimate_tokens_many(pruned_ctx)

    assert output_tokens < result.baseline_tokens, (
        f"Expected output_tokens ({output_tokens}) < baseline ({result.baseline_tokens})"
    )


@pytest.mark.parametrize("convo", AGENT_CONVERSATIONS)
def test_savings_above_minimum(pipeline, convo):
    """Savings percentage must be at least 15%."""
    result = pipeline.process_task(
        task_text=convo["task"],
        incoming_messages=convo["messages"],
        prior_context=convo["prior_context"],
        compression_level=2,
        prune_budget=4,
    )
    compressed_msgs = result.debug.get("compressed_messages", convo["messages"])
    pruned_ctx      = result.debug.get("pruned_context", convo["prior_context"])
    output_tokens   = estimate_tokens_many(compressed_msgs) + estimate_tokens_many(pruned_ctx)
    savings = (1 - output_tokens / max(1, result.baseline_tokens)) * 100

    assert savings >= 15.0, (
        f"Expected at least 15% savings, got {savings:.1f}% "
        f"(baseline={result.baseline_tokens}, output={output_tokens})"
    )


@pytest.mark.parametrize("convo", AGENT_CONVERSATIONS)
def test_quality_proxy_above_floor(pipeline, convo):
    """Quality proxy must stay above 0.75 (conservative floor for tests)."""
    result = pipeline.process_task(
        task_text=convo["task"],
        incoming_messages=convo["messages"],
        prior_context=convo["prior_context"],
        compression_level=2,
        prune_budget=4,
    )
    assert result.quality_proxy >= 0.75, (
        f"Quality proxy {result.quality_proxy:.4f} below floor 0.75"
    )


def test_aggressive_compression_still_valid(pipeline):
    """Level-3 compression should still produce valid (non-empty) output."""
    convo = AGENT_CONVERSATIONS[0]
    result = pipeline.process_task(
        task_text=convo["task"],
        incoming_messages=convo["messages"],
        prior_context=convo["prior_context"],
        compression_level=3,
        prune_budget=2,
    )
    compressed_msgs = result.debug.get("compressed_messages", [])
    assert len(compressed_msgs) > 0, "Aggressive compression should not produce empty output"


def test_tiktoken_vs_heuristic_alignment():
    """tiktoken counts should be reasonably close to the word-count heuristic."""
    sample = "The quick brown fox jumps over the lazy dog. This is a test sentence."
    tok_count = estimate_tokens(sample)
    word_heuristic = max(1, int(len(sample.split()) * 1.3))
    # Allow ±50% deviation — they use different methods but should be in the same ballpark
    assert abs(tok_count - word_heuristic) / max(1, word_heuristic) < 0.5, (
        f"tiktoken ({tok_count}) and heuristic ({word_heuristic}) diverged too much"
    )


def test_empty_inputs_safe(pipeline):
    """Empty messages/context should not raise."""
    result = pipeline.process_task(
        task_text="",
        incoming_messages=[],
        prior_context=[],
    )
    assert result is not None
    assert result.baseline_tokens == 0 or result.baseline_tokens >= 0


def test_routing_emitted(pipeline):
    """Progress callback should receive a 'routed' event."""
    events = []
    convo = AGENT_CONVERSATIONS[0]
    pipeline.process_task(
        task_text=convo["task"],
        incoming_messages=convo["messages"],
        prior_context=convo["prior_context"],
        progress_callback=lambda stage, data: events.append(stage),
    )
    assert "routed" in events, f"Expected 'routed' in events, got: {events}"
    assert "compressed" in events, f"Expected 'compressed' in events, got: {events}"
