"""Request handling layer for mode selection and orchestration.

Wires mode selection from HTTP headers/request fields and applies tiered optimization.
"""

from typing import Dict, Any, Optional, List
import logging

from .tiered_orchestrator import (
    BrevitasMode,
    ModeConfig,
    TieredModeOrchestrator,
    ModeResult,
)


logger = logging.getLogger(__name__)


class ModeRequestHandler:
    """Handles mode selection and request processing."""

    # Default mode for all customers
    DEFAULT_MODE = BrevitasMode.LOSSLESS

    def __init__(
        self,
        orchestrator: Optional[TieredModeOrchestrator] = None,
        customer_defaults: Optional[Dict[str, BrevitasMode]] = None,
    ):
        """
        Initialize the request handler.

        Args:
            orchestrator: TieredModeOrchestrator instance (created if None).
            customer_defaults: Per-customer default modes (customer_id -> mode).
        """
        self.orchestrator = orchestrator or TieredModeOrchestrator()
        self.customer_defaults = customer_defaults or {}

    def extract_mode_from_request(
        self,
        request_headers: Optional[Dict[str, str]] = None,
        request_body: Optional[Dict[str, Any]] = None,
        customer_id: Optional[str] = None,
    ) -> BrevitasMode:
        """
        Extract the optimization mode from request headers/body or customer default.

        Precedence:
        1. x-brevitas-mode header (if present)
        2. mode field in request body (if present)
        3. customer default (if customer_id provided and customer has a default)
        4. global default (lossless)

        Args:
            request_headers: HTTP headers dict (e.g., {"x-brevitas-mode": "balanced"}).
            request_body: Request body dict (e.g., {"mode": "max_savings"}).
            customer_id: Customer identifier for looking up per-customer defaults.

        Returns:
            BrevitasMode to use for this request.
        """
        # 1. Check header
        if request_headers:
            mode_str = request_headers.get("x-brevitas-mode", "").strip().lower()
            if mode_str:
                mode = self._parse_mode_string(mode_str)
                if mode:
                    logger.info(f"Mode from header: {mode.value}")
                    return mode

        # 2. Check request body
        if request_body:
            mode_str = request_body.get("mode", "").strip().lower()
            if mode_str:
                mode = self._parse_mode_string(mode_str)
                if mode:
                    logger.info(f"Mode from request body: {mode.value}")
                    return mode

        # 3. Check customer default
        if customer_id and customer_id in self.customer_defaults:
            mode = self.customer_defaults[customer_id]
            logger.info(f"Mode from customer default (customer_id={customer_id}): {mode.value}")
            return mode

        # 4. Use global default
        logger.info(f"Mode: using global default {self.DEFAULT_MODE.value}")
        return self.DEFAULT_MODE

    def set_customer_default(self, customer_id: str, mode: BrevitasMode) -> None:
        """Set the default mode for a customer."""
        self.customer_defaults[customer_id] = mode
        logger.info(f"Set customer default (customer_id={customer_id}): {mode.value}")

    def get_customer_default(self, customer_id: str) -> Optional[BrevitasMode]:
        """Get the default mode for a customer."""
        return self.customer_defaults.get(customer_id)

    def process_request(
        self,
        task_text: str,
        incoming_messages: List[str],
        prior_context: List[str],
        request_headers: Optional[Dict[str, str]] = None,
        request_body: Optional[Dict[str, Any]] = None,
        customer_id: Optional[str] = None,
    ) -> ModeResult:
        """
        Process a request with mode selection and optimization.

        Args:
            task_text: The task/question.
            incoming_messages: List of incoming message strings.
            prior_context: List of prior context chunks.
            request_headers: HTTP headers (may contain x-brevitas-mode).
            request_body: Request body (may contain mode field).
            customer_id: Customer identifier (for default mode lookup).

        Returns:
            ModeResult with optimized content and quality assessment.
        """
        # Determine mode
        mode = self.extract_mode_from_request(
            request_headers=request_headers,
            request_body=request_body,
            customer_id=customer_id,
        )

        # Extract per-request config overrides from request_body (if any)
        config = self._build_mode_config(mode, request_body)

        # Process through orchestrator
        result = self.orchestrator.process(
            task_text=task_text,
            incoming_messages=incoming_messages,
            prior_context=prior_context,
            config=config,
        )

        return result

    def _parse_mode_string(self, mode_str: str) -> Optional[BrevitasMode]:
        """Parse a mode string to BrevitasMode enum."""
        try:
            return BrevitasMode(mode_str)
        except ValueError:
            logger.warning(f"Invalid mode string: '{mode_str}'. Using default: {self.DEFAULT_MODE.value}")
            return None

    def _build_mode_config(
        self,
        mode: BrevitasMode,
        request_body: Optional[Dict[str, Any]] = None,
    ) -> ModeConfig:
        """
        Build ModeConfig for a given mode, with optional overrides from request body.

        Args:
            mode: The BrevitasMode to configure.
            request_body: Request body that may contain config overrides.

        Returns:
            ModeConfig appropriate for the mode.
        """
        # Default settings per mode
        if mode == BrevitasMode.LOSSLESS:
            config = ModeConfig(
                mode=mode,
                compression_level=1,
                prune_budget=5,
                quality_floor=0.8,
                apply_quality_gate=False,
                fallback_to_full_on_gate_fail=False,
                enable_rlm_retrieval=True,
            )
        elif mode == BrevitasMode.BALANCED:
            config = ModeConfig(
                mode=mode,
                compression_level=1,  # light
                prune_budget=5,
                quality_floor=0.8,
                apply_quality_gate=True,
                fallback_to_full_on_gate_fail=True,
                enable_rlm_retrieval=True,
            )
        else:  # MAX_SAVINGS
            config = ModeConfig(
                mode=mode,
                compression_level=3,  # aggressive
                prune_budget=3,
                quality_floor=0.8,
                apply_quality_gate=True,
                fallback_to_full_on_gate_fail=True,
                enable_rlm_retrieval=True,
            )

        # Apply overrides from request_body if present
        if request_body and isinstance(request_body, dict):
            if "compression_level" in request_body:
                config.compression_level = int(request_body["compression_level"])
            if "prune_budget" in request_body:
                config.prune_budget = int(request_body["prune_budget"])
            if "quality_floor" in request_body:
                config.quality_floor = float(request_body["quality_floor"])
            if "enable_rlm_retrieval" in request_body:
                config.enable_rlm_retrieval = bool(request_body["enable_rlm_retrieval"])

        return config


def create_default_handler() -> ModeRequestHandler:
    """Create a default ModeRequestHandler with global defaults."""
    return ModeRequestHandler(
        orchestrator=TieredModeOrchestrator(),
        customer_defaults={},
    )
