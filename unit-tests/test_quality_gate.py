"""Semantic quality gate: a meaning-degrading compression must fall back to the original."""

import pytest

from token_efficiency_model.lossless import remote_compress, semantic_gate
from token_efficiency_model.lossless.message_optimizer import optimize_message_text

from conftest import LONG_PROMPT


def test_gate_config_defaults_and_killswitch(monkeypatch):
    monkeypatch.delenv("BREVITAS_QUALITY_MIN_SIM", raising=False)
    assert semantic_gate.min_similarity() == pytest.approx(0.75)
    assert semantic_gate.gate_enabled() is True
    monkeypatch.setenv("BREVITAS_QUALITY_MIN_SIM", "0")
    assert semantic_gate.gate_enabled() is False
    monkeypatch.setenv("BREVITAS_QUALITY_MIN_SIM", "0.90")
    assert semantic_gate.min_similarity() == pytest.approx(0.90)


def test_low_similarity_triggers_fallback(fake_remote, monkeypatch):
    # force the gate ON and force a low similarity -> compression rejected, original returned
    monkeypatch.setattr(semantic_gate, "min_similarity", lambda: 0.82)
    monkeypatch.setattr(semantic_gate, "semantic_similarity", lambda a, b: 0.40)
    out = optimize_message_text(LONG_PROMPT)
    assert out["reason"] == "quality_gate"
    assert out["text"] == LONG_PROMPT            # byte-identical fallback
    assert out["quality_sim"] == 0.40
    assert out["tokens_after"] == out["tokens_before"]


def test_high_similarity_keeps_compression(fake_remote, monkeypatch):
    monkeypatch.setattr(semantic_gate, "min_similarity", lambda: 0.82)
    monkeypatch.setattr(semantic_gate, "semantic_similarity", lambda a, b: 0.97)
    out = optimize_message_text(LONG_PROMPT)
    assert out["reason"] == "compressed"
    assert out["tokens_after"] < out["tokens_before"]
    assert out["quality_sim"] == 0.97


def test_context_ladder_is_aggressive_first_and_bounded():
    from token_efficiency_model.lossless.message_optimizer import _context_ladder
    lad = _context_ladder()
    assert lad == sorted(lad)                   # ascending == most aggressive (lowest keep) first
    assert lad[0] <= 0.5                         # starts aggressive — context is the safe part
    assert all(0.0 < r < 1.0 for r in lad)      # never lossless-only, never >1


def test_adaptive_backs_off_to_meet_quality_bar(fake_remote, monkeypatch):
    # similarity grows with how much text survives -> aggressive rate fails, a gentler rate passes
    monkeypatch.setattr(semantic_gate, "min_similarity", lambda: 0.80)
    monkeypatch.setattr(semantic_gate, "semantic_similarity",
                        lambda a, b: min(1.0, len(b) / max(1, len(a))))
    out = optimize_message_text(LONG_PROMPT)
    assert out["reason"] == "compressed"
    assert out["quality_sim"] >= 0.80          # the chosen result clears the bar
    assert out["tokens_after"] <= out["tokens_before"]
    # a backed-off (gentler than the aggressive creative default of 0.45) rate was chosen
    assert out["rate"] is not None and out["rate"] > 0.45


def test_adaptive_gives_up_when_no_rate_meets_bar(fake_remote, monkeypatch):
    monkeypatch.setattr(semantic_gate, "min_similarity", lambda: 0.999)
    monkeypatch.setattr(semantic_gate, "semantic_similarity", lambda a, b: 0.50)
    out = optimize_message_text(LONG_PROMPT)
    assert out["reason"] == "quality_gate"
    assert out["text"] == LONG_PROMPT          # byte-identical fallback
    assert out["quality_sim"] == 0.50          # reports the best it saw


def test_unmeasurable_similarity_fails_open(fake_remote, monkeypatch):
    # encoder unavailable -> similarity None -> gate must NOT block (keep compression)
    monkeypatch.setattr(semantic_gate, "min_similarity", lambda: 0.82)
    monkeypatch.setattr(semantic_gate, "semantic_similarity", lambda a, b: None)
    out = optimize_message_text(LONG_PROMPT)
    assert out["reason"] == "compressed"
    assert out["quality_sim"] is None


def test_real_encoder_scores_identical_text_as_1():
    # a genuine end-to-end check of the similarity function (uses the real MiniLM encoder)
    sim = semantic_gate.semantic_similarity("the quick brown fox", "the quick brown fox")
    if sim is None:
        pytest.skip("encoder unavailable in this environment")
    assert sim > 0.99
