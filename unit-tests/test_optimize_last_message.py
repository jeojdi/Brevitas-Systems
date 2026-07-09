"""The server-side helper `optimize_message_text` and its fail-safe reason codes."""

from token_efficiency_model.lossless import remote_compress
from token_efficiency_model.lossless.message_optimizer import optimize_message_text

from conftest import LONG_PROMPT


def test_remote_unavailable_is_lossless_passthrough(monkeypatch):
    monkeypatch.setattr(remote_compress, "remote_available", lambda: False)
    out = optimize_message_text(LONG_PROMPT)
    assert out["reason"] == "remote_unavailable"
    assert out["text"] == LONG_PROMPT              # byte-identical
    assert out["method"] == "lossless"
    assert out["tokens_after"] == out["tokens_before"]


def test_empty_message(monkeypatch):
    monkeypatch.setattr(remote_compress, "remote_available", lambda: True)
    out = optimize_message_text("   ")
    assert out["reason"] == "empty"


def test_compressed_when_remote_present(fake_remote):
    out = optimize_message_text(LONG_PROMPT)
    assert out["reason"] == "compressed"
    assert out["tokens_after"] < out["tokens_before"]
    assert out["method"] == "structural+llmlingua2"


def test_remote_error_is_distinguished_from_too_short(monkeypatch):
    # remote is "available" but every call fails -> reason must be remote_error, text unchanged
    monkeypatch.setattr(remote_compress, "remote_available", lambda: True)
    monkeypatch.setattr(remote_compress, "remote_optimize",
                        lambda *a, **k: None)
    out = optimize_message_text(LONG_PROMPT)
    assert out["reason"] == "remote_error"
    assert out["text"] == LONG_PROMPT
    assert out["method"] == "lossless"
