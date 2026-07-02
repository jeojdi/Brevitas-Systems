"""Calibration layer (brief b5): isotonic score->P(correct) maps, risk-based
thresholds, ECE, conservative fallback. Deterministic."""
from __future__ import annotations

import random

from token_efficiency_model.quality.calibration import CalibrationStore, Calibrator


def _synthetic(n=400, seed=0):
    """Scores where true P(correct) rises monotonically with score (miscalibrated:
    raw score overstates correctness at the low end)."""
    rng = random.Random(seed)
    scores, correct = [], []
    for _ in range(n):
        s = rng.random()
        p_true = s ** 2           # true correctness is score^2 (score overstates it)
        scores.append(s)
        correct.append(rng.random() < p_true)
    return scores, correct


def test_isotonic_is_monotonic_and_improves_ece():
    scores, correct = _synthetic()
    cal = Calibrator.fit("minilm/qa", scores, correct)
    assert cal.fitted
    # calibrated map is monotonic non-decreasing
    ps = [cal.p_correct(x) for x in sorted(scores)]
    assert all(b >= a - 1e-9 for a, b in zip(ps, ps[1:]))
    # calibrated ECE beats treating the raw score as a probability
    raw = Calibrator(key="raw", fitted=False)
    hs, hc = _synthetic(n=400, seed=1)          # held-out
    assert cal.ece(hs, hc) < raw.ece(hs, hc)


def test_threshold_for_risk_is_conservative():
    scores, correct = _synthetic(n=600)
    cal = Calibrator.fit("k", scores, correct)
    # to accept only >=90%-correct requests, the raw-score threshold must be high,
    # since true P(correct)=score^2 => need score ~ sqrt(0.9) ~ 0.95
    thr = cal.threshold_for_risk(0.9)
    assert 0.85 <= thr <= 1.0
    # everything at/above the threshold really is >=~0.9 correct
    assert cal.p_correct(thr) >= 0.9 - 0.1


def test_unfitted_falls_back_to_raw_score():
    cal = Calibrator.fit("tiny", [0.5, 0.6], [True, False])   # < _MIN_SAMPLES
    assert not cal.fitted
    assert abs(cal.p_correct(0.7) - 0.7) < 1e-9
    assert abs(cal.threshold_for_risk(0.8) - 0.8) < 1e-9      # conservative


def test_pav_fallback_matches_monotonic_when_sklearn_missing(monkeypatch):
    # force the pure-python PAV path
    import builtins
    real_import = builtins.__import__

    def no_sklearn(name, *a, **k):
        if name.startswith("sklearn"):
            raise ImportError("blocked")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_sklearn)
    scores, correct = _synthetic(n=300)
    cal = Calibrator.fit("nosk", scores, correct)
    assert cal.fitted
    ps = [cal.p_correct(x) for x in sorted(scores)]
    assert all(b >= a - 1e-9 for a, b in zip(ps, ps[1:]))     # still monotonic


def test_store_roundtrip(tmp_path):
    p = str(tmp_path / "cal.json")
    store = CalibrationStore(p)
    scores, correct = _synthetic()
    store.set(Calibrator.fit("minilm/qa", scores, correct))
    reloaded = CalibrationStore(p)
    assert reloaded.get("minilm/qa") is not None
    assert reloaded.get("minilm/qa").fitted
    # unknown key falls back to raw score
    assert abs(reloaded.p_correct("unknown", 0.42) - 0.42) < 1e-9
