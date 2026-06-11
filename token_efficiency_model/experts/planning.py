import re
from .base import BaseExpert

class PlanningExpert(BaseExpert):
    name = "planning"
    anchor_regexes = [
        re.compile(r"\b(first|second|third|then|finally|next|step\s*\d+|precondition)\b", re.IGNORECASE),
    ]
    # No action filter.
