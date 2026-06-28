import re
from .base import BaseExpert


class MathExpert(BaseExpert):
    name = "math"
    anchor_regexes = [re.compile(r"[-+]?\d+(?:\.\d+)?")]

    @staticmethod
    def excluded_action_predicate(cfg) -> bool:
        # Exclude maximally aggressive compression and pruning to preserve
        # numeric tokens and operands. Field names per RLActionConfig:
        # compression_level and prune_budget
        try:
            compression_level = getattr(cfg, "compression_level", None)
            prune_budget = getattr(cfg, "prune_budget", None)
        except (AttributeError, TypeError):
            # Handle dict-like objects
            if isinstance(cfg, dict):
                compression_level = cfg.get("compression_level")
                prune_budget = cfg.get("prune_budget")
            else:
                return False

        return compression_level == 3 or prune_budget == 3
