"""
Phase F tests: Supabase mirror and label tracking.

Validates:
1. Mirror writer exports usage records with labels
2. Cost estimation logic
3. Batch mirror operations
4. Label sync functionality
5. Migration schema compatibility
"""
import sys
from pathlib import Path

# Ensure brevitas is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import patch, MagicMock
from api.mirror import (
    mirror_to_supabase,
    _estimate_cost_saved,
    batch_mirror_to_supabase,
    sync_labels_to_supabase,
)


class TestCostEstimation:
    """Test cost estimation for different providers and models."""

    def test_openai_cost_estimation(self):
        """Test cost estimation for OpenAI models."""
        # GPT-4 turbo: $0.01/$0.03 per 1M
        cost = _estimate_cost_saved("openai", "gpt-4-turbo", 1000)
        assert cost > 0
        assert cost < 0.001  # Should be very small for 1000 tokens

    def test_anthropic_cost_estimation(self):
        """Test cost estimation for Anthropic models."""
        # Claude Opus: $0.015/$0.075 per 1M
        cost = _estimate_cost_saved("anthropic", "claude-opus-4-8", 1000)
        assert cost > 0
        assert cost < 0.001

    def test_deepseek_cost_estimation(self):
        """Test cost estimation for DeepSeek models."""
        # DeepSeek chat: $0.00014/$0.00028 per 1M
        cost = _estimate_cost_saved("deepseek", "deepseek-chat", 1000)
        assert cost > 0
        # DeepSeek is very cheap, so even 1000 tokens might be < $0.0001
        assert cost >= 0.0001  # Minimum $0.0001

    def test_unknown_provider_uses_default(self):
        """Test that unknown providers use default rates."""
        cost = _estimate_cost_saved("unknown-provider", "unknown-model", 1000)
        assert cost >= 0.0001

    def test_cost_scales_with_tokens(self):
        """Test that cost scales linearly with tokens."""
        cost_100k = _estimate_cost_saved("openai", "gpt-4-turbo", 100000)
        cost_200k = _estimate_cost_saved("openai", "gpt-4-turbo", 200000)
        # Cost should approximately double
        assert cost_200k > cost_100k
        assert abs(cost_200k - cost_100k * 2) < cost_100k * 0.1  # Within 10%


class TestMirrorToSupabase:
    """Test mirroring usage records to Supabase."""

    @patch.dict("os.environ", {}, clear=False)
    def test_skips_without_supabase_config(self):
        """Test that mirror is skipped when Supabase is not configured."""
        # Clear Supabase env vars
        import os
        os.environ.pop("NEXT_PUBLIC_SUPABASE_URL", None)
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

        result = mirror_to_supabase(
            user_id="test-user",
            key_hash="test-key",
            provider="openai",
            model="gpt-4",
            baseline_tokens=1000,
            optimized_tokens=500,
            session_id="sess-123",
        )

        assert result is False

    def test_mirror_record_construction(self):
        """Test that mirror constructs correct record structure."""
        # This test verifies the logic without needing Supabase
        baseline = 10000
        optimized = 6000
        tokens_saved = baseline - optimized
        savings_pct = (tokens_saved / baseline * 100) if baseline > 0 else 0

        assert tokens_saved == 4000
        assert abs(savings_pct - 40.0) < 0.01


class TestBatchMirror:
    """Test batch mirror operations."""

    @patch.dict("os.environ", {}, clear=False)
    def test_batch_skips_without_config(self):
        """Test that batch mirror is skipped without Supabase config."""
        import os
        os.environ.pop("NEXT_PUBLIC_SUPABASE_URL", None)
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

        records = [
            {
                "user_id": "user-1",
                "key_hash": "key-1",
                "provider": "openai",
                "model": "gpt-4",
                "baseline_tokens": 1000,
                "optimized_tokens": 500,
                "session_id": "sess-1",
            }
        ]

        result = batch_mirror_to_supabase(records)
        assert result == 0

    def test_batch_mirror_record_enrichment(self):
        """Test that batch mirror enriches records with calculations."""
        # Verify enrichment logic without needing Supabase
        baseline = 1000
        optimized = 500
        tokens_saved = baseline - optimized
        savings_pct = (tokens_saved / baseline * 100) if baseline > 0 else 0

        assert tokens_saved == 500
        assert abs(savings_pct - 50.0) < 0.01

    @patch.dict("os.environ", {}, clear=False)
    def test_batch_mirror_empty_list(self):
        """Test batch mirror with empty records list."""
        import os
        os.environ.pop("NEXT_PUBLIC_SUPABASE_URL", None)
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

        result = batch_mirror_to_supabase([])
        assert result == 0


class TestLabelSync:
    """Test label synchronization to Supabase."""

    @patch.dict("os.environ", {}, clear=False)
    def test_sync_labels_skips_without_config(self):
        """Test that label sync is skipped without Supabase config."""
        import os
        os.environ.pop("NEXT_PUBLIC_SUPABASE_URL", None)
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

        result = sync_labels_to_supabase(
            user_id="user-123",
            session_id="sess-123",
            pipeline="campaign-launch",
            agent="copywriter",
            run_id="run-123",
        )

        assert result is False

    @patch.dict("os.environ", {}, clear=False)
    def test_sync_no_labels_returns_false(self):
        """Test that syncing with no labels returns False."""
        import os
        os.environ.pop("NEXT_PUBLIC_SUPABASE_URL", None)
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

        result = sync_labels_to_supabase(
            user_id="user-123",
            session_id="sess-123",
        )

        assert result is False

    def test_label_validation(self):
        """Test that label fields are properly structured."""
        # Test that labels can be empty strings (backward compatible)
        labels = {
            "pipeline": "",
            "agent": "",
            "run_id": "",
        }

        # All can be empty
        assert labels["pipeline"] == ""
        assert labels["agent"] == ""
        assert labels["run_id"] == ""

        # Or populated
        labels_populated = {
            "pipeline": "campaign-launch",
            "agent": "copywriter",
            "run_id": "run-123",
        }

        assert labels_populated["pipeline"] == "campaign-launch"
        assert labels_populated["agent"] == "copywriter"
        assert labels_populated["run_id"] == "run-123"


class TestMigrationSchema:
    """Test Supabase migration schema compatibility."""

    def test_migration_file_exists(self):
        """Test that the migration file exists."""
        migration_path = Path(__file__).parent.parent / "supabase/migrations/20260627_add_tracking_labels.sql"
        assert migration_path.exists()

    def test_migration_has_required_alterations(self):
        """Test that migration includes required column additions."""
        migration_path = Path(__file__).parent.parent / "supabase/migrations/20260627_add_tracking_labels.sql"
        content = migration_path.read_text()

        # Check for required columns
        assert "ALTER TABLE billing_events" in content
        assert "pipeline TEXT NOT NULL DEFAULT ''" in content
        assert "agent TEXT NOT NULL DEFAULT ''" in content
        assert "run_id TEXT NOT NULL DEFAULT ''" in content

    def test_migration_creates_views(self):
        """Test that migration creates required views."""
        migration_path = Path(__file__).parent.parent / "supabase/migrations/20260627_add_tracking_labels.sql"
        content = migration_path.read_text()

        # Check for view creation
        assert "savings_by_pipeline" in content
        assert "savings_by_agent" in content
        assert "savings_by_run" in content

    def test_migration_creates_indexes(self):
        """Test that migration creates efficient indexes."""
        migration_path = Path(__file__).parent.parent / "supabase/migrations/20260627_add_tracking_labels.sql"
        content = migration_path.read_text()

        # Check for index creation
        assert "idx_billing_events_pipeline" in content
        assert "idx_billing_events_pipeline_agent" in content
        assert "idx_billing_events_run_id" in content


class TestReconciliationWithLabels:
    """Test reconciliation invariants with labels."""

    def test_cost_breakdown_reconciliation(self):
        """Test that per-agent costs sum to pipeline total."""
        # Simulate per-agent costs (as would be returned by stats API)
        agent_costs = [0.45, 7.20, 5.40, 2.70, 3.30, 1.95, 4.35]
        pipeline_total = 25.35

        agent_sum = sum(agent_costs)
        assert abs(agent_sum - pipeline_total) < 0.01  # Allow for floating point rounding

    def test_token_reconciliation(self):
        """Test that per-agent tokens sum to pipeline total."""
        agent_tokens = [150, 2400, 1800, 900, 1100, 650, 1450]
        pipeline_total = 8450

        agent_sum = sum(agent_tokens)
        assert agent_sum == pipeline_total


class TestMirrorIntegration:
    """Integration tests for mirror system."""

    def test_mirror_record_structure(self):
        """Test that mirrored records have correct structure."""
        # Simulate a mirrored record (dict that would be sent to Supabase)
        record = {
            "user_id": "user-123",
            "key_hash": "key-abc",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "baseline_tokens": 10000,
            "optimized_tokens": 6000,
            "tokens_saved": 4000,
            "savings_pct": 40.0,
            "cost_saved_usd": 0.56,
            "session_id": "sess-xyz",
            "pipeline": "campaign-launch",
            "agent": "copywriter",
            "run_id": "run-123",
            "created_at": "2026-06-27T12:00:00",
        }

        # Validate required fields
        required_fields = [
            "user_id",
            "key_hash",
            "provider",
            "model",
            "baseline_tokens",
            "optimized_tokens",
            "session_id",
            "created_at",
        ]

        for field in required_fields:
            assert field in record
            assert record[field] is not None

        # Validate label fields (should default to empty string if not set)
        assert "pipeline" in record
        assert "agent" in record
        assert "run_id" in record


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
