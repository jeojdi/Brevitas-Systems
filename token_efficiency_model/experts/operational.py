from .base import BaseExpert


class OperationalExpert(BaseExpert):
    name = "operational"
    anchor_regexes = []  # No regex anchoring; let sampler use its defaults.
    # Inherit excluded_action_predicate (returns False — no filtering)
