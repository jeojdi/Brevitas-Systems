from dataclasses import dataclass
from typing import Dict


@dataclass
class RoutingDecision:
    model_name: str
    confidence: float
    route_fit: float


class TaskAwareRouter:
    def __init__(self, small_model: str = "llama-small", big_model: str = "llama-large"):
        self.small_model = small_model
        self.big_model = big_model

    def route(self, task_features: Dict[str, float]) -> RoutingDecision:
        complexity = task_features.get("complexity", 0.5)
        urgency = task_features.get("urgency", 0.5)
        context_load = task_features.get("context_load", 0.5)

        score = 0.55 * complexity + 0.25 * context_load + 0.20 * urgency
        if score < 0.48:
            confidence = 1.0 - score
            return RoutingDecision(model_name=self.small_model, confidence=confidence, route_fit=0.82)

        confidence = score
        return RoutingDecision(model_name=self.big_model, confidence=confidence, route_fit=0.92)
