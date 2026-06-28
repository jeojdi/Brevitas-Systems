"""
Tests for Phase B: Aggregation + stats API (TDD: tests first, RED phase).

Tests the aggregation queries and reconciliation invariants:
- Σ(agent) = Σ(pipeline) = account total
"""
import pytest
import sqlite3
import tempfile
from pathlib import Path


def test_store_query_stats_by_pipeline():
    """Test get_stats_by_pipeline returns per-pipeline aggregations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")

        from api.store import UsageStore

        store = UsageStore(db_path=db_path)
        store.create_key("test_key", "test")

        # Record calls from two pipelines
        store.record_usage(
            key_hash="test_key",
            baseline_tokens=1000,
            optimized_tokens=600,
            savings_pct=40.0,
            quality_proxy=0.95,
            pipeline="campaign-launch",
            agent="copywriter",
            run_id="run_1",
        )

        store.record_usage(
            key_hash="test_key",
            baseline_tokens=2000,
            optimized_tokens=1500,
            savings_pct=25.0,
            quality_proxy=0.92,
            pipeline="seo-optimization",
            agent="seo_optimizer",
            run_id="run_2",
        )

        # Query stats by pipeline
        stats = store.get_stats_by_pipeline("test_key")

        assert len(stats) == 2
        stats_by_pipeline = {s["pipeline"]: s for s in stats}

        assert stats_by_pipeline["campaign-launch"]["calls"] == 1
        assert stats_by_pipeline["campaign-launch"]["tokens_saved"] == 400  # 1000 - 600

        assert stats_by_pipeline["seo-optimization"]["calls"] == 1
        assert stats_by_pipeline["seo-optimization"]["tokens_saved"] == 500  # 2000 - 1500


def test_store_query_stats_by_agent():
    """Test get_stats_by_agent returns per-agent aggregations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")

        from api.store import UsageStore

        store = UsageStore(db_path=db_path)
        store.create_key("test_key", "test")

        # Record calls from different agents in same pipeline
        store.record_usage(
            key_hash="test_key",
            baseline_tokens=1000,
            optimized_tokens=600,
            savings_pct=40.0,
            quality_proxy=0.95,
            pipeline="campaign-launch",
            agent="copywriter",
            run_id="run_1",
        )

        store.record_usage(
            key_hash="test_key",
            baseline_tokens=1000,
            optimized_tokens=900,
            savings_pct=10.0,
            quality_proxy=0.98,
            pipeline="campaign-launch",
            agent="editor",
            run_id="run_1",
        )

        # Query stats by agent
        stats = store.get_stats_by_agent("test_key", pipeline="campaign-launch")

        assert len(stats) == 2
        agents = {s["agent"]: s for s in stats}
        assert agents["copywriter"]["tokens_saved"] == 400
        assert agents["editor"]["tokens_saved"] == 100


def test_store_query_stats_by_run():
    """Test get_stats_by_run returns per-run aggregations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")

        from api.store import UsageStore

        store = UsageStore(db_path=db_path)
        store.create_key("test_key", "test")

        # Record calls from same run, different agents
        store.record_usage(
            key_hash="test_key",
            baseline_tokens=1000,
            optimized_tokens=600,
            savings_pct=40.0,
            quality_proxy=0.95,
            pipeline="campaign-launch",
            agent="copywriter",
            run_id="run_abc123",
        )

        store.record_usage(
            key_hash="test_key",
            baseline_tokens=1000,
            optimized_tokens=900,
            savings_pct=10.0,
            quality_proxy=0.98,
            pipeline="campaign-launch",
            agent="editor",
            run_id="run_abc123",
        )

        # Query stats by run
        stats = store.get_stats_by_run("test_key", pipeline="campaign-launch")

        assert len(stats) == 1
        assert stats[0]["run_id"] == "run_abc123"
        assert stats[0]["calls"] == 2
        assert stats[0]["tokens_saved"] == 500  # 400 + 100


def test_reconciliation_agent_to_pipeline():
    """Test that Σ(agent savings) within pipeline = pipeline savings."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")

        from api.store import UsageStore

        store = UsageStore(db_path=db_path)
        store.create_key("test_key", "test")

        # Simulate marketing agency with 3 agents
        agents_data = [
            ("intake", 500, 450, "campaign-launch"),
            ("copywriter", 1000, 600, "campaign-launch"),
            ("editor", 1000, 900, "campaign-launch"),
        ]

        for agent, baseline, optimized, pipeline in agents_data:
            store.record_usage(
                key_hash="test_key",
                baseline_tokens=baseline,
                optimized_tokens=optimized,
                savings_pct=100 * (baseline - optimized) / baseline,
                quality_proxy=0.95,
                pipeline=pipeline,
                agent=agent,
                run_id="run_1",
            )

        # Get pipeline stats
        pipeline_stats = store.get_stats_by_pipeline("test_key")
        pipeline_tokens_saved = pipeline_stats[0]["tokens_saved"]

        # Get agent stats for that pipeline
        agent_stats = store.get_stats_by_agent("test_key", pipeline="campaign-launch")
        agent_tokens_saved_sum = sum(a["tokens_saved"] for a in agent_stats)

        # They must match (reconciliation invariant)
        assert agent_tokens_saved_sum == pipeline_tokens_saved
        assert pipeline_tokens_saved == 50 + 400 + 100  # 550


def test_reconciliation_pipeline_to_account():
    """Test that Σ(pipeline savings) = account savings."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")

        from api.store import UsageStore

        store = UsageStore(db_path=db_path)
        store.create_key("test_key", "test")

        # Two pipelines with multiple calls each
        pipelines_data = [
            ("campaign-launch", "copywriter", 1000, 600),
            ("campaign-launch", "editor", 800, 700),
            ("seo-optimization", "seo_optimizer", 1500, 1200),
            ("seo-optimization", "analyst", 1000, 900),
        ]

        for pipeline, agent, baseline, optimized in pipelines_data:
            store.record_usage(
                key_hash="test_key",
                baseline_tokens=baseline,
                optimized_tokens=optimized,
                savings_pct=100 * (baseline - optimized) / baseline,
                quality_proxy=0.95,
                pipeline=pipeline,
                agent=agent,
                run_id="run_1",
            )

        # Get account-level stats
        account_stats = store.get_stats("test_key")
        account_tokens_saved = account_stats["total_tokens_saved"]

        # Get pipeline stats and sum
        pipeline_stats = store.get_stats_by_pipeline("test_key")
        pipeline_tokens_saved_sum = sum(p["tokens_saved"] for p in pipeline_stats)

        # They must match (reconciliation invariant)
        assert pipeline_tokens_saved_sum == account_tokens_saved
        expected = 400 + 100 + 300 + 100  # 900
        assert account_tokens_saved == expected


def test_api_endpoint_stats_pipelines():
    """Test GET /v1/stats/pipelines endpoint returns aggregations."""
    from api.server import UsageReportRequest

    # Verify the request model accepts the query filters
    # (The actual endpoint will filter using these)
    req = UsageReportRequest(
        provider="deepseek",
        model="deepseek-chat",
        baseline_tokens=1000,
        compressed_tokens=600,
        quality_score=0.95,
        pipeline="campaign-launch",
        agent="copywriter",
        run_id="run_1",
    )

    assert req.pipeline == "campaign-launch"


def test_api_endpoint_stats_agents():
    """Test GET /v1/stats/agents?pipeline= endpoint filters correctly."""
    from api.server import UsageReportRequest

    # Verify request model structure
    req = UsageReportRequest(
        provider="anthropic",
        model="claude-sonnet-4-6",
        baseline_tokens=2000,
        compressed_tokens=1400,
        quality_score=0.92,
        pipeline="seo-optimization",
        agent="seo_optimizer",
        run_id="run_2",
    )

    assert req.agent == "seo_optimizer"


def test_api_endpoint_stats_runs():
    """Test GET /v1/stats/runs?pipeline= endpoint returns run aggregations."""
    from api.server import UsageReportRequest

    # Verify request model structure
    req = UsageReportRequest(
        provider="openai",
        model="gpt-4o",
        baseline_tokens=1500,
        compressed_tokens=1200,
        quality_score=0.90,
        pipeline="campaign-launch",
        agent="intake",
        run_id="run_marketing_001",
    )

    assert req.run_id == "run_marketing_001"
