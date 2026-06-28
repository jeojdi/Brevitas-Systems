from dataclasses import dataclass, field
from typing import Dict, List, Any


@dataclass
class TacticConfig:
    compression_level: int = 1
    prune_budget: int = 5
    protocol_mode: str = "compact"
    use_shared_memory: bool = True
    delta_mode: str = "off"
    delta_aggressiveness: int = 1
    wire_mode: str = "json"


@dataclass
class TaskPacket:
    task_id: str
    task_text: str
    incoming_messages: List[str]
    prior_context: List[str]
    complexity: float
    urgency: float


@dataclass
class PipelineResult:
    routed_model: str
    baseline_tokens: int
    optimized_tokens: int
    steady_state_tokens: int
    cold_start_tokens: int
    savings_pct: float
    quality_proxy: float
    protocol_payload: str
    model_response: str
    debug: Dict[str, Any] = field(default_factory=dict)
