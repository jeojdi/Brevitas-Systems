import re
from .base import BaseExpert


class ResearchExpert(BaseExpert):
    name = "research"
    anchor_regexes = [
        re.compile(r"\([A-Z][a-zA-Z]+(?:\s+et\s+al\.?)?,?\s*\d{4}\)"),    # (Author, Year) citations
        re.compile(r'"[^"]+"'),                                            # quoted findings
        re.compile(r"\bp\s*[<>=]\s*0?\.\d+\b"),                            # p-values
        re.compile(r"\bn\s*=\s*\d+\b"),                                    # sample sizes
        re.compile(r"\b\d+(?:\.\d+)?%"),                                   # percentages
        re.compile(r"\bDOI:\s*\S+", re.IGNORECASE),                        # DOIs
    ]

    @staticmethod
    def excluded_action_predicate(cfg) -> bool:
        # Exclude aggressive delta — citations and stats are unique and don't compress well
        if isinstance(cfg, dict):
            return cfg.get("delta_aggressiveness") == 3
        return getattr(cfg, "delta_aggressiveness", None) == 3
