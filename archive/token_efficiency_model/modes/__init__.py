"""Tiered optimization modes for Brevitas Phase 4.

Modes compose existing Phase 1-3 components into three tiers:
- lossless: native cache + RLM retrieval (no lossy compression)
- balanced: cache + retrieval + light tail compression + quality gate
- max_savings: cache + retrieval + aggressive lossy compression + quality gate

Components:
- tiered_orchestrator: Core mode orchestration (composes Phase 1-3)
- request_handler: HTTP request handling with mode selection
"""

from .tiered_orchestrator import (
    BrevitasMode,
    ModeConfig,
    TieredModeOrchestrator,
    ModeResult,
)
from .request_handler import (
    ModeRequestHandler,
    create_default_handler,
)

__all__ = [
    "BrevitasMode",
    "ModeConfig",
    "TieredModeOrchestrator",
    "ModeResult",
    "ModeRequestHandler",
    "create_default_handler",
]
