"""Test suite for ModeRequestHandler."""

import pytest
from unittest.mock import Mock, patch

from .request_handler import ModeRequestHandler, create_default_handler
from .tiered_orchestrator import BrevitasMode, TieredModeOrchestrator


@pytest.fixture
def handler():
    """Create a ModeRequestHandler with mock orchestrator."""
    orch = Mock(spec=TieredModeOrchestrator)
    orch.process = Mock(return_value=Mock(
        mode=BrevitasMode.LOSSLESS,
        optimized_context=["ctx"],
        optimized_messages=["msg"],
        quality_assessment=None,
        fallback_applied=False,
        metadata={"mode": "lossless"},
    ))
    return ModeRequestHandler(orchestrator=orch)


@pytest.fixture
def sample_request():
    """Sample request data."""
    return {
        "task_text": "What is 2+2?",
        "incoming_messages": ["Question: What is 2+2?"],
        "prior_context": ["Math: addition of two positive integers."],
    }


class TestModeSelection:
    """Test mode selection from headers/body/defaults."""

    def test_default_mode_is_lossless(self, handler):
        """Default mode should be lossless."""
        mode = handler.extract_mode_from_request()
        assert mode == BrevitasMode.LOSSLESS

    def test_mode_from_header(self, handler):
        """Mode can be selected via x-brevitas-mode header."""
        headers = {"x-brevitas-mode": "balanced"}
        mode = handler.extract_mode_from_request(request_headers=headers)
        assert mode == BrevitasMode.BALANCED

    def test_mode_from_header_case_insensitive(self, handler):
        """Header mode is case-insensitive."""
        headers = {"x-brevitas-mode": "MAX_SAVINGS"}
        mode = handler.extract_mode_from_request(request_headers=headers)
        assert mode == BrevitasMode.MAX_SAVINGS

    def test_mode_from_request_body(self, handler):
        """Mode can be selected via mode field in request body."""
        body = {"mode": "max_savings"}
        mode = handler.extract_mode_from_request(request_body=body)
        assert mode == BrevitasMode.MAX_SAVINGS

    def test_header_precedence_over_body(self, handler):
        """Header takes precedence over request body."""
        headers = {"x-brevitas-mode": "balanced"}
        body = {"mode": "max_savings"}
        mode = handler.extract_mode_from_request(
            request_headers=headers,
            request_body=body,
        )
        assert mode == BrevitasMode.BALANCED

    def test_customer_default_mode(self, handler):
        """Customer default is used if no header/body."""
        handler.set_customer_default("cust-123", BrevitasMode.BALANCED)
        mode = handler.extract_mode_from_request(customer_id="cust-123")
        assert mode == BrevitasMode.BALANCED

    def test_header_overrides_customer_default(self, handler):
        """Header takes precedence over customer default."""
        handler.set_customer_default("cust-123", BrevitasMode.BALANCED)
        headers = {"x-brevitas-mode": "lossless"}
        mode = handler.extract_mode_from_request(
            request_headers=headers,
            customer_id="cust-123",
        )
        assert mode == BrevitasMode.LOSSLESS

    def test_invalid_mode_string_falls_back_to_default(self, handler):
        """Invalid mode string falls back to default."""
        headers = {"x-brevitas-mode": "invalid_mode"}
        mode = handler.extract_mode_from_request(request_headers=headers)
        assert mode == BrevitasMode.LOSSLESS

    def test_empty_mode_string_ignored(self, handler):
        """Empty mode string is ignored (doesn't override default)."""
        headers = {"x-brevitas-mode": ""}
        mode = handler.extract_mode_from_request(request_headers=headers)
        assert mode == BrevitasMode.LOSSLESS


class TestCustomerDefaults:
    """Test per-customer default mode management."""

    def test_set_get_customer_default(self, handler):
        """Can set and get customer default mode."""
        handler.set_customer_default("cust-456", BrevitasMode.MAX_SAVINGS)
        mode = handler.get_customer_default("cust-456")
        assert mode == BrevitasMode.MAX_SAVINGS

    def test_get_nonexistent_customer_default_returns_none(self, handler):
        """Getting nonexistent customer default returns None."""
        mode = handler.get_customer_default("unknown-customer")
        assert mode is None

    def test_multiple_customer_defaults(self, handler):
        """Multiple customers can have different defaults."""
        handler.set_customer_default("cust-1", BrevitasMode.LOSSLESS)
        handler.set_customer_default("cust-2", BrevitasMode.BALANCED)
        handler.set_customer_default("cust-3", BrevitasMode.MAX_SAVINGS)

        assert handler.get_customer_default("cust-1") == BrevitasMode.LOSSLESS
        assert handler.get_customer_default("cust-2") == BrevitasMode.BALANCED
        assert handler.get_customer_default("cust-3") == BrevitasMode.MAX_SAVINGS


class TestModeConfiguration:
    """Test mode-specific configuration."""

    def test_lossless_config_defaults(self, handler):
        """Lossless mode has appropriate defaults."""
        config = handler._build_mode_config(BrevitasMode.LOSSLESS)
        assert config.mode == BrevitasMode.LOSSLESS
        assert config.compression_level == 1
        assert config.apply_quality_gate is False
        assert config.enable_rlm_retrieval is True

    def test_balanced_config_defaults(self, handler):
        """Balanced mode has appropriate defaults."""
        config = handler._build_mode_config(BrevitasMode.BALANCED)
        assert config.mode == BrevitasMode.BALANCED
        assert config.compression_level == 1  # light
        assert config.apply_quality_gate is True
        assert config.fallback_to_full_on_gate_fail is True

    def test_max_savings_config_defaults(self, handler):
        """Max_savings mode has appropriate defaults."""
        config = handler._build_mode_config(BrevitasMode.MAX_SAVINGS)
        assert config.mode == BrevitasMode.MAX_SAVINGS
        assert config.compression_level == 3  # aggressive
        assert config.apply_quality_gate is True
        assert config.fallback_to_full_on_gate_fail is True

    def test_override_compression_level(self, handler):
        """Config overrides from request body work."""
        body = {"compression_level": 2}
        config = handler._build_mode_config(BrevitasMode.MAX_SAVINGS, body)
        assert config.compression_level == 2

    def test_override_prune_budget(self, handler):
        """Prune budget can be overridden."""
        body = {"prune_budget": 10}
        config = handler._build_mode_config(BrevitasMode.MAX_SAVINGS, body)
        assert config.prune_budget == 10

    def test_override_quality_floor(self, handler):
        """Quality floor can be overridden."""
        body = {"quality_floor": 0.85}
        config = handler._build_mode_config(BrevitasMode.BALANCED, body)
        assert config.quality_floor == 0.85

    def test_override_enable_rlm(self, handler):
        """RLM enablement can be overridden."""
        body = {"enable_rlm_retrieval": False}
        config = handler._build_mode_config(BrevitasMode.LOSSLESS, body)
        assert config.enable_rlm_retrieval is False


class TestProcessRequest:
    """Test end-to-end request processing."""

    def test_process_request_default_mode(self, handler, sample_request):
        """Process request uses default lossless mode."""
        result = handler.process_request(**sample_request)

        # Orchestrator should have been called
        handler.orchestrator.process.assert_called_once()
        call_kwargs = handler.orchestrator.process.call_args[1]
        assert call_kwargs["config"].mode == BrevitasMode.LOSSLESS

    def test_process_request_with_header_mode(self, handler, sample_request):
        """Process request respects header mode."""
        headers = {"x-brevitas-mode": "max_savings"}
        result = handler.process_request(**sample_request, request_headers=headers)

        call_kwargs = handler.orchestrator.process.call_args[1]
        assert call_kwargs["config"].mode == BrevitasMode.MAX_SAVINGS

    def test_process_request_with_customer_default(self, handler, sample_request):
        """Process request respects customer default."""
        handler.set_customer_default("cust-xyz", BrevitasMode.BALANCED)
        result = handler.process_request(**sample_request, customer_id="cust-xyz")

        call_kwargs = handler.orchestrator.process.call_args[1]
        assert call_kwargs["config"].mode == BrevitasMode.BALANCED

    def test_process_request_passes_all_arguments(self, handler, sample_request):
        """Process request passes all task data to orchestrator."""
        headers = {"x-brevitas-mode": "balanced"}
        result = handler.process_request(**sample_request, request_headers=headers)

        call_kwargs = handler.orchestrator.process.call_args[1]
        assert call_kwargs["task_text"] == sample_request["task_text"]
        assert call_kwargs["incoming_messages"] == sample_request["incoming_messages"]
        assert call_kwargs["prior_context"] == sample_request["prior_context"]


class TestDefaultHandlerFactory:
    """Test the create_default_handler factory function."""

    def test_create_default_handler(self):
        """create_default_handler creates a ready-to-use handler."""
        handler = create_default_handler()

        assert isinstance(handler, ModeRequestHandler)
        assert handler.DEFAULT_MODE == BrevitasMode.LOSSLESS
        assert handler.customer_defaults == {}

    def test_default_handler_has_orchestrator(self):
        """Default handler has an orchestrator."""
        handler = create_default_handler()
        assert handler.orchestrator is not None
        assert isinstance(handler.orchestrator, TieredModeOrchestrator)

    def test_default_handler_mode_selection_works(self):
        """Default handler mode selection works."""
        handler = create_default_handler()
        mode = handler.extract_mode_from_request()
        assert mode == BrevitasMode.LOSSLESS
