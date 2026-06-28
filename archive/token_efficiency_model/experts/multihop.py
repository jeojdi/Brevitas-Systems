import re
from .base import BaseExpert


class MultiHopQAExpert(BaseExpert):
    name = "multihop"
    anchor_regexes = [
        re.compile(r"\b[A-Z][a-zA-Z0-9_-]+(?:\s+[A-Z][a-zA-Z0-9_-]+){0,3}\b"),
    ]

    @staticmethod
    def excluded_action_predicate(cfg) -> bool:
        if isinstance(cfg, dict):
            return cfg.get("delta_aggressiveness") == 3
        return getattr(cfg, "delta_aggressiveness", None) == 3
