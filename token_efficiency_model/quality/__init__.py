"""Billing-grade quality verification (design brief b4).

Three layers, all grounded in published statistics — never hand-rolled heuristics:
  1. `gate`       — judge protocol (temperature-0 LLM judge with position-swap
                    debiasing + local embedding similarity; calibrator hook for b5).
  2. `sequential` — always-valid sequential test (mixture-martingale SPRT) that
                    certifies "quality unchanged" per (customer, lever) stream with
                    controlled type-I error, and trips BEFORE further billing when
                    quality degrades.
  3. `audit`      — deterministic hash-based audit sampling: only a reproducible
                    sample of calls pays the reference-answer cost.

House rules: unverified savings are NEVER billed; every layer fails safe (gate
unavailable ⇒ status "unverified" ⇒ $0 fee, requests untouched).
"""
from .audit import AuditPolicy
from .gate import QualityAssessment, QualityGate, QualityGateConfig
from .sequential import SequentialQualityGate

__all__ = ["AuditPolicy", "QualityAssessment", "QualityGate", "QualityGateConfig",
           "SequentialQualityGate"]
