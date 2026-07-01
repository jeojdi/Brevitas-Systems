"""Always-valid sequential quality test — mixture-martingale SPRT (brief b4).

Purpose: certify, per (customer, lever) stream, that the pass-rate of audited calls
stays at/above a contractual floor p0 — with a type-I error guarantee that holds at
EVERY sample size (anytime-valid), so billing can watch the stream continuously and
stop the moment evidence of degradation is strong, without peeking penalties.

Method (published, not hand-rolled): Robbins' method of mixtures / the mixture
sequential probability ratio test (mSPRT) as used for always-valid inference
(Robbins 1970; Johari, Koomen, Pekelis & Walsh — "Peeking at A/B tests", KDD'17 /
Ann. Stat. line of work; Howard et al. confidence sequences). For Bernoulli
outcomes X_i ∈ {0,1} we test

    H0: p >= p0   (quality holds)   vs   H1: p < p0

with the one-sided mixture likelihood ratio using a Beta(a, b) mixing density
restricted to [0, p0]:

    M_n = ∫_0^{p0} p^S (1-p)^{n-S} dπ(p)  /  ( p0^S (1-p0)^{n-S} )

where S = Σ X_i (passes). Under H0, M_n is a supermartingale with E[M_n] <= 1, so by
Ville's inequality P(sup_n M_n >= 1/α) <= α — rejecting when M_n >= 1/α controls the
type-I error at α for ALL n simultaneously. Computed in log space via a fixed
quadrature grid over the mixing density (deterministic, dependency-free).

Fail-safe semantics: `tripped` is sticky — once a stream trips, savings from that
lever stop being billed until a human resets the stream.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

_GRID_N = 512  # quadrature grid size; deterministic and plenty for billing use


@dataclass
class SequentialState:
    n: int = 0
    passes: int = 0
    log_m: float = 0.0            # log mixture martingale value
    tripped: bool = False
    tripped_at_n: Optional[int] = None


class SequentialQualityGate:
    """Anytime-valid one-sided test that a stream's pass-rate is >= p0.

    Args:
        p0:    contractual quality floor for the pass-rate (e.g. 0.95).
        alpha: type-I error budget over the whole (unbounded) monitoring horizon.
        a, b:  Beta mixing-density parameters over the alternative p < p0
               (defaults weight alternatives near p0, the hardest-to-detect ones).
    """

    def __init__(self, p0: float = 0.95, alpha: float = 0.05,
                 a: float = 1.0, b: float = 1.0):
        if not 0.0 < p0 < 1.0:
            raise ValueError("p0 must be in (0,1)")
        if not 0.0 < alpha < 1.0:
            raise ValueError("alpha must be in (0,1)")
        self.p0 = p0
        self.alpha = alpha
        self.state = SequentialState()
        # fixed quadrature grid over (0, p0): midpoint rule on the Beta(a,b) density
        # rescaled to [0, p0]
        self._grid: List[float] = []
        self._logw: List[float] = []
        for i in range(_GRID_N):
            u = (i + 0.5) / _GRID_N            # in (0,1)
            p = u * p0                          # rescaled to (0, p0)
            # Beta(a,b) density on u, times the 1/p0 rescale Jacobian; uniform weights
            dens = (u ** (a - 1.0)) * ((1.0 - u) ** (b - 1.0))
            self._grid.append(p)
            self._logw.append(math.log(dens / _GRID_N) if dens > 0 else -math.inf)
        # normalize mixing weights to sum to 1 (proper prior ⇒ E[M]<=1 under H0)
        tot = _logsumexp(self._logw)
        self._logw = [w - tot for w in self._logw]

    # ------------------------------------------------------------------ update
    def update(self, passed: bool) -> SequentialState:
        """Record one audited outcome; returns the new state (sticky trip)."""
        st = self.state
        if st.tripped:
            return st
        st.n += 1
        st.passes += 1 if passed else 0
        s, n = st.passes, st.n
        # log M_n = logsumexp_i [ logw_i + s*log(p_i) + (n-s)*log(1-p_i) ]
        #           - ( s*log(p0) + (n-s)*log(1-p0) )
        terms = []
        for lp, lw in zip(self._grid, self._logw):
            if lp <= 0.0 or lp >= 1.0:
                continue
            terms.append(lw + s * math.log(lp) + (n - s) * math.log(1.0 - lp))
        log_num = _logsumexp(terms)
        log_den = s * math.log(self.p0) + (n - s) * math.log(1.0 - self.p0)
        st.log_m = log_num - log_den
        if st.log_m >= math.log(1.0 / self.alpha):
            st.tripped = True
            st.tripped_at_n = st.n
        return st

    # -------------------------------------------------------------- serialization
    def to_dict(self) -> dict:
        st = self.state
        return {"p0": self.p0, "alpha": self.alpha, "n": st.n, "passes": st.passes,
                "log_m": st.log_m, "tripped": st.tripped, "tripped_at_n": st.tripped_at_n}

    @classmethod
    def from_dict(cls, d: dict) -> "SequentialQualityGate":
        g = cls(p0=d["p0"], alpha=d["alpha"])
        g.state = SequentialState(n=d["n"], passes=d["passes"], log_m=d["log_m"],
                                  tripped=d["tripped"], tripped_at_n=d.get("tripped_at_n"))
        return g


def _logsumexp(xs: List[float]) -> float:
    m = max((x for x in xs if x != -math.inf), default=-math.inf)
    if m == -math.inf:
        return -math.inf
    return m + math.log(sum(math.exp(x - m) for x in xs if x != -math.inf))
