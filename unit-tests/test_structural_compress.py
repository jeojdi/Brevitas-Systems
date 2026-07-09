"""Structural compression via optimize_message_text: only Context changes, output-driving parts
stay byte-identical, criticals survive, gate is combined (semantic AND info-density)."""

import pytest

from token_efficiency_model.lossless import semantic_gate
from token_efficiency_model.lossless.message_optimizer import optimize_message_text

STRUCTURED = (
    "Write a marketing post about Brevitas.\n\n"
    "Audience: AI founders\nTone: technical\nPlatform: X\nLength: short\n\n"
    "Output JSON. Use snake_case. Never hallucinate. Cite sources.\n\n"
    "Context: Brevitas is a token-efficiency layer that sits in front of large language model "
    "providers. It compresses prompts losslessly where it can and reduces repeated context across "
    "turns, so teams cut their input-token spend without changing the model or the output returned."
)

PROTECTED_VERBATIM = [
    "Write a marketing post about Brevitas.",
    "Audience: AI founders",
    "Tone: technical",
    "Output JSON. Use snake_case. Never hallucinate. Cite sources.",
]


def test_only_context_is_rewritten(fake_remote):
    out = optimize_message_text(STRUCTURED)
    assert out["reason"] == "compressed"
    assert out["method"] == "structural+llmlingua2"
    # every output-driving line survives byte-for-byte
    for line in PROTECTED_VERBATIM:
        assert line in out["text"], line
    # and the message actually got smaller (context was compressed)
    assert out["tokens_after"] < out["tokens_before"]
    assert "context" in (out["roles"] or [])


def test_info_density_reported_and_ok(fake_remote):
    out = optimize_message_text(STRUCTURED)
    assert out["info_density"] is not None
    assert out["info_density"]["overall_ok"] is True   # criticals retained


def test_pure_instruction_has_no_context_to_compress(fake_remote):
    out = optimize_message_text("Please explain why the sky is blue in one short paragraph.")
    assert out["reason"] == "no_context"
    assert out["text"] == "Please explain why the sky is blue in one short paragraph."


def test_combined_gate_rejects_when_info_density_fails(fake_remote, monkeypatch):
    # force info-density to always fail -> even with the semantic gate off, compression is rejected
    monkeypatch.setattr("token_efficiency_model.lossless.message_optimizer.information_density",
                        lambda a, b, *args, **kw: {"overall_ok": False})
    out = optimize_message_text(STRUCTURED)
    assert out["reason"] == "quality_gate"
    assert out["text"] == STRUCTURED               # byte-identical fallback


def test_semantic_gate_still_applies(fake_remote, monkeypatch):
    monkeypatch.setattr(semantic_gate, "min_similarity", lambda: 0.95)
    monkeypatch.setattr(semantic_gate, "semantic_similarity", lambda a, b: 0.40)
    out = optimize_message_text(STRUCTURED)
    assert out["reason"] == "quality_gate"         # low similarity blocks it
    assert out["quality_sim"] == 0.40
