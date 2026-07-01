"""Deterministic audit sampling (brief b4).

Only a sample of optimized calls pays the verification cost (reference answer +
judge). Sampling is HASH-BASED on the request id, not random: the same request id
always yields the same decision, so a customer (or an auditor) can reproduce exactly
which calls were audited — reproducibility is part of the billing-credibility story.

Rates follow the brief's guidance: small streams audit more (worst case every call),
large streams need only a thin slice for the sequential test to have power.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

_DENOM = 2 ** 32


@dataclass
class AuditPolicy:
    """Decide whether a given call is audited.

    Args:
        rate: target audit fraction in (0, 1]. 0.10 = audit ~10% of calls.
        min_first_n: always audit the first N calls of a stream (cold streams need
                     data before the sequential gate has any power).
    """
    rate: float = 0.10
    min_first_n: int = 10

    def should_audit(self, request_id: str, stream_n: int = 0) -> bool:
        """stream_n = number of calls seen so far on this (customer, lever) stream."""
        if stream_n < self.min_first_n:
            return True
        h = int.from_bytes(hashlib.sha256(request_id.encode("utf-8")).digest()[:4], "big")
        return (h / _DENOM) < self.rate
