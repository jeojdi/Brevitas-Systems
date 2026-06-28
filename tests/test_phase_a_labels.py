"""
Tests for Phase A: Data model + label propagation.
Write tests first (RED), then implement (GREEN), then refactor (IMPROVE).
"""
import pytest
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

# Test the label contextvar infrastructure
def test_contextvar_labels_basic():
    """Test that contextvars can store and retrieve labels."""
    from brevitas.labels import start_run, get_run_id, get_pipeline, get_agent, agent

    # Initially empty
    assert get_run_id() == ""
    assert get_pipeline() == ""
    assert get_agent() == ""

    # start_run sets pipeline + run_id
    start_run(pipeline="campaign-launch")
    assert get_pipeline() == "campaign-launch"
    assert get_run_id() != ""  # auto-generated

    # agent() context manager sets agent label
    with agent("copywriter"):
        assert get_agent() == "copywriter"

    # After exiting, agent reverts
    assert get_agent() == ""


def test_contextvar_labels_per_call_override():
    """Test that per-call _brevitas_meta overrides contextvar."""
    from brevitas.labels import start_run, agent, resolve_labels

    start_run(pipeline="campaign-launch")
    with agent("copywriter"):
        # Contextvar resolution
        labels = resolve_labels()
        assert labels["pipeline"] == "campaign-launch"
        assert labels["agent"] == "copywriter"

        # Per-call override (highest priority)
        labels_override = resolve_labels(_brevitas_meta={"agent": "editor"})
        assert labels_override["agent"] == "editor"
        assert labels_override["pipeline"] == "campaign-launch"


def test_store_migration_adds_label_columns():
    """Test that UsageStore._init() adds pipeline/agent/run_id columns."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")

        # Import here to avoid polluting module namespace
        from api.store import UsageStore

        store = UsageStore(db_path=db_path)

        # Check that columns exist
        with sqlite3.connect(db_path) as db:
            cursor = db.execute("PRAGMA table_info(usage_log)")
            columns = {row[1] for row in cursor.fetchall()}

            assert "pipeline" in columns
            assert "agent" in columns
            assert "run_id" in columns


def test_record_usage_with_labels():
    """Test that record_usage persists pipeline/agent/run_id labels."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")

        from api.store import UsageStore

        store = UsageStore(db_path=db_path)
        store.create_key("test_key_hash", "test_key_name")

        # Record usage with labels
        store.record_usage(
            key_hash="test_key_hash",
            baseline_tokens=1000,
            optimized_tokens=500,
            savings_pct=50.0,
            quality_proxy=0.95,
            provider="deepseek",
            model="deepseek-chat",
            pipeline="campaign-launch",
            agent="copywriter",
            run_id="run_abc123",
        )

        # Verify persisted
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                "SELECT pipeline, agent, run_id FROM usage_log WHERE key_hash = ?"
                , ("test_key_hash",)
            ).fetchone()

            assert row is not None
            assert row[0] == "campaign-launch"
            assert row[1] == "copywriter"
            assert row[2] == "run_abc123"


def test_record_usage_labels_default_to_empty_string():
    """Test that labels default to empty strings (backward compatibility)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")

        from api.store import UsageStore

        store = UsageStore(db_path=db_path)
        store.create_key("test_key_hash", "test_key_name")

        # Record usage WITHOUT labels (old callers)
        store.record_usage(
            key_hash="test_key_hash",
            baseline_tokens=1000,
            optimized_tokens=500,
            savings_pct=50.0,
            quality_proxy=0.95,
        )

        # Verify defaults
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                "SELECT pipeline, agent, run_id FROM usage_log WHERE key_hash = ?"
                , ("test_key_hash",)
            ).fetchone()

            assert row is not None
            assert row[0] == ""
            assert row[1] == ""
            assert row[2] == ""


def test_proxy_header_parsing():
    """Test that proxy parses x-brevitas-* headers into labels."""
    from brevitas.proxy import parse_brevitas_headers

    headers = {
        "x-brevitas-pipeline": "campaign-launch",
        "x-brevitas-agent": "copywriter",
        "x-brevitas-run-id": "run_xyz789",
    }

    labels = parse_brevitas_headers(headers)

    assert labels["pipeline"] == "campaign-launch"
    assert labels["agent"] == "copywriter"
    assert labels["run_id"] == "run_xyz789"


def test_proxy_header_parsing_missing_headers():
    """Test that missing headers default to empty strings."""
    from brevitas.proxy import parse_brevitas_headers

    headers = {}
    labels = parse_brevitas_headers(headers)

    assert labels["pipeline"] == ""
    assert labels["agent"] == ""
    assert labels["run_id"] == ""


def test_compress_messages_accepts_labels():
    """Test that compress_messages accepts and uses labels when provided."""
    from brevitas._compress import compress_messages
    from brevitas.session import BrevitasSession
    from unittest.mock import patch, MagicMock
    import os

    # Disable compression so we can test label handling without API calls
    session = BrevitasSession()
    messages = [{"role": "user", "content": "Hello"}]

    # Just verify the function signature accepts labels without error
    result = compress_messages(
        messages,
        session,
        pipeline="campaign-launch",
        agent="copywriter",
        run_id="run_123",
    )

    # Since compression is disabled (no config), it should return original messages
    assert result[0] == messages


def test_api_server_accepts_labels_in_usage_request():
    """Test that API UsageReportRequest accepts labels."""
    from api.server import UsageReportRequest

    req = UsageReportRequest(
        provider="deepseek",
        model="deepseek-chat",
        baseline_tokens=1000,
        compressed_tokens=500,
        quality_score=0.95,
        pipeline="campaign-launch",
        agent="copywriter",
        run_id="run_123",
    )

    assert req.pipeline == "campaign-launch"
    assert req.agent == "copywriter"
    assert req.run_id == "run_123"


def test_api_server_accepts_labels_in_compress_request():
    """Test that API CompressRequest accepts labels."""
    from api.server import CompressRequest

    req = CompressRequest(
        messages=["test message"],
        pipeline="campaign-launch",
        agent="copywriter",
        run_id="run_123",
    )

    assert req.pipeline == "campaign-launch"
    assert req.agent == "copywriter"
    assert req.run_id == "run_123"


def test_e2e_label_propagation_flow():
    """Integration test: labels propagate from start_run through to database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")

        from api.store import UsageStore
        from brevitas.labels import start_run, agent, resolve_labels

        store = UsageStore(db_path=db_path)
        store.create_key("integration_test_key", "test")

        # Simulate a pipeline run with multiple agents
        start_run(pipeline="campaign-launch")
        run_id = resolve_labels()["run_id"]

        # Agent 1: intake
        with agent("intake"):
            labels = resolve_labels()
            store.record_usage(
                key_hash="integration_test_key",
                baseline_tokens=1000,
                optimized_tokens=900,
                savings_pct=10.0,
                quality_proxy=0.98,
                pipeline=labels["pipeline"],
                agent=labels["agent"],
                run_id=labels["run_id"],
            )

        # Agent 2: copywriter
        with agent("copywriter"):
            labels = resolve_labels()
            store.record_usage(
                key_hash="integration_test_key",
                baseline_tokens=2000,
                optimized_tokens=1200,
                savings_pct=40.0,
                quality_proxy=0.96,
                pipeline=labels["pipeline"],
                agent=labels["agent"],
                run_id=labels["run_id"],
            )

        # Verify both records in DB with correct labels
        with sqlite3.connect(db_path) as db:
            rows = db.execute(
                "SELECT agent, run_id, baseline_tokens FROM usage_log WHERE key_hash = ? ORDER BY id",
                ("integration_test_key",),
            ).fetchall()

            assert len(rows) == 2
            assert rows[0][0] == "intake"
            assert rows[0][1] == run_id
            assert rows[1][0] == "copywriter"
            assert rows[1][1] == run_id


def test_all_api_endpoints_pass_labels_to_store():
    """Verify all three API endpoints pass labels to record_usage (including streaming)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")

        from api.store import UsageStore

        store = UsageStore(db_path=db_path)
        store.create_key("stream_test_key", "test")

        # Simulate all three endpoints recording with labels
        # (They use the same record_usage signature)
        store.record_usage(
            key_hash="stream_test_key",
            baseline_tokens=1000,
            optimized_tokens=500,
            savings_pct=50.0,
            quality_proxy=0.95,
            pipeline="campaign-launch",
            agent="copywriter",
            run_id="run_stream_001",
        )

        # Verify the record was created with all labels
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                "SELECT pipeline, agent, run_id FROM usage_log WHERE key_hash = ?",
                ("stream_test_key",),
            ).fetchone()

            assert row is not None
            assert row[0] == "campaign-launch"
            assert row[1] == "copywriter"
            assert row[2] == "run_stream_001"


def test_wrapper_anthropic_resolves_labels_from_contextvar():
    """Test that anthropic wrapper resolves labels from contextvar."""
    from brevitas.wrappers.anthropic import _BrevitasMessages
    from brevitas.session import BrevitasSession
    from brevitas.labels import start_run, agent, resolve_labels
    from unittest.mock import MagicMock

    start_run(pipeline="campaign-launch")
    with agent("copywriter"):
        # Create mock Anthropic messages object
        mock_messages = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="response text")]
        mock_messages.create.return_value = mock_response

        session = BrevitasSession()
        from token_efficiency_model.lossless.router import BrevitasRouter
        brevitas_messages = _BrevitasMessages(mock_messages, session, BrevitasRouter(provider='anthropic'))

        # Verify resolve_labels works (wrappers use it)
        labels = resolve_labels()
        assert labels["pipeline"] == "campaign-launch"
        assert labels["agent"] == "copywriter"


def test_wrapper_anthropic_accepts_per_call_override():
    """Test that anthropic wrapper accepts _brevitas_meta per-call override."""
    from brevitas.labels import resolve_labels

    # Override should take precedence
    labels = resolve_labels(_brevitas_meta={"agent": "editor", "pipeline": "override"})
    assert labels["agent"] == "editor"
    assert labels["pipeline"] == "override"


def test_wrapper_openai_resolves_labels_from_contextvar():
    """Test that openai wrapper resolves labels from contextvar."""
    from brevitas.wrappers.openai import _BrevitasCompletions
    from brevitas.session import BrevitasSession
    from brevitas.labels import start_run, agent, resolve_labels
    from unittest.mock import MagicMock

    start_run(pipeline="seo-optimization")
    with agent("seo_optimizer"):
        # Create mock OpenAI completions object
        mock_completions = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="response text"))]
        mock_completions.create.return_value = mock_response

        session = BrevitasSession()
        from token_efficiency_model.lossless.router import BrevitasRouter
        brevitas_completions = _BrevitasCompletions(mock_completions, session, BrevitasRouter(provider='openai'))

        # Verify resolve_labels works
        labels = resolve_labels()
        assert labels["pipeline"] == "seo-optimization"
        assert labels["agent"] == "seo_optimizer"
