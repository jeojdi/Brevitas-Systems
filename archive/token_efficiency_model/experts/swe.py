import re
from .base import BaseExpert


class SWEExpert(BaseExpert):
    name = "swe"
    anchor_regexes = [
        re.compile(r"`[^`]+`"),                                          # backticked tokens
        re.compile(r"\bdef\s+\w+|\bclass\s+\w+|\bimport\s+\w+|\bfrom\s+[\w.]+\s+import\s+[\w.,\s]+"),
        re.compile(r"\b\w+\.py:\d+|\btests?/[\w/]+\.py"),                # file:line and test paths
        re.compile(r"\b[A-Z][a-zA-Z]*(?:Error|Exception)\b"),            # error class names
    ]

    @staticmethod
    def excluded_action_predicate(cfg) -> bool:
        # Exclude aggressive compression — kills error messages and stack traces
        if isinstance(cfg, dict):
            return cfg.get("compression_level") == 3
        return getattr(cfg, "compression_level", None) == 3
