import re
from .base import BaseExpert


class LogicalDeductionExpert(BaseExpert):
    name = "logical"
    anchor_regexes = [
        re.compile(r"\b(if|then|therefore|implies|because|hence|thus|iff|modus\s+ponens)\b", re.IGNORECASE),
    ]
    # No action filter — leave the full action space available.
