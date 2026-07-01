"""Billing-gate tests (brief b4): sequential mSPRT, audit sampling, judge protocol,
store idempotency. Deterministic — no live API calls, no randomness without seeds."""
from __future__ import annotations

import tempfile
from pathlib import Path

from token_efficiency_model.quality.audit import AuditPolicy
from token_efficiency_model.quality.gate import QualityGate, QualityGateConfig
from token_efficiency_model.quality.sequential import SequentialQualityGate


# --------------------------------------------------------------------------- #
# sequential mSPRT
# --------------------------------------------------------------------------- #
def test_healthy_stream_never_trips():
    # Exactly-at-floor stream: repeating 9 passes + 1 fail = 90% at p0=0.9.
    g = SequentialQualityGate(p0=0.9, alpha=0.05)
    for i in range(1000):
        g.update(i % 10 != 9)
    assert not g.state.tripped
    # a clearly-above-floor stream stays far from the boundary
    g2 = SequentialQualityGate(p0=0.9, alpha=0.05)
    for _ in range(500):
        g2.update(True)
    assert not g2.state.tripped


def test_degraded_stream_trips_quickly_and_stays_tripped():
    # 60% pass rate against a 90% floor must trip fast.
    g = SequentialQualityGate(p0=0.9, alpha=0.05)
    seq = [True, True, True, False, False] * 40   # 60% passes, deterministic
    for i, x in enumerate(seq, 1):
        g.update(x)
        if g.state.tripped:
            break
    assert g.state.tripped, "60% pass-rate stream never tripped a 90% floor"
    assert g.state.tripped_at_n is not None and g.state.tripped_at_n <= 100
    # sticky: further passes cannot untrip it
    n_at_trip = g.state.n
    for _ in range(50):
        g.update(True)
    assert g.state.tripped and g.state.n == n_at_trip


def test_serialization_roundtrip():
    g = SequentialQualityGate(p0=0.95, alpha=0.01)
    for x in [True, False, True, True]:
        g.update(x)
    g2 = SequentialQualityGate.from_dict(g.to_dict())
    assert g2.state == g.state
    assert (g2.p0, g2.alpha) == (g.p0, g.alpha)


# --------------------------------------------------------------------------- #
# audit sampling
# --------------------------------------------------------------------------- #
def test_audit_is_deterministic_and_hits_target_rate():
    pol = AuditPolicy(rate=0.10, min_first_n=0)
    ids = [f"req-{i}" for i in range(20_000)]
    picks = [pol.should_audit(i, stream_n=100) for i in ids]
    assert picks == [pol.should_audit(i, stream_n=100) for i in ids]  # reproducible
    rate = sum(picks) / len(picks)
    assert 0.085 <= rate <= 0.115, f"empirical rate {rate}"


def test_audit_first_n_always_sampled():
    pol = AuditPolicy(rate=0.0, min_first_n=10)
    assert all(pol.should_audit(f"r{i}", stream_n=i) for i in range(10))
    assert not any(pol.should_audit(f"r{i}", stream_n=i) for i in range(10, 200))


# --------------------------------------------------------------------------- #
# judge protocol
# --------------------------------------------------------------------------- #
def test_gate_degraded_never_passes(monkeypatch):
    gate = QualityGate(QualityGateConfig())
    monkeypatch.setattr(gate, "judge_key", "")          # no judge available
    monkeypatch.setattr(gate, "_embedding_similarity", lambda a, b: 0.99)
    a = gate.assess("same answer", "same answer", "q?")
    assert a.degraded and not a.passed and a.judge_score is None


def test_gate_position_swap_averages_two_judge_calls(monkeypatch):
    gate = QualityGate(QualityGateConfig(position_swap=True, floor=0.8))
    monkeypatch.setattr(gate, "judge_key", "fake")
    monkeypatch.setattr(gate, "_embedding_similarity", lambda a, b: 1.0)
    calls = []

    def fake_once(a, b, q):
        calls.append((a, b))
        return (0.9, "ok") if len(calls) == 1 else (0.7, "ok")

    monkeypatch.setattr(gate, "_judge_once", fake_once)
    a = gate.assess("opt", "ref", "q?")
    assert len(calls) == 2
    assert calls[0] == ("ref", "opt") and calls[1] == ("opt", "ref")  # swapped
    assert abs(a.judge_score - 0.8) < 1e-9                             # averaged
    assert abs(a.score - (0.5 * 1.0 + 0.5 * 0.8)) < 1e-9
    assert a.passed


def test_gate_custom_combiner_hook(monkeypatch):
    cfg = QualityGateConfig(combiner=lambda e, j: j)  # judge-only combiner (b5 hook)
    gate = QualityGate(cfg)
    monkeypatch.setattr(gate, "judge_key", "fake")
    monkeypatch.setattr(gate, "_embedding_similarity", lambda a, b: 0.0)
    monkeypatch.setattr(gate, "_judge_once", lambda a, b, q: (0.95, "ok"))
    a = gate.assess("opt", "ref", "q?")
    assert a.score == 0.95 and a.passed


# --------------------------------------------------------------------------- #
# store idempotency + complete records
# --------------------------------------------------------------------------- #
def test_store_request_id_idempotency_and_receipt_columns():
    from api.store import UsageStore
    with tempfile.TemporaryDirectory() as td:
        store = UsageStore(db_path=str(Path(td) / "t.db"))
        store.create_key("k", "t")
        assert not store.has_request("k", "req-1")
        store.record_usage(key_hash="k", baseline_tokens=100, optimized_tokens=100,
                           savings_pct=0.0, quality_proxy=None, request_id="req-1",
                           usage_raw='{"prompt_tokens": 100}',
                           quality_status="unverified", strategy="cache_only")
        assert store.has_request("k", "req-1")
        assert not store.has_request("k", "req-2")
        # zero-savings rows ARE recorded (complete audit log)
        stats = store.get_stats("k")
        assert stats["total_calls"] == 1 and stats["total_tokens_saved"] == 0
