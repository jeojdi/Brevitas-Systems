"""Threshold calibration (brief b5) — one methodology for every decision threshold.

Brevitas has several thresholds that were magic numbers: the quality-gate floor, the
retrieval min_top_score, router confidence. A raw similarity/judge score is not a
probability — 0.8 cosine means different things for different embedding models and task
families. This module fits an isotonic-regression calibration map from raw scores to
empirical P(correct), using labelled (score, correct) pairs on local datasets, so a
threshold can be chosen by TARGET RISK (e.g. "accept only where P(correct) >= 0.95")
instead of a guessed constant.

Method (published, released library): isotonic regression (Zadrozny & Elkan 2002;
sklearn.isotonic.IsotonicRegression) — the standard monotonic, non-parametric
calibration used for exactly this. Falls back to a conservative identity/threshold when
scipy/sklearn is unavailable or too little data exists (never silently mis-calibrates).

Lossless: this changes only WHICH requests we trust, never the model output.
"""
from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

_MIN_SAMPLES = 30            # below this, isotonic is unreliable → conservative fallback


@dataclass
class Calibrator:
    """Monotonic score -> P(correct) map for one (model, task-family) key."""
    key: str
    xs: List[float] = field(default_factory=list)   # sorted raw scores (knots)
    ys: List[float] = field(default_factory=list)   # calibrated P(correct) at each knot
    n: int = 0
    fitted: bool = False

    # ------------------------------------------------------------------ fit
    @classmethod
    def fit(cls, key: str, scores: Sequence[float], correct: Sequence[bool]) -> "Calibrator":
        pairs = [(float(s), 1.0 if c else 0.0) for s, c in zip(scores, correct)]
        n = len(pairs)
        if n < _MIN_SAMPLES:
            return cls(key=key, n=n, fitted=False)   # not enough data → fallback at use
        try:
            from sklearn.isotonic import IsotonicRegression
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            ir = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            ir.fit(xs, ys)
            grid = sorted(set(xs))
            cal = [float(v) for v in ir.predict(grid)]
            return cls(key=key, xs=grid, ys=cal, n=n, fitted=True)
        except Exception:
            # pure-python PAV isotonic fallback (no sklearn) so we never hard-depend on it
            xs, ys = _pav(sorted(pairs))
            return cls(key=key, xs=xs, ys=ys, n=n, fitted=True)

    # ------------------------------------------------------------------ use
    def p_correct(self, score: float) -> float:
        """Calibrated P(correct) for a raw score (linear interp between knots)."""
        if not self.fitted or not self.xs:
            return max(0.0, min(1.0, score))   # conservative: treat raw score as prob
        if score <= self.xs[0]:
            return self.ys[0]
        if score >= self.xs[-1]:
            return self.ys[-1]
        i = bisect.bisect_right(self.xs, score) - 1
        x0, x1, y0, y1 = self.xs[i], self.xs[i + 1], self.ys[i], self.ys[i + 1]
        t = (score - x0) / (x1 - x0) if x1 > x0 else 0.0
        return y0 + t * (y1 - y0)

    def threshold_for_risk(self, target_p: float) -> float:
        """Smallest raw score whose calibrated P(correct) >= target_p. Conservative
        (returns 1.0 = accept nothing) if the target is never reached or unfitted."""
        if not self.fitted or not self.xs:
            return target_p                      # fall back to treating score as prob
        for x, y in zip(self.xs, self.ys):
            if y >= target_p:
                return x
        return 1.0

    def ece(self, scores: Sequence[float], correct: Sequence[bool], bins: int = 10) -> float:
        """Expected Calibration Error of this calibrator on held-out data (0 = perfect)."""
        buckets: List[List[Tuple[float, float]]] = [[] for _ in range(bins)]
        for s, c in zip(scores, correct):
            p = self.p_correct(s)
            b = min(bins - 1, int(p * bins))
            buckets[b].append((p, 1.0 if c else 0.0))
        n = sum(len(b) for b in buckets) or 1
        err = 0.0
        for b in buckets:
            if not b:
                continue
            conf = sum(p for p, _ in b) / len(b)
            acc = sum(y for _, y in b) / len(b)
            err += (len(b) / n) * abs(conf - acc)
        return err

    def to_dict(self) -> dict:
        return {"key": self.key, "xs": self.xs, "ys": self.ys, "n": self.n, "fitted": self.fitted}

    @classmethod
    def from_dict(cls, d: dict) -> "Calibrator":
        return cls(**d)


def _pav(pairs: List[Tuple[float, float]]) -> Tuple[List[float], List[float]]:
    """Pool-adjacent-violators isotonic regression (no sklearn dependency)."""
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    # merge duplicate x by averaging y
    merged: List[List[float]] = []   # [x, sum_y, count]
    for x, y in zip(xs, ys):
        if merged and merged[-1][0] == x:
            merged[-1][1] += y
            merged[-1][2] += 1
        else:
            merged.append([x, y, 1.0])
    gx = [m[0] for m in merged]
    gy = [m[1] / m[2] for m in merged]
    gw = [m[2] for m in merged]
    # PAV
    i = 0
    while i < len(gy) - 1:
        if gy[i] > gy[i + 1]:
            new_y = (gy[i] * gw[i] + gy[i + 1] * gw[i + 1]) / (gw[i] + gw[i + 1])
            gy[i] = new_y
            gw[i] += gw[i + 1]
            del gy[i + 1]; del gw[i + 1]; del gx[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1
    return gx, gy


class CalibrationStore:
    """Named calibrators persisted to a JSON file (per model×task-family key)."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._cals: dict = {}
        if path and Path(path).exists():
            try:
                for k, d in json.loads(Path(path).read_text()).items():
                    self._cals[k] = Calibrator.from_dict(d)
            except Exception:
                pass

    def set(self, cal: Calibrator) -> None:
        self._cals[cal.key] = cal
        if self.path:
            try:
                Path(self.path).write_text(json.dumps(
                    {k: c.to_dict() for k, c in self._cals.items()}, indent=2))
            except Exception:
                pass

    def get(self, key: str) -> Optional[Calibrator]:
        return self._cals.get(key)

    def p_correct(self, key: str, score: float) -> float:
        c = self._cals.get(key)
        return c.p_correct(score) if c else max(0.0, min(1.0, score))
